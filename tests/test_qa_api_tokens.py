"""QA: API tokens, and the removal of the `/dev/*` authentication exemption.

These two are one change. `AuthMiddleware` used to treat every path under
`/dev/` as public, and the one endpoint that lived there minted an admin session
with no credential and returned its id to anybody who sent a GET. That is not a
feature anybody chose -- it was debug scaffolding swept into a commit by a
whole-file `git commit` -- but the reason it survived is worth keeping in mind
while reading the tests below: a caller that cannot hold a cookie had no
supported way in, so an unsupported one kept being invented.

So the exemption is gone and a real credential replaces it. The tests split
along that seam:

* nothing is exempt from authentication any more, asserted through the live
  middleware stack rather than by reading the source;
* a token authenticates, carries its owner's identity and role, and stops
  working the moment it is revoked or expires;
* a token-authenticated mutating request does not need CSRF -- and, the part
  that actually needs guarding, a *cookie*-authenticated one still does, even
  when the caller also sends a junk token header.

That last case is the one to read twice. The CSRF exemption keys on what the
auth middleware decided, not on the presence of a header. Keying on the header
would let any unauthenticated request switch CSRF off by sending a made-up
token, which is the same class of mistake as the `/dev/` prefix: a convenience
that quietly becomes the bypass.

The harness detail matters as much here as in test_qa_question_access.py: the
session cookie is `Secure`, httpx will not send a Secure cookie to an `http://`
URL, and TestClient defaults to `http://testserver` -- so a suite written that
way logs in, receives the cookie and is anonymous for every request afterwards.
`base_url="https://testserver"` is what makes the cookie half of these tests
mean anything.
"""
from __future__ import annotations

import datetime
import re
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db

HTTPS = "https://testserver"
ROOT = Path(__file__).resolve().parent.parent


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


def _stamp(days: int) -> str:
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class ApiTokenBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.passwords = {
            "alice": secrets.token_urlsafe(16),
            "bob": secrets.token_urlsafe(16),
        }
        await db.user_create("alice", None, auth.hash_password(self.passwords["alice"]))
        await db.user_create(
            "bob", None, auth.hash_password(self.passwords["bob"]), role="user"
        )
        await db.chat_create("c-alice", "Alice's", None, f"{self.tmp.name}/p", "alice")
        # Cleared between tests: the touch throttle is process-global, so a
        # token id reused across tests would silently skip its own write.
        import app
        app._token_touched.clear()

    async def _mint(self, user="alice", role="admin", name="test",
                    expires_at=None) -> str:
        """Create a token directly and return the secret."""
        token_id, secret, token_hash = auth.new_api_token()
        await db.api_token_create(token_id, name, token_hash, user, role, expires_at)
        self.last_token_id = token_id
        return secret

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}


class DevExemptionIsGoneTests(ApiTokenBase):
    """Nothing is exempt from authentication except login and static assets."""

    def test_a_dev_path_is_no_longer_public(self):
        """Refused by the middleware, which runs ahead of routing.

        `follow_redirects=False` is load-bearing. An unauthenticated request to
        a non-API path is refused with `303 -> /login`, and httpx follows that
        by default and reports the login page's 200 -- so a version of this test
        without the flag reads "200" and asserts nothing about whether the path
        was ever public. A 404 here would be the failure to watch for: it would
        mean the request reached the router, so the prefix is still exempt and
        the next route added under it is reachable by anyone.
        """
        response = _client(follow_redirects=False).get("/dev/supervisor-trigger")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_no_arbitrary_dev_path_is_public(self):
        client = _client(follow_redirects=False)
        for path in ("/dev/", "/dev/anything", "/dev/trigger?x=1"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 303, path)
                self.assertEqual(response.headers["location"], "/login")

    def test_a_dev_path_under_api_is_refused_with_401(self):
        """The API branch of the same check, where the refusal is a status code
        rather than a redirect."""
        response = _client(follow_redirects=False).get("/api/dev/anything")
        self.assertEqual(response.status_code, 401)

    def test_the_source_no_longer_exempts_the_prefix(self):
        """Belt to the braces above. The behavioural tests would also pass if
        `/dev/` were exempt but no route matched -- FastAPI would 404 and the
        middleware never speak -- so the source is checked too."""
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('startswith("/dev/")', source)

    def test_only_login_and_assets_are_public(self):
        """Pins the whole public set, not just the prefix that was removed.
        A future addition to this list should have to change a test."""
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        block = source.split("public_route = ")[1].split("if not public_route")[0]
        self.assertIn('== "/login"', block)
        self.assertIn('startswith(\n            "/assets/"', block)
        # Nothing else: any other startswith in that expression is a new
        # exemption and should be read by a person.
        self.assertEqual(block.count("startswith"), 1, block)


class TokenAuthenticationTests(ApiTokenBase):
    """A token gets in, and stops getting in when it should."""

    async def test_a_valid_token_authenticates_a_read(self):
        secret = await self._mint()
        response = _client().get(
            "/api/chats", headers={"Authorization": f"Bearer {secret}"}
        )
        self.assertEqual(response.status_code, 200)
        titles = [c["title"] for c in response.json()["chats"]]
        self.assertEqual(titles, ["Alice's"],
                         "the token must act as its owner, not as nobody")

    async def test_the_x_api_token_header_works_too(self):
        secret = await self._mint()
        response = _client().get("/api/chats", headers={"X-API-Token": secret})
        self.assertEqual(response.status_code, 200)

    async def test_an_unknown_token_is_refused(self):
        _token_id, secret, _hash = auth.new_api_token()  # never stored
        response = _client().get(
            "/api/chats", headers={"Authorization": f"Bearer {secret}"}
        )
        self.assertEqual(response.status_code, 401)

    async def test_a_malformed_token_is_refused(self):
        for bad in ("", "Bearer", "not-a-token", "wct_short.x", "wct_zzzzzzzzzzzz.x"):
            with self.subTest(token=bad):
                response = _client().get(
                    "/api/chats", headers={"Authorization": f"Bearer {bad}"}
                )
                self.assertEqual(response.status_code, 401)

    async def test_a_revoked_token_stops_working_immediately(self):
        secret = await self._mint()
        headers = {"Authorization": f"Bearer {secret}"}
        self.assertEqual(_client().get("/api/chats", headers=headers).status_code, 200)
        self.assertTrue(await db.api_token_revoke(self.last_token_id, "alice"))
        self.assertEqual(_client().get("/api/chats", headers=headers).status_code, 401)

    async def test_an_expired_token_is_refused(self):
        secret = await self._mint(expires_at=_stamp(-1))
        response = _client().get(
            "/api/chats", headers={"Authorization": f"Bearer {secret}"}
        )
        self.assertEqual(response.status_code, 401)

    async def test_a_token_expiring_later_still_works(self):
        """The other half of the expiry comparison. Without it a bug that
        treated every `expires_at` as past would pass the test above."""
        secret = await self._mint(expires_at=_stamp(1))
        response = _client().get(
            "/api/chats", headers={"Authorization": f"Bearer {secret}"}
        )
        self.assertEqual(response.status_code, 200)

    async def test_the_role_comes_from_the_token_not_the_request(self):
        """A non-admin token must not reach an admin route. The role travels
        with the credential, so a token minted for a `user` cannot be widened by
        the caller."""
        secret = await self._mint(user="bob", role="user")
        response = _client().patch(
            "/api/settings", json={"turn_timeout": 120},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(response.status_code, 403)

    async def test_an_admin_token_reaches_an_admin_route(self):
        secret = await self._mint(user="alice", role="admin")
        response = _client().patch(
            "/api/settings", json={"turn_timeout": 120},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(response.status_code, 200)

    async def test_use_is_recorded(self):
        secret = await self._mint()
        _client().get("/api/chats", headers={"Authorization": f"Bearer {secret}"})
        rows = await db.api_token_list("alice")
        self.assertIsNotNone(rows[0]["last_used_at"],
                             "a credential nobody can tell is in use cannot be "
                             "retired with any confidence")


class TokenAndCsrfTests(ApiTokenBase):
    """The exemption that makes tokens usable, and its exact boundary."""

    async def test_a_token_can_mutate_without_a_csrf_header(self):
        """The point of the whole feature: a script has no cookie to
        double-submit with."""
        secret = await self._mint()
        response = _client().post(
            "/api/chats", json={"title": "Made by a script"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["title"], "Made by a script")

    def test_a_cookie_session_still_needs_csrf(self):
        """No regression. The exemption must not have widened into "mutating
        requests no longer need CSRF"."""
        client, _headers = self._login("alice")
        response = client.post("/api/chats", json={"title": "no token here"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", response.json()["error"])

    def test_a_junk_token_header_does_not_switch_csrf_off(self):
        """The attack the implementation is shaped to refuse.

        A page that can make the browser send its cookies cannot read them, so
        it cannot produce the CSRF header -- unless it can make the server stop
        asking. Sending an invented `Authorization` header is free. If the CSRF
        check keyed on the presence of a token header rather than on the auth
        middleware's decision, this request would go through on the strength of
        a credential that was never accepted.
        """
        client, _headers = self._login("alice")
        _id, unaccepted, _hash = auth.new_api_token()
        response = client.post(
            "/api/chats", json={"title": "forged"},
            headers={"Authorization": f"Bearer {unaccepted}"},
        )
        self.assertEqual(response.status_code, 403)

    async def test_a_valid_token_beside_a_session_still_needs_csrf(self):
        """Cookie authentication wins when both are presented, so the request is
        a browser request and is guarded as one. Otherwise a token leaked into a
        page's JavaScript would disable CSRF for that whole session."""
        secret = await self._mint()
        client, _headers = self._login("alice")
        response = client.post(
            "/api/chats", json={"title": "both credentials"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(response.status_code, 403)


class TokenManagementApiTests(ApiTokenBase):
    """Creating, listing and revoking through HTTP."""

    def test_creation_returns_the_secret_exactly_once(self):
        client, headers = self._login("alice")
        created = client.post("/api/tokens", json={"name": "cron"}, headers=headers)
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        secret = body["token"]
        self.assertTrue(secret.startswith(auth.API_TOKEN_PREFIX))
        self.assertIn("only time", body["note"])

        listed = client.get("/api/tokens").json()["tokens"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], body["id"])
        blob = str(listed)
        self.assertNotIn(secret, blob, "the list must never carry the secret")
        self.assertNotIn("token_hash", blob,
                         "nor the hash: publishing it turns an authenticated "
                         "read into an offline target")

    def test_the_created_token_actually_works(self):
        """A token the API hands out and the API then refuses would be a
        convincing-looking feature that does nothing."""
        client, headers = self._login("alice")
        secret = client.post(
            "/api/tokens", json={"name": "cron"}, headers=headers
        ).json()["token"]
        response = _client().get(
            "/api/chats", headers={"Authorization": f"Bearer {secret}"}
        )
        self.assertEqual(response.status_code, 200)

    def test_an_expiry_can_be_requested_and_is_bounded(self):
        client, headers = self._login("alice")
        ok = client.post("/api/tokens", json={"name": "short", "expires_in_days": 1},
                         headers=headers)
        self.assertEqual(ok.status_code, 200)
        self.assertIsNotNone(ok.json()["expires_at"])
        for bad in (0, -1, 4000, "soon"):
            with self.subTest(days=bad):
                response = client.post(
                    "/api/tokens", json={"name": "x", "expires_in_days": bad},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400)

    async def test_a_token_cannot_mint_another_token(self):
        """Otherwise one leaked credential becomes an unrevocable supply: revoke
        the one you know about and the tokens it created keep working."""
        secret = await self._mint()
        response = _client().post(
            "/api/tokens", json={"name": "child"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("logged-in session", response.json()["error"])

    async def test_a_token_may_revoke_itself(self):
        """Deliberately allowed where creation is not: needing a browser to
        retire a credential you think is loose is the wrong way round."""
        secret = await self._mint()
        response = _client().delete(
            f"/api/tokens/{self.last_token_id}",
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_client().get(
            "/api/chats", headers={"Authorization": f"Bearer {secret}"}
        ).status_code, 401)

    async def test_one_account_cannot_revoke_anothers_token(self):
        secret = await self._mint(user="bob", role="user")
        bobs_id = self.last_token_id
        client, headers = self._login("alice")
        response = client.delete(f"/api/tokens/{bobs_id}", headers=headers)
        self.assertEqual(response.status_code, 404)
        # And Bob's token still works, which is the half that would go unnoticed.
        self.assertEqual(_client().get(
            "/api/chats", headers={"Authorization": f"Bearer {secret}"}
        ).status_code, 200)

    async def test_one_account_cannot_see_anothers_tokens(self):
        await self._mint(user="bob", role="user", name="bob's")
        client, _headers = self._login("alice")
        listed = client.get("/api/tokens").json()["tokens"]
        self.assertEqual(listed, [])

    def test_listing_requires_authentication(self):
        self.assertEqual(_client().get("/api/tokens").status_code, 401)


class TokenStorageTests(ApiTokenBase):
    """What the database is left holding."""

    async def test_only_a_hash_is_stored(self):
        secret = await self._mint()
        con = sqlite3.connect(f"{self.tmp.name}/db")
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM api_tokens")]
        con.close()
        self.assertEqual(len(rows), 1)
        blob = str(rows)
        self.assertNotIn(secret, blob,
                         "a plaintext token here makes every /api/admin/export "
                         "backup a set of working keys")
        self.assertEqual(rows[0]["token_hash"], auth.hash_api_token(secret))
        self.assertRegex(rows[0]["token_hash"], r"^[0-9a-f]{64}$")

    async def test_the_id_is_public_and_the_secret_is_not(self):
        """The id is printed in logs and shown in lists, so it must not be
        enough to authenticate with on its own."""
        secret = await self._mint()
        token_id = self.last_token_id
        self.assertTrue(secret.startswith(token_id + "."))
        response = _client().get(
            "/api/chats", headers={"Authorization": f"Bearer {token_id}"}
        )
        self.assertEqual(response.status_code, 401)

    def test_secrets_are_not_reused(self):
        seen = {auth.new_api_token()[1] for _ in range(50)}
        self.assertEqual(len(seen), 50)

    def test_the_secret_carries_enough_entropy_to_not_be_guessed(self):
        """The hash is fast on purpose, which is only safe while the secret is
        large. If the token ever shrinks, this is the assertion that objects."""
        _id, secret, _hash = auth.new_api_token()
        random_part = secret.split(".", 1)[1]
        self.assertGreaterEqual(len(random_part), 40, random_part)
        self.assertRegex(random_part, r"^[A-Za-z0-9_-]+$")


class TokenCliTests(unittest.TestCase):
    """The CLI exists to break the bootstrap circle; check it stays that way."""

    def test_it_does_not_print_the_secret_by_default(self):
        """A credential on stdout is a credential in scrollback, in CI logs and
        in any transcript being kept. The file path and the id are what a person
        needs; the secret is what they must not have to scroll past."""
        source = (ROOT / "bin" / "wc-token.py").read_text(encoding="utf-8")
        create = source.split("async def _create(")[1].split("\nasync def ")[0]
        printed = re.findall(r"print\((.*)\)", create)
        self.assertTrue(printed, create)
        for call in printed:
            if "secret" in call:
                self.assertIn("args.stdout", create,
                              "printing the secret must be opt-in")
        self.assertIn("0o600", create, "the file must be created private")
        self.assertIn("os.open", create,
                      "mode at creation, not chmod afterwards -- otherwise the "
                      "secret is world-readable for the gap between the calls")


if __name__ == "__main__":
    unittest.main()
