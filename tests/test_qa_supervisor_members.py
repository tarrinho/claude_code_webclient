"""QA coverage for supervising conversations and agents that already exist.

A supervisor could only run work it invented itself: it decomposes a plan into
a TaskGraph and runs each task as a throwaway headless worker, and the
``subtask_*`` ids it uses are labels rather than rows. Nothing linked a
supervisor to a conversation or to a running agent.

Members close that gap, and are deliberately decoupled from the task graph --
adding an agent must not imply handing it a task.

Two properties carry most of the risk and most of these tests:

* **Ownership.** An unchecked ``ref_id`` would pull another account's
  conversation into a supervisor the caller owns, exposing its title, preview
  and status through the members feed.
* **One definition of "stuck".** The feed reuses handle_supervisor's classifier
  rather than recomputing status. A second definition would agree with the
  sidebar only by coincidence and would drift the first time either changed, so
  the test asserts the two *agree* rather than pinning a literal.
"""
from __future__ import annotations

import json
import tempfile
import types
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
import config
import db

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def make_request(user="alice", role="admin", body=None):
    request = types.SimpleNamespace(
        method="GET",
        url=types.SimpleNamespace(path="/api/supervisors/x/members"),
        cookies={}, headers={},
        client=types.SimpleNamespace(host="127.0.0.1"),
        state=types.SimpleNamespace(session={"user": user, "role": role}),
        query_params={},
    )
    request.json = AsyncMock(return_value=body if body is not None else {})
    request.is_disconnected = AsyncMock(return_value=False)
    return request


def body_of(response):
    return json.loads(bytes(response.body))


class MembersBase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", self.tmp.name)
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.sup = await self._supervisor("alice", "Release 0.9")

    async def asyncTearDown(self):
        # Closed unconditionally. aiosqlite's connection runs on a non-daemon
        # thread, so a setup that raised before this point left the interpreter
        # hanging at exit rather than reporting the failure -- which is how a
        # wrong argument order cost 600 seconds instead of one traceback.
        try:
            await db.close()
        except Exception:  # noqa: BLE001,S110 -- must not mask the real failure
            pass
        self.root_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def _supervisor(self, owner, title):
        sid = uuid.uuid4().hex
        # (id, title, description, owner_id) -- config is last and optional.
        await db.supervisor_create(sid, title, "", owner)
        return sid

    async def _chat(self, owner="alice", title="a chat"):
        cid = uuid.uuid4().hex
        await db.chat_create(cid, title, "", f"{self.tmp.name}/w", owner)
        return cid


class MembershipTests(MembersBase):

    async def test_a_chat_can_be_added_and_listed(self):
        chat = await self._chat(title="cweb5")
        self.assertTrue(await db.supervisor_member_add(self.sup, chat))
        members = await db.supervisor_members_list(self.sup)
        self.assertEqual([m["chat_id"] for m in members], [chat])
        self.assertEqual(members[0]["title"], "cweb5")

    async def test_adding_twice_is_a_no_op(self):
        """The composite key is what makes the picker safe to press twice."""
        chat = await self._chat()
        self.assertTrue(await db.supervisor_member_add(self.sup, chat))
        self.assertFalse(await db.supervisor_member_add(self.sup, chat),
                         "a repeat add must report that nothing changed")
        self.assertEqual(len(await db.supervisor_members_list(self.sup)), 1)

    async def test_one_chat_can_serve_two_supervisors(self):
        """Many-to-many is intended: an agent can work for two supervisors."""
        other = await self._supervisor("alice", "Bug sweep")
        chat = await self._chat()
        await db.supervisor_member_add(self.sup, chat)
        await db.supervisor_member_add(other, chat)
        self.assertEqual(len(await db.supervisor_members_list(self.sup)), 1)
        self.assertEqual(len(await db.supervisor_members_list(other)), 1)

    async def test_removing_a_member_keeps_the_conversation(self):
        """A supervisor is a view over work, never its owner."""
        chat = await self._chat()
        await db.supervisor_member_add(self.sup, chat)
        self.assertTrue(await db.supervisor_member_remove(self.sup, chat))
        self.assertEqual(await db.supervisor_members_list(self.sup), [])
        self.assertIsNotNone(await db.chat_get(chat, "alice"),
                             "removing a member must never delete the chat")

    async def test_removing_a_non_member_reports_false(self):
        self.assertFalse(await db.supervisor_member_remove(self.sup, "nope"))

    async def test_a_deleted_conversation_stops_appearing(self):
        """The panel must not list a conversation that no longer exists."""
        chat = await self._chat()
        await db.supervisor_member_add(self.sup, chat)
        await db.chat_delete(chat, "alice")
        self.assertEqual(await db.supervisor_members_list(self.sup), [],
                         "a deleted chat must drop out of the members list")


class OwnershipTests(MembersBase):
    """The security boundary: a ref_id must belong to the caller."""

    async def test_another_owners_chat_is_refused(self):
        intruder = await self._chat(owner="bob", title="bob's private work")
        request = make_request(user="alice",
                               body={"members": [{"kind": "chat", "ref_id": intruder}]})
        response = await app.handle_supervisor_members_add(request, self.sup)
        payload = body_of(response)
        self.assertEqual(payload["added"], [],
                         "another owner's conversation must never become a member")
        self.assertEqual(len(payload["failed"]), 1)
        self.assertEqual(await db.supervisor_members_list(self.sup), [])

    async def test_a_supervisor_belonging_to_someone_else_is_not_found(self):
        theirs = await self._supervisor("bob", "bob's supervisor")
        chat = await self._chat(owner="alice")
        request = make_request(user="alice",
                               body={"members": [{"kind": "chat", "ref_id": chat}]})
        with self.assertRaises(HTTPException) as caught:
            await app.handle_supervisor_members_add(request, theirs)
        self.assertEqual(caught.exception.status_code, 404)

    async def test_listing_someone_elses_supervisor_is_not_found(self):
        theirs = await self._supervisor("bob", "bob's supervisor")
        with self.assertRaises(HTTPException) as caught:
            await app.handle_supervisor_members_get(make_request(user="alice"), theirs)
        self.assertEqual(caught.exception.status_code, 404)

    async def test_removing_from_someone_elses_supervisor_is_not_found(self):
        theirs = await self._supervisor("bob", "theirs")
        chat = await self._chat(owner="bob")
        await db.supervisor_member_add(theirs, chat)
        with self.assertRaises(HTTPException) as caught:
            await app.handle_supervisor_member_remove(
                make_request(user="alice"), theirs, chat)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(len(await db.supervisor_members_list(theirs)), 1,
                         "the real owner's membership must be untouched")


class BulkAddTests(MembersBase):

    async def test_one_bad_entry_does_not_lose_the_others(self):
        """Nine good agents and one dead one must leave nine members."""
        good = [await self._chat() for _ in range(3)]
        payload = {"members": [{"kind": "chat", "ref_id": c} for c in good]
                   + [{"kind": "chat", "ref_id": "does-not-exist"}]}
        response = await app.handle_supervisor_members_add(
            make_request(body=payload), self.sup)
        result = body_of(response)
        self.assertCountEqual(result["added"], good)
        self.assertEqual(len(result["failed"]), 1)

    async def test_an_empty_list_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            await app.handle_supervisor_members_add(
                make_request(body={"members": []}), self.sup)
        self.assertEqual(caught.exception.status_code, 400)

    async def test_an_unreasonable_batch_is_rejected(self):
        payload = {"members": [{"kind": "chat", "ref_id": str(i)} for i in range(101)]}
        with self.assertRaises(HTTPException) as caught:
            await app.handle_supervisor_members_add(
                make_request(body=payload), self.sup)
        self.assertEqual(caught.exception.status_code, 400)

    async def test_an_unknown_kind_is_refused(self):
        response = await app.handle_supervisor_members_add(
            make_request(body={"members": [{"kind": "wormhole", "ref_id": "x"}]}),
            self.sup)
        self.assertEqual(body_of(response)["added"], [])

    async def test_a_repeat_add_is_reported_separately_from_a_new_one(self):
        chat = await self._chat()
        payload = {"members": [{"kind": "chat", "ref_id": chat}]}
        first = body_of(await app.handle_supervisor_members_add(
            make_request(body=payload), self.sup))
        second = body_of(await app.handle_supervisor_members_add(
            make_request(body=payload), self.sup))
        self.assertEqual(first["added"], [chat])
        self.assertEqual(second["added"], [])
        self.assertEqual(second["already_members"], [chat])


class AdoptionTests(MembersBase):
    """A live agent becomes a chat on the way in, so there is one member type."""

    async def test_a_session_is_stored_as_the_chat_it_adopts_to(self):
        adopted_chat = await self._chat(title="cweb5")
        fake = types.SimpleNamespace(
            body=json.dumps({"id": adopted_chat, "title": "cweb5"}).encode())
        with patch.object(app, "handle_sessions_resume",
                          AsyncMock(return_value=fake)) as resume:
            response = await app.handle_supervisor_members_add(
                make_request(body={"members": [
                    {"kind": "session", "ref_id": "8e2e8bd0-1111-2222-3333-444455556666"}
                ]}), self.sup)
        resume.assert_awaited_once()
        self.assertEqual(body_of(response)["added"], [adopted_chat],
                         "the member must be the chat id, never the session id")
        stored = await db.supervisor_members_list(self.sup)
        self.assertEqual([m["chat_id"] for m in stored], [adopted_chat])

    async def test_an_agent_that_cannot_be_adopted_is_reported_not_stored(self):
        with patch.object(app, "handle_sessions_resume",
                          AsyncMock(side_effect=HTTPException(404, "no session"))):
            response = await app.handle_supervisor_members_add(
                make_request(body={"members": [{"kind": "session", "ref_id": "gone"}]}),
                self.sup)
        self.assertEqual(body_of(response)["added"], [])
        self.assertEqual(await db.supervisor_members_list(self.sup), [])


class StatusReuseTests(MembersBase):
    """The members feed and the sidebar must never disagree.

    These drive real conversations through the real classifier rather than
    mocking a feed, so what they assert is agreement between the two surfaces
    -- which is the property worth having. A test that pinned a literal string
    would keep passing while the two drifted apart.
    """

    async def _sidebar_status(self, chat_id):
        """What /api/supervisor says about this chat, for comparison."""
        feed = body_of(await app.handle_supervisor(make_request()))
        for bucket in ("waiting", "working", "updated"):
            for entry in feed[bucket]:
                if entry["kind"] == "chat" and entry["id"] == chat_id:
                    return entry["status"], entry.get("reason")
        return None, None

    async def _members(self):
        return body_of(await app.handle_supervisor_members_get(
            make_request(), self.sup))["members"]

    async def test_an_asking_member_agrees_with_the_sidebar(self):
        chat = await self._chat(title="cweb5")
        await db.messages_batch(chat, [("user", "go"), ("assistant", "Which one?")])
        await db.supervisor_member_add(self.sup, chat)

        sidebar_status, sidebar_reason = await self._sidebar_status(chat)
        member = (await self._members())[0]
        self.assertEqual(sidebar_status, "waiting", "fixture must actually ask")
        self.assertEqual(member["status"], sidebar_status,
                         "the two surfaces must not disagree about one chat")
        self.assertEqual(member["reason"], sidebar_reason)

    async def test_finished_work_agrees_with_the_sidebar(self):
        """Was `test_routine_output_agrees_with_the_sidebar`.

        Its fixture guard asserted the sidebar reported `updated` for
        "All done.", and that is no longer reachable: under the rule that an
        ended action is worth surfacing, a bare conversation whose newest message
        is the agent's is either an ask or `done`. **No message text produces
        `updated` for a chat with no linked session and no marks** -- the quiet
        bucket now holds only rows that are quiet for a *reason* (a linked
        session still busy, or a dismissal), not rows that are quiet by nature.

        So this is not repairable by choosing a different string, which is what
        it looks like from the failure. Renamed to the branch it now exercises;
        the property under test -- that the two surfaces never disagree about one
        chat -- is unchanged, and is the reason the test is worth keeping.
        """
        chat = await self._chat(title="chatty")
        await db.messages_batch(chat, [("user", "go"), ("assistant", "All done.")])
        await db.supervisor_member_add(self.sup, chat)

        sidebar_status, sidebar_reason = await self._sidebar_status(chat)
        self.assertEqual(sidebar_status, "waiting", "fixture must be finished work")
        self.assertEqual(sidebar_reason, "done", "and finished, not asking")
        member = (await self._members())[0]
        self.assertEqual(member["status"], sidebar_status,
                         "the two surfaces must not disagree about one chat")
        self.assertEqual(member["reason"], sidebar_reason)

    async def test_a_quiet_chat_agrees_with_the_sidebar(self):
        """The `updated` branch, reached the only way it still can be.

        Agreement has to hold on the quiet branch too, and that branch is now
        narrow enough to be worth pinning: a conversation whose linked terminal
        session is **still busy**. Its output is listed, but announcing it as
        finished would be wrong -- the work is happening in the terminal, where
        this process cannot see a turn.

        Two other routes were tried and neither reaches `updated`, recorded so
        the next reader does not repeat them:

        * A different message string -- there isn't one. No text produces
          `updated` for a bare chat any more.
        * A dismissal -- ``read_mark_set(dismiss=True)`` writes the same
          timestamp to ``read_at``, and the read check runs first and drops the
          row entirely, which is the documented contract that dismissing is also
          reading.
        """
        chat = await self._chat(title="busy-linked")
        await db.messages_batch(chat, [("user", "go"), ("assistant", "All done.")])
        await db.chat_set_session(chat, SESSION_ID)
        await db.supervisor_member_add(self.sup, chat)

        cli = [{
            "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb5",
            "kind": "interactive", "entrypoint": "", "live": True,
            "status": "busy", "status_updated_at": "2026-08-31T19:00:00Z",
        }]
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=cli)):
            sidebar_status, _ = await self._sidebar_status(chat)
            members = await self._members()

        self.assertEqual(sidebar_status, "updated", "fixture must be quiet")
        mine = [m for m in members if m["id"] == chat]
        self.assertTrue(mine, "the member vanished from the panel")
        self.assertEqual(mine[0]["status"], sidebar_status,
                         "the two surfaces must not disagree about one chat")

    async def test_a_quiet_member_is_listed_as_idle_not_dropped(self):
        """chat_last_activity only carries conversations that have some.

        Without this branch the panel would silently hide every member with
        nothing new, which is worse than showing nothing at all -- and the
        sidebar legitimately omits them, so agreement is the wrong rule here.
        """
        chat = await self._chat(title="quiet one")
        await db.supervisor_member_add(self.sup, chat)
        sidebar_status, _ = await self._sidebar_status(chat)
        self.assertIsNone(sidebar_status, "the sidebar omits a silent chat")

        members = await self._members()
        self.assertEqual(len(members), 1, "a quiet member must still be listed")
        self.assertEqual(members[0]["status"], "idle")
        self.assertEqual(members[0]["title"], "quiet one")

    async def test_a_non_member_is_not_listed(self):
        member = await self._chat(title="mine")
        outsider = await self._chat(title="not a member")
        for c in (member, outsider):
            await db.messages_batch(c, [("user", "go"), ("assistant", "done")])
        await db.supervisor_member_add(self.sup, member)
        self.assertEqual([m["title"] for m in await self._members()], ["mine"])

    async def test_a_failure_sorts_above_another_waiting_member(self):
        """A supervisor is opened to find what needs attention.

        The fixture is built so ONLY the failed-first rule can produce the
        expected order. Both members are "waiting", so the status key cannot
        separate them, and the failed one is given the LATER timestamp so the
        recency tie-break would put it second. The first version of this test
        compared a waiting member against a working one, where the status key
        alone gave the right answer -- deleting the failed-first rule left all
        23 tests green, which is exactly the kind of test that is worse than
        none.
        """
        broken = await self._chat(title="broken")
        asker = await self._chat(title="asker")
        await db.messages_batch(broken, [("user", "go"), ("assistant", "x")])
        await db.messages_batch(asker, [("user", "go"), ("assistant", "y")])
        for c in (asker, broken):
            await db.supervisor_member_add(self.sup, c)

        real = app.classify_chat

        def fake(chat, last, *args, **kwargs):
            entry = real(chat, last, *args, **kwargs)
            if not entry:
                return entry
            if chat["id"] == broken:
                # Later "since" on purpose: recency alone would rank it second.
                return {**entry, "status": "waiting", "reason": "failed",
                        "since": "2026-08-30T23:59:59Z"}
            if chat["id"] == asker:
                return {**entry, "status": "waiting", "reason": "asks",
                        "since": "2026-08-30T00:00:01Z"}
            return entry

        with patch.object(app, "classify_chat", fake):
            titles = [m["title"] for m in await self._members()]
        self.assertEqual(
            titles, ["broken", "asker"],
            "a failure must outrank another waiting member even when it is newer",
        )

    async def test_a_member_whose_chat_was_deleted_is_skipped_not_fatal(self):
        alive = await self._chat(title="alive")
        doomed = await self._chat(title="doomed")
        for c in (alive, doomed):
            await db.supervisor_member_add(self.sup, c)
        await db.chat_delete(doomed, "alice")
        titles = [m["title"] for m in await self._members()]
        self.assertEqual(titles, ["alive"],
                         "a deleted conversation must not break the panel")

    async def test_another_accounts_conversation_is_not_rendered(self):
        """The handler's skip for a chat it cannot resolve, which nothing
        reached until now.

        ``supervisor_members_list`` filters on ``deleted_at`` but not on owner,
        and it selects ``c.title`` from the join -- so a membership row naming
        another account's conversation yields that account's title. The owner
        lookup beside it (``chat_list``) does filter by owner, so the chat comes
        back None, which is the case the handler skips. Constructed at the DB
        layer on purpose: ``supervisor_member_add``'s own docstring says this
        layer stores what it is given and that the caller must check ownership,
        so this is the state that layer warns about rather than an impossible
        one. The write path does check, which makes the skip defence in depth --
        and untested defence in depth is how the deleted-chat branch above came
        to be believed without ever running.
        """
        mine = await self._chat(title="mine")
        theirs = await self._chat(owner="bob", title="bob's private title")
        for c in (mine, theirs):
            await db.supervisor_member_add(self.sup, c)
        titles = [m["title"] for m in await self._members()]
        self.assertEqual(titles, ["mine"])
        self.assertNotIn("bob's private title", titles)


if __name__ == "__main__":
    unittest.main()
