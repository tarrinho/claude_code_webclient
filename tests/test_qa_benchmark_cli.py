# tests/test_qa_benchmark_cli.py
"""QA: the estimate, which is the part of the CLI that can lie.

Spec 10. A sweep runs only in idle hours, so a projection in hours is not a
projection in elapsed time. Every case below asserts that the output SAYS what
it is: an estimate from one cell must admit it, and an estimate with no nightly
history must say it excludes idle time rather than implying the sweep finishes
tonight.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "wc_benchmark", REPO_ROOT / "bin" / "wc-benchmark.py")
wc_benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wc_benchmark)


class EstimateTests(unittest.TestCase):
    def test_no_history_gives_no_estimate(self):
        out = wc_benchmark.format_estimate([], [], cells_total=70)
        self.assertIn("no prior sweep", out)

    def test_cell_history_without_nights_states_the_exclusion(self):
        out = wc_benchmark.format_estimate([360.0] * 10, [], cells_total=70)
        self.assertIn("7.0h", out)
        self.assertIn("excludes", out)
        self.assertNotIn("nights at", out)

    def test_cell_and_night_history_reports_nights(self):
        out = wc_benchmark.format_estimate([360.0] * 10, [2.4] * 6,
                                           cells_total=70)
        self.assertIn("7.0h", out)
        self.assertIn("3 nights", out)

    def test_an_estimate_from_one_cell_says_so(self):
        out = wc_benchmark.format_estimate([360.0], [2.4] * 6, cells_total=70)
        self.assertIn("from 1 cell", out)


class ModelListTests(unittest.TestCase):
    def test_terra_is_in_default_models(self):
        """A sweep that skips a live routing rung is worse than no sweep."""
        import importlib.util as util
        s = util.spec_from_file_location(
            "wc_bench", REPO_ROOT / "bin" / "wc-bench.py")
        mod = util.module_from_spec(s)
        s.loader.exec_module(mod)
        self.assertIn("azure_ai/gpt-5.6-terra", mod.DEFAULT_MODELS)
