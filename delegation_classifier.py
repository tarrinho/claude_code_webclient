"""Classify a task into (task_type, score, mutates) -- spec section 2.

Split out of orchestrator.COMPLEXITY_PATTERNS, which returned a score alone.
The two extra fields are what the rest of the design needs: task_type selects
the ladder, and mutates decides placement (section 8) and which pipeline
stages run (section 4.7).

Pure: no database, no config read, no import from app. The router is testable
by table without spawning anything (section 11).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable

MUTATES_FALSE: Final[str] = "false"
MUTATES_SIDE_EFFECTING_READ: Final[str] = "side_effecting_read"
MUTATES_TRUE: Final[str] = "true"

#: (regex, score, task_type, mutates) -- spec section 2's table, verbatim.
PATTERNS: Final[tuple[tuple[str, int, str, str], ...]] = (
    (r"architect|design.*system|create.*framework", 4, "planning", MUTATES_FALSE),
    (r"implement.*multiple|coordinate.*agent|orchestrate", 5, "planning", MUTATES_TRUE),
    (r"debug.*complex|trace.*error.*chain|performance.*bottleneck", 4, "reasoning", MUTATES_FALSE),
    (r"write.*test.*suite|integration.*test|e2e.*test", 3, "coding", MUTATES_TRUE),
    (r"analyze.*code.*review|refactor.*large|migrate.*database", 4, "coding", MUTATES_TRUE),
    (r"write.*doc.*umentation|create.*tutorial|explain.*concept", 2, "comprehension", MUTATES_TRUE),
    (r"research.*api.*document|find.*replacement|evaluate.*option", 3, "comprehension", MUTATES_FALSE),
    (r"read.*file|list.*directory|grep.*pattern|summarize.*log", 1, "long-context", MUTATES_FALSE),
    (r"simple|small|quick|minor|fix.*typo", 1, "coding", MUTATES_TRUE),
)

DEFAULT_TASK_TYPE: Final[str] = "comprehension"
DEFAULT_MUTATES: Final[str] = MUTATES_TRUE
#: Section 2.2 states only the task_type and mutates defaults; score is silent.
#: Ruling: 3 is the reference/mid-size score (5.1's size factor 1.0), not the
#: table's minimum -- an unclassifiable task must not get the smallest budget,
#: because under-budgeting manufactures a false timeout escalation (2.1).
DEFAULT_SCORE: Final[int] = 3

#: Spec section 2.1: the regex metacharacters excluded from a literal count.
_METACHARACTERS: Final[frozenset[str]] = frozenset(".*+?|()[]\\^$")


@dataclass(frozen=True)
class Classification:
    task_type: str
    score: int
    mutates: str


def literal_character_count(alternative: str) -> int:
    """Section 2.1: count of characters in one matched alternative that are
    not regex metacharacters. `refactor.*large` is 13 (the `.` and `*` do not
    count); `quick` is 5.
    """
    return sum(1 for ch in alternative if ch not in _METACHARACTERS)


def _ranked_alternatives(lowered: str) -> list[tuple[int, int, str, tuple[str, int, str, str]]]:
    """Every `|`-alternative, across every row of PATTERNS, that matches
    `lowered`, as (literal_count, matched_span_length, alternative, row).

    All nine PATTERNS rows use top-level alternation only (`a|b|c`), so
    splitting on `|` is exactly "one branch of a pattern's `|`" as section 2.1
    describes. A future pattern using a *grouped* alternation such as
    `(a|b)c` would need a real parser to isolate its branches -- this split
    is not one, and is not meant to be.

    Built in PATTERNS' own order, row by row and alternative by alternative,
    so that Python's max() -- which returns the first maximal item it sees --
    resolves a full tie (equal literal count and equal span) to table order
    for free, exactly as section 2.1 specifies.
    """
    candidates: list[tuple[int, int, str, tuple[str, int, str, str]]] = []
    for row in PATTERNS:
        for alternative in row[0].split("|"):
            match = re.search(alternative, lowered)
            if match:
                span = match.end() - match.start()
                candidates.append((literal_character_count(alternative), span, alternative, row))
    return candidates


def _specificity_key(candidate: tuple[int, int, str, tuple[str, int, str, str]]) -> tuple[int, int]:
    """Section 2.1's ranking key: literal count first, matched span second.

    Table order is not part of the key -- it falls out of max()'s
    first-maximal-wins behaviour over candidates built in table order.
    """
    literal_count, span, _alternative, _row = candidate
    return (literal_count, span)


def winning_alternative(text: str) -> str | None:
    """Section 11 (line 1057): the alternative that wins specificity for
    `text`, exposed directly because two alternatives can share a task_type
    while differing in which one actually wins -- `classify().task_type`
    alone cannot distinguish them.
    """
    lowered = (text or "").lower()
    candidates = _ranked_alternatives(lowered)
    if not candidates:
        return None
    return max(candidates, key=_specificity_key)[2]


def resolve_mutates(mutates_values: Iterable[str]) -> str:
    """Section 2.1, literally: unanimous agreement keeps that value; any
    disagreement resolves to True. side_effecting_read is not ranked between
    False and True -- it only survives when every matched pattern agrees on
    it, which no production PATTERNS row does yet (section 2.3 keeps it
    distinct so it can eventually be tagged and cost-accounted separately).
    """
    values = set(mutates_values)
    if len(values) == 1:
        return next(iter(values))
    return MUTATES_TRUE


def classify(text: str) -> Classification:
    """Spec section 2, including 2.1's three resolution rules.

    Every multi-match is worth logging in production so pattern refinement is
    driven by data rather than guesswork (2.1); the caller does that, because
    this module stays free of a logger for the same reason it stays free of a
    database -- it is a pure function and its tests say so.
    """
    lowered = (text or "").lower()
    candidates = _ranked_alternatives(lowered)
    if not candidates:
        return Classification(DEFAULT_TASK_TYPE, DEFAULT_SCORE, DEFAULT_MUTATES)

    # task_type: the most specific matched alternative wins (2.1), ties
    # broken by longer matched span then by table order (see
    # _ranked_alternatives and _specificity_key).
    winner = max(candidates, key=_specificity_key)
    task_type = winner[3][2]

    # matched rows, deduplicated and in table order, for score and mutates --
    # those two resolve over every matched *pattern*, not over the single
    # winning alternative.
    matched_rows = list(dict.fromkeys(candidate[3] for candidate in candidates))

    # score: the HIGHEST among all matched rows, not the winning alternative's
    # own. Score feeds the size factor and therefore the deadline (5.1), and
    # under-budgeting a deadline manufactures a false timeout escalation.
    score = max(row[1] for row in matched_rows)

    mutates = resolve_mutates(row[3] for row in matched_rows)

    return Classification(task_type, score, mutates)
