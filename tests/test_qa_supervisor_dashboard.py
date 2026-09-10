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
import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

try:  # pragma: no cover - reported as a skip
    import quickjs  # noqa: E402
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
