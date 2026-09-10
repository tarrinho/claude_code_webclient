"""QA: the Statistics page's figures mean what its labels say.

Three defects, all measured against the live table on 2026-09-10, all fixed
together because they are the same failure seen from three angles: a chart that
sums a column without asking what the column means.

  * The first two charts grouped by `usage_events.provider`, which four code
    paths write with four different meanings. 131,314 of 131,557 rows said
    "cli" -- the transcript importer's hardcoded literal -- while two web paths
    wrote raw machine-provider values ("anthropic", "anthropic-compatible") and
    only the remainder carried a `shared.backend_kind` display kind. The chart
    named four display kinds and fell back to the raw string, so 99.9% of the
    data rendered in a series labelled after an implementation detail.

  * The by-model chart could not be reconciled against the gateway's own
    figures for three separate reasons: 86.6% of the charted total (6.30B of
    7.27B tokens) was re-counted context on `context_unsplit` rows, which the
    Usage tab subtracts and the charts did not; 25.78B cache-read tokens were
    charted nowhere; and one model was drawn as two, because
    `nvidia/Qwen3.6-35B-A3B-NVFP4` and `vllm/Qwen3.6-35B-A3B-NVFP4` are the
    same weights reached two ways.

  * Nothing said which agent spent it, though 131,314 rows carry a session id.

The billing route is not recoverable from a transcript -- checked across 120 of
them, every assistant record, seven models: `service_tier` is "standard" on all
of them, `quotaLimits` is absent from all, `cache_read_input_tokens` present on
all. So it is recorded going forward by the sites that resolve a backend, and
inferred from the model id for older rows, and the two are kept
distinguishable. That distinction is what several tests below pin.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db
from routes.db_usage import (
    GATEWAY,
    SUBSCRIPTION,
    UNCLASSIFIED,
    billing_route_from_machine,
    billing_route_of,
    normalise_model_id,
)


class RouteClassificationTests(unittest.TestCase):
    """`billing_route_of`: a record beats a guess, and a guess says so."""

    def test_a_stored_route_wins_and_is_not_inferred(self):
        """The whole point of the column. A site that resolved the backend
        knows; nothing downstream may second-guess it from a model name."""
        self.assertEqual(
            billing_route_of(SUBSCRIPTION, "vllm/Qwen3.6-35B-A3B-NVFP4"),
            (SUBSCRIPTION, False),
        )

    def test_a_gateway_prefix_is_inferred_as_gateway(self):
        for model in ("vllm/Qwen3.6-35B-A3B-NVFP4",
                      "nvidia/Qwen3.6-35B-A3B-NVFP4",
                      "azure_ai/gpt-5.4-mini",
                      "Qwen/Qwen3.5-0.8B"):
            with self.subTest(model=model):
                self.assertEqual(billing_route_of("", model), (GATEWAY, True))

    def test_a_bare_openai_family_id_is_gateway(self):
        """`gpt-5.6-luna` arrives with no prefix, and no Anthropic
        subscription serves an OpenAI-family id."""
        self.assertEqual(billing_route_of("", "gpt-5.6-luna"), (GATEWAY, True))

    def test_a_bare_claude_id_is_the_subscription(self):
        for model in ("claude-opus-5", "claude-sonnet-5",
                      "claude-haiku-4-5-20251001"):
            with self.subTest(model=model):
                self.assertEqual(
                    billing_route_of("", model), (SUBSCRIPTION, True))

    def test_a_gateway_served_claude_is_gateway_not_subscription(self):
        """`azure_ai/claude-opus-4-8-corporate` is billed by the gateway.

        What protects this is the anchor on the subscription pattern, not the
        order of the two checks: a prefixed id cannot match `^claude-` at all.
        Confirmed by swapping the two branches, which changed no result --
        this docstring previously claimed the ordering was load-bearing, and
        the mutation proved it was not.
        """
        self.assertEqual(
            billing_route_of("", "azure_ai/claude-opus-4-8-corporate"),
            (GATEWAY, True),
        )

    def test_an_unknown_id_is_unclassified_rather_than_assumed(self):
        """An empty bucket is the desired state, not a reason to fold the
        row into whichever side looks plausible. A new gateway model must
        appear as unattributed rather than inflate the subscription."""
        self.assertEqual(
            billing_route_of("", "something-nobody-has-seen"),
            (UNCLASSIFIED, True),
        )

    def test_an_empty_model_is_unclassified(self):
        self.assertEqual(billing_route_of("", ""), (UNCLASSIFIED, True))
        self.assertEqual(billing_route_of(None, None), (UNCLASSIFIED, True))

    def test_every_provider_value_in_the_live_table_classifies(self):
        """The five values actually on disk, so this cannot pass against a
        vocabulary the database does not use."""
        for model in ("nvidia/Qwen3.6-35B-A3B-NVFP4", "claude-opus-5",
                      "gpt-5.6-luna", "Qwen/Qwen3.5-0.8B", "claude-sonnet-5"):
            with self.subTest(model=model):
                route, inferred = billing_route_of("", model)
                self.assertIn(route, (SUBSCRIPTION, GATEWAY))
                self.assertTrue(inferred)


class RouteFromMachineTests(unittest.TestCase):
    """`billing_route_from_machine`: the base URL decides, because on this
    deployment nothing else can -- the official API and the gateway both
    carry provider "claude_code"."""

    def test_no_machine_records_nothing(self):
        """"" means "not recorded", which leaves the row to be inferred at
        read time like any historical one. Guessing here would write a guess
        into the column that exists to hold facts."""
        self.assertEqual(billing_route_from_machine(None), "")

    def test_no_base_url_is_the_subscription(self):
        """"No base URL" means the official API -- the same rule
        backend_env.deltas applies when it removes an inherited one."""
        self.assertEqual(
            billing_route_from_machine({"provider": "claude_code"}),
            SUBSCRIPTION,
        )

    def test_the_official_api_is_the_subscription(self):
        self.assertEqual(
            billing_route_from_machine({"base_url": "https://api.anthropic.com"}),
            SUBSCRIPTION,
        )

    def test_any_other_host_is_the_gateway(self):
        self.assertEqual(
            billing_route_from_machine(
                {"base_url": "https://llm.ai-machine.cfappsecurity.com"}),
            GATEWAY,
        )

    def test_the_provider_column_does_not_decide(self):
        """Both real backends carry provider "claude_code" and differ only in
        their base URL, so a rule reading `provider` separates nothing."""
        gateway = {"provider": "claude_code",
                   "base_url": "https://llm.ai-machine.cfappsecurity.com"}
        official = {"provider": "claude_code",
                    "base_url": "https://api.anthropic.com"}
        self.assertNotEqual(
            billing_route_from_machine(gateway),
            billing_route_from_machine(official),
        )

    def test_a_host_merely_ending_in_the_name_is_not_anthropic(self):
        """`notanthropic.com` must not pass as the official API: the check is
        on a domain boundary, not a suffix."""
        self.assertEqual(
            billing_route_from_machine({"base_url": "https://notanthropic.com"}),
            GATEWAY,
        )

    def test_a_subdomain_of_anthropic_is_the_subscription(self):
        self.assertEqual(
            billing_route_from_machine({"base_url": "https://eu.anthropic.com/v1"}),
            SUBSCRIPTION,
        )

    def test_a_port_and_credentials_do_not_confuse_it(self):
        self.assertEqual(
            billing_route_from_machine(
                {"base_url": "https://user@gateway.example:8443/v1"}),
            GATEWAY,
        )


class ModelNormalisationTests(unittest.TestCase):

    def test_the_same_model_under_two_prefixes_becomes_one_name(self):
        self.assertEqual(
            normalise_model_id("nvidia/Qwen3.6-35B-A3B-NVFP4"),
            normalise_model_id("vllm/Qwen3.6-35B-A3B-NVFP4"),
        )

    def test_a_bare_id_is_left_alone(self):
        self.assertEqual(normalise_model_id("claude-opus-5"), "claude-opus-5")

    def test_a_renamed_model_stays_two_models(self):
        """`vllm/Qwen3.5-0.8` became `Qwen/Qwen3.5-0.8B` on the gateway. They
        are almost certainly the same weights, and collapsing them would mean
        guessing that two ids differing in a size suffix are one thing --
        which is the class of inference that put the wrong number on this page
        to begin with."""
        self.assertNotEqual(
            normalise_model_id("vllm/Qwen3.5-0.8"),
            normalise_model_id("Qwen/Qwen3.5-0.8B"),
        )

    def test_an_empty_id_does_not_raise(self):
        self.assertEqual(normalise_model_id(None), "")


class _UsageFixture(unittest.IsolatedAsyncioTestCase):
    """A throwaway database with rows shaped like the real ones."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        patcher.start()
        self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _row(self, **kwargs):
        """Insert one usage row, defaulting everything the test does not set."""
        fields = {
            "chat_id": "c1", "owner_id": "admin", "model": "claude-opus-5",
            "provider": "cli", "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "context_unsplit": 0, "session_id": "", "billing_route": "",
            "created_at": db._now(), "origin": "web",
        }
        fields.update(kwargs)
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        await db.db_conn.execute(
            f"INSERT INTO usage_events ({columns}) VALUES ({marks})",  # nosec B608
            list(fields.values()),
        )
        await db.db_conn.commit()


class BillingRouteColumnTests(_UsageFixture):

    async def test_usage_record_persists_the_route_it_is_given(self):
        await db.usage_record(
            "c1", "admin", "claude-opus-5", "through_claude_code",
            input_tokens=10, billing_route=SUBSCRIPTION,
        )
        cur = await db.db_conn.execute(
            "SELECT billing_route FROM usage_events LIMIT 1")
        self.assertEqual((await cur.fetchone())["billing_route"], SUBSCRIPTION)

    async def test_a_caller_that_does_not_know_stores_nothing(self):
        """Empty, not a guess -- so `billing_route_of` can tell that this row
        needs classifying and the chart can say it was."""
        await db.usage_record("c1", "admin", "claude-opus-5", "cli",
                              input_tokens=10)
        cur = await db.db_conn.execute(
            "SELECT billing_route FROM usage_events LIMIT 1")
        self.assertEqual((await cur.fetchone())["billing_route"], "")

    async def test_the_migration_does_not_backfill(self):
        """Running init again must not fill the column with inferences: it
        holds recorded facts, and a backfill would erase the distinction the
        legend reports."""
        await self._row(model="claude-opus-5", input_tokens=5)
        await db._ensure_usage_columns()
        cur = await db.db_conn.execute(
            "SELECT billing_route FROM usage_events LIMIT 1")
        self.assertEqual((await cur.fetchone())["billing_route"], "")


class SeriesByRouteTests(_UsageFixture):

    async def test_rows_are_grouped_by_route_not_provider(self):
        """Both rows carry provider "cli" -- the value 99.9% of the table
        has -- so a grouping that still keyed on provider would return one
        series here instead of two."""
        await self._row(model="claude-opus-5", provider="cli", input_tokens=100)
        await self._row(model="vllm/Qwen3.6-35B", provider="cli", input_tokens=200)

        series = await db.usage_series("admin", None, "day")

        by_route = {row["route"]: row for row in series}
        self.assertEqual(set(by_route), {SUBSCRIPTION, GATEWAY})
        self.assertEqual(by_route[SUBSCRIPTION]["billable_input"], 100)
        self.assertEqual(by_route[GATEWAY]["billable_input"], 200)

    async def test_a_recorded_route_beats_the_model_id(self):
        await self._row(model="vllm/Qwen3.6-35B", billing_route=SUBSCRIPTION,
                        input_tokens=7)
        series = await db.usage_series("admin", None, "day")
        self.assertEqual([row["route"] for row in series], [SUBSCRIPTION])
        self.assertEqual(series[0]["inferred_requests"], 0)

    async def test_inferred_turns_are_counted_separately(self):
        """The number the legend renders. Without it the chart cannot say how
        much of itself is a guess, which is the whole reason the column and
        the classifier are kept apart."""
        await self._row(model="claude-opus-5", input_tokens=1)
        await self._row(model="claude-opus-5", billing_route=SUBSCRIPTION,
                        input_tokens=1)
        series = await db.usage_series("admin", None, "day")
        row = next(r for r in series if r["route"] == SUBSCRIPTION)
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["inferred_requests"], 1)

    async def test_recounted_context_is_excluded_from_billable_input(self):
        """86.6% of this table's headline was re-counted context. It is
        excluded per row, not per model: one unsplit row must not remove a
        normal row's tokens, and vice versa."""
        await self._row(model="claude-opus-5", input_tokens=1000,
                        context_unsplit=1)
        await self._row(model="claude-opus-5", input_tokens=10)

        series = await db.usage_series("admin", None, "day")
        row = series[0]

        self.assertEqual(row["billable_input"], 10)
        self.assertEqual(row["unsplit_tokens"], 1000)
        self.assertEqual(row["requests"], 2)

    async def test_cache_creation_counts_as_billable_input(self):
        """Writing the cache is charged at full rate; reading it is not."""
        await self._row(model="claude-opus-5", input_tokens=10,
                        cache_creation_tokens=5, cache_read_tokens=90)
        row = (await db.usage_series("admin", None, "day"))[0]
        self.assertEqual(row["billable_input"], 15)
        self.assertEqual(row["cache_read"], 90)

    async def test_cache_reads_are_reported_rather_than_dropped(self):
        """25.78B tokens were charted nowhere at all before this."""
        await self._row(model="claude-opus-5", cache_read_tokens=25_000)
        self.assertEqual(
            (await db.usage_series("admin", None, "day"))[0]["cache_read"],
            25_000,
        )

    async def test_another_owners_rows_are_not_counted(self):
        await self._row(owner_id="someone-else", input_tokens=999)
        self.assertEqual(await db.usage_series("admin", None, "day"), [])


class ModelSeriesTests(_UsageFixture):

    async def test_two_prefixes_of_one_model_become_one_series(self):
        await self._row(model="nvidia/Qwen3.6-35B", input_tokens=100)
        await self._row(model="vllm/Qwen3.6-35B", input_tokens=50)

        rows = await db.usage_model_series("admin", None, "day")

        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["model"], "Qwen3.6-35B")
        self.assertEqual(rows[0]["billable_input"], 150)

    async def test_the_merged_ids_come_back_for_the_tooltip(self):
        """A merge nobody can see is a merge nobody can check."""
        await self._row(model="nvidia/Qwen3.6-35B", input_tokens=1)
        await self._row(model="vllm/Qwen3.6-35B", input_tokens=1)
        rows = await db.usage_model_series("admin", None, "day")
        self.assertEqual(
            sorted(rows[0]["ids"]), ["nvidia/Qwen3.6-35B", "vllm/Qwen3.6-35B"])

    async def test_ranking_uses_the_charted_basis_not_the_raw_total(self):
        """With re-counted context included, one unsplit model outranked
        everything and the ordering described the defect rather than the
        usage. `big` has 100x the raw input and nothing chartable."""
        await self._row(model="big-unsplit", input_tokens=100_000,
                        context_unsplit=1)
        await self._row(model="small-real", input_tokens=1_000)

        rows = await db.usage_model_series("admin", None, "day", top=1)

        kept = {row["model"] for row in rows if row["model"] != "Other"}
        self.assertEqual(kept, {"small-real"})

    async def test_models_past_the_cut_merge_into_other(self):
        for index in range(4):
            await self._row(model=f"model-{index}", input_tokens=100 - index)
        rows = await db.usage_model_series("admin", None, "day", top=2)
        names = {row["model"] for row in rows}
        self.assertIn("Other", names)
        self.assertEqual(len(names), 3)


class AgentSeriesTests(_UsageFixture):

    async def test_spend_is_grouped_by_session(self):
        """131,314 of 131,557 rows carry a session id and only 15,885 carry a
        chat id, so the session is what a run's spend belongs to."""
        await self._row(session_id="s-1", input_tokens=100)
        await self._row(session_id="s-2", input_tokens=25)

        rows = await db.usage_agent_series("admin", None, "day")

        by_agent = {row["agent_id"]: row["billable_input"] for row in rows}
        self.assertEqual(by_agent, {"s-1": 100, "s-2": 25})

    async def test_a_row_with_no_session_falls_back_to_its_chat(self):
        await self._row(chat_id="c-9", session_id="", input_tokens=5)
        rows = await db.usage_agent_series("admin", None, "day")
        self.assertEqual([row["agent_id"] for row in rows], ["c-9"])

    async def test_the_session_wins_when_a_row_has_both(self):
        await self._row(chat_id="c-9", session_id="s-9", input_tokens=5)
        rows = await db.usage_agent_series("admin", None, "day")
        self.assertEqual([row["agent_id"] for row in rows], ["s-9"])

    async def test_agents_past_the_cut_merge_into_other(self):
        """The chart's total must still equal the page's total, so the tail
        is merged rather than dropped."""
        for index in range(4):
            await self._row(session_id=f"s-{index}", input_tokens=100 - index)

        rows = await db.usage_agent_series("admin", None, "day", top=2)

        total = sum(row["billable_input"] for row in rows)
        self.assertEqual(total, 100 + 99 + 98 + 97)
        self.assertIn("Other", {row["agent_id"] for row in rows})

    async def test_a_name_comes_from_the_conversation_when_there_is_one(self):
        await db.chat_create(
            "c-named", "cweb2 - supervisor plan", None, "/tmp", "admin")
        await db.db_conn.execute(
            "UPDATE chats SET session_id = 's-named' WHERE id = 'c-named'")
        await db.db_conn.commit()

        names = await db.usage_agent_names("admin", ["s-named", "s-unknown"])

        self.assertEqual(names.get("s-named"), "cweb2 - supervisor plan")
        self.assertNotIn("s-unknown", names)

    async def test_another_owners_conversation_does_not_name_an_agent(self):
        await db.chat_create(
            "c-theirs", "Their private title", None, "/tmp", "someone-else")
        await db.db_conn.execute(
            "UPDATE chats SET session_id = 's-theirs' WHERE id = 'c-theirs'")
        await db.db_conn.commit()
        self.assertEqual(await db.usage_agent_names("admin", ["s-theirs"]), {})


class SeriesEndpointTests(_UsageFixture):
    """GET /api/usage/series -- what the four charts are actually served."""

    def _request(self, query=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/api/usage/series"),
            cookies={}, headers={"accept": "*/*"},
            query_params=dict(query or {}),
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={}),
        )

    async def _payload(self, **query):
        import json as _json

        from routes.misc import handle_usage_series_get
        # The CLI import pass reads transcripts off disk and has nothing to do
        # with what this endpoint returns; patched out so the test measures
        # the response rather than the filesystem.
        with patch("routes.misc._import_cli_usage", new=AsyncMock()):
            response = await handle_usage_series_get(self._request(query))
        return _json.loads(bytes(response.body))

    async def test_the_payload_carries_every_chart_it_draws(self):
        await self._row(model="claude-opus-5", session_id="s-1", input_tokens=10)
        body = await self._payload(days="30", bucket="day")
        for key in ("series", "models", "agents", "unsplit_tokens", "spine"):
            with self.subTest(key=key):
                self.assertIn(key, body)

    async def test_cost_survives_on_the_subscription(self):
        """The old rule blanked cost unless `provider` was
        "through_claude_code" -- and the importer writes "cli", so a
        subscription turn from a terminal had its cost silently removed."""
        await self._row(model="claude-opus-5", provider="cli",
                        input_tokens=10, cost_usd=0.25)
        body = await self._payload(days="30", bucket="day")
        row = next(r for r in body["series"] if r["route"] == SUBSCRIPTION)
        self.assertEqual(row["cost_usd"], 0.25)

    async def test_cost_is_blanked_on_the_gateway(self):
        """Claude Code prices every turn with Anthropic's rates, so a
        gateway's figure is arithmetic on the wrong number."""
        await self._row(model="vllm/Qwen3.6-35B", provider="cli",
                        input_tokens=10, cost_usd=0.25)
        body = await self._payload(days="30", bucket="day")
        row = next(r for r in body["series"] if r["route"] == GATEWAY)
        self.assertIsNone(row["cost_usd"])

    async def test_agents_are_named_for_the_legend(self):
        await db.chat_create(
            "c-named", "cweb2 - supervisor plan", None, "/tmp", "admin")
        await db.db_conn.execute(
            "UPDATE chats SET session_id = 's-named' WHERE id = 'c-named'")
        await db.db_conn.commit()
        await self._row(session_id="s-named", input_tokens=10)

        body = await self._payload(days="30", bucket="day")

        self.assertEqual(body["agents"][0]["name"], "cweb2 - supervisor plan")

    async def test_an_unnamed_agent_falls_back_to_a_short_id(self):
        """A session with no conversation is still spend somebody made, and a
        legend entry reading "undefined" is worse than a truncated id."""
        await self._row(session_id="0123456789abcdef", input_tokens=10)
        body = await self._payload(days="30", bucket="day")
        self.assertEqual(body["agents"][0]["name"], "01234567")

    async def test_the_excluded_context_is_reported_not_hidden(self):
        """86.6% of this table's raw total. Excluding it silently would be
        the same class of error as including it silently."""
        await self._row(model="claude-opus-5", input_tokens=5000,
                        context_unsplit=1)
        body = await self._payload(days="30", bucket="day")
        self.assertEqual(body["unsplit_tokens"], 5000)


if __name__ == "__main__":
    unittest.main()
