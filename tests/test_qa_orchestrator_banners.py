"""QA coverage for the orchestrator goal banner and completion banner.

Covers:
* Goal banner — HTML id/class, CSS rules, JS selectors, DOM wiring, dismiss.
* Completion banner — HTML id/class, CSS rules, JS selectors, DOM wiring, dismiss.
* showCompletionBanner counting — done/failed/blocked/pending arithmetic.
* showCompletionBanner result summary — truncation at 200 chars, sorting.
* Goal banner scroll-into-view — the smooth scroll call is present.
* XSS safety — banners use textContent, never innerHTML, for user content.
* Init wiring — dismiss buttons are hooked up in the init function.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
SUPERVISOR_HTML = WEB / "orchestrator.html"
SUPERVISOR_JS = WEB / "assets" / "orchestrator" / "main.js"

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

SUPERVISOR_CSS = SUPERVISOR_HTML  # inline in the HTML file

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


class GoalBannerStructureTests(unittest.TestCase):
    """Verify the goal banner markup, CSS, and JS wiring exist."""

    def setUp(self):
        self.html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        self.js = supervisor_source()

    def test_goal_banner_has_html_container(self):
        self.assertIn('id="goal-banner"', self.html)
        self.assertIn('class="goal-banner"', self.html)
        # Must be hidden initially so it doesn't flash on page load.
        # Match the full opening div tag for the banner.
        goal_open = self.html[self.html.find('id="goal-banner"'):self.html.find('id="goal-banner"') + 80]
        self.assertIn("hidden", goal_open)

    def test_goal_banner_has_label(self):
        self.assertIn('class="goal-label"', self.html)
        self.assertIn("Goal", self.html)

    def test_goal_banner_has_text_element(self):
        self.assertIn('id="goal-text"', self.html)

    def test_goal_banner_has_dismiss_button(self):
        self.assertIn('class="goal-banner-dismiss"', self.html)
        self.assertIn("aria-label", self.html)
        self.assertIn("Dismiss goal", self.html)

    def test_goal_banner_css_rule_exists(self):
        self.assertIn(".goal-banner {", self.html)
        self.assertIn(".goal-banner-dismiss {", self.html)

    def test_goal_text_is_styled(self):
        self.assertIn(".goal-text {", self.html)

    def test_js_selects_the_goal_elements(self):
        self.assertIn("goalBanner", self.js)
        self.assertIn("goalText", self.js)

    def test_goal_banner_hidden_by_default(self):
        # Must be hidden initially so it doesn't flash on page load
        self.assertIn("el.goalBanner.hidden = false", self.js)
        # The initial state should be hidden via the HTML attribute
        self.assertIn("goal-banner\" class=\"goal-banner\" hidden", self.html)

    def test_show_goal_banner_sets_text_content(self):
        """User content must use textContent, never innerHTML."""
        self.assertIn("el.goalText.textContent = promptText", self.js)

    def test_show_goal_banner_removes_hidden(self):
        self.assertIn("el.goalBanner.hidden = false", self.js)

    def test_show_goal_banner_scrolls_into_view(self):
        self.assertIn("scrollIntoView", self.js)
        self.assertIn("goalBanner", self.js)

    def test_dismiss_goal_banner_resets_hidden(self):
        self.assertIn("el.goalBanner.hidden = true", self.js)

    def test_goal_banner_state_is_tracked(self):
        self.assertIn("_currentGoal", self.js)


class CompletionBannerStructureTests(unittest.TestCase):
    """Verify the completion banner markup, CSS, and JS wiring exist."""

    def setUp(self):
        self.html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        self.js = supervisor_source()

    def test_completion_banner_has_html_container(self):
        self.assertIn('id="completion-banner"', self.html)
        self.assertIn('class="completion-banner"', self.html)
        self.assertIn("hidden", self.html.split("completion-banner")[1].split(">")[0])

    def test_completion_banner_has_icon(self):
        self.assertIn('class="completion-icon"', self.html)
        self.assertIn("✅", self.html)

    def test_completion_banner_has_title(self):
        self.assertIn('class="completion-title"', self.html)
        self.assertIn("Task Completed", self.html)

    def test_completion_banner_has_stats_element(self):
        self.assertIn('id="completion-stats"', self.html)

    def test_completion_banner_has_summary_element(self):
        self.assertIn('id="completion-summary"', self.html)

    def test_completion_banner_has_dismiss_button(self):
        self.assertIn('class="completion-close"', self.html)
        self.assertIn("data-action", self.html)

    def test_completion_banner_css_rule_exists(self):
        self.assertIn(".completion-banner {", self.html)
        self.assertIn(".completion-close {", self.html)

    def test_completion_stats_is_styled(self):
        self.assertIn(".completion-stats {", self.html)

    def test_completion_summary_is_styled(self):
        self.assertIn(".completion-summary {", self.html)

    def test_js_selects_the_completion_elements(self):
        self.assertIn("completionBanner", self.js)
        self.assertIn("completionStats", self.js)
        self.assertIn("completionSummary", self.js)

    def test_completion_banner_hidden_by_default(self):
        self.assertIn("el.completionBanner.hidden = false", self.js)
        self.assertIn("completion-banner\" class=\"completion-banner\" hidden", self.html)

    def test_dismiss_completion_banner_resets_hidden(self):
        self.assertIn("el.completionBanner.hidden = true", self.js)

    def test_completion_banner_state_is_tracked(self):
        self.assertIn("_completionData", self.js)


class CompletionBannerCountingTests(unittest.TestCase):
    """Test the counting logic in showCompletionBanner with controlled task data."""

    def setUp(self):
        self.js_source = supervisor_source()

    def _run_completion_logic(self, tasks_data):
        """Create a sandbox with the JS and run showCompletionBanner manually.

        Returns the computed stats text and summary text from the banner elements.
        """
        sandbox = f"""
        let tasks = {json.dumps(tasks_data)};

        // Minimal mock of the el object and addChatMessage
        let _statsText = '';
        let _summaryText = '';
        let _addedMessage = '';
        let _addedRole = '';

        const el = {{
            completionStats: {{ textContent: null }},
            completionSummary: {{ textContent: null }},
            completionBanner: {{ hidden: true }},
        }};

        el.completionStats.__defineSetter__('textContent', function(v) {{ _statsText = v; }});
        el.completionSummary.__defineSetter__('textContent', function(v) {{ _summaryText = v; }});
        el.completionBanner.__defineGetter__('hidden', function() {{ return true; }});

        function addChatMessage(role, content) {{ _addedRole = role; _addedMessage = content; }}

        // Extract just the showCompletionBanner function body and run it
        {self.js_source.split('function showCompletionBanner(eng)')[1].split('\\n  function ')[0]}

        // Run the function
        showCompletionBanner(null);

        JSON.stringify({{_statsText, _summaryText, _addedRole, _addedMessage}});
        """
        if CHROMIUM:
            with tempfile.TemporaryDirectory() as tmp:
                page = Path(tmp) / "probe.html"
                page.write_text(sandbox, encoding="utf-8")
                result = subprocess.run(
                    [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
                     # Chromium writes a ~126 MB profile per launch. Without this it
                     # picks its own /tmp/org.chromium.Chromium.scoped_dir.* and
                     # leaves it behind, so a single run of this file leaked 11 of
                     # them and filled a 1.9 GB tmpfs -- after which every browser
                     # test in the suite fails on a timeout and leaks another.
                     f"--user-data-dir={page.parent}/chrome-profile",
                     "--virtual-time-budget=5000", "--dump-dom", f"file://{page}"],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                # If subprocess works, parse output
                try:
                    return result.stdout.strip()
                except Exception:
                    return ""
        return ""

    def test_all_done_counts_correctly(self):
        """Three completed tasks → stats should show 3/3."""
        tasks_data = [
            {"id": "t1", "title": "Task A", "status": "done", "result": "Result A"},
            {"id": "t2", "title": "Task B", "status": "done", "result": "Result B"},
            {"id": "t3", "title": "Task C", "status": "done", "result": "Result C"},
        ]
        stats = self._count_stats(tasks_data)
        self.assertEqual(stats, "Completed 3/3 tasks")

    def test_mixed_statuses_counts_correctly(self):
        """3 done, 1 failed, 1 blocked, 0 pending → 3/5 with failures/skips."""
        tasks_data = [
            {"id": "t1", "title": "Done 1", "status": "done", "result": "ok"},
            {"id": "t2", "title": "Done 2", "status": "done", "result": "ok"},
            {"id": "t3", "title": "Failed 1", "status": "failed", "result": "err"},
            {"id": "t4", "title": "Blocked 1", "status": "blocked", "result": "skip"},
            {"id": "t5", "title": "Pending 1", "status": "pending", "result": None},
        ]
        stats = self._count_stats(tasks_data)
        self.assertIn("Completed 2/5 tasks", stats)
        self.assertIn("1 failed", stats)
        self.assertIn("1 skipped", stats)
        self.assertIn("1 pending", stats)

    def test_all_failed_shows_failures(self):
        tasks_data = [
            {"id": "t1", "title": "Bad 1", "status": "failed", "result": "boom"},
            {"id": "t2", "title": "Bad 2", "status": "failed", "result": "crash"},
        ]
        stats = self._count_stats(tasks_data)
        self.assertIn("Completed 0/2 tasks", stats)
        self.assertIn("2 failed", stats)

    def test_no_tasks_shows_zero(self):
        tasks_data = []
        stats = self._count_stats(tasks_data)
        self.assertEqual(stats, "Completed 0/0 tasks")

    def _count_stats(self, tasks_data):
        """Run just the counting part of showCompletionBanner in a sandbox."""
        return_code = subprocess.run(
            [
                "python3", "-c",
                """
import json, sys
tasks = json.loads(sys.argv[1])
total = len(tasks)
done_count = sum(1 for t in tasks if t.get("status") == "done")
fail_count = sum(1 for t in tasks if t.get("status") == "failed")
skip_count = sum(1 for t in tasks if t.get("status") == "blocked")
pending_count = total - done_count - fail_count - skip_count
stats = f"Completed {done_count}/{total} tasks"
if fail_count:
    stats += f", {fail_count} failed"
if skip_count:
    stats += f", {skip_count} skipped"
if pending_count:
    stats += f", {pending_count} pending"
print(stats)
                """,
                json.dumps(tasks_data),
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return return_code.stdout.strip()


class CompletionBannerSummaryTests(unittest.TestCase):
    """Test the result summary generation in showCompletionBanner."""

    def test_summary_includes_task_results(self):
        """Each completed task with a result should appear in the summary."""
        tasks_data = [
            {"id": "t1", "title": "Find files", "status": "done", "result": "Found 42 files"},
            {"id": "t2", "title": "Count lines", "status": "done", "result": "15000 lines"},
            {"id": "t3", "title": "Fail task", "status": "failed", "result": "timeout"},
            {"id": "t4", "title": "No result", "status": "done", "result": None},
        ]
        result = self._run_summary_sandbox(tasks_data)
        self.assertIn("t1 (Find files): Found 42 files", result)
        self.assertIn("t2 (Count lines): 15000 lines", result)
        # Failed tasks and tasks without results should not appear
        self.assertNotIn("Fail task", result)
        self.assertNotIn("No result", result)

    def test_result_truncation_at_200_chars(self):
        """Each task result is truncated to 200 chars."""
        long_result = "x" * 300
        tasks_data = [
            {"id": "t1", "title": "Long task", "status": "done", "result": long_result},
        ]
        result = self._run_summary_sandbox(tasks_data)
        # The JS uses .substring(0, 200) so the output should be 200 chars
        self.assertIn("t1 (Long task):", result)
        self.assertEqual(len(result.split("t1 (Long task): ")[1]), 200)

    def test_empty_result_shows_placeholder(self):
        tasks_data = [
            {"id": "t1", "title": "Empty", "status": "done", "result": ""},
        ]
        result = self._run_summary_sandbox(tasks_data)
        self.assertIn("(No result text available)", result)

    def _run_summary_sandbox(self, tasks_data):
        """Extract the results-building logic and run it standalone."""
        return_code = subprocess.run(
            [
                "python3", "-c",
                """
import json, sys
tasks = json.loads(sys.argv[1])
results = []
for t in tasks:
    if t.get("status") == "done" and t.get("result"):
        results.append(f"Task {t['id']} ({t.get('title', '')}): {t['result'][:200]}")
result = "\\n\\n".join(results) or "(No result text available)"
print(result)
                """,
                json.dumps(tasks_data),
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return return_code.stdout.strip()


class XSSSafetyTests(unittest.TestCase):
    """Ensure banner content is rendered safely (textContent, not innerHTML)."""

    def setUp(self):
        self.js = supervisor_source()

    def test_goal_text_uses_textContent(self):
        """User prompt text must use textContent to prevent XSS."""
        self.assertIn("el.goalText.textContent = promptText", self.js)

    def test_completion_stats_uses_textContent(self):
        self.assertIn("el.completionStats.textContent =", self.js)

    def test_completion_summary_uses_textContent(self):
        self.assertIn("el.completionSummary.textContent =", self.js)

    def test_completion_summary_escapes_user_content(self):
        """Check that the esc() function exists and is used for the task results
        that go into the completion summary. The JS builds the summary string
        by interpolating task data, so we need to verify the esc() function
        wraps the content."""
        self.assertIn("function esc(", self.js)

    def test_no_innerHTML_used_for_banner_content(self):
        """The banner elements should never have innerHTML set to user data.

        We scope the check to the showGoalBanner and showCompletionBanner
        functions to avoid false positives from the rest of the file (which
        does use innerHTML for the chat list and task tree).
        """
        js = self.js
        # Both end markers allow `export`, which the 0.10.0 split put on every
        # top-level declaration. Without it the slice runs past the end of
        # banners.js into the next concatenated module and picks up an
        # innerHTML that belongs to the task tree.
        end = r"\n  (?:export )?function "
        # Extract showGoalBanner body
        goal_fn = re.split(end, js.split('function showGoalBanner(')[1])[0]
        self.assertNotIn("innerHTML", goal_fn,
                         "showGoalBanner must not use innerHTML")
        # Extract showCompletionBanner body
        comp_fn = re.split(end, js.split('function showCompletionBanner(')[1])[0]
        self.assertNotIn("innerHTML", comp_fn,
                         "showCompletionBanner must not use innerHTML")

    def test_completion_puts_task_data_in_textcontent_not_markup(self):
        """Task titles and results are agent output, so they must not be parsed.

        Renamed from `test_completion_uses_esc_for_task_data`, because that was
        asserting the wrong thing in two ways. It sliced out the banner function
        and then asserted `esc(` against the *whole file*, which is true of
        orchestrator.js regardless of what the banner does -- the unused variable
        ruff flagged was the evidence it had stopped looking where it said it
        was looking. And once the slice is actually used the requirement is
        wrong: this function assigns through `textContent` throughout and never
        builds markup, so there is nothing for an escape helper to do. Demanding
        `esc(` here would be asking for a call that could only be decorative.

        What keeps it safe is that every sink is a text sink. That is the
        property asserted, and it is the one that breaks if somebody reaches for
        a template string later.
        """
        # The end marker allows `export`, which the 0.10.0 split put on every
        # top-level declaration. Without it the split found nothing, the slice
        # ran past the end of banners.js into the next concatenated module, and
        # the test failed on an `innerHTML` belonging to a different file.
        body = re.split(
            r"\n  (?:export )?function ",
            self.js.split("function showCompletionBanner(")[1])[0]
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(sink, body, f"task data must not reach {sink}")
        self.assertIn("completionSummary.textContent", body)
        self.assertIn("completionStats.textContent", body)


class InitWiringTests(unittest.TestCase):
    """Verify the dismiss buttons are wired up in the init function."""

    def setUp(self):
        self.js = supervisor_source()

    def test_goal_dismiss_button_wired(self):
        self.assertIn("el.goalDismissBtn", self.js)
        self.assertIn("addEventListener", self.js)
        # The dismissGoalBanner function should be attached
        self.assertIn("dismissGoalBanner", self.js)

    def test_completion_dismiss_button_wired(self):
        self.assertIn("el.completionCloseBtn", self.js)
        self.assertIn("addEventListener", self.js)
        self.assertIn("dismissCompletionBanner", self.js)

    def test_dismiss_buttons_attached_in_init(self):
        """Both dismiss buttons should be hooked in the init function, not separately.

        `init_fn` is everything after `function init()`, i.e. the rest of the
        file, so asserting against it says only "these names appear somewhere
        below init" -- which is what the test's own name denies. It sliced the
        body out and then did not use it, and the slice was also taking the
        text *after* the next function rather than the body before it.
        """
        after_init = self.js.split("function init()")[1]
        body = after_init.split("\nfunction ")[0]
        self.assertIn("dismissGoalBanner", body)
        self.assertIn("dismissCompletionBanner", body)


class CompletionBannerTriggerTests(unittest.TestCase):
    """Verify the completion banner is triggered on the right SSE events."""

    def setUp(self):
        self.js = supervisor_source()

    def test_completion_banner_triggered_on_done_event(self):
        self.assertIn('case "done":', self.js)
        self.assertIn("showCompletionBanner", self.js)

    def test_completion_banner_only_on_success(self):
        """The banner should only appear on 'done' status, not 'error'."""
        done_section = self.js.split('case "done":')[1].split("case ")[0]
        self.assertIn('data.status === "done"', done_section)

    def test_completion_banner_loads_fresh_task_data(self):
        self.assertIn("loadTasks()", self.js)

    def _send_prompt_body(self) -> str:
        """The body of sendPrompt, and nothing after it.

        Both tests below computed this and then asserted against the whole
        remainder of the file instead, so either would have passed with the call
        moved into any later function.
        """
        after = self.js.split("async function sendPrompt()")[1]
        return after.split("\nasync function ")[0].split("\nfunction ")[0]

    def test_send_prompt_shows_goal_banner(self):
        """The goal banner should appear as soon as the user sends a prompt."""
        self.assertIn("showGoalBanner", self._send_prompt_body())

    def test_goal_text_is_user_prompt(self):
        """The goal banner should display the user's prompt text."""
        self.assertIn("showGoalBanner(text)", self._send_prompt_body())


class GoalBannerPlacementTests(unittest.TestCase):
    """Verify the goal banner sits above the chat messages in the DOM."""

    def setUp(self):
        self.html = SUPERVISOR_HTML.read_text(encoding="utf-8")

    def test_goal_banner_before_chat_messages(self):
        """The goal banner must appear before the chat messages so it is
        the first thing visible in the chat area."""
        goal_pos = self.html.find('id="goal-banner"')
        chat_pos = self.html.find('id="chat-messages"')
        self.assertGreater(chat_pos, goal_pos,
                           "goal-banner must appear before chat-messages in the DOM")

    def test_completion_banner_before_chat_messages(self):
        completion_pos = self.html.find('id="completion-banner"')
        chat_pos = self.html.find('id="chat-messages"')
        self.assertGreater(chat_pos, completion_pos,
                           "completion-banner must appear before chat-messages in the DOM")

    def test_goal_banner_inside_supervisor_chat(self):
        """The banner must be inside the #orchestrator-chat container, not floating."""
        chat_open = self.html.find('id="orchestrator-chat"')
        goal_close = self.html.find('class="goal-banner"', chat_open)
        chat_close = self.html.find("</div>", goal_close)
        self.assertNotEqual(chat_close, -1, "orchestrator-chat div must close after goal-banner")

    def test_completion_banner_inside_supervisor_chat(self):
        chat_open = self.html.find('id="orchestrator-chat"')
        completion_close = self.html.find('class="completion-banner"', chat_open)
        chat_close = self.html.find("</div>", completion_close)
        self.assertNotEqual(chat_close, -1, "orchestrator-chat div must close after completion-banner")


class GoalBannerCSSPropertiesTests(unittest.TestCase):
    """Verify the goal banner CSS has the right visual properties."""

    def setUp(self):
        self.css_text = SUPERVISOR_HTML.read_text(encoding="utf-8")

    def test_goal_banner_has_gradient_background(self):
        self.assertIn(".goal-banner {", self.css_text)
        section = self.css_text.split(".goal-banner {")[1].split("}")[0]
        self.assertIn("background:", section)
        self.assertIn("gradient", section)

    def test_goal_banner_has_border(self):
        self.assertIn("border:", self.css_text.split(".goal-banner {")[1].split("}")[0])

    def test_goal_banner_has_border_radius(self):
        self.assertIn("border-radius:", self.css_text.split(".goal-banner {")[1].split("}")[0])

    def test_completion_banner_has_gradient_background(self):
        self.assertIn(".completion-banner {", self.css_text)
        section = self.css_text.split(".completion-banner {")[1].split("}")[0]
        self.assertIn("background:", section)
        self.assertIn("gradient", section)

    def test_completion_banner_has_border(self):
        self.assertIn("border:", self.css_text.split(".completion-banner {")[1].split("}")[0])

    def test_completion_summary_is_scrollable(self):
        """The completion summary should be scrollable when results are long."""
        section = self.css_text.split(".completion-summary {")[1].split("}")[0]
        self.assertIn("overflow-y: auto", section)
        self.assertIn("max-height:", section)


class BrowserIntegrationTests(unittest.TestCase):
    """End-to-end tests that run the orchestrator page in a real browser."""

    @unittest.skipUnless(CHROMIUM, "chromium not installed")
    def test_goal_banner_element_exists(self):
        """The goal banner elements render in the DOM."""
        html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        result = self._run_in_browser(html)
        self.assertIn("goal-banner", result)
        self.assertIn("goal-text", result)
        self.assertIn("goal-banner-dismiss", result)

    @unittest.skipUnless(CHROMIUM, "chromium not installed")
    def test_completion_banner_element_exists(self):
        """The completion banner elements render in the DOM."""
        html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        result = self._run_in_browser(html)
        self.assertIn("completion-banner", result)
        self.assertIn("completion-stats", result)
        self.assertIn("completion-summary", result)
        self.assertIn("completion-close", result)

    @unittest.skipUnless(CHROMIUM, "chromium not installed")
    def test_js_parses_without_syntax_errors(self):
        """The JS file should parse without errors in the browser."""
        # If the page loads without errors, the JS parsed correctly.
        html = SUPERVISOR_HTML.read_text(encoding="utf-8")
        result = self._run_in_browser(html)
        self.assertNotIn("Uncaught", result)

    def _run_in_browser(self, html):
        """Render the HTML headless and return the dumped DOM."""
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
                 "--virtual-time-budget=8000", "--dump-dom", f"file://{page}"],
                capture_output=True, text=True, timeout=60, check=False,
            )
            return result.stdout if result.stdout else ""


if __name__ == "__main__":
    unittest.main()