"""PERF guard — tối ưu writeup_assessor (early-gate + memo) phải giữ verdict.

Khóa 3 cam kết:
1. Fast-path similarity (_template_similarity) bit-exact với
   ``difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()`` trên cả
   input degenerate (autojunk), biên ngưỡng 0.95 và chuỗi rỗng.
2. Memo cùng-process: cùng input → dict CÙNG GIÁ TRỊ, object KHÁC (caller
   mutate không nhiễm cache).
3. Verdict/score không đổi khi đi lại nhiều lần (cold == warm).
"""
import difflib
import random
import unittest

from ctf_downloader.storage.constants import FLAG_PLACEHOLDER
from ctf_downloader.utils import writeup_assessor as wa
from ctf_downloader.utils.writeup_assessor import assess_writeup

TEMPLATE = """# Writeup — perf-guard-chall

- [ ] Solved

**Flag**: `FLAG{...}`

## Mô tả

Đề bài: tìm flag trong dịch vụ Pwn.

## Reconnaissance Strategy

## Exploitation Strategy

```
python3 ../solver/solve.py
```

## Bài học

- Status: `- [ ] Solved`
"""

FILLED = TEMPLATE.replace("FLAG{...}", "FLAG{perf_guard_flag_1234}").replace(
    "- [ ] Solved", "- [x] Solved").replace(
    "## Reconnaissance Strategy\n",
    "## Reconnaissance Strategy\n\nBuffer overflow trong hàm read — kiểm tra "
    "boundary của stack canary trước khi khai thác sâu hơn.\n").replace(
    "```\npython3 ../solver/solve.py\n```",
    "```\npython3 ../solver/solve.py\n# payload riêng: cyclic(120)\n```")


class TestSimilarityFastPathEquivalence(unittest.TestCase):
    def _ref_ratio(self, a, b):
        return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()

    def test_degenerate_and_boundary_pairs_match_difflib(self):
        rng = random.Random(20260825)
        cases = [
            ("", ""), ("", "abc"), ("abc", ""), ("   ", "\n\t"),
            ("a" * 300, "a" * 300), ("a" * 199, "a" * 199),
            ("ab" * 150, "ab" * 150), ("x" * 250 + "!", "x" * 250 + "!"),
            ("a" * 300, "a" * 299),
            (TEMPLATE, TEMPLATE), (TEMPLATE, FILLED), (FILLED, TEMPLATE),
        ]
        base = TEMPLATE
        for k in range(int(0.17 * len(base)), int(0.25 * len(base)) + 6, 2):
            cases.append((base, base + "q" * k))
        cur = base
        for _ in range(30):
            pos = rng.randrange(len(cur))
            cur = cur[:pos] + rng.choice("ab \n`") + cur[pos:]
            cases.append((cur, base))

        for a, b in cases:
            self.assertAlmostEqual(
                wa._template_similarity(a, b), self._ref_ratio(a, b), places=14,
                msg=f"fast-path lệch difflib tại cặp len=({len(a)},{len(b)})")

    def test_identical_long_text_is_exact_one(self):
        # Writeup thật chưa đụng: nội dung == template regenerate → 1.0 chính xác
        self.assertEqual(wa._template_similarity(TEMPLATE, TEMPLATE), 1.0)
        self.assertEqual(
            wa._template_similarity("  " + TEMPLATE + " \n", TEMPLATE.strip() + " "),
            1.0)


class TestMemoContract(unittest.TestCase):
    def setUp(self):
        wa._assess_memo.clear()

    def tearDown(self):
        wa._assess_memo.clear()

    def test_warm_result_equal_but_isolated(self):
        cold = assess_writeup(FILLED, flag_format=r"^FLAG\{.+\}$",
                              reference_template=TEMPLATE)
        warm = assess_writeup(FILLED, flag_format=r"^FLAG\{.+\}$",
                              reference_template=TEMPLATE)
        self.assertEqual(cold, warm)
        self.assertIsNot(cold["signals"], warm["signals"])
        self.assertIsNot(cold["missing"], warm["missing"])
        # caller mutate bản nhận về — cache không nhiễm
        warm["signals"]["has_real_flag"] = False
        warm["missing"].append("rác")
        again = assess_writeup(FILLED, flag_format=r"^FLAG\{.+\}$",
                               reference_template=TEMPLATE)
        self.assertTrue(again["signals"]["has_real_flag"])
        self.assertNotIn("rác", again["missing"])

    def test_verdict_stable_across_repeats(self):
        seen = set()
        for _ in range(3):
            res = assess_writeup(FILLED, flag_format=r"^FLAG\{.+\}$",
                                 reference_template=TEMPLATE)
            seen.add((res["status"], res["score"]))
        self.assertEqual(len(seen), 1)

    def test_skeleton_guard_still_fires(self):
        res = assess_writeup(TEMPLATE, reference_template=TEMPLATE)
        self.assertEqual(res["status"], "skeleton")
        self.assertEqual(res["score"], 0)
        self.assertGreaterEqual(res["signals"]["template_similarity"], 0.95)


if __name__ == "__main__":
    unittest.main()
