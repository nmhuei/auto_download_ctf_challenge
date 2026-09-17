# Instance Endpoint Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every challenge metadata file a user/script-owned `instance` endpoint string while retaining platform lifecycle compatibility.

**Architecture:** `WorkspaceBuilder` establishes `instance: ""` for new metadata. Incremental pulls preserve it, and `InstanceService` writes an endpoint returned by lifecycle operations while only clearing the endpoint it previously managed.

**Tech Stack:** Python 3, pytest, JSON metadata, existing `WorkspaceRepo` atomic updates.

**Spec:** `docs/superpowers/specs/2026-09-17-instance-endpoint-metadata-design.md`

## Global Constraints

- Every new `metadata.json` contains top-level `"instance": ""`.
- `instance` is user/script-owned data; pull/update must not overwrite it.
- Do not execute content stored in `instance`.
- `instance_info` remains the platform lifecycle record.
- Stop clears `instance` only when it equals the platform-managed active entry.

---

## File structure

- `ctf_downloader/generator/workspace_builder.py` creates the field.
- `ctf_downloader/services/pull_service.py` preserves and migrates it.
- `ctf_downloader/services/instance_service.py` synchronizes lifecycle endpoints.
- `tests/test_instance_endpoint_metadata.py` covers the new contract without network calls.

### Task 1: Create and retain the endpoint field

**Files:**
- Create: `tests/test_instance_endpoint_metadata.py`
- Modify: `ctf_downloader/generator/workspace_builder.py:261-278`
- Modify: `ctf_downloader/services/pull_service.py:689-699, 1001-1035`

**Interfaces:**
- Consumes: `WorkspaceBuilder.create_challenge_workspace(...)` and `PullService._refresh_existing_metadata(...)`.
- Produces: generated metadata containing `instance: ""`; refreshes preserve a user value and migrate absent legacy fields.

- [ ] **Step 1: Write failing tests**

```python
def test_builder_initializes_empty_instance_field(tmp_path, challenge):
    root = WorkspaceBuilder.create_challenge_workspace(
        str(tmp_path), challenge, [], [], [], create_solve_template=False,
    )
    assert json.loads((Path(root) / "metadata.json").read_text())["instance"] == ""

def test_refresh_preserves_and_migrates_instance(repo, metadata_path, challenge):
    metadata_path.write_text(json.dumps({"id": challenge.id, "instance": "nc manual.host 31337"}))
    PullService._refresh_existing_metadata(repo, metadata_path, challenge, {})
    assert json.loads(metadata_path.read_text())["instance"] == "nc manual.host 31337"
    metadata_path.write_text(json.dumps({"id": challenge.id}))
    PullService._refresh_existing_metadata(repo, metadata_path, challenge, {})
    assert json.loads(metadata_path.read_text())["instance"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_instance_endpoint_metadata.py -q`

Expected: field is absent from generated and refreshed metadata.

- [ ] **Step 3: Implement the minimum**

```python
# WorkspaceBuilder metadata
"instance": "",

# PullService refresh mutator, before return
if "instance" not in meta:
    meta["instance"] = ""
    changed[0] = True
```

Add `"instance"` to `_USER_OWNED_META_KEYS` so full rebuild snapshot/restore retains it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_instance_endpoint_metadata.py -q`

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit**

```bash
git add ctf_downloader/generator/workspace_builder.py ctf_downloader/services/pull_service.py tests/test_instance_endpoint_metadata.py
git commit -m "feat(metadata): add persistent instance endpoint field"
```

### Task 2: Synchronize lifecycle endpoint safely

**Files:**
- Modify: `ctf_downloader/services/instance_service.py:467-507`
- Modify: `tests/test_instance_endpoint_metadata.py`

**Interfaces:**
- Consumes: `InstanceService._update_local_instance_info(challenge_id, entry, time_left, status)`.
- Produces: `metadata["instance"]` from an entry; preserves a manual value on stop; clears a matching managed value.

- [ ] **Step 1: Write failing tests**

```python
def test_lifecycle_entry_sets_instance_endpoint(service, metadata_path):
    service._update_local_instance_info("challenge-1", "service.example:443", 600)
    assert json.loads(metadata_path.read_text())["instance"] == "service.example:443"

def test_stop_preserves_manual_endpoint_but_clears_managed_endpoint(service, metadata_path):
    _write_metadata(metadata_path, instance="nc manual.example 31337",
                    active_instance="service.example:443")
    service._update_local_instance_info("challenge-1", None, 0, status="stopped")
    assert _read(metadata_path)["instance"] == "nc manual.example 31337"

    _write_metadata(metadata_path, instance="service.example:443",
                    active_instance="service.example:443")
    service._update_local_instance_info("challenge-1", None, 0, status="stopped")
    assert _read(metadata_path)["instance"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_instance_endpoint_metadata.py -q`

Expected: lifecycle tests fail because the helper only updates `connection_info` and `instance_info`.

- [ ] **Step 3: Implement the minimum**

```python
previous_active = inst.get("active_instance")
if entry:
    m["instance"] = str(entry)
elif status == "stopped" and m.get("instance") == previous_active:
    m["instance"] = ""
```

Capture `previous_active` before removing `active_instance`. Do not modify a nonmatching manual value.

- [ ] **Step 4: Run focused compatibility checks**

Run: `pytest tests/test_instance_endpoint_metadata.py tests/test_instance_contract_matrix.py tests/test_incremental_pull.py -q`

Expected: all selected tests pass without a platform request.

- [ ] **Step 5: Commit**

```bash
git add ctf_downloader/services/instance_service.py tests/test_instance_endpoint_metadata.py
git commit -m "feat(instance): synchronize managed endpoint metadata"
```

### Task 3: Final verification

**Files:**
- Modify: `tests/test_instance_endpoint_metadata.py`

**Interfaces:**
- Consumes: completed builder, refresh, and lifecycle logic.
- Produces: regression coverage for the empty field, legacy migration, preservation, and safe clearing.

- [ ] **Step 1: Add explicit no-execution assertion**

```python
def test_instance_value_is_stored_as_data_only(metadata_path):
    value = "echo should-not-run"
    _write_metadata(metadata_path, instance=value)
    assert _read(metadata_path)["instance"] == value
```

- [ ] **Step 2: Run the final verification command**

Run: `pytest tests/test_instance_endpoint_metadata.py tests/test_instance_contract_matrix.py tests/test_incremental_pull.py tests/test_solver_workspace_rules.py -q && python -m compileall -q ctf_downloader && git diff --check`

Expected: tests pass, compilation succeeds, and the diff has no whitespace errors.

- [ ] **Step 3: Commit**

```bash
git add tests/test_instance_endpoint_metadata.py
git commit -m "test(metadata): cover instance endpoint contract"
```

