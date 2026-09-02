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


# --- verifiers ---------------------------------------------------------------


def _verify_bug_fix(response: str) -> verify.Verdict:
    """Executed. The bug is subtle enough that reading is not enough.

    `seen` is built by walking the list backwards, so the returned order and
    the slice direction both matter. Two of the checks below distinguish a fix
    that returns the right *elements* from one that also returns them in the
    right *order* -- the original bug report says "preserving order", and an
    implementation can pass the first while failing the second.
    """
    checks = '''
assert last_n_unique([1, 2, 3, 2, 1], 2) == [2, 1]
assert last_n_unique([1, 2, 3], 2) == [2, 3]
assert last_n_unique([1, 2, 3], 5) == [1, 2, 3]
assert last_n_unique([], 3) == []
assert last_n_unique([7, 7, 7], 2) == [7]
assert last_n_unique(["a", "b", "a", "c"], 3) == ["b", "a", "c"]
assert last_n_unique([1, 2, 3, 4], 0) == []
assert last_n_unique.__doc__, "the prompt asked for a docstring"
import inspect; assert inspect.signature(last_n_unique).parameters, "no params"
assert getattr(last_n_unique, "__annotations__", None), "the prompt asked for type hints"
'''
    return verify.run_checks(verify.extract_code(response), checks)


def _verify_lru(response: str) -> verify.Verdict:
    """Executed, including the edge case three models got wrong by hand-review.

    `capacity=0` is checked explicitly. All three of gpt-5.6-sol, gpt-5-mini and
    Qwen3.6 shipped an LRU that raises on it, scored ⚠️ twice and 0 once, and
    the difference was who happened to trace it. Either behaviour is defensible
    -- reject at construction, or accept and store nothing -- so the check
    accepts both and fails only a crash.
    """
    checks = '''
c = LRUCache(2); c.put(1, 1); c.put(2, 2)
assert c.get(1) == 1
c.put(3, 3)
assert c.get(2) == -1, "least-recently-used key was not evicted"
assert c.get(3) == 3
c2 = LRUCache(2); c2.put(1, 1); c2.put(2, 2); c2.get(1); c2.put(3, 3)
assert c2.get(2) == -1 and c2.get(1) == 1, "get() did not count as a use"
c3 = LRUCache(1); c3.put(1, 1); c3.put(1, 9)
assert c3.get(1) == 9, "overwriting an existing key lost the new value"
c4 = LRUCache(2)
assert c4.get(99) == -1, "a miss must return -1"
try:
    c5 = LRUCache(0); c5.put(1, 1)
    assert c5.get(1) == -1, "a zero-capacity cache stored something"
except (ValueError, TypeError):
    pass
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
    return verify.run_checks(verify.extract_code(response), checks)


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


# --- the set -----------------------------------------------------------------


TASKS: tuple[Task, ...] = (
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
)

BY_ID = {task.id: task for task in TASKS}
