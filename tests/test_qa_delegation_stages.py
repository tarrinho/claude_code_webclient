"""QA: which pipeline stages run -- spec 4.6, 4.7 and 4.8, and their order.

Three rules each subtract stages, and one of them (4.7, read-only) can also
RESTORE a stage an earlier rule removed. The order they resolve in is the
whole behaviour: a stage set that is correct for every rule taken alone, but
wrong once two apply to the same task, is the defect this file exists to
catch. In particular: a trivial-bypass implementation that collapses every
score-1 task to stages [1, 2] regardless of `mutates` looks correct against
any test that only ever checks the write case or the read case in isolation,
but is wrong per spec 4.8 -- a trivial READ keeps stage 3, because 4.7 makes
stage 3 the read's only real gate and 4.8 says no rule may remove it. This
file's trivial-read and blast-radius-on-a-read cases exist specifically to
catch that collapse.

It also checks that `mutates` is treated as the three-valued string it is,
not as a boolean or a truthiness check: MUTATES_SIDE_EFFECTING_READ must be
refused the read-only exemption (4.7 holds it to the same five stages as
MUTATES_TRUE), including under the trivial bypass, where a string-equality
bug (or a bug that only distinguishes "== MUTATES_TRUE" from "everything
else") would let it slip through as if it were a read.
"""
from __future__ import annotations

import unittest

import delegation_classifier as dc
import delegation_pipeline as pipeline


def _d(task_type="coding", score=3, mutates=dc.MUTATES_TRUE):
    return dc.Classification(task_type, score, mutates)


class StageSelectionTests(unittest.TestCase):
    def test_a_non_trivial_write_runs_all_five(self):
        self.assertEqual(
            pipeline.stages_for(_d(score=3), files_changed=1),
            [1, 2, 3, 4, 5],
        )

    def test_a_trivial_write_runs_one_and_two(self):
        self.assertEqual(
            pipeline.stages_for(_d(score=1), files_changed=1),
            [1, 2],
        )

    def test_blast_radius_overrides_the_trivial_bypass(self):
        """4.6: measured blast radius beats the classifier's guess from
        prompt text. A score-1 task touching more than MAX_FILES_TRIVIAL
        files gets the full pipeline."""
        self.assertEqual(
            pipeline.stages_for(
                _d(score=1), files_changed=pipeline.MAX_FILES_TRIVIAL + 1
            ),
            [1, 2, 3, 4, 5],
        )

    def test_a_read_only_task_runs_one_to_three(self):
        """4.7: stages 4 and 5 check consequences of changing something, and
        a task that changes nothing cannot produce them."""
        self.assertEqual(
            pipeline.stages_for(
                _d(score=3, mutates=dc.MUTATES_FALSE), files_changed=0
            ),
            [1, 2, 3],
        )

    def test_a_trivial_read_keeps_stage_three(self):
        """The precedence case -- spec 4.8, not the naive reading of it.

        4.8's bypass table gives writes and reads DIFFERENT rows: a trivial
        write drops 3, 4 and 5; a trivial read drops only 4 and 5, because
        4.7 already established stage 3 as the read's one real gate and 4.8
        says explicitly that no rule may remove it ("stage 3 restored if any
        earlier rule took it"). So a score-1 read runs [1, 2, 3], the same
        as a non-trivial read -- the trivial bypass has no visible effect on
        a read at all. An implementation that applies the trivial bypass by
        `mutates` alone (collapsing to [1, 2] whenever score <= 1, the same
        as the write case) passes every single-rule test in this file and
        fails only here.
        """
        self.assertEqual(
            pipeline.stages_for(
                _d(score=1, mutates=dc.MUTATES_FALSE), files_changed=0
            ),
            [1, 2, 3],
        )

    def test_side_effecting_read_takes_all_five(self):
        """4.7 covers mutates=False only. side_effecting_read spends money or
        consumes a rate limit, so it has real consequences to review."""
        self.assertEqual(
            pipeline.stages_for(
                _d(score=3, mutates=dc.MUTATES_SIDE_EFFECTING_READ),
                files_changed=0,
            ),
            [1, 2, 3, 4, 5],
        )

    def test_trivial_side_effecting_read_loses_stage_three_too(self):
        """Combination of the trivial bypass and the three-valued mutates
        check. side_effecting_read is not a read for 4.7's purposes (it
        takes the full five stages, same as True) and 4.8's bypass table has
        no read-shaped row for it -- it follows the write row. So a score-1
        side_effecting_read task runs [1, 2], same as a score-1 True task,
        and UNLIKE a score-1 False task (previous test). An implementation
        that grants the read-only stage-3 protection to anything that is not
        literally MUTATES_TRUE -- rather than to exactly MUTATES_FALSE --
        would keep stage 3 here and fail only this test.
        """
        self.assertEqual(
            pipeline.stages_for(
                _d(score=1, mutates=dc.MUTATES_SIDE_EFFECTING_READ),
                files_changed=0,
            ),
            [1, 2],
        )

    def test_blast_radius_never_fires_on_a_read(self):
        """Its blast radius is zero by definition, so the override exists
        only for writes misclassified as trivial. Forcing a large
        files_changed on a read must not change the outcome from the
        trivial-read case above: still [1, 2, 3], not the full five (a bug
        that lets an over-threshold blast radius force stages 4 and 5 back
        on regardless of mutates) and not [1, 2] (the naive trivial-bypass
        collapse, again)."""
        self.assertEqual(
            pipeline.stages_for(
                _d(score=1, mutates=dc.MUTATES_FALSE), files_changed=99
            ),
            [1, 2, 3],
        )


if __name__ == "__main__":
    unittest.main()
