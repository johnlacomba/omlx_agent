"""Regression tests for model default estimation."""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import omlx_agent  # noqa: E402


class EstimateModelDefaultsTests(unittest.TestCase):
    def test_explicit_footprint_overrides_quantization_heuristic(self):
        conc, mem = omlx_agent._estimate_model_defaults("custom-manager-4bit-35GB", "manager")
        self.assertEqual(conc, 1)
        self.assertEqual(mem, "35GB")

    def test_unknown_model_without_explicit_footprint_keeps_legacy_defaults(self):
        conc, mem = omlx_agent._estimate_model_defaults("custom-manager-model", "manager")
        self.assertEqual(conc, 2)
        self.assertEqual(mem, "26GB")


if __name__ == "__main__":
    unittest.main()