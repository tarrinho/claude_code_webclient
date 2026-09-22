"""QA: active conversations float to the top of Favourites.

The sidebar shows a running dot for a conversation that is mid-turn or whose
terminal session is busy, but the list itself was ordered purely by placement
and recency, so the conversation you were waiting on could sit anywhere in
Favourites -- including below the fold.

`floatActive` reorders one section so those lead. It is deliberately a pure
function taking a predicate rather than reading `activeTurnIds` itself, because
the two activity signals live in different places (`activeTurnIds` is a
browser-side Set, `terminal_busy` arrives on the chat row) and a function that
reached for both could not be tested without a DOM.

These cases run the real module under node rather than asserting on its source
text. `tests/test_qa_chat_order.py` says why that matters for ordering work:
"Ordering assertions are easy to write vacuously -- a fixture whose natural
order already matches the expected one passes no matter what the query does."
A source-text assertion cannot tell a stable sort from an unstable one at all,
and stability is the property that keeps drag-placement meaningful underneath
the float. Every fixture below is therefore arranged so the expected order
differs from the input order.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT_LIST = ROOT / "web" / "assets" / "chat-list.js"

NODE = shutil.which("node")


def _function_body(source: str, signature: str) -> str:
    """The text of one function, from *signature* to its matching brace.

    The wiring cases below used a fixed character window instead --
    `source[start:start + 900]`. That is a magic number standing in for "this
    function", and it expires silently: on 2026-09-22 a five-line comment added
    inside `nudge` pushed `commitOrder(` to 1023 characters from the signature,
    so the assertion failed against code that was entirely correct. A window
    that is too small reports a defect that is not there; a window that is too
    large reads into the next function and reports a defect that belongs to it.
    Neither failure mode is visible when the test is written, because both
    depend on how long the function happens to be that day.

    Counting braces costs one loop and removes the number from the test. It is
    still source inspection -- see this module's docstring on why that is a weak
    check -- but it is at least scoped to what it claims to be reading.
    """
    start = source.index(signature)
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def _run_float(chats, active_ids):
    """Call floatActive(chats, predicate) in node and return the id order.

    The module is copied to a `.mjs` name because this repo's assets are
    plain `.js` with no package.json declaring module type, and node would
    otherwise parse the ES module as CommonJS and fail on `export`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "chat_list.mjs"
        module.write_text(CHAT_LIST.read_text(), encoding="utf-8")
        script = f"""
        const m = await import({json.dumps(module.as_uri())});
        const chats = {json.dumps(chats)};
        const active = new Set({json.dumps(active_ids)});
        const before = JSON.stringify(chats);
        const out = m.floatActive(
            chats, chat => active.has(chat.id) || Boolean(chat.terminal_busy));
        process.stdout.write(JSON.stringify({{
            order: out.map(c => c.id),
            mutated: JSON.stringify(chats) !== before,
        }}));
        """
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"node failed: {result.stderr.strip()[:600]}")
        return json.loads(result.stdout)


def _chats(*specs):
    """Rows shaped like the sidebar's, from ("id", terminal_busy) pairs."""
    out = []
    for spec in specs:
        if isinstance(spec, tuple):
            chat_id, busy = spec
        else:
            chat_id, busy = spec, False
        out.append({"id": chat_id, "title": chat_id, "pinned": 1,
                    "terminal_busy": busy})
    return out


@unittest.skipIf(NODE is None, "node is required to execute the module")
class FloatActiveTests(unittest.TestCase):

    def test_an_active_conversation_leads(self):
        got = _run_float(_chats("a", "b", "c"), ["c"])
        self.assertEqual(got["order"], ["c", "a", "b"])

    def test_terminal_busy_counts_as_active(self):
        """The second signal, which arrives on the row rather than the Set."""
        got = _run_float(_chats("a", ("b", True), "c"), [])
        self.assertEqual(got["order"], ["b", "a", "c"])

    def test_active_conversations_keep_their_relative_order(self):
        """Stability is what keeps the placement underneath meaningful."""
        got = _run_float(_chats("a", "b", "c", "d"), ["d", "b"])
        self.assertEqual(got["order"], ["b", "d", "a", "c"])

    def test_idle_conversations_keep_their_relative_order(self):
        got = _run_float(_chats("a", "b", "c", "d"), ["c"])
        self.assertEqual(got["order"], ["c", "a", "b", "d"])

    def test_nothing_active_leaves_the_order_alone(self):
        got = _run_float(_chats("b", "a", "c"), [])
        self.assertEqual(got["order"], ["b", "a", "c"])

    def test_everything_active_leaves_the_order_alone(self):
        got = _run_float(_chats("b", "a", "c"), ["a", "b", "c"])
        self.assertEqual(got["order"], ["b", "a", "c"])

    def test_an_empty_section_is_handled(self):
        self.assertEqual(_run_float([], [])["order"], [])

    def test_the_input_array_is_not_mutated(self):
        """The caller still holds the grouped array; reordering in place
        would reorder it for every other reader too."""
        got = _run_float(_chats("a", "b", "c"), ["c"])
        self.assertFalse(got["mutated"], "floatActive must not sort in place")

    def test_an_unknown_active_id_is_harmless(self):
        got = _run_float(_chats("a", "b"), ["not-here"])
        self.assertEqual(got["order"], ["a", "b"])


class FloatWiringTests(unittest.TestCase):
    """The parts that need a DOM, pinned at source level.

    These are weaker than the behavioural cases above and are here only
    because `commitOrder` and the render path close over browser state.
    """

    SOURCE = CHAT_LIST.read_text()

    def test_float_active_is_exported(self):
        self.assertIn("export function floatActive", self.SOURCE)

    def test_favourites_are_floated_and_recent_is_not(self):
        self.assertIn("floatActive(groups.pinned", self.SOURCE)
        self.assertNotIn("floatActive(groups.recent", self.SOURCE)
        self.assertNotIn("floatActive(groups.archived", self.SOURCE)

    def test_both_activity_signals_feed_the_predicate(self):
        self.assertIn("activeTurnIds.has", self.SOURCE)
        self.assertIn("terminal_busy", self.SOURCE)

    def test_placement_is_persisted_without_the_floated_rows(self):
        """A floated row sits at the top only while it is busy. Persisting
        the displayed order would freeze that temporary slot as its
        permanent `position` -- silently converting a placement the user
        never chose, which is the failure the comment above commitOrder
        already warns about for cross-section drags."""
        body = _function_body(self.SOURCE, "function commitOrder(")
        self.assertIn("dataset.floated !== '1'", body,
                      "commitOrder must exclude floated rows")

    def test_the_phone_path_persists_through_commitOrder_too(self):
        """`nudge` is the second way an order reaches the server. It must
        delegate rather than build its own id list, or the float protection
        would cover drag and quietly miss every phone reorder."""
        body = _function_body(self.SOURCE, "function nudge(")
        self.assertIn("commitOrder(", body)
        self.assertNotIn("onReorder(", body)

    def test_floated_rows_are_marked_from_what_was_rendered(self):
        """commitOrder reads the marker off the DOM rather than re-testing
        activity, so a turn ending between render and drop cannot make the
        two disagree about which rows were floated."""
        self.assertIn("item.dataset.floated = '1'", self.SOURCE)


if __name__ == "__main__":
    unittest.main()
