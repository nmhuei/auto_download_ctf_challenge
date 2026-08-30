"""P0 cross-process test: two CLI-like submitters must not double-POST."""

import json
import multiprocessing
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ctf_downloader.storage.workspace_repo import WorkspaceRepo


def _submit_worker(workspace, counter_path, barrier):
    # Imports inside the child keep this worker spawn/fork friendly.
    from ctf_downloader.services.submit_service import SubmitService

    class FakePlatform:
        def __init__(self):
            self.ctf_info = SimpleNamespace(platform_type="ctfd")
            self.last_verdict = "unknown"

        def authenticate(self):
            return True

        def submit_flag(self, _cid, _flag):
            # This is the externally visible side effect we must serialize.
            with open(counter_path, "a", encoding="utf-8") as f:
                f.write(f"{os.getpid()}\n")
                f.flush()
                os.fsync(f.fileno())
            time.sleep(0.20)
            self.last_verdict = "correct"
            return True, "correct"

    platform = FakePlatform()
    with patch(
        "ctf_downloader.services.submit_service.create_session",
        return_value=MagicMock(),
    ), patch(
        "ctf_downloader.services.submit_service.PlatformDetector.detect_platform",
        return_value=platform,
    ):
        svc = SubmitService(
            url="https://ctfd.test",
            workspace_dir=workspace,
            flag_format=r"^FLAG\{.+\}$",
        )
        barrier.wait(timeout=5)
        svc.submit(1, "FLAG{race}")


class TestCrossProcessSubmitGate(unittest.TestCase):
    def test_same_challenge_is_posted_once(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "challenges.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "ctf_info": {
                        "url": "https://ctfd.test",
                        "flag_format": "^FLAG\\{.+\\}$",
                        "flag_format_source": "test",
                    },
                    "challenges": [{"id": 1, "name": "Race", "category": "Web"}],
                }, f)
            chall_dir = os.path.join(ws, "Web", "Race")
            os.makedirs(chall_dir)
            meta_path = os.path.join(chall_dir, "metadata.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"id": 1, "name": "Race", "category": "Web"}, f)

            counter = os.path.join(ws, "network-posts.txt")
            # spawn avoids unsafe fork-from-multithreaded-xdist warnings
            # while preserving a real cross-process submit race.
            ctx = multiprocessing.get_context("spawn")
            barrier = ctx.Barrier(2)
            procs = [
                ctx.Process(target=_submit_worker, args=(ws, counter, barrier))
                for _ in range(2)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=10)
                self.assertEqual(p.exitcode, 0)

            with open(counter, encoding="utf-8") as f:
                posts = [line for line in f if line.strip()]
            self.assertEqual(
                len(posts), 1,
                "per-challenge gate must allow exactly one network POST",
            )

            repo = WorkspaceRepo(ws)
            hist = repo.load_submit_history()["entries"]
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]["result"], "correct")
            self.assertEqual(repo.read_status(meta_path)["solve"], "solved_by_me")


if __name__ == "__main__":
    unittest.main()
