"""QA: the benchmark harness must not be the thing being measured.

`bin/model_benchmark.py` ranked a working model last at 55.4% with "Code Quality
0". Three harness limits produced that, and none of them was visible in the
output: a token cap calibrated on non-reasoning models, an extractor that
replaced truncation with a placeholder, and a read timeout no slow model could
clear. The replacement in `bench/` is only worth having if its own failures are
distinguishable from a model's, so that is what this file asserts.

The tests that matter most are the discriminating ones: fixtures below are real
model output from the 2026-09-02 runs, and the verifiers have to score them the
way a careful reader did -- including finding a defect the careful reader
missed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench import cost, tasks, transports, verify

# --- real model output, kept verbatim ---------------------------------------

#: Qwen3.6's answer, 2026-09-02 17:05. Correct on the reported bug.
QWEN_BUG_FIX = '''```python
def last_n_unique(lst: list, n: int) -> list:
    """Return the last n unique elements, preserving order."""
    seen = []
    for x in reversed(lst):
        if x not in seen:
            seen.append(x)
    return seen[::-1][-n:]
```'''

#: The original buggy function from the prompt. Must not pass.
ORIGINAL_BUGGY = '''
def last_n_unique(lst, n):
    seen = []
    for x in reversed(lst):
        if x not in seen:
            seen.append(x)
    return seen[:n]
'''

#: A fix with no docstring and no annotations: correct, but the prompt asked
#: for both, so it must score partial rather than full.
BARE_FIX = '''
def last_n_unique(lst, n):
    seen = []
    for x in reversed(lst):
        if x not in seen:
            seen.append(x)
    out = seen[::-1]
    return out[-n:] if n else []
'''

#: Qwen3.6's LRU, trimmed to the parts under test. Correct and O(1), but
#: `put` compares len(cache) == capacity, so capacity=0 pops the sentinel and
#: raises KeyError.
QWEN_LRU = '''```python
class LRUCache:
    class _Node:
        def __init__(self, key=0, value=0):
            self.key = key; self.value = value
            self.prev = None; self.next = None

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.cache = {}
        self.head = self._Node(); self.tail = self._Node()
        self.head.next = self.tail; self.tail.prev = self.head

    def _add(self, node):
        self.head.next.prev = node; node.next = self.head.next
        self.head.next = node; node.prev = self.head

    def _remove(self, node):
        node.prev.next = node.next; node.next.prev = node.prev

    def get(self, key: int) -> int:
        if key not in self.cache:
            return -1
        node = self.cache[key]
        self._remove(node); self._add(node)
        return node.value

    def put(self, key: int, value: int) -> None:
        if key in self.cache:
            node = self.cache[key]; node.value = value
            self._remove(node); self._add(node)
        else:
            if len(self.cache) == self.capacity:
                lru = self.tail.prev
                self._remove(lru)
                del self.cache[lru.key]
            node = self._Node(key, value)
            self._add(node); self.cache[key] = node

    def __repr__(self):
        return f"LRUCache({self.capacity})"
```'''

#: The same, with the zero-capacity case handled.
FIXED_LRU = QWEN_LRU.replace(
    "if len(self.cache) == self.capacity:",
    "if self.capacity <= 0:\n                return\n"
    "            if len(self.cache) >= self.capacity:")


class ExtractionTests(unittest.TestCase):
    """Getting the code out must not itself be a source of zeros."""

    def test_a_fenced_block_is_extracted(self):
        code = verify.extract_code(QWEN_BUG_FIX)
        self.assertIn("def last_n_unique", code)
        self.assertNotIn("```", code)

    def test_unfenced_python_is_accepted(self):
        """"Return valid Python code only" is one of the prompts.

        A model that complies literally emits no fence, and rejecting that
        would score instruction-following as a syntax error.
        """
        self.assertIn("def last_n_unique", verify.extract_code(ORIGINAL_BUGGY))

    def test_prose_yields_nothing_rather_than_garbage(self):
        self.assertEqual(verify.extract_code("I would use a dict here."), "")

    def test_the_longest_block_wins(self):
        """Models often quote the buggy original before giving the fix."""
        response = "```python\nx = 1\n```\nand the fix:\n" + QWEN_BUG_FIX
        self.assertIn("def last_n_unique", verify.extract_code(response))

    def test_an_empty_response_is_empty_not_an_error(self):
        self.assertEqual(verify.extract_code(""), "")
        self.assertEqual(verify.extract_code(None), "")


class ExecutionTests(unittest.TestCase):
    """Running the code is the point; these pin how it is scored."""

    def test_no_code_is_reported_as_no_code_not_as_a_wrong_answer(self):
        """The original defect, in miniature.

        An empty answer and a wrong answer are different findings. The old
        harness recorded both as a score of zero, which is how a truncated
        response became "produces zero output text".
        """
        verdict = verify.run_checks("", "assert True")
        self.assertTrue(verdict.no_code)
        self.assertEqual(verdict.score, 0.0)
        self.assertIn("no extractable code", verdict.detail[0])

    def test_a_partial_pass_scores_partially(self):
        verdict = verify.run_checks(
            "x = 1", "assert x == 1\nassert x == 2\nassert x == 1")
        self.assertEqual(verdict.passed, 2)
        self.assertEqual(verdict.total, 3)
        self.assertEqual(verdict.score, 66.7)

    def test_one_failing_check_does_not_hide_the_rest(self):
        """Each statement runs independently, so a class that gets eviction
        right and an edge case wrong scores partial rather than zero."""
        verdict = verify.run_checks("x = 1", "assert x == 2\nassert x == 1")
        self.assertEqual(verdict.passed, 1)
        self.assertEqual(len(verdict.detail), 1)

    def test_code_that_will_not_import_says_so(self):
        verdict = verify.run_checks("this is not python", "assert True")
        self.assertEqual(verdict.passed, 0)
        self.assertTrue(any("failed to import" in d for d in verdict.detail))

    def test_an_infinite_loop_fails_instead_of_hanging(self):
        """Model code that does not terminate is a real outcome."""
        verdict = verify.run_checks("while True:\n    pass\n", "assert True")
        self.assertEqual(verdict.passed, 0)
        self.assertTrue(any("timed out" in d for d in verdict.detail))

    def test_the_subprocess_does_not_inherit_this_environment(self):
        """Model-written code has no business reading our variables."""
        verdict = verify.run_checks(
            "import os\nLEAKED = os.environ.get('WC_BENCH_SECRET_PROBE')",
            "assert LEAKED is None",
        )
        self.assertEqual(verdict.passed, 1, "the parent environment leaked in")


class VerifierDiscriminationTests(unittest.TestCase):
    """The verifiers must agree with a careful reader -- and go further."""

    def test_the_original_buggy_function_fails(self):
        """Guards the guard: if this passed, every score would be meaningless."""
        verdict = tasks.BY_ID["coding-bug-fix"].verifier(ORIGINAL_BUGGY)
        self.assertLess(verdict.score, 60.0)

    def test_qwens_bug_fix_is_scored_and_the_n_equals_zero_case_is_found(self):
        """The defect a hand-review missed, which is the argument for this file.

        Qwen3.6's fix returns `seen[::-1][-n:]`. That is correct for every n it
        was checked against by hand -- and wrong for n=0, because `x[-0:]` is
        `x[0:]`, so asking for zero elements returns all of them. It was scored
        100 in the comparison document on 2026-09-02 by someone (me) tracing it
        against `[1,2,3,2,1]` and `[1,2,3]` and stopping there.

        A reader chooses which cases to try. A test list does not.
        """
        verdict = tasks.BY_ID["coding-bug-fix"].verifier(QWEN_BUG_FIX)
        self.assertTrue(verdict.solved, "the fix solves the stated bug")
        self.assertLess(verdict.score, 100.0,
                        "n=0 returns the whole list; a perfect score would "
                        "mean the check list is not being run")
        self.assertEqual(verdict.edge_passed, 0, "the n=0 check must fail")
        self.assertTrue(any("[edge]" in d for d in verdict.detail),
                        f"expected a labelled edge failure; got {verdict.detail}")

    def test_a_fix_without_the_requested_docstring_loses_those_checks(self):
        verdict = tasks.BY_ID["coding-bug-fix"].verifier(BARE_FIX)
        self.assertTrue(
            any("docstring" in d or "type hints" in d for d in verdict.detail),
            f"the prompt asked for both; detail was {verdict.detail}")

    def test_the_lru_capacity_zero_defect_is_reported_but_does_not_fail_it(self):
        """Found mechanically, and in the edge tier where it belongs.

        Three models shipped this and it was scored a warning twice and 0 once,
        depending on who traced it. It is now the same check every time -- and
        it no longer decides the verdict, because a correct O(1) cache that
        raises on a capacity the prompt never mentioned is not the same outcome
        as a response containing no code.
        """
        verdict = tasks.BY_ID["coding-algo"].verifier(QWEN_LRU)
        self.assertTrue(verdict.solved, "the cache solves the stated problem")
        self.assertEqual(verdict.edge_passed, 0, "capacity=0 must still fail")
        self.assertLess(verdict.score, 100.0, "and must still show in the score")
        self.assertTrue(any("[edge]" in d for d in verdict.detail),
                        f"the failure must be labelled: {verdict.detail}")

    def test_the_fixed_lru_scores_full(self):
        """Without this, the test above passes for a verifier that always
        fails -- and a verifier nothing can satisfy is not a measurement."""
        verdict = tasks.BY_ID["coding-algo"].verifier(FIXED_LRU)
        self.assertEqual(verdict.score, 100.0, f"failures: {verdict.detail}")

    def test_the_math_verifier_accepts_the_formats_the_prompt_allows(self):
        verifier = tasks.BY_ID["reasoning-math"].verifier
        for text in ("The answer is 0.5073, using 1 - prod(...)",
                     "P = 50.73% via 1 - \\frac{365!}{...}"):
            self.assertEqual(verifier(text).score, 100.0, text)
        self.assertLess(verifier("About one in two, I think.").score, 100.0)

    def test_claim_verifiers_are_labelled_as_claims(self):
        """A regex can confirm a stated answer, not the reasoning behind it.
        Presenting the two as one number is what this harness replaces.
        """
        self.assertEqual(
            tasks.BY_ID["reasoning-puzzle"].verifier("(4, 0, 4) in 7 pours").kind,
            "claim")
        self.assertEqual(tasks.BY_ID["coding-algo"].verifier(QWEN_LRU).kind, "exec")

    def test_there_is_a_floor_a_simple_tier_and_a_hard_tier(self):
        """The original set was all `hard`, which is why every Anthropic model
        landed between 90 and 100 and the differences that remained were a
        Python slicing quirk and an edge case the prompt never mentioned. A
        benchmark with no floor cannot tell a weak model from an unfair task.
        """
        levels = {t.difficulty for t in tasks.TASKS}
        self.assertEqual(levels, {"floor", "simple", "hard"})
        self.assertEqual(
            [t.id for t in tasks.TASKS if t.difficulty == "floor"], ["floor-add"],
            "exactly one control task, or it stops being a control")
        self.assertGreaterEqual(
            sum(1 for t in tasks.TASKS if t.difficulty == "simple"), 5)

    def test_the_floor_task_is_trivial_and_its_verifier_agrees(self):
        """If this ever fails for a real model, suspect the harness. It has
        been the harness twice in one day: a gateway rejecting an Anthropic
        model, and answers delivered as files."""
        verifier = tasks.BY_ID["floor-add"].verifier
        self.assertEqual(verifier("def add(a, b):\n    return a + b\n").score, 100.0)
        self.assertEqual(verifier("def add(a, b):\n    return a - b\n").core_passed, 1,
                         "only the -1+1==0 case survives a subtraction")
        self.assertFalse(verifier("def add(a, b):\n    return a - b\n").solved)

    def test_every_simple_task_is_solved_by_an_obvious_answer(self):
        """Guards the guard. A simple tier whose verifiers reject correct
        answers is worse than no simple tier -- it would read as every model
        being weak.
        """
        answers = {
            "simple-fizzbuzz": (
                "def fizzbuzz(n):\n"
                "    out = []\n"
                "    for i in range(1, n + 1):\n"
                "        if i % 15 == 0: out.append('FizzBuzz')\n"
                "        elif i % 3 == 0: out.append('Fizz')\n"
                "        elif i % 5 == 0: out.append('Buzz')\n"
                "        else: out.append(str(i))\n"
                "    return out\n"),
            "simple-count-vowels": (
                "def count_vowels(text):\n"
                "    return sum(1 for c in text.lower() if c in 'aeiou')\n"),
            "simple-reverse-words": (
                "def reverse_words(text):\n"
                "    return ' '.join(reversed(text.split()))\n"),
            "simple-sum-evens": (
                "def sum_evens(numbers):\n"
                "    return sum(n for n in numbers if n % 2 == 0)\n"),
            "simple-json-field": (
                "import json\n"
                "def active_names(raw):\n"
                "    return [o['name'] for o in json.loads(raw) if o.get('active')]\n"),
        }
        for task_id, code in answers.items():
            verdict = tasks.BY_ID[task_id].verifier(code)
            self.assertTrue(verdict.solved,
                            f"{task_id} rejected a correct answer: {verdict.detail}")
            self.assertEqual(verdict.score, 100.0,
                             f"{task_id} edge checks failed a good answer: "
                             f"{verdict.detail}")

    def test_every_simple_task_rejects_a_wrong_answer(self):
        """Otherwise the tier is decoration: a verifier that passes anything
        measures nothing, which is how a clean tree and a broken scanner became
        indistinguishable in the first place."""
        wrong = {
            "simple-fizzbuzz": "def fizzbuzz(n):\n    return []\n",
            "simple-count-vowels": "def count_vowels(text):\n    return 0\n",
            "simple-reverse-words": "def reverse_words(text):\n    return text\n",
            "simple-sum-evens": "def sum_evens(numbers):\n    return 0\n",
            "simple-json-field": "def active_names(raw):\n    return []\n",
        }
        for task_id, code in wrong.items():
            verdict = tasks.BY_ID[task_id].verifier(code)
            self.assertFalse(verdict.solved,
                             f"{task_id} accepted a stub answer")

    def test_core_and_edge_are_counted_separately(self):
        verdict = verify.run_checks(
            "x = 1", "assert x == 1\nassert x == 1", "assert x == 99")
        self.assertEqual((verdict.core_passed, verdict.core_total), (2, 2))
        self.assertEqual((verdict.edge_passed, verdict.edge_total), (0, 1))
        self.assertTrue(verdict.solved, "core passed, so the problem is solved")
        self.assertEqual(verdict.score, 66.7, "the edge miss still shows")
        self.assertEqual(verdict.core_score, 100.0)

    def test_a_core_failure_still_means_unsolved(self):
        verdict = verify.run_checks(
            "x = 1", "assert x == 99", "assert x == 1")
        self.assertFalse(verdict.solved)

    def test_claim_checks_are_all_core(self):
        """Each claim is a distinct question the prompt asked, so none of them
        is optional."""
        verdict = tasks.BY_ID["reasoning-math"].verifier("0.5073 via 1 - prod")
        self.assertEqual(verdict.core_total, verdict.total)
        self.assertTrue(verdict.solved)

    def test_the_needle_task_is_long_and_has_exactly_one_needle(self):
        task = tasks.BY_ID["long-context-needle"]
        self.assertGreater(len(task.prompt), 30_000,
                           "a long-context task that fits in 200 tokens tests "
                           "nothing the other six do not")
        self.assertEqual(task.prompt.count("4200"), 1)
        self.assertEqual(task.verifier("The window is 4200 seconds.").score, 100.0)
        self.assertLess(task.verifier("The window is 3600 seconds.").score, 100.0)

    def test_the_multi_turn_task_asks_for_something_only_context_supplies(self):
        """Its follow-up names no function, so a model that lost the thread
        cannot answer it by guessing."""
        task = tasks.BY_ID["multi-turn-resume"]
        self.assertIsNotNone(task.followup)
        self.assertNotIn("count_words", task.followup)
        good = "def count_words(t):\n    d={}\n    " \
               "for w in t.lower().split(): d[w]=d.get(w,0)+1\n    return d\n"
        self.assertEqual(task.verifier(good).score, 100.0)
        bad = "def count_words(t):\n    d={}\n    " \
              "for w in t.split(): d[w]=d.get(w,0)+1\n    return d\n"
        self.assertLess(task.verifier(bad).score, 100.0,
                        "the case-insensitive requirement was not applied")


class TruncationTests(unittest.TestCase):
    """A run that hit its ceiling must never be scoreable as a wrong answer."""

    def test_hitting_the_cap_is_recorded_not_inferred(self):
        turn = transports.Turn(text="partial", transport="http",
                               model_requested="m", output_tokens=transports.MAX_TOKENS,
                               stop_reason="max_tokens")
        self.assertTrue(turn.hit_cap)
        self.assertEqual(turn.cap_headroom, 0.0)

    def test_a_comfortable_run_reports_headroom(self):
        turn = transports.Turn(text="ok", transport="http", model_requested="m",
                               output_tokens=6097, stop_reason="end_turn")
        self.assertFalse(turn.hit_cap)
        self.assertGreater(turn.cap_headroom, 0.5)

    def test_unknown_token_count_is_unknown_not_zero_headroom(self):
        """Absent data must not read as a truncated run."""
        turn = transports.Turn(text="ok", transport="cli", model_requested="m")
        self.assertIsNone(turn.cap_headroom)
        self.assertFalse(turn.hit_cap)

    def test_no_placeholder_is_ever_substituted_for_thinking(self):
        """The single defect that cost a model its ranking.

        The old extractor wrote the literal string "[thinking]" when there was
        no text block, making truncation indistinguishable from silence. This
        asserts the string appears nowhere in the transport source.
        """
        source = (ROOT / "bench" / "transports.py").read_text(encoding="utf-8")
        code = "\n".join(
            line.split("#")[0] for line in source.splitlines()
            if not line.strip().startswith("#"))
        self.assertNotIn('"[thinking]"', code)
        self.assertNotIn("'[thinking]'", code)


class TransportTests(unittest.TestCase):
    def test_an_unknown_transport_is_refused_not_defaulted(self):
        with self.assertRaises(SystemExit):
            transports.send("smoke-signal", "m", [{"role": "user", "content": "x"}], "k")

    def test_both_transports_are_registered(self):
        self.assertEqual(set(transports.TRANSPORTS), {"http", "cli"})

    def test_the_cli_ttft_comes_from_the_result_frame_not_the_stream(self):
        """Both halves of this were learned the hard way.

        Timing the first `assistant` frame gives a fake TTFT: stream-json emits
        whole message blocks, so the earliest observable text is the finished
        answer, and that measured ttft=57.05 against total=57.44. An earlier
        version of this module concluded TTFT was therefore unmeasurable here
        and reported None -- which was also wrong, because the `result` frame
        carries `ttft_ms` outright. Same model, measured properly: 2.35s
        against 4.94s total.

        So the requirement is specific: read the field, not the stream.
        """
        source = (ROOT / "bench" / "transports.py").read_text(encoding="utf-8")
        cli = source.split("def _cli_once")[1].split("TRANSPORTS = ")[0]
        self.assertIn("ttft_ms", cli, "the result frame's own figure must be used")
        # The assignment must live in the `result` branch, not the `assistant`
        # branch where the fake measurement came from.
        assistant_branch = cli.split('kind == "assistant"')[1].split(
            'elif kind == "result"')[0]
        self.assertNotIn("turn.ttft_s =", assistant_branch,
                         "timing the first assistant frame measures the "
                         "finished answer, not a first token")

    def test_anthropic_models_get_the_bare_binary_and_the_login_env(self):
        """`wc-claude.sh` resolves the *active machine*, which is the gateway.

        Asking it for an Anthropic model returns
        `400 Invalid model name passed in model=claude-haiku-4-5` -- the
        gateway rejecting a model it does not serve. Found on the first live
        run, which is why this is pinned.
        """
        binary, env = transports._cli_invocation("claude-haiku-4-5")
        self.assertNotIn("wc-claude.sh", binary)
        self.assertIsNotNone(env)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        self.assertNotIn("ANTHROPIC_API_KEY", env,
                         "the host login is the credential here")
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env,
                         "CLAUDE_CODE_SIMPLE makes the CLI refuse to read the "
                         "host login, leaving the child with no credentials "
                         "at all (CLAUDE.md §3)")

    def test_gateway_models_still_go_through_the_wrapper(self):
        """Otherwise the http-versus-cli comparison stops being same-backend,
        and the 26x figure would be measuring two different services."""
        binary, env = transports._cli_invocation("vllm/Qwen3.6-35B-A3B-NVFP4")
        self.assertIn("wc-claude.sh", binary)
        self.assertIsNone(env, "the wrapper needs the inherited environment")

    def test_anthropic_models_are_not_offered_the_gateway(self):
        self.assertFalse(transports.reachable("claude-opus-5", "http"))
        self.assertTrue(transports.reachable("claude-opus-5", "cli"))
        self.assertIn("use --transports cli",
                      transports.why_unreachable("claude-opus-5", "http"))

    def test_an_unknown_model_is_tried_rather_than_skipped(self):
        """A new backend must not be silently dropped from the table."""
        self.assertTrue(transports.reachable("brand/new-model", "http"))
        self.assertTrue(transports.reachable("brand/new-model", "cli"))

    def test_every_result_records_its_transport(self):
        """A speed figure that does not say which path produced it is not a
        measurement -- the same task is 26x apart between these two."""
        self.assertEqual(
            transports.Turn(text="", transport="cli", model_requested="m").transport,
            "cli")


class CostTests(unittest.TestCase):
    """Cost per correct answer, and the ways it must not lie."""

    @staticmethod
    def _runs(n_correct: int, n_total: int):
        return [{"input_tokens": 100, "output_tokens": 1000,
                 "correct": i < n_correct} for i in range(n_total)]

    def test_cached_input_is_priced_not_ignored(self):
        """Costing a turn from `input_tokens` alone understates it hugely.

        The CLI caches its system prompt and tool definitions, so opus-5
        reported a constant 12,029 input tokens for every task -- including the
        30k-token long-context one -- and haiku-4-5 reported 10. A single
        trivial haiku turn actually moved 12,276 cache writes and 18,905 cache
        reads. My first cost table priced haiku's whole 16-run set at $0.2868
        on `input_tokens`; one floor-add turn alone bills $0.0269.
        """
        rates = {"m": {"input": 5.0, "output": 25.0,
                       "cache_read": 0.5, "cache_write": 10.0}}
        without = cost.summarise("m", [{
            "input_tokens": 10, "output_tokens": 100, "correct": True}], rates)
        with_cache = cost.summarise("m", [{
            "input_tokens": 10, "output_tokens": 100,
            "cache_read_tokens": 18905, "cache_write_tokens": 12276,
            "correct": True}], rates)
        self.assertGreater(with_cache.dollars, without.dollars * 20,
                           "cache tokens dominate and must be charged")
        self.assertEqual(with_cache.cache_read_tokens, 18905)

    def test_the_vendors_own_figure_beats_a_computed_one(self):
        """The CLI reports total_cost_usd with a costBasis. Where the basis is
        'list' that is what gets billed, and nothing computed here beats it."""
        rates = {"m": {"input": 999.0, "output": 999.0}}
        spend = cost.summarise("m", [{
            "input_tokens": 10, "output_tokens": 100, "correct": True,
            "reported_cost_usd": 0.0269425, "cost_basis": "list"}], rates)
        self.assertEqual(spend.dollars, 0.026943)
        self.assertEqual(spend.basis, "list")
        self.assertIn("reported by the CLI", spend.source)

    def test_a_partially_reported_set_says_so_rather_than_mixing(self):
        rates = {"m": {"input": 1.0, "output": 1.0}}
        spend = cost.summarise("m", [
            {"input_tokens": 10, "output_tokens": 10, "correct": True,
             "reported_cost_usd": 0.5, "cost_basis": "list"},
            {"input_tokens": 10, "output_tokens": 10, "correct": True},
        ], rates)
        self.assertIn("computed", spend.source)
        self.assertIn("1 of 2", spend.detail)

    def test_an_unrecorded_rate_is_unknown_not_free(self):
        """The important one. A model whose price nobody wrote down must not
        appear as the cheapest option in a table."""
        spend = cost.summarise("mystery/model", self._runs(2, 2), {})
        self.assertFalse(spend.known)
        self.assertIsNone(spend.dollars)
        self.assertIsNone(spend.per_correct)
        self.assertEqual(spend.note, "rate not recorded")

    def test_a_free_backend_that_is_correct_costs_zero_per_correct(self):
        """The finding the old table could not express."""
        spend = cost.summarise("vllm/Qwen3.6-35B-A3B-NVFP4", self._runs(2, 2),
                               {"vllm/*": {"input": 0.0, "output": 0.0}})
        self.assertTrue(spend.known)
        self.assertEqual(spend.dollars, 0.0)
        self.assertEqual(spend.per_correct, 0.0)

    def test_cost_per_correct_rises_when_answers_are_wrong(self):
        rates = {"paid/m": {"input": 1.0, "output": 10.0}}
        both = cost.summarise("paid/m", self._runs(2, 2), rates)
        one = cost.summarise("paid/m", self._runs(1, 2), rates)
        self.assertGreater(one.per_correct, both.per_correct,
                           "a wrong answer must make each correct one dearer")

    def test_never_correct_is_undefined_not_infinity(self):
        """`inf` in a table gets read as a large number rather than as an
        absence, and the difference decides whether the row is comparable."""
        spend = cost.summarise("paid/m", self._runs(0, 3),
                               {"paid/m": {"input": 1.0, "output": 10.0}})
        self.assertIsNone(spend.per_correct)
        self.assertIn("undefined", spend.note)

    def test_a_glob_rate_matches_a_family(self):
        self.assertEqual(
            cost.rate_for("vllm/anything-at-all", {"vllm/*": {"input": 0.0}}),
            {"input": 0.0})
        self.assertIsNone(cost.rate_for("azure_ai/x", {"vllm/*": {"input": 0.0}}))

    def test_documentation_keys_are_not_backends(self):
        """`bench_rates.json` carries a `_comment` recording where the numbers
        came from. Without filtering it, `rate_for` hands that comment back as
        though it were a rate card."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rates.json"
            path.write_text(json.dumps({
                "_comment": ["a note", "another note"],
                "_source": "derived",
                "paid/m": {"input": 1.0, "output": 2.0},
            }), encoding="utf-8")
            import os
            old = os.environ.get("WC_BENCH_RATES")
            os.environ["WC_BENCH_RATES"] = str(path)
            try:
                rates = cost.load_rates()
            finally:
                if old is None:
                    os.environ.pop("WC_BENCH_RATES", None)
                else:
                    os.environ["WC_BENCH_RATES"] = old
        self.assertEqual(set(rates), {"paid/m"})
        self.assertIsNone(cost.rate_for("_comment", rates))

    def test_the_committed_rates_file_is_loadable_and_omits_azure(self):
        """Azure rows are absent on purpose: nobody recorded what this
        deployment pays, and an absent rate must read as unknown rather than
        as free."""
        rates = cost.load_rates()
        self.assertIn("claude-opus-5", rates)
        self.assertEqual(rates["claude-opus-5"]["input"], 5.0)
        self.assertEqual(rates["claude-opus-5"]["output"], 25.0)
        self.assertIsNone(cost.rate_for("azure_ai/gpt-5.6-luna", rates))
        self.assertEqual(cost.rate_for("vllm/anything", rates)["input"], 0.0)

    def test_an_exact_rate_beats_a_glob(self):
        rates = {"vllm/*": {"input": 0.0}, "vllm/special": {"input": 9.0}}
        self.assertEqual(cost.rate_for("vllm/special", rates), {"input": 9.0})


class RunnerTests(unittest.TestCase):
    """The entry point's own safety properties."""

    def test_it_refuses_to_overwrite_an_existing_results_file(self):
        """The old script wrote into the current directory and clobbered the
        only copy of the six-model baseline."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "existing.json"
            target.write_text("{}", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "wc-bench.py"),
                 "--models", "m", "--out", str(target)],
                capture_output=True, text=True, timeout=120, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing to overwrite", result.stdout + result.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_list_names_every_task_and_how_it_is_verified(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "wc-bench.py"), "--list"],
            capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for task in tasks.TASKS:
            self.assertIn(task.id, result.stdout)
        self.assertIn("exec", result.stdout)
        self.assertIn("claim", result.stdout)

    @staticmethod
    def _runner():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wcbench", ROOT / "bin" / "wc-bench.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_default_repeat_count_can_measure_consistency(self):
        """Consistency was weighted 9% of the old score and measured zero
        times, because every task ran exactly once. A default of 1 would
        recreate that silently, so the default is asserted rather than trusted.
        """
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "wc-bench.py"), "--help"],
            capture_output=True, text=True, timeout=120, check=False)
        self.assertIn("--repeats", result.stdout)
        self.assertIn("default: 3", result.stdout,
                      "the default must be visible in --help; a silent 1 would "
                      "recreate the unmeasured-consistency defect")

    def test_aggregate_reports_a_spread_not_a_single_number(self):
        module = self._runner()
        runs = [
            {"model": "m", "task": "t", "transport": "http", "score": 100.0,
             "correct": True, "total_s": 10.0, "ttft_s": 1.0, "hit_cap": False,
             "error": None, "verified_by": "exec"},
            {"model": "m", "task": "t", "transport": "http", "score": 50.0,
             "correct": False, "total_s": 20.0, "ttft_s": 2.0, "hit_cap": False,
             "error": None, "verified_by": "exec"},
        ]
        agg = module.aggregate(runs)["m|t|http"]
        self.assertEqual(agg["pass_rate"], 0.5)
        self.assertEqual(agg["score_spread"], [50.0, 100.0])
        self.assertEqual(agg["repeats"], 2)


if __name__ == "__main__":
    unittest.main()
