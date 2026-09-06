"""QA coverage for the orchestrator page being able to call its own API.

The page could not make a single request. ``getCsrf()`` awaited ``apiFetch()``
and ``apiFetch()``'s first statement awaited ``getCsrf()``, so the two recursed
into each other and ``fetch`` was never reached. Because async recursion never
throws, the cookie fallback in the ``catch`` was unreachable too.

Underneath that sat a second, independent fault: ``/api/settings`` returns no
``csrf_token`` field, so unwinding the recursion alone would have stored ``""``
and re-entered on the next call. Either bug alone breaks the page, which is
part of why it was hard to see -- and the supervisors, supervisor_tasks and
supervisor_messages tables all held zero rows, the feature never having worked.

The fix reads the cookie directly and synchronously. ``wc_csrf`` is set
``httponly=false`` precisely so this script can read it; that is the
double-submit design working as intended. Synchronous matters: with no await in
``getCsrf`` the mutual recursion is impossible by construction rather than by
remembering not to reintroduce it.

These tests run the real file's logic in a browser rather than reading it,
because "does this hang" is not a question source inspection answers.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR_JS = REPO / "web" / "assets" / "orchestrator" / "main.js"

def supervisor_source() -> str:
    """Every orchestrator module, concatenated.

    The 0.10.0 split turned one file into nine, so a substring assertion that
    reads main.js alone searches a fraction of the code and fails on everything
    that moved. Globbing the directory means the next extraction needs no edit
    here, and deduplicating by resolved path means SUPERVISOR_JS pointing inside
    that directory does not read one module twice.
    """
    seen, parts = set(), []
    for path in sorted(SUPERVISOR_JS.parent.glob("*.js")):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        parts.append(path.read_text(encoding="utf-8"))
    if not parts:
        raise AssertionError(
            f"no orchestrator modules found beside {SUPERVISOR_JS} -- the split "
            "moved them somewhere this test does not know about"
        )
    return "\n".join(parts)

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def run_in_browser(html: str, budget_ms: int = 8000) -> str:
    """Render *html* headless and return the document title."""
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "probe.html"
        page.write_text(html, encoding="utf-8")
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             # Chromium writes a ~126 MB profile per launch. Without this it
             # picks its own /tmp/org.chromium.Chromium.scoped_dir.* and
             # leaves it behind, so a single run of this file leaked 11 of
             # them and filled a 1.9 GB tmpfs -- after which every browser
             # test in the suite fails on a timeout and leaks another.
             f"--user-data-dir={page.parent}/chrome-profile",
             f"--virtual-time-budget={budget_ms}", "--dump-dom", f"file://{page}"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    match = re.search(r"<title>([^<]*)</title>", result.stdout)
    return match.group(1) if match else ""


class GetCsrfSourceTests(unittest.TestCase):
    """Cheap structural guards, so a rewrite cannot quietly restore the loop."""

    def setUp(self):
        self.source = supervisor_source()

    def test_get_csrf_is_synchronous(self):
        """An async getCsrf is what let apiFetch await its way back into it."""
        self.assertIn("function getCsrf()", self.source)
        self.assertNotIn("async function getCsrf()", self.source,
                         "getCsrf must not be async, or apiFetch can re-enter it")

    def test_get_csrf_does_not_call_the_api(self):
        """The token is only ever in the cookie; asking the server is the bug."""
        body = self.source.split("function getCsrf()", 1)[1].split("\n  }", 1)[0]
        for forbidden in ("apiFetch", "fetch(", "/api/"):
            self.assertNotIn(forbidden, body,
                             f"getCsrf must not reach the network ({forbidden})")

    def test_it_reads_the_cookie_the_server_actually_sets(self):
        self.assertIn("wc_csrf=", self.source)

    def test_no_caller_awaits_it(self):
        """`await getCsrf()` is the shape that made the recursion possible."""
        self.assertNotIn("await getCsrf()", self.source)


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class ApiFetchTerminatesTests(unittest.TestCase):
    """The property that matters: a call reaches fetch instead of hanging."""

    HARNESS = """
    <script>
    // Defined rather than assigned: a file:// page silently drops writes to
    // document.cookie, so `document.cookie = ...` reads back empty and the
    // test would fail on the sandbox instead of on the code. Verified.
    Object.defineProperty(document, "cookie", {
      value: "wc_session=abc; wc_csrf=token-from-cookie; other=x",
      configurable: true,
    });
    let reachedFetch = false, sentHeader = null;
    window.fetch = async (url, opts) => {
      reachedFetch = true;
      sentHeader = (opts && opts.headers) ? opts.headers["X-CSRF-Token"] : null;
      return {ok: true, json: async () => ({}), status: 200};
    };
    // A depth guard, so a reintroduced recursion fails fast and loudly rather
    // than hanging the test until the virtual time budget expires.
    let depth = 0;
    %(definitions)s
    (async () => {
      try {
        await apiFetch("/api/supervisors");
        document.title = "reachedFetch=" + reachedFetch + " header=" + sentHeader;
      } catch (e) {
        document.title = "THREW: " + e.message;
      }
    })();
    </script>
    """

    def _definitions(self) -> str:
        """Lift getCsrf and apiFetch out of the real file, verbatim."""
        source = supervisor_source()
        start = source.index("function getCsrf()")
        # Up to the end of apiFetch, which is the next helper after it.
        end = source.index("async function apiJson", start) if "async function apiJson" in source \
            else source.index("\n  // ──", source.index("async function apiFetch", start))
        body = source[start:end]
        # Drop any `import ...;` the lifted region carries. This harness runs
        # the code in a classic <script>, where an import is a syntax error --
        # and once the 0.10.0 split moves getCsrf/apiFetch into their own
        # module, the region above them starts with one.
        body = "\n".join(
            re.sub(r"^(\s*)export\s+", r"\1", line)
            for line in body.splitlines()
            if not line.lstrip().startswith("import "))
        # `state`, not a bare `csrfToken`: the shared bindings moved into a
        # state object in 0.10.0, because an ES module's exports are live
        # bindings that an importing module cannot assign to. The stub has to
        # match what the lifted code now reads, or it throws "state is not
        # defined" and the failure reads as apiFetch never reaching fetch.
        return 'const state = { csrfToken: "" };\n' + body.replace("\n  ", "\n")

    def test_a_call_reaches_fetch_and_carries_the_token(self):
        title = run_in_browser(self.HARNESS % {"definitions": self._definitions()})
        self.assertIn("reachedFetch=true", title,
                      f"apiFetch never reached fetch: {title!r}")
        self.assertIn("header=token-from-cookie", title,
                      f"the CSRF header was not taken from the cookie: {title!r}")

    def test_the_old_recursive_shape_would_have_failed_this(self):
        """Guards the guard: prove the harness detects the original bug.

        Without this, the test above could be passing for some incidental
        reason and would not actually be pinning anything.
        """
        recursive = """
        async function getCsrf() {
          if (csrfToken) return csrfToken;
          if (++depth > 500) throw new Error("RECURSION");
          try {
            const r = await apiFetch("/api/settings");
            csrfToken = r.csrf_token || "";
          } catch (e) {
            if (e.message === "RECURSION") throw e;
            csrfToken = "cookie";
          }
          return csrfToken;
        }
        async function apiFetch(url, opts = {}) {
          const token = await getCsrf();
          return window.fetch(url, {headers: {"X-CSRF-Token": token}});
        }
        """
        title = run_in_browser(
            self.HARNESS % {"definitions": 'let csrfToken = "";' + recursive})
        self.assertNotIn("reachedFetch=true", title,
                         "the harness must fail on the recursive version, or it "
                         f"proves nothing about the fixed one: {title!r}")


if __name__ == "__main__":
    unittest.main()
