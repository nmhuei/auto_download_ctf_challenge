# Spec — Rebuild kiến trúc ctf_downloader (Phương án A: Layered Monolith + Registry)

> **Ngày:** 24/08/2026 · **Nhánh:** `rebuild/architecture` (đóng băng gốc: tag `v2.1-frozen`, commit `ff1a023`)
> **Nguồn tổng hợp:** 3 agent nghiên cứu song song — đề xuất kiến trúc (3 phương án), audit hiện trạng, khảo sát pattern tham khảo (ctfcli, pwntools, ctfbridge/ctf-dl, CTFx).

---

## 1. Mục tiêu & phi mục tiêu

### Mục tiêu
1. Thêm platform mới = **1 file + 0-1 dòng đăng ký** (hiện tại ≥5 file); thêm downloader = 1 file tự khai báo pattern.
2. Xoá ~12 cụm logic trùng lặp (~300-400 dòng): workspace-scan ×3, URL-resolve ×4, auth-resolve ×2, container-predicate ×3, challenge-lookup ×4, menu instance ×3…
3. Mọi truy cập state file (`challenges.json`, `metadata.json`, `SUMMARY.md`, `RANKING.md`, `submit_history.json`) đi qua **một lớp duy nhất**.
4. CLI layer mỏng tuyệt đối; prompt/`input()` nằm ở service layer.
5. Sửa kèm các bug phát hiện trong quá trình audit (mục 8).

### Phi mục tiêu
- Không đổi hành vi/lệnh/cờ CLI nhìn từ người dùng cuối.
- Không đổi format state file hiện có → không cần migrate dữ liệu workspace cũ.
- Không chuyển sang hexagonal/plugin-package/multi-repo (phương án B/C bị loại).
- Không đụng bảo mật (credentials plaintext… — user quyết định bỏ qua giai đoạn này).

## 2. Ràng buộc bắt buộc

| # | Ràng buộc | Biện pháp kiểm chứng |
|---|---|---|
| R1 | 100 test hiện có phải xanh sau MỖI giai đoạn migrate | Chạy `pytest test_suite.py test_sp1_submit.py test_sp2_download.py test_sp3_recon.py -q` cuối mỗi phase |
| R2 | Giữ nguyên văn chuỗi message mà test SP2 assert trong `downloaders/manager.py` khi thay if/elif bằng registry | Diff chuỗi trước/sau phải rỗng |
| R3 | Import path công khai hiện tại phải tiếp tục tồn tại: `ctf_downloader.{core,submitter,instance_manager,dashboard,ranking}`, `platforms.detector.PlatformDetector.detect_platform*`, dataclass ở `platforms.base` | File cũ thành facade mỏng re-export |
| R4 | Cấm import ngược tầng (service→CLI, storage→service, extractor→downloader) | Review thủ công + quy ước ghi trong CLAUDE.md sau này |
| R5 | Linux-only, Python 3.13, không thêm dependency mới | — |

## 3. Kiến trúc mục tiêu

```
┌─ Entrypoints ─ main.py/ctf.py/submit.py/manage.py/instance.py/rank.py → shim ≤10 dòng
├─ Interface ─── cli.py (parser+dispatch thuần) · cli_commands.py (handlers)
│                interactive_menu.py (wizard rich, gọi services)
├─ Service ───── services/: pull · submit · instance · rank · status
│                services/auth_service.py · session_factory.py · platform_resolver.py
├─ Registry ──── platforms/registry.py (@register_platform)
│                downloaders/registry.py (@register_downloader)
├─ Storage ───── storage/workspace_repo.py · storage/global_config.py
│                storage/constants.py (marker strings chia sẻ)
│                storage/fileio.py (atomic write + flock)
├─ Adapters ──── platforms/{ctfd,gzctf,rctf,custom_rest,generic_html}.py
│                downloaders/* · generator/* · extractors/*
└─ Utils ─────── utils/* (giữ nguyên) · models.py (dataclass trung lập)
```

### 3.1 Cây thư mục mục tiêu

```
ctf_downloader/
├── __init__.py              # re-export API công khai
├── cli.py                   # FACADE: build_unified_parser + main + get_auth_for_workspace (delegate auth_service)
├── cli_commands.py          # handle_pull/status/workspaces/instance/submit/rank → services
├── cli_legacy.py            # argparse của submit.py/manage.py/instance.py/rank.py cũ
├── config.py                # DownloaderConfig (giữ nguyên)
├── core.py                  # CTFDownloader → wrapper mỏng quanh PullService (giữ class name)
├── dashboard.py             # delegate status_service (giữ class name)
├── interactive_menu.py      # wizard, giữ vị trí load_global_config (re-export)
├── models.py                # Challenge, CTFInfo, DownloadResult, SubmitVerdict (move từ base.py)
├── submitter.py             # facade mỏng quanh SubmitService
├── instance_manager.py      # facade mỏng quanh InstanceService
├── ranking.py               # facade mỏng quanh RankService
├── storage/
│   ├── workspace_repo.py    # WorkspaceRepo: challenges.json, metadata.json scan,
│   │                        #   SUMMARY.md, RANKING.md, submit_history.json, flag-format cache
│   ├── global_config.py     # load/save ~/.config/ctf_toolkit/config.json
│   ├── constants.py         # SOLVED_MARKERS, TARGET_CONNECTION_FMT, SOLVE_VARS, PLACEHOLDER_FLAG…
│   └── fileio.py            # atomic_write_text/json + locked_json_update (fcntl.flock)
├── services/
│   ├── session_factory.py   # create_session — DUY NHẤT nơi tạo requests.Session
│   ├── auth_service.py      # get_auth_for_workspace + resolve cookie-file/string
│   ├── platform_resolver.py # url|workspace → (session, platform, PlatformInfo)
│   ├── pull_service.py      # pipeline detect→auth→fetch→download→build→summary
│   ├── submit_service.py    # gate format + blacklist + throttle + verdict
│   ├── instance_service.py  # start/stop/extend/status/list/sync + sync local files
│   ├── rank_service.py      # fetch scoreboard + render + RANKING.md/SUMMARY patch
│   └── status_service.py    # stats/tree + scan_all_workspaces (duy nhất)
├── platforms/
│   ├── registry.py          # PLATFORMS dict + @register_platform(key, label, throttle,
│   │                        #   html_markers, cookie_hints, probes, has_container…)
│   ├── detection.py         # pipeline 4 tầng đọc metadata từ registry;
│   │                        #   detect_platform_info/detect_platform giữ nguyên chữ ký
│   ├── detector.py          # facade: PlatformDetector delegating detection.py
│   ├── capabilities.py      # PlatformInfo (giữ nguyên); PLATFORM_TYPES sinh từ registry
│   ├── base.py              # BasePlatform + re-export models
│   └── {ctfd,gzctf,rctf,custom_rest,generic_html}.py  # thêm decorator, thân giữ nguyên
├── downloaders/
│   ├── registry.py          # DOWNLOADERS dict + @register_downloader(link_type, url_patterns)
│   └── manager.py …         # dispatch tra bảng; ExtractedLink classification mega về đây
├── generator/, extractors/          # giữ nguyên (trừ cắt lazy-import mega ở link_extractor)
└── utils/
    ├── {logger,sanitize,flag_format,http_client}.py  # giữ nguyên
    └── urlnorm.py                    # MỚI (Phase 4): hợp nhất 2 bộ URL-suffix stripping
```

Root scripts: `main.py`/`ctf.py` shim gọi `cli.main`; `submit.py`/`manage.py`/`instance.py`/`rank.py` shim gọi `cli_legacy.legacy_{submit,manage,instance,rank}_main`.

### 3.2 Thiết kế registry platform

```python
# platforms/registry.py
PLATFORMS: dict[str, PlatformSpec] = {}

@dataclass(frozen=True)
class PlatformSpec:
    key: str                      # "gzctf"
    label: str                    # "GZCTF" (cho bảng workspaces/dashboard)
    cls: type                     # class adapter
    throttle: float = 2.0         # giây giữa 2 lần submit
    html_markers: tuple[str, ...] = ()   # chuỗi nhận diện tầng 1
    cookie_hints: tuple[str, ...] = ()   # tên cookie tầng 2
    probes: tuple[Callable, ...] = ()    # hàm probe tầng 3 (origin, session, info, done) -> bool
    supports_container: bool = False
    supports_scoreboard: bool = False
    rules_via_api: bool = False

def register(key=None, *, label=None, **kw):
    def deco(cls):
        spec = PlatformSpec(key=key or cls.__name__.lower(), label=label or key, cls=cls, **kw)
        PLATFORMS[spec.key] = spec
        cls.spec = spec
        return cls
    return deco

# platforms/gzctf.py — chỉ thêm phần trang trí, thân class không đổi:
@register("gzctf", label="GZCTF", throttle=2.0,
          html_markers=("GZCTF", "GZ::CTF"),
          cookie_hints=("GZCTF_Token",),
          probes=(probe_api_config, probe_game_recent),
          supports_container=True, supports_scoreboard=True, rules_via_api=True)
class GZCTFPlatform(BaseCTFPlatform): ...
```

- `capabilities.PLATFORM_TYPES` sinh tự: `tuple(PLATFORMS)` (giữ thêm giá trị legacy `"unknown"`).
- `detector.detection.py`: pipeline 4 tầng hiện có giữ nguyên logic thứ tự; dữ liệu markers/cookies/probes lấy từ registry thay vì hardcode. Kết quả vẫn `(instance, PlatformInfo)`; instance được gán `.info`.
- `THROTTLE_BY_PLATFORM` trong submitter → `PLATFORMS[key].throttle`.
- `instance_manager._init_platform` if/elif + hardcode `'infosecptit'` → xoá, dùng `platform_resolver`.

### 3.3 Thiết kế registry downloader

```python
# downloaders/registry.py
DOWNLOADERS: dict[str, type] = {}

def register_downloader(link_type: str, *, domains: tuple[str, ...] = (), extensions: tuple[str, ...] = ()): ...
# manager.download_url(): tra DOWNLOADERS[link.link_type]; nhánh generic http giữ nguyên là default.
# link_extractor: BỎ lazy-import MegaDownloader (:121); mega availability check do manager thực hiện.
```

### 3.4 WorkspaceRepo (storage)

```python
class WorkspaceRepo:
    def __init__(self, root: Path): ...
    # challenges.json
    def read_challenges(self) -> dict          # load + validate tối thiểu, error chuẩn hoá
    def write_challenges(self, data: dict)     # atomic + flock
    def update_ctf_info(self, **fields)        # flag_format cache v.v.
    def resolve_platform_url(self) -> str|None # hợp nhất 4 bản _resolve_url (fallback đầy đủ nhất)
    def find_challenge(self, q) -> dict|None   # hợp nhất 4 pattern lookup (exact id → exact name → partial; trả kèm nguồn khớp)
    # metadata.json
    def iter_challenges(self) -> Iterator[ChallengeFolder]   # os.walk duy nhất
    def read_metadata(self, path) / write_metadata(path, meta)
    def is_container(self, meta) -> bool       # predicate duy nhất (hợp nhất 3 bản, dùng tập điều kiện rộng nhất của instance_manager)
    # SUMMARY.md / RANKING.md / submit_history.json / solved-markers
    def read_summary(self) / patch_summary_live_rank(self, rank_line)
    def write_ranking_md(self, content)
    def load_submit_history(self) / save_submit_history(self, hist)   # giữ schema + .bak
    def read_solved_state(self, readme_paths) / write_solved_state(...)  # marker từ constants.py
```

- `constants.py` chứa mọi chuỗi literal chia sẻ: `SOLVED_DONE = "- [x] Solved"`, `SOLVED_TODO = "- [ ] Solved"`, `TARGET_CONNECTION_LINE = "- Target Connection: \`{info}\`"`, `SUMMARY_FILES_LINE = "- **Total Files Downloaded**:"`, biến template solve.py (`HOST`, `PORT`, `TARGET_URL`), placeholder flag — cả `workspace_builder` lẫn các service import từ đây.
- Regex patch `solve.py` neo đầu dòng `^\s*(HOST|PORT|TARGET_URL)\s*=`, `count=1`.
- Ghi file: `fileio.atomic_write_*` cho mọi JSON/md; `locked_json_update` cho challenges.json/metadata.json.

### 3.5 Auth & session

```python
# services/session_factory.py — bọc utils.http_client.create_session (không đổi nội dung)
# services/auth_service.py
class AuthService:
    @staticmethod
    def resolve(workspace, cookie_arg=None, token_arg=None) -> tuple[str|None, str|None]
    # ưu tiên: arg CLI > global config auth map (key = os.path.abspath chuẩn hoá)
# cli.get_auth_for_workspace = AuthService.resolve (facade giữ tên)
```

### 3.6 Services & facades

- `pull_service.PullService.run(config)`: thân logic từ `core.CTFDownloader.run`; `core.CTFDownloader` giữ constructor + method cũ, delegate.
- `submit_service.SubmitService`: thân `FlagSubmitter` (gate/blacklist/throttle/auto-scan). `FlagSubmitter` facade giữ nguyên method công khai (`submit`, `auto_scan_and_submit`, `resolve_flag_format`, `interactive_submit`) và **hằng message** (`NO_FORMAT_MESSAGE`…) vì test SP1 assert trực tiếp.
- `instance_service.InstanceService`: thân `InstanceManager`; bổ sung `sync_containers()` (logic `--sync` từ `instance.py`) để CLI và script cùng dùng.
- `status_service.StatusService.get_summary_stats/render_tree` từ dashboard; `scan_all_workspaces()` duy nhất cho cli/manage/menu.
- `rank_service.RankService` từ ranking.

## 4. Hợp đồng tương thích người dùng cuối

- Lệnh + cờ: `ctf pull/status/workspaces/instance/submit/rank/menu` và 4 script legacy — argv/help text KHÔNG đổi.
- State file: format byte-compatible với hiện tại (kể cả field mở rộng `ctf_info.flag_format`).
- Console scripts `ctf`, `ctfcli`, `ctf-tool` không đổi.

## 5. Chiến lược migrate — 7 giai đoạn strangler

Mỗi giai đoạn: implement → chạy 100 test → xanh mới sang giai đoạn kế → commit riêng trên `rebuild/architecture`.

| Phase | Nội dung | Gate test | File chính |
|---|---|---|---|
| 1 | Tạo `models.py` (move dataclass), `base.py` re-export | test_suite | models.py, base.py |
| 2 | `storage/` (repo + constants + fileio); sửa tuần tự dashboard → instance_manager → ranking → submitter dùng repo | test_suite + sp1 cuối bước | storage/*, 4 module trên |
| 3 | `session_factory` + `auth_service`; `cli.get_auth_for_workspace` delegate | sp1/sp3 | services/auth*, cli.py |
| 4 | `platforms/registry.py` + decorator 5 platform; detector → facade; xoá brute-force còn sót, fix `detect_and_init` qua `platform_resolver` | sp3 (16) | platforms/* |
| 5 | Tách `services/*.py` từ core/submitter/instance_manager/ranking/dashboard; file cũ thành facade | sp1 (43) | services/*, 5 module |
| 6 | `downloaders/registry.py` thay if/elif; cắt lazy-import mega khỏi link_extractor; **diff chuỗi message = rỗng** | sp2 (31) | downloaders/*, extractors/link_extractor.py |
| 7 | Entrypoint shims + `cli_legacy.py`; đẩy `input()` khỏi cli_commands xuống services; gộp workspace-scan | test_suite smoke | cli*, script root |

## 6. Test strategy

- Giữ nguyên 100 test hiện có làm regression net; chỉ Phase 7 được phép sửa lại phần smoke gắn entrypoint.
- Thêm test mới mỗi phase vào `test_arch_phase{N}.py`: registry round-trip, WorkspaceRepo atomic/lock (mock flock), auth priority, resolver chọn đúng platform theo PlatformInfo.
- Sau Phase cuối: chạy thêm smoke thật offline (`workspaces`, `status -w PTIT_CTF_2026`, `instance.py -l`) như lần freeze.

## 7. Rủi ro & biện pháp

| Rủi ro | Biện pháp |
|---|---|
| Facade bị bypass, code mới import thẳng sâu vào internals | Quy tắc R4 + review; facade mỏng dễ grep vi phạm (`grep -rn "from ctf_downloader.submitter import _"` ) |
| Refactor manager làm sai chuỗi message test SP2 assert | R2: diff literal trước/sau; nếu test vỡ coi là fail của phase |
| `interactive_menu.py` (477 dòng) gọi internals cũ | Giữ đủ re-export; smoke menu ở Phase 7 |
| Migration treo giữa chừng | Mỗi phase 1 commit độc lập; rollback = checkout tag/commit trước |

## 8. Bug được sửa kèm trong rebuild

1. `instance_manager.py:68` gọi `detect_and_init()` không tồn tại → thay bằng `platform_resolver`.
2. Hardcode `'infosecptit' in base_url` (`instance_manager.py:55`) → xoá theo registry.
3. Dead path đọc key `platform_url` (`submitter.py:84`) → bỏ khi hợp nhất vào repo.resolve_platform_url.
4. Lazy-import đảo chiều extractor→downloader (`link_extractor.py:121`) → cắt theo Phase 6.
5. Normalize URL 2 bộ suffix lệch nhau (`config.py` vs `detector._normalize`) → hợp nhất một hàm trong `utils/urlnorm.py`, cả hai gọi chung.
6. Container-predicate lệch kết quả giữa dashboard/instance list → predicate duy nhất trong repo (tập điều kiện rộng nhất).
7. Regex patch không neo (`instance_manager.py:227,240-246`) → anchor `^` + `count=1` theo §3.4.
8. Session chia sẻ đa luồng (`core.py`) → thread-local session copy cookie/header từ master sau authenticate, trong `session_factory.for_thread()`.

## 9. Điều kiện hoàn tất (Definition of Done)

- [ ] 7 phase merge đủ, mỗi phase 1 commit, 100 test xanh sau từng phase.
- [ ] Thêm platform giả lập trong test mới tốn đúng 1 file (test chứng minh bằng fixture).
- [ ] `grep` xác nhận: không còn `_resolve_url`/`scan_all_workspaces`/`get_auth_for_workspace` nhân bản ngoài services/storage.
- [ ] Smoke offline pass trên workspace PTIT_CTF_2026 (layout cũ lẫn mới).
- [ ] README cập nhật cây thư mục mới + hướng dẫn thêm platform.
