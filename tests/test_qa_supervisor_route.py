"""QA: the supervisor page is reachable at the path people type."""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

import app

WEB = Path(__file__).resolve().parent.parent / "web"


class SupervisorRouteQA(unittest.IsolatedAsyncioTestCase):
    """Both paths must serve the page, and the assets it asks for must resolve.

    "/supervisor" returned 404 while "/supervisor.html" worked. Nothing in the
    app linked to the short form -- index.html frames the long one -- so this was
    invisible from inside the product and only showed up on typing the obvious
    URL. "/" and "/login" both serve their templates extensionless, so the short
    form is the one that matches the rest of the app.
    """

    def _routes(self):
        return {r.path for r in app.app.routes if hasattr(r, "path")}

    def test_both_paths_are_registered(self):
        registered = self._routes()
        self.assertIn("/supervisor", registered)
        self.assertIn("/supervisor.html", registered)

    def test_the_long_form_is_kept_because_the_app_asks_for_it(self):
        # index.html's iframe and its standalone fallback both use the long
        # form; removing it in favour of the short one would break the button.
        app_js = (WEB / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("supervisor.html", app_js)

    async def test_both_serve_the_same_page(self):
        request = SimpleNamespace(state=SimpleNamespace(session={"user": "admin"}))
        response = await app.handle_supervisor_page(request)
        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Supervisor", body)
        # The script tag the page depends on, and its cache-buster.
        self.assertIn("supervisor.js?v=", body)

    def test_every_asset_the_page_requests_has_a_route(self):
        """A page that loads but whose script 404s looks identical to a bug.

        Checked by reading the markup rather than trusting a list: a new asset
        added to the page with no route is exactly this failure again.
        """
        import re
        html = (WEB / "supervisor.html").read_text(encoding="utf-8")
        registered = self._routes()
        for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
            if ref.startswith(("http", "//", "#", "data:", "about:")):
                continue
            path = "/" + ref.split("?")[0].lstrip("/")
            with self.subTest(asset=ref):
                served = (
                    path in registered
                    or path == "/"
                    or path.startswith("/assets/")   # mounted StaticFiles
                )
                self.assertTrue(served, f"{ref} has no route")


if __name__ == "__main__":
    unittest.main()
