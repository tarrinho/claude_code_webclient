"""QA: two routers must not register the same method and path.

Starlette matches the first route whose path and method fit and never consults
a second. A duplicate registration therefore does not conflict, does not warn,
and does not fail at startup -- it silently wins, and the loser becomes
unreachable code that still looks live in its own module, still has tests, and
still passes them.

That is not hypothetical. `POST /api/machines/{machine_id}/test` was registered
in both routes/machines.py and routes/machines_tunnel.py, and app.py includes
the tunnel router first. So the tunnel module answered every machine test for
every provider, and for anything other than an ssh_proxy it returned
{"status": "configured"} -- a description of the database row, not a probe of
the endpoint, with no `ok` key at all. The frontend keys on `ok` and falls back
to `status` for the reason, so a healthy gateway reported

    Could not reach llm.ai-machine.cfappsecurity.com:443: configured

which reads as a network failure. The real implementation in routes/machines.py
had been unreachable the whole time.

This asserts on the assembled application, not on any one router, because the
collision only exists once they are included together and in order.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path


def _app():
    """Import app against a throwaway database.

    Never the production file: importing app.py pulls in config, and config
    resolves WC_DB_PATH at import time (CLAUDE.md rule 9 -- db.init migrates,
    so it must never be pointed at production by a test).
    """
    tmp = Path(tempfile.mkdtemp(prefix="wc-routes-")) / "wc.db"
    os.environ["WC_DB_PATH"] = str(tmp)
    os.environ.setdefault("WC_PROXY_ENABLED", "0")
    import config

    importlib.reload(config)
    import app as app_module

    return app_module.app


def _walk(routes):
    """Yield every real route, descending into included routers.

    This FastAPI version does not flatten `include_router` into `app.routes`.
    It appends an `_IncludedRouter` wrapper holding the original router, so
    walking `app.routes` directly sees 16 entries -- the docs endpoints and
    the handful defined on `app` itself -- and not one of the /api routes.

    The first version of this test did exactly that and passed against a tree
    that had the duplicate it was written to catch. A guard test that cannot
    see the thing it guards is worse than no test, because it reports safety.
    Hence the assertion below that /api routes were found at all.
    """
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)
            continue
        yield route


class NoDuplicateRouteRegistrationTests(unittest.TestCase):
    def test_no_method_and_path_is_registered_twice(self):
        app = _app()
        seen: dict[tuple[str, str], list[str]] = defaultdict(list)
        for route in _walk(app.routes):
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            endpoint = getattr(route, "endpoint", None)
            if not path or not methods:
                continue
            for method in methods:
                where = (
                    f"{endpoint.__module__}.{endpoint.__name__}"
                    if endpoint is not None
                    else "<unknown>"
                )
                seen[(method, path)].append(where)

        # The walk must have reached the application's real surface. Without
        # this, a future FastAPI that changes the wrapper shape again turns
        # this whole file back into a test that always passes.
        api_paths = {path for _, path in seen if path.startswith("/api/")}
        self.assertGreater(
            len(api_paths), 20,
            f"only found {len(api_paths)} /api routes -- the walk is not "
            f"reaching the included routers, so this test proves nothing",
        )

        duplicates = {
            key: where for key, where in seen.items() if len(where) > 1
        }
        self.assertEqual(
            duplicates, {},
            "these routes are registered more than once; the first one "
            "included wins and the rest are unreachable:\n"
            + "\n".join(
                f"  {method} {path}\n" + "\n".join(f"      {w}" for w in where)
                for (method, path), where in sorted(duplicates.items())
            ),
        )


if __name__ == "__main__":
    unittest.main()
