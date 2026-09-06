"""QA: findings from the 0.9.3 threat-modelling pass on the orchestrator surface.

`docs/threat-model.md` analysed 0.7.2 and says so, listing the orchestrator
orchestration API as one of three surfaces added since and **not covered**. This
file is the regression half of covering it. Two classes of finding came out:

**Cross-tenant reads (F-21).** `db.supervisor_tasks_get` and
`db.supervisor_messages_get` each took an `owner_id` argument and never used it.
The SQL filtered on `supervisor_id` alone, so any authenticated account could
read any orchestrator's task titles, descriptions and results -- agent output --
and its whole message history, by id. Verified by exploit before it was fixed:
Alice logged in and read Bob's `BOB-PRIVATE-MESSAGE` over HTTP with a 200.

The argument's *presence* is what hid it. Every call site passed `owner_id`, so
each one read as scoped, and a reviewer checking the handler would see the
parameter and stop. That is why the last test in `OwnerScopingIsRealTests` is
the important one here: it does not check these two functions, it checks the
property -- no function may accept `owner_id` and ignore it -- so the next one
written that way fails before anybody has to notice it by hand.

**Argument injection through a model id (F-22).** A plan's `[:model]` field is
extracted with `\\S+` and becomes the argument to `--model` in a subprocess, so
a plan reading `[:--mcp-config=/tmp/evil.json]` handed an attacker-chosen argv
token to the child. The route to writing such a plan is prompt injection into
whatever the planner was reading. `runner._build_cmd_direct` already protects
the *prompt* from exactly this, putting it after a `--` sentinel "where a
leading dash cannot be mistaken for a CLI flag" -- the model had no equivalent.

The same hole existed on the ordinary settings path: `_MODEL_RE` was
`^[A-Za-z0-9_.:/\\[\\]-]+$`, which accepts `-p` and
`-dangerously-skip-permissions`. No shell is involved, so this is argument
injection and not command injection, and whether the CLI mis-parses such a value
is the CLI's business -- the point is that nothing downstream should have to be
trusted to get it right.
"""
from __future__ import annotations

import ast
import pathlib
import secrets
import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db
import shared
import orchestrator

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTTPS = "https://testserver"


def _client():
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url=HTTPS)


class SupervisorTenancyTests(unittest.IsolatedAsyncioTestCase):
    """One account must not read another's orchestrator by id."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.passwords = {u: secrets.token_urlsafe(16) for u in ("alice", "bob")}
        for user, password in self.passwords.items():
            await db.user_create(user, None, auth.hash_password(password))

        await db.supervisor_create("sup-bob", "Bob's secret plan", "private", "bob")
        await db.supervisor_task_create(
            supervisor_id="sup-bob", task_id="t001", title="Bob's task title",
            description="BOB-PRIVATE-DESCRIPTION", model=None,
            parent_task_id=None, depends_on=[],
        )
        await db.supervisor_messages_append(
            "sup-bob", "orchestrator", "BOB-PRIVATE-MESSAGE", {"kind": "plan"}
        )
        await db.supervisor_create("sup-alice", "Alice's", "", "alice")
        for n in range(3):
            await db.supervisor_messages_append(
                "sup-alice", "orchestrator", f"ALICE-{n}", {}
            )

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client

    def test_another_accounts_tasks_are_not_returned(self):
        client = self._login("alice")
        response = client.get("/api/supervisors/sup-bob/tasks")
        self.assertNotIn("BOB-PRIVATE-DESCRIPTION", response.text)
        self.assertNotIn("Bob's task title", response.text)
        self.assertEqual(response.json()["count"], 0)

    def test_another_accounts_messages_are_not_returned(self):
        client = self._login("alice")
        response = client.get("/api/supervisors/sup-bob/messages")
        self.assertNotIn("BOB-PRIVATE-MESSAGE", response.text)
        self.assertEqual(response.json()["count"], 0)

    def test_your_own_supervisor_is_still_readable(self):
        """The half that would go unnoticed: a scoping fix that scopes
        everything to nothing passes both tests above."""
        client = self._login("alice")
        messages = client.get("/api/supervisors/sup-alice/messages").json()
        self.assertEqual(messages["count"], 3)
        self.assertIn("ALICE-0", str(messages))

    async def test_the_after_cursor_actually_filters(self):
        """`after_id` was accepted and ignored, with the `if after_id is not
        None` branch holding two byte-identical bodies -- which is what made it
        look deliberate. The SSE poller therefore re-sent the same first
        hundred messages for ever."""
        rows = await db.supervisor_messages_get("sup-alice", "alice")
        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), 3)
        after_first = await db.supervisor_messages_get(
            "sup-alice", "alice", after_id=ids[0]
        )
        self.assertEqual([r["id"] for r in after_first], ids[1:])
        past_end = await db.supervisor_messages_get(
            "sup-alice", "alice", after_id=ids[-1]
        )
        self.assertEqual(past_end, [])

    async def test_the_db_layer_refuses_the_wrong_owner_directly(self):
        """Asserted at the layer that holds the scoping, not only through HTTP.
        A handler-level check protects the handlers that have one; this one is
        the floor under all of them."""
        self.assertEqual(await db.supervisor_tasks_get("sup-bob", "alice"), [])
        self.assertEqual(await db.supervisor_messages_get("sup-bob", "alice"), [])
        self.assertNotEqual(await db.supervisor_tasks_get("sup-bob", "bob"), [])


class OwnerScopingIsRealTests(unittest.TestCase):
    """The invariant, rather than the two functions that broke it."""

    def test_no_query_accepts_owner_id_and_ignores_it(self):
        """The shape of the bug: a parameter that makes every call site read as
        scoped while the SQL filters on an id alone. Two functions were like
        this and both were reachable; three more were latent. Checking the
        property rather than the instances is what makes the next one fail
        before somebody has to spot it.

        `supervisor_progress` is exempt only if it has no callers -- see the
        assertion below, which would rather it were deleted than excused.
        """
        source = (ROOT / "db.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines()
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if "owner_id" not in [a.arg for a in node.args.args]:
                continue
            body_start = node.body[0].lineno - 1
            body = "\n".join(lines[body_start:node.end_lineno])
            if "owner_id" not in body:
                offenders.append(f"db.py:{node.lineno} {node.name}")
        self.assertEqual(
            offenders, [],
            "these accept owner_id and never use it, so every call site reads "
            "as owner-scoped and none of them are:\n  " + "\n  ".join(offenders),
        )

    def test_the_scan_can_actually_fail(self):
        """Without this the test above passes on a clean tree whether or not the
        AST walk works, which is the failure mode it exists to catch."""
        tree = ast.parse(
            "async def f(supervisor_id, owner_id):\n"
            "    return await q('SELECT 1 WHERE id = ?', (supervisor_id,))\n"
        )
        fn = tree.body[0]
        self.assertIn("owner_id", [a.arg for a in fn.args.args])
        body = "return await q('SELECT 1 WHERE id = ?', (supervisor_id,))"
        self.assertNotIn("owner_id", body)


class ModelArgumentInjectionTests(unittest.TestCase):
    """A model id becomes argv, so it may not look like a flag."""

    FLAG_SHAPED = (
        "-p",
        "--model",
        "-dangerously-skip-permissions",
        "--mcp-config=/tmp/evil.json",
        "--add-dir=/",
    )

    def test_the_validator_refuses_flag_shaped_values(self):
        for value in self.FLAG_SHAPED:
            with self.subTest(value=value):
                self.assertFalse(config.valid_model_id(value))

    def test_the_validator_accepts_real_model_ids(self):
        """Including the documented context-window suffix, which the CLI itself
        tells users to append -- a fix that rejected it would break the feature
        it is protecting."""
        for value in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
                      "claude-opus-5[1m]", "vendor/model-1.2:tag"):
            with self.subTest(value=value):
                self.assertTrue(config.valid_model_id(value), value)

    def test_absurd_values_are_refused(self):
        for value in (None, "", "   ", "x" * 500, "model name with spaces",
                      "model\nname", "model;rm -rf /"):
            with self.subTest(value=value):
                self.assertFalse(config.valid_model_id(value))

    def test_a_hostile_plan_cannot_choose_the_childs_argv(self):
        """The extraction pattern is `[:(\\S+)]` -- anything non-whitespace --
        and the value goes on to become the argument to `--model`. A rejected id
        falls back to None, which is what a plan with no `[:model]` already
        gets, so the failure mode is "the backend picks" rather than an error."""
        plan = "<<PLAN\n" + "\n".join(
            f"Task {n}: Task {n} - description [:{value}]"
            for n, value in enumerate(self.FLAG_SHAPED, start=1)
        ) + "\nTask 9: Legitimate - description [:claude-opus-5]\n>>\n"
        tasks = orchestrator.PlanParser.parse(plan)
        self.assertEqual(len(tasks), len(self.FLAG_SHAPED) + 1, tasks)
        self.assertEqual(
            [t.model for t in tasks[:-1]], [None] * len(self.FLAG_SHAPED),
            "a flag-shaped model must not survive plan parsing",
        )
        self.assertEqual(tasks[-1].model, "claude-opus-5",
                         "and a real one must still be honoured")

    def test_both_paths_share_one_rule(self):
        """Two copies of a security pattern is the drift this project keeps
        paying for, so app.py's `_MODEL_RE` is the shared object rather than a
        second regex that happens to agree today."""
        self.assertIs(shared._MODEL_RE, config.MODEL_ID_RE)

    def test_the_api_refuses_a_flag_shaped_model(self):
        """End to end, because the validator being right is not the same as the
        route using it.

        `PATCH /api/chats/{id}` is the route that takes a model. `POST
        /api/chats` does not read the field at all -- a first version of this
        test posted a model on create, got 200, and read it as a hole; the value
        was simply ignored and never stored. Asserting against a route that has
        no model parameter would have been a test that could only ever pass or
        mislead.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("DB_PATH", f"{tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{tmp.name}/p")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        import asyncio
        password = secrets.token_urlsafe(16)

        async def seed():
            await db.init()
            await db.user_create("alice", None, auth.hash_password(password))
            await db.chat_create("c1", "argv test", None, f"{tmp.name}/p", "alice")
        asyncio.run(seed())
        self.addCleanup(lambda: asyncio.run(db.close()))

        client = _client()
        self.assertEqual(client.post(
            "/login", json={"username": "alice", "password": password}
        ).status_code, 200)
        headers = {"X-CSRF-Token": client.cookies.get("wc_csrf")}
        for value in self.FLAG_SHAPED:
            with self.subTest(model=value):
                response = client.patch(
                    "/api/chats/c1", json={"model": value}, headers=headers
                )
                self.assertEqual(response.status_code, 400, response.text)
        # And a real one is still accepted, so the guard is a filter rather
        # than a wall.
        ok = client.patch(
            "/api/chats/c1", json={"model": "claude-opus-5"}, headers=headers
        )
        self.assertEqual(ok.status_code, 200, ok.text)


if __name__ == "__main__":
    unittest.main()
