"""Shared HTTP session with safe retries and adaptive Cloudflare transport."""
from __future__ import annotations

import json
import threading
from http.cookiejar import CookieJar
from typing import Any, Dict, Optional, Set, Union
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_CF_CHALLENGE_STATUSES = frozenset({403, 429, 503})
_CF_BODY_MARKERS = (
    "cf-chl-",
    "/cdn-cgi/challenge-platform/",
    "just a moment...",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "cloudflare ray id",
)
_BROWSER_MANAGED_HEADERS = frozenset({
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "connection",
    "upgrade-insecure-requests",
})
_CROSS_ORIGIN_SAFE_SESSION_HEADERS = frozenset({
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "connection",
    "upgrade-insecure-requests",
    "cache-control",
    "pragma",
})


def _normalized_origin(url: Optional[str]) -> Optional[str]:
    """Return scheme://host[:port] for HTTP(S), or None for invalid input."""
    if not url:
        return None
    try:
        parsed = urlparse(str(url))
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        if scheme not in ("http", "https") or not host:
            return None
        port = parsed.port
        default_port = 443 if scheme == "https" else 80
        host_text = f"[{host}]" if ":" in host else host
        return f"{scheme}://{host_text}" + (
            f":{port}" if port is not None and port != default_port else ""
        )
    except (TypeError, ValueError):
        return None


def _cookie_scope(url: Optional[str]) -> tuple[Optional[str], bool]:
    if not url:
        return None, False
    try:
        parsed = urlparse(str(url))
        host = (parsed.hostname or "").lower().rstrip(".") or None
        # Python's http.cookiejar canonicalizes requests to bare localhost as
        # localhost.local; a Domain=localhost cookie is never returned. Use the
        # canonical local-only form instead of a domainless cookie (which would
        # leak to every host in the session).
        if host == "localhost":
            host = "localhost.local"
        return host, parsed.scheme.lower() == "https"
    except (TypeError, ValueError):
        return None, False


def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """Parse a Cookie header / JSON object / raw session token."""
    if not cookie_str:
        return {}

    cookie_str = cookie_str.strip()

    if cookie_str.startswith("{") and cookie_str.endswith("}"):
        try:
            parsed = json.loads(cookie_str)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            pass

    cookies: Dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, val = part.split("=", 1)
            cookies[key.strip()] = val.strip()
        elif "session" not in cookies:
            cookies["session"] = part
    return cookies


try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.exceptions import RequestException as CurlRequestException
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    CurlRequestException = None
    HAS_CURL_CFFI = False


def _header_text(response: Any, name: str) -> str:
    """Return a response header as lowercase text without trusting mocks."""
    try:
        value = (getattr(response, "headers", {}) or {}).get(name)
    except Exception:
        return ""
    if isinstance(value, bytes):
        return value.decode("latin1", "replace").strip().lower()
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def is_cloudflare_proxy_response(response: Any) -> bool:
    """True when the response demonstrably traversed Cloudflare."""
    if _header_text(response, "cf-mitigated") == "challenge":
        return True
    if _header_text(response, "cf-ray"):
        return True
    return "cloudflare" in _header_text(response, "server")


class CloudflareChallengeError(requests.RequestException):
    """Managed/interstitial Cloudflare challenge still blocks safe preflight."""


def is_cloudflare_challenge(response: Any, *, inspect_body: bool = True) -> bool:
    """Detect a Cloudflare Challenge Page without treating every CF 4xx as one."""
    if _header_text(response, "cf-mitigated") == "challenge":
        return True

    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status not in _CF_CHALLENGE_STATUSES:
        return False
    if not is_cloudflare_proxy_response(response):
        return False
    if not inspect_body:
        return False

    try:
        text = getattr(response, "text", "")
    except Exception:
        text = ""
    if not isinstance(text, str):
        return False
    lower = text[:512 * 1024].lower()
    return any(marker in lower for marker in _CF_BODY_MARKERS)


class CloudflareAdaptiveSession(requests.Session):
    """requests.Session that upgrades to browser TLS/HTTP fingerprints on CF.

    Normal traffic still uses requests and its urllib3 retry policy. Once a
    response proves the origin is behind Cloudflare, subsequent requests use a
    lazily-created curl_cffi browser session. If the first response is itself a
    Challenge Page, only idempotent methods are replayed.

    A mutating request is never automatically replayed after it was sent.
    """

    SAFE_REPLAY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(
        self,
        *,
        impersonate: str = "chrome",
        cloudflare_fallback: bool = True,
    ) -> None:
        super().__init__()
        self._cf_impersonate = impersonate
        self._cf_fallback = bool(cloudflare_fallback)
        self._cf_seen = False
        self._cf_active = False
        self._cf_browser_session: Any = None
        self._cf_foreign_sessions: Dict[str, Any] = {}
        self._cf_lock = threading.RLock()
        self._cf_warned_unavailable = False
        self._cf_warned_persistent_challenge = False
        self._credential_origin: Optional[str] = None
        self._mutation_preflight_ok: Set[str] = set()

    @property
    def cloudflare_seen(self) -> bool:
        return self._cf_seen

    @property
    def cloudflare_active(self) -> bool:
        return self._cf_active

    @property
    def credential_origin(self) -> Optional[str]:
        return self._credential_origin

    def _scope_request_kwargs(self, url: str,
                              kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Strip inherited platform credentials outside their owning origin.

        Explicit per-request headers are treated as an intentional override;
        only credentials inherited from ``session.headers`` are nulled. Cookie
        leakage is prevented separately by domain-scoping CLI/auth-map cookies
        when the session is created.
        """
        owner = self._credential_origin
        target = _normalized_origin(url)
        if not owner or not target or target == owner:
            return kwargs

        out = dict(kwargs)
        raw_headers = out.get("headers")
        request_headers = dict(raw_headers or {})
        explicit = {str(k).lower() for k in request_headers}
        for key in self.headers.keys():
            low = str(key).lower()
            if low in _CROSS_ORIGIN_SAFE_SESSION_HEADERS or low in explicit:
                continue
            request_headers[str(key)] = None

        # Cookie jars are hostname/path scoped, not origin/port scoped. A
        # platform cookie for 127.0.0.1:8000 would otherwise be sent to an
        # attachment/service on 127.0.0.1:9000. An explicit Cookie header is
        # treated as an intentional caller override; inherited jar cookies are
        # suppressed with an empty Cookie header for every foreign origin.
        if "cookie" not in explicit:
            request_headers["Cookie"] = ""

        if request_headers:
            out["headers"] = request_headers
        return out

    def _warn_once(self, kind: str, message: str) -> None:
        attr = (
            "_cf_warned_unavailable"
            if kind == "unavailable"
            else "_cf_warned_persistent_challenge"
        )
        if getattr(self, attr, False):
            return
        setattr(self, attr, True)
        try:
            from .logger import Logger
            Logger.warning(message)
        except Exception:
            pass

    def _activate_browser_transport(self) -> bool:
        if not self._cf_fallback:
            return False
        if self._cf_active and self._cf_browser_session is not None:
            return True
        if not HAS_CURL_CFFI or curl_requests is None:
            self._warn_once(
                "unavailable",
                "Cloudflare được phát hiện nhưng curl_cffi chưa được cài; "
                "không thể chuyển sang browser TLS fingerprint.",
            )
            return False

        with self._cf_lock:
            if self._cf_active and self._cf_browser_session is not None:
                return True
            try:
                browser = curl_requests.Session(impersonate=self._cf_impersonate)
                for key, value in self.headers.items():
                    if str(key).lower() not in _BROWSER_MANAGED_HEADERS:
                        browser.headers[str(key)] = str(value)

                for cookie in self.cookies:
                    browser.cookies.set(
                        cookie.name,
                        cookie.value,
                        domain=cookie.domain or "",
                        path=cookie.path or "/",
                        secure=bool(cookie.secure),
                    )

                self._cf_browser_session = browser
                self._cf_active = True
                try:
                    from .logger import Logger
                    Logger.info(
                        "Cloudflare detected — switched HTTP transport to "
                        f"browser fingerprint ({self._cf_impersonate})."
                    )
                except Exception:
                    pass
                return True
            except Exception as exc:
                self._warn_once(
                    "unavailable",
                    f"Cloudflare được phát hiện nhưng không khởi tạo được "
                    f"browser transport: {type(exc).__name__}: {exc}",
                )
                return False

    def _sync_browser_cookies_back(self) -> None:
        browser = self._cf_browser_session
        if browser is None:
            return
        jar = getattr(getattr(browser, "cookies", None), "jar", None)
        if not isinstance(jar, CookieJar):
            return
        for cookie in jar:
            try:
                self.cookies.set_cookie(cookie)
            except Exception:
                pass

    @staticmethod
    def _browser_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(kwargs)
        out.pop("hooks", None)
        cookies = out.get("cookies")
        if isinstance(cookies, CookieJar):
            out["cookies"] = requests.utils.dict_from_cookiejar(cookies)
        return out

    def _browser_for_url(self, url: str) -> tuple[Any, bool]:
        """Return an isolated browser session for foreign credential origins.

        HTTP cookies cannot be scoped by port, so reusing the platform browser
        jar for ``host:other-port`` would leak session/cf_clearance. Foreign
        origins get their own cached curl session with no platform credentials.
        The bool indicates whether this is the main platform browser session.
        """
        if self._cf_browser_session is None and not self._activate_browser_transport():
            return None, False

        owner = self._credential_origin
        target = _normalized_origin(url)
        if not owner or not target or target == owner:
            return self._cf_browser_session, True

        with self._cf_lock:
            browser = self._cf_foreign_sessions.get(target)
            if browser is None:
                if not HAS_CURL_CFFI or curl_requests is None:
                    return None, False
                browser = curl_requests.Session(impersonate=self._cf_impersonate)
                self._cf_foreign_sessions[target] = browser
            return browser, False

    def _browser_request(self, method: str, url: str, **kwargs: Any) -> Any:
        browser, is_main = self._browser_for_url(url)
        if browser is None:
            return None
        browser_kwargs = self._browser_kwargs(kwargs)
        if not is_main:
            headers = dict(browser_kwargs.get("headers") or {})
            # The isolated session has no platform cookie jar, so allow its own
            # origin-local cookies instead of forcing Cookie: "" forever.
            if headers.get("Cookie") == "":
                headers.pop("Cookie", None)
            browser_kwargs["headers"] = headers
        try:
            response = browser.request(
                method=method,
                url=url,
                **browser_kwargs,
            )
        except Exception as exc:
            # Keep the public session contract stable: downloader/retry code
            # already catches requests.RequestException. curl_cffi has its own
            # unrelated RequestException hierarchy, so normalize only those
            # transport failures instead of leaking backend-specific types.
            if CurlRequestException is not None and isinstance(exc, CurlRequestException):
                raise requests.RequestException(
                    f"Cloudflare browser transport failed: {exc}"
                ) from exc
            raise
        if is_main:
            self._sync_browser_cookies_back()
        return response

    def _preflight_mutation(self, url: str, kwargs: Dict[str, Any]) -> None:
        """Probe an origin safely before its first mutating request.

        The probe is intentionally advisory for ordinary transport/HTTP
        failures: a server may reject HEAD while still accepting POST. Only a
        confirmed Cloudflare Challenge that survives browser fallback blocks
        the mutation. This lets us activate curl_cffi/cf_clearance before a
        side effect without turning HEAD compatibility quirks into false
        negatives.
        """
        origin = _normalized_origin(url)
        if not origin or origin in self._mutation_preflight_ok:
            return

        preflight_kwargs: Dict[str, Any] = {}
        for key in (
            "headers", "cookies", "timeout", "verify", "cert", "proxies",
            "auth", "params",
        ):
            if key in kwargs:
                preflight_kwargs[key] = kwargs[key]
        preflight_kwargs["allow_redirects"] = True
        preflight_kwargs["stream"] = False
        preflight_kwargs["_cf_skip_preflight"] = True

        response = None
        try:
            response = self.request("HEAD", url, **preflight_kwargs)
        except requests.RequestException:
            # HEAD is a compatibility/security probe, not the actual operation.
            # Keep legacy availability when the probe itself cannot connect;
            # the eventual mutation still retains the no-blind-replay invariant.
            self._mutation_preflight_ok.add(origin)
            return
        finally:
            # Response gets closed below after challenge inspection.
            pass

        try:
            if is_cloudflare_challenge(response, inspect_body=False):
                raise CloudflareChallengeError(
                    "Cloudflare Managed/Interstitial Challenge vẫn chặn "
                    "preflight; mutation chưa được gửi. Lấy cf_clearance bằng "
                    "browser rồi thử lại."
                )
            self._mutation_preflight_ok.add(origin)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        method_up = str(method or "GET").upper()
        skip_preflight = bool(kwargs.pop("_cf_skip_preflight", False))
        kwargs = self._scope_request_kwargs(url, kwargs)

        if method_up not in self.SAFE_REPLAY_METHODS and not skip_preflight:
            self._preflight_mutation(url, kwargs)

        if self._cf_active:
            response = self._browser_request(method_up, url, **kwargs)
            if response is not None:
                if is_cloudflare_challenge(
                    response, inspect_body=not bool(kwargs.get("stream"))
                ):
                    self._warn_once(
                        "persistent",
                        "Cloudflare vẫn trả Challenge Page sau browser "
                        "fingerprint. Nếu site dùng Managed Challenge/Turnstile, "
                        "hãy lấy cookie cf_clearance bằng trình duyệt rồi truyền "
                        "cùng cookie platform; tool sẽ không tự bypass CAPTCHA.",
                    )
                    if method_up not in self.SAFE_REPLAY_METHODS:
                        close = getattr(response, "close", None)
                        if callable(close):
                            close()
                        raise CloudflareChallengeError(
                            "Cloudflare Challenge chặn mutation; request không "
                            "được replay. Lấy cf_clearance bằng browser rồi thử lại."
                        )
                return response

        response = super().request(method_up, url, **kwargs)

        if not is_cloudflare_proxy_response(response):
            return response

        self._cf_seen = True
        activated = self._activate_browser_transport()
        challenged = is_cloudflare_challenge(
            response, inspect_body=not bool(kwargs.get("stream"))
        )

        if not challenged or not activated:
            return response

        if method_up not in self.SAFE_REPLAY_METHODS:
            self._warn_once(
                "persistent",
                "Cloudflare challenge chặn request có side effect; tool không "
                "replay tự động để tránh gửi trùng. Chạy lại sau khi browser "
                "transport/cf_clearance đã sẵn sàng.",
            )
            close = getattr(response, "close", None)
            if callable(close):
                close()
            raise CloudflareChallengeError(
                "Cloudflare Challenge chặn mutation sau preflight sạch; request "
                "đã bị edge từ chối và không được replay. Lấy cf_clearance bằng "
                "browser rồi thử lại."
            )

        browser_response = self._browser_request(method_up, url, **kwargs)
        if browser_response is None:
            return response
        if is_cloudflare_challenge(
            browser_response, inspect_body=not bool(kwargs.get("stream"))
        ):
            self._warn_once(
                "persistent",
                "Cloudflare vẫn trả Challenge Page sau browser fingerprint. "
                "Nếu site dùng Managed Challenge/Turnstile, hãy lấy cookie "
                "cf_clearance bằng trình duyệt rồi truyền cùng cookie platform; "
                "tool sẽ không tự bypass CAPTCHA.",
            )
        return browser_response

    def close(self) -> None:
        try:
            browser = self._cf_browser_session
            if browser is not None:
                browser.close()
            for foreign in list(self._cf_foreign_sessions.values()):
                try:
                    foreign.close()
                except Exception:
                    pass
            self._cf_foreign_sessions.clear()
        finally:
            super().close()


def create_session(
    cookie: Optional[Union[str, Dict[str, str]]] = None,
    token: Optional[str] = None,
    custom_headers: Optional[Dict[str, str]] = None,
    retries: int = 3,
    backoff_factor: float = 0.5,
    timeout: int = 30,
    impersonate: str = "chrome",
    use_browser_impersonation: bool = False,
    cloudflare_fallback: bool = True,
    base_url: Optional[str] = None,
) -> Any:
    """Create the toolkit HTTP session with adaptive Cloudflare fallback."""
    if use_browser_impersonation and (not HAS_CURL_CFFI or curl_requests is None):
        raise RuntimeError(
            "Browser impersonation requested but curl_cffi is not installed"
        )

    # Always return the policy-enforcing wrapper. Force-browser mode activates
    # its curl backend after credentials/cookies are scoped, instead of
    # returning a raw curl_cffi.Session that would bypass origin stripping and
    # mutation preflight safeguards.
    session: Any = CloudflareAdaptiveSession(
        impersonate=impersonate,
        cloudflare_fallback=(True if use_browser_impersonation
                             else cloudflare_fallback),
    )
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        allowed_methods=frozenset(["HEAD", "GET", "OPTIONS"]),
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=64,
        pool_maxsize=64,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    credential_origin = _normalized_origin(base_url)
    try:
        setattr(session, "_credential_origin", credential_origin)
    except Exception:
        pass

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": (
            "application/json, text/html;q=0.9, "
            "application/xhtml+xml;q=0.8, */*;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
    }
    if custom_headers:
        headers.update(custom_headers)

    if token:
        token_text = str(token).strip()
        low = token_text.lower()
        if low.startswith("bearer ") or low.startswith("token "):
            headers["Authorization"] = token_text
        elif token_text.startswith("ctfd_"):
            headers["Authorization"] = f"Token {token_text}"
        else:
            headers["Authorization"] = f"Bearer {token_text}"

    session.headers.update(headers)

    if cookie:
        if isinstance(cookie, str):
            cookie_dict = parse_cookie_string(cookie)
        else:
            cookie_dict = cookie
        cookie_host, cookie_secure = _cookie_scope(base_url)
        if cookie_host:
            for name, value in dict(cookie_dict).items():
                session.cookies.set(
                    str(name), str(value), domain=cookie_host, path="/",
                    secure=cookie_secure,
                )
        else:
            session.cookies.update(cookie_dict)

    if use_browser_impersonation:
        if not session._activate_browser_transport():
            raise RuntimeError("Không khởi tạo được browser impersonation transport")

    return session
