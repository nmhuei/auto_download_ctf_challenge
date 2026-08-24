# test_arch_phase1.py
import unittest
class TestModels(unittest.TestCase):
    def test_models_module_and_reexport(self):
        from ctf_downloader import models
        from ctf_downloader.platforms.base import Challenge as C1, CTFInfo as I1
        self.assertIs(models.Challenge, C1)
        self.assertIs(models.CTFInfo, I1)
        self.assertEqual(models.Verdict.__args__, ("correct","incorrect","unknown","ratelimited"))
