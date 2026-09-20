"""Unit tests for Extensible Platform Architecture, Schema Store, Configurable Platform, and Auto-Recon."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from ctf_downloader.platforms.base import Challenge
from ctf_downloader.platforms.configurable import ConfigurablePlatform
from ctf_downloader.platforms.detection import detect_platform_info
from ctf_downloader.platforms.recon import PlatformReconEngine, ReconResult
from ctf_downloader.platforms.registry import PLATFORMS
from ctf_downloader.platforms.schema import (
    PlatformEndpoints,
    PlatformSchema,
    PlatformSchemaMapping,
    PlatformSubmitSpec,
)
from ctf_downloader.platforms.schema_store import PlatformSchemaStore


class TestPlatformSchemaModels(unittest.TestCase):
    """Test schema data structures and JSON serialization."""

    def test_round_trip_serialization(self):
        schema = PlatformSchema(
            key="test_ctf",
            label="Test CTF Platform",
            throttle=2.5,
            html_markers=["meta-ctf-marker", "regex:custom-[0-9]+"],
            cookie_hints=["test_session"],
            endpoints=PlatformEndpoints(
                auth_check="/api/me",
                challenges="/api/challs",
                submit="/api/challs/{id}/flag",
            ),
            schema_mapping=PlatformSchemaMapping(
                challenges_root="data.list",
                id="chall_id",
                name="title",
                points="score",
            ),
            submit_spec=PlatformSubmitSpec(
                method="POST",
                flag_param="secret",
                success_field="is_valid",
            ),
            supports_container=True,
            supports_scoreboard=True,
        )

        json_str = schema.to_json()
        restored = PlatformSchema.from_json(json_str)

        self.assertEqual(restored.key, "test_ctf")
        self.assertEqual(restored.label, "Test CTF Platform")
        self.assertEqual(restored.throttle, 2.5)
        self.assertIn("meta-ctf-marker", restored.html_markers)
        self.assertEqual(restored.endpoints.auth_check, "/api/me")
        self.assertEqual(restored.endpoints.challenges, "/api/challs")
        self.assertEqual(restored.endpoints.submit, "/api/challs/{id}/flag")
        self.assertEqual(restored.schema_mapping.challenges_root, "data.list")
        self.assertEqual(restored.schema_mapping.id, "chall_id")
        self.assertEqual(restored.schema_mapping.name, "title")
        self.assertEqual(restored.schema_mapping.points, "score")
        self.assertEqual(restored.submit_spec.flag_param, "secret")
        self.assertEqual(restored.submit_spec.success_field, "is_valid")
        self.assertTrue(restored.supports_container)
        self.assertTrue(restored.supports_scoreboard)


class TestPlatformSchemaStore(unittest.TestCase):
    """Test multi-tier loading, storage, and registry synchronization."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ws_path = Path(self.tmp_dir.name)
        self._orig_platforms = dict(PLATFORMS)

    def tearDown(self):
        self.tmp_dir.cleanup()
        PLATFORMS.clear()
        PLATFORMS.update(self._orig_platforms)

    def test_load_builtin_metactf(self):
        all_schemas = PlatformSchemaStore.load_all()
        self.assertIn("metactf", all_schemas)
        meta = all_schemas["metactf"]
        self.assertEqual(meta.label, "MetaCTF")
        self.assertEqual(meta.endpoints.challenges, "/api/v1/challenges")

    def test_save_and_load_workspace_tier(self):
        schema = PlatformSchema(
            key="custom_ws_ctf",
            label="Workspace CTF",
            endpoints=PlatformEndpoints(challenges="/api/v1/challs"),
        )
        saved_path = PlatformSchemaStore.save(schema, scope="workspace", workspace_path=self.ws_path)
        self.assertTrue(saved_path.is_file())

        all_schemas = PlatformSchemaStore.load_all(workspace_path=self.ws_path)
        self.assertIn("custom_ws_ctf", all_schemas)
        loaded = all_schemas["custom_ws_ctf"]
        self.assertEqual(loaded.source, "workspace")
        self.assertEqual(loaded.endpoints.challenges, "/api/v1/challs")

        # Delete
        self.assertTrue(PlatformSchemaStore.delete("custom_ws_ctf", scope="workspace", workspace_path=self.ws_path))
        all_after_del = PlatformSchemaStore.load_all(workspace_path=self.ws_path)
        self.assertNotIn("custom_ws_ctf", all_after_del)

    def test_sync_to_registry(self):
        schema = PlatformSchema(
            key="dyn_registry_ctf",
            label="Dynamic Registry CTF",
            endpoints=PlatformEndpoints(challenges="/api/list"),
            html_markers=["dyn-marker"],
        )
        PlatformSchemaStore.save(schema, scope="workspace", workspace_path=self.ws_path)
        count = PlatformSchemaStore.sync_to_registry(workspace_path=self.ws_path)
        self.assertGreaterEqual(count, 1)

        self.assertIn("dyn_registry_ctf", PLATFORMS)
        spec = PLATFORMS["dyn_registry_ctf"]
        self.assertEqual(spec.label, "Dynamic Registry CTF")
        self.assertIn("dyn-marker", spec.html_markers)
        self.assertTrue(issubclass(spec.cls, ConfigurablePlatform))


class TestConfigurablePlatformAdapter(unittest.TestCase):
    """Test dynamic platform adapter request handling and parsing."""

    def setUp(self):
        self.schema = PlatformSchema(
            key="mock_ctf",
            label="Mock CTF",
            endpoints=PlatformEndpoints(
                auth_check="/api/auth/me",
                challenges="/api/challenges",
                submit="/api/challenges/{id}/submit",
            ),
            schema_mapping=PlatformSchemaMapping(
                challenges_root="data.challenges",
                id="id",
                name="title",
                category="tag",
                points="score",
                description="desc",
                files="downloads",
                solved="is_solved",
            ),
            submit_spec=PlatformSubmitSpec(
                method="POST",
                flag_param="flag_val",
                success_field="success",
            ),
        )
        self.session = mock.MagicMock()
        self.adapter = ConfigurablePlatform("https://mockctf.test", self.session, schema=self.schema)

    def test_authenticate_success(self):
        resp_mock = mock.MagicMock()
        resp_mock.status_code = 200
        resp_mock.json.return_value = {"user": {"username": "hacker_one"}}
        self.session.get.return_value = resp_mock

        self.assertTrue(self.adapter.authenticate())
        self.assertEqual(self.adapter.ctf_info.user_name, "hacker_one")

    def test_fetch_challenges(self):
        resp_mock = mock.MagicMock()
        resp_mock.status_code = 200
        resp_mock.json.return_value = {
            "data": {
                "challenges": [
                    {
                        "id": 101,
                        "title": "Crypto 1",
                        "tag": "crypto",
                        "score": 150,
                        "desc": "Solve this cipher",
                        "downloads": ["/files/cipher.txt"],
                        "is_solved": False,
                    },
                    {
                        "id": 102,
                        "title": "Web 1",
                        "tag": "web",
                        "score": 200,
                        "desc": "Bypass login",
                        "downloads": [{"url": "http://other.host/app.zip"}],
                        "is_solved": True,
                    },
                ]
            }
        }
        self.session.get.return_value = resp_mock

        challs = self.adapter.fetch_challenges()
        self.assertEqual(len(challs), 2)
        c1 = challs[0]
        self.assertEqual(c1.id, 101)
        self.assertEqual(c1.name, "Crypto 1")
        self.assertEqual(c1.category, "Crypto")
        self.assertEqual(c1.points, 150)
        self.assertEqual(c1.files, ["https://mockctf.test/files/cipher.txt"])
        self.assertFalse(c1.solved_by_me)

        c2 = challs[1]
        self.assertEqual(c2.id, 102)
        self.assertEqual(c2.name, "Web 1")
        self.assertEqual(c2.points, 200)
        self.assertTrue(c2.solved_by_me)

    def test_submit_flag_correct_and_incorrect(self):
        # Correct submit
        resp_mock = mock.MagicMock()
        resp_mock.status_code = 200
        resp_mock.json.return_value = {"success": True, "message": "Flag accepted"}
        self.session.post.return_value = resp_mock

        verdict, msg = self.adapter.submit_flag(101, "flag{test_passed}")
        self.assertTrue(verdict)
        self.assertEqual(self.adapter.last_verdict, "correct")
        self.session.post.assert_called_with(
            "https://mockctf.test/api/challenges/101/submit",
            json={"flag_val": "flag{test_passed}"},
            timeout=mock.ANY,
        )

        # Incorrect submit
        resp_mock.json.return_value = {"success": False, "message": "Wrong flag"}
        verdict_bad, msg_bad = self.adapter.submit_flag(101, "flag{wrong}")
        self.assertFalse(verdict_bad)
        self.assertEqual(self.adapter.last_verdict, "incorrect")


class TestPlatformReconEngine(unittest.TestCase):
    """Test autonomous reconnaissance and schema deduction."""

    @mock.patch("ctf_downloader.platforms.recon.safe_get")
    @mock.patch("ctf_downloader.platforms.recon.safe_get_json")
    def test_recon_probe_url(self, mock_safe_get_json, mock_safe_get):
        # Mock HTML root
        mock_root_resp = mock.MagicMock()
        mock_root_resp.status_code = 200
        mock_root_resp.text = "<html><head><title>PicoNew CTF 2026</title><meta name='generator' content='PicoPlatform'></head></html>"
        mock_safe_get.return_value = mock_root_resp

        # Mock API responses
        def fake_get_json(sess, url, statuses=(200,)):
            if "/api/v1/challenges" in url:
                return {
                    "data": {
                        "challenges": [
                            {
                                "id": 42,
                                "name": "Super RSA",
                                "category": "crypto",
                                "points": 300,
                                "description": "Factor n",
                                "files": ["/f/key.pem"],
                                "solved": False,
                            }
                        ]
                    }
                }, 200
            elif "/api/auth/me" in url:
                return {"user": {"username": "ctf_recon_pro"}}, 200
            return None, 404

        mock_safe_get_json.side_effect = fake_get_json

        res = PlatformReconEngine.probe_url("https://recon-ctf.example.com")
        self.assertEqual(res.detected_title, "PicoNew CTF 2026")
        self.assertEqual(res.confidence, "high")
        self.assertIn("PicoPlatform", res.html_markers)
        self.assertEqual(res.endpoints_found.get("challenges"), "/api/v1/challenges")
        self.assertEqual(res.endpoints_found.get("auth_check"), "/api/auth/me")

        schema = res.candidate_schema
        self.assertIsNotNone(schema)
        self.assertEqual(schema.schema_mapping.id, "id")
        self.assertEqual(schema.schema_mapping.name, "name")
        self.assertEqual(schema.schema_mapping.points, "points")


class TestDynamicDetectionIntegration(unittest.TestCase):
    """Test detect_platform_info discovering schemas dynamically."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ws_path = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    @mock.patch("ctf_downloader.platforms.detection.safe_get")
    def test_detect_via_custom_schema_html_marker(self, mock_safe_get):
        schema = PlatformSchema(
            key="marker_detected_ctf",
            label="Marker Detected CTF",
            html_markers=["unique-marker-str-12345"],
            endpoints=PlatformEndpoints(challenges="/api/list"),
        )
        PlatformSchemaStore.save(schema, scope="workspace", workspace_path=self.ws_path)

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>Welcome to unique-marker-str-12345 competition!</body></html>"
        mock_safe_get.return_value = mock_resp

        sess = mock.MagicMock()
        sess.cookies.keys.return_value = []

        platform, info = detect_platform_info(
            "https://ctf.marker.test",
            sess,
            quiet=True,
            workspace_path=self.ws_path,
        )

        self.assertEqual(platform.ctf_info.platform_type, "marker_detected_ctf")
        self.assertEqual(info.confidence, "high")
        self.assertEqual(info.platform_type, "marker_detected_ctf")


class TestPlatformSchemaStoreSecurityAndHardening(unittest.TestCase):
    """Test security boundaries, traversal defense, and malformed schema handling."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ws_path = Path(self.tmp_dir.name)
        self._orig_platforms = dict(PLATFORMS)

    def tearDown(self):
        self.tmp_dir.cleanup()
        PLATFORMS.clear()
        PLATFORMS.update(self._orig_platforms)

    def test_key_validation_blocks_traversal(self):
        for bad_key in ("../../evil", "/etc/passwd", "dir/key", "..", ""):
            with self.subTest(bad_key=bad_key):
                with self.assertRaises(ValueError):
                    PlatformSchemaStore.validate_key(bad_key)

    def test_save_blocks_traversal(self):
        with self.assertRaises(ValueError):
            PlatformSchemaStore.save(
                PlatformSchema(key="../../escaped", label="Evil"),
                scope="workspace",
                workspace_path=self.ws_path,
            )

    def test_delete_and_sync_purges_runtime_registry(self):
        schema = PlatformSchema(
            key="purge_test_ctf",
            label="Purge Test CTF",
            endpoints=PlatformEndpoints(challenges="/api/challs"),
        )
        PlatformSchemaStore.save(schema, scope="workspace", workspace_path=self.ws_path)
        PlatformSchemaStore.sync_to_registry(workspace_path=self.ws_path)
        self.assertIn("purge_test_ctf", PLATFORMS)

        # Delete removes from PLATFORMS
        PlatformSchemaStore.delete("purge_test_ctf", scope="workspace", workspace_path=self.ws_path)
        self.assertNotIn("purge_test_ctf", PLATFORMS)

        # Re-save and sync
        PlatformSchemaStore.save(schema, scope="workspace", workspace_path=self.ws_path)
        PlatformSchemaStore.sync_to_registry(workspace_path=self.ws_path)
        self.assertIn("purge_test_ctf", PLATFORMS)

        # Delete file directly on disk, then sync should purge it from PLATFORMS
        disk_file = self.ws_path / ".ctf" / "platforms" / "purge_test_ctf.json"
        disk_file.unlink()
        PlatformSchemaStore.sync_to_registry(workspace_path=self.ws_path)
        self.assertNotIn("purge_test_ctf", PLATFORMS)

    def test_from_dict_handles_malformed_inputs(self):
        malformed = {
            "key": "malformed_ctf",
            "throttle": "not_a_number",
            "endpoints": "not_a_dict",
            "schema_mapping": None,
            "submit_spec": 12345,
            "html_markers": "not_a_list",
        }
        schema = PlatformSchema.from_dict(malformed)
        self.assertEqual(schema.key, "malformed_ctf")
        self.assertEqual(schema.throttle, 3.0)
        self.assertIsInstance(schema.endpoints, PlatformEndpoints)
        self.assertIsInstance(schema.schema_mapping, PlatformSchemaMapping)
        self.assertIsInstance(schema.submit_spec, PlatformSubmitSpec)
        self.assertEqual(schema.html_markers, [])

    def test_configurable_platform_quotes_challenge_id(self):
        schema = PlatformSchema(
            key="quote_test_ctf",
            label="Quote Test CTF",
            endpoints=PlatformEndpoints(submit="/api/challenges/{id}/submit"),
        )
        session = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"success": True}
        session.post.return_value = resp

        adapter = ConfigurablePlatform("https://ctf.example.com", session, schema=schema)
        # Pass a challenge ID with special characters
        verdict, msg = adapter.submit_flag("chall 42/extra", "flag{test}")
        self.assertTrue(verdict)
        # Verify URL quoted the slash and space properly
        session.post.assert_called_with(
            "https://ctf.example.com/api/challenges/chall%2042%2Fextra/submit",
            json={"flag": "flag{test}"},
            timeout=mock.ANY,
        )

    def test_platform_resolver_with_workspace_schema(self):
        from ctf_downloader.services.platform_resolver import PlatformResolver

        schema = PlatformSchema(
            key="resolver_ws_schema",
            label="Resolver WS Schema",
            endpoints=PlatformEndpoints(challenges="/api/list"),
        )
        PlatformSchemaStore.save(schema, scope="workspace", workspace_path=self.ws_path)

        mock_repo = mock.MagicMock()
        mock_repo.root = self.ws_path
        mock_repo.read_challenges.return_value = {
            "ctf_info": {"platform": "resolver_ws_schema", "url": "https://resolver.test"}
        }
        mock_repo.resolve_platform_url.return_value = "https://resolver.test"

        session, platform, info = PlatformResolver.for_workspace(mock_repo)
        self.assertEqual(info.platform_type, "resolver_ws_schema")
        self.assertEqual(platform.ctf_info.platform_type, "resolver_ws_schema")
        self.assertEqual(info.confidence, "high")

    def test_throttle_bounds_and_nan_sanitization(self):
        # NaN throttle
        schema_nan = PlatformSchema.from_dict({"key": "throttle_nan", "throttle": float("nan")})
        self.assertEqual(schema_nan.throttle, 3.0)

        # Negative / zero throttle
        schema_neg = PlatformSchema.from_dict({"key": "throttle_neg", "throttle": -5.0})
        self.assertEqual(schema_neg.throttle, 3.0)

        # Extremely large throttle bounded to 60s
        schema_large = PlatformSchema.from_dict({"key": "throttle_large", "throttle": 999.0})
        self.assertEqual(schema_large.throttle, 60.0)

    def test_configurable_platform_submit_comparison_dsl(self):
        schema = PlatformSchema(
            key="dsl_ctf",
            label="DSL CTF",
            endpoints=PlatformEndpoints(submit="/api/submit"),
            submit_spec=PlatformSubmitSpec(success_field="status:ok"),
        )
        session = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": "ok", "message": "Success"}
        session.post.return_value = resp

        adapter = ConfigurablePlatform("https://dsl.test", session, schema=schema)
        verdict, msg = adapter.submit_flag(1, "flag{dsl}")
        self.assertTrue(verdict)

        # Test failure condition
        resp.json.return_value = {"status": "wrong", "message": "Incorrect"}
        verdict, msg = adapter.submit_flag(1, "flag{wrong}")
        self.assertFalse(verdict)

    def test_configurable_platform_fetch_scoreboard(self):
        schema = PlatformSchema(
            key="score_ctf",
            label="Score CTF",
            endpoints=PlatformEndpoints(scoreboard="/api/scores"),
            supports_scoreboard=True,
        )
        session = mock.MagicMock()
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"standings": [{"pos": 1, "team": "Alpha", "score": 1000}]}
        session.get.return_value = resp

        adapter = ConfigurablePlatform("https://score.test", session, schema=schema)
        scores = adapter.fetch_scoreboard()
        self.assertIn("standings", scores)
        self.assertEqual(len(scores["standings"]), 1)
        self.assertEqual(scores["standings"][0]["team"], "Alpha")

    def test_registry_sync_purges_stale_custom_schemas(self):
        from ctf_downloader.platforms.registry import PLATFORMS
        schema = PlatformSchema(key="stale_to_purge", label="Stale Schema")
        saved = PlatformSchemaStore.save(schema, scope="workspace", workspace_path=self.ws_path)
        PlatformSchemaStore.sync_to_registry(workspace_path=self.ws_path)
        self.assertIn("stale_to_purge", PLATFORMS)
        self.assertEqual(PLATFORMS["stale_to_purge"].source, "custom_schema")

        # Now delete from disk
        saved.unlink()
        # Resync
        PlatformSchemaStore.sync_to_registry(workspace_path=self.ws_path)
        self.assertNotIn("stale_to_purge", PLATFORMS)


if __name__ == "__main__":
    unittest.main()
