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
