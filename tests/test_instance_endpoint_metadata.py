import json
from pathlib import Path

from ctf_downloader.generator.workspace_builder import WorkspaceBuilder
from ctf_downloader.models import Challenge
from ctf_downloader.services.instance_service import InstanceService
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


def _challenge(challenge_id=7, *, connection_info=None, instance_info=None):
    return Challenge(
        id=challenge_id,
        name="Endpoint Box",
        category="Web",
        points=100,
        description="test challenge",
        connection_info=connection_info,
        instance_info=instance_info or {},
    )


def _service(root: Path) -> InstanceService:
    service = InstanceService.__new__(InstanceService)
    service.workspace_path = str(root)
    service.repo = WorkspaceRepo(root)
    return service


def test_downloaded_metadata_always_initializes_empty_instance(tmp_path):
    challenge_dir = Path(WorkspaceBuilder.create_challenge_workspace(
        str(tmp_path), _challenge(), [], [], [], create_solve_template=False))

    metadata = json.loads((challenge_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["instance"] == ""


def test_incremental_refresh_migrates_missing_instance_and_preserves_manual_value(tmp_path):
    challenge_dir = tmp_path / "Web" / "Endpoint Box"
    challenge_dir.mkdir(parents=True)
    metadata_path = challenge_dir / "metadata.json"
    metadata_path.write_text(json.dumps({"id": 7, "name": "Endpoint Box"}), encoding="utf-8")
    repo = WorkspaceRepo(tmp_path)

    assert PullService._refresh_existing_metadata(
        repo, metadata_path, _challenge(connection_info="static.example:31337"), {})
    metadata = repo.read_metadata(metadata_path)
    assert metadata["instance"] == ""

    repo.update_metadata(metadata_path, lambda meta: {**meta, "instance": "manual.example:4444"})
    PullService._refresh_existing_metadata(
        repo, metadata_path, _challenge(connection_info="new-static.example:31337"), {})
    assert repo.read_metadata(metadata_path)["instance"] == "manual.example:4444"


def test_instance_start_writes_endpoint_to_metadata_and_challenges_mirror(tmp_path):
    challenge_dir = tmp_path / "Web" / "Endpoint Box"
    challenge_dir.mkdir(parents=True)
    payload = {
        "id": 7,
        "name": "Endpoint Box",
        "instance_info": {"is_container": True},
    }
    (challenge_dir / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "challenges.json").write_text(
        json.dumps({"challenges": [dict(payload)]}), encoding="utf-8")
    service = _service(tmp_path)

    service._update_local_instance_info(7, "box.example:31337", 120, status="running")

    metadata = json.loads((challenge_dir / "metadata.json").read_text(encoding="utf-8"))
    mirror = json.loads((tmp_path / "challenges.json").read_text(encoding="utf-8"))["challenges"][0]
    assert metadata["instance"] == "box.example:31337"
    assert mirror["instance"] == "box.example:31337"


def test_stopping_preserves_manual_endpoint_but_clears_managed_endpoint(tmp_path):
    challenge_dir = tmp_path / "Web" / "Endpoint Box"
    challenge_dir.mkdir(parents=True)
    payload = {
        "id": 7,
        "name": "Endpoint Box",
        "instance": "manual.example:4444",
        "instance_info": {
            "is_container": True,
            "active_instance": "managed.example:31337",
        },
    }
    (challenge_dir / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "challenges.json").write_text(
        json.dumps({"challenges": [dict(payload)]}), encoding="utf-8")
    service = _service(tmp_path)

    service._update_local_instance_info(7, None, 0, status="stopped")
    assert service.repo.read_metadata(challenge_dir / "metadata.json")["instance"] == "manual.example:4444"
    assert service.repo.read_challenges()["challenges"][0]["instance"] == "manual.example:4444"

    service.repo.update_metadata(challenge_dir / "metadata.json", lambda meta: {
        **meta,
        "instance": "managed.example:31337",
        "instance_info": {**meta["instance_info"], "active_instance": "managed.example:31337"},
    })
    service.repo.mutate_challenges(lambda data: {
        **data,
        "challenges": [{
            **data["challenges"][0],
            "instance": "managed.example:31337",
            "instance_info": {
                **data["challenges"][0]["instance_info"],
                "active_instance": "managed.example:31337",
            },
        }],
    })
    service._update_local_instance_info(7, None, 0, status="stopped")
    meta = service.repo.read_metadata(challenge_dir / "metadata.json")
    assert meta["instance"] in ("", "off")
    assert meta.get("instance_status") == "off"
    assert "start" in meta.get("instance_note", "").lower()
    assert service.repo.read_challenges()["challenges"][0]["instance"] in ("", "off")

