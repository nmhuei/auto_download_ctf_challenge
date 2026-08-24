import re
import shutil
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional, Tuple
from bs4 import BeautifulSoup

KNOWN_FILE_EXTENSIONS = {
    '.zip', '.tar', '.gz', '.tgz', '.bz2', '.tbz2', '.xz', '.txz', '.7z', '.rar',
    '.bin', '.elf', '.exe', '.dll', '.so', '.dylib', '.apk', '.ipa',
    '.py', '.c', '.cpp', '.h', '.hpp', '.rs', '.go', '.java', '.js', '.ts', '.php', '.rb', '.sh',
    '.pcap', '.pcapng', '.cap',
    '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.webp',
    '.txt', '.log', '.md', '.json', '.xml', '.yaml', '.yml', '.toml', '.csv',
    '.iso', '.img', '.vmdk', '.ova', '.qcow2',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.wav', '.mp3', '.mp4', '.avi', '.mkv',
    '.raw', '.dmp', '.mem', '.enc', '.key', '.pem', '.crt', '.pub', '.db', '.sqlite', '.sqlite3'
}

@dataclass
class ExtractedLink:
    url: str
    link_type: str  # 'direct_file', 'gdrive', 'mega', 'dropbox', 'mediafire', 'github', 'discord', 'generic_url'
    filename_hint: Optional[str] = None
    title: Optional[str] = None
    is_downloadable: bool = False

@dataclass
class ConnectionInfo:
    proto: str  # 'nc', 'ssh', 'http', 'https', 'custom'
    host: str
    port: Optional[int] = None
    raw_command: str = ""

class LinkExtractor:
    @staticmethod
    def _host_in(host: str, domains: tuple) -> bool:
        """
        So khớp domain bằng hậu tố CHÍNH XÁC: host == domain hoặc host là
        subdomain thật ('.' + domain là hậu tố của host). KHÔNG dùng phép
        'domain in netloc' — kiểu đó khiến drive.google.com.evil.io bị nhận
        nhầm là Google Drive và route HTTP tới host kẻ tấn công.
        """
        host = (host or "").lower().strip()
        for d in domains:
            d = (d or "").lower()
            if not d:
                continue
            if host == d or host.endswith("." + d):
                return True
        return False

    @staticmethod
    def extract_links_and_files(text: str, base_url: str = "") -> List[ExtractedLink]:
        """
        Extract all links, embedded files, and 3rd party download links from Markdown / HTML / text.
        """
        if not text:
            return []
            
        found_urls = []
        links_dict = {}  # url -> ExtractedLink

        # 1. Parse HTML with BeautifulSoup if contains tags
        if "<" in text and ">" in text:
            try:
                soup = BeautifulSoup(text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    title = a.get_text(strip=True) or None
                    if href:
                        found_urls.append((href, title))
                        
                for img in soup.find_all("img", src=True):
                    src = img["src"].strip()
                    title = img.get("alt") or None
                    if src:
                        found_urls.append((src, title))
            except Exception:
                pass

        # 2. Extract Markdown links: [text](url)
        md_matches = re.findall(r'\[([^\]]*)\]\((https?://[^\s\)]+|/[^\s\)]+)\)', text)
        for title, url in md_matches:
            found_urls.append((url.strip(), title.strip() or None))

        # 3. Extract raw URLs
        raw_matches = re.findall(r'(https?://[^\s<>"\')]+)', text)
        for url in raw_matches:
            found_urls.append((url.strip(), None))

        # Process each URL
        for url, title in found_urls:
            # Handle relative URLs
            if base_url and url.startswith("/"):
                url = urllib.parse.urljoin(base_url, url)
                
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
                
            # Clean trailing punctuation from raw regex match
            url = re.sub(r'[\.,;:\)\]>]+$', '', url)
            
            if url in links_dict:
                continue

            extracted = LinkExtractor.classify_link(url, title)
            links_dict[url] = extracted

        return list(links_dict.values())

    @staticmethod
    def classify_link(url: str, title: Optional[str] = None) -> ExtractedLink:
        """
        Classifies URL into service types (gdrive, mega, dropbox, mediafire, etc.)
        and detects if it is directly downloadable.
        """
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()  # hostname đã strip port + lowercase
        path = parsed.path.lower()

        # Google Drive
        if LinkExtractor._host_in(host, ("drive.google.com", "docs.google.com")):
            # Check if it is a file link or folder link
            return ExtractedLink(
                url=url,
                link_type="gdrive",
                filename_hint=title,
                title=title,
                is_downloadable=True
            )

        # Mega.nz — chỉ đánh dấu downloadable khi có megatools (megadl/mega-get);
        # kiểm tra tool tại đây bằng shutil.which, KHÔNG import ngược tầng
        # downloaders (DownloadManager vẫn tự kiểm tra lại một lần lúc tải).
        if LinkExtractor._host_in(host, ("mega.nz", "mega.co.nz")):
            has_mega_tool = any(shutil.which(tool) for tool in ("megadl", "mega-get"))
            return ExtractedLink(
                url=url,
                link_type="mega",
                filename_hint=title,
                title=title,
                is_downloadable=bool(has_mega_tool)
            )

        # Dropbox
        if LinkExtractor._host_in(host, ("dropbox.com",)):
            return ExtractedLink(
                url=url,
                link_type="dropbox",
                filename_hint=title,
                title=title,
                is_downloadable=True
            )

        # Mediafire
        if LinkExtractor._host_in(host, ("mediafire.com",)):
            return ExtractedLink(
                url=url,
                link_type="mediafire",
                filename_hint=title,
                title=title,
                is_downloadable=True
            )

        # Discord CDN
        if LinkExtractor._host_in(host, ("cdn.discordapp.com", "media.discordapp.net")):
            return ExtractedLink(
                url=url,
                link_type="discord",
                filename_hint=title,
                title=title,
                is_downloadable=True
            )

        # GitHub releases or raw
        if LinkExtractor._host_in(host, ("github.com",)) and ("/releases/download/" in path or "/raw/" in path):
            return ExtractedLink(
                url=url,
                link_type="github",
                filename_hint=title,
                title=title,
                is_downloadable=True
            )
            
        if LinkExtractor._host_in(host, ("raw.githubusercontent.com", "gitlab.com")):
            return ExtractedLink(
                url=url,
                link_type="github",
                filename_hint=title,
                title=title,
                is_downloadable=True
            )

        # Direct file extension check
        for ext in KNOWN_FILE_EXTENSIONS:
            if path.endswith(ext) or (ext + "?") in path:
                return ExtractedLink(
                    url=url,
                    link_type="direct_file",
                    filename_hint=title,
                    title=title,
                    is_downloadable=True
                )

        # General URL
        return ExtractedLink(
            url=url,
            link_type="generic_url",
            filename_hint=title,
            title=title,
            is_downloadable=False
        )

    @staticmethod
    def extract_connection_info(text: str) -> List[ConnectionInfo]:
        """
        Extracts netcat / socat / ssh / web challenge connection commands.
        e.g., `nc chall.pwnable.com 1337` or `nc -vn 1.2.3.4 9000`
        """
        if not text:
            return []
            
        conns = []
        
        # 1. HTTP(S) challenge URLs (e.g., http://web.ctf.site:5000 or http://chall.ctf.org:8080/login)
        NON_CHALLENGE_DOMAINS = {
            "drive.google.com", "docs.google.com", "dropbox.com", "www.dropbox.com",
            "mediafire.com", "www.mediafire.com", "mega.nz", "mega.co.nz",
            "github.com", "raw.githubusercontent.com", "gitlab.com",
            "cdn.discordapp.com", "media.discordapp.net"
        }
        http_pattern = re.compile(
            r'(https?://[a-zA-Z0-9\.\-_]+(?::[0-9]{2,5})?(?:/[^\s<>"\']*)?)',
            re.IGNORECASE
        )
        for match in http_pattern.finditer(text):
            url = match.group(1)
            parsed = urllib.parse.urlparse(url)
            proto = parsed.scheme
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()

            if host in NON_CHALLENGE_DOMAINS or any(host.endswith("." + d) for d in NON_CHALLENGE_DOMAINS):
                continue
            # Skip if it is a direct file download link
            if any(path.endswith(ext) for ext in KNOWN_FILE_EXTENSIONS):
                continue

            port = parsed.port or (443 if proto == "https" else 80)
            if not any(c.host == host and c.port == port for c in conns):
                conns.append(ConnectionInfo(
                    proto=proto,
                    host=host,
                    port=port,
                    raw_command=url
                ))

        # 2. Netcat patterns: nc [-options] host port / ncat ... / socat ...
        nc_pattern = re.compile(
            r'(?:nc|ncat|netcat)\s+(?:-[a-zA-Z0-9]+\s+)*([a-zA-Z0-9\.\-_]+)\s+([0-9]{2,5})',
            re.IGNORECASE
        )
        for match in nc_pattern.finditer(text):
            host = match.group(1)
            port = int(match.group(2))
            conns.append(ConnectionInfo(
                proto="nc",
                host=host,
                port=port,
                raw_command=match.group(0).strip()
            ))

        # 3. Host:port pattern (e.g., "chall.ctf.org:31337" or "192.168.1.1:8080")
        host_port_pattern = re.compile(
            r'(https?://)?(?:connect to|server|service|target|host)?\s*[:\s]?\s*([a-zA-Z0-9\-_]+\.[a-zA-Z0-9\.\-_]+):([0-9]{2,5})',
            re.IGNORECASE
        )
        for match in host_port_pattern.finditer(text):
            has_http = match.group(1)
            if has_http:
                continue
            host = match.group(2)
            port = int(match.group(3))
            if not any(c.host == host and c.port == port for c in conns):
                conns.append(ConnectionInfo(
                    proto="nc",
                    host=host,
                    port=port,
                    raw_command=f"nc {host} {port}"
                ))

        # 4. SSH patterns: ssh [user@]host -p port
        ssh_pattern = re.compile(
            r'ssh\s+(?:([a-zA-Z0-9\-_]+)@)?([a-zA-Z0-9\.\-_]+)(?:\s+-p\s+([0-9]+))?',
            re.IGNORECASE
        )
        for match in ssh_pattern.finditer(text):
            user = match.group(1) or "user"
            host = match.group(2)
            port = int(match.group(3)) if match.group(3) else 22
            conns.append(ConnectionInfo(
                proto="ssh",
                host=host,
                port=port,
                raw_command=match.group(0).strip()
            ))

        return conns
