import re
from bs4 import BeautifulSoup, NavigableString, Tag

class TextParser:
    @staticmethod
    def html_to_markdown(html_content: str) -> str:
        """
        Converts HTML to clean Markdown text for readable README.md files.
        """
        if not html_content or not isinstance(html_content, str):
            return ""

        # If it doesn't look like HTML, return trimmed string
        if "<" not in html_content or ">" not in html_content:
            return html_content.strip()

        try:
            soup = BeautifulSoup(html_content, "html.parser")
            
            # Replace code tags
            for pre in soup.find_all("pre"):
                code_text = pre.get_text()
                pre.replace_with(f"\n\n```\n{code_text.strip()}\n```\n\n")
                
            for code in soup.find_all("code"):
                code_text = code.get_text()
                code.replace_with(f"`{code_text}`")
                
            # Replace links
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True) or href
                a.replace_with(f"[{text}]({href})")
                
            # Replace bold / italic
            for b in soup.find_all(["b", "strong"]):
                b.replace_with(f"**{b.get_text()}**")
                
            for i in soup.find_all(["i", "em"]):
                i.replace_with(f"*{i.get_text()}*")
                
            # Replace lists
            for li in soup.find_all("li"):
                li.replace_with(f"\n- {li.get_text().strip()}")
                
            # Replace breaks and paragraphs
            for br in soup.find_all("br"):
                br.replace_with("\n")
                
            for p in soup.find_all("p"):
                p_text = p.get_text().strip()
                p.replace_with(f"\n\n{p_text}\n\n")

            # Extract raw text
            text = soup.get_text()
            
            # Clean up extra newlines
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text.strip()
        except Exception:
            return html_content.strip()

    @staticmethod
    def clean_text(text: str) -> str:
        """
        Normalizes line endings and removes trailing spaces.
        """
        if not text:
            return ""
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        return text.strip()
