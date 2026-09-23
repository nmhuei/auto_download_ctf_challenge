"""Burp Suite MCP and Proxy integration service.

Allows auto-detecting running Burp Suite instances on localhost:9876 (MCP Server)
and localhost:8080 (Proxy), extracting captured authentication session cookies
directly from Burp Proxy HTTP history without manual copy-pasting, and optionally
routing traffic through Burp Proxy.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

DEFAULT_BURP_MCP_PORT = 9876
DEFAULT_BURP_PROXY_PORT = 8080


def _normalize_domain(domain_or_url: str) -> str:
    """Extract clean domain name without scheme, port, or path."""
    val = domain_or_url.strip().lower()
    if "://" not in val:
        val = f"http://{val}"
    parsed = urlparse(val)
    host = parsed.hostname or val
    return host.lower()


class BurpService:
    """Client for Burp Suite Model Context Protocol (MCP) server & HTTP Proxy."""

    def __init__(self, host: str = "127.0.0.1", mcp_port: int = DEFAULT_BURP_MCP_PORT, proxy_port: int = DEFAULT_BURP_PROXY_PORT):
        self.host = host
        self.mcp_port = mcp_port
        self.proxy_port = proxy_port

    def is_mcp_available(self, timeout: float = 0.6) -> bool:
        """Check if Burp Suite MCP SSE server is listening."""
        try:
            with socket.create_connection((self.host, self.mcp_port), timeout=timeout):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    def is_proxy_available(self, timeout: float = 0.6) -> bool:
        """Check if Burp Suite HTTP proxy is listening."""
        try:
            with socket.create_connection((self.host, self.proxy_port), timeout=timeout):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.proxy_port}"

    def extract_cookies(self, target_url_or_domain: str, count: int = 50, timeout: float = 4.0) -> Dict[str, str]:
        """Extract clean session cookies for the given domain from Burp Proxy HTTP History.

        Connects to Burp MCP server over SSE, queries get_proxy_http_history,
        and parses Cookie and Set-Cookie headers for matching requests/responses.
        """
        if not self.is_mcp_available(timeout=0.5):
            return {}

        domain = _normalize_domain(target_url_or_domain)
        base_url = f"http://{self.host}:{self.mcp_port}"

        session_id_holder: List[str] = []
        responses: Dict[int, dict] = {}
        stop_event = threading.Event()

        def _sse_reader():
            try:
                resp = requests.get(f"{base_url}/", stream=True, timeout=timeout)
                for line in resp.iter_lines():
                    if stop_event.is_set():
                        break
                    if not line:
                        continue
                    line_str = line.decode(errors="ignore")
                    if line_str.startswith("data: ?sessionId="):
                        sid = line_str.split("sessionId=")[1].strip()
                        session_id_holder.append(sid)
                    elif line_str.startswith("data: "):
                        try:
                            payload = json.loads(line_str[6:])
                            req_id = payload.get("id")
                            if req_id is not None:
                                responses[req_id] = payload
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Burp MCP SSE stream closed or timed out: %s", e)

        thread = threading.Thread(target=_sse_reader, daemon=True)
        thread.start()

        # 1. Wait for sessionId
        wait_deadline = time.time() + 1.5
        while time.time() < wait_deadline and not session_id_holder:
            time.sleep(0.05)

        if not session_id_holder:
            stop_event.set()
            return {}

        session_id = session_id_holder[0]
        rpc_url = f"{base_url}/?sessionId={session_id}"

        try:
            # 2. Initialize handshake
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ctf-toolkit", "version": "1.0"},
                },
            }
            requests.post(rpc_url, json=init_req, timeout=1.5)

            # Wait for init ACK
            ack_deadline = time.time() + 1.0
            while time.time() < ack_deadline and 1 not in responses:
                time.sleep(0.05)

            # Notify initialized
            requests.post(rpc_url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=1.0)

            # 3. Call get_proxy_http_history
            history_req = {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "get_proxy_http_history",
                    "arguments": {"count": count, "offset": 0},
                },
            }
            requests.post(rpc_url, json=history_req, timeout=2.0)

            # Wait for response 10
            resp_deadline = time.time() + 2.0
            while time.time() < resp_deadline and 10 not in responses:
                time.sleep(0.05)

            call_resp = responses.get(10, {})
            if "error" in call_resp:
                logger.debug("Burp MCP returned JSON-RPC error: %s", call_resp.get("error"))
                return {}

            content = call_resp.get("result", {}).get("content", [])
            if not content:
                return {}

            all_extracted: Dict[str, str] = {}
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    raw_text = block.get("text", "")
                    if raw_text:
                        block_cookies = self._parse_cookies_from_raw_history(raw_text, domain)
                        all_extracted.update(block_cookies)
            return all_extracted
        except Exception as e:
            logger.debug("Failed querying Burp MCP history: %s", e)
            return {}
        finally:
            stop_event.set()

    def _parse_cookies_from_raw_history(self, raw_text: str, domain: str) -> Dict[str, str]:
        """Parse HTTP history entries from Burp and filter cookies by domain with strict scoping."""
        raw_stripped = raw_text.strip()
        parsed_entries = []

        if raw_stripped.startswith("[") and raw_stripped.endswith("]"):
            try:
                loaded = json.loads(raw_stripped)
                if isinstance(loaded, list):
                    parsed_entries = loaded
            except Exception:
                pass

        if not parsed_entries:
            for chunk in re.split(r"(?=\{\"request\":)", raw_text):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    obj = json.loads(chunk)
                    if isinstance(obj, dict):
                        parsed_entries.append(obj)
                except Exception:
                    pass

        exact_extracted: Dict[str, str] = {}
        sub_extracted: Dict[str, str] = {}

        # Known noise cookie keys that are NOT authentication cookies
        ignored_keys = {
            "expires", "max-age", "path", "domain", "samesite", "secure",
            "httponly", "priority", "mode", "desc", "dur", "ma", "url",
            "q", "v", "charset"
        }

        for item in parsed_entries:
            try:
                req = item.get("request", "")
                res = item.get("response", "")

                # Verify target domain matches host header strictly:
                # Target host must exactly match domain or be a strict subdomain of domain.
                # Cross-domain / parent-domain matching is disallowed to prevent cookie leakage.
                is_exact = False
                is_sub = False
                req_head = req.split("\r\n\r\n")[0]
                for line in req_head.split("\r\n"):
                    if line.lower().startswith("host:"):
                        host_val = line.split(":", 1)[1].strip().split(":")[0].lower()
                        if host_val == domain:
                            is_exact = True
                            break
                        elif host_val.endswith(f".{domain}"):
                            is_sub = True
                            break

                if not is_exact and not is_sub:
                    continue

                target_dict = exact_extracted if is_exact else sub_extracted

                # 1. Extract from request Cookie: header
                for line in req_head.split("\r\n"):
                    if line.lower().startswith("cookie:"):
                        cookie_line = line.split(":", 1)[1].strip()
                        for pair in cookie_line.split(";"):
                            if "=" in pair:
                                k, v = pair.strip().split("=", 1)
                                k_clean = k.strip()
                                v_clean = v.strip().strip('"')
                                if k_clean.lower() not in ignored_keys and re.match(r"^[a-zA-Z0-9_\-]+$", k_clean):
                                    target_dict[k_clean] = v_clean

                # 2. Extract from response Set-Cookie: headers (latest response wins)
                res_head = res.split("\r\n\r\n")[0]
                for line in res_head.split("\r\n"):
                    if line.lower().startswith("set-cookie:"):
                        cookie_line = line.split(":", 1)[1].strip()
                        first_pair = cookie_line.split(";")[0]
                        if "=" in first_pair:
                            k, v = first_pair.strip().split("=", 1)
                            k_clean = k.strip()
                            v_clean = v.strip().strip('"')
                            if k_clean.lower() not in ignored_keys and re.match(r"^[a-zA-Z0-9_\-]+$", k_clean):
                                target_dict[k_clean] = v_clean
            except Exception:
                pass

        # Exact host matches always take strict precedence over subdomains
        final_cookies = dict(sub_extracted)
        final_cookies.update(exact_extracted)
        return final_cookies

    def get_cookie_header(self, target_url_or_domain: str, count: int = 50, timeout: float = 4.0) -> Optional[str]:
        """Convenience method returning formatted Cookie header string (e.g. 'key1=val1; key2=val2')."""
        cookies = self.extract_cookies(target_url_or_domain, count=count, timeout=timeout)
        if not cookies:
            return None
        return "; ".join(f"{k}={v}" for k, v in cookies.items())
