import os
import shutil
import unittest
from unittest.mock import MagicMock, patch

from ctf_downloader.config import DownloaderConfig
from ctf_downloader.platforms.base import Challenge, CTFInfo
from ctf_downloader.platforms.ctfd import CTFdPlatform
from ctf_downloader.platforms.rctf import RCTFPlatform
from ctf_downloader.extractors.link_extractor import LinkExtractor
from ctf_downloader.extractors.text_parser import TextParser
from ctf_downloader.downloaders.dropbox import DropboxDownloader
from ctf_downloader.downloaders.gdrive import GDriveDownloader
from ctf_downloader.generator.workspace_builder import WorkspaceBuilder
from ctf_downloader.generator.summary_generator import SummaryGenerator
from ctf_downloader.utils.sanitize import sanitize_folder_name, sanitize_filename, extract_filename_from_headers
from ctf_downloader.utils.http_client import parse_cookie_string, create_session

class TestCTFDownloader(unittest.TestCase):
    def setUp(self):
        self.test_output_dir = "./test_workspace"
        os.makedirs(self.test_output_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.test_output_dir):
            shutil.rmtree(self.test_output_dir)

    def test_cookie_parser(self):
        # Format 1: standard header string
        cookie_str1 = "session=.eJw1zl...; cf_clearance=abc123xyz; other=value"
        res1 = parse_cookie_string(cookie_str1)
        self.assertEqual(res1["session"], ".eJw1zl...")
        self.assertEqual(res1["cf_clearance"], "abc123xyz")
        self.assertEqual(res1["other"], "value")

        # Format 2: raw session string
        cookie_str2 = ".eJw1zl..."
        res2 = parse_cookie_string(cookie_str2)
        self.assertEqual(res2["session"], ".eJw1zl...")

        # Format 3: JSON string
        cookie_str3 = '{"session": "xyz456", "token": "abc"}'
        res3 = parse_cookie_string(cookie_str3)
        self.assertEqual(res3["session"], "xyz456")
        self.assertEqual(res3["token"], "abc")

    def test_link_extractor_and_services(self):
        text = """
        Welcome to the challenge!
        Source code: https://drive.google.com/file/d/1ABCXYZ_12345/view?usp=sharing
        Backup: https://www.dropbox.com/s/abcdef12345/chall.zip?dl=0
        Mediafire mirror: https://www.mediafire.com/file/xyz123/payload.rar/file
        Direct binary: https://ctf.org/files/exploit.elf
        
        Connect to the service:
        nc challenge.ctf.org 13337
        or visit http://web.ctf.org:8080/login
        """
        links = LinkExtractor.extract_links_and_files(text, base_url="https://ctf.org")
        conns = LinkExtractor.extract_connection_info(text)

        link_types = {l.link_type for l in links}
        self.assertIn("gdrive", link_types)
        self.assertIn("dropbox", link_types)
        self.assertIn("mediafire", link_types)
        self.assertIn("direct_file", link_types)

        proto_types = {c.proto for c in conns}
        self.assertIn("nc", proto_types)
        self.assertIn("http", proto_types)
        
        nc_conn = next(c for c in conns if c.proto == "nc")
        self.assertEqual(nc_conn.host, "challenge.ctf.org")
        self.assertEqual(nc_conn.port, 13337)

    def test_gdrive_and_dropbox_converters(self):
        gdrive_url = "https://drive.google.com/file/d/1B2C3D4E5F6G7H8I9J0K/view?usp=sharing"
        file_id = GDriveDownloader.extract_file_id(gdrive_url)
        self.assertEqual(file_id, "1B2C3D4E5F6G7H8I9J0K")

        dropbox_url = "https://www.dropbox.com/s/xyz987/test.zip?dl=0"
        direct_dropbox = DropboxDownloader.get_direct_url(dropbox_url)
        self.assertIn("dl=1", direct_dropbox)

    def test_sanitization(self):
        unsafe_name = 'Challenge /: *?"<>| Test 1.0! '
        clean = sanitize_folder_name(unsafe_name)
        self.assertNotIn("/", clean)
        self.assertNotIn(":", clean)
        self.assertNotIn("*", clean)
        self.assertNotIn("?", clean)
        self.assertNotIn("<", clean)
        self.assertNotIn(">", clean)
        self.assertNotIn("|", clean)

        cd_header = {'Content-Disposition': 'attachment; filename="super_secret_source.zip"'}
        extracted = extract_filename_from_headers(cd_header)
        self.assertEqual(extracted, "super_secret_source.zip")

    def test_text_parser(self):
        html = "<p>Hello <b>World</b></p><pre><code>nc host 1337</code></pre><a href='https://ctf.org'>link</a>"
        md = TextParser.html_to_markdown(html)
        self.assertIn("**World**", md)
        self.assertIn("```", md)
        self.assertIn("[link](https://ctf.org)", md)

    def test_ctfd_parser_mock(self):
        mock_session = MagicMock()
        
        # Mock /api/v1/challenges
        mock_chall_resp = MagicMock()
        mock_chall_resp.status_code = 200
        mock_chall_resp.headers = {"content-type": "application/json"}
        mock_chall_resp.json.return_value = {
            "success": True,
            "data": [
                {"id": 1, "name": "Sanity Check", "category": "Welcome", "value": 50, "tags": ["intro"]}
            ]
        }

        # Mock /api/v1/challenges/1
        mock_detail_resp = MagicMock()
        mock_detail_resp.status_code = 200
        mock_detail_resp.headers = {"content-type": "application/json"}
        mock_detail_resp.json.return_value = {
            "success": True,
            "data": {
                "id": 1,
                "name": "Sanity Check",
                "category": "Welcome",
                "description": "Welcome to CTF! Flag is FLAG{welcome}",
                "files": ["/files/abc123hash/rules.pdf"],
                "tags": ["intro", "easy"],
                "hints": ["Read the rules"]
            }
        }

        def mock_get(url, *args, **kwargs):
            if "/api/v1/challenges/1" in url:
                return mock_detail_resp
            elif "/api/v1/challenges" in url:
                return mock_chall_resp
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/html"}
            resp.text = "<html><title>Cyber CTF - CTFd</title></html>"
            return resp

        mock_session.get.side_effect = mock_get
        
        platform = CTFdPlatform("https://demo.ctfd.io", mock_session)
        challs = platform.fetch_challenges()
        
        self.assertEqual(len(challs), 1)
        self.assertEqual(challs[0].name, "Sanity Check")
        self.assertEqual(challs[0].category, "Welcome")
        self.assertEqual(len(challs[0].files), 1)
        self.assertEqual(challs[0].files[0][0], "https://demo.ctfd.io/files/abc123hash/rules.pdf")

    def test_gzctf_parser_mock(self):
        from ctf_downloader.platforms.gzctf import GZCTFPlatform
        mock_session = MagicMock()
        
        # Mock /api/account/profile
        profile_resp = MagicMock()
        profile_resp.status_code = 200
        profile_resp.json.return_value = {"userName": "hacker1337", "email": "hacker@ctf.org"}

        # Mock /api/game/6
        game_resp = MagicMock()
        game_resp.status_code = 200
        game_resp.json.return_value = {"title": "PTIT CTF", "teamName": "NoTeam"}

        # Mock /api/game/6/details
        details_resp = MagicMock()
        details_resp.status_code = 200
        details_resp.json.return_value = {
            "challenges": {
                "Pwn": [{"id": 10, "title": "PwnMe", "score": 200, "solved": 5}]
            }
        }

        # Mock /api/game/6/challenges/10
        chall10_resp = MagicMock()
        chall10_resp.status_code = 200
        chall10_resp.json.return_value = {
            "id": 10,
            "title": "PwnMe",
            "content": "nc pwn.site.org 9000",
            "type": "DynamicContainer",
            "hints": None,
            "context": {"url": "/assets/pwnme.zip"}
        }

        def mock_gz_get(url, *args, **kwargs):
            if "/api/account/profile" in url:
                return profile_resp
            elif "/api/game/6/challenges/10" in url:
                return chall10_resp
            elif "/api/game/6/details" in url:
                return details_resp
            elif "/api/game/6" in url:
                return game_resp
            resp = MagicMock()
            resp.status_code = 200
            return resp

        mock_session.get.side_effect = mock_gz_get
        gz = GZCTFPlatform("https://gzctf.org/games/6/challenges", mock_session)
        self.assertTrue(gz.authenticate())
        self.assertEqual(gz.ctf_info.user_name, "hacker1337")
        
        challs = gz.fetch_challenges()
        self.assertEqual(len(challs), 1)
        self.assertEqual(challs[0].name, "PwnMe")
        self.assertEqual(challs[0].category, "Pwn")
        self.assertEqual(challs[0].files[0][0], "https://gzctf.org/assets/pwnme.zip")

    def test_workspace_builder_and_summary(self):
        chall = Challenge(
            id=1,
            name="Buffer Overflow 101",
            category="Pwn",
            points=150,
            description="<p>Can you overflow the buffer?</p><pre>nc pwn.site.org 9999</pre>",
            tags=["pwn", "easy"],
            hints=[{"content": "Look at gets() function"}],
            connection_info="nc pwn.site.org 9999",
            solved_by_me=True,
            solves_count=42
        )

        links = LinkExtractor.extract_links_and_files(chall.description)
        conns = LinkExtractor.extract_connection_info(chall.description)
        
        dl_results = [{
            "url": "https://pwn.site.org/files/vuln",
            "name": "vuln",
            "saved_path": "/fake/path/vuln",
            "success": True,
            "source": "platform_attachment"
        }]

        chall_dir = WorkspaceBuilder.create_challenge_workspace(
            base_output_dir=self.test_output_dir,
            challenge=chall,
            extracted_links=links,
            connections=conns,
            download_results=dl_results,
            create_solve_template=True
        )

        self.assertTrue(os.path.isdir(chall_dir))
        self.assertTrue(os.path.isfile(os.path.join(chall_dir, "writeup", "README.md")))
        self.assertTrue(os.path.isfile(os.path.join(chall_dir, "metadata.json")))
        self.assertTrue(os.path.isfile(os.path.join(chall_dir, "solver", "solve.py")))
        self.assertTrue(os.path.isdir(os.path.join(chall_dir, "challenge")))

        # Check README contents
        with open(os.path.join(chall_dir, "writeup", "README.md"), "r") as f:
            readme = f.read()
            self.assertIn("Buffer Overflow 101", readme)
            self.assertIn("150", readme)
            self.assertIn("nc pwn.site.org 9999", readme)

        # Check solve.py template content for Pwn
        with open(os.path.join(chall_dir, "solver", "solve.py"), "r") as f:
            solve_content = f.read()
            self.assertIn("from pwn import *", solve_content)
            self.assertIn("pwn.site.org", solve_content)
            self.assertIn("9999", solve_content)

        # Check summary generation
        ctf_info = CTFInfo(
            title="Sample CTF 2026",
            url="https://sample.ctf.org",
            challenges=[chall]
        )
        summary_path = SummaryGenerator.generate_summary(
            base_output_dir=self.test_output_dir,
            ctf_info=ctf_info,
            all_results={1: dl_results}
        )

        self.assertTrue(os.path.isfile(summary_path))
        with open(summary_path, "r") as f:
            summary = f.read()
            self.assertIn("Sample CTF 2026", summary)
            self.assertIn("Buffer Overflow 101", summary)

    def test_flag_submitter_resolution(self):
        from ctf_downloader.submitter import FlagSubmitter
        
        # Test challenge ID resolution from name and cache
        submitter = FlagSubmitter(url="https://demo.ctfd.io", timeout=5)
        submitter.challenges_cache = {
            "101": {"id": 101, "name": "Sanity Check"},
            "sanity check": {"id": 101, "name": "Sanity Check"}
        }

        cid, name = submitter.resolve_challenge_id(101)
        self.assertEqual(cid, 101)
        self.assertEqual(name, "Sanity Check")

        cid2, name2 = submitter.resolve_challenge_id("Sanity Check")
        self.assertEqual(cid2, 101)
        self.assertEqual(name2, "Sanity Check")

    def test_ranking_manager(self):
        from ctf_downloader.ranking import RankingManager
        
        # Test initialization and ranking parsing
        mgr = RankingManager(url="https://demo.ctfd.io", timeout=5)
        self.assertEqual(mgr.url, "https://demo.ctfd.io")
        self.assertIsNotNone(mgr.platform)

if __name__ == "__main__":
    unittest.main()

