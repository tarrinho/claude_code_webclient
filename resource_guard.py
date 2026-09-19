"""Refuse to start new work when this host has no memory for it.

Design: docs/superpowers/specs/2026-09-08-resource-guard-design.md

Measured on 2026-09-08, in the box's ordinary working state: 635 MB available
of 3816, 1470 MB already in swap, and 2520 MB held by ``claude`` processes --
of which 1884 MB was seven interactive sessions and 3 MB was console-spawned
turns. ``config.MAX_CONCURRENT`` and ``claude_proxy._MAX_CONCURRENT`` gate only
that 3 MB, which is why neither has ever helped: the memory is held by
long-lived interactive agents that nothing limits.

This module answers exactly one question -- *is there room for N more
megabytes* -- which is what lets three unrelated callers share it without
agreeing on anything else. It deliberately does not know what an agent is, does
not terminate anything, and has no daemon, timer or state.

Kept free of ``config`` and ``db`` imports on purpose: two of its three callers
are shell scripts, and booting FastAPI to ask about free memory would cost more
than the memory in question.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger("wc.resource_guard")

# Starting values, calibrated on one machine on one day (see the spec's
# "The rule"). Environment variables rather than constants because they are
# expected to need tuning within a week of real use, and tuning should not need
# a code change or a restart of anything that has not already restarted.
# Re-measured 2026-09-08 across 6 live agents: mean 337 MB, median 349 MB,
# range 242-386 MB. The old 320 came from a 310 MB mean and was an *under*
# estimate, so the projection was optimistic everywhere it was used -- worth
# knowing, because the guard was disabled on the agent path in the belief that
# it over-stated the cost. It did the opposite.
_DEFAULT_COST_MB = 350
_DEFAULT_FLOOR_MB = 400     # headroom for the OS, the console and the proxy
_DEFAULT_SWAP_MIN_RATIO = 0.40

_MEMINFO = Path("/proc/meminfo")
_PROC = Path("/proc")

# What distinguishes a console-spawned turn from an interactive session. Pinned
# by CLAUDE.md §0, which fixes the argv the console builds; if that ever stops
# carrying this flag the split below silently mis-attributes, so the coupling is
# named on both ends.
_TURN_MARKER = "--output-format stream-json"

_OFF_VALUES = {"off", "0", "false", "no"}


@dataclass
class Verdict:
    """A decision, and the numbers it was made on.

    Callers print `reason` rather than composing their own sentence, so a
    refusal reads identically whether it came from a shell script or the
    console.
    """

    ok: bool
    reason: str
    available_mb: int = 0
    cost_mb: int = 0
    floor_mb: int = 0
    swap_free_ratio: float | None = None
    measured: bool = True

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Load:
    """Who is holding memory, split the way the decision cares about."""

    interactive_count: int = 0
    interactive_mb: int = 0
    turn_count: int = 0
    turn_mb: int = 0
    console_mb: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.interactive_count} interactive agent"
            f"{'' if self.interactive_count == 1 else 's'} "
            f"holding {self.interactive_mb} MB"
        ]
        if self.turn_count:
            parts.append(f"{self.turn_count} console turn(s) holding {self.turn_mb} MB")
        if self.console_mb:
            parts.append(f"the console holds {self.console_mb} MB")
        return "; ".join(parts)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse kernel-style ``/proc/meminfo`` text as kilobytes.

    Kept separate so that remote reads (which arrive as shell output rather
    than a file) can reuse the parser without making this module know about SSH.
    """
    out: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        try:
            out[name.strip()] = int(rest.strip().split()[0])
        except (IndexError, ValueError):
            continue
    return out


def read_meminfo(path: Path | None = None) -> dict[str, int]:
    """`/proc/meminfo` as kilobytes, keyed without the trailing colon.

    Separate from sysstats._read_meminfo, which returns a different shape and
    lives in a module that imports the world. Both read the same three lines;
    this one is the version a shell script can afford.
    """
    return parse_meminfo((path or _MEMINFO).read_text(encoding="utf-8"))


def check(
    cost_mb: int | None = None,
    *,
    floor_mb: int | None = None,
    swap_min_ratio: float | None = None,
    meminfo: dict[str, int] | None = None,
    env: dict[str, str] | None = None,
) -> Verdict:
    """Is there room to start something costing *cost_mb* megabytes?

    A projection rather than a level: the question is what remains *after* the
    new work, not what is free now.

        MemAvailable - cost_mb < floor_mb  ->  refuse

    MemAvailable, not MemFree and not "used". MemFree read 213 MB on the day
    this was written and would refuse constantly; "used" counts reclaimable
    page cache and would refuse never.

    **Fails open.** If the numbers cannot be read, this returns ok=True with
    `measured=False` and logs it. A guard that blocks work because it could not
    measure turns a monitoring bug into an outage, and does so at every caller
    at once -- the same trade tunnel_manager's boot grace makes, and for the
    same stated reason: acting on missing information is worse than waiting.
    """
    environ = os.environ if env is None else env
    cost_mb = _int_env("WC_RESOURCE_COST_MB", _DEFAULT_COST_MB) if cost_mb is None else cost_mb
    floor_mb = _int_env("WC_RESOURCE_FLOOR_MB", _DEFAULT_FLOOR_MB) if floor_mb is None else floor_mb
    if swap_min_ratio is None:
        swap_min_ratio = _float_env("WC_RESOURCE_SWAP_MIN_RATIO", _DEFAULT_SWAP_MIN_RATIO)

    override = str(environ.get("WC_RESOURCE_GUARD", "")).strip().lower()
    if override in _OFF_VALUES:
        # WARNING, not INFO: an override that stops being visible stops being
        # an override and becomes the default nobody chose.
        _log.warning(
            "resource_guard_overridden WC_RESOURCE_GUARD=%s — allowing work "
            "that would otherwise be refused", override,
        )
        return Verdict(True, f"guard disabled (WC_RESOURCE_GUARD={override})",
                       cost_mb=cost_mb, floor_mb=floor_mb, measured=False)

    if meminfo is None:
        try:
            meminfo = read_meminfo()
        except OSError as exc:
            _log.warning("resource_guard_unmeasurable: %s — allowing", exc)
            return Verdict(True, f"could not read memory ({exc}); allowing",
                           cost_mb=cost_mb, floor_mb=floor_mb, measured=False)

    available_kb = meminfo.get("MemAvailable")
    if available_kb is None:
        _log.warning("resource_guard_unmeasurable: no MemAvailable — allowing")
        return Verdict(True, "no MemAvailable in meminfo; allowing",
                       cost_mb=cost_mb, floor_mb=floor_mb, measured=False)
    available_mb = available_kb // 1024

    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    ratio = (swap_free / swap_total) if swap_total else None

    remaining = available_mb - cost_mb
    if remaining < floor_mb:
        return Verdict(
            False,
            f"{remaining} MB would remain after a {cost_mb} MB start, "
            f"floor is {floor_mb} MB",
            available_mb=available_mb, cost_mb=cost_mb, floor_mb=floor_mb,
            swap_free_ratio=ratio,
        )

    # Independent of the headroom test: the box can show adequate MemAvailable
    # while already thrashing, and adding load to a thrashing box is how a slow
    # host becomes a stopped one.
    if ratio is not None and ratio < swap_min_ratio:
        return Verdict(
            False,
            f"only {ratio:.0%} of swap is free (minimum {swap_min_ratio:.0%}); "
            f"the host is already swapping",
            available_mb=available_mb, cost_mb=cost_mb, floor_mb=floor_mb,
            swap_free_ratio=ratio,
        )

    return Verdict(
        True,
        f"{remaining} MB would remain after a {cost_mb} MB start",
        available_mb=available_mb, cost_mb=cost_mb, floor_mb=floor_mb,
        swap_free_ratio=ratio,
    )


def capacity(
    cost_mb: int | None = None,
    *,
    floor_mb: int | None = None,
    meminfo: dict[str, int] | None = None,
    env: dict[str, str] | None = None,
    load: "Load | None" = None,
) -> dict[str, int | None]:
    """How many agent-equivalents are running now, and how many this host
    could hold in total.

    Composes the other two functions rather than re-deriving either:
    ``report()`` for who is running now, ``check()`` for the headroom formula.
    *total* is the same ceiling ``check()`` would apply to a start attempted
    right now, projected forward one agent at a time --

        total = existing + floor((available_mb - floor_mb) / cost_mb)

    which is exactly what running ``check()`` in a loop, letting each accepted
    start consume *cost_mb*, would converge to: this host's projections and its
    live admission decisions are the same arithmetic, so this function does not
    get to disagree with ``check()`` about when the host is full.

    *total* is ``None`` when the inputs cannot be measured -- the same
    fails-open case ``check()`` has, and for the same reason: a number this
    function cannot back up is worse than admitting it does not know.
    """
    cost_mb = _int_env("WC_RESOURCE_COST_MB", _DEFAULT_COST_MB) if cost_mb is None else cost_mb
    floor_mb = _int_env("WC_RESOURCE_FLOOR_MB", _DEFAULT_FLOOR_MB) if floor_mb is None else floor_mb
    if load is None:
        load = report()
    existing = load.interactive_count + load.turn_count

    # cost_mb=0: this asks what is available, not whether a start of a
    # particular size fits -- reusing check()'s own measurement rather than
    # reading meminfo a second time, so the two can never disagree about it.
    verdict = check(0, floor_mb=floor_mb, meminfo=meminfo, env=env)
    if not verdict.measured:
        return {
            "existing": existing, "total": None,
            "cost_mb": cost_mb, "floor_mb": floor_mb, "available_mb": None,
        }

    more = max(0, (verdict.available_mb - floor_mb) // cost_mb)
    return {
        "existing": existing, "total": existing + more,
        "cost_mb": cost_mb, "floor_mb": floor_mb,
        "available_mb": verdict.available_mb,
    }


def _rss_mb(pid: str) -> int | None:
    try:
        for line in (_PROC / pid / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def report() -> Load:
    """Who is holding memory right now.

    A linear scan of /proc, so it belongs on a refusal path and nowhere
    frequent. The interactive-versus-turn split is the single most useful line
    in a refusal: it is what showed console turns were 3 MB of the problem
    while everything else was interactive sessions, which is the finding the
    whole design rests on.
    """
    load = Load()
    try:
        pids = [p.name for p in _PROC.iterdir() if p.name.isdigit()]
    except OSError as exc:
        load.errors.append(f"could not scan /proc: {exc}")
        return load

    for pid in pids:
        try:
            cmdline = (_PROC / pid / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except OSError:
            continue  # exited between listing and reading; normal
        if not cmdline.strip():
            continue
        rss = _rss_mb(pid)
        if rss is None:
            continue
        if "share/claude/versions" in cmdline or "/claude " in cmdline:
            if _TURN_MARKER in cmdline:
                load.turn_count += 1
                load.turn_mb += rss
            else:
                load.interactive_count += 1
                load.interactive_mb += rss
        elif "uvicorn" in cmdline and "app:app" in cmdline:
            load.console_mb += rss
    return load


def explain(verdict: Verdict, load: Load | None = None) -> str:
    """The sentence a caller prints. One phrasing, three callers.

    A bare "low memory" sends the reader hunting; the breakdown is a decision
    they can act on without one.
    """
    lines = [verdict.reason]
    if load is None:
        load = report()
    summary = load.summary()
    if summary:
        lines.append(summary)
    if not verdict.ok:
        lines.append("Close an agent, or override with WC_RESOURCE_GUARD=off.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI for the shell callers. Exit 0 to allow, 1 to refuse.

    The reason goes to stderr so a caller can show it without it landing in
    whatever the caller's stdout is being piped into.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="resource_guard",
        description="Refuse new work when this host has no memory for it.",
    )
    parser.add_argument("--cost-mb", type=int, default=None,
                        help="megabytes the work about to start will need")
    # Callers do not all want the same floor. The floor protects the OS, the
    # console and the proxy from the OOM killer, and how much protection is
    # warranted depends on what is being started: a test suite that can simply
    # be retried should yield to a live service earlier than an interactive
    # session someone is waiting on. Without this flag the only way to move the
    # floor was an environment variable, so the agent path could not ask for a
    # different one than the suite runner -- and it was commented out instead.
    parser.add_argument("--floor-mb", type=int, default=None,
                        help="megabytes that must remain after the work starts")
    parser.add_argument("--quiet", action="store_true",
                        help="decide silently; report only via the exit code")
    args = parser.parse_args(argv)

    verdict = check(args.cost_mb, floor_mb=args.floor_mb)
    if not args.quiet:
        print(explain(verdict), file=sys.stderr)
    return 0 if verdict.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised via the CLI test
    raise SystemExit(main())


def check_remote(
    machine_id: str | None = None,
    *,
    cost_mb: int | None = None,
    floor_mb: int | None = None,
    swap_min_ratio: float | None = None,
    meminfo: dict[str, int] | None = None,
    env: dict[str, str] | None = None,
) -> RemoteVerdict:
    """Compose a local check with an optional remote SSH meminfo read.

    When *machine_id* identifies a machine whose tunnel is live, the remote
    host's ``/proc/meminfo`` is read over the existing SSH connection and
    subjected to the same rules.  The turn is admitted only when **both**
    hosts pass the headroom test.

    If the tunnel is down, the machine_id is blank, or the SSH read raises,
    the remote half is treated as *unmeasured* (fails open).

    The caller (runner.py) holds the async machinery and the tunnel state
    reference, so it resolves proxy_target and feeds the machine_id back in.
    """
    local_check = check(
        cost_mb=cost_mb,
        floor_mb=floor_mb,
        swap_min_ratio=swap_min_ratio,
        meminfo=meminfo,
        env=env,
    )

    if not local_check:
        return RemoteVerdict(
            ok=False,
            reason=local_check.reason,
            local_check=local_check,
        )

    remote_measured = False
    remote_ok = False
    remote_verdict: Verdict | None = None

    if machine_id:
        try:
            from _remote_read import remote_meminfo_sync

            remote_text = remote_meminfo_sync(machine_id, timeout=5)
            if remote_text is not None:
                remote_measured = True
                remote_meminfo_dict = parse_meminfo(remote_text)
                remote_verdict = check(
                    cost_mb=cost_mb,
                    floor_mb=floor_mb,
                    swap_min_ratio=swap_min_ratio,
                    meminfo=remote_meminfo_dict,
                    env=env,
                )
                remote_ok = bool(remote_verdict)
        except Exception:
            _log.debug("remote_meminfo failed for machine=%s — admitting", machine_id)

    ok = bool(local_check)
    if remote_measured and remote_verdict is not None and not remote_ok:
        ok = False

    reason = local_check.reason if (remote_ok or not remote_measured) else (
        remote_verdict.reason if remote_verdict else local_check.reason
    )

    return RemoteVerdict(
        ok=ok,
        reason=reason,
        local_check=local_check,
        remote_measured=remote_measured,
        remote_ok=remote_ok,
    )


class RemoteVerdict:
    """A composite verdict for a turn that may span two hosts.

    The turn is admitted only when both local and remote hosts pass.
    When the remote host cannot be reached the local verdict stands.
    """
    def __init__(
        self,
        ok: bool,
        reason: str,
        local_check: Verdict,
        remote_measured: bool = False,
        remote_ok: bool = False,
        remote_reason: str | None = None,
    ):
        self.ok = ok
        self.reason = reason
        self.local_check = local_check
        self.remote_measured = remote_measured
        self.remote_ok = remote_ok
        self.remote_reason = remote_reason
        self.cost_mb = local_check.cost_mb
        self.floor_mb = local_check.floor_mb
        self.available_mb = local_check.available_mb
        self.swap_free_ratio = local_check.swap_free_ratio

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        parts = [self.reason]
        if self.remote_measured:
            tag = "ok" if self.remote_ok else "refused"
            parts.append(f"remote host: {tag}")
            if self.remote_reason:
                parts.append(f"  {self.remote_reason}")
        else:
            parts.append("remote host: not measured (tunnel down)")
        return "\n".join(parts)
