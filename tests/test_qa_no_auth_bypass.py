"""QA: only logging in may mint a session.

A `/dev/supervisor-trigger` route reached production. It was a GET, so the CSRF
middleware did not apply to it -- that guards {POST, PUT, PATCH, DELETE} -- it
required no credential, and it called `auth.session_new("admin", "admin")` and
returned the session id in the response body. Anyone who could reach the host
had admin without seeing the login page, and any page open in the operator's
browser could fire the request cross-origin: unreadable there, but the session
was still minted and a supervisor run still started.

It was labelled "temporary, remove when done" and had been live for hours.
ruff had flagged RUF059 on its very line -- `csrf` unpacked and never used --
and that was filed as lint noise, because the warning's category did not match
the severity of what it was pointing at.

These tests are about the shape rather than that one route: session minting
belongs to the login path, and a debug route must not reach a running server.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
# app.py plus the modules the 0.10.0 split moved its guards into. The CSRF
# and auth middleware now live in middleware.py, so a scan of app.py alone
# searches a file that no longer contains the thing being asserted -- and
# an absent string reads as "the guard is gone" rather than "it moved".
SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in (APP, APP.parent / "middleware.py",
                 APP.parent / "net_validation.py",
                 APP.parent / "classification.py")
    if path.is_file()
)
TREE = ast.parse(SOURCE)

# Where minting a session is legitimate: the login handler, and the bootstrap
# that creates the first admin. Both are named here so adding a third is a
# deliberate act with a test change attached, not a quiet edit.
SESSION_MINTERS = {"handle_login", "_api_login"}


def route_functions():
    """(decorator path, function node) for everything mounted on the app."""
    found = []
    for node in ast.walk(TREE):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            func = call.func if call else dec
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "app"):
                path = ""
                if call and call.args and isinstance(call.args[0], ast.Constant):
                    path = str(call.args[0].value)
                found.append((path, node))
    return found


class NoDebugRoutesTests(unittest.TestCase):
    def test_there_are_routes_to_check(self):
        """A find-nothing bug here would make every assertion below vacuous."""
        self.assertGreater(len(route_functions()), 20)

    def test_no_route_is_mounted_under_dev(self):
        offenders = [p for p, _ in route_functions() if p.startswith("/dev")]
        self.assertEqual(offenders, [],
                         "a debug route is mounted on the running application")

    def test_no_route_path_advertises_itself_as_temporary(self):
        for path, node in route_functions():
            with self.subTest(path=path):
                self.assertNotIn("debug", path.lower())
                self.assertNotIn("trigger", path.lower())


class OnlyLoginMintsASessionTests(unittest.TestCase):
    """The invariant the dev route broke, stated directly."""

    def _minting_functions(self):
        callers = set()
        for node in ast.walk(TREE):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                func = inner.func
                if (isinstance(func, ast.Attribute)
                        and func.attr == "session_new"):
                    callers.add(node.name)
        return callers

    def test_session_new_is_called_somewhere(self):
        """Guards the test: logging in must still mint a session."""
        self.assertTrue(self._minting_functions(),
                        "nothing mints a session; login cannot work")

    def test_only_the_login_path_mints_a_session(self):
        unexpected = self._minting_functions() - SESSION_MINTERS
        self.assertEqual(
            unexpected, set(),
            "a function outside the login path mints a session: any route "
            "reaching it hands out credentials without a password",
        )

    def test_no_handler_returns_a_session_id_in_its_body(self):
        """The id is a credential. It belongs in a cookie, not a payload."""
        offenders = re.findall(r'"session_id"\s*:\s*sid', SOURCE)
        self.assertEqual(offenders, [],
                         "a response body carries a session id")


class CsrfCoversWhatItLooksLikeItCoversTests(unittest.TestCase):
    """Why a GET was able to do this at all."""

    def test_get_is_deliberately_outside_the_csrf_guard(self):
        """Not a bug -- but it is why a GET must never have side effects.

        The guard covers the mutating verbs. That is correct, and it means a
        route which changes state behind a GET is unprotected by construction,
        which is exactly how the dev endpoint slipped past every check here.
        """
        self.assertIn('_MUTATING: ClassVar[set] = {"POST", "PUT", "PATCH", "DELETE"}',
                      SOURCE)


if __name__ == "__main__":
    unittest.main()
