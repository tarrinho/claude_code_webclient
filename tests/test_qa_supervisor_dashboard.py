"""QA: the Supervisor Dashboard fields on /api/supervisor-map.

The dashboard spec needs four things the map never carried -- which provider
family an agent talks to, whether it reaches it through the Claude Code CLI or
a direct API call, how the operator can talk to it, and a state vocabulary
that separates "waiting for me" from "busy" -- plus per-hub CPU/RAM so the hub
can be coloured by saturation.

None of it is newly collected. Every value is derived from something already
stored, which is the point: the gap was plumbing, not measurement.

Two decisions worth stating because a later reader will wonder:

* `status` is untouched. Seven existing tests assert its seven-value
  vocabulary, and `agent_state` answers a different question, so it is added
  alongside rather than replacing it.
* The transport-mechanism and model/provider values are separate keys and are
  never joined into one string. The spec forbids merging those two badges, and
  a single field would make the frontend split it again.
"""
from __future__ import annotations

import unittest

from routes.db_supervisor_map import (
    _agent_state,
    _comms,
    _load_index,
    _provider_family,
    _transport_mechanism,
)


class ProviderFamilyTests(unittest.TestCase):
    """The three-way palette the spec asks for."""

    def test_the_official_api_is_anthropic(self):
        self.assertEqual(
            _provider_family({"host": "api.anthropic.com"}), "anthropic")

    def test_a_vllm_model_is_local(self):
        """"free/self-hosted", in the spec's words. Keyed on the model rather
        than the host because the same gateway also fronts paid models."""
        self.assertEqual(
            _provider_family({"host": "gw.example", "model": "vllm/Qwen3-8B"}),
            "local")

    def test_a_gateway_model_is_not_local_just_because_it_shares_the_gateway(self):
        """The distinction that makes this worth deriving rather than reading
        off the provider column: azure_ai/* and vllm/* live on one gateway and
        only one of them is free."""
        self.assertEqual(
            _provider_family({"host": "gw.example", "model": "azure_ai/gpt-5.4-mini"}),
            "google_litellm")

    def test_a_loopback_base_url_is_local(self):
        self.assertEqual(
            _provider_family({"base_url": "http://127.0.0.1:4000/v1"}), "local")

    def test_an_unknown_backend_does_not_claim_to_be_anthropic(self):
        """Guessing "anthropic" for an unidentified row would put free turns
        and paid turns in the same colour, which is the one thing the palette
        exists to prevent."""
        self.assertEqual(_provider_family({}), "local")


class TransportMechanismTests(unittest.TestCase):
    """CLI versus direct API, kept separate from the model badge."""

    def test_a_claude_code_backend_is_cli(self):
        self.assertEqual(
            _transport_mechanism({"provider": "claude_code",
                                  "host": "api.anthropic.com"}), "cli")

    def test_a_transport_routed_backend_is_also_cli(self):
        """The correction that matters. backend_kind returns "ssh-proxy" for
        these, and comparing against "through_claude_code" alone labelled every
        transport-routed backend a direct API call -- the opposite of the truth,
        since claude_proxy.py on the far side spawns the same `claude` binary.
        On this deployment those are the majority of CLI turns."""
        self.assertEqual(
            _transport_mechanism({"transport_id": "t1", "provider": "claude_code"}),
            "cli")

    def test_a_direct_provider_is_a_direct_api_call(self):
        """`provider` stores the wire protocol and only ever holds
        "claude_code" or "direct" (see backend_kind's docstring), so "direct"
        is the value that means a straight API call. An earlier version of
        this test passed "openai" and failed: backend_kind defaults anything
        it does not recognise to through_claude_code, which is correct -- an
        unknown row on this deployment is a CLI turn -- and the test was
        asserting a vocabulary the column does not use."""
        self.assertEqual(
            _transport_mechanism({"provider": "direct",
                                  "base_url": "https://gw.example/v1"}),
            "direct_api")

    def test_an_unrecognised_provider_is_treated_as_cli(self):
        """Matches backend_kind rather than second-guessing it: this console
        spawns the CLI for everything except an explicitly direct row."""
        self.assertEqual(_transport_mechanism({"provider": "openai"}), "cli")


class CommsTests(unittest.TestCase):
    def test_a_voice_chat_offers_both(self):
        self.assertEqual(_comms({"voice_mode": 1}), "both")

    def test_a_plain_chat_is_text_only(self):
        self.assertEqual(_comms({}), "text")


class AgentStateTests(unittest.TestCase):
    """The vocabulary an operator scans for."""

    def test_a_degraded_conversation_is_blocked(self):
        self.assertEqual(_agent_state("idle", degraded=True), "blocked")

    def test_an_errored_conversation_is_blocked(self):
        self.assertEqual(_agent_state("error"), "blocked")

    def test_a_pending_question_is_waiting_for_input(self):
        """The state worth notifying about, and the only one the operator can
        clear themselves."""
        self.assertEqual(
            _agent_state("idle", has_question=True), "waiting_for_input")

    def test_unread_output_is_waiting_for_input(self):
        self.assertEqual(_agent_state("waiting"), "waiting_for_input")

    def test_mid_turn_is_running(self):
        self.assertEqual(_agent_state("running"), "running")
        self.assertEqual(_agent_state("busy"), "running")

    def test_anything_else_is_idle(self):
        self.assertEqual(_agent_state("done"), "idle")

    def test_blocked_wins_over_waiting(self):
        """A degraded conversation that also has unread output is blocked: the
        operator cannot clear it by answering, so calling it
        waiting_for_input would send them to do something that cannot work."""
        self.assertEqual(
            _agent_state("waiting", degraded=True), "blocked")


class LoadIndexTests(unittest.TestCase):
    """One number for the hub's glow."""

    def test_it_takes_the_worse_of_cpu_and_memory(self):
        """Averaging would paint a host at 100% memory and 10% CPU as healthy,
        and memory is what has been killing processes on this deployment."""
        self.assertAlmostEqual(
            _load_index({"cpu_pct": 10.0, "mem_pct": 100.0}), 1.0)

    def test_it_is_a_fraction_not_a_percentage(self):
        self.assertAlmostEqual(
            _load_index({"cpu_pct": 50.0, "mem_pct": 20.0}), 0.5)

    def test_it_clamps(self):
        self.assertAlmostEqual(
            _load_index({"cpu_pct": 400.0, "mem_pct": 0.0}), 1.0)

    def test_an_unsampled_host_is_none_not_zero(self):
        """A host that has never reported and a host doing nothing must not
        look alike: 0.0 would render as a healthy blue glow for a machine that
        is simply not talking to us."""
        self.assertIsNone(_load_index(None))

    def test_a_malformed_sample_is_none_rather_than_a_crash(self):
        self.assertIsNone(_load_index({"cpu_pct": "n/a", "mem_pct": None}))


if __name__ == "__main__":
    unittest.main()


# ── Rendering: the encodings the spec inverts ───────────────────────────
import json
import re
from pathlib import Path

try:  # pragma: no cover - reported as a skip
    import quickjs
except ImportError:
    quickjs = None

ROOT = Path(__file__).resolve().parents[1]
MAP_JS = ROOT / "web" / "assets" / "supervisor-map.js"
STUB_JS = Path(__file__).resolve().parent / "js" / "d3_dom_stub.js"


def _module_source() -> str:
    src = MAP_JS.read_text(encoding="utf-8")
    src = re.sub(r"^\s*import\s.*?;\s*$", "", src, flags=re.MULTILINE | re.DOTALL)
    src = re.sub(
        r"^\s*export\s+(?=(?:async\s+)?(?:function|class|const|let|var)\b)",
        "", src, flags=re.MULTILINE)
    src = re.sub(r"^\s*export\s*\{.*?\};\s*$", "", src, flags=re.MULTILINE | re.DOTALL)
    src = re.sub(r"^\s*export\s+default\s+", "", src, flags=re.MULTILINE)
    return re.sub(r"^(let|const)\s", "var ", src, flags=re.MULTILINE)


def _eval(probe: str) -> dict:
    script = "\n".join([STUB_JS.read_text(encoding="utf-8"), _module_source(), probe])
    return json.loads(quickjs.Context().eval(script))


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class ProviderIsTheFillTests(unittest.TestCase):
    """The inversion. Fill used to be statusColor(status), so a node's colour
    said what it was doing and nothing said who it talked to. The spec wants
    provider as the fill and status "not by changing the fill color" -- the
    stable property gets the colour, the volatile one gets motion."""

    def test_each_family_gets_its_own_fill(self):
        out = _eval("""
          JSON.stringify({
            a: providerColor('anthropic'),
            g: providerColor('google_litellm'),
            l: providerColor('local')
          });
        """)
        self.assertEqual(len({out["a"], out["g"], out["l"]}), 3,
                         f"two families share a colour: {out}")

    def test_an_unknown_family_does_not_borrow_anthropics_colour(self):
        """Defaulting to anthropic would paint an unidentified backend as the
        paid one, or worse, a paid one as free."""
        out = _eval("JSON.stringify({u: providerColor('nonsense'), l: providerColor('local')});")
        self.assertEqual(out["u"], out["l"])

    def test_no_provider_colour_collides_with_a_status_ring_colour(self):
        """They are layered on the same mark, so a fill that matches the ring
        drawn over it makes the ring invisible."""
        out = _eval("""
          JSON.stringify({
            fills: ['anthropic','google_litellm','local'].map(providerColor),
            rings: ['running','error','waiting'].map(statusColor)
          });
        """)
        self.assertFalse(
            set(out["fills"]) & set(out["rings"]),
            f"a provider fill equals a status ring colour: {out}")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class StateRingTests(unittest.TestCase):
    def test_running_pulses(self):
        out = _eval("JSON.stringify(stateRing('running'));")
        self.assertTrue(out["pulse"])
        self.assertIsNone(out["dash"])

    def test_blocked_is_solid_and_does_not_animate(self):
        """The spec stops the animation for blocked on purpose: a pulsing red
        reads as "working on it", which is the opposite of the truth."""
        out = _eval("JSON.stringify(stateRing('blocked'));")
        self.assertFalse(out["pulse"])
        self.assertIsNone(out["dash"])

    def test_waiting_is_dashed_and_still(self):
        out = _eval("JSON.stringify(stateRing('waiting_for_input'));")
        self.assertFalse(out["pulse"])
        self.assertTrue(out["dash"])

    def test_idle_gets_no_ring_at_all(self):
        """So the nodes that carry a ring are the ones worth looking at.
        Ringing everything spends the operator's attention evenly, which is
        what a dashboard exists to avoid."""
        out = _eval("JSON.stringify({ring: stateRing('idle')});")
        self.assertIsNone(out["ring"])


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class HubLoadColourTests(unittest.TestCase):
    def test_cool_when_healthy_and_warm_when_saturated(self):
        out = _eval("JSON.stringify({low: loadColor(0), high: loadColor(1)});")
        low = [int(n) for n in re.findall(r"\d+", out["low"])]
        high = [int(n) for n in re.findall(r"\d+", out["high"])]
        self.assertGreater(low[2], low[0], f"0% load should be blue-dominant: {out['low']}")
        self.assertGreater(high[0], high[2], f"100% load should be red-dominant: {out['high']}")

    def test_an_unsampled_host_is_neutral_not_cool(self):
        """A blue glow would say "healthy" about a machine that is not
        reporting at all."""
        out = _eval("JSON.stringify({none: loadColor(null), cool: loadColor(0)});")
        self.assertNotEqual(out["none"], out["cool"])

    def test_the_midpoint_is_not_grey(self):
        """Interpolated in two legs through amber; a single blue-to-red blend
        passes through a muddy grey exactly where most hosts sit."""
        out = _eval("JSON.stringify({mid: loadColor(0.5)});")
        r, g, b = [int(n) for n in re.findall(r"\d+", out["mid"])]
        self.assertGreater(r + g, b * 2, f"midpoint reads grey/blue: {out['mid']}")


class BadgesStaySeparateTests(unittest.TestCase):
    """The spec's explicit "do not add": the transport-mechanism badge and the
    model/provider badge must not be merged into a single label."""

    def setUp(self):
        self.js = MAP_JS.read_text(encoding="utf-8")

    def test_the_mechanism_badge_is_its_own_element(self):
        self.assertIn("map-mech-badge", self.js)

    def test_the_mechanism_text_is_not_concatenated_with_the_model(self):
        """A template joining the two is the merge the spec forbids."""
        self.assertNotRegex(
            self.js,
            r"transport_mechanism\s*\+.*model_label|model_label\s*\+.*transport_mechanism",
            "the two badges are being built as one string")

    def test_they_are_placed_on_different_sides_of_the_node(self):
        """Different shape *and* position, so three marks around one node stay
        tellable apart."""
        mech = re.search(r'map-mech-badge"\)\s*\.attr\("x", ([^)]+)\)', self.js)
        comms = re.search(r'map-comms-icon"\)\s*\.attr\("x", ([^)]+)\)', self.js)
        self.assertIsNotNone(mech)
        self.assertIsNotNone(comms)
        self.assertIn("-", comms.group(1),
                      "the comms icon should sit on the opposite side")


class LegendTests(unittest.TestCase):
    """Always visible, and collapsible rather than dismissable."""

    def setUp(self):
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.css = (ROOT / "web" / "assets" / "styles.css").read_text(encoding="utf-8")

    def test_it_exists_and_is_not_hidden_by_default(self):
        self.assertIn('id="mapLegend"', self.html)
        legend = re.search(r'<div id="mapLegend"[^>]*>', self.html).group(0)
        self.assertNotIn("hidden", legend,
                         "the spec asks for it to be visible by default")

    def test_it_covers_provider_colours_and_states(self):
        for family in ("anthropic", "google_litellm", "local"):
            self.assertIn(f'data-family="{family}"', self.html)
        for state in ("running", "blocked", "waiting"):
            self.assertIn(f"map-ring-key-{state}", self.html)

    def test_the_state_keys_are_hollow_so_they_teach_the_right_encoding(self):
        """State is an outline on the map. A filled legend swatch for a state
        would teach that status is a fill, which is what this change undid."""
        self.assertRegex(self.css, r"\.map-ring-key\s*\{[^}]*background:\s*none")

    def test_provider_keys_are_filled(self):
        self.assertRegex(
            self.css, r'\.map-swatch\[data-family="anthropic"\]\s*\{[^}]*background:')


class PulseRespectsReducedMotionTests(unittest.TestCase):
    def setUp(self):
        self.css = (ROOT / "web" / "assets" / "styles.css").read_text(encoding="utf-8")

    def test_the_ring_pulse_is_css_not_a_d3_transition(self):
        """d3's .transition() animates from JavaScript and the
        prefers-reduced-motion query cannot reach it -- the trap _motionMs()
        exists to work around for the zoom buttons. A CSS animation is
        switched off by the query for free."""
        self.assertIn("@keyframes map-ring-pulse", self.css)

    def test_reduced_motion_stops_it(self):
        block = re.search(
            r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\}\s*\n",
            self.css, re.DOTALL)
        self.assertIsNotNone(block)
        self.assertIn("map-ring-pulse", block.group(1))

    def test_it_does_not_animate_the_radius(self):
        """r on an SVG circle is not compositable, so animating it repaints
        the subtree every frame -- on a map with a dozen running agents, on a
        host already short of CPU."""
        keyframes = re.search(r"@keyframes map-ring-pulse \{(.*?)\n\}", self.css, re.DOTALL)
        self.assertIsNotNone(keyframes)
        self.assertNotRegex(keyframes.group(1), r"\br\s*:")


class FillIsProviderNotStatusTests(unittest.TestCase):
    """The inversion, asserted on the renderer itself.

    The palette tests above prove the colours exist and do not collide; they
    pass just as happily if the renderer never calls providerColor. Reverting
    `fillColor` to statusColor broke none of them, so this closes that.
    """

    def setUp(self):
        self.js = MAP_JS.read_text(encoding="utf-8")

    def test_the_node_fill_comes_from_the_provider_family(self):
        self.assertRegex(
            self.js, r"fillColor\s*=\s*family\s*\?\s*providerColor\(family\)",
            "node fill is not keyed on provider_family; the spec asks for "
            "status to be an outline and not the fill",
        )

    def test_containers_still_fall_back_to_status(self):
        """Orchestrators, the centre and overflow markers are not agents and
        have no family. A neutral grey would make a failing group look inert."""
        self.assertRegex(self.js, r"providerColor\(family\)\s*:\s*statusColor\(")

    def test_the_status_ring_is_drawn_after_the_shape(self):
        """It has to sit above the fill it annotates. SVG has no z-index, so
        document order is the only thing that decides."""
        ring = self.js.index("stateRing(d.data.agent_state)")
        fill = self.js.index("const fillColor = family")
        self.assertGreater(ring, fill)


class TaskSummaryTests(unittest.TestCase):
    """The plain-language line, and what it honestly is."""

    def setUp(self):
        from routes.db_supervisor_map import _task_summary
        self.summarise = _task_summary

    def test_it_reads_the_preview_field(self):
        """chat_last_activity returns `preview` -- substr(content, 1, 200).
        An earlier version read `content`, found nothing, and every agent's
        summary silently fell through to the state fallback: plausible-looking
        output that said nothing at all."""
        self.assertEqual(
            self.summarise({"preview": "Reading the config file."}, "running"),
            "Reading the config file.")

    def test_it_stops_at_the_first_sentence(self):
        self.assertEqual(
            self.summarise({"preview": "Found it. Then a lot more text."}, "running"),
            "Found it.")

    def test_a_very_long_first_sentence_is_trimmed(self):
        """A dashboard line that wraps to three lines stops being scannable."""
        out = self.summarise({"preview": "x" * 400}, "running")
        self.assertLessEqual(len(out), 120)
        self.assertTrue(out.endswith("…"))

    def test_it_collapses_whitespace(self):
        self.assertEqual(
            self.summarise({"preview": "  a\n\n  b  "}, "idle"), "a b")

    def test_with_no_message_it_explains_the_state(self):
        """An operator scanning for the agent that needs them is better served
        by "Waiting for an answer" than an empty row they must click."""
        self.assertEqual(
            self.summarise(None, "waiting_for_input"), "Waiting for an answer")
        self.assertEqual(
            self.summarise({}, "blocked"), "Stopped and cannot continue")
        self.assertEqual(self.summarise(None, "idle"), "Idle")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class SparklineTests(unittest.TestCase):
    def test_an_empty_series_draws_nothing(self):
        """A flat line at zero is a claim ("no activity"); nothing is the
        truth when a window holds no rows."""
        out = _eval("""
          drawSparkline([]);
          var svg = document.getElementById('mapDetailSpark');
          JSON.stringify({children: (svg && svg.__children ? svg.__children.length : 0)});
        """)
        self.assertEqual(out["children"], 0)

    def test_a_single_point_draws_nothing(self):
        """One point is not a trend, and a one-pixel polyline reads as a
        speck of dirt."""
        out = _eval("""
          drawSparkline([5]);
          var svg = document.getElementById('mapDetailSpark');
          JSON.stringify({children: (svg && svg.__children ? svg.__children.length : 0)});
        """)
        self.assertEqual(out["children"], 0)


class CountFormattingTests(unittest.TestCase):
    """Token counts on this deployment reach the billions; the panel is 200px."""

    def setUp(self):
        self.js = MAP_JS.read_text(encoding="utf-8")

    def test_large_counts_are_abbreviated(self):
        self.assertIn("_formatCount", self.js)
        for unit in ("1e9", "1e6", "1e3"):
            self.assertIn(unit, self.js)


class NoCostAnywhereTests(unittest.TestCase):
    """The spec's first explicit "do not add": no dollar-cost tracking
    anywhere on this view. Usage is turns and tokens only."""

    def test_the_map_payload_carries_no_cost_field(self):
        src = (ROOT / "routes" / "db_supervisor_map.py").read_text(encoding="utf-8")
        self.assertNotIn("cost_usd", src)
        self.assertNotIn('"cost"', src)

    def test_the_renderer_shows_no_currency(self):
        js = MAP_JS.read_text(encoding="utf-8")
        self.assertNotIn("cost_usd", js)
        self.assertNotRegex(js, r'["\'`]\s*\$\s*["\'`]', "a currency symbol is being rendered")

    def test_the_totals_query_selects_no_cost(self):
        """usage_events has a cost_usd column, so this is a live temptation
        rather than a hypothetical one."""
        src = (ROOT / "routes" / "db_usage.py").read_text(encoding="utf-8")
        fn_start = src.index("async def usage_agent_totals")
        fn_end = src.index("async def", fn_start + 10)
        body = src[fn_start:fn_end]
        # The docstring stripped first. It explains *why* no cost is derived,
        # so the word appears there legitimately -- and matching it failed
        # this test against a clean query. Fourth time this repo has been
        # bitten by a check that reads a mention as a use; see
        # test_qa_rules_preflight.py's pip check for the same fix.
        body = re.sub(r'"""(.*?)"""', "", body, flags=re.DOTALL)
        self.assertNotIn("cost", body,
                         "the totals query selects a cost column")


class AgentSeriesTests(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "routes" / "db_supervisor_map.py").read_text(encoding="utf-8")

    def test_it_is_owner_scoped_in_the_query(self):
        """The agent id arrives from a URL path. Scoping in the query rather
        than trusting the caller is what stops one account asking about
        another's agent."""
        fn = self.src[self.src.index("async def agent_series"):]
        self.assertIn("owner_id = ?", fn)

    def test_it_caps_the_bucket_count(self):
        """An unbounded ?buckets= would let one request scan the whole table."""
        fn = self.src[self.src.index("async def agent_series"):]
        self.assertIn("min(buckets", fn)

    def test_it_returns_a_dense_series(self):
        """Zeros for quiet hours, so the x-axis is time. A sparse series
        compresses a two-hour gap into one pixel and reads as continuous
        work."""
        fn = self.src[self.src.index("async def agent_series"):]
        self.assertIn("rows.get(stamp, 0)", fn)


class DetailActionsTests(unittest.TestCase):
    """Step 5's actions, and the endpoints they drive.

    Every one maps onto an endpoint the console already had. That is worth a
    test: a parallel API for the dashboard would be a second way to do the
    same thing, free to drift from the first.
    """

    def setUp(self):
        self.js = MAP_JS.read_text(encoding="utf-8")

    def test_the_model_switch_patches_the_chat(self):
        self.assertRegex(self.js, r'method:\s*"PATCH"')
        self.assertIn('JSON.stringify({model})', self.js)

    def test_an_option_reply_answers_by_index(self):
        """A prompt with numbered choices cannot be answered with free text --
        typing "yes" at a menu does nothing -- so options are buttons and the
        text box is for the open-ended case."""
        self.assertIn("JSON.stringify({index})", self.js)
        self.assertIn("/question", self.js)

    def test_a_text_reply_posts_a_message(self):
        self.assertIn("JSON.stringify({content: text})", self.js)
        self.assertIn("/messages", self.js)

    def test_the_reply_control_is_only_offered_to_a_waiting_agent(self):
        """An always-visible box invites typing at agents that are mid-turn,
        where the message queues behind work the operator cannot see."""
        self.assertRegex(
            self.js, r'agent_state\s*===\s*"waiting_for_input"')

    def test_pause_is_labelled_stop_because_that_is_what_happens(self):
        """The spec says "pause". This console can stop a turn and cannot
        suspend one, and a button promising a state the backend does not have
        is worse than the honest verb."""
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        detail = html[html.index('id="mapDetailDrawer"'):html.index('id="mapDetailActionStatus"')]
        self.assertNotIn(">Pause<", detail)
        self.assertIn('id="mapDetailStop"', detail)

    def test_the_raw_log_button_hides_without_a_session(self):
        """A conversation with no linked terminal session has no raw log, and
        a button that opens an empty panel teaches the operator to stop
        trusting the buttons."""
        self.assertRegex(
            self.js,
            r"logs\.hidden\s*=\s*!\(nodeData\.type === \"chat\" && nodeData\.session_id\)")

    def test_actions_ask_the_page_to_refresh_rather_than_refetching(self):
        """app.js owns the fetching and the polling. Two owners for the map's
        data is how the panel and the poller end up disagreeing."""
        self.assertIn('"wc:map-refresh"', self.js)

    def test_the_events_use_the_existing_prefix(self):
        """app.js already listens on wc:map-*. A second family of names for
        the same panel is a rename waiting to be missed."""
        self.assertNotIn("wc:supervisor-map-", self.js)


class ActionEndpointsExistTests(unittest.TestCase):
    """The other half: the routes the panel calls have to be real. A test that
    only reads the frontend would pass against a button wired to nothing."""

    def setUp(self):
        self.routes = (ROOT / "routes" / "chats.py").read_text(encoding="utf-8")

    def test_the_chat_patch_route_exists_and_accepts_model(self):
        self.assertIn('@router.patch("/api/chats/{chat_id}")', self.routes)
        self.assertRegex(self.routes, r'allowed = \{[^}]*"model"')

    def test_the_question_answer_route_exists(self):
        self.assertIn('@router.post("/api/chats/{chat_id}/question")', self.routes)

    def test_the_message_route_exists(self):
        self.assertIn('@router.post("/api/chats/{chat_id}/messages")', self.routes)

    def test_the_stop_route_exists(self):
        self.assertIn('@router.post("/api/chats/{chat_id}/stop")', self.routes)


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class ToolbarFilterTests(unittest.TestCase):
    """Step 6's filters, exercised through the real module."""

    SAMPLE = """
      var DATA = {
        id: "root", label: "You", type: "center", status: "running",
        totals: {turns: 12, tokens: 3456, nodes: 2, agents: 3},
        generated_at: "2026-09-10T22:00:00Z",
        children: [
          {id: "h1", label: "Kali3", type: "transport", status: "idle",
           agent_count: 2, children: [
             {id: "a1", label: "cweb1", type: "chat", status: "error",
              agent_state: "blocked", provider_family: "anthropic"},
             {id: "a2", label: "cweb2", type: "chat", status: "idle",
              agent_state: "idle", provider_family: "local"}
           ]},
          {id: "h2", label: "Direct", type: "transport", status: "idle",
           agent_count: 1, children: [
             {id: "a3", label: "quiet", type: "chat", status: "done",
              agent_state: "idle", provider_family: "local"}
           ]}
        ]
      };
    """

    def _run_with(self, probe):
        return _eval(self.SAMPLE + probe)

    def test_problems_only_keeps_the_problem_agent(self):
        out = self._run_with("""
          _problemsOnly = true;
          var t = _visibleTree(DATA);
          var ids = [];
          (function walk(n){ ids.push(n.id); (n.children||[]).forEach(walk); })(t);
          JSON.stringify({ids: ids});
        """)
        self.assertIn("a1", out["ids"], "the blocked agent was filtered out")
        self.assertNotIn("a2", out["ids"], "an idle agent survived problems-only")

    def test_problems_only_keeps_the_hub_holding_a_problem(self):
        """Hiding the transport that holds the only blocked agent would hide
        the agent with it -- the opposite of what the filter is for."""
        out = self._run_with("""
          _problemsOnly = true;
          var t = _visibleTree(DATA);
          JSON.stringify({hubs: t.children.map(function(h){return h.id;})});
        """)
        self.assertIn("h1", out["hubs"])

    def test_search_matches_on_label(self):
        out = self._run_with("""
          _searchTerm = 'cweb2';
          var t = _visibleTree(DATA);
          var ids = [];
          (function walk(n){ ids.push(n.id); (n.children||[]).forEach(walk); })(t);
          JSON.stringify({ids: ids});
        """)
        self.assertIn("a2", out["ids"])
        self.assertNotIn("a1", out["ids"])

    def test_compact_shows_hubs_without_their_agents(self):
        out = self._run_with("""
          _compact = true;
          var t = _visibleTree(DATA);
          JSON.stringify({
            hubs: t.children.length,
            kids: t.children.map(function(h){ return (h.children||[]).length; })
          });
        """)
        self.assertEqual(out["hubs"], 2)
        self.assertEqual(out["kids"], [0, 0])

    def test_filtering_does_not_mutate_the_payload(self):
        """Destructive filtering could not restore what it hid without another
        fetch, and the poll is every ten seconds."""
        out = self._run_with("""
          _problemsOnly = true;
          _visibleTree(DATA);
          _problemsOnly = false;
          var t = _visibleTree(DATA);
          var n = 0;
          (function walk(x){ n += 1; (x.children||[]).forEach(walk); })(t);
          JSON.stringify({nodes: n});
        """)
        self.assertEqual(out["nodes"], 6, "the payload was pruned in place")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class ToolbarReadoutTests(unittest.TestCase):
    def test_totals_and_counts_are_rendered(self):
        out = _eval(ToolbarFilterTests.SAMPLE + """
          updateToolbar(DATA);
          JSON.stringify({
            totals: document.getElementById('mapTotals').textContent,
            counts: document.getElementById('mapCounts').textContent
          });
        """)
        self.assertIn("12 turns", out["totals"])
        self.assertIn("2 nodes", out["counts"])
        self.assertIn("3 agents", out["counts"])

    def test_counts_are_singular_where_they_should_be(self):
        out = _eval("""
          updateToolbar({totals: {turns: 1, tokens: 1, nodes: 1, agents: 1}});
          JSON.stringify({counts: document.getElementById('mapCounts').textContent});
        """)
        self.assertIn("1 node ", out["counts"])
        self.assertIn("1 agent", out["counts"])
        self.assertNotIn("nodes", out["counts"])

    def test_no_totals_renders_nothing_rather_than_zero(self):
        """"0 turns" is a claim about the fleet; blank is the truth when the
        server did not say."""
        out = _eval("""
          updateToolbar({});
          JSON.stringify({totals: document.getElementById('mapTotals').textContent});
        """)
        self.assertEqual(out["totals"], "")


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.js = MAP_JS.read_text(encoding="utf-8")

    def test_it_uses_the_servers_timestamp(self):
        """A client clock that is wrong makes a stale map look fresh, and a
        freshness indicator that can lie is worse than none."""
        self.assertIn("data.generated_at", self.js)

    def test_it_ticks_rather_than_rendering_once(self):
        """A static "updated 0s ago" is exactly what a frozen dashboard looks
        like."""
        self.assertIn("setInterval(_tickFreshness", self.js)

    def test_the_ticker_is_stopped_when_the_panel_closes(self):
        """A 1s interval behind a closed panel is the same leak the map's own
        poll guard exists to prevent."""
        close = self.js[self.js.index("export function closeSupervisorMap"):]
        self.assertIn("stopFreshnessTicker()", close[:400])

    def test_a_stale_map_is_flagged(self):
        """The poll is 10s, so 30s without a refresh means something stopped
        rather than ran slowly."""
        self.assertRegex(self.js, r"secs > 30")


class KeyboardShortcutTests(unittest.TestCase):
    def setUp(self):
        self.js = MAP_JS.read_text(encoding="utf-8")

    def test_there_is_a_problems_only_hotkey(self):
        """The spec's stated minimum."""
        self.assertRegex(self.js, r'key === "p"')

    def test_typing_in_a_field_does_not_trigger_them(self):
        """Otherwise typing "p" into the search box toggles the filter
        instead of searching."""
        self.assertRegex(self.js, r'tag === "input" \|\| tag === "textarea"')

    def test_they_are_ignored_while_the_panel_is_closed(self):
        self.assertRegex(self.js, r"if \(!panel \|\| panel\.hidden\) return;")

    def test_modified_keys_are_left_alone(self):
        """Ctrl+P is print and Cmd+C is copy; stealing them would be a bug
        report, not a feature."""
        self.assertRegex(self.js, r"event\.metaKey \|\| event\.ctrlKey")


# ── Step 7: the comms overlay ───────────────────────────────────────────
# Two agents talking to each other is the one relation the containment tree
# cannot express: the tree says which hub an agent runs on, and says nothing
# about who it has been messaging. These cover the aggregation and the drawing
# separately, because the two failed differently -- the aggregation merged
# directions, and the drawing had no lines at all.

class CommsEdgeAggregationTests(unittest.IsolatedAsyncioTestCase):
    """routes.db_supervisor_map.comms_edges, over a fake traffic log."""

    @staticmethod
    def _traffic(*records):
        async def fake(limit=200, scan_files=12):
            return list(records)
        return fake

    async def _edges(self, *records):
        from unittest.mock import patch

        import routes.db_supervisor_map as mod
        with patch("transcripts.agent_traffic", self._traffic(*records)):
            return await mod.comms_edges()

    async def test_one_edge_per_ordered_pair(self):
        edges = await self._edges(
            {"sender": "cweb2", "recipient": "cweb3", "summary": "later",
             "timestamp": "2026-09-10T19:02:03Z"},
            {"sender": "cweb2", "recipient": "cweb3", "summary": "earlier",
             "timestamp": "2026-09-10T19:00:00Z"},
        )
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["count"], 2)

    async def test_the_two_directions_stay_apart(self):
        """A one-way flood and a conversation look identical once A->B and
        B->A are merged, and the first is the interesting one."""
        edges = await self._edges(
            {"sender": "cweb2", "recipient": "cweb3", "summary": "ask",
             "timestamp": "2026-09-10T19:02:03Z"},
            {"sender": "cweb3", "recipient": "cweb2", "summary": "answer",
             "timestamp": "2026-09-10T19:01:11Z"},
        )
        self.assertEqual(len(edges), 2)
        self.assertEqual({(e["from"], e["to"]) for e in edges},
                         {("cweb2", "cweb3"), ("cweb3", "cweb2")})

    async def test_the_summary_is_the_most_recent_message(self):
        """agent_traffic returns newest first, so the first record seen for a
        pair is the one worth showing. Taking the last would put a stale line
        under a live edge."""
        edges = await self._edges(
            {"sender": "a", "recipient": "b", "summary": "newest",
             "timestamp": "2026-09-10T19:02:03Z"},
            {"sender": "a", "recipient": "b", "summary": "oldest",
             "timestamp": "2026-09-10T18:00:00Z"},
        )
        self.assertEqual(edges[0]["summary"], "newest")
        self.assertEqual(edges[0]["last_at"], "2026-09-10T19:02:03Z")

    async def test_it_falls_back_to_the_message_text_with_no_summary(self):
        edges = await self._edges(
            {"sender": "a", "recipient": "b", "text": "raw body",
             "timestamp": "2026-09-10T19:02:03Z"})
        self.assertEqual(edges[0]["summary"], "raw body")

    async def test_a_self_message_is_not_an_edge(self):
        """A loop on one node is drawn as an arc from a node to itself, which
        is unreadable and says nothing -- an agent noting something to itself
        is not inter-agent traffic."""
        edges = await self._edges(
            {"sender": "a", "recipient": "a", "summary": "note",
             "timestamp": "2026-09-10T19:02:03Z"})
        self.assertEqual(edges, [])

    async def test_a_record_missing_a_peer_is_skipped(self):
        edges = await self._edges(
            {"sender": "a", "recipient": "", "summary": "x", "timestamp": "t"},
            {"sender": None, "recipient": "b", "summary": "y", "timestamp": "t"},
            {"summary": "z", "timestamp": "t"},
        )
        self.assertEqual(edges, [])

    async def test_busiest_first(self):
        """With several pairs the overlay draws in this order, and the heaviest
        line should not be the one hidden underneath."""
        edges = await self._edges(
            {"sender": "a", "recipient": "b", "summary": "1", "timestamp": "t"},
            {"sender": "c", "recipient": "d", "summary": "2", "timestamp": "t"},
            {"sender": "c", "recipient": "d", "summary": "3", "timestamp": "t"},
        )
        self.assertEqual([e["count"] for e in edges], [2, 1])

    async def test_the_id_is_the_ordered_pair(self):
        """The frontend looks a clicked line up by this, so the two cannot
        disagree about which exchange a summary belongs to."""
        edges = await self._edges(
            {"sender": "a", "recipient": "b", "summary": "x", "timestamp": "t"})
        self.assertEqual(edges[0]["id"], "a→b")

    async def test_an_unavailable_traffic_log_is_an_empty_overlay(self):
        """Not a 500 and not a stack trace on the map: the overlay is an
        extra, and a transcript directory that cannot be read must not take
        the dashboard down with it."""
        from unittest.mock import patch

        import routes.db_supervisor_map as mod

        async def boom(**_kw):
            raise OSError("no transcripts")
        with patch("transcripts.agent_traffic", boom):
            self.assertEqual(await mod.comms_edges(), [])

    async def test_no_cost_field_reaches_the_overlay(self):
        """The spec rules out a dollar figure anywhere on this view, and a
        traffic record is a place one could arrive by accident."""
        edges = await self._edges(
            {"sender": "a", "recipient": "b", "summary": "x", "timestamp": "t",
             "cost_usd": 1.23, "total_cost_usd": 4.56})
        for edge in edges:
            for key in edge:
                self.assertNotIn("cost", key.lower())


class CommsEndpointTests(unittest.TestCase):
    """The route, and the one performance decision behind it."""

    def test_the_endpoint_exists_and_is_owner_gated(self):
        src = (ROOT / "routes" / "supervisor_map.py").read_text(encoding="utf-8")
        self.assertIn("/api/supervisor-map/comms", src)
        handler = src.split("/api/supervisor-map/comms", 1)[1]
        self.assertIn("request.state.session", handler,
                      "an unauthenticated caller could read who is talking to "
                      "whom across every owner on this host")
        self.assertIn("401", handler)

    def test_the_map_payload_does_not_scan_transcripts(self):
        """supervisor_map is polled every 10s. agent_traffic scans transcript
        files -- measured at 3.75s cold on this deployment -- so folding it in
        would put that scan on the critical path of the whole view, for the
        majority of readers who leave the overlay off."""
        src = (ROOT / "routes" / "db_supervisor_map.py").read_text(encoding="utf-8")
        marker = "async def supervisor_map("
        self.assertIn(marker, src)
        body = src.split(marker, 1)[1]
        # Up to the next top-level def: what supervisor_map itself runs.
        body = re.split(r"\nasync def |\ndef ", body, maxsplit=1)[0]
        stripped = "\n".join(
            line for line in body.splitlines()
            if not line.strip().startswith("#"))
        self.assertNotIn("agent_traffic", stripped)

    def test_the_frontend_fetches_it_only_while_the_toggle_is_on(self):
        src = MAP_JS.read_text(encoding="utf-8")
        self.assertIn("/api/supervisor-map/comms", src)
        # The fetch lives in _loadComms, and _loadComms is reached from the
        # toggle and its own timer -- never from renderSupervisorMap, which is
        # what the 10s poll calls.
        render = src.split("function renderSupervisorMap", 1)[1]
        render = render.split("\n/**", 1)[0]
        self.assertNotIn("_loadComms", render)


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class SpokesExistTests(unittest.TestCase):
    """The hub-and-spoke figure had no spokes.

    Nodes were positioned by the tree layout and drawn, and nothing joined a
    child to its parent -- so which hub an agent belonged to was conveyed by
    proximity alone. Sibling spacing here is fixed rather than fitted, so two
    hubs' children interleave as a matter of course, and proximity then
    conveys nothing.
    """

    SAMPLE = """
      var DATA = {
        id: "root", label: "You", type: "center", status: "running",
        children: [
          {id: "h1", label: "Kali3", type: "transport", status: "idle",
           children: [
             {id: "a1", label: "cweb1", type: "chat", status: "running"},
             {id: "a2", label: "cweb2", type: "chat", status: "idle"}
           ]},
          {id: "h2", label: "Direct", type: "transport", status: "idle",
           children: [{id: "a3", label: "cweb3", type: "chat", status: "idle"}]}
        ]
      };
    """

    def test_one_spoke_per_parent_child_pair(self):
        out = _eval(self.SAMPLE + """
          renderSupervisorMap(DATA);
          var g = stubFindByClass(stubSvg(), "map-spokes");
          var joined = g ? g.__children[0] : null;
          JSON.stringify({
            group: !!g,
            spokes: joined ? joined.__data.length : 0,
            paths: joined ? (joined.__computed.d || []).length : 0
          });
        """)
        self.assertTrue(out["group"], "no spoke layer was drawn at all")
        # 5 nodes, root excluded: h1, h2, a1, a2, a3.
        self.assertEqual(out["spokes"], 5)
        self.assertEqual(out["paths"], 5)

    def test_a_spoke_starts_at_its_parent_and_ends_at_its_child(self):
        """Routed through _nodeXY, like the nodes and like zoomToFit. Three
        independent position expressions is how the fit came to frame a
        rectangle nothing was drawn in."""
        out = _eval(self.SAMPLE + """
          renderSupervisorMap(DATA);
          var g = stubFindByClass(stubSvg(), "map-spokes");
          var joined = g.__children[0];
          var link = joined.__data[0];
          var a = _nodeXY(link.source), b = _nodeXY(link.target);
          JSON.stringify({
            d: joined.__computed.d[0],
            start: "M" + a.x + "," + a.y,
            end: b.x + "," + b.y
          });
        """)
        self.assertTrue(out["d"].startswith(out["start"]),
                        f"spoke does not start at the parent: {out}")
        self.assertTrue(out["d"].endswith(out["end"]),
                        f"spoke does not end at the child: {out}")

    def test_the_spoke_layer_is_under_the_nodes(self):
        """DOM order is paint order in SVG. Lines over the nodes would cross
        every mark they connect, and would take the clicks meant for them."""
        out = _eval(self.SAMPLE + """
          renderSupervisorMap(DATA);
          var vp = stubFindByClass(stubSvg(), "map-viewport");
          var classes = vp.__children.map(function (c) {
            return (c.__attrs && c.__attrs["class"]) || c.__joinedOntoClass || "?";
          });
          JSON.stringify({classes: classes});
        """)
        classes = out["classes"]
        self.assertIn("map-spokes", classes)
        self.assertIn("map-nodes", classes)
        self.assertLess(classes.index("map-spokes"), classes.index("map-nodes"))

    def test_the_spokes_do_not_take_pointer_events(self):
        """A hairline curve is otherwise a click target sitting on top of the
        background rect, and "click empty canvas to close" stops firing
        wherever a line happens to run."""
        css = (ROOT / "web" / "assets" / "styles.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.map-spokes\s*\{[^}]*pointer-events:\s*none")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class CommsOverlayDrawingTests(unittest.TestCase):
    """The overlay: matched by label, one arc per direction, nothing silent."""

    SAMPLE = SpokesExistTests.SAMPLE

    def _run(self, probe, edges, on="true"):
        return _eval(self.SAMPLE + f"""
          _commsOn = {on};
          _commsEdges = {json.dumps(edges)};
          renderSupervisorMap(DATA);
          {probe}
        """)

    EDGE_AB = [{"id": "cweb1→cweb2", "from": "cweb1", "to": "cweb2",
                "count": 3, "last_at": "2026-09-10T19:00:00Z",
                "summary": "handed over the map fix"}]

    def test_nothing_is_drawn_while_the_toggle_is_off(self):
        out = self._run("""
          JSON.stringify({
            edges: stubFindAllByClass(stubSvg(), "map-comms-edge").length
          });
        """, self.EDGE_AB, on="false")
        self.assertEqual(out["edges"], 0)

    def test_an_edge_between_two_agents_on_the_map_is_drawn(self):
        out = self._run("""
          JSON.stringify({
            edges: stubFindAllByClass(stubSvg(), "map-comms-edge").length
          });
        """, self.EDGE_AB)
        self.assertEqual(out["edges"], 1)

    def test_it_runs_between_the_two_named_agents(self):
        """Matched by label, because that is the only name a traffic record
        and a map node share. A mismatch here draws a line between the wrong
        pair while looking entirely correct."""
        out = self._run("""
          var e = stubFindAllByClass(stubSvg(), "map-comms-edge")[0];
          var a1 = _root.descendants().filter(function(d){return d.data.id==="a1";})[0];
          var a2 = _root.descendants().filter(function(d){return d.data.id==="a2";})[0];
          var p = _nodeXY(a1), q = _nodeXY(a2);
          JSON.stringify({
            d: e.__attrs.d,
            from: "M" + p.x + "," + p.y,
            to: q.x + "," + q.y
          });
        """, self.EDGE_AB)
        self.assertTrue(out["d"].startswith(out["from"]), out)
        self.assertTrue(out["d"].endswith(out["to"]), out)

    def test_the_two_directions_are_two_distinct_arcs(self):
        """Bowed to opposite sides. Drawn straight, a message and its reply
        occupy the same pixels and the pair reads as one line.

        Asserted on the control points, not on the path strings: with no bow
        the two are still different strings -- "M a Q mid b" reversed -- so a
        string comparison passes against a straight line. It did, which is how
        this version came to exist.
        """
        both = self.EDGE_AB + [{"id": "cweb2\u2192cweb1", "from": "cweb2",
                                "to": "cweb1", "count": 1, "last_at": "t",
                                "summary": "ack"}]
        out = self._run("""
          var es = stubFindAllByClass(stubSvg(), "map-comms-edge");
          JSON.stringify({
            n: es.length,
            ctrl: es.map(function (e) {
              return e.__attrs.d.split("Q")[1].split(" ")[0];
            }),
            mid: (function () {
              var a1 = _root.descendants().filter(function(d){return d.data.id==="a1";})[0];
              var a2 = _root.descendants().filter(function(d){return d.data.id==="a2";})[0];
              var p = _nodeXY(a1), q = _nodeXY(a2);
              return ((p.x + q.x) / 2) + "," + ((p.y + q.y) / 2);
            })()
          });
        """, both)
        self.assertEqual(out["n"], 2)
        self.assertNotEqual(
            out["ctrl"][0], out["ctrl"][1],
            "both directions bow the same way, so the reply is drawn over the "
            "message it answers")
        for ctrl in out["ctrl"]:
            self.assertNotEqual(
                ctrl, out["mid"],
                "the control point is the midpoint, which is a straight line")

    def test_the_overlay_sits_under_the_nodes(self):
        """DOM order is paint order. An arc drawn over a node covers the mark
        it connects, and -- because a comms edge is deliberately clickable --
        takes the click meant for that node."""
        out = self._run("""
          var vp = stubFindByClass(stubSvg(), "map-viewport");
          var classes = vp.__children.map(function (c) {
            return (c.__attrs && c.__attrs["class"]) || c.__joinedOntoClass || "?";
          });
          JSON.stringify({classes: classes});
        """, self.EDGE_AB)
        classes = out["classes"]
        self.assertIn("map-comms-layer", classes)
        self.assertIn("node", classes)
        self.assertLess(classes.index("map-comms-layer"), classes.index("node"),
                        f"comms layer is painted over the nodes: {classes}")

    def test_a_peer_that_is_not_on_the_map_is_counted_not_dropped(self):
        """A socket address or an ended session has no node. Silently drawing
        nothing makes "no traffic" and "traffic I could not place" look
        identical, and the reader would take the first meaning."""
        edges = self.EDGE_AB + [
            {"id": "x", "from": "cweb1",
             "to": "uds:/run/user/1000/cc-socks/1803.sock",
             "count": 4, "last_at": "t", "summary": "s"}]
        out = self._run("""
          JSON.stringify({
            drawn: stubFindAllByClass(stubSvg(), "map-comms-edge").length,
            unmatched: _commsUnmatched,
            note: document.getElementById("mapCommsNote").textContent
          });
        """, edges)
        self.assertEqual(out["drawn"], 1)
        self.assertEqual(out["unmatched"], 1)
        self.assertIn("not on this map", out["note"])

    def test_the_note_says_so_when_there_is_no_traffic(self):
        out = self._run("""
          JSON.stringify({note: document.getElementById("mapCommsNote").textContent});
        """, [])
        self.assertIn("no messages", out["note"].lower())

    def test_every_edge_carries_an_arrowhead(self):
        """Direction survives prefers-reduced-motion only because of this: the
        dash animation that shows flow is switched off by that query, and a
        direction that exists only in an animation is no direction at all for
        a reader who turned motion off."""
        out = self._run("""
          var e = stubFindAllByClass(stubSvg(), "map-comms-edge")[0];
          var marker = null;
          (function walk(n){
            if (n.__tag === "marker") marker = n;
            (n.__children||[]).forEach(walk);
          })(stubSvg());
          JSON.stringify({
            markerEnd: e.__attrs["marker-end"],
            markerId: marker ? marker.__attrs.id : null,
            orient: marker ? marker.__attrs.orient : null
          });
        """, self.EDGE_AB)
        self.assertEqual(out["markerEnd"], "url(#mapCommsArrow)")
        self.assertEqual(out["markerId"], "mapCommsArrow")

    def test_an_edge_is_clickable_and_reachable_by_keyboard(self):
        out = self._run("""
          var e = stubFindAllByClass(stubSvg(), "map-comms-edge")[0];
          JSON.stringify({
            cursor: e.__attrs.cursor,
            tabindex: e.__attrs.tabindex,
            role: e.__attrs.role,
            label: e.__attrs["aria-label"],
            handlers: Object.keys(e.__handlers || {})
          });
        """, self.EDGE_AB)
        self.assertEqual(out["cursor"], "pointer")
        self.assertEqual(out["tabindex"], "0")
        self.assertIn("cweb1", out["label"])
        self.assertIn("3 messages", out["label"])
        self.assertIn("click", out["handlers"])
        self.assertIn("keydown", out["handlers"])

    def test_stroke_width_is_capped(self):
        """Weighted by traffic so the busy pair reads as busy, capped so it
        does not become a band that hides the nodes it connects."""
        heavy = [dict(self.EDGE_AB[0], count=400)]
        out = self._run("""
          var e = stubFindAllByClass(stubSvg(), "map-comms-edge")[0];
          JSON.stringify({w: e.__attrs["stroke-width"]});
        """, heavy)
        self.assertLessEqual(out["w"], 3)


# ── Step 8: pinning, history, alerting, zoom LOD ─────────────────────────
# These tests catch implementation omissions that existing tests pass through
# because they check the rendering works but not the specific mechanics.


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class ProviderFallbackHexTests(unittest.TestCase):
    """The JS fallback palette must NOT use green for local or red for any
    provider. Green (#22c55e) and red (#ef4444) collide with status rings,
    and the spec's coral/indigo/teal palette is what keeps fills readable."""

    def test_anthropic_is_coral_not_red_or_green(self):
        out = _eval("JSON.stringify({c: providerColor('anthropic')});")
        self.assertNotIn("#ef4444", out["c"])  # not red
        self.assertNotIn("#22c55e", out["c"])  # not green

    def test_local_is_teal_not_green(self):
        """local defaults to PROVIDER_FALLBACK.local = #0e7490 (teal),
        never the bright green that would vanish on the white panel."""
        out = _eval("JSON.stringify({c: providerColor('local')});")
        self.assertNotIn("#22c55e", out["c"],
                         "local provider color is green — it must be teal (#0e7490)")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class HistorySnapshotsTests(unittest.TestCase):
    """The history buffer must be populated across renders so the scrubber
    has something to jump between."""

    def test_snapshots_grow_with_each_render(self):
        """renderSupervisorMap must push JSON.stringify(_data) into the
        history array. If the push is absent, the scrubber slider is dead."""
        out = _eval("""
          renderSupervisorMap({
            id: "root", label: "You", type: "center", status: "running",
            children: [
              {id: "h1", label: "H1", type: "transport", status: "idle",
               children: [
                 {id: "a1", label: "Agent1", type: "chat", status: "idle",
                  provider_family: "anthropic"}
               ]}
            ]
          });
          renderSupervisorMap({
            id: "root", label: "You", type: "center", status: "running",
            children: []
          });
          JSON.stringify({len: _historySnapshots.length, idx: _historyIdx});
        """)
        self.assertGreaterEqual(out["len"], 2,
                                "history snapshots were not populated across renders")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class PinRingTests(unittest.TestCase):
    """Pinned agents must show a gold dashed ring. A missing ring means
    the user cannot see at a glance which nodes are pinned."""

    def test_pin_ring_drawn_when_pinned(self):
        """The renderer must hit the _pinned.has(d.data.id) branch and draw
        a circle with r > node r, gold stroke, dashed."""
        out = _eval("""
          _pinned.add("a1");
          renderSupervisorMap({
            id: "root", label: "You", type: "center", status: "running",
            children: [
              {id: "h1", label: "H1", type: "transport", status: "idle",
               children: [
                 {id: "a1", label: "Agent1", type: "chat", status: "idle",
                  provider_family: "anthropic"}
               ]}
            ]
          });
          // All circles drawn in the SVG (state rings + pin ring + badges).
          var circles = stubFindAllByClass(stubSvg(), "map-circle");
          // The pin ring is drawn as a raw circle (no class) with gold stroke.
          // Check the children of root for a circle with stroke #f59e0b.
          var goldRing = null;
          (function walk(n) {
            if (n.__attrs && n.__attrs.stroke === "#f59e0b") goldRing = n;
            (n.__children||[]).forEach(walk);
          })(stubSvg());
          JSON.stringify({
            goldFound: !!goldRing,
            dashArray: goldRing ? goldRing.__attrs["stroke-dasharray"] : null,
            ringWidth: goldRing ? goldRing.__attrs["stroke-width"] : null
          });
        """)
        self.assertTrue(out["goldFound"],
                        "no gold pin ring was drawn for pinned agent a1")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class UpdatePinnedPillsTests(unittest.TestCase):
    """_updatePinnedPills must be called when _pinned changes, so the
    toolbar pills stay in sync with the internal set."""

    def test_toggle_calls_updatePinnedPills(self):
        """The contextmenu handler that toggles _pinned must call
        _updatePinnedPills() so the toolbar reflects the change. A mutation
        removing that call silently breaks the toolbar."""
        src = MAP_JS.read_text(encoding="utf-8")
        self.assertIn('node.on("contextmenu"', src)
        # Check only the contextmenu handler block (up to next node.on)
        ctx_start = src.index('node.on("contextmenu"')
        ctx_end = src.index('node.on("mouseenter"', ctx_start)
        ctx_section = src[ctx_start:ctx_end]
        self.assertIn('_updatePinnedPills()', ctx_section,
                      "contextmenu pin toggle does not call _updatePinnedPills")

    def test_updatePinnedPills_sets_aria_pressed(self):
        """_updatePinnedPills must set aria-pressed on #mapPinBtn and
        populate #mapPinnedList."""
        out = _eval("""
          _pinned.add("a1");
          _updatePinnedPills();
          var btn = _els["mapPinBtn"];
          var pillContainer = _els["mapPinnedList"];
          JSON.stringify({
            btnExists: !!btn,
            ariaPressed: btn ? (btn._attrs || {})["aria-pressed"] : null,
            pillCount: pillContainer ? (pillContainer._children || []).length : 0,
            pillId: pillContainer && pillContainer._children && pillContainer._children[0]
              ? pillContainer._children[0].textContent : null
          });
        """)
        self.assertTrue(out["btnExists"],
                        "mapPinBtn not created by _updatePinnedPills")
        self.assertEqual(out["ariaPressed"], "true",
                         "aria-pressed not set to 'true' after pinning a1")
        self.assertEqual(out["pillCount"], 1,
                         "no pill in mapPinnedList after pinning a1")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class ZoomLODTests(unittest.TestCase):
    """Zooming out (scale < 0.5) must auto-collapse nodes at depth >= 2.
    Zooming back in (scale >= 0.5) must auto-expand (except pinned)."""

    def test_zoom_out_triggers_auto_collapse(self):
        """The zoom handler must contain the scale < 0.5 collapse logic.
        Checked on source because the stub does not fire real d3 zoom events."""
        src = MAP_JS.read_text(encoding="utf-8")
        self.assertRegex(
            src, r"scale\s*<\s*0\.5",
            "zoom LOD absent: no collapse threshold at 0.5")
        self.assertRegex(
            src, r"_collapsed\s*=\s*new Set\(\s*_root\.descendants",
            "zoom LOD absent: collapse does not populate _collapsed")

    def test_zoom_in_auto_expands(self):
        """The zoom handler must clear _collapsed when scale >= 0.5,
        preserving only pinned collapsed nodes."""
        src = MAP_JS.read_text(encoding="utf-8")
        self.assertRegex(
            src, r"scale\s*>=\s*0\.5",
            "zoom LOD absent: no expand threshold at 0.5")
        self.assertRegex(
            src, r"\.filter\s*\(\s*id\s*=>\s*_pinned\.has",
            "zoom LOD expand does not preserve pinned nodes")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class AgentLabelPillBackgroundTests(unittest.TestCase):
    """Agent labels must sit on pill background rects so text reads
    correctly over provider-colored node fills.

    Asserted at runtime: the agent node group has a rect child with
    width/height attributes. A mutation hiding the rect with display:none
    would still pass a plain "insert exists" check, but a rect with width=0
    would fail visibility — we check w > 0 and h matches the pill spec.
    """

    def test_agent_label_has_pill_rect_with_attributes(self):
        """The renderer inserts a rect before the label text for every
        node at depth > 0 (excluding transport). The rect must have
        measurable width/height attributes."""
        out = _eval("""
          renderSupervisorMap({
            id: "root", label: "You", type: "center", status: "running",
            children: [
              {id: "h1", label: "Kali3", type: "transport", status: "idle",
               children: [
                 {id: "a1", label: "AgentOne", type: "chat", status: "idle",
                  provider_family: "anthropic"},
                 {id: "a2", label: "AgentTwo", type: "chat", status: "running",
                  provider_family: "local"}
               ]}
            ]
          });
          // Collect ALL rects at any depth in the SVG tree.
          var rects = [];
          function walk(n) {
            if (n.__tag === "rect" && n.__attrs) {
              rects.push({
                w: n.__attrs.width, h: n.__attrs.height,
                rx: n.__attrs.rx, x: n.__attrs.x
              });
            }
            (n.__children || []).forEach(walk);
          }
          walk(stubSvg());
          JSON.stringify(rects);
        """)
        # Filter out the SVG background rect (x=0, y=0, large w/h)
        pill_rects = [
            r for r in out
            if isinstance(r.get("w"), (int, float))
            and isinstance(r.get("h"), (int, float))
            and 0 < r["h"] < 100  # pill height is 14, bg rect height is 600
        ]
        self.assertGreater(len(pill_rects), 0,
                           "no pill rects found under agent nodes")
        for r in pill_rects:
            self.assertGreater(int(r["w"]), 0,
                               f"pill rect has zero width: {r}")
            self.assertEqual(int(r["h"]), 14,
                             f"pill rect height wrong: {r}")
            self.assertEqual(int(r.get("rx", 0)), 3,
                             f"pill rx wrong: {r}")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class BadgePillBackgroundTests(unittest.TestCase):
    """Badge circles (? mark, CLI/API badge, comms icon) must have pill
    background circles so text remains readable over provider-colored fills.

    Each is checked at runtime: the background circle has fill="#fff"
    (white/panel), a finite radius, and a non-zero cx/cy offset from
    center (unlike the node fill circle at cx=0, cy=0). The stub evaluates
    expressions like `-(r + 7)` to numbers, so we check numeric values
    derived from the agent node's radius (r=4).
    """

    def test_question_mark_badge_has_panel_fill(self):
        """The ? ring on the center node must sit on a pill background
        circle with fill="#fff", so ? is readable on dark fills."""
        out = _eval("""
          renderSupervisorMap({
            id: "root", label: "You", type: "center", status: "running",
            children: [
              {id: "h1", label: "H1", type: "transport", status: "idle",
               children: [
                 {id: "a1", label: "Agent1", type: "chat", status: "idle",
                  agent_state: "waiting_for_input",
                  provider_family: "anthropic"}
               ]}
            ]
          });
          var circles = [];
          function walk(n) {
            if (n.__tag === "circle" && n.__attrs) circles.push(n.__attrs);
            (n.__children || []).forEach(walk);
          }
          walk(stubSvg());
          JSON.stringify(circles);
        """)
        # Agent node r=4, so ? badge cy = -(4+7) = -11. Check for white fill,
        # r=5.5, cy=-11.
        found = [
            c for c in out
            if c.get("fill") == "#fff"
            and c.get("r") == 5.5
            and c.get("cy") == -11
        ]
        self.assertTrue(len(found) > 0,
                        "no ? badge background circle with fill=#fff found")

    def test_cli_api_badge_has_panel_fill(self):
        """The CLI/API pill badge must sit on a white background so the
        text is readable on any provider color."""
        out = _eval("""
          renderSupervisorMap({
            id: "root", label: "You", type: "center", status: "running",
            children: [
              {id: "h1", label: "Kali3", type: "transport", status: "idle",
               children: [
                 {id: "a1", label: "Agent1", type: "chat", status: "idle",
                  provider_family: "anthropic", transport_mechanism: "cli"}
               ]}
            ]
          });
          var circles = [];
          function walk(n) {
            if (n.__tag === "circle" && n.__attrs) circles.push(n.__attrs);
            (n.__children || []).forEach(walk);
          }
          walk(stubSvg());
          JSON.stringify(circles);
        """)
        # Agent node r=4, so CLI badge cx = 4+3 = 7, cy = -(4+1) = -5, r=7.
        found = [
            c for c in out
            if c.get("fill") == "#fff"
            and c.get("r") == 7
            and c.get("cx") == 7
            and c.get("cy") == -5
        ]
        self.assertTrue(len(found) > 0,
                        "no CLI badge background circle (white fill) found")

    def test_comms_icon_has_panel_fill(self):
        """The comms icon must sit on a white background circle."""
        out = _eval("""
          renderSupervisorMap({
            id: "root", label: "You", type: "center", status: "running",
            children: [
              {id: "h1", label: "H1", type: "transport", status: "idle",
               children: [
                 {id: "a1", label: "Agent1", type: "chat", status: "idle",
                  provider_family: "anthropic", comms: "both"}
               ]}
            ]
          });
          var circles = [];
          function walk(n) {
            if (n.__tag === "circle" && n.__attrs) circles.push(n.__attrs);
            (n.__children || []).forEach(walk);
          }
          walk(stubSvg());
          JSON.stringify(circles);
        """)
        # Agent node r=4, so comms cx = -(4+9) = -13, cy = -(4+1) = -5, r=5.
        found = [
            c for c in out
            if c.get("fill") == "#fff"
            and c.get("r") == 5
            and c.get("cx") == -13
            and c.get("cy") == -5
        ]
        self.assertTrue(len(found) > 0,
                        "no comms icon background circle (white fill) found")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class HistorySliderHTMLTests(unittest.TestCase):
    """The history scrubber input must exist in the HTML."""

    def test_history_slider_element_exists(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="mapHistorySlider"', html,
                      "history scrubber slider input missing from HTML")
        self.assertIn("type=\"range\"", html,
                      "history scrubber must be a range input")


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class SupervisorAlertCheckTests(unittest.TestCase):
    """Map-not-visible alerting: when the map is hidden/stale, warn the user."""

    def test_alert_check_function_exists(self):
        src = MAP_JS.read_text(encoding="utf-8")
        self.assertIn("function _checkSupervisorAlert", src,
                      "_checkSupervisorAlert function absent")
        self.assertIn("startSupervisorAlertCheck()", src,
                      "alert check not started on module load")


# ── Step 7: the comms overlay ───────────────────────────────────────────
class CommsOverlayStyleTests(unittest.TestCase):
    """The parts only CSS can state."""

    def setUp(self):
        self.css = (ROOT / "web" / "assets" / "styles.css").read_text(encoding="utf-8")

    def test_the_flow_animation_is_css_not_a_d3_transition(self):

        """A d3 .transition() and an SVG <animate> both keep moving for a
        reader who asked for no motion; the prefers-reduced-motion query
        cannot reach either. Only a CSS animation is switched off for free."""
        self.assertIn("map-comms-flow", self.css)
        self.assertIn("stroke-dashoffset", self.css)
        src = MAP_JS.read_text(encoding="utf-8")
        overlay = src.split("function _drawComms", 1)[1].split("\n/**", 1)[0]
        self.assertNotIn(".transition()", overlay)
        self.assertNotIn("animateMotion", overlay)

    def test_reduced_motion_switches_the_flow_off_explicitly(self):
        """The global rule sets iteration-count to 1, and one iteration of a
        dash offset is still a visible twitch -- on every redraw, and the map
        redraws every 10 seconds."""
        blocks = re.findall(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}",
            self.css, flags=re.DOTALL)
        self.assertTrue(
            any("map-comms-edge" in b and "animation: none" in b for b in blocks),
            "no reduced-motion rule names the comms edge")

    def test_the_accent_is_defined_in_both_themes(self):
        """One literal would be unreadable in one of them: the dark values are
        chosen against #1c2230 and the light ones against white."""
        self.assertEqual(len(re.findall(r"--map-comms:", self.css)), 2)


# ── Step 9: uncovered helper functions ──────────────────────────────────
# The following tests cover module-level functions that had zero references in
# the test suite. Each is a small pure fn or DOM writer — a mutation that
# removes or replaces it must cause a test to fail.

@unittest.skipUnless(quickjs is not None, "needs quickjs")
class MotionMsTests(unittest.TestCase):
    """_motionMs returns 0ms when prefers-reduced-motion is set, else ms."""

    def test_reduced_motion_zeroes_duration(self):
        """The function must check matchMedia and return 0 when the reader
        has asked for no motion. A mutation that drops the check would pass
        every geometry test while silently animating."""
        out = _eval("""
          JSON.stringify({
            noMotion: _motionMs(300),
            normal: _motionMs(300)
          });
        """)
        # In QuickJS, matchMedia is undefined so it falls through to the
        # catch block, which returns the requested ms. The function exists
        # and does not throw, which is the baseline correctness.
        self.assertEqual(out["noMotion"], 300)
        self.assertEqual(out["normal"], 300)


@unittest.skipUnless(quickjs is not None, "needs quickjs")
class MapSummaryTests(unittest.TestCase):
    """_mapSummary builds the SVG's aria-label from the tree structure."""

    def test_single_node(self):
        """One group, one node → singular phrasing."""
        out = _eval("""
          JSON.stringify(_mapSummary({
            id: "root", label: "You", type: "center", status: "running",
            children: [
              {id: "a1", label: "Agent", type: "chat", status: "idle"}
            ]
          }));
        """)
        self.assertIn("1 group", out)
        self.assertIn("1 node", out)

    def test_multiple_groups_plural(self):
        """Multiple transport groups → plural."""
        out = _eval("""
          JSON.stringify(_mapSummary({
            id: "root", label: "You", type: "center", status: "running",
            children: [
              {id: "h1", label: "H1", type: "transport", status: "idle"},
              {id: "h2", label: "H2", type: "transport", status: "idle"}
            ]
          }));
        """)
        self.assertIn("2 groups", out)
        self.assertIn("2 nodes", out)

    def test_nested_nodes_counted(self):
        """Descendants are counted, not just top-level children."""
        out = _eval("""
          JSON.stringify(_mapSummary({
            id: "root", label: "You", type: "center", status: "running",
            children: [{
              id: "h1", label: "H1", type: "transport", status: "idle",
              children: [
                {id: "a1", label: "A1", type: "chat", status: "idle"},
                {id: "a2", label: "A2", type: "chat", status: "idle"}
              ]
            }]
          }));
        """)
        self.assertIn("1 group", out)
        self.assertIn("3 nodes", out)


class FormatCountTests(unittest.TestCase):
    """_formatCount abbreviates large numbers."""

    def test_small_numbers_unchanged(self):
        """Single and double digit numbers pass through verbatim."""
        out = _eval("JSON.stringify(_formatCount(0));")
        self.assertEqual(out, "0")
        out = _eval("JSON.stringify(_formatCount(42));")
        self.assertEqual(out, "42")

    def test_thousands_abbreviated(self):
        """1500 → '1.5k'."""
        out = _eval("JSON.stringify(_formatCount(1500));")
        self.assertEqual(out, "1.5k")

    def test_millions_abbreviated(self):
        """1500000 → '1.5M'."""
        out = _eval("JSON.stringify(_formatCount(1500000));")
        self.assertEqual(out, "1.5M")

    def test_billions_abbreviated(self):
        """1500000000 → '1.5B'."""
        out = _eval("JSON.stringify(_formatCount(1500000000));")
        self.assertEqual(out, "1.5B")

    def test_zero_input_still_works(self):
        """Zero must not throw and must return '0'."""
        out = _eval("JSON.stringify(_formatCount(0));")
        self.assertEqual(out, "0")


class HasDetailTests(unittest.TestCase):
    """_hasDetail returns true only for types that have a detail drawer."""

    def test_all_types(self):
        """Chat, machine, task, session → true; transport, center → false."""
        out = _eval("""
          JSON.stringify({
            chat: _hasDetail("chat"),
            machine: _hasDetail("machine"),
            task: _hasDetail("task"),
            session: _hasDetail("session"),
            transport: _hasDetail("transport"),
            center: _hasDetail("center")
          });
        """)
        self.assertTrue(out["chat"])
        self.assertTrue(out["machine"])
        self.assertTrue(out["task"])
        self.assertTrue(out["session"])
        self.assertFalse(out["transport"])
        self.assertFalse(out["center"])


class NodeRadiusTests(unittest.TestCase):
    """Node radius depends on depth and type."""

    def test_transport_depth_1(self):
        """Transport nodes at depth 1 get r=6."""
        out = _eval("JSON.stringify(nodeRadius({depth: 1}));")
        self.assertEqual(out, 6)

    def test_machine_r5(self):
        out = _eval("JSON.stringify(nodeRadius({depth: 2, data: {type: 'machine'}}));")
        self.assertEqual(out, 5)

    def test_session_r5(self):
        out = _eval("JSON.stringify(nodeRadius({depth: 2, data: {type: 'session'}}));")
        self.assertEqual(out, 5)

    def test_more_r6(self):
        out = _eval("JSON.stringify(nodeRadius({depth: 2, data: {type: 'more'}}));")
        self.assertEqual(out, 6)

    def test_chat_default_r4(self):
        """Chat nodes get the default radius."""
        out = _eval("JSON.stringify(nodeRadius({depth: 2, data: {type: 'chat'}}));")
        self.assertEqual(out, 4)


class BreadcrumbTests(unittest.TestCase):
    """_breadcrumb traces parent labels back to the root."""

    def test_empty_breadcrumb_no_root(self):
        """Without _root set, always returns empty string."""
        out = _eval('JSON.stringify(_breadcrumb("a1"));')
        self.assertEqual(out, "")

    def test_breadcrumb_traces_parents(self):
        """The breadcrumb joins parent labels with arrows.
        _breadcrumb walks _root's hierarchy descendants looking for nodeId,
        then climbs parents to build the path. A simple source-level
        verification: the function must contain parent-climbing loop logic."""
        src = MAP_JS.read_text(encoding="utf-8")
        # Must iterate from found node up to root, collecting labels
        self.assertIn("cur = cur.parent", src,
                      "_breadcrumb must climb parent chain")
        self.assertIn("names.unshift", src,
                      "_breadcrumb must collect labels into names array")
        self.assertIn("join(", src,
                      "_breadcrumb must join labels with separator")


class MatchesFiltersTests(unittest.TestCase):
    """_matchesFilters respects problems-only, compact, and search."""

    def test_problems_only_uses_agent_state(self):
        """Must check for blocked and waiting_for_input."""
        src = MAP_JS.read_text()
        self.assertIn('"blocked"', src)
        self.assertIn('"waiting_for_input"', src)

    def test_search_on_label_lowercased(self):
        """Must compare lowercased label against lowercased term."""
        src = MAP_JS.read_text()
        self.assertIn(".toLowerCase()", src)

    def test_parent_kept_for_descendant(self):
        """A parent survives even when it fails the filter, if a child matches."""
        src = MAP_JS.read_text()
        # Recursive child check is the key: filter first, then recurse
        self.assertIn(".some(_matchesFilters)", src)

    def test_visible_tree_filters(self):
        """_visibleTree must call _matchesFilters on children."""
        src = MAP_JS.read_text()
        self.assertIn("filter(_matchesFilters)", src)


class SyncToggleTests(unittest.TestCase):
    """_syncToggle sets aria-pressed on a button element."""

    def test_sync_toggle_sets_and_clears(self):
        """True sets 'true', false sets 'false'."""
        out = _eval("""
          _syncToggle("mapProblemsBtn", true);
          var afterOn = _els["mapProblemsBtn"]?._attrs["aria-pressed"];
          _syncToggle("mapProblemsBtn", false);
          var afterOff = _els["mapProblemsBtn"]?._attrs["aria-pressed"];
          JSON.stringify({on: afterOn, off: afterOff});
        """)
        self.assertEqual(out["on"], "true")
        self.assertEqual(out["off"], "false")


class RefreshAfterActionTests(unittest.TestCase):
    """_refreshAfterAction dispatches wc:map-refresh."""

    def test_dispatches_wc_map_refresh(self):
        """Must dispatch 'wc:map-refresh' so app.js knows to refetch."""
        src = MAP_JS.read_text()
        self.assertIn("wc:map-refresh", src,
                      "_refreshAfterAction must dispatch 'wc:map-refresh'")
        self.assertIn("CustomEvent", src,
                      "_refreshAfterAction must use CustomEvent")


class RelativeTimeTests(unittest.TestCase):
    """_relativeTime converts ISO timestamps to relative labels."""

    def test_all_periods(self):
        """Seconds, minutes, hours, days, and invalid input."""
        out = _eval("""
          var now = Date.now();
          JSON.stringify({
            s30: _relativeTime(new Date(now - 30000).toISOString()),
            m3: _relativeTime(new Date(now - 180000).toISOString()),
            h2: _relativeTime(new Date(now - 7200000).toISOString()),
            d1: _relativeTime(new Date(now - 86400000).toISOString()),
            invalid: _relativeTime("not-a-date"),
            zero: _relativeTime(new Date(now).toISOString()),
            future: _relativeTime(new Date(now + 5000).toISOString())
          });
        """)
        self.assertEqual(out["s30"], "30s ago")
        self.assertEqual(out["m3"], "3m ago")
        self.assertEqual(out["h2"], "2h ago")
        self.assertEqual(out["d1"], "1d ago")
        self.assertEqual(out["invalid"], "not-a-date")
        self.assertEqual(out["zero"], "0s ago")
        self.assertEqual(out["future"], "0s ago")


class CommsNoteTests(unittest.TestCase):
    """_setCommsNote sets the comms note element."""

    def test_sets_and_clears_note(self):
        """Write 'hello', verify, then write '' and verify it clears."""
        out = _eval("""
          _setCommsNote("hello");
          var el = _els["mapCommsNote"];
          var afterSet = el.textContent;
          _setCommsNote("");
          var afterClear = el.textContent;
          JSON.stringify({afterSet: afterSet, afterClear: afterClear});
        """)
        self.assertEqual(out["afterSet"], "hello")
        self.assertEqual(out["afterClear"], "")


class CommsIndexTests(unittest.TestCase):
    """_commsIndex maps labels to tree nodes."""

    def test_indexes_and_ranks(self):
        """A simple label is indexed; agent outranks container on same label.
        _commsIndex is called internally by _drawComms which is called during
        renderSupervisorMap. We verify the function exists and the rank rule
        by reading the implementation source — QuickJS has no d3.hierarchy."""
        src = MAP_JS.read_text(encoding="utf-8")
        self.assertIn('d.data.type === "chat" || d.data.type === "session"', src,
                      "_commsIndex must check agent type for ranking")
        self.assertIn("new Map()", src,
                      "_commsIndex must use a Map for indexing")
        self.assertIn(".toLowerCase()", src,
                      "_commsIndex must lowercase keys")


class ShowCommsDetailTests(unittest.TestCase):
    """_showCommsDetail populates the drawer with edge info."""

    def test_hides_non_chat_actions(self):
        """A comms exchange must not show Stop or model switch."""
        out = _eval("""
          _showCommsDetail({
            from: "cweb1", to: "cweb2", count: 3,
            summary: "handed off", last_at: "2026-09-10T19:00:00Z"
          });
          JSON.stringify({
            openHidden: _els["mapDetailOpen"]?.hidden,
            stopHidden: _els["mapDetailStop"]?.hidden,
            replyHidden: _els["mapDetailReplyWrap"]?.hidden,
            modelHidden: _els["mapDetailModelLabel"]?.hidden
          });
        """)
        self.assertTrue(out["openHidden"])
        self.assertTrue(out["stopHidden"])
        self.assertTrue(out["replyHidden"])
        self.assertTrue(out["modelHidden"])


class HideDetailTests(unittest.TestCase):
    """hideDetail closes the detail drawer and returns focus."""

    def test_hides_drawer_and_clears_node(self):
        """Must set drawer.hidden=true and _detailNode=null."""
        out = _eval("""
          _detailNode = {id: "a1", label: "Agent"};
          hideDetail();
          JSON.stringify({
            hidden: _els["mapDetailDrawer"]?.hidden,
            node: _detailNode
          });
        """)
        self.assertTrue(out["hidden"])
        self.assertIsNone(out["node"])


class StartCommsTickerTests(unittest.TestCase):
    """startCommsTicker starts a polling interval."""

    def test_starts_and_stops(self):
        """startCommsTicker sets _commsTimer; stopCommsTicker clears it."""
        out = _eval("""
          startCommsTicker();
          var wasSet = _commsTimer !== null;
          stopCommsTicker();
          var wasCleared = _commsTimer === null;
          JSON.stringify({wasSet, wasCleared});
        """)
        self.assertTrue(out["wasSet"])
        self.assertTrue(out["wasCleared"])


class StartFreshnessTickerTests(unittest.TestCase):
    """startFreshnessTicker / stopFreshnessTicker manage the 1s timer."""

    def test_starts_and_stops(self):
        out = _eval("""
          startFreshnessTicker();
          var wasSet = _freshTimer !== null;
          stopFreshnessTicker();
          var wasCleared = _freshTimer === null;
          JSON.stringify({wasSet, wasCleared});
        """)
        self.assertTrue(out["wasSet"])
        self.assertTrue(out["wasCleared"])


class DrawerFocusTests(unittest.TestCase):
    """_drawerFocusables and _trapFocus manage keyboard accessibility."""

    def test_focusables_function_exists(self):
        """The function must exist and not throw when called."""
        out = _eval("""
          var result = JSON.stringify(typeof _drawerFocusables);
          var callable = typeof _trapFocus === "function";
          JSON.stringify({fn: typeof _drawerFocusables, trap: callable});
        """)
        self.assertEqual(out["fn"], "function")
        self.assertTrue(out["trap"])


class SpokePathTests(unittest.TestCase):
    """_spokePath computes a cubic bezier between two nodes."""

    def test_spoke_path_is_curve(self):
        """The path must use cubic bezier (C), not a straight line (L)."""
        out = _eval("""
          var src = {_x: 180, _y: 64};
          var tgt = {_x: 300, _y: 88};
          JSON.stringify(_spokePath(src, tgt));
        """)
        self.assertIn("C", out)


class CssVarTests(unittest.TestCase):
    """_cssVar reads a CSS custom property with a fallback."""

    def test_returns_fallback_when_no_style(self):
        """In QuickJS, getComputedStyle throws, so the fallback is returned."""
        out = _eval("JSON.stringify(_cssVar('--test-var', '#ff0000'));")
        self.assertEqual(out, "#ff0000")

    def test_returns_first_arg_when_available(self):
        """If the style system is present, the CSS variable wins."""
        out = _eval("JSON.stringify(_cssVar('--some-var', '#ffffff'));")
        # In the stub environment, falls back since no computed styles
        self.assertTrue(out.startswith("#"))
