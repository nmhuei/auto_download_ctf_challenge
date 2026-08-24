import re
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BasePlatform, Challenge, CTFInfo
from ..utils.logger import Logger
from ..extractors.link_extractor import KNOWN_FILE_EXTENSIONS
from .registry import register


@register("generic_html", label="Generic HTML", throttle=5.0,
          supports_scoreboard=False)
class GenericHTMLPlatform(BasePlatform):
    def __init__(self, base_url: str, session: requests.Session):
        super().__init__(base_url, session)
        self.ctf_info.platform_type = "generic_html"

    def authenticate(self) -> bool:
        try:
            resp = self.session.get(self.base_url, timeout=15)
            return resp.status_code == 200
        except Exception:
            return False

    def fetch_challenges(self) -> List[Challenge]:
        """
        Parses the challenges page using common CSS classes and structure.
        """
        target_urls = [
            f"{self.base_url}/challenges",
            f"{self.base_url}/challenge",
            f"{self.base_url}/challs",
            self.base_url
        ]

        page_html = ""
        found_url = ""
        for url in target_urls:
            try:
                resp = self.session.get(url, timeout=15)
                if resp.status_code == 200 and ("challenge" in resp.text.lower() or "flag" in resp.text.lower()):
                    page_html = resp.text
                    found_url = url
                    break
            except Exception:
                continue

        if not page_html:
            Logger.error("Could not find challenges on the provided URL.")
            return []

        soup = BeautifulSoup(page_html, "html.parser")
        challenges = []

        # Look for challenge card elements
        chall_elements = soup.find_all(lambda tag: tag.has_attr('class') and any(
            c in ' '.join(tag['class']).lower() for c in ['challenge-card', 'chall-card', 'challenge_item', 'card-challenge', 'challenge']
        ))

        if not chall_elements:
            # Fallback: look for headers
            chall_elements = soup.find_all(['div', 'article', 'section'], class_=re.compile(r'card|box|item', re.I))

        for idx, elem in enumerate(chall_elements, start=1):
            name_elem = elem.find(['h2', 'h3', 'h4', 'h5', 'strong', 'a'])
            name = name_elem.get_text(strip=True) if name_elem else f"Challenge_{idx}"
            
            # Extract category
            cat_elem = elem.find(class_=re.compile(r'category|badge|type', re.I))
            category = cat_elem.get_text(strip=True) if cat_elem else "General"
            
            # Extract description
            desc_elem = elem.find(class_=re.compile(r'desc|content|body', re.I)) or elem
            desc = desc_elem.get_text("\n", strip=True) if desc_elem else ""

            # Extract points
            pts_match = re.search(r'(\d+)\s*(?:pts|points|pt)?', elem.get_text())
            points = int(pts_match.group(1)) if pts_match else 0

            # Extract attached file links
            files_list = []
            for a in elem.find_all("a", href=True):
                href = a["href"]
                if any(href.lower().endswith(ext) for ext in KNOWN_FILE_EXTENSIONS):
                    files_list.append((self.get_full_file_url(href), a.get_text(strip=True) or href.split("/")[-1]))

            if name:
                chall_obj = Challenge(
                    id=idx,
                    name=name,
                    category=category,
                    points=points,
                    description=desc,
                    files=files_list
                )
                challenges.append(chall_obj)

        self.ctf_info.challenges = challenges
        return challenges

    def get_full_file_url(self, file_path: str) -> str:
        if file_path.startswith("http://") or file_path.startswith("https://"):
            return file_path
        return urllib.parse.urljoin(self.base_url, file_path)

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        return False, "Automated flag submission not supported for generic HTML scraper platforms."

