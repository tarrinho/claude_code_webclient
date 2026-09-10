"""QA: transport server stats — parsed correctly, attributed, kept apart.

The collection half of this feature was already running when this file was
written, and had been since the day it was wired up. `tunnel_manager.start`
passes `db.system_sample_insert` down to `tunnel_manager_health.collect_stats`,
which polls every connected transport over its live SSH connection. Two things
made it worthless:

* `collect_stats` produced ``{"cpu": "49.9", "disk": "45%", "mem": "84.2",
  "load": "0.5 0.4 0.3"}`` while `system_sample_insert` writes columns named
  ``cpu_pct``/``disk_pct``/``mem_pct``/``load1..15`` and defaults anything it
  is not given to 0. Every transport sample was therefore a row of zeros.
* `system_samples` carries no host in its older columns, and the readers were
  unscoped, so those zero rows went into the local host's own series. 146 of
  14,987 rows were all-zero when this was found, the newest three a second
  apart -- one per connected transport.

So the local Server tab was averaging in zeros from machines it was not
describing, and no transport's real figures existed anywhere. The tests below
pin the three fixes: parse, attribute, and scope.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
from tunnel_manager_health import _one_number, parse_stats


class NumberParsingTests(unittest.TestCase):
    """`_one_number`, where the "no reading" case lives."""

    def test_a_plain_percentage(self):
        self.assertEqual(_one_number("84.2"), 84.2)

    def test_a_trailing_percent_sign(self):
        """`df -h` prints "45%"."""
        self.assertEqual(_one_number("45%"), 45.0)

    def test_a_unit_suffix(self):
        """`top` prints the CPU figure as "12.5%us" on some hosts."""
        self.assertEqual(_one_number("12.5%us"), 12.5)

    def test_surrounding_whitespace(self):
        self.assertEqual(_one_number("  7.0  "), 7.0)

    def test_an_integer(self):
        self.assertEqual(_one_number("3"), 3.0)

    def test_the_error_sentinel_is_not_a_reading(self):
        """collect_stats writes "ERROR" when the exec fails. Reading that as
        0 is what made a broken SSH exec look like an idle machine."""
        self.assertIsNone(_one_number("ERROR"))

    def test_empty_is_not_a_reading(self):
        self.assertIsNone(_one_number(""))
        self.assertIsNone(_one_number("   "))

    def test_text_is_not_a_reading(self):
        self.assertIsNone(_one_number("command not found"))

    def test_zero_is_a_reading(self):
        """The distinction that matters: a real 0% is a reading, and must not
        be confused with the absence of one."""
        self.assertEqual(_one_number("0"), 0.0)
        self.assertEqual(_one_number("0.0"), 0.0)


class StatsMappingTests(unittest.TestCase):
    """`parse_stats`: shell labels onto column names."""

    RAW = {
        "cpu": "12.5",
        "mem": "84.2",
        "disk": "45%",
        "load": "0.52 0.41 0.38",
    }

    def test_every_label_reaches_its_column(self):
        self.assertEqual(parse_stats(self.RAW), {
            "cpu_pct": 12.5,
            "mem_pct": 84.2,
            "disk_pct": 45.0,
            "load1": 0.52,
            "load5": 0.41,
            "load15": 0.38,
        })

    def test_the_column_names_are_the_ones_the_writer_stores(self):
        """The bug in one assertion: the keys have to be SYSTEM_FIELDS, or
        the writer's `values.get(field, 0)` returns 0 for all of them."""
        from routes.db_usage import SYSTEM_FIELDS
        for column in parse_stats(self.RAW):
            with self.subTest(column=column):
                self.assertIn(column, SYSTEM_FIELDS)

    def test_a_failed_metric_is_omitted_not_zeroed(self):
        raw = dict(self.RAW, cpu="ERROR")
        parsed = parse_stats(raw)
        self.assertNotIn("cpu_pct", parsed)
        self.assertEqual(parsed["mem_pct"], 84.2)

    def test_a_short_loadavg_fills_what_it_has(self):
        """Positional by design, so a truncated read gives fewer fields
        rather than misaligned ones."""
        parsed = parse_stats({"load": "0.52"})
        self.assertEqual(parsed, {"load1": 0.52})

    def test_an_absent_loadavg_yields_no_load_columns(self):
        parsed = parse_stats({"cpu": "1.0"})
        self.assertEqual(parsed, {"cpu_pct": 1.0})

    def test_everything_failing_yields_nothing(self):
        """Which is what lets the caller decline to store the sample at all
        instead of recording the machine as idle."""
        self.assertEqual(
            parse_stats({"cpu": "ERROR", "mem": "ERROR", "disk": "ERROR",
                         "load": "ERROR"}),
            {},
        )

    def test_a_genuinely_idle_host_still_stores(self):
        """The other side of the previous test: all-zero readings are real
        and must not be mistaken for a failed collection."""
        parsed = parse_stats({"cpu": "0.0", "mem": "0.0", "disk": "0%",
                              "load": "0.00 0.00 0.00"})
        self.assertEqual(len(parsed), 6)
        self.assertEqual(set(parsed.values()), {0.0})


class SampleAttributionTests(unittest.IsolatedAsyncioTestCase):
    """Which machine a stored sample describes, and who reads it back."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_a_sample_defaults_to_this_host(self):
        """Every caller that predates the arguments means the local host, and
        the columns were added with these same defaults -- so the 14,987 rows
        already on disk stay correctly labelled without a migration."""
        await db.system_sample_insert({"cpu_pct": 5.0})
        row = await db.system_latest()
        self.assertEqual(row["host_type"], "local")
        self.assertEqual(row["host_id"], "local")

    async def test_a_transport_sample_records_which_transport(self):
        await db.system_sample_insert(
            {"cpu_pct": 12.5}, host_type="transport", host_id="t-kali3",
        )
        rows = await db.system_latest_by_host()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host_id"], "t-kali3")
        self.assertEqual(rows[0]["cpu_pct"], 12.5)

    async def test_a_transport_sample_is_not_this_hosts_latest(self):
        """The contamination, in one assertion. system_latest was unscoped,
        and the transport poller writes more often than the local sampler --
        so the number the Server tab showed was usually a transport's."""
        await db.system_sample_insert({"cpu_pct": 5.0})
        await db.system_sample_insert(
            {"cpu_pct": 99.0}, host_type="transport", host_id="t-kali3",
        )
        row = await db.system_latest()
        self.assertEqual(row["cpu_pct"], 5.0)
        self.assertEqual(row["host_id"], "local")

    async def test_a_transport_sample_is_not_in_this_hosts_series(self):
        await db.system_sample_insert({"cpu_pct": 10.0})
        await db.system_sample_insert(
            {"cpu_pct": 0.0}, host_type="transport", host_id="t-kali3",
        )
        series = await db.system_series(days=None, bucket="day")
        self.assertTrue(series)
        # One local sample at 10.0; averaging the transport's 0.0 in would
        # halve it, which is exactly what the graphs were doing.
        self.assertEqual(series[-1]["cpu_pct"], 10.0)
        self.assertEqual(series[-1]["samples"], 1)

    async def test_the_local_host_is_not_in_the_by_host_list(self):
        """That list feeds a per-transport table; the local host has its own
        panel and would read as a transport called "local"."""
        await db.system_sample_insert({"cpu_pct": 5.0})
        self.assertEqual(await db.system_latest_by_host(), [])

    async def test_each_transport_appears_once_with_its_newest_sample(self):
        for cpu in (1.0, 2.0, 3.0):
            await db.system_sample_insert(
                {"cpu_pct": cpu}, host_type="transport", host_id="t-a",
            )
        await db.system_sample_insert(
            {"cpu_pct": 9.0}, host_type="transport", host_id="t-b",
        )
        rows = await db.system_latest_by_host()
        self.assertEqual(len(rows), 2)
        by_host = {r["host_id"]: r["cpu_pct"] for r in rows}
        self.assertEqual(by_host, {"t-a": 3.0, "t-b": 9.0})

    async def test_nothing_stored_reads_as_nothing(self):
        self.assertIsNone(await db.system_latest())
        self.assertEqual(await db.system_latest_by_host(), [])


class CollectorStorageTests(unittest.IsolatedAsyncioTestCase):
    """What `collect_stats` hands the writer."""

    async def test_an_unreadable_collection_stores_nothing(self):
        """Rather than a row of zeros that reads as a healthy idle host."""
        from tunnel_manager_health import collect_stats
        import tunnel_manager

        calls = []

        async def store(values, **kwargs):
            calls.append((values, kwargs))

        with patch.dict(
            tunnel_manager._STATE,
            {"m1": {"ssh_client": object()}}, clear=False,
        ), patch(
            "tunnel_manager_ssh.exec_command",
            side_effect=OSError("channel closed"),
        ):
            await collect_stats("m1", store_fn=store)

        self.assertEqual(calls, [])

    async def test_a_machine_with_no_transport_id_still_gets_attributed(self):
        """Falling back to the machine id keeps the sample attributable rather
        than dropping it, which is the safer direction of the two."""
        from tunnel_manager_health import collect_stats
        import tunnel_manager

        calls = []

        async def store(values, **kwargs):
            calls.append(kwargs)

        class _Out:
            def read(self):
                return b"1.0"

        async def fake_exec(machine_id, cmd, timeout=5):
            return None, _Out(), None

        with patch.dict(
            tunnel_manager._STATE, {"m9": {"ssh_client": object()}}, clear=False,
        ), patch("tunnel_manager_ssh.exec_command", fake_exec):
            await collect_stats("m9", store_fn=store)

        self.assertEqual(calls, [{"host_type": "transport", "host_id": "m9"}])

    async def test_a_readable_collection_is_stored_attributed(self):
        from tunnel_manager_health import collect_stats
        import tunnel_manager

        calls = []

        async def store(values, **kwargs):
            calls.append((values, kwargs))

        class _Out:
            def __init__(self, text):
                self._text = text

            def read(self):
                return self._text.encode()

        # Matches the command order in collect_stats: cpu, disk, disk_bytes, mem,
        # mem_bytes, swap, load, cores, uptime, hostname, kernel.
        _expected = [
            "12.5",          # cpu
            "45%",           # disk
            "0",             # disk_bytes  (single value → _split_bytes fails)
            "84.2",          # mem
            "0",             # mem_bytes   (single value → _split_bytes fails)
            "5.0",           # swap
            "0.52 0.41 0.38", # load
            "0.0",           # cores
            "0.0",           # uptime
            "0.0",           # hostname
            "0.0",           # kernel
        ]
        _reply_idx = [0]

        async def fake_exec(machine_id, cmd, timeout=5):
            if _reply_idx[0] < len(_expected):
                text = _expected[_reply_idx[0]]
                _reply_idx[0] += 1
                return None, _Out(text), None
            return None, _Out("0.0"), None

        with patch.dict(
            tunnel_manager._STATE,
            {"m1": {"ssh_client": object(), "transport_id": "t-kali3"}},
            clear=False,
        ), patch("tunnel_manager_ssh.exec_command", fake_exec):
            await collect_stats("m1", store_fn=store)

        self.assertEqual(len(calls), 1)
        values, kwargs = calls[0]
        # The transport, not the machine: two machines share Kali3, and keying
        # on the machine would store one host's readings twice per interval.
        self.assertEqual(
            kwargs, {"host_type": "transport", "host_id": "t-kali3"})
        self.assertEqual(values["cpu_pct"], 12.5)
        self.assertEqual(values["disk_pct"], 45.0)
        self.assertEqual(values["load1"], 0.52)


class PerHostSeriesTests(unittest.IsolatedAsyncioTestCase):
    """`system_series_by_host`: one grouped query for every transport's
    history, keyed by transport, so the Server page can draw a chart each
    without a request per host."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        patcher.start()
        self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_each_transport_gets_its_own_key(self):
        await db.system_sample_insert(
            {"cpu_pct": 10.0}, host_type="transport", host_id="t-a")
        await db.system_sample_insert(
            {"cpu_pct": 90.0}, host_type="transport", host_id="t-b")

        series = await db.system_series_by_host(days=None, bucket="day")

        self.assertEqual(set(series), {"t-a", "t-b"})
        self.assertEqual(series["t-a"][-1]["cpu_pct"], 10.0)
        self.assertEqual(series["t-b"][-1]["cpu_pct"], 90.0)

    async def test_the_local_host_is_not_among_them(self):
        """It has its own series and its own charts; including it here would
        draw the console's host twice under a transport heading."""
        await db.system_sample_insert({"cpu_pct": 5.0})
        self.assertEqual(await db.system_series_by_host(days=None), {})

    async def test_the_columns_match_the_local_series(self):
        """The charts are the same charts, so the rows must have the same
        shape -- a second, subtly different aggregation would render two
        graphs that look alike and mean different things."""
        await db.system_sample_insert({"cpu_pct": 1.0})
        await db.system_sample_insert(
            {"cpu_pct": 1.0}, host_type="transport", host_id="t-a")

        local = await db.system_series(days=None, bucket="day")
        remote = await db.system_series_by_host(days=None, bucket="day")

        for field in ("bucket", "samples", "cpu_pct", "cpu_max", "mem_pct",
                      "mem_max", "disk_pct", "disk_pct_max", "load1",
                      "load1_max", "load5", "load15"):
            with self.subTest(field=field):
                self.assertIn(field, local[-1])
                self.assertIn(field, remote["t-a"][-1])

    async def test_no_transport_history_is_an_empty_mapping(self):
        self.assertEqual(await db.system_series_by_host(days=None), {})

    async def test_load_is_divided_by_the_hosts_own_core_count(self):
        """The number that makes four machines comparable on one chart.

        A load of 4 is idle on a 16-core box and a queue on a 2-core one, so
        the raw figure puts them on incomparable scales. The division happens
        in SQL because the core count is per host and the average is per
        bucket.
        """
        await db.system_sample_insert(
            {"load1": 4.0, "cores": 8}, host_type="transport", host_id="t-big")
        await db.system_sample_insert(
            {"load1": 4.0, "cores": 2}, host_type="transport", host_id="t-small")

        series = await db.system_series_by_host(days=None, bucket="day")

        self.assertEqual(series["t-big"][-1]["load_per_core"], 0.5)
        self.assertEqual(series["t-small"][-1]["load_per_core"], 2.0)
        # The raw load is still there: the tables print it, and dividing is a
        # presentation choice that must not destroy the measurement.
        self.assertEqual(series["t-big"][-1]["load1"], 4.0)

    async def test_an_unknown_core_count_yields_no_figure_at_all(self):
        """cores = 0 is "we do not know" -- every row written before the
        column existed says that. A zero denominator must produce NULL, not
        an error and not a made-up 1, so the chart can leave the host out
        rather than plot a figure nobody measured."""
        await db.system_sample_insert(
            {"load1": 3.0, "cores": 0}, host_type="transport", host_id="t-nocore")

        series = await db.system_series_by_host(days=None, bucket="day")

        self.assertIsNone(series["t-nocore"][-1]["load_per_core"])
        self.assertEqual(series["t-nocore"][-1]["load1"], 3.0)

    async def test_the_local_series_computes_it_the_same_way(self):
        """Both series feed one chart, so a difference here would be two
        lines meaning different things on one pair of axes."""
        await db.system_sample_insert({"load1": 2.0, "cores": 4})
        local = await db.system_series(days=None, bucket="day")
        self.assertEqual(local[-1]["load_per_core"], 0.5)
        self.assertEqual(local[-1]["cores"], 4)


class SpineAlignmentTests(unittest.TestCase):
    """`align_hosts_to_spine`: every host on one x-axis, gaps as nulls.

    Pure function, tested without a database: what it has to get right is the
    filler value, and a zero there would draw a confident idle machine across
    exactly the windows nobody sampled -- the same defect the collector's
    unmapped keys produced, arriving by a different route.
    """

    def test_a_missing_bucket_is_filled_with_null_not_zero(self):
        aligned = db.align_hosts_to_spine(
            {"t-a": [{"bucket": "B", "samples": 2, "cpu_pct": 40.0}]},
            ["A", "B", "C"],
        )
        rows = aligned["t-a"]
        self.assertEqual([r["bucket"] for r in rows], ["A", "B", "C"])
        self.assertIsNone(rows[0]["cpu_pct"])
        self.assertIsNone(rows[2]["cpu_pct"])
        self.assertEqual(rows[1]["cpu_pct"], 40.0)
        # samples is a count of what was stored, so an empty bucket is 0 of
        # them -- that one is a real zero rather than a missing reading.
        self.assertEqual(rows[0]["samples"], 0)

    def test_every_host_ends_up_on_the_same_axis(self):
        aligned = db.align_hosts_to_spine(
            {
                "t-a": [{"bucket": "A", "samples": 1, "cpu_pct": 1.0}],
                "t-b": [{"bucket": "C", "samples": 1, "cpu_pct": 2.0}],
            },
            ["A", "B", "C"],
        )
        self.assertEqual([r["bucket"] for r in aligned["t-a"]], ["A", "B", "C"])
        self.assertEqual([r["bucket"] for r in aligned["t-b"]], ["A", "B", "C"])

    def test_a_bucket_off_the_spine_is_kept_not_dropped(self):
        """A reading is never discarded for failing to line up."""
        aligned = db.align_hosts_to_spine(
            {"t-a": [{"bucket": "D", "samples": 1, "cpu_pct": 9.0}]},
            ["A", "B"],
        )
        self.assertEqual([r["bucket"] for r in aligned["t-a"]], ["A", "B", "D"])
        self.assertEqual(aligned["t-a"][-1]["cpu_pct"], 9.0)

    def test_an_empty_spine_changes_nothing(self):
        rows = {"t-a": [{"bucket": "A", "samples": 1, "cpu_pct": 1.0}]}
        self.assertEqual(db.align_hosts_to_spine(rows, []), rows)


class SystemEndpointTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/system carries the table's rows.

    On this route rather than a new one because the Statistics tab needs
    stored values, not a collection pass -- and this endpoint is already the
    one that answers "how is the hardware doing".
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        import auth
        import secrets
        self.password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(self.password))

    def _client(self):
        from fastapi.testclient import TestClient
        from app import app as web_app
        client = TestClient(
            web_app, raise_server_exceptions=False, base_url="https://testserver")
        resp = client.post(
            "/login", json={"username": "admin", "password": self.password})
        self.assertEqual(resp.status_code, 200, "the fixture must log in")
        return client

    async def test_a_transport_with_no_sample_is_listed_with_no_readings(self):
        """Absent from the table and non-existent are indistinguishable, and
        the second is the reassuring reading -- so every transport is listed
        whether or not anything has been sampled from it."""
        await db.ssh_transport_create(
            "t-1", "Kali3", "admin", "kali-3.example", "kali", "~/.ssh/id_ed25519",
        )
        body = self._client().get("/api/system").json()

        rows = {r["id"]: r for r in body["transports"]}
        self.assertIn("t-1", rows)
        self.assertEqual(rows["t-1"]["name"], "Kali3")
        self.assertIsNone(rows["t-1"]["cpu_pct"])
        self.assertIsNone(rows["t-1"]["sampled_at"])

    async def test_a_stored_sample_reaches_the_payload(self):
        await db.ssh_transport_create(
            "t-1", "Kali3", "admin", "kali-3.example", "kali", "~/.ssh/id_ed25519",
        )
        await db.system_sample_insert(
            {"cpu_pct": 12.5, "mem_pct": 38.0, "disk_pct": 8.0, "load1": 0.4},
            host_type="transport", host_id="t-1",
        )
        body = self._client().get("/api/system").json()

        row = next(r for r in body["transports"] if r["id"] == "t-1")
        self.assertEqual(row["cpu_pct"], 12.5)
        self.assertEqual(row["mem_pct"], 38.0)
        self.assertEqual(row["disk_pct"], 8.0)
        self.assertEqual(row["load1"], 0.4)
        self.assertTrue(row["sampled_at"])

    async def test_no_transports_is_an_empty_list_not_an_error(self):
        body = self._client().get("/api/system").json()
        self.assertEqual(body["transports"], [])

    async def test_the_series_endpoint_carries_every_transport(self):
        """One request for the whole page: the local series and every
        transport's, so drawing four charts costs no extra round trips."""
        await db.ssh_transport_create(
            "t-1", "Kali3", "admin", "kali-3.example", "kali", "~/.ssh/id_ed25519",
        )
        await db.system_sample_insert({"cpu_pct": 5.0})
        await db.system_sample_insert(
            {"cpu_pct": 42.0}, host_type="transport", host_id="t-1")

        body = self._client().get("/api/system/series?days=1&bucket=day").json()

        self.assertIn("series", body)
        self.assertIn("transports", body)
        self.assertIn("t-1", body["transports"])
        self.assertEqual(body["transports"]["t-1"][-1]["cpu_pct"], 42.0)
        # The local series must not have the transport's reading in it.
        self.assertEqual(body["series"][-1]["cpu_pct"], 5.0)

    async def test_the_transports_come_back_on_the_local_buckets(self):
        """One chart per metric now, so the hosts share an x-axis and have to
        agree on what the nth point means. Only the local series is
        continuous -- a transport is sampled while its tunnel is up -- so the
        response aligns them, filling with nulls so a disconnect breaks the
        line instead of drawing a zero."""
        import datetime
        await db.ssh_transport_create(
            "t-1", "Kali3", "admin", "kali-3.example", "kali", "~/.ssh/id_ed25519",
        )
        now = datetime.datetime.now(datetime.UTC)
        older = (now - datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Two local buckets, one transport bucket inside them.
        await db.db_conn.execute(
            "INSERT INTO system_samples (created_at,host_type,host_id,cpu_pct) "
            "VALUES (?,'local','local',5.0)", (older,))
        await db.db_conn.commit()
        await db.system_sample_insert({"cpu_pct": 6.0})
        await db.system_sample_insert(
            {"cpu_pct": 42.0}, host_type="transport", host_id="t-1")

        body = self._client().get("/api/system/series?days=7&bucket=day").json()

        local = [row["bucket"] for row in body["series"]]
        remote = body["transports"]["t-1"]
        self.assertGreater(len(local), 1, "the fixture needs two local buckets")
        self.assertEqual([row["bucket"] for row in remote], local)
        # The bucket it did not report in is a hole, not a reading of zero.
        self.assertIsNone(remote[0]["cpu_pct"])
        self.assertEqual(remote[-1]["cpu_pct"], 42.0)

    async def test_the_local_snapshot_still_comes_back(self):
        """The addition must not displace what this route already served."""
        body = self._client().get("/api/system").json()
        self.assertIn("sample_interval_s", body)
        self.assertIn("retention_days", body)


if __name__ == "__main__":
    unittest.main()
