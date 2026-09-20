"""Loop-bound asyncio primitives: the failure that only shows under contention.

`asyncio.Lock`, `Queue`, `Semaphore`, `Event` and `Condition` all use
`_LoopBoundMixin`. They bind to the first event loop that makes them **wait**,
and raise ``RuntimeError: ... is bound to a different event loop`` for every
later loop that does. An operation that never suspends never reaches
``_get_loop()``, so an uncontended acquire touches nothing and the object looks
perfectly healthy.

That asymmetry is the whole bug, and it is why this file is behavioural rather
than a source scan. Two independent checks missed it on the same evening:

* a probe that acquired an uncontended lock in three successive loops and
  printed three clean passes;
* a grep for module-scope ``= asyncio.X()`` assignments, which could not see
  ``db._db_write_lock`` at all because it starts as ``None`` and is built on
  first use.

The live instance was `db.write`. Its module-global lock bound to whichever
loop first contended on it; production runs one loop for the life of the
process so it never bit there, while every test gets a fresh loop, so the first
contended test bound the lock and later contended tests died with the
RuntimeError above. Three cases were failing in the suite before the fix.
See ``db._ensure_lock`` (re-created when the running loop changes) and the
comment on ``tunnel_manager._queue`` (the same shape, latent).

The discriminator that actually works, and the one every test here uses: make
the primitive genuinely suspend, twice, in two separate ``asyncio.run()``
calls. Anything less reports success against a broken object.
"""
from __future__ import annotations

import asyncio
import unittest

import db


def _run_contended(body) -> None:
    """Run *body* in its own fresh event loop, one loop per call.

    ``asyncio.run`` creates a new loop and closes it on the way out, which is
    what makes consecutive calls a faithful stand-in for consecutive tests --
    and what makes a primitive bound to the previous loop raise.
    """
    asyncio.run(body())


class WriteLockSurvivesANewEventLoopTests(unittest.TestCase):
    """Regression for the db.write lock binding itself to one loop for ever."""

    @staticmethod
    async def _contend_on_the_write_lock() -> None:
        """Force real contention, which is the only thing that binds the loop.

        Two tasks, and the second has to find the lock already held -- hence
        the sleep inside the critical section rather than a bare acquire.
        An uncontended acquire would prove nothing at all.
        """
        lock = await db._ensure_lock()

        async def hold() -> None:
            async with lock:
                await asyncio.sleep(0.01)

        await asyncio.gather(hold(), hold())

    def test_contending_in_a_second_loop_does_not_raise(self):
        """The failure this file exists for.

        Before the fix the first call bound the module-global lock and the
        second raised "is bound to a different event loop". Three loops rather
        than two because the binding is cached on first contention, so a fix
        that merely deferred the binding by one loop would pass a two-loop
        test.
        """
        for attempt in range(3):
            with self.subTest(loop=attempt):
                try:
                    _run_contended(self._contend_on_the_write_lock)
                except RuntimeError as exc:  # pragma: no cover - the bug
                    self.fail(
                        f"contending in loop {attempt} raised {exc!r}. The "
                        "write lock is bound to a loop that has since closed; "
                        "see db._ensure_lock."
                    )

    def test_the_lock_is_replaced_when_the_loop_changes(self):
        """The mechanism behind the test above, asserted directly.

        Pinned because the symptom and the cause can be fixed independently:
        catching the RuntimeError, or serialising through something that is not
        a lock, would both make the case above pass while leaving the object
        bound to a dead loop.
        """
        seen = []

        async def capture() -> None:
            await self._contend_on_the_write_lock()
            seen.append(db._db_write_lock)

        _run_contended(capture)
        _run_contended(capture)

        self.assertEqual(len(seen), 2)
        self.assertIsNot(
            seen[0], seen[1],
            "the same Lock object was reused across two event loops; it is "
            "bound to the first and can only raise in the second",
        )


class TheLockMustStillBeALockTests(unittest.TestCase):
    """The counterweight, and the reason it is here.

    "Re-create the lock when the loop changes" is one character away from
    "re-create the lock on every call", and that version passes every test in
    the class above while serialising nothing. These two cases are what stop a
    cross-loop fix from quietly deleting the mutual exclusion it was protecting.
    """

    def test_writes_do_not_interleave_within_one_loop(self):
        order: list[str] = []

        @db.write
        async def section(tag: str) -> None:
            order.append(f"{tag}-in")
            # Yields control. Without real mutual exclusion the other task runs
            # here and the halves interleave.
            await asyncio.sleep(0.01)
            order.append(f"{tag}-out")

        async def race() -> None:
            await asyncio.gather(section("x"), section("y"))

        _run_contended(race)

        self.assertIn(
            order,
            (["x-in", "x-out", "y-in", "y-out"],
             ["y-in", "y-out", "x-in", "x-out"]),
            f"writes interleaved: {order}. The lock is not excluding anything.",
        )

    def test_a_nested_write_does_not_deadlock(self):
        """Reentrancy, which the cross-loop fix must not cost.

        Several decorated functions call other decorated ones --
        chat_update -> _fts_rebuild, messages_append -> _fts_index_ids,
        messages_batch -> chats_reorder, queue_hold_orphans -> queue_counts,
        chat_routing -> ai_machine_backend. asyncio.Lock is not reentrant, so
        the inner acquire waited on the outer's hold and never returned; the
        lock was then never released and every later write and decorated read
        hung, including the one behind GET /api/chats.
        """
        @db.write
        async def inner() -> int:
            return 42

        @db.write
        async def outer() -> int:
            return await inner()

        async def both() -> list[int]:
            # Concurrently, so the nesting happens while the lock is contended
            # rather than only on a quiet path.
            return await asyncio.gather(outer(), outer())

        results: list[int] = []

        async def body() -> None:
            results.extend(await both())

        try:
            asyncio.run(asyncio.wait_for(_with_timeout(body), timeout=10))
        except asyncio.TimeoutError:  # pragma: no cover - the deadlock
            self.fail(
                "a nested db.write deadlocked: the inner acquire is waiting on "
                "the outer's hold. See the contextvar in db._write."
            )

        self.assertEqual(results, [42, 42])


async def _with_timeout(body) -> None:
    """Await *body* so ``wait_for`` has a coroutine to cancel.

    A deadlock here must fail as a timeout rather than hang the whole suite --
    an unbounded wait would take the run down with it, which is the failure
    mode this test is about in the first place.
    """
    await body()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
