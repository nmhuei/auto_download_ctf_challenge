import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Optional, Dict, Union
import re
import json

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """
    Parses a cookie string into a dictionary.
    Supports formats:
    - Standard cookie string: "session=.eJw1...; other=123"
    - JSON string: '{"session": "...", "other": "123"}'
    - Raw token / session string
    """
    if not cookie_str:
        return {}
        
    cookie_str = cookie_str.strip()
    
    # Check if JSON
    if cookie_str.startswith("{") and cookie_str.endswith("}"):
        try:
            return json.loads(cookie_str)
        except Exception:
            pass
            
    cookies = {}
    
    # Try splitting by semicolon
    parts = cookie_str.split(";")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, val = part.split("=", 1)
            cookies[key.strip()] = val.strip()
        else:
            # If no equal sign, treat as 'session' cookie if not already set
            if "session" not in cookies:
                cookies["session"] = part
                
    return cookies

def create_session(
    cookie: Optional[Union[str, Dict[str, str]]] = None,
    token: Optional[str] = None,
    custom_headers: Optional[Dict[str, str]] = None,
    retries: int = 3,
    backoff_factor: float = 0.5,
    timeout: int = 30
) -> requests.Session:
    """
    Creates a configured requests.Session instance with custom headers, cookies, and retry logic.
    """
    session = requests.Session()
    
    # Setup retries
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # Headers
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8,application/json",
        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
    }
    if custom_headers:
        headers.update(custom_headers)
        
    if token:
        # If token is provided, set Authorization header (CTFd Token style or Bearer)
        if token.startswith("ctfd_") or "token" in token.lower():
            headers["Authorization"] = f"Token {token}"
        else:
            headers["Authorization"] = f"Bearer {token}"
            
    session.headers.update(headers)
    
    # Cookies
    if cookie:
        if isinstance(cookie, str):
            cookie_dict = parse_cookie_string(cookie)
        else:
            cookie_dict = cookie
        session.cookies.update(cookie_dict)
        
    return session
