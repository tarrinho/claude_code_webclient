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
from typing import Final

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


@dataclass(frozen=True)
class Classification:
    task_type: str
    score: int
    mutates: str


def classify(text: str) -> Classification:
    """Spec section 2, including 2.1's three resolution rules.

    Every multi-match is worth logging in production so pattern refinement is
    driven by data rather than guesswork (2.1); the caller does that, because
    this module stays free of a logger for the same reason it stays free of a
    database -- it is a pure function and its tests say so.
    """
    lowered = (text or "").lower()
    matched = [p for p in PATTERNS if re.search(p[0], lowered)]
    if not matched:
        return Classification(DEFAULT_TASK_TYPE, 1, DEFAULT_MUTATES)

    # task_type: the most specific pattern wins, and specificity is the
    # pattern's own length. Not list order -- ordering a dict was how this
    # silently depended on insertion sequence.
    most_specific = max(matched, key=lambda p: len(p[0]))

    # score: the HIGHEST among all matches, not the winning pattern's own.
    # Score feeds the size factor and therefore the deadline (5.1), and
    # under-budgeting a deadline manufactures a false timeout escalation.
    score = max(p[1] for p in matched)

    # mutates: any disagreement resolves to True (2.1), and side_effecting_read
    # outranks False for the same reason -- both are refused a transport.
    values = {p[3] for p in matched}
    if MUTATES_TRUE in values:
        mutates = MUTATES_TRUE
    elif MUTATES_SIDE_EFFECTING_READ in values:
        mutates = MUTATES_SIDE_EFFECTING_READ
    else:
        mutates = MUTATES_FALSE

    return Classification(most_specific[2], score, mutates)
