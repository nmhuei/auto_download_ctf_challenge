# Spec — Challenge Status Model đa chiều

> Ngày 24/08/2026 · Nguồn: 3 agent (solve attribution platform, status model design, writeup detection) · Phụ thuộc WorkspaceRepo + submit_service có sẵn

## 1. Mục tiêu

Mỗi challenge có **trạng thái đa chiều** phân biệt rõ: ai đã giải (tôi / team / người khác), flag đang ở giai đoạn nào, writeup hoàn thành tới đâu, container sống hay chết — thay vì 1 bool `solved_by_me`. Mọi UI bắt buộc có icon sinh động.

## 2. Schema — block `status` trong metadata.json per-challenge

```jsonc
{
  "solved_by_me": false,          // GIỮ — legacy mirror, luôn == (solve=="solved_by_me")
  "status": {
    "schema_version": 2,
    "solve": "unsolved",          // unsolved|working|solved_by_me|solved_by_team|solved_other
    "flag": { "value": null,      // flag thật tìm được (hoarded local-first)
              "state": "none" },  // none|found_unverified|hoarded|submitted_correct|submitted_wrong
    "writeup": "none",            // none|skeleton|draft|complete
    "writeup_auto": true,         // false = user set tay, heuristic không ghi đè
    "notes": "",                  // ghi chú 1 dòng hiện trên dashboard
    "labels": [],                 // tag do MÌNH đặt ("todo","hard","review") — khác tags đề bài
    "container": "none",          // none|running|stopped (mirror instance_info)
    "synced_at": null, "updated_at": null
  }
}
```

**Nguồn sự thật**: `metadata.json` per-challenge qua `WorkspaceRepo.read_status/update_status` (locked_update_json — lock granularity theo challenge, submit song song không chặn nhau). `challenges.json` chỉ là aggregate cache. `submit_history.json` là nguồn sự thật trục flag.

## 3. API mới trên WorkspaceRepo

```python
def read_status(self, meta_path) -> dict            # normalize + migrate-on-read từ field legacy
def update_status(self, meta_path, mutator) -> dict # read-mutate-write trong flock; stamp updated_at;
                                                    # mirror solved_by_me + toggle marker README
```

Migrate-on-read (workspace cũ chạy ngay, không cần convert): `solved_by_me=true` → `solve=solved_by_me`; marker `- [x] Solved` → tương tự; placeholder FLAG đã thay → `flag=found_unverified`; `instance_info.is_container` → `container=stopped`.

## 4. Solve attribution — sync từ server (`fetch_solve_attribution`)

Thêm vào BasePlatform: `fetch_solve_attribution(challenge_ids) -> dict[cid, SolveAttribution]`, default `{}`:

```python
@dataclass
class SolveAttribution:
    by_me: bool = False; by_team: bool = False
    solver_names: list = field(default_factory=list)
    first_blood: bool = False; solved_at: Optional[int] = None  # epoch-ms
```

| Platform | Cách lấy | Requests |
|---|---|---|
| GZCTF | `/api/game/{id}/scoreboard` → item team mình (match teamName hoặc chứa userName của mình) → mỗi solve: `by_me=(userName==profile.userName)`, membership chốt bằng `GET /api/team/{item.id}.members[].userName` | 1–2 |
| CTFd | users mode: `users/me/solves`; teams mode (detect bằng `GET /teams/me` 200): `teams/me/solves` — **mỗi dòng mang `user.id/name` của thành viên submit**, `by_me = row.user.id == me_id` | 2 |
| rCTF | `users/me.solves[]` có sẵn (by_team ≡ by_me — 1 account = 1 team); `challs/{id}/solves` public cho solver_names/first-blood | 0–1+N |

Edge cases đã verify: GZCTF scoreboard anonymous, 400 trước giờ mở → fallback `/details` (own-team only); CTFd `/me/solves` không bị freeze cắt; rCTF luôn `by_team==by_me`. Cache kết quả trong phiên; mọi exception → trả `{}` không raise.

**Luồng sync**: pull/watch tick gọi attribution → server báo solved mà local chưa → set `solve=solved_by_team/solved_other/by_me`, stamp `synced_at`. KHÔNG BAO GIỜ hạ trạng thái local cao hơn (nguyên tắc chỉ-nâng).

## 5. Writeup assessment (`utils/writeup_assessor.py` — MỚI)

`assess_writeup(md_text, flag_format, reference_template=None) -> {status, score, signals, missing}` — scoring 100đ:

| Nhóm | Tín hiệu | Điểm |
|---|---|---|
| Flag 35 | khớp `flag_format` giải +30 · generic-flag regex +20 · chỉ còn placeholder 0 · xoá placeholder chưa có flag mới +5 | 35 |
| Evidence 30 | code block KHÔNG phải boilerplate template +18 · command/output thật ($ , nc , hex/base64) +7 · screenshot cục bộ +5 | 30 |
| Prose 25 | mục Recon có văn thật >30 từ +12 (8-30 từ +6); Exploitation như vậy +13; dung lượng văn mới >500 ký tự bù tối đa đến 25 | 25 |
| Checkbox 10 | `- [x]` +10 | 10 |

**Guard skeleton**: sinh lại template qua `WorkspaceBuilder._generate_writeup_template()` → similarity ≥0.95 → SKELETON ngay (rẻ, chắc).
**COMPLETE**: score ≥70 VÀ có flag thật (F1/F2) VÀ (code block riêng HOẶC exploitation ≥13đ).
Trả `missing[]` tiếng Việt để render gợi ý ("Mục 'Reconnaissance' chưa có nội dung thực").
Heuristic chỉ áp khi `writeup_auto=true`.

## 6. Render — thiết kế 2 tầng

> **Cập nhật 2026-08-25 theo spec-audit:** triển khai thực tế tách 2 tầng render độc lập; bảng emoji gốc của spec này chỉ còn là tham chiếu cho tầng web.

| Tầng | Nguồn icon | Ghi chú |
|---|---|---|
| Terminal (dashboard/tree/status) | `ROW_GLYPHS` phosphor — `services/status_service.py`: solve `·` unsolved · `◆` working · `✔` solved; badge `✎` draft · `⛁` container · `⎘` file | by_team/by_other gộp chung `✔` với by_me (cùng style `solved`) |
| Web dashboard | `STATUS_ICONS` emoji riêng trong `storage/constants.py` (solve/flag/writeup/container + `CATEGORY_ICONS`) — giữ bộ emoji của bản thiết kế gốc | Tách biệt hoàn toàn với glyph terminal |

- Tree terminal **không có badge trục flag** (flag state chỉ hiển thị ở view chi tiết/web).
- Mapping signal→icon→message nằm trong 1 dict duy nhất (`storage/constants.py` mở rộng).

## 7. Luồng cập nhật tự động

| Sự kiện | Trục đổi | Hook |
|---|---|---|
| Submit verdict correct | flag→submitted_correct, solve nâng lên solved_by_me | submit_service sau verdict |
| Verdict incorrect | flag→submitted_wrong (+value để biết flag nào chết) | submit_service |
| ratelimited/unknown | KHÔNG đổi | submit_service |
| Lệnh `ctf hoard <chal> <FLAG>` (Cập nhật 2026-08-25 theo spec-audit: `hoard` mới là lệnh stash local-first — `flag` chỉ là alias của `submit`) | flag.value=x, state→hoarded | CLI `ctf hoard` |
| Sync/pull attribution từ server | solve→by_team/by_other/by_me | pull_service + watch tick |
| User tick marker README | solve→solved_by_me (chỉ nâng) | repo.write_solved_state |
| Start/stop container | container running/stopped | instance_service |

## 8. Testing

- Unit: normalize/migrate status; update_status mirror + lock (multi-process); assessor với 4 mẫu (template nguyên vẹn→SKELETON, điền đủ→COMPLETE, thiếu flag→DRAFT, viết tay không template); attribution parser từng platform (mock JSON shape thật đã verify).
- Integration: submit correct → status chain đúng; dashboard render icon snapshot test.
- Gate: full suite cũ xanh không sửa assertion; test mới `test_status_model.py`.

## 9. Known deviations & follow-ups

> **Cập nhật 2026-08-25 theo spec-audit** — lệch thực tế so với spec, kèm trạng thái xử lý:

| Mục | Trạng thái | Ghi chú |
|---|---|---|
| Wiring attribution vào watch tick (luồng sync §4: pull/watch tick gọi `fetch_solve_attribution`) chưa hoàn chỉnh | [IN-PROGRESS] | Đang được fixer xử lý |
| `solver_names` render thẳng ra output chưa escape markup | [DEFERRED-L] | Backlog mức thấp; escape trước khi render tên solver/team |
