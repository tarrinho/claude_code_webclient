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
