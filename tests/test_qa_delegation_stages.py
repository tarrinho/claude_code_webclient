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
not as a boolean or a truthiness check: for a NON-trivial task,
MUTATES_SIDE_EFFECTING_READ must be refused the mutates=False exemption from
stages 4 and 5 (4.7 holds it to the same five stages as MUTATES_TRUE), where
a string-equality bug (or a bug that only distinguishes "== MUTATES_TRUE"
from "everything else") would let it slip through as if it were a read.
Under the TRIVIAL bypass, though, it takes the read row and keeps stage 3
(4.8) -- the bypass is scoped by whether an oracle can still verify the
output, not by the read/write alignment 4.7 uses for the non-trivial case,
so a trivial side_effecting_read is not "sliding through as a read" here,
it is the correct outcome. See
`test_trivial_side_effecting_read_keeps_stage_three` below.
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
        a read at all.

        This does NOT mean the test is sensitive to every way that guarantee
        could break. In `delegation_pipeline.stages_for`, the trivial-bypass
        read branch (`if is_read_only: stages -= {4, 5}`, scoped inside
        `trivial_bypass_applies`) and the later read-only `stages.add(3)`
        restore are mutually redundant against today's rule set: the trivial
        bypass never removes stage 3 from a read in the first place (only
        the write branch does), so the restore has nothing to restore on
        this path, and the read-only rule's own `stages -= {4, 5}` already
        does what the bypass's read branch does. Breaking either one alone
        -- collapsing the bypass to always take the write branch (dropping
        3, 4 and 5 regardless of `mutates`), or deleting the `stages.add(3)`
        restore -- leaves this test, and every other test in the repo,
        green; measured by brute-forcing all 126 `(mutates, score,
        files_changed-bucket)` combinations, neither mutation has a
        distinguishing input. This is deliberate defence in depth (spec
        4.8: the restore is phrased to hold "no matter what order a future
        rule is added in"), not a defect, so `delegation_pipeline.py` is not
        to be changed on this account.

        Breaking BOTH at once -- collapsing the bypass branch AND deleting
        the restore -- is caught, here and by
        `tests/test_qa_delegation_spec_coverage.py::ReadOnlyFloorTests::test_stage_three_appears_in_every_read_only_combination`,
        which enumerates every `(mutates, score, files_changed)` combination
        and is where the real coverage for this pair lives.
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

    def test_trivial_side_effecting_read_keeps_stage_three(self):
        """Combination of the trivial bypass and the three-valued mutates
        check -- and the case that reverses an earlier, wrong ruling of
        2026-09-16.

        That ruling read 4.7's "side_effecting_read takes the same five
        stages as True" as license to also put it on 4.8's write row,
        stripping stage 3 under the trivial bypass. That is wrong, and 4.8
        already says why: the bypass may drop the reviewer gate only where
        an oracle can still check the output. A trivial write's output is
        code stage 2 can execute. A trivial side_effecting_read's output may
        be prose, which stage 2 cannot check at all (4.2, 4.7) -- so the old
        ruling ran stage 1, then a stage 2 that verifies nothing, then
        stopped, with no gate having checked the result at all. That is
        exactly the hole 4.8 exists to close, reproduced for the third
        value instead of closed by it.

        So a score-1 side_effecting_read task runs [1, 2, 3], the same as a
        score-1 False task (previous test), and UNLIKE a score-1 True task,
        which does lose stage 3 here because its output is verifiable code.
        An implementation that grants the read-only stage-3 protection to
        anything that is not literally MUTATES_TRUE -- rather than to
        exactly MUTATES_FALSE -- is the CORRECT one for this case; the old
        version of this test asserted the opposite and encoded the wrong
        ruling in its own name.
        """
        self.assertEqual(
            pipeline.stages_for(
                _d(score=1, mutates=dc.MUTATES_SIDE_EFFECTING_READ),
                files_changed=0,
            ),
            [1, 2, 3],
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
