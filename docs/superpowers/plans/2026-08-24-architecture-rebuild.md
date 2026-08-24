# Architecture Rebuild Implementation Plan (Phương án A — Layered Monolith + Registry)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tái cấu trúc `ctf_downloader` theo phân lớp Entrypoint → CLI → Services → Registry/Adapters → Storage, đưa chi phí thêm platform mới từ ≥5 file xuống 1 file, xoá ~12 cụm logic trùng lặp — mà 100 test hiện có phải xanh sau từng phase.

**Architecture:** Strangler migration 7 phase trên nhánh `rebuild/architecture`. Module cũ thành facade mỏng giữ nguyên import path; registry decorator cho platform/downloader; WorkspaceRepo là nơi duy nhất biết schema state file.

**Tech Stack:** Python 3.13 stdlib + requests/beautifulsoup4/rich (không thêm dep). Test: unittest-style + pytest runner.

**Spec:** `docs/superpowers/specs/2026-08-24-architecture-rebuild-design.md` (đọc song song — plan này dẫn chiếu mục § của spec).

## Global Constraints

- R1: `python3 -m pytest test_suite.py test_sp1_submit.py test_sp2_download.py test_sp3_recon.py -q` phải **100 passed** sau mỗi task kết thúc một phase.
- R2: Chuỗi message trong `downloaders/manager.py` mà `test_sp2_download.py` assert phải giữ nguyên văn (Phase 6: diff literal trước/sau = rỗng).
- R3: Import path công khai giữ nguyên: `ctf_downloader.{core,submitter,instance_manager,dashboard,ranking}`, `platforms.detector.PlatformDetector.detect_platform/detect_platform_info`, `Challenge/CTFInfo` từ `platforms.base`.
- R4: Cấm import ngược tầng: storage không import services; services không import cli; extractors không import downloaders.
- R5: Không thêm dependency mới; Linux-only.
- Mỗi Task cuối có commit riêng trên `rebuild/architecture`; KHÔNG push.
- Giữ phong cách code hiện tại: Logger tiếng Việt, rich console, dataclass, type hint nhẹ.

---

## Phase 1 — models.py

### Task 1: Tách dataclass vào models.py

**Files:**
- Create: `ctf_downloader/models.py`
- Modify: `ctf_downloader/platforms/base.py`
- Test: `test_arch_phase1.py` (gốc repo)

**Interfaces:**
- Produces: `from ctf_downloader.models import Challenge, CTFInfo` ; `Verdict = Literal["correct","incorrect","unknown","ratelimited"]` trong `ctf_downloader/models.py`. `platforms.base` tiếp tục export `Challenge`, `CTFInfo`.

- [ ] **Step 1: Viết test thất bại**

```python
# test_arch_phase1.py
import unittest
class TestModels(unittest.TestCase):
    def test_models_module_and_reexport(self):
        from ctf_downloader import models
        from ctf_downloader.platforms.base import Challenge as C1, CTFInfo as I1
        self.assertIs(models.Challenge, C1)
        self.assertIs(models.CTFInfo, I1)
        self.assertEqual(models.Verdict.__args__, ("correct","incorrect","unknown","ratelimited"))
```

- [ ] **Step 2: Chạy xác nhận FAIL** — `python3 -m pytest test_arch_phase1.py -q` → ImportError models.

- [ ] **Step 3: Implement** — Cut nguyên văn định nghĩa `@dataclass class Challenge` (base.py:5-23) và `@dataclass class CTFInfo` (base.py:24-34) sang `ctf_downloader/models.py`; thêm `Verdict = Literal[...]`. Trong `base.py` thay bằng `from ..models import Challenge, CTFInfo, Verdict  # noqa: F401` (re-export). Không đổi field nào.

- [ ] **Step 4: PASS** — pytest test_arch_phase1 + full suite (R1).
- [ ] **Step 5: Commit** — `git commit -m "refactor(phase1): tách Challenge/CTFInfo/Verdict sang models.py"`

---

## Phase 2 — Storage layer

### Task 2: fileio + constants

**Files:**
- Create: `ctf_downloader/storage/__init__.py`, `ctf_downloader/storage/fileio.py`, `ctf_downloader/storage/constants.py`
- Test: `test_arch_phase2.py`

**Interfaces:**
- Produces:
  - `atomic_write_text(path: Path, text: str) -> None` ; `atomic_write_json(path: Path, obj) -> None`
  - `locked_update_json(path: Path, mutator: Callable[[dict], dict|None]) -> dict` (fcntl.flock LOCK_EX; file hỏng → coi như `{}` + backup `.bak`)
  - constants: `SOLVED_DONE`, `SOLVED_TODO`, `SOLVED_MARKERS_DONE` (tuple), `TARGET_CONNECTION_FMT`, `SUMMARY_FILES_LINE`, `LIVE_RANK_PREFIX`, `SOLVE_VAR_NAMES=("HOST","PORT","TARGET_URL")`, `FLAG_PLACEHOLDER="FLAG{...}"`, `DEFAULT_CATEGORY="Misc"`.

- [ ] **Step 1: Test**

```python
import json, tempfile, pathlib, unittest
from ctf_downloader.storage.fileio import atomic_write_json, locked_update_json
class TestFileIO(unittest.TestCase):
    def test_atomic_roundtrip_and_corrupt_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)/"x.json"
            atomic_write_json(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text()), {"a": 1})
            p.write_text("{corrupt")            # hỏng
            out = locked_update_json(p, lambda d: {**(d or {}), "b": 2})
            self.assertEqual(out, {"b": 2})
            self.assertTrue((pathlib.Path(d)/"x.json.bak").exists())
```

- [ ] **Step 2: FAIL** — pytest → ModuleNotFoundError.
- [ ] **Step 3: Implement** — `atomic_write_*`: ghi `<name>.tmp` rồi `os.replace(tmp, path)`; `locked_update_json`: mở `open(path, "a+")`, `fcntl.flock(f, LOCK_EX)`, seek(0) đọc JSON, try parse trừ corrupt (copy nội dung cũ sang `.bak` trước khi ghi đè), gọi mutator, atomic write, trả dict. `constants.py`: copy NGUYÊN VĂN các literal đang tồn tại từ: dashboard.py:33-41 (`SOLVED_MARKERS_DONE`), workspace_builder.py:316 (`TARGET_CONNECTION_FMT` dạng `- Target Connection: \`{info}\``), summary_generator.py:47 (`SUMMARY_FILES_LINE`), ranking.py:186-189 (`LIVE_RANK_PREFIX`), workspace_builder.py:197/:336 (`FLAG_PLACEHOLDER`). Sau đó sửa 4 module trên IMPORT từ constants (thay literal inline) — hành vi không đổi.
- [ ] **Step 4: PASS** — pytest test_arch_phase2 + full suite.
- [ ] **Step 5: Commit** — `"refactor(phase2): storage/fileio + constants chia sẻ"`

### Task 3: WorkspaceRepo

**Files:**
- Create: `ctf_downloader/storage/workspace_repo.py`
- Test: `test_arch_phase2.py` (thêm class TestWorkspaceRepo)

**Interfaces:**
- Consumes: Task 2 fileio/constants.
- Produces (đúng tên, Phase sau phụ thuộc):
```python
class WorkspaceRepo:
    def __init__(self, root): ...                      # root: Path|str workspace
    def read_challenges(self) -> dict                  # challenges.json; lỗi → {}
    def write_challenges(self, data: dict) -> None     # atomic+lock
    def update_ctf_info(self, **fields) -> None        # merge vào ctf_info
    def resolve_platform_url(self) -> str | None       # ctf_info.url (fallback submit_endpoint qua iter_challenges)
    def find_challenge(self, q) -> dict | None         # exact id(str) → exact name.lower() → substring name; None nếu không khớp
    def iter_challenges(self) -> Iterator[Path]        # yield đường dẫn metadata.json (os.walk duy nhất)
    def read_metadata(self, p: Path) -> dict
    def write_metadata(self, p: Path, meta: dict) -> None   # atomic
    def is_container(self, meta: dict) -> bool         # predicate rộng nhất: instance_info.is_container or type=='DynamicContainer' or raw.type in ('dynamic_docker','DynamicContainer') or 'container' in tags — an toàn khi raw None
    def load_submit_history(self) -> dict              # schema {"entries":[...]}; corrupt→{} + .bak
    def save_submit_history(self, hist: dict) -> None
    def read_solved_state(self, readme_paths: list[Path]) -> bool
    def write_solved_state(self, readme_paths: list[Path], solved: bool) -> int  # trả số file đổi
    def patch_summary_live_rank(self, rank_line: str) -> bool    # chèn/thay trước SUMMARY_FILES_LINE
```

- [ ] **Step 1: Test** — tạo temp workspace giả lập (challenges.json + 1 challenge folder có metadata.json/writeup/README.md chứa `- [ ] Solved`) rồi assert: find_challenge("web") partial match; is_container với `"raw": null` KHÔNG crash và False; write_solved_state đổi marker thành `[x]`; patch_summary_live_rank chèn đúng vị trí dòng `- **Total Files Downloaded**:`; gọi lần 2 thì replace chứ không nhân đôi.
- [ ] **Step 2: FAIL** → **Step 3: Implement** theo spec §3.4 (logic di chuyển từ: submitter._load_challenges/_resolve_url_from_workspace, instance_manager.find_challenge/_update_local_instance_info phần ghi, dashboard._scan_local_challenges, ranking._save_ranking_docs phần patch SUMMARY). **Step 4: PASS + full suite. Step 5: Commit** — `"refactor(phase2): WorkspaceRepo hợp nhất truy cập state file"`

### Task 4: Chuyển 4 module dùng WorkspaceRepo

**Files:**
- Modify: `ctf_downloader/dashboard.py`, `instance_manager.py`, `ranking.py`, `submitter.py`

**Interfaces:**
- Consumes: WorkspaceRepo (Task 3).
- Produces: mỗi module giữ nguyên API công khai; bên trong thay mọi đọc/ghi trực tiếp state file bằng repo. Xoá dead path `data.get("platform_url")` (submitter.py:84).

- [ ] **Step 1: Test bảo vệ hành vi hiện tại** (thêm vào test_arch_phase2.py): dashboard stats trên temp workspace = số liệu kỳ vọng; instance list container gồm bài có raw.type='dynamic_docker'; ranking patch SUMMARY idempotent; submitter resolve URL từ workspace không có ctf_info.url (fallback metadata submit_endpoint). Đây là characterization test viết TRƯỚC khi sửa.
- [ ] **Step 2: FAIL nếu thiếu** (bỏ qua nếu characterization đã pass — chúng là lưới) → **Step 3: Sửa từng module**, mỗi module xong chạy full suite. Thứ tự: dashboard → instance_manager → ranking → submitter.
- [ ] **Step 4: PASS 100 test (R1). Step 5: Commit** — `"refactor(phase2): 4 module dùng WorkspaceRepo, xoá dead platform_url"`

---

## Phase 3 — Auth & Session

### Task 5: session_factory + auth_service

**Files:**
- Create: `ctf_downloader/services/__init__.py`, `services/session_factory.py`, `services/auth_service.py`
- Modify: `ctf_downloader/cli.py` (get_auth_for_workspace delegate), `core.py`, `interactive_menu.py` (load/save_global_config re-export từ storage.global_config)
- Create: `storage/global_config.py` (move load_global_config/save_global_config nguyên văn từ interactive_menu.py:17-32; interactive_menu re-export)
- Test: `test_arch_phase2.py` → chuyển toàn bộ sang file mới `test_arch_phase3.py`

**Interfaces:**
- Produces:
```python
# services/session_factory.py
def create_session(cookie=None, token=None, custom_headers=None, timeout=30): ...  # wrap utils.http_client.create_session — điểm tạo session DUY NHẤT
def thread_local_sessions(master) -> ContextManager  # Phase 5 dùng: mỗi worker thread copy cookies+headers từ master
# services/auth_service.py
class AuthService:
    @staticmethod
    def resolve(workspace, cookie_arg=None, token_arg=None) -> tuple[str|None, str|None]
    # ưu tiên arg CLI > global config auth map[key=os.path.abspath(workspace)]
```

- [ ] **Step 1: Test**: resolve ưu tiên arg; resolve đọc auth map từ config.json giả lập; get_auth_for_workspace của cli trả cùng kết quả AuthService.resolve.
- [ ] **Step 2: FAIL** → **Step 3: Implement** (move + facade; cli.get_auth_for_workspace thành `return AuthService.resolve(...)` giữ chữ ký cũ). **Step 4: PASS + R1. Step 5: Commit** — `"refactor(phase3): session_factory + auth_service"`

---

## Phase 4 — Platform registry

### Task 6: registry + decorate 5 platform

**Files:**
- Create: `ctf_downloader/platforms/registry.py`
- Modify: `platforms/{base,capabilities,ctfd,gzctf,rctf,custom_rest,generic_html}.py`
- Test: `test_arch_phase4.py`

**Interfaces:**
- Consumes: PlatformInfo (capabilities.py hiện có).
- Produces:
```python
# platforms/registry.py
@dataclass(frozen=True)
class PlatformSpec:
    key: str; label: str; cls: type
    throttle: float = 2.0
    html_markers: tuple[str, ...] = ()
    cookie_hints: tuple[str, ...] = ()
    probes: tuple = ()
    supports_container: bool = False
    supports_scoreboard: bool = False
    rules_via_api: bool = False
PLATFORMS: dict[str, PlatformSpec]
def register(key=None, *, label=None, **kw)          # decorator; gán cls.spec
def get_spec(key: str) -> PlatformSpec               # KeyError → UnknownPlatformError(ValueError)
# capabilities.py: PLATFORM_TYPES = ("unknown", *sorted(PLATFORMS))
```

- [ ] **Step 1: Test**: đăng ký platform giả trong test; PLATFORM_TYPES chứa 5 key thật; capabilities.PLATFORM_TYPES không còn hardcode; throttle của gzctf=2.0, ctfd=6.0, rctf=5.0 (giá trị copy từ THROTTLE_BY_PLATFORM hiện tại submitter.py:25-29 — sau đó submitter đọc từ registry, xoá dict hardcode).
- [ ] **Step 2: FAIL** → **Step 3: Implement** decorator theo spec §3.2; trang trí 5 class (markers/cookie_hints/probes copy từ detector.py hiện tại: markers tầng 1 :200-213, cookies :216-231, probes :103-171 thành các hàm module-level nhận `(origin, session, info, done) -> bool`); **chưa đụng detection pipeline ở task này** (detector cũ vẫn chạy như cũ — chỉ chuẩn bị dữ liệu). **Step 4: PASS + R1 (sp3 16 test đặc biệt quan trọng). Step 5: Commit** — `"refactor(phase4): platform registry + decorator 5 platform"`

### Task 7: detection từ registry + platform_resolver + urlnorm

**Files:**
- Create: `platforms/detection.py`, `utils/urlnorm.py`, `services/platform_resolver.py`
- Modify: `platforms/detector.py` (facade), `config.py` (validate dùng urlnorm), `instance_manager.py` (xoá `_init_platform` if/elif + hardcode `'infosecptit'` + fix gọi `detect_and_init()` không tồn tại — instance_manager.py:68)
- Test: `test_arch_phase4.py` bổ sung

**Interfaces:**
- Produces:
```python
# utils/urlnorm.py
def normalize_base_url(url: str) -> str   # hợp nhất suffix-stripping: /challenges /scoreboard /login /register /users /teams /rules /notifications
# platforms/detection.py — giữ NGUYÊN chữ ký & hành vi:
def detect_platform_info(url, session, cookie_hint=None) -> tuple[BasePlatform, PlatformInfo]
def detect_platform(url, session) -> BasePlatform
# detector.py: class PlatformDetector delegating (R3)
# services/platform_resolver.py
class PlatformResolver:
    @staticmethod
    def for_workspace(repo: WorkspaceRepo, cookie=None, token=None) -> tuple[requests.Session, BasePlatform, PlatformInfo]
```

- [ ] **Step 1: Test**: sp3 cũ phải pass không sửa; thêm test resolver chọn GZCTF từ workspace có ctf_info.platform="gzctf" (mock session); urlnorm strip đủ 8 suffix; instance_manager không còn chuỗi "infosecptit"/"detect_and_init" (assert bằng `inspect.getsource` hoặc grep trong test).
- [ ] **Step 2: FAIL** → **Step 3: Implement**: detection.py = nội dung pipeline 4 tầng hiện có của detector.py, nguồn markers/probes đọc PLATFORMS registry; detector.py thành facade. Fix bug detect_and_init bằng PlatformResolver. **Step 4: PASS + R1. Step 5: Commit** — `"refactor(phase4): detection registry-driven, platform_resolver, fix detect_and_init"`

---

## Phase 5 — Service extraction

### Task 8: pull/status service + facades core/dashboard

**Files:** Create `services/pull_service.py`, `services/status_service.py`; Modify `core.py`, `dashboard.py`
**Interfaces:**
- Produces: `PullService.run(config: DownloaderConfig) -> dict` (thân logic `CTFDownloader.run`, gồm thread-local sessions từ session_factory — sửa luôn bug session đa luồng §8.8 spec); `StatusService.summary_stats(repo)`, `.scan_all_workspaces(base_dir)` (hợp nhất 3 bản scan: cli.py:181-206/manage.py:10-34/interactive_menu.py:416-439 — cả 3 chỗ sau đó gọi hàm này).
- Steps: characterization test trước (workspaces scan trên temp tree) → move → facade → **PASS R1 → Commit** `"refactor(phase5): pull/status services"`

### Task 9: submit/instance/rank service + facades

**Files:** Create `services/submit_service.py`, `services/instance_service.py`, `services/rank_service.py`; Modify `submitter.py`, `instance_manager.py`, `ranking.py`, `cli.py`
**Interfaces:**
- Produces: `SubmitService` giữ NGUYÊN method công khai + hằng message mà test_sp1 assert (`NO_FORMAT_MESSAGE`, chuỗi "Already solved"/"Blacklisted"); `InstanceService` + method mới `sync_containers()` (logic --sync từ instance.py:39-131); `RankService.display_and_update`.
- `FlagSubmitter/InstanceManager/RankingManager` thành facade mỏng (constructor cùng chữ ký cũ, delegate service). Menu instance ×3 (cli.handle_instance/instance.py/interactive_menu) gọi chung `InstanceService.interactive_pick()`.
- Steps: characterization (sp1 43 test chính là lưới — KHÔNG được sửa assertion) → move từng service, chạy full suite sau từng cái → **PASS R1 → Commit** `"refactor(phase5): submit/instance/rank services + facades"`

---

## Phase 6 — Downloader registry

### Task 10: downloader registry + cắt lazy-import mega

**Files:** Create `downloaders/registry.py`; Modify `downloaders/manager.py`, `extractors/link_extractor.py`
**Interfaces:**
- Produces:
```python
@register_downloader("gdrive", domains=("drive.google.com","docs.google.com"))
class GDriveDownloader: ...      # tương tự dropbox/mediafire/mega/http
# manager.download_url(): tra DOWNLOADERS[link.link_type]; default = HttpDownloader (generic_url/direct_file/github/discord...)
```
- link_extractor.py:121 lazy-import MegaDownloader → XOÁ; availability check megatools do manager thực hiện lúc init (message giữ nguyên văn test SP2).
- **R2 bắt buộc**: trước khi sửa, xuất danh sách chuỗi message manager.py; sau khi sửa diff phải rỗng; 31 test sp2 là gate.
- Steps: test dispatch table (link_type giả → handler giả) → implement → **PASS R1 → Commit** `"refactor(phase6): downloader registry"`

---

## Phase 7 — CLI mỏng + entrypoint shims

### Task 11: cli_commands + cli_legacy + prompt đẩy xuống services

**Files:** Create `cli_commands.py`, `cli_legacy.py`; Modify `cli.py`, `interactive_menu.py`, script root `main.py/ctf.py/submit.py/manage.py/instance.py/rank.py`
**Interfaces:**
- Produces: `cli_legacy.legacy_{submit,manage,instance,rank}_main()` (argparse nguyên văn từ 4 script); script root shim ≤10 dòng; `cli_commands.handle_{pull,status,workspaces,instance,submit,rank}` không còn `input()` nào (grep kiểm chứng) — wizard nằm ở InstanceService.interactive_pick / SubmitService.
- Steps: smoke help từng entrypoint so sánh stdout trước/sau (lưu snapshot trước khi sửa) → move → **PASS R1 + smoke offline (`workspaces`, `status -w PTIT_CTF_2026`, `instance.py -l`) → Commit** `"refactor(phase7): CLI mỏng + entrypoint shims"`

### Task 12: Dọn dẹp + DoD checklist

- [ ] `grep -rn "_resolve_url\|scan_all_workspaces\|get_auth_for_workspace" ctf_downloader/ | grep -v services/ | grep -v storage/` → chỉ còn facade delegation.
- [ ] Fixture test chứng minh thêm platform mới = 1 file: tạo `tests fixture` đăng ký platform giả chỉ bằng 1 module rồi detect được (thêm vào test_arch_phase4.py).
- [ ] README cập nhật cây thư mục + hướng dẫn "Thêm platform mới".
- [ ] Full suite + smoke offline lần cuối → **Commit** `"docs(phase7): README kiến trúc mới + DoD"`.

---

## Self-Review đã thực hiện

- Spec coverage: §3.1 cây thư mục ↔ Tasks 1-11 (mỗi file mới có task sở hữu); §3.2↔T6-T7; §3.3↔T10; §3.4↔T2-T4; §3.5↔T5; §5 phases↔Tasks; §8 bugs: #1/#2↔T7, #3↔T4, #4/#5↔T10/T7, #6↔T3-T4, #7↔T2-T4 (constants + anchor regex khi move), #8↔T8. ✓
- Type consistency: tên method WorkspaceRepo/AuthService/PlatformResolver thống nhất giữa các Task Interfaces. ✓
- Không placeholder: mọi step có code/lệnh/literal nguồn cụ thể. ✓
