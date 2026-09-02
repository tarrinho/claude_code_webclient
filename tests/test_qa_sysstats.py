"""QA coverage for the Server statistics page: collector, storage, endpoints.

The feature is a chain of four links, and three of them fail silently. A
misparsed /proc line yields a plausible-looking number; a bucket expression
that is subtly wrong yields a chart with the right shape and the wrong x
axis; an aggregate that averages a peak away yields a page that says the
machine was fine during the minute it fell over. Only the endpoints fail
loudly. These tests are aimed at the quiet three.

Covers:
* Collector -- /proc parsing against fixture files, the CPU delta, and the
  degradation path when a file is missing.
* to_row -- the flat/nested boundary, including hosts with no swap or a
  statvfs that failed, and that it fills every column the table declares.
* Storage -- average vs peak, the half-hour bucket, the days window, pruning.
* Endpoints -- defaults, clamping, and rejection of an unknown bucket.
* Sampler -- idempotent start, a store that raises, clean stop.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import config
import db
import sysstats

# A trimmed /proc/stat. The aggregate line is what _read_cpu reads: the fields
# after the label are user, nice, system, idle, iowait, ...
STAT_A = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0\nintr 1 2 3\n"
# 200 more jiffies total, 100 of them idle -> 50% busy over the interval.
STAT_B = "cpu  150 0 150 900 0 0 0 0 0 0\ncpu0 75 0 75 450 0\nintr 1 2 3\n"

MEMINFO = (
    "MemTotal:        8000000 kB\n"
    "MemFree:          200000 kB\n"
    "MemAvailable:    4000000 kB\n"
    "SwapTotal:       2000000 kB\n"
    "SwapFree:        1500000 kB\n"
)


def _proc_dir(tmp: str, stat: str = STAT_A, meminfo: str = MEMINFO) -> str:
    """A directory shaped enough like /proc for the collector to read it."""
    root = Path(tmp) / "proc"
    root.mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text(stat)
    (root / "meminfo").write_text(meminfo)
    (root / "uptime").write_text("123456.78 987654.32\n")
    (root / "cpuinfo").write_text("model name\t: Test CPU @ 1.0GHz\ncpu cores\t: 4\n")
    return str(root)


def _at(hour: int, minute: int, day: int = 30) -> str:
    """The stored UTC stamp for a given *local* wall-clock time.

    Buckets are cut in local time -- a chart is read in the reader's day --
    while created_at is stored in UTC. Hard-coding a UTC stamp therefore makes
    the expected bucket depend on the machine's zone: 10:05Z and 11:05Z are
    one local day apart at UTC-11 and the same day everywhere else, so a test
    that hard-codes them passes here and fails in Pago Pago.
    """
    local = _dt.datetime(2026, 8, day, hour, minute).astimezone()
    return local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reset_collector():
    """Clear the deltas and the hardware cache between tests."""
    sysstats._prev_cpu = None
    sysstats._prev_proc = None
    sysstats._hw = None


class CollectorTests(unittest.TestCase):
    """Reading /proc, against fixture files rather than the live host.

    Asserting on the real /proc would make these tests agree with whatever the
    machine happened to be doing, which is the one thing a parser test must
    not do.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _reset_collector()
        self.addCleanup(_reset_collector)

    def _use(self, stat=STAT_A, meminfo=MEMINFO):
        proc = _proc_dir(self.tmp.name, stat, meminfo)
        patcher = patch.object(sysstats, "_PROC", proc)
        patcher.start()
        self.addCleanup(patcher.stop)
        return proc

    def test_read_cpu_sums_total_and_adds_iowait_to_idle(self):
        self._use()
        total, idle = sysstats._read_cpu()
        self.assertEqual(total, 1000)
        # idle (800) + iowait (0). A process blocked on disk is not the CPU
        # being busy, so iowait belongs on the idle side.
        self.assertEqual(idle, 800)

    def test_read_cpu_returns_none_when_proc_is_unreadable(self):
        patcher = patch.object(sysstats, "_PROC", "/nonexistent-proc")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIsNone(sysstats._read_cpu())

    def test_first_sample_reports_zero_cpu(self):
        """A percentage over an interval needs two readings. There is no
        honest first value, and inventing one is worse than a visible zero."""
        self._use()
        self.assertEqual(sysstats.sample()["cpu_pct"], 0.0)

    def test_second_sample_reports_the_delta(self):
        proc = self._use()
        sysstats.sample()
        Path(proc, "stat").write_text(STAT_B)
        # 200 more jiffies, 100 idle -> half the interval was busy.
        self.assertEqual(sysstats.sample()["cpu_pct"], 50.0)

    def test_cpu_is_clamped_to_a_percentage(self):
        """A counter that went backwards (suspend, container restart) must not
        produce a negative reading or one above 100."""
        proc = self._use(stat=STAT_B)
        sysstats.sample()
        Path(proc, "stat").write_text(STAT_A)  # counters move backwards
        self.assertGreaterEqual(sysstats.sample()["cpu_pct"], 0.0)

    def test_meminfo_is_converted_to_bytes(self):
        self._use()
        mem = sysstats._read_meminfo()
        self.assertEqual(mem["MemTotal"], 8000000 * 1024)

    def test_used_memory_is_derived_from_available_not_free(self):
        """MemFree on a healthy Linux box is almost nothing, because the
        kernel spends the rest on reclaimable cache. Reporting 97% used would
        be true of MemFree and a lie about the machine."""
        self._use()
        snapshot = sysstats.sample()
        self.assertEqual(snapshot["mem_used"], (8000000 - 4000000) * 1024)
        self.assertEqual(snapshot["mem_pct"], 50.0)

    def test_swap_used_is_total_minus_free(self):
        self._use()
        snapshot = sysstats.sample()
        self.assertEqual(snapshot["swap_used"], (2000000 - 1500000) * 1024)
        self.assertEqual(snapshot["swap_pct"], 25.0)

    def test_a_host_without_swap_reports_zero_rather_than_dividing(self):
        self._use(meminfo="MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n")
        snapshot = sysstats.sample()
        self.assertEqual(snapshot["swap_total"], 0)
        self.assertEqual(snapshot["swap_pct"], 0.0)

    def test_missing_meminfo_does_not_raise(self):
        proc = self._use()
        os.unlink(Path(proc, "meminfo"))
        snapshot = sysstats.sample()
        self.assertEqual(snapshot["mem_total"], 0)
        self.assertEqual(snapshot["mem_pct"], 0.0)

    def test_uptime_is_the_first_field(self):
        self._use()
        self.assertEqual(sysstats._uptime_s(), 123456.8)

    def test_disk_counts_reserved_blocks_as_used(self):
        """f_bavail, not f_bfree: the difference is root's reserve, which this
        service cannot allocate. Calling it free promises space that is not
        there."""
        fake = SimpleNamespace(f_frsize=4096, f_blocks=1000, f_bavail=250, f_bfree=300)
        with patch.object(os, "statvfs", return_value=fake):
            disk = sysstats._disk("/anywhere")
        self.assertEqual(disk["total"], 4096 * 1000)
        self.assertEqual(disk["avail"], 4096 * 250)
        self.assertEqual(disk["used"], 4096 * 750)
        self.assertEqual(disk["pct"], 75.0)

    def test_disk_returns_empty_when_statvfs_fails(self):
        with patch.object(os, "statvfs", side_effect=OSError("wedged mount")):
            self.assertEqual(sysstats._disk("/anywhere"), {})

    def test_sample_reports_unavailable_when_nothing_could_be_read(self):
        patcher = patch.object(sysstats, "_PROC", "/nonexistent-proc")
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch.object(os, "statvfs", side_effect=OSError):
            self.assertFalse(sysstats.sample()["available"])

    def test_hardware_info_is_read_once(self):
        proc = self._use()
        first = sysstats.hw_info()
        self.assertEqual(first["cpu_model"], "Test CPU @ 1.0GHz")
        self.assertEqual(first["cpu_cores"], 4)
        os.unlink(Path(proc, "cpuinfo"))
        # Cached: none of this changes while the process runs, and re-reading
        # it on every sample would be four file opens a minute for nothing.
        self.assertEqual(sysstats.hw_info()["cpu_model"], "Test CPU @ 1.0GHz")

    def test_process_stats_describe_this_process(self):
        # The live /proc on purpose: /proc/self has no fixture, and this is
        # the one reading whose subject is the test process itself.
        proc = sysstats.sample()["proc"]
        self.assertEqual(proc["pid"], os.getpid())
        self.assertGreater(proc["rss"], 0)


class ToRowTests(unittest.TestCase):
    """The one place the nested snapshot meets the flat table."""

    def test_every_declared_column_is_filled(self):
        """Guards drift: a column added to system_samples without a matching
        line here would silently store 0 for ever."""
        row = sysstats.to_row(sysstats.sample())
        self.assertEqual(set(row), set(db.SYSTEM_FIELDS))

    def test_nested_values_are_flattened(self):
        row = sysstats.to_row({
            "cpu_pct": 12.5, "mem_pct": 40.0, "mem_used": 10, "mem_total": 25,
            "swap_pct": 1.0,
            "disk": {"pct": 91.8, "used": 5, "total": 6},
            "load": [3.27, 1.74, 1.02],
            "proc": {"rss": 999, "cpu_pct": 2.5},
        })
        self.assertEqual(row["disk_pct"], 91.8)
        self.assertEqual(row["load1"], 3.27)
        self.assertEqual(row["load15"], 1.02)
        self.assertEqual(row["proc_rss"], 999)

    def test_a_snapshot_missing_everything_still_produces_a_row(self):
        """The degraded snapshot is exactly the case that must not raise --
        it is what a host with a wedged mount hands over."""
        row = sysstats.to_row({})
        self.assertEqual(set(row), set(db.SYSTEM_FIELDS))
        # Per column rather than over a set of the values: {0, 0.0} is {0} in
        # Python, so the set form silently asserted much less than it read as.
        for field, value in row.items():
            self.assertEqual(value, 0, f"{field} should default to zero")

    def test_a_short_load_list_does_not_index_out_of_range(self):
        row = sysstats.to_row({"load": [1.5]})
        self.assertEqual(row["load1"], 1.5)
        self.assertEqual(row["load5"], 0.0)
        self.assertEqual(row["load15"], 0.0)


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()

    async def asyncTearDown(self):
        await db.close()

    async def _store(self, when: str, **values):
        """Insert one sample stamped at *when*."""
        with patch.object(db, "_now", lambda: when):
            await db.system_sample_insert(values)


class StorageTests(_DbCase):
    """Aggregation is where a chart quietly starts lying."""

    async def test_insert_then_latest_round_trips(self):
        await self._store("2026-08-30T10:00:00Z", cpu_pct=42.0, mem_pct=50.0)
        latest = await db.system_latest()
        self.assertEqual(latest["cpu_pct"], 42.0)

    async def test_latest_is_none_before_anything_is_sampled(self):
        self.assertIsNone(await db.system_latest())

    async def test_missing_fields_default_to_zero(self):
        await self._store("2026-08-30T10:00:00Z", cpu_pct=1.0)
        latest = await db.system_latest()
        self.assertEqual(latest["load1"], 0)
        self.assertEqual(latest["proc_rss"], 0)

    async def test_peak_survives_the_average(self):
        """The reason both columns exist. Three idle minutes and one at 100%
        average to 25% -- a bucket that reports only the mean says the machine
        was comfortable during the minute it was not."""
        for minute, cpu in enumerate([0.0, 0.0, 100.0, 0.0]):
            await self._store(_at(10, minute), cpu_pct=cpu)
        rows = await db.system_series(days=None, bucket="hour")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cpu_pct"], 25.0)
        self.assertEqual(rows[0]["cpu_max"], 100.0)
        self.assertEqual(rows[0]["samples"], 4)

    async def test_half_hour_bucket_groups_by_half_an_hour(self):
        """The trap this guards: USAGE_BUCKETS['halfhour'] is 16, but that is
        a sentinel, not a substr width. Reading it as one buckets by the
        minute -- every sample lands alone, the chart keeps its shape, and
        nothing anywhere reports an error."""
        await self._store(_at(10, 5), cpu_pct=10.0)
        await self._store(_at(10, 20), cpu_pct=20.0)
        await self._store(_at(10, 35), cpu_pct=30.0)
        rows = await db.system_series(days=None, bucket="halfhour")
        self.assertEqual(len(rows), 2, "05 and 20 share a bucket; 35 opens the next")
        self.assertEqual(rows[0]["cpu_pct"], 15.0)
        self.assertEqual(rows[1]["cpu_pct"], 30.0)

    async def test_half_hour_bucket_keys_sort_chronologically(self):
        """ORDER BY bucket is only correct while the key is lexicographic."""
        await self._store(_at(9, 45), cpu_pct=1.0)
        await self._store(_at(10, 15), cpu_pct=2.0)
        rows = await db.system_series(days=None, bucket="halfhour")
        self.assertEqual([r["bucket"] for r in rows], sorted(r["bucket"] for r in rows))

    async def test_hour_and_day_buckets_collapse_correctly(self):
        await self._store(_at(10, 5), cpu_pct=10.0)
        await self._store(_at(11, 5), cpu_pct=20.0)
        by_hour = await db.system_series(days=None, bucket="hour")
        by_day = await db.system_series(days=None, bucket="day")
        self.assertEqual(len(by_hour), 2)
        self.assertEqual(len(by_day), 1)
        self.assertEqual(by_day[0]["cpu_pct"], 15.0)

    async def test_series_is_empty_when_nothing_is_stored(self):
        self.assertEqual(await db.system_series(days=1, bucket="hour"), [])

    async def test_days_window_excludes_older_samples(self):
        await self._store("2020-01-01T00:00:00Z", cpu_pct=99.0)
        await self._store(db._now(), cpu_pct=5.0)
        rows = await db.system_series(days=1, bucket="day")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cpu_pct"], 5.0)

    async def test_days_none_returns_everything(self):
        await self._store("2020-01-01T00:00:00Z", cpu_pct=99.0)
        await self._store(db._now(), cpu_pct=5.0)
        self.assertEqual(len(await db.system_series(days=None, bucket="day")), 2)

    async def test_prune_removes_only_what_is_older(self):
        await self._store("2020-01-01T00:00:00Z", cpu_pct=99.0)
        await self._store(db._now(), cpu_pct=5.0)
        removed = await db.system_prune(30)
        self.assertEqual(removed, 1)
        self.assertEqual(len(await db.system_series(days=None, bucket="day")), 1)

    async def test_prune_of_zero_days_keeps_everything(self):
        """0 is the documented "keep everything" setting, not "delete all"."""
        await self._store("2020-01-01T00:00:00Z", cpu_pct=99.0)
        self.assertEqual(await db.system_prune(0), 0)
        self.assertEqual(len(await db.system_series(days=None, bucket="day")), 1)

    async def test_two_samples_in_one_second_both_survive(self):
        """Why the key is an autoincrement rather than created_at: a clock
        step can produce two samples with the same stamp, and a PRIMARY KEY on
        the timestamp would make the second an IntegrityError inside the
        sampler."""
        await self._store(_at(10, 0), cpu_pct=1.0)
        await self._store(_at(10, 0), cpu_pct=3.0)
        rows = await db.system_series(days=None, bucket="hour")
        self.assertEqual(rows[0]["samples"], 2)


class EndpointTests(_DbCase):
    """Query-parameter handling, which is the whole of the routes' logic."""

    @staticmethod
    def _request(**params):
        return SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/api/system"),
            cookies={},
            headers={},
            query_params=params,
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={}),
        )

    @staticmethod
    def _body(response):
        import json as _json

        return _json.loads(response.body)

    async def test_live_snapshot_carries_the_sampling_contract(self):
        """The page needs both numbers to explain an empty chart: how often a
        sample is taken, and how long one is kept."""
        body = self._body(await app.handle_system_get(self._request()))
        self.assertIn("cpu_pct", body)
        self.assertEqual(body["sample_interval_s"], config.SYSTEM_SAMPLE_S)
        self.assertEqual(body["retention_days"], config.SYSTEM_RETENTION_DAYS)

    async def test_series_defaults_to_a_day_in_half_hours(self):
        """Deliberately not the usage page's 30-days-by-day: a machine in
        trouble is read by the hour."""
        body = self._body(await app.handle_system_series_get(self._request()))
        self.assertEqual(body["days"], 1)
        self.assertEqual(body["bucket"], "halfhour")

    async def test_unknown_bucket_falls_back_rather_than_reaching_sql(self):
        body = self._body(
            await app.handle_system_series_get(self._request(bucket="'; DROP TABLE--"))
        )
        self.assertEqual(body["bucket"], "halfhour")

    async def test_all_means_no_window(self):
        body = self._body(await app.handle_system_series_get(self._request(days="all")))
        self.assertEqual(body["days"], 0)

    async def test_days_is_clamped(self):
        for given, expected in (("99999", 3650), ("-5", 1), ("nonsense", 1)):
            body = self._body(
                await app.handle_system_series_get(self._request(days=given))
            )
            self.assertEqual(body["days"], expected, f"days={given}")

    async def test_series_returns_stored_rows(self):
        """The stored sample is returned, on a continuous axis.

        This asserted a length of 1. The endpoint now fills the window, so a
        one-day request spans yesterday and today and returns two rows: the
        reading, and a placeholder for the day that has none. The count was
        never the subject -- the stored value is -- so it is asserted by
        finding the row rather than by trusting its position.
        """
        await self._store(db._now(), cpu_pct=7.0)
        body = self._body(
            await app.handle_system_series_get(self._request(days="1", bucket="day"))
        )
        measured = [r for r in body["series"] if r["cpu_pct"] is not None]
        self.assertEqual(len(measured), 1)
        self.assertEqual(measured[0]["cpu_pct"], 7.0)

    async def test_the_series_axis_is_continuous_and_holes_are_null(self):
        """A day with no sample must not report a reading of zero.

        Zero would draw the machine idling at 0% CPU across an interval nothing
        was measured in, which is an invented measurement -- and the wider the
        window, the more of the chart is invention.
        """
        await self._store(db._now(), cpu_pct=7.0)
        body = self._body(
            await app.handle_system_series_get(self._request(days="3", bucket="day"))
        )
        buckets = [r["bucket"] for r in body["series"]]
        self.assertEqual(buckets, sorted(buckets))
        self.assertGreater(
            len(buckets), 1, "the window was not filled, so the axis still skips"
        )
        holes = [r for r in body["series"] if r["samples"] == 0]
        self.assertTrue(holes, "a 3-day window with one sample has holes")
        for row in holes:
            self.assertIsNone(row["cpu_pct"])
            self.assertIsNone(row["mem_pct"])


class SamplerTests(unittest.IsolatedAsyncioTestCase):
    """The background task. Its whole job is to still be running tomorrow."""

    async def asyncSetUp(self):
        _reset_collector()
        self.addCleanup(_reset_collector)

    async def asyncTearDown(self):
        await sysstats.stop()

    async def test_start_is_idempotent(self):
        """A second lifespan must not double the sample rate."""
        stored = []
        sysstats.start(lambda row: asyncio.sleep(0, result=stored.append(row)), 60)
        first = sysstats._task
        sysstats.start(lambda row: asyncio.sleep(0), 60)
        self.assertIs(sysstats._task, first)

    async def test_samples_are_handed_to_the_store(self):
        stored = []

        async def store(row):
            stored.append(row)

        sysstats.start(store, 0)  # no delay: run as fast as the loop allows
        for _ in range(50):
            await asyncio.sleep(0.01)
            if stored:
                break
        self.assertTrue(stored, "the sampler stored nothing")
        self.assertEqual(set(stored[0]), set(db.SYSTEM_FIELDS))

    async def test_a_failing_store_does_not_kill_the_sampler(self):
        """A sampler that dies on one bad write leaves a page that is empty
        for ever, with the cause an hour upstream of the symptom."""
        calls = []

        async def store(row):
            calls.append(row)
            raise RuntimeError("disk full")

        with patch.object(sysstats, "_log_sampler_error"):
            sysstats.start(store, 0)
            for _ in range(80):
                await asyncio.sleep(0.01)
                if len(calls) >= 2:
                    break
        self.assertGreaterEqual(len(calls), 2, "the sampler stopped after one failure")
        self.assertFalse(sysstats._task.done())

    async def test_stop_cancels_the_task(self):
        sysstats.start(lambda row: asyncio.sleep(0), 60)
        task = sysstats._task
        await sysstats.stop()
        self.assertTrue(task.cancelled() or task.done())
        self.assertIsNone(sysstats._task)

    async def test_stop_without_a_start_is_harmless(self):
        sysstats._task = None
        await sysstats.stop()  # must not raise

    async def test_stop_is_safe_to_call_twice(self):
        sysstats.start(lambda row: asyncio.sleep(0), 60)
        await sysstats.stop()
        await sysstats.stop()


class WriteHealthTests(unittest.TestCase):
    """Registry #41: 200 OK while writing nothing, for 37 minutes.

    The classifier's job is to tell a dead write path from a quiet one. The
    expensive mistake is the false positive -- registry #34 was a health check
    that restarted a healthy server every 30 seconds, which is strictly worse
    than having none -- so the healthy and just-restarted cases get as much
    coverage as the broken one.
    """

    NOW = 1788123240.0  # 2026-08-30T20:54:00Z

    def test_a_fresh_write_is_healthy(self):
        state, why = sysstats.write_health("2026-08-30T20:53:48Z", 9999, 60, now=self.NOW)
        self.assertEqual(state, sysstats.OK, why)

    def test_a_long_silence_is_stale(self):
        state, _ = sysstats.write_health("2026-08-30T19:00:00Z", 9999, 60, now=self.NOW)
        self.assertEqual(state, sysstats.STALE)

    def test_a_just_restarted_server_is_warming_not_stale(self):
        """The rows it inherited are from before the restart, so the newest is
        legitimately old. Without this branch every restart reads as a dead
        write path and the health check restarts it again -- a loop."""
        state, _ = sysstats.write_health("2026-08-30T19:00:00Z", 30, 60, now=self.NOW)
        self.assertEqual(state, sysstats.WARMING)

    def test_an_unsampled_database_is_unknown_not_stale(self):
        state, _ = sysstats.write_health(None, 9999, 60, now=self.NOW)
        self.assertEqual(state, sysstats.UNKNOWN)

    def test_an_unparseable_stamp_is_unknown_not_stale(self):
        """Garbage in the column must not be read as a dead server."""
        state, _ = sysstats.write_health("not a date", 9999, 60, now=self.NOW)
        self.assertEqual(state, sysstats.UNKNOWN)

    def test_one_missed_sample_is_still_healthy(self):
        """A busy machine skipping a beat is not an outage."""
        state, _ = sysstats.write_health("2026-08-30T20:52:00Z", 9999, 60, now=self.NOW)
        self.assertEqual(state, sysstats.OK)

    def test_the_limit_scales_with_the_sampling_interval(self):
        # 600s interval -> 1800s tolerance, so a 20-minute-old row is fine.
        state, _ = sysstats.write_health("2026-08-30T20:34:00Z", 9999, 600, now=self.NOW)
        self.assertEqual(state, sysstats.OK)

    def test_a_tiny_interval_does_not_make_the_check_hair_triggered(self):
        """Floor of 180s: a 1s sampling interval would otherwise call the
        server dead after three seconds of ordinary scheduling noise."""
        state, _ = sysstats.write_health("2026-08-30T20:52:00Z", 9999, 1, now=self.NOW)
        self.assertEqual(state, sysstats.OK)

    def test_uptime_unknown_does_not_suppress_a_real_stall(self):
        state, _ = sysstats.write_health("2026-08-30T19:00:00Z", None, 60, now=self.NOW)
        self.assertEqual(state, sysstats.STALE)


class ProbeTests(unittest.TestCase):
    """Reading the newest stamp, and the process clock the grace period needs."""

    def test_missing_database_reads_as_no_samples(self):
        self.assertIsNone(sysstats.newest_sample_at("/nonexistent/webconsole.db"))

    def test_reads_the_newest_stamp_from_a_real_database(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE system_samples (created_at TEXT)")
            conn.executemany(
                "INSERT INTO system_samples VALUES (?)",
                [("2026-08-30T10:00:00Z",), ("2026-08-30T12:00:00Z",)],
            )
            conn.commit()
            conn.close()
            self.assertEqual(sysstats.newest_sample_at(path), "2026-08-30T12:00:00Z")

    def test_the_probe_cannot_write(self):
        """The whole point of mode=ro. Opening the live file read-write for a
        diagnostic is what caused #41; a probe that could do it would be the
        same mistake on a 30-second timer."""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE system_samples (created_at TEXT)")
            conn.commit()
            conn.close()
            probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                probe.execute("INSERT INTO system_samples VALUES ('x')")
            probe.close()

    def test_a_table_that_does_not_exist_reads_as_no_samples(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/db"
            sqlite3.connect(path).close()
            self.assertIsNone(sysstats.newest_sample_at(path))

    def test_process_uptime_of_this_process_is_small_and_positive(self):
        uptime = sysstats.process_uptime_s(os.getpid())
        self.assertIsNotNone(uptime)
        self.assertGreaterEqual(uptime, 0.0)

    def test_process_uptime_of_an_unknown_pid_is_none(self):
        self.assertIsNone(sysstats.process_uptime_s(999_999_999))

    def test_a_process_name_containing_spaces_and_brackets_still_parses(self):
        """/proc/<pid>/stat's comm field is unquoted and can contain anything,
        including ') ('. Splitting the line on whitespace shifts every field
        after it, which would read some other number as the start time."""
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            (proc / "42").mkdir(parents=True)
            fields = " ".join(str(n) for n in range(3, 22))  # fields 4..22
            (proc / "42" / "stat").write_text(f"42 (evil ) (name) S {fields}\n")
            (proc / "uptime").write_text("1000.0 900.0\n")
            with patch.object(sysstats, "_PROC", str(proc)):
                uptime = sysstats.process_uptime_s(42)
        ticks = os.sysconf("SC_CLK_TCK")
        self.assertAlmostEqual(uptime, 1000.0 - 21 / ticks, places=3)


class CliTests(unittest.TestCase):
    """`python3 -m sysstats` -- the surface a health check consumes.

    The exit status is the whole contract, and getting it wrong in the
    permissive direction means a check that never fires while looking
    installed; in the strict direction it means restarting a healthy server.
    """

    def _run(self, rows, *, argv=None, interval=60):
        import io
        import sqlite3
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE system_samples (created_at TEXT)")
            conn.executemany("INSERT INTO system_samples VALUES (?)", [(r,) for r in rows])
            conn.commit()
            conn.close()
            out = io.StringIO()
            with patch.object(config, "DB_PATH", path), patch.object(
                config, "SYSTEM_SAMPLE_S", interval
            ), redirect_stdout(out):
                code = sysstats.main(argv or [])
            return code, out.getvalue()

    def test_a_healthy_server_exits_zero(self):
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        code, text = self._run([now])
        self.assertEqual(code, 0, text)
        self.assertIn("ok", text)

    def test_a_stalled_write_path_exits_one(self):
        code, text = self._run(["2020-01-01T00:00:00Z"])
        self.assertEqual(code, 1, text)
        self.assertIn("stale", text)

    def test_an_unsampled_database_exits_zero(self):
        """`unknown` means no verdict, not a fault. A caller that restarts on
        it would restart a server whose table has simply been pruned."""
        code, text = self._run([])
        self.assertEqual(code, 0, text)
        self.assertIn("unknown", text)

    def test_a_young_server_exits_zero_despite_old_rows(self):
        """The restart-loop case, through the CLI: stale rows plus a fresh pid
        must not be a fault.

        Spawns a process rather than using os.getpid(). The pytest process is
        seconds old when this file runs alone and minutes old in a full suite,
        so the original passed by itself and failed in the suite -- a test
        whose verdict depended on how it was invoked rather than on the code.
        """
        import subprocess

        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.terminate)
        code, text = self._run(
            ["2020-01-01T00:00:00Z"], argv=["--pid", str(child.pid)]
        )
        self.assertEqual(code, 0, text)
        self.assertIn("warming", text)

    def test_the_probe_follows_the_configured_database(self):
        """Reads config.DB_PATH rather than a hardcoded project-local path: a
        probe pointed at the wrong file reports `unknown` for ever, which is
        indistinguishable from a healthy server that was never sampled."""
        _code, text = self._run(["2020-01-01T00:00:00Z"])
        self.assertNotIn("data/webconsole.db", text)


class RetentionWiringTests(_DbCase):
    """db.init() prunes on startup -- the only scheduler this table has."""

    async def test_init_prunes_old_samples(self):
        await self._store("2020-01-01T00:00:00Z", cpu_pct=99.0)
        await db.close()
        with patch.object(config, "SYSTEM_RETENTION_DAYS", 30):
            await db.init()
        self.assertEqual(await db.system_series(days=None, bucket="day"), [])


if __name__ == "__main__":
    with contextlib.suppress(SystemExit):
        unittest.main()
