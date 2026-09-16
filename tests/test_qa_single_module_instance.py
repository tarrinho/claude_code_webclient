"""QA: the page must load one copy of each module, not two.

On 2026-09-16 the conversation ⋯ menu did nothing, at every width, with no
console error. The page was loading `app.js` twice — once under the version
`index.html` named and once under the version the other modules imported —
because assets were served from the working tree while the HTML came from the
deployed release. Two module instances means two `DOMContentLoaded` runs, two
controllers, and two of every listener. Confirmed with CDP at the time:

    #chatList / #chatListDesktop : click x2   (chat-list.js:951)
    #menuBtn                     : click x2   (two different script ids)
    #composerInput               : input x2, keydown x2

The user-visible failure was total and silent, and it only hit *toggles*:
controller A opened the menu, controller B saw an open menu and closed it, in
the same tick. Selecting a conversation still worked, because doing that twice
is idempotent — which is why a page-wide fault presented as one broken button.

Why this test is worth having even after the serving fix. The serving change
removes the cause that was found; it does not remove the failure mode. A
module loaded under two URLs — a hand-edited `?v=`, a new entry point added
with a stale version, a bare `import './app.js'` alongside a versioned one —
produces the same silent doubling, and nothing else in the suite would catch
it. `test_qa_asset_versions_match_content.py` compares repo files against repo
files and passed throughout the outage.

Asserted on listener counts rather than on behaviour because that is the
mechanism: one bound handler per control. A behavioural assertion ("the menu
opens") would also catch the doubling, but only for controls that happen to be
toggles, and only while they stay toggles.

The fixture serves from the repo through the app itself, so this exercises
module identity, not the deployment. The deployment half is
tests/smoke_assets_match_release.py, which needs the live host.
"""
from __future__ import annotations

import unittest

import tests.test_frontend_browser as fb


@unittest.skipUnless(fb.DRIVER_OK, f"playwright driver unusable ({fb.DRIVER_WHY})")
@unittest.skipIf(fb.CHROMIUM is None, "no Chromium binary on PATH")
class SingleModuleInstanceTests(fb._BrowserFixture):
    """One listener per control means one copy of the module that bound it."""

    def _listener_counts(self, element_id: str) -> dict[str, int]:
        """Event listeners actually attached to an element, by type.

        Read through CDP rather than by counting handler side effects: a
        duplicate listener is invisible to the DOM API, and inferring it from
        behaviour is what let this survive so long.
        """
        cdp = self.page.context.new_cdp_session(self.page)
        cdp.send("Runtime.enable")
        handle = cdp.send("Runtime.evaluate", {
            "expression": f"document.getElementById({element_id!r})"})
        object_id = handle["result"].get("objectId")
        self.assertIsNotNone(
            object_id, f"#{element_id} is not in the page at all")
        listeners = cdp.send(
            "DOMDebugger.getEventListeners", {"objectId": object_id}
        ).get("listeners", [])
        counts: dict[str, int] = {}
        for entry in listeners:
            counts[entry["type"]] = counts.get(entry["type"], 0) + 1
        return counts

    def _load(self):
        self._login()
        self.page.goto(self.base, timeout=15_000, wait_until="domcontentloaded")
        self.page.wait_for_selector("#chatListDesktop", timeout=20_000)
        self.page.wait_for_timeout(1500)

    def test_the_chat_list_has_one_click_listener(self):
        """chat-list.js binds one delegated click handler per list. Two means
        two controllers, and the second undoes what the first did."""
        self._load()
        counts = self._listener_counts("chatListDesktop")
        self.assertEqual(
            counts.get("click", 0), 1,
            f"expected exactly one click listener on the conversation list, "
            f"saw {counts} -- the page is running more than one copy of "
            f"chat-list.js, and every toggle in it will cancel itself",
        )

    def test_the_composer_has_one_of_each_listener(self):
        """conversation.js is bound from the same init path, so it doubles for
        the same reason -- and a doubled keydown sends a prompt twice."""
        self._load()
        counts = self._listener_counts("composerInput")
        for event in ("input", "keydown"):
            with self.subTest(event=event):
                self.assertLessEqual(
                    counts.get(event, 0), 1,
                    f"#composerInput has {counts.get(event)} {event} "
                    f"listeners: {counts}",
                )

    def test_each_module_is_fetched_under_exactly_one_url(self):
        """The cause, rather than the symptom. Two URLs for one file is what
        produces two instances; catching it here names the file, while the
        listener assertions above only say that something doubled."""
        self._load()
        urls = self.page.evaluate("""() => performance.getEntriesByType('resource')
            .map(e => e.name)
            .filter(n => n.includes('/assets/') && n.includes('.js'))""")
        by_file: dict[str, set[str]] = {}
        for url in urls:
            path = url.split("/assets/")[1]
            by_file.setdefault(path.split("?")[0], set()).add(path)
        doubled = {name: sorted(v) for name, v in by_file.items() if len(v) > 1}
        self.assertEqual(
            doubled, {},
            f"these modules were fetched under more than one URL, so the page "
            f"holds a separate instance of each: {doubled}",
        )


if __name__ == "__main__":
    unittest.main()
