"""QA: bench/gate_accuracy.py's labels, arithmetic, and error handling.

Everything here is checked without a model call. `evaluate_sample` is the
only function that makes one (via bench.transports.send -> a real `claude`
subprocess), so it is exercised only through a monkeypatched `_call` -- the
model layer is substituted, not invoked.

Three things this file exists to catch, named directly:

1. A mutant's `is_correct`/`is_vulnerable` label must match what actually
   happens when its code runs (for correctness mutants) or must be provable
   by construction (for security mutants) -- a label that is merely asserted
   in a docstring is worth nothing.
2. The confusion-matrix arithmetic (spec §4.5's false-reject rate, not just
   §2.6's single accuracy figure) must be right.
3. An errored gate call must be structurally impossible to mistake for a
   verdict -- the terra bug this module's docstring records: 14 errored
   calls read as a plausible 0.77s median because failure and judgement were
   the same shape in the output.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench import gate_accuracy as ga
from bench import tasks as tasks_mod
from bench import verify


class MutantLabelsMatchTheirDefect(unittest.TestCase):
    """Every correctness mutant must actually fail the task's own verifier,
    and every reference solution must actually pass it. The label
    (`is_correct`) is not decoration -- it is checked against ground truth
    computed the same way pipeline_ab.py computes it: by running the code.
    """

    def test_every_reference_solves_its_task(self):
        for task_id, code in ga.REFERENCE_SOLUTIONS.items():
            task = tasks_mod.BY_ID[task_id]
            verdict = task.verifier(f"```python\n{code}\n```")
            self.assertTrue(
                verdict.solved,
                f"{task_id}: reference solution does not solve its own task "
                f"({verdict.detail})",
            )

    def test_every_correctness_mutant_fails_its_task(self):
        for sample in ga._CORRECTNESS_MUTANTS:
            task = tasks_mod.BY_ID[sample.task_id]
            verdict = task.verifier(f"```python\n{sample.code}\n```")
            self.assertFalse(
                verdict.solved,
                f"{sample.id}: labelled is_correct=False but the verifier "
                f"says it solved the task -- the mutant does not carry the "
                f"defect it claims ({sample.defect})",
            )
            self.assertFalse(sample.is_correct)
            self.assertFalse(
                sample.is_vulnerable,
                f"{sample.id}: a correctness mutant must stay security-neutral "
                "(the 'buggy but safe' sample), or it is not isolating what "
                "the reviewer gate is being tested on",
            )

    def test_correctness_mutant_ids_and_tasks_are_declared(self):
        # Every mutant must name a real task -- a typo'd task_id would
        # silently build a sample nothing ever validates against.
        for sample in ga._CORRECTNESS_MUTANTS:
            self.assertIn(sample.task_id, tasks_mod.BY_ID)
            self.assertIn(sample.task_id, ga.REFERENCE_SOLUTIONS)


class SecurityMutantsAreCorrectByConstruction(unittest.TestCase):
    """A security mutant's `is_correct=True` is not asserted -- it is proved
    by removing the one injected line and recovering the reference solution
    exactly. Running the mutant itself is deliberately avoided: it contains
    the vulnerability it is testing for (os.system, a written file, printed
    text), and this file must not execute that.
    """

    def test_stripping_the_injection_recovers_the_reference_exactly(self):
        for task_id, mutation, _defect, injected_line in ga._SECURITY_INJECTIONS:
            sample = next(
                s for s in ga._SECURITY_MUTANTS
                if s.task_id == task_id and s.mutation == mutation
            )
            recovered = ga.strip_injected_line(sample.code, injected_line)
            self.assertEqual(
                recovered,
                ga.REFERENCE_SOLUTIONS[task_id],
                f"{sample.id}: removing the injected line did not recover the "
                "reference byte-for-byte -- the mutation changed more than "
                "the one vulnerability it claims to add",
            )

    def test_each_security_mutant_carries_its_named_vulnerability_pattern(self):
        # SECURITY NOTE: "os.system(" below is a substring match against a
        # sample's source text, not a call -- this test never executes any
        # sample's code, only inspects it as a string.
        patterns = {
            "unsanitised_os_system": "os.system(",
            "path_built_by_concatenation": '"/tmp/chunks-" + str(size)',
            "secret_written_to_output": "sk-live-",
        }
        for sample in ga._SECURITY_MUTANTS:
            pattern = patterns[sample.mutation]
            self.assertIn(
                pattern, sample.code,
                f"{sample.id}: does not contain the vulnerability pattern its "
                f"own mutation name claims ({sample.mutation})",
            )
            self.assertTrue(sample.is_vulnerable)
            self.assertTrue(
                sample.is_correct,
                f"{sample.id}: a security mutant must stay correct (the "
                "'correct but scary' sample), or it is not isolating what "
                "the security gate is being tested on",
            )

    def test_security_mutant_code_still_parses(self):
        # A construction bug (bad indentation on the injected line, etc.)
        # would be invisible until a real gate call choked on it. Catch it
        # here instead, with no model call.
        import ast
        for sample in ga._SECURITY_MUTANTS:
            ast.parse(sample.code)


class FixtureRegressionTest(unittest.TestCase):
    """The LRU __repr__ fixture: spec §4.5's documented case."""

    def test_lru_repr_fixture_is_labelled_safe_and_correct(self):
        self.assertEqual(ga.LRU_REPR_FIXTURE.task_id, "coding-algo")
        self.assertTrue(ga.LRU_REPR_FIXTURE.is_correct)
        self.assertFalse(ga.LRU_REPR_FIXTURE.is_vulnerable)

    def test_lru_repr_fixture_solves_the_task(self):
        task = tasks_mod.BY_ID["coding-algo"]
        verdict = task.verifier(f"```python\n{ga.LRU_REPR_FIXTURE.code}\n```")
        self.assertTrue(verdict.solved)

    def test_lru_repr_fixture_is_present_in_build_samples(self):
        samples = ga.build_samples()
        self.assertIn(ga.LRU_REPR_FIXTURE.id, {s.id for s in samples})


class BuildSamplesIsWellFormed(unittest.TestCase):
    def test_every_sample_has_a_unique_id(self):
        samples = ga.build_samples()
        ids = [s.id for s in samples]
        self.assertEqual(len(ids), len(set(ids)), "duplicate sample id")

    def test_every_sample_targets_a_real_task(self):
        samples = ga.build_samples()
        for s in samples:
            self.assertIn(s.task_id, tasks_mod.BY_ID)

    def test_every_coding_task_with_an_exec_verifier_has_a_reference(self):
        # Scoped to task_type == "coding" deliberately: spec §4 runs the
        # reviewer/security gates on a "coding leaf", not on a multi-turn
        # leaf, even though four multi-turn-* tasks happen to share the same
        # exec verifier (verify.run_checks) under the hood. Widening this to
        # every exec-verified task regardless of type would pull in
        # multi-turn-rename etc., which this harness does not claim to cover.
        #
        # If tasks.py grows a new exec-verified *coding* task, this fails
        # loudly instead of silently under-covering it. Detected by asking
        # the verifier to score empty input: an exec verifier's `kind` is
        # "exec" even on a trivial call; a claim verifier's is "claim".
        exec_coding_task_ids = set()
        for task in tasks_mod.TASKS:
            if task.task_type != "coding":
                continue
            verdict = task.verifier("")
            if verdict.kind == "exec":
                exec_coding_task_ids.add(task.id)
        missing = exec_coding_task_ids - set(ga.REFERENCE_SOLUTIONS)
        self.assertEqual(
            missing, set(),
            f"exec-verified coding task(s) with no gate_accuracy reference: {missing}",
        )


class ConfusionMatrixArithmetic(unittest.TestCase):
    """Built from hand-constructed records, so the expected numbers are
    known independently of the code under test.
    """

    @staticmethod
    def _record(ground_truth_pass: bool, verdict_pass, gate: str = "reviewer") -> ga.GateVerdictRecord:
        call = ga.Call(
            stage=gate, model_requested="test-model", model_served="test-model",
            total_s=1.0, input_tokens=10, output_tokens=10,
            cache_read_tokens=0, cache_write_tokens=0,
            reported_cost_usd=None, cost_basis=None,
            error=None if verdict_pass is not None else "simulated transport error",
        )
        return ga.GateVerdictRecord(
            gate=gate, sample_id="x", task_id="floor-add", category="reference",
            mutation=None, defect=None,
            ground_truth_pass=ground_truth_pass, verdict_pass=verdict_pass,
            reason="", call=call,
        )

    def test_all_four_cells_and_the_derived_rates(self):
        records = [
            self._record(True, True),    # true accept
            self._record(True, True),    # true accept
            self._record(True, False),   # false reject
            self._record(False, False),  # true reject
            self._record(False, False),  # true reject
            self._record(False, False),  # true reject
            self._record(False, True),   # false accept
        ]
        cm = ga.compute_confusion(records, "reviewer", "test-model")
        self.assertEqual(cm.true_accept, 2)
        self.assertEqual(cm.false_reject, 1)
        self.assertEqual(cm.true_reject, 3)
        self.assertEqual(cm.false_accept, 1)
        self.assertEqual(cm.excluded_errors, 0)
        self.assertEqual(cm.total, 7)
        self.assertAlmostEqual(cm.accuracy, 5 / 7, places=4)
        # precision = TA / (TA + FA) = 2 / 3
        self.assertAlmostEqual(cm.precision, 2 / 3, places=4)
        # recall = TA / (TA + FR) = 2 / 3
        self.assertAlmostEqual(cm.recall, 2 / 3, places=4)
        # false_reject_rate = FR / (TA + FR) = 1 / 3
        self.assertAlmostEqual(cm.false_reject_rate, 1 / 3, places=4)

    def test_no_data_gives_none_not_zero_or_a_crash(self):
        cm = ga.compute_confusion([], "reviewer", "test-model")
        self.assertEqual(cm.total, 0)
        self.assertIsNone(cm.accuracy)
        self.assertIsNone(cm.precision)
        self.assertIsNone(cm.recall)
        self.assertIsNone(cm.false_reject_rate)

    def test_only_false_rejects_gives_zero_recall_not_none(self):
        records = [self._record(True, False), self._record(True, False)]
        cm = ga.compute_confusion(records, "reviewer", "test-model")
        self.assertEqual(cm.recall, 0.0)
        self.assertEqual(cm.false_reject_rate, 1.0)

    def test_gate_filter_ignores_records_from_the_other_gate(self):
        records = [
            self._record(True, True, gate="reviewer"),
            self._record(True, False, gate="security"),
        ]
        cm = ga.compute_confusion(records, "reviewer", "test-model")
        self.assertEqual(cm.total, 1)
        self.assertEqual(cm.true_accept, 1)


class ErroredCallsAreExcludedNotCounted(unittest.TestCase):
    """The terra regression: an error must not be countable as any of the
    four verdict cells, and must not silently vanish either -- it has its
    own counter.
    """

    def test_a_record_with_verdict_pass_none_is_excluded_from_every_cell(self):
        good = ConfusionMatrixArithmetic._record(True, True)
        errored = ConfusionMatrixArithmetic._record(True, None)  # ground truth says PASS, call errored
        cm = ga.compute_confusion([good, errored], "reviewer", "test-model")
        self.assertEqual(cm.true_accept, 1)
        self.assertEqual(cm.false_reject, 0)
        self.assertEqual(cm.true_reject, 0)
        self.assertEqual(cm.false_accept, 0)
        self.assertEqual(cm.excluded_errors, 1)
        self.assertEqual(cm.total, 1)  # the errored call must not inflate the denominator

    def test_a_real_fail_is_still_counted_distinctly_from_an_error(self):
        # A ground-truth-pass sample that the gate genuinely rejected
        # (verdict_pass=False) is a false_reject, not an excluded error --
        # only verdict_pass=None (no verdict at all) is excluded.
        real_fail = ConfusionMatrixArithmetic._record(True, False)
        cm = ga.compute_confusion([real_fail], "reviewer", "test-model")
        self.assertEqual(cm.false_reject, 1)
        self.assertEqual(cm.excluded_errors, 0)

    def test_all_errors_gives_an_empty_matrix_not_a_fabricated_score(self):
        # This is the terra shape exactly: every call errored. The matrix
        # must report zero judgements, not compute a number out of nothing.
        records = [ConfusionMatrixArithmetic._record(True, None) for _ in range(14)]
        cm = ga.compute_confusion(records, "reviewer", "test-model")
        self.assertEqual(cm.total, 0)
        self.assertEqual(cm.excluded_errors, 14)
        self.assertIsNone(cm.accuracy)
        self.assertIsNone(cm.false_reject_rate)

    def test_evaluate_sample_maps_a_transport_error_to_verdict_pass_none(self):
        # No real model call: bench.gate_accuracy._call is monkeypatched to
        # simulate exactly the terra failure (a transport error) instead of
        # calling bench.transports.send.
        sample = ga.LRU_REPR_FIXTURE
        errored_call = ga.Call(
            stage="security", model_requested="azure_ai/gpt-5.6-terra",
            model_served=None, total_s=0.0, input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0,
            reported_cost_usd=None, cost_basis=None,
            error="cli exited 1: API Error: 429 No deployments available",
        )
        with mock.patch.object(ga, "_call", return_value=("", errored_call)):
            rec = ga.evaluate_sample("security", sample, "azure_ai/gpt-5.6-terra", tasks_mod.BY_ID)
        self.assertIsNone(rec.verdict_pass)
        self.assertIn("errored", rec.reason)
        # Ground truth is still recorded -- an error does not erase what the
        # sample's label was, only what the gate said about it.
        self.assertTrue(rec.ground_truth_pass)

    def test_evaluate_sample_records_a_real_verdict_when_the_call_succeeds(self):
        sample = ga._CORRECTNESS_MUTANTS[0]
        ok_call = ga.Call(
            stage="reviewer", model_requested="azure_ai/gpt-5.6-luna",
            model_served="azure_ai/gpt-5.6-luna", total_s=1.2,
            input_tokens=200, output_tokens=5,
            cache_read_tokens=0, cache_write_tokens=0,
            reported_cost_usd=0.01, cost_basis="list", error=None,
        )
        with mock.patch.object(ga, "_call", return_value=("FAIL: wrong result", ok_call)):
            rec = ga.evaluate_sample("reviewer", sample, "azure_ai/gpt-5.6-luna", tasks_mod.BY_ID)
        self.assertEqual(rec.verdict_pass, False)
        self.assertFalse(rec.ground_truth_pass)  # a correctness mutant: gate should have failed it
        # A correct rejection of a genuinely bad sample is a true reject.
        cm = ga.compute_confusion([rec], "reviewer", "azure_ai/gpt-5.6-luna")
        self.assertEqual(cm.true_reject, 1)


class OutputRecordsTheModelActuallyUsed(unittest.TestCase):
    """No model call: _summarise is a pure function over records that were
    themselves built without a network call.
    """

    def test_summarise_embeds_the_gate_model_argument_not_the_default(self):
        rec = ConfusionMatrixArithmetic._record(True, True)
        custom_model = "claude-opus-5"  # deliberately not GATE_MODEL's default
        self.assertNotEqual(custom_model, ga.GATE_MODEL)
        out = ga._summarise([rec], custom_model, ["reviewer"])
        self.assertEqual(out["config"]["gate_model"], custom_model)

    def test_summarise_reports_unpriced_calls_as_none_not_zero(self):
        # test-model has no entry in bench_rates.json, so its cost must come
        # back as an honest "unknown", never a fabricated 0.0 that would make
        # an unpriced gate look free.
        rec = ConfusionMatrixArithmetic._record(True, True)
        out = ga._summarise([rec], "test-model", ["reviewer"])
        self.assertEqual(out["unpriced_calls"], 1)

    def test_out_path_refuses_to_overwrite_an_existing_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
            with self.assertRaises(SystemExit):
                ga._out_path(tmp.name)

    def test_out_path_is_timestamped_when_not_given_explicitly(self):
        path = ga._out_path(None)
        self.assertTrue(path.name.startswith("gate_accuracy_"))
        self.assertFalse(path.exists())


class VerifierUsedByThisModuleIsExecKind(unittest.TestCase):
    """Sanity check that every reference/mutant is scored by execution, not
    by a prose claim check -- the ground truth this module asserts is only
    meaningful for the `exec` kind of verifier.
    """

    def test_every_covered_task_uses_the_exec_verifier(self):
        for task_id in ga.REFERENCE_SOLUTIONS:
            task = tasks_mod.BY_ID[task_id]
            verdict = task.verifier("")
            self.assertEqual(verdict.kind, "exec")


if __name__ == "__main__":
    unittest.main()
