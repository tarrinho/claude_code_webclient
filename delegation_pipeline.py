"""delegation_pipeline.py -- which of the five stages run, and in what order.

Spec section 4.8's precedence table, as code. Three rules can each subtract
stages -- one of them can also restore a stage an earlier rule removed --
and the order they resolve in is the whole behaviour:

  1. Blast radius (4.6) first. It is the only rule keyed on what the task
     DID rather than what its text predicted, so it overrides the trivial
     bypass: a score-1 task that touched more than MAX_FILES_TRIVIAL files
     runs the full pipeline regardless of what the classifier guessed.
  2. Trivial bypass (4.8) next, if blast radius did not override it. Its
     effect depends on `mutates`: a trivial WRITE drops stages 3, 4 and 5;
     a trivial READ drops only 4 and 5 -- stage 3 is never removed from a
     read here, because 4.7 makes it the read's only real gate and 4.8 is
     explicit that no rule may take it away.
  3. Read-only (4.7) last, applied to whatever survives: mutates=False
     drops 4 and 5 (a no-op if step 2 already dropped them) and restores
     stage 3 if any earlier rule removed it. 4.8's invariant is that a
     read-only leaf never reaches the end without a reviewer gate, no
     matter how the earlier rules resolved.

`mutates` is the three-valued string from delegation_classifier, not a
bool. Only the literal MUTATES_FALSE value is "read-only" for this module:
MUTATES_SIDE_EFFECTING_READ spends money or consumes an external rate
limit, so 4.7 explicitly holds it to the same five stages as MUTATES_TRUE,
including under the trivial bypass, which has no read-shaped row for it.
"""
from __future__ import annotations

from typing import Final

from delegation_classifier import Classification, MUTATES_FALSE

#: Above this many changed files, a task gets the full pipeline whatever the
#: classifier's score said (4.6).
MAX_FILES_TRIVIAL: Final[int] = 3

#: At or below this score, a task is a candidate for the trivial bypass (4.8).
TRIVIAL_SCORE: Final[int] = 1

ALL_STAGES: Final[tuple[int, ...]] = (1, 2, 3, 4, 5)


def stages_for(decision: Classification, files_changed: int) -> list[int]:
    """The stages, in ascending order, that run for one leaf.

    Implements the precedence in section 4.8: blast radius, then the
    trivial bypass, then read-only -- read-only applied last so it can
    restore stage 3 no matter what the earlier two rules did.
    """
    stages = set(ALL_STAGES)

    is_read_only = decision.mutates == MUTATES_FALSE
    over_blast_radius = files_changed > MAX_FILES_TRIVIAL
    trivial_bypass_applies = decision.score <= TRIVIAL_SCORE and not over_blast_radius

    # 2. Trivial bypass, scoped by mutates (4.8): a trivial write loses its
    #    reviewer gate along with QA and security; a trivial read never
    #    loses the reviewer gate, only QA and security.
    if trivial_bypass_applies:
        if is_read_only:
            stages -= {4, 5}
        else:
            stages -= {3, 4, 5}

    # 3. Read-only, last, applied to whatever survives (4.7): drop 4 and 5,
    #    and restore 3 if an earlier rule removed it -- a read-only leaf
    #    never ends up with no verification at all.
    if is_read_only:
        stages -= {4, 5}
        stages.add(3)

    return sorted(stages)
