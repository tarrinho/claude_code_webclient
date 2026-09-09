"""QA: seeding the Anthropic backend must survive a schema migration.

`ai_machine_seed_anthropic` is called on every Backends panel load, so it has
to be idempotent. It was — against itself. What it was not idempotent across
was `db.init()`, and that is the cycle a real deployment actually runs:

    init()  ->  UPDATE ai_machines SET provider='claude_code'
                            WHERE provider='anthropic'      (db.py, every boot)
    seed()  ->  SELECT ... WHERE provider='anthropic'        (found nothing)
             -> INSERT another "Anthropic API"

One new row per restart-then-open cycle. Seven had accumulated on this
deployment before anyone noticed, because they are all inactive with no
declared models, so nothing routes through them and nothing breaks — the only
symptom is a Backends list filling up with identical entries.

The existing coverage in test_machine_provider.py calls seed() twice in a row
and asserts one row, which passes both before and after the bug. The migration
between the two calls is the entire failure, so that is what these tests put
there.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.testing_model import TESTING_MODEL


class _SeedFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import importlib
        import os

        self.tmp = Path(tempfile.mkdtemp(prefix="wc-seed-qa-")) / "wc.db"
        os.environ["WC_DB_PATH"] = str(self.tmp)
        os.environ.setdefault("WC_PROXY_ENABLED", "0")
        import config

        importlib.reload(config)
        import db

        db.config = config
        self.db = db
        await db.init()

    async def asyncTearDown(self):
        await self.db.close()

    def _count(self) -> int:
        con = sqlite3.connect(str(self.tmp))
        try:
            return con.execute(
                "SELECT count(*) FROM ai_machines WHERE host = 'api.anthropic.com'"
            ).fetchone()[0]
        finally:
            con.close()

    async def _reinit(self):
        """A restart: close, re-init, which re-runs every migration."""
        await self.db.close()
        await self.db.init()


class SeedSurvivesTheProviderMigrationTests(_SeedFixture):
    async def test_seeding_twice_across_a_migration_creates_one_machine(self):
        """The regression, in the order a deployment performs it."""
        first = await self.db.ai_machine_seed_anthropic("admin")
        self.assertEqual(self._count(), 1)
        await self._reinit()
        second = await self.db.ai_machine_seed_anthropic("admin")
        self.assertEqual(
            self._count(), 1,
            "db.init() rewrites the provider column, so a guard that keys on "
            "the old provider literal stops recognising its own row",
        )
        self.assertEqual(first, second, "the same machine must be returned")

    async def test_many_restart_cycles_still_leave_one(self):
        """Seven accumulated in production; one cycle passing is not enough."""
        ids = set()
        for _ in range(5):
            ids.add(await self.db.ai_machine_seed_anthropic("admin"))
            await self._reinit()
        self.assertEqual(self._count(), 1)
        self.assertEqual(len(ids), 1, f"expected one machine id, got {ids}")

    async def test_the_seeded_provider_is_one_the_api_accepts(self):
        """It wrote provider='anthropic', which validation no longer allows."""
        from routes.machines import _MACHINE_PROVIDERS

        machine_id = await self.db.ai_machine_seed_anthropic("admin")
        machine = await self.db.ai_machine_get(machine_id, "admin")
        self.assertIn(
            machine["provider"], _MACHINE_PROVIDERS,
            f"seeded provider {machine['provider']!r} is not in "
            f"{_MACHINE_PROVIDERS} -- the row cannot be edited through the API",
        )

    async def test_an_existing_anthropic_backend_is_reused_not_duplicated(self):
        """A backend the owner already pointed at api.anthropic.com satisfies
        this function, whatever they named it."""
        await self.db.ai_machine_create(
            "mine", "My Anthropic", "api.anthropic.com", 443, None,
            TESTING_MODEL, "https://api.anthropic.com", None, "admin",
            provider="claude_code",
        )
        returned = await self.db.ai_machine_seed_anthropic("admin")
        self.assertEqual(returned, "mine")
        self.assertEqual(self._count(), 1)

    async def test_seeding_is_per_owner(self):
        """Scoping is the one thing the original guard got right."""
        await self.db.ai_machine_seed_anthropic("admin")
        await self.db.ai_machine_seed_anthropic("someone-else")
        self.assertEqual(self._count(), 2)


if __name__ == "__main__":
    unittest.main()
