"""RACE guard — ``_assess_memo`` phải an toàn khi gọi từ nhiều thread (review-5).

Caller hiện tại single-thread nên khoá là hardening, nhưng hợp đồng memo
(pure-function cache, luôn trả copy, bounded LRU 4096) phải giữ vững cả khi:

1. 2 thread gọi đồng thời 1000 lần trên tập key chung + riêng → mọi kết quả
   đúng bằng ground-truth uncached, không exception, không mất entry oan.
2. Áp lực eviction (> ``_ASSESS_MEMO_MAX`` key duy nhất) diễn ra cùng lúc
   với các thread đang get-hit/promote key nóng → không KeyError/corrupt,
   memo chốt đúng biên tối đa.
"""
import threading
import unittest

from ctf_downloader.utils import writeup_assessor as wa
from ctf_downloader.utils.writeup_assessor import assess_writeup

_FF = r"^FLAG\{.+\}$"


def _mk_md(i: int) -> str:
    """Writeup 'complete' biến thể theo i — mỗi i một key blake2b riêng."""
    return (
        f"# Writeup — race-{i}\n\n- [x] Solved\n\n**Flag**: `FLAG{{race_flag_{i}}}`\n"
        "\n## Reconnaissance Strategy\n\n"
        f"Nội dung recon thread-safe số {i}: quét binary thấy hàm read không "
        "kiểm tra boundary, stack canary bị tắt nên buffer overflow khai thác "
        "được ngay bằng payload cyclic dài hơn vùng đệm.\n"
        "\n## Exploitation Strategy\n\n"
        f"Ghi đè return address tại offset {i} rồi nhảy vào shellcode đã đặt "
        "trên stack, lấy flag trực tiếp từ stdin của dịch vụ remote.\n"
        "\n```\npython3 ../solver/solve.py\n# payload riêng cho chall\n```"
    )


class _RaceHarness(unittest.TestCase):
    """Chạy các job trên nhiều thread giải phóng đồng loạt qua barrier,
    gom exception/lech-ket-qua về main thread để assert rõ ràng."""

    def run_race(self, jobs) -> list:
        errors: list = []
        lock = threading.Lock()
        barrier = threading.Barrier(len(jobs))
        threads = []

        def wrap(idx: int, fn) -> None:
            try:
                barrier.wait(timeout=30)
                fn(idx)
            except Exception as exc:  # noqa: BLE001 — lỗi race bắn về main
                with lock:
                    errors.append(f"job={idx}: {exc!r}")

        for idx, fn in enumerate(jobs):
            t = threading.Thread(target=wrap, args=(idx, fn))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=180)
        alive = [t for t in threads if t.is_alive()]
        self.assertEqual(alive, [], "có worker thread chưa join sau 180s")
        return errors


class TestMemoThreadSafety(_RaceHarness):
    def setUp(self):
        wa._assess_memo.clear()

    def tearDown(self):
        wa._assess_memo.clear()

    def test_two_threads_1000_calls_same_and_distinct_keys(self):
        n_calls, n_keys = 1000, 64
        texts = [_mk_md(i) for i in range(n_keys)]
        # Ground truth QUA THÂN UNCACHED — không đụng memo trước vòng race.
        expected = [wa._assess_writeup_uncached(t, flag_format=_FF)
                    for t in texts]

        def job(tid: int) -> None:
            for k in range(n_calls):
                # (k*11+tid*37) % n_keys: xen kẽ key CHUNG giữa 2 thread và
                # key RIÊNG — ép cả đường miss-store lẫn hit-promote đua nhau.
                i = (k * 11 + tid * 37) % n_keys
                got = assess_writeup(texts[i], flag_format=_FF)
                if got != expected[i]:
                    raise AssertionError(
                        f"sai kết quả k={k} key={i}: {got} != {expected[i]}")

        errors = self.run_race([job, job])
        self.assertEqual(errors, [])
        # Không mất entry oan: đủ n_keys entry, mỗi key duy nhất một bản ghi.
        self.assertEqual(len(wa._assess_memo), n_keys)
        for t in texts:
            self.assertIn(wa._memo_digest(t, _FF, None), wa._assess_memo)

    def test_concurrent_eviction_with_hot_key_promote(self):
        total_keys = wa._ASSESS_MEMO_MAX + 400   # 4496 > 4096 → evict liên tục
        texts = [_mk_md(i) for i in range(total_keys)]
        hot_text = texts[0]
        hot_expected = wa._assess_writeup_uncached(hot_text, flag_format=_FF)
        stop = threading.Event()
        errors: list = []
        lock = threading.Lock()

        def _err(msg: str) -> None:
            with lock:
                errors.append(msg)

        def sweeper(tid: int) -> None:
            # Chèn key duy nhất xen kẽ giữa 2 sweeper → popitem evict suốt
            # quãng đường trong lúc reader đang promote key nóng.
            try:
                for i in range(tid, total_keys, 2):
                    got = assess_writeup(texts[i], flag_format=_FF)
                    want = wa._assess_writeup_uncached(texts[i], flag_format=_FF)
                    if got != want:
                        raise AssertionError(
                            f"sweeper sai tại key {i}: {got} != {want}")
            except Exception as exc:  # noqa: BLE001
                _err(f"sweeper={tid}: {exc!r}")

        def hot_reader(_: int) -> None:
            # Hit-lại key nóng: move_to_end đua với evict của sweeper.
            # Tự kết thúc theo stop HOẶC trần số vòng để không treo join.
            n = 0
            try:
                while not stop.is_set() and n < 200_000:
                    if assess_writeup(hot_text, flag_format=_FF) != hot_expected:
                        raise AssertionError("hot key trả kết quả lệch")
                    n += 1
            except Exception as exc:  # noqa: BLE001
                _err(f"reader: {exc!r}")

        sweeper_threads = [threading.Thread(target=sweeper, args=(t,))
                           for t in range(2)]
        reader_threads = [threading.Thread(target=hot_reader, args=(t,),
                                           daemon=True)
                          for t in range(2)]
        # Reader vào vùng đua gần như ngay lập tức (sweeper mất vài giây chèn
        # 4496 key) nên không cần barrier thêm.
        for t in sweeper_threads + reader_threads:
            t.start()
        for t in sweeper_threads:
            t.join(timeout=300)
        self.assertEqual([t for t in sweeper_threads if t.is_alive()], [])
        stop.set()
        for t in reader_threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        # Memo chốt đúng biên tối đa sau khi chèn total_keys key duy nhất.
        self.assertEqual(len(wa._assess_memo), wa._ASSESS_MEMO_MAX)


if __name__ == "__main__":
    unittest.main()
