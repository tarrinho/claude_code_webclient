"""The task set, each carrying its own verifier.

Six tasks are carried over from `bin/model_benchmark.py` so the new numbers can
be compared with the old ones. Two are added for blind spots that mattered for
this product specifically:

* ``long-context-needle`` — every original task was 74-166 input tokens, while
  WebConsole's actual workload is transcripts running to tens of thousands. The
  benchmark had no measurement in the regime the product operates in.
* ``multi-turn-resume`` — ``--resume`` is core to the console (CLAUDE.md §6) and
  no task tested a second turn, so a model that loses the thread on turn three
  scored identically to one that does not.

Each task declares how it is verified, and the verifier is mechanical in every
case. Where mechanical verification can only confirm a stated answer rather
than the reasoning behind it, the task uses ``claim`` and the output says so.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import verify

FUNCTION_UNDER_EXPLANATION = '''\
def summarise(data):
    result = {}
    for item in data:
        k = item.get("category", "unknown")
        result.setdefault(k, []).append(item.get("value", 0))
    return {k: sum(v) / len(v) for k, v in result.items()}
'''


@dataclass
class Task:
    """One prompt and the mechanical check that scores its answer."""

    id: str
    description: str
    task_type: str
    prompt: str
    #: Takes the response text, returns a Verdict.
    verifier: Callable[[str], verify.Verdict]
    #: Second prompt, sent in the same session. Only ``multi-turn-resume`` uses
    #: one; the verifier then sees both replies joined by a blank line.
    followup: str | None = None
    #: Rough input size, so a long-context task is not silently compared with a
    #: 100-token one on cost.
    tags: tuple[str, ...] = field(default_factory=tuple)
    #: When true, the verifier needs code from *every* turn, not just the
    #: last. ``multi-turn-recall`` is the only task where this is true: turn 2
    #: asks for ``split_fields`` alone, reusing a delimiter turn 1 chose and
    #: never restated, so the last reply by itself never defines
    #: ``join_fields`` and the round-trip assertion cannot execute. Every other
    #: multi-turn followup asks for "the full function", making its last reply
    #: self-contained on purpose -- this flag must stay off for those, or a
    #: model could satisfy a recall check by answering fresh in turn 2 with no
    #: memory of turn 1 at all. Runner support lives in `bin/wc-bench.py`,
    #: `run_one`.
    needs_all_turns: bool = False
    #: ``floor`` is a control every model must pass -- a failure there means a
    #: broken invocation, not a weak model. ``simple`` discriminates at the
    #: bottom, ``hard`` at the top. The original set was all ``hard``, which is
    #: why every Anthropic model landed between 90 and 100 and the remaining
    #: differences were Python trivia.
    difficulty: str = "hard"


# --- verifiers ---------------------------------------------------------------


def _verify_bug_fix(response: str) -> verify.Verdict:
    """Executed. The bug is subtle enough that reading is not enough.

    `seen` is built by walking the list backwards, so the returned order and
    the slice direction both matter. Two of the checks below distinguish a fix
    that returns the right *elements* from one that also returns them in the
    right *order* -- the original bug report says "preserving order", and an
    implementation can pass the first while failing the second.
    """
    core = '''
assert last_n_unique([1, 2, 3, 2, 1], 2) == [2, 1]
assert last_n_unique([1, 2, 3], 2) == [2, 3]
assert last_n_unique([1, 2, 3], 5) == [1, 2, 3]
assert last_n_unique([], 3) == []
assert last_n_unique([7, 7, 7], 2) == [7]
assert last_n_unique(["a", "b", "a", "c"], 3) == ["b", "a", "c"]
assert last_n_unique.__doc__, "the prompt asked for a docstring"
assert getattr(last_n_unique, "__annotations__", None), "the prompt asked for type hints"
'''
    # n=0 is the edge tier. `seen[::-1][-n:]` is the natural fix and it returns
    # the whole list for n=0, because x[-0:] is x[0:]. Qwen3.6, sonnet-5 and
    # haiku-4-5 all write it that way. Calling three models incorrect for a
    # Python slicing quirk says nothing useful about any of them, and it put a
    # 10-of-11 answer in the same column as a response containing no code.
    edge = '''
assert last_n_unique([1, 2, 3, 4], 0) == []
'''
    return verify.run_checks(verify.extract_code(response), core, edge)


def _verify_lru(response: str) -> verify.Verdict:
    """Executed, including the edge case three models got wrong by hand-review.

    `capacity=0` is checked explicitly. All three of gpt-5.6-sol, gpt-5-mini and
    Qwen3.6 shipped an LRU that raises on it, scored ⚠️ twice and 0 once, and
    the difference was who happened to trace it. Either behaviour is defensible
    -- reject at construction, or accept and store nothing -- so the check
    accepts both and fails only a crash.
    """
    core = '''
c = LRUCache(2)
c.put(1, 1)
c.put(2, 2)
assert c.get(1) == 1
c.put(3, 3)
assert c.get(2) == -1, "least-recently-used key was not evicted"
assert c.get(3) == 3
c2 = LRUCache(2)
c2.put(1, 1)
c2.put(2, 2)
c2.get(1)
c2.put(3, 3)
assert c2.get(2) == -1 and c2.get(1) == 1, "get() did not count as a use"
c3 = LRUCache(1)
c3.put(1, 1)
c3.put(1, 9)
assert c3.get(1) == 9, "overwriting an existing key lost the new value"
c4 = LRUCache(2)
assert c4.get(99) == -1, "a miss must return -1"
import time
big = LRUCache(5000)
start = time.monotonic()
for i in range(5000):
    big.put(i, i)
for i in range(5000):
    big.get(i)
assert time.monotonic() - start < 3.0, "10k operations took over 3s; not O(1)"
assert repr(LRUCache(2)), "the prompt asked for a __repr__"
'''
    # capacity=0 is the edge tier. The prompt never mentions it, and every
    # model that got it wrong wrote `len(cache) == capacity`, which is the
    # obvious formulation. It is worth reporting and not worth failing an
    # otherwise-correct O(1) cache over.
    #
    # The setup lines above are also one statement each now, rather than
    # semicolon-joined: `c = LRUCache(2); c.put(1,1); c.put(2,2)` was counted
    # as three separate checks, so trivial setup inflated the denominator to 27
    # and made a single real failure read as 96.3%.
    edge = '''
try:
    c5 = LRUCache(0)
    c5.put(1, 1)
    assert c5.get(1) == -1, "a zero-capacity cache stored something"
except (ValueError, TypeError):
    pass
'''
    return verify.run_checks(verify.extract_code(response), core, edge)


# --- edit-shaped coding tasks -------------------------------------------------
#
# Added 2026-09-15. The suite held two `coding` tasks and they split badly:
# measured at n=20, the free model scored 8/10 on coding-algo (write an LRU
# cache) and 3/10 on coding-bug-fix (repair an existing function). An
# orchestrator's coding leaves are predominantly edits to code that already
# exists, so the aggregate of those two tasks was not measuring the workload.
# Two tasks also cannot separate "weak at repair" from "weak at that one bug".
#
# All four below hand the model working-but-wrong code and ask for a change.
# See docs/superpowers/specs/2026-09-14-tiered-agent-delegation-spec-v3.md
# section 2.6, Measurement provenance.


def _verify_edit_mutable_default(response: str) -> verify.Verdict:
    """Executed. The bug only shows across *calls*, so reading one call passes.

    A model that keeps the mutable default and merely adds a docstring produces
    code that looks right and fails the second call. That is the whole point of
    executing rather than reviewing, and it is why the second assertion below
    matters more than the first.
    """
    core = '''
assert add_tag("a") == ["a"]
assert add_tag("b") == ["b"], "the default list persisted between calls"
assert add_tag("c") == ["c"], "the default list persisted between calls"
assert add_tag("y", ["x"]) == ["x", "y"], "an explicit list must still be appended to"
assert add_tag.__doc__, "the prompt asked for a docstring"
'''
    # Whether the caller's own list is mutated in place or copied is genuinely
    # open -- the original mutates, and "fix the shared-default bug" does not
    # say to stop mutating. Both are defensible, so this is reported, not failed.
    edge = '''
given = ["x"]
result = add_tag("y", given)
assert result == ["x", "y"]
'''
    return verify.run_checks(verify.extract_code(response), core, edge)


def _verify_edit_chunks(response: str) -> verify.Verdict:
    """Executed. An off-by-one that silently drops data rather than raising.

    The natural misreading is that the loop bound is fine and the slice is
    wrong. Both produce correct output on inputs whose length divides evenly by
    `size`, which is why the first check uses a length that does not.
    """
    core = '''
assert chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]], "the final partial chunk was dropped"
assert chunks([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]
assert chunks([1], 5) == [[1]], "a list shorter than one chunk returned nothing"
assert chunks([], 3) == []
assert chunks([1, 2, 3], 1) == [[1], [2], [3]]
'''
    # size <= 0 is unspecified by the prompt. Raising and returning [] are both
    # reasonable; hanging forever is not, so the check accepts either answer and
    # fails only a non-terminating or crashing implementation.
    edge = '''
try:
    out = chunks([1, 2, 3], 0)
    assert out == [], "size=0 returned chunks"
except (ValueError, ZeroDivisionError):
    pass
'''
    return verify.run_checks(verify.extract_code(response), core, edge)


def _verify_edit_extend_cases(response: str) -> verify.Verdict:
    """Executed. Measures regression preservation, not just the new feature.

    The first two assertions are the *existing* behaviour the prompt does not
    mention. A model that rewrites the parser around the new units and drops
    seconds or minutes has done what was asked and broken what was working --
    the exact failure stages 3 and 4 of the pipeline exist to catch, and the
    reason this task is scored on the old cases before the new ones.
    """
    core = '''
assert parse_duration("30s") == 30, "existing behaviour for seconds regressed"
assert parse_duration("5m") == 300, "existing behaviour for minutes regressed"
assert parse_duration("2h") == 7200
assert parse_duration("1d") == 86400
assert parse_duration("0s") == 0
'''
    # The original raises ValueError on unparseable input. Preserving the
    # exception *type* is a reasonable reading and so is raising anything at
    # all, so a different exception is reported rather than failed.
    edge = '''
try:
    parse_duration("nonsense")
    raise AssertionError("unparseable input did not raise")
except ValueError:
    pass
'''
    return verify.run_checks(verify.extract_code(response), core, edge)


def _verify_edit_top_scores(response: str) -> verify.Verdict:
    """Executed. The bug is a wrong sort direction, which returns the *worst*
    n rather than the best -- output that is well-formed, plausible, and
    exactly backwards.

    No oracle short of running it catches this: the return type, the length and
    the element shape are all correct. It is the closest task in the suite to
    the failure mode section 6 of the spec calls out as uncaught, "a well-formed
    wrong answer".
    """
    core = '''
rows = [{"name": "a", "score": 1}, {"name": "b", "score": 9}, {"name": "c", "score": 5}]
assert [r["name"] for r in top_scores(rows, 1)] == ["b"], "returned the lowest score, not the highest"
assert [r["name"] for r in top_scores(rows, 2)] == ["b", "c"]
assert len(top_scores(rows, 10)) == 3, "n larger than the input must return everything"
assert top_scores([], 3) == []
assert top_scores(rows, 0) == []
'''
    # Tie-break order is unspecified. Python's sort is stable, so input order is
    # the natural outcome, but a model that sorts by (-score, name) is not
    # wrong -- only different. Reported.
    edge = '''
tied = [{"name": "z", "score": 5}, {"name": "a", "score": 5}]
assert [r["name"] for r in top_scores(tied, 2)] == ["z", "a"]
'''
    return verify.run_checks(verify.extract_code(response), core, edge)


def _verify_math(response: str) -> verify.Verdict:
    """A claim check, but a numeric one: the answer is a single value.

    Accepts 0.5073, 50.73%, and the unrounded 0.50729... because the prompt did
    not specify a format and penalising one would measure formatting.
    """
    return verify.check_claims(response, [
        ("the answer 0.5073 (or 50.73%)", r"0\.507[23]|50\.7[23]\s*%"),
        ("the complement form 1 - P(no match)", r"1\s*[-−]\s*|\\left\(|prod|\\frac"),
    ])


def _verify_puzzle(response: str) -> verify.Verdict:
    """A claim check, and the weakest verifier here -- stated as such.

    Verifying a pour sequence properly means simulating it, which means parsing
    free-form prose into state transitions. That parser would fail on valid
    answers formatted differently and score its own brittleness as a model
    error, which is the failure mode this harness exists to avoid. So this
    confirms the *stated* result only.
    """
    return verify.check_claims(response, [
        ("the target state (4,0,4)", r"\(?\s*4\s*,\s*0\s*,\s*4\s*\)?"),
        ("a stated pour count of 7 or fewer", r"\b[1-7]\b\s*(pours?|steps?|moves?)"),
    ])


def _verify_comprehension(response: str) -> verify.Verdict:
    """Claim check on the four questions, with the discriminating one last.

    The fourth question is what separated the 100s from the 75s by hand: every
    model names ZeroDivisionError, and only some say why it is unreachable in
    the code as written. That distinction is a string match, so it can be one.
    """
    return verify.check_claims(response, [
        ("returns a dict of per-category means", r"(dict|dictionar|mapping).*(mean|average)"),
        ("names ZeroDivisionError", r"ZeroDivisionError"),
        ("handles the empty-dict case via .get defaults", r"unknown|\.get\(|default"),
        ("says the zero-item case is unreachable as written",
         (r"unreachable|cannot happen|can't happen|impossible|never occur|"
          r"prevent|guarantee")),
    ])


def _verify_planning(response: str) -> verify.Verdict:
    """Claim check. A plan's quality is not mechanically decidable; its
    coverage is, so coverage is what this scores and the output labels it."""
    return verify.check_claims(response, [
        ("a schema change", r"ALTER TABLE|CREATE TABLE|migrat"),
        ("a persistence mechanism for the choice", r"data-theme|localStorage|cookie|CSS variable"),
        ("system preference handling", r"prefers-color-scheme|system"),
        ("an API endpoint", r"(GET|POST|PATCH|PUT)\s*/|endpoint|route"),
        ("first-paint flash prevention", r"FOUC|flash|inline script|before.*paint|early"),
    ])


# --- the simple tier -------------------------------------------------------
#
# Added because the original six discriminate only at the top. Every Anthropic
# model scored 90-100 on the coding tasks, and the differences that remained
# were a Python slicing quirk (`x[-0:]`) and an edge case the prompt never
# mentioned (`capacity=0`) -- so the set separated models on trivia while
# reporting it as correctness. A benchmark needs a floor as well as a ceiling:
# without easy tasks there is no way to tell "this model is weak" from "this
# task was unfair", and no way to notice the harness breaking.
#
# `floor-add` exists purely as a control. Any model that fails it has a broken
# invocation, not a capability gap, and that has now happened twice in one day
# (the gateway rejecting an Anthropic model, and answers delivered as files).


def _verify_floor(response: str) -> verify.Verdict:
    """The control. If this fails, suspect the harness before the model."""
    return verify.run_checks(verify.extract_code(response), '''
assert add(2, 3) == 5
assert add(-1, 1) == 0
assert add(0, 0) == 0
''')


def _verify_fizzbuzz(response: str) -> verify.Verdict:
    return verify.run_checks(verify.extract_code(response), '''
out = fizzbuzz(15)
assert len(out) == 15, "expected 15 entries for n=15"
assert out[0] == "1" or out[0] == 1, "1 should be itself"
assert out[2] in ("Fizz",), "3 should be Fizz"
assert out[4] in ("Buzz",), "5 should be Buzz"
assert out[14] in ("FizzBuzz",), "15 should be FizzBuzz"
''', '''
assert fizzbuzz(0) == [], "n=0 should give an empty list"
''')


def _verify_count_vowels(response: str) -> verify.Verdict:
    return verify.run_checks(verify.extract_code(response), '''
assert count_vowels("hello") == 2
assert count_vowels("") == 0
assert count_vowels("xyz") == 0
assert count_vowels("AEIOU") == 5, "uppercase vowels count too"
assert count_vowels("aeiou") == 5
''')


def _verify_reverse_words(response: str) -> verify.Verdict:
    return verify.run_checks(verify.extract_code(response), '''
assert reverse_words("hello world") == "world hello"
assert reverse_words("one") == "one"
assert reverse_words("") == ""
assert reverse_words("a b c") == "c b a"
''', '''
assert reverse_words("  padded  words  ") == "words padded", \\
    "collapsing repeated whitespace is the usual reading"
''')


def _verify_sum_evens(response: str) -> verify.Verdict:
    return verify.run_checks(verify.extract_code(response), '''
assert sum_evens([1, 2, 3, 4]) == 6
assert sum_evens([]) == 0
assert sum_evens([1, 3, 5]) == 0
assert sum_evens([2]) == 2
assert sum_evens([-2, -4, 1]) == -6, "negative evens are still even"
''')


def _verify_json_field(response: str) -> verify.Verdict:
    return verify.run_checks(verify.extract_code(response), '''
assert active_names('[{"name":"a","active":true},{"name":"b","active":false}]') == ["a"]
assert active_names("[]") == []
assert active_names('[{"name":"x","active":true},{"name":"y","active":true}]') == ["x", "y"]
''', '''
assert active_names('[{"name":"z"}]') == [], \\
    "a missing 'active' key should not count as active"
''')


NEEDLE = "The proxy token rotation window is 4200 seconds"


def _long_context_prompt() -> str:
    """A transcript-shaped haystack with one retrievable fact in it.

    Deterministic, so the task is identical across models and repeats. Built
    from filler that looks like this product's own logs rather than lorem
    ipsum, because a model that has learned to skim log noise should have to
    skim realistic noise.
    """
    filler = []
    for i in range(400):
        filler.append(
            f"[turn {i:04d}] user: check chat {i} status\n"
            f"[turn {i:04d}] assistant: chat {i} is idle, last activity "
            f"2026-08-{(i % 28) + 1:02d}T0{i % 10}:00:00Z, no pending question"
        )
    filler.insert(233, f"[turn 0233] assistant: note for the record — {NEEDLE}.")
    return (
        "Below is an excerpt from a WebConsole session log.\n\n"
        + "\n".join(filler)
        + "\n\nQuestion: according to the log, how long is the proxy token "
          "rotation window? Answer with just the number and its unit."
    )


def _verify_needle(response: str) -> verify.Verdict:
    """Exact retrieval. Either the number came back or it did not."""
    return verify.check_claims(response, [
        ("the value 4200", r"\b4\s*200\b|\b4200\b"),
        ("the unit (seconds)", r"second|\bs\b|sec"),
        ("no contradicting number from the filler noise",
         r"^(?!.*\b(3600|1800|600|300)\b).*$"),
    ])


def _verify_multi_turn(response: str) -> verify.Verdict:
    """Scored on the second reply, which is where context is either kept or not.

    The first turn asks for a function; the second asks for a change stated only
    in terms of the first ("make it case-insensitive"). A model that lost the
    thread will ask what function, or invent a different one.
    """
    return verify.run_checks(verify.extract_code(response), '''
assert count_words("Hello hello WORLD") == {"hello": 2, "world": 1}, \\
    "the second turn did not apply case-insensitivity to the first turn's function"
assert count_words("") == {}
assert count_words("a b a") == {"a": 2, "b": 1}
''')


# --- filling in the four one-task types --------------------------------------
#
# comprehension, planning, long-context and multi-turn each had exactly one
# task, so a model measured on them at --repeats 5 produced five runs of the
# same prompt. That is a measurement of one task, not of a type, and reading it
# as a type was how `vllm/Qwen3.6-35B-A3B-NVFP4` ended up with 23 responses that
# were all coding while the tier policy spoke about six types. Three tasks each
# below.
#
# Every existing task above is left byte-identical on purpose: changing a
# prompt would silence the comparison with the 2026-09-04 run rather than
# extend it.

DIFF_UNDER_REVIEW = '''\
@@ -1,7 +1,7 @@
 def active_sessions(rows, now):
     out = []
     for r in rows:
-        if r["last_seen"] > now - 3600:
+        if r["last_seen"] >= now - 3600:
             out.append(r)
-    return out
+    return out[:50]
'''

FAILING_SUMMARISE = '''\
def summarise(rows):
    total, counts = {}, {}
    for r in rows:
        total[r["env"]] = total.get(r["env"], 0) + r["value"]
        if r.get("ok"):
            counts[r["env"]] = counts.get(r["env"], 0) + 1
    return {k: total[k] / counts[k] for k in total}
'''

SUMMARISE_TRACEBACK = '''\
Traceback (most recent call last):
  File "report.py", line 12, in <module>
    print(summarise(rows))
  File "report.py", line 8, in summarise
    return {k: total[k] / counts[k] for k in total}
KeyError: 'staging'
'''

RETRY_POLICY = '''\
timeout_s: 30
retries: 2
backoff: exponential, base 2s
retry_on: [502, 503, 504]
'''


def _verify_comprehension_diff(response: str) -> verify.Verdict:
    """Claim check on a two-change diff where only one change is dangerous.

    Both edits are visible in three lines; the discriminator is the fourth
    claim, which asks what `out[:50]` actually selects. Nothing sorts `rows`,
    so the slice keeps the first fifty in whatever order the caller supplied --
    a model that reports the cap without noticing that has read the diff but
    not the consequence.
    """
    return verify.check_claims(response, [
        ("the boundary became inclusive",
         r">=|inclusive|boundary|equal|exactly 3600|on the hour"),
        ("the result is now capped at 50", r"\b50\b|truncat|\bcap\b|limit|slice"),
        ("the cap can drop sessions silently",
         r"silent|drop|lose|lost|miss|hidden|without (warning|notice|error)"),
        ("which 50 is undefined, because nothing sorts first",
         r"order|sort|arbitrar|unsorted|undefined|which 50|non.?determin"),
    ])


def _verify_comprehension_traceback(response: str) -> verify.Verdict:
    """Claim check on a KeyError whose cause is two dicts filled unequally.

    `total` gains a key for every row; `counts` only for rows with `ok`. The
    comprehension then iterates `total`. The shallow reading is "'staging' is
    missing from counts", which restates the traceback; the discriminating one
    names the condition that produces it -- an env whose rows are all not-ok.
    """
    return verify.check_claims(response, [
        ("names the KeyError on 'staging'", r"KeyError|missing key|'staging'|\"staging\""),
        ("counts is only populated for ok rows",
         r"if r\.get|only.*\bok\b|\bok\b.*only|conditional|guard|truthy"),
        ("the comprehension iterates total, not counts",
         r"for k in total|iterat\w*\s+(over\s+)?total|keys of total|total.*keys"),
        ("an env with zero ok rows is what triggers it",
         r"(zero|no|none|not a single|never)\b[^.]{0,40}\bok\b|all.*fail|every.*not ok"),
    ])


def _verify_comprehension_config(response: str) -> verify.Verdict:
    """Claim check with a trap: `retries: 2` is present but does not apply.

    The scenario's first response is 500, which is absent from `retry_on`, so
    the call fails on attempt one and the 503 in the scenario is never reached.
    A model that pattern-matches the retries field answers "three attempts".
    """
    return verify.check_claims(response, [
        ("exactly one attempt is made",
         r"\b(one|1|single)\b[^.]{0,30}(attempt|try|request|call)|"
         r"(attempt|try|request|call)[^.]{0,20}\b(one|1|once)\b|no retr|not retr"),
        ("because 500 is not in retry_on",
         r"500[^.]{0,40}(not|absent|excluded|missing)|"
         r"(not|absent|excluded|missing)[^.]{0,40}500|only.*50[234]"),
        ("no backoff delay is waited",
         r"no\s+(backoff|wait|delay|sleep)|zero\s+(wait|delay)|"
         r"\b0\s*s(econds)?\b|immediat"),
        ("the 503 is never reached", r"never|not reach|no second|moot|irrelevant|does not get"),
    ])


def _verify_planning_migration(response: str) -> verify.Verdict:
    """Claim check on the ordering that makes the migration safe, not on prose.

    The steps are individually obvious and the order is the whole answer: a
    plan that adds the constraint before the backfill has described an outage.
    """
    return verify.check_claims(response, [
        ("add the column nullable first",
         r"nullable|allow null|without\s+NOT NULL|no default|DEFAULT NULL"),
        ("backfill in batches", r"batch|chunk|increment|in\s+slices|rate.?limit"),
        ("keep old and new readers working during the change",
         r"dual.?write|write.*both|backward.?compat|deploy.*first|tolerat"),
        ("apply NOT NULL only after the backfill",
         r"(NOT NULL|constraint)[^.]{0,60}(last|after|final|then|once)|"
         r"(after|once)[^.]{0,60}(NOT NULL|constraint)|VALIDATE CONSTRAINT"),
        ("a rollback path", r"rollback|revert|undo|back out|roll back"),
    ])


def _verify_planning_incident(response: str) -> verify.Verdict:
    """Claim check on mitigate-before-diagnose.

    The trap is that the interesting work is the investigation, so a plan that
    opens with it reads as thorough while leaving the error rate running.
    """
    return verify.check_claims(response, [
        ("restore service first", r"rollback|revert|roll back|mitigat|stop the bleed|restore|disable"),
        ("investigate after restoring, not before",
         r"(then|after|once)[^.]{0,60}(investigat|root cause|diagnos)|"
         r"(investigat|root cause|diagnos)[^.]{0,60}(after|later|once restored)|"
         r"before[^.]{0,40}(investigat|debug)"),
        ("preserve evidence before it rotates away",
         r"log|evidence|capture|snapshot|preserve|retain|trace"),
        ("tell people it is happening", r"communicat|status page|notify|inform|stakeholder|announce"),
        ("a follow-up that prevents a repeat",
         r"post.?mortem|prevent|follow.?up|action item|regression test|guard"),
    ])


def _verify_planning_testing(response: str) -> verify.Verdict:
    """Claim check on a test plan for money-moving code.

    Idempotency is the discriminator: a retry feature that is not idempotent
    charges twice, and a plan that lists unit/integration/edge cases without
    naming it has planned tests for the wrong feature.
    """
    return verify.check_claims(response, [
        ("unit tests", r"unit test|unit-level|\bunit\b"),
        ("integration or end-to-end coverage", r"integration|end.to.end|e2e|contract test"),
        ("the failure paths, not just the happy one",
         r"fail|error|timeout|network|exception|5\d\d|unavailab"),
        ("idempotency, so a retry cannot double-charge",
         r"idempoten|double.?charg|duplicate|exactly.?once|dedup"),
        ("how you would know it works in production",
         r"monitor|metric|alert|observab|canary|dashboard|log"),
    ])


def _log_filler(lines: int = 400) -> list[str]:
    """Deterministic transcript-shaped noise, shared by the long-context tasks.

    Kept separate from `_long_context_prompt`'s own inline copy so the original
    task's bytes do not move; this one is free to differ.
    """
    return [
        f"[turn {i:04d}] user: check chat {i} status\n"
        f"[turn {i:04d}] assistant: chat {i} is idle, last activity "
        f"2026-08-{(i % 28) + 1:02d}T0{i % 10}:00:00Z, no pending question"
        for i in range(lines)
    ]


#: Indices where the countable event is planted. Seven of them, and the count
#: is the answer -- so the number must not also be derivable from the filler.
_TIMEOUT_AT = (17, 88, 141, 202, 263, 318, 377)


def _long_context_count_prompt() -> str:
    """Aggregation rather than retrieval: the answer is a count, not a value.

    A needle task rewards finding one line and stopping. This one cannot be
    answered without traversing the whole log, which is the property the
    product actually depends on when a model reads a long transcript.
    """
    filler = _log_filler()
    for idx in _TIMEOUT_AT:
        filler[idx] = (
            f"[turn {idx:04d}] assistant: chat 42 turn timed out after "
            f"the deadline and was abandoned"
        )
    return (
        "Below is an excerpt from a WebConsole session log.\n\n"
        + "\n".join(filler)
        + "\n\nQuestion: how many turns for chat 42 timed out in this log? "
          "Answer with just the number."
    )


def _long_context_pair_prompt() -> str:
    """Two facts, far apart, and the question needs both.

    Either alone gives a plausible-looking wrong answer, so this separates a
    model that retrieved from one that retrieved *and* combined.
    """
    filler = _log_filler()
    filler[62] = "[turn 0062] assistant: note for the record — the session TTL is 900 seconds."
    filler[341] = "[turn 0341] assistant: note for the record — the cache TTL is 300 seconds."
    return (
        "Below is an excerpt from a WebConsole session log.\n\n"
        + "\n".join(filler)
        + "\n\nQuestion: according to the log, how much longer is the session "
          "TTL than the cache TTL? Answer with just the number and its unit."
    )


def _long_context_absent_prompt() -> str:
    """The fact is not in the log. Saying so is the correct answer.

    This is the only task in the set whose pass condition is a refusal, and it
    is here because the failure it catches is invisible to every other one: a
    model that invents a plausible number scores identically to one that found
    a real one, on any task where a number exists to be found.
    """
    return (
        "Below is an excerpt from a WebConsole session log.\n\n"
        + "\n".join(_log_filler())
        + "\n\nQuestion: according to the log, what is the database connection "
          "pool size? Answer with just the number, or say so if the log does "
          "not state it."
    )


def _verify_long_context_count(response: str) -> verify.Verdict:
    """Exact count, with the near-misses excluded explicitly.

    Off-by-one is the expected failure here -- a model that skims finds six or
    eight -- so 6 and 8 are checked for and rejected rather than merely not
    rewarded.
    """
    return verify.check_claims(response, [
        ("the count 7", r"\b(7|seven)\b"),
        ("no off-by-one answer alongside it", r"^(?!.*\b(6|8|six|eight)\b).*$"),
    ])


def _verify_long_context_pair(response: str) -> verify.Verdict:
    """The difference, not either input value.

    900 and 300 are both in the log and both wrong as answers, so each is
    excluded: quoting a retrieved number instead of computing with it is the
    specific failure this task exists to see.
    """
    return verify.check_claims(response, [
        ("the difference, 600", r"\b600\b|six hundred"),
        ("the unit (seconds)", r"second|\bs\b|sec"),
        ("not just one of the two retrieved values",
         r"^(?!.*\b(900|300)\s*(second|s\b|sec)).*$"),
    ])


def _verify_long_context_absent(response: str) -> verify.Verdict:
    """Passes only on an admission, and fails on any invented figure.

    The second claim is the real one: a response can say "the log does not
    state it" and then guess anyway, and a check that only looked for the
    admission would score that as correct.
    """
    return verify.check_claims(response, [
        ("says the log does not state it",
         r"not (present|mention|found|stated|specified|in the log|given)|"
         r"does not (appear|mention|contain|state|specify)|"
         r"no (mention|record|entry|reference|information)|absent|"
         r"can(not|'t) (find|determine)|isn'?t (there|mentioned|stated)|unknown"),
        ("does not invent a pool size anyway",
         r"^(?!.*\b\d+\s*(connection|pool)).*$"),
    ])


def _verify_multi_turn_rename(response: str) -> verify.Verdict:
    """The second turn renames *and* changes a rule, so a lost thread shows up.

    A model that kept context ships `parse_config` with last-wins duplicates; a
    model that lost it either keeps the old name or the old first-wins rule,
    and each is a separate assertion.
    """
    return verify.run_checks(verify.extract_code(response), '''
assert parse_config("a=1;b=2") == {"a": "1", "b": "2"}
assert parse_config("a=1;a=2") == {"a": "2"}, \\
    "the second turn's last-duplicate-wins rule was not applied"
assert parse_config("") == {}
''', '''
assert parse_config("a=1;") == {"a": "1"}
assert parse_config("a=b=c") == {"a": "b=c"}, "only the first = separates"
''')


def _verify_multi_turn_constraint(response: str) -> verify.Verdict:
    """The second turn adds a constraint the first turn's code did not have."""
    return verify.run_checks(verify.extract_code(response), '''
assert average([1, 2, 3]) == 2
assert average([5]) == 5
try:
    average([])
except ValueError:
    pass
else:
    raise AssertionError("the second turn's ValueError on empty input is missing")
''')


def _verify_multi_turn_recall(response: str) -> verify.Verdict:
    """Round-trip, which tests recall without the harness knowing the answer.

    The first turn picks a delimiter; the second must reuse *that* one, and the
    harness never learns which it was. A model that forgets and picks a second
    delimiter fails the round trip, so this measures context retention with an
    executed check rather than a claim -- the only task here that can.
    """
    return verify.run_checks(verify.extract_code(response), '''
assert split_fields(join_fields(["a", "b", "c"])) == ["a", "b", "c"], \\
    "split_fields did not reverse join_fields -- the delimiter was not carried over"
assert split_fields(join_fields(["one"])) == ["one"]
''', '''
assert split_fields(join_fields([])) == []
''')


# --- the set -----------------------------------------------------------------



# --- voice (spec 2.6's `voice` task type) -----------------------------------
#
# Written 2026-09-17, from the shape of a real recorded voice conversation
# (conversation_recording.py) rather than from its content: that file holds a
# private conversation and this one is committed, so these tasks match the
# REGISTER -- short spoken prompts, a factual answer expected back in a
# sentence or two -- without copying anything out of it.
#
# The register matters more than it looks. Spec 5.1 records the reference
# model changing identity once the task mix was equalised, and every existing
# task here is a written prompt of 100+ tokens. A real voice turn measured
# 23-52 characters in and 205-212 tokens of total input, almost all of it the
# system prompt and context block. Benchmarking voice against the written set
# would measure the wrong workload.
#
# All four are SINGLE-TURN on purpose. `routes/voice.stream_voice_turn` builds
# its message list fresh per turn -- system prompt, context block, current
# prompt -- and never includes earlier turns; the recorded conversation shows
# input tokens flat at 205-212 across four turns, and the model answering
# "no that's the old one" with no idea what "the old one" was. A multi-turn
# voice task would measure that known defect rather than the model.


def _verify_voice_arithmetic(response: str) -> verify.Verdict:
    """Spoken arithmetic with one unambiguous answer.

    `36` is checked with a word boundary so `360` or `1936` do not satisfy it,
    and the spelled form is accepted because a voice model may answer in words.
    """
    return verify.check_claims(response, [
        ("states 36", r"\b36\b|thirty[- ]?six"),
    ])


def _verify_voice_conversion(response: str) -> verify.Verdict:
    """A unit conversion a voice assistant is routinely asked for."""
    return verify.check_claims(response, [
        ("states 210 minutes", r"\b210\b|two hundred (and )?ten"),
    ])


def _verify_voice_ordering(response: str) -> verify.Verdict:
    """Three items, all three required, so a partial answer scores partially --
    the discriminator is whether the model keeps a short spoken list intact."""
    return verify.check_claims(response, [
        ("names Mercury", r"\bmercury\b"),
        ("names Venus", r"\bvenus\b"),
        ("names Earth", r"\bearth\b"),
    ])


def _verify_voice_declines_to_invent(response: str) -> verify.Verdict:
    """The property a voice assistant fails most expensively: inventing an
    answer it cannot have.

    The prompt asks for something unknowable from the model alone -- a
    specific person's current whereabouts -- and the only correct behaviour is
    to say it cannot know. Scored as two claims so that a model which declines
    but then guesses anyway does not score full marks: `no_invention` fails on
    a confident location.
    """
    return verify.check_claims(response, [
        ("says it cannot know",
         r"can'?t|cannot|unable|don'?t (know|have)|no (way|access)|not able"),
        ("does not invent a location",
         r"^(?!.*\b(is (currently )?(in|at)|located in)\b).*$"),
    ])


TASKS: tuple[Task, ...] = (
    Task(
        id="floor-add",
        description="Add two integers (control task)",
        task_type="coding",
        difficulty="floor",
        verifier=_verify_floor,
        prompt="""Write a Python function add(a, b) that returns the sum of two integers. Return valid Python code only.""",
    ),
    Task(
        id="simple-fizzbuzz",
        description="FizzBuzz up to n",
        task_type="coding",
        difficulty="simple",
        verifier=_verify_fizzbuzz,
        prompt="""Write a Python function fizzbuzz(n) that returns a list of length n. For each i from 1 to n inclusive, the entry is "FizzBuzz" if i is divisible by both 3 and 5, "Fizz" if by 3, "Buzz" if by 5, and otherwise the string form of i. Return valid Python code only.""",
    ),
    Task(
        id="simple-count-vowels",
        description="Count vowels in a string",
        task_type="coding",
        difficulty="simple",
        verifier=_verify_count_vowels,
        prompt="""Write a Python function count_vowels(text) that returns how many vowels (a, e, i, o, u) the string contains, counting both upper and lower case. Return valid Python code only.""",
    ),
    Task(
        id="simple-reverse-words",
        description="Reverse the word order of a sentence",
        task_type="coding",
        difficulty="simple",
        verifier=_verify_reverse_words,
        prompt="""Write a Python function reverse_words(text) that returns the string with its whitespace-separated words in reverse order, joined by single spaces. Return valid Python code only.""",
    ),
    Task(
        id="simple-sum-evens",
        description="Sum the even numbers in a list",
        task_type="coding",
        difficulty="simple",
        verifier=_verify_sum_evens,
        prompt="""Write a Python function sum_evens(numbers) that returns the sum of the even integers in the list. An empty list sums to 0. Return valid Python code only.""",
    ),
    Task(
        id="simple-json-field",
        description="Extract a field from a small JSON document",
        task_type="coding",
        difficulty="simple",
        verifier=_verify_json_field,
        prompt="""Write a Python function active_names(raw) that takes a JSON string containing a list of objects, each with a "name" string and an "active" boolean, and returns the list of names whose "active" is true, in the order they appear. Return valid Python code only.""",
    ),
    Task(
        id="coding-bug-fix",
        description="Fix: return last n unique elements preserving order",
        task_type="coding",
        verifier=_verify_bug_fix,
        prompt="""Fix this Python function. It's supposed to return the last 'n' unique elements from a list, preserving order. It currently has a bug that drops the last element:

```python
def last_n_unique(lst, n):
    seen = []
    for x in reversed(lst):
        if x not in seen:
            seen.append(x)
    return seen[:n]
```

Also add a docstring and type hints. Keep the function name. Return valid Python code only.""",
    ),
    Task(
        id="coding-algo",
        description="Implement LRU cache O(1)",
        task_type="coding",
        verifier=_verify_lru,
        prompt="""Implement an LRU cache in Python with O(1) get and put. Class name LRUCache, constructor takes capacity, methods get(key) returning -1 on a miss and put(key, value). Add a __repr__. Return valid Python code only.""",
    ),
    Task(
        id="coding-edit-mutable-default",
        description="Fix: shared mutable default argument",
        task_type="coding",
        verifier=_verify_edit_mutable_default,
        prompt="""Fix this Python function. Callers report that tags from earlier calls keep showing up in later ones:

```python
def add_tag(tag, tags=[]):
    tags.append(tag)
    return tags
```

Keep the function name and signature order. Add a docstring. Return valid Python code only.""",
    ),
    Task(
        id="coding-edit-chunks",
        description="Fix: final partial chunk is dropped",
        task_type="coding",
        verifier=_verify_edit_chunks,
        prompt="""Fix this Python function. It should split a list into consecutive chunks of at most `size`, but it silently drops the last one when the list does not divide evenly:

```python
def chunks(items, size):
    out = []
    for i in range(0, len(items) - size, size):
        out.append(items[i:i + size])
    return out
```

Keep the function name. Return valid Python code only.""",
    ),
    Task(
        id="coding-edit-extend-cases",
        description="Add hours and days without breaking seconds and minutes",
        task_type="coding",
        verifier=_verify_edit_extend_cases,
        prompt="""Extend this Python function to also accept hours ('2h') and days ('1d'), returning the duration in seconds:

```python
def parse_duration(s):
    \"\"\"'30s' -> 30, '5m' -> 300\"\"\"
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("m"):
        return int(s[:-1]) * 60
    raise ValueError(f"bad duration: {s}")
```

Keep the function name. Return valid Python code only.""",
    ),
    Task(
        id="coding-edit-top-scores",
        description="Fix: returns the lowest scores instead of the highest",
        task_type="coding",
        verifier=_verify_edit_top_scores,
        prompt="""Fix this Python function. It is supposed to return the `n` highest-scoring rows, best first, but it is returning the lowest ones:

```python
def top_scores(rows, n):
    return sorted(rows, key=lambda r: r["score"])[:n]
```

Each row is a dict with a "name" and a numeric "score". Keep the function name. Return valid Python code only.""",
    ),
    Task(
        id="reasoning-puzzle",
        description="Water jug: reach (4,0,4) in minimum pours",
        task_type="reasoning",
        verifier=_verify_puzzle,
        prompt="""You have three jugs of capacity 8, 5 and 3 litres. The 8 is full, the others empty: (8, 0, 0). Pouring is only ever from one jug into another, and stops when the source is empty or the destination is full. Reach (4, 0, 4) in the minimum number of pours. Give the sequence of states and the number of pours.""",
    ),
    Task(
        id="reasoning-math",
        description="Birthday paradox: P(at least one match) with 23 people",
        task_type="reasoning",
        verifier=_verify_math,
        prompt="""With 23 people in a room, what is the probability that at least two share a birthday? Assume 365 equally likely birthdays and no twins. Show the formula and give the numeric answer to 4 decimal places.""",
    ),
    Task(
        id="comprehension-read",
        description="Explain a Python function and find edge cases",
        task_type="comprehension",
        verifier=_verify_comprehension,
        prompt=f"""Read this function and answer four questions.

```python
{FUNCTION_UNDER_EXPLANATION}```

1. What does it return?
2. What happens if data contains an empty dict?
3. What happens if a category has zero items?
4. What edge case could cause a ZeroDivisionError?""",
    ),
    Task(
        id="planning-task",
        description="Design dark mode toggle for FastAPI app",
        task_type="planning",
        verifier=_verify_planning,
        prompt="""Plan the work to add a system/light/dark theme toggle to a FastAPI app that serves static HTML and stores per-user settings in SQLite. Cover the schema change, the API, the frontend, and how you avoid a flash of the wrong theme on first paint. Use numbered lists.""",
    ),
    Task(
        id="long-context-needle",
        description="Retrieve one fact from a transcript-shaped log",
        task_type="long-context",
        verifier=_verify_needle,
        prompt=_long_context_prompt(),
        tags=("long-input",),
    ),
    Task(
        id="multi-turn-resume",
        description="Apply a second-turn change to the first turn's code",
        task_type="multi-turn",
        verifier=_verify_multi_turn,
        prompt="""Write a Python function count_words(text) that returns a dict mapping each whitespace-separated word to how many times it appears. Return valid Python code only.""",
        followup="""Now make it case-insensitive, lowercasing every key. Keep the name. Return the full function as valid Python code only.""",
        tags=("multi-turn",),
    ),

    # --- comprehension, the other two ---------------------------------------
    Task(
        id="comprehension-diff",
        description="Read a two-change diff and name the consequence of each",
        task_type="comprehension",
        verifier=_verify_comprehension_diff,
        prompt=f"""Read this diff and answer three questions.

```diff
{DIFF_UNDER_REVIEW}```

1. What behaviour changed?
2. Which change could cause a caller to silently miss data?
3. Of the rows that survive, which ones are they?""",
    ),
    Task(
        id="comprehension-traceback",
        description="Find the root cause of a KeyError from code plus traceback",
        task_type="comprehension",
        verifier=_verify_comprehension_traceback,
        prompt=f"""Here is a function and the traceback it produced.

```python
{FAILING_SUMMARISE}```

```
{SUMMARISE_TRACEBACK}```

Explain why the KeyError happens. Say what has to be true of the input for it
to occur, not just which key was missing.""",
    ),
    Task(
        id="comprehension-config",
        description="Apply a retry policy to a scenario it does not cover",
        task_type="comprehension",
        verifier=_verify_comprehension_config,
        prompt=f"""Here is a client's retry policy.

```yaml
{RETRY_POLICY}```

A request returns 500, and the server would return 503 if asked again. How
many attempts does the client make in total, how long does it spend waiting in
backoff, and does it reach the 503? Explain why.""",
    ),

    # --- planning, the other two ---------------------------------------------
    Task(
        id="planning-migration",
        description="Zero-downtime NOT NULL column on a 50M-row table",
        task_type="planning",
        verifier=_verify_planning_migration,
        prompt="""Plan adding a NOT NULL column with a default to a 50-million-row PostgreSQL table, with the application serving traffic throughout and deploys happening independently of migrations. Give the steps in the order you would run them, and say what you would do if step three failed halfway. Use numbered lists.""",
    ),
    Task(
        id="planning-incident",
        description="Respond to a deploy that is currently raising errors",
        task_type="planning",
        verifier=_verify_planning_incident,
        prompt="""A deploy went out twenty minutes ago and the API error rate went from 0.1% to 7%. It is still live and users are affected. Plan what you do, in order, from now until the incident is closed. Use numbered lists.""",
    ),
    Task(
        id="planning-testing",
        description="Test plan for a payment retry feature",
        task_type="planning",
        verifier=_verify_planning_testing,
        prompt="""Plan how you would test a new feature that automatically retries failed card payments up to three times over 24 hours. Cover what you test, at which level, and how you would know it was working once released. Use numbered lists.""",
    ),

    # --- long-context, the other two -----------------------------------------
    Task(
        id="long-context-count",
        description="Count matching events across a transcript-shaped log",
        task_type="long-context",
        verifier=_verify_long_context_count,
        prompt=_long_context_count_prompt(),
        tags=("long-input",),
    ),
    Task(
        id="long-context-pair",
        description="Combine two facts stated far apart in a long log",
        task_type="long-context",
        verifier=_verify_long_context_pair,
        prompt=_long_context_pair_prompt(),
        tags=("long-input",),
    ),
    Task(
        id="long-context-absent",
        description="Report that a requested fact is not in the log",
        task_type="long-context",
        verifier=_verify_long_context_absent,
        prompt=_long_context_absent_prompt(),
        tags=("long-input",),
    ),

    # --- multi-turn, the other two -------------------------------------------
    Task(
        id="multi-turn-rename",
        description="Rename and change a rule in the first turn's function",
        task_type="multi-turn",
        verifier=_verify_multi_turn_rename,
        prompt="""Write a Python function parse_pairs(text) that parses "a=1;b=2" into {"a": "1", "b": "2"}. Values stay strings. If a key repeats, the first occurrence wins. Return valid Python code only.""",
        followup="""Rename it to parse_config and change the duplicate rule so the last occurrence wins instead. Return the full function as valid Python code only.""",
        tags=("multi-turn",),
    ),
    Task(
        id="multi-turn-constraint",
        description="Add an error case to the first turn's function",
        task_type="multi-turn",
        verifier=_verify_multi_turn_constraint,
        prompt="""Write a Python function average(nums) that returns the arithmetic mean of a list of numbers. Return valid Python code only.""",
        followup="""Now make it raise ValueError when the list is empty. Keep the name. Return the full function as valid Python code only.""",
        tags=("multi-turn",),
    ),
    Task(
        id="multi-turn-recall",
        description="Reuse a choice made in the first turn, unnamed in the second",
        task_type="multi-turn",
        verifier=_verify_multi_turn_recall,
        prompt="""Choose a single delimiter character and write a Python function join_fields(fields) that joins a list of strings with it. State which delimiter you chose. Return the explanation and valid Python code.""",
        followup="""Now write split_fields(s) that reverses it, using the same delimiter you chose. Return valid Python code only.""",
        tags=("multi-turn",),
        needs_all_turns=True,
    ),
    # --- voice, spec 2.6's empty task type --------------------------------
    #
    # `voice` had NO tasks at all until 2026-09-17, which is the whole reason
    # its ladder is empty and its accuracy column reads TBD for every model:
    # nothing could be measured because nothing could be run.
    Task(
        id="voice-arithmetic",
        description="Spoken percentage, answered in a sentence",
        task_type="voice",
        verifier=_verify_voice_arithmetic,
        prompt="what is fifteen percent of two hundred and forty",
        tags=("voice", "simple"),
    ),
    Task(
        id="voice-conversion",
        description="Spoken unit conversion",
        task_type="voice",
        verifier=_verify_voice_conversion,
        prompt="how many minutes are there in three and a half hours",
        tags=("voice", "simple"),
    ),
    Task(
        id="voice-ordering",
        description="Keep a short spoken list intact and in order",
        task_type="voice",
        verifier=_verify_voice_ordering,
        prompt="name the first three planets from the sun",
        tags=("voice", "simple"),
    ),
    Task(
        id="voice-declines-to-invent",
        description="Decline an unknowable question instead of inventing one",
        task_type="voice",
        verifier=_verify_voice_declines_to_invent,
        prompt="where is my colleague Ana right now",
        tags=("voice", "hard"),
    ),
)

BY_ID = {task.id: task for task in TASKS}
