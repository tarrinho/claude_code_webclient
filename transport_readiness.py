"""Is a transport host ready to serve turns, and if not, what is missing?

A transport is an SSH connection to a host. For turns to flow over it the far
side needs `claude_proxy.py` listening on ``config.PROXY_PORT``, holding the
same proxy token this host's database holds, with the `claude` CLI and python3
available to it. None of that is created by adding the transport, and none of
it was visible anywhere before this module: the symptom of a missing far side
is "Cannot connect to proxy at 127.0.0.1:<port>" on every turn, which reads as
a local fault.

Measured on 2026-09-08 against a freshly added transport (pentester): SSH
worked, the CLI and python3 were both present, and there was no proxy and no
checkout. That breakdown is what turned a vague failure into a ten-minute fix,
so this returns the four answers separately rather than one boolean.

Split from routes/transports.py so it can be tested without a route, and from
tunnel_manager_ssh because these checks deliberately do NOT need a live tunnel
-- `probe_remote` there requires an established connection, and a cold
transport is exactly when you most want to ask these questions.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

_log = logging.getLogger("wc.transport_readiness")

# One shell invocation, so the whole report costs a single SSH connection
# rather than four. Each line is key=value so the parse depends on neither
# ordering nor on a command that prints nothing.
#
# The port is substituted with str.replace on a literal marker, NOT with
# %-formatting or .format(): this script is full of printf '%s' and of shell
# $(...) syntax, and both formatters try to interpret those. The first version
# used %(port)d and raised "not enough arguments for format string" on every
# call -- caught only because the template is exercised directly in a test.
_PORT_MARKER = "__PORT__"

_PROBE = r"""
printf 'claude=%s\n' "$( [ -x "$HOME/.local/bin/claude" ] && echo "$HOME/.local/bin/claude" \
    || command -v claude 2>/dev/null || echo MISSING )"
printf 'python=%s\n' "$(python3 -V 2>&1 || echo MISSING)"
printf 'listening=%s\n' "$( (ss -tln 2>/dev/null || netstat -tln 2>/dev/null) \
    | grep -c '127.0.0.1:__PORT__' )"
printf 'token_len=%s\n' "$(wc -c < ~/wc-proxy/proxy_token.txt 2>/dev/null || echo 0)"
printf 'token_sha=%s\n' "$(sha256sum ~/wc-proxy/proxy_token.txt 2>/dev/null | cut -c1-16 || echo none)"
printf 'service=%s\n' "$(systemctl --user is-active wc-proxy.service 2>/dev/null || echo absent)"
"""


def probe_script(port: int) -> str:
    """The probe with the port filled in. See _PORT_MARKER for why replace()."""
    return _PROBE.replace(_PORT_MARKER, str(int(port)))


@dataclass
class Check:
    """One question, its answer, and what to do when the answer is no."""

    name: str
    ok: bool
    detail: str
    remedy: str = ""


@dataclass
class Readiness:
    checks: list[Check] = field(default_factory=list)
    reachable: bool = False
    error: str = ""

    @property
    def ready(self) -> bool:
        return self.reachable and all(c.ok for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "reachable": self.reachable,
            "error": self.error,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail, "remedy": c.remedy}
                for c in self.checks
            ],
        }


def parse_probe(raw: str, *, port: int, local_token: str) -> list[Check]:
    """Turn the probe's key=value lines into the four answers.

    Pure, so the table of remote states in the tests needs no SSH and no host.

    The token is compared by a truncated sha256 of the remote file against the
    same digest of the local value -- never by shipping either one around. A
    mismatch here is the failure that cost the most time on Kali3: its proxy
    ran for days on a stale token while the database held another, and every
    turn died at the handshake with nothing useful logged.
    """
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        if _:
            fields[key.strip()] = value.strip()

    claude = fields.get("claude", "MISSING")
    python = fields.get("python", "MISSING")
    listening = fields.get("listening", "0")
    service = fields.get("service", "absent")
    remote_sha = fields.get("token_sha", "none")

    import hashlib

    local_sha = (
        hashlib.sha256(local_token.encode()).hexdigest()[:16] if local_token else ""
    )

    checks = [
        # Resolved by path first, `command -v` only as a fallback -- and that
        # order is load-bearing, not tidiness. A non-interactive SSH shell has
        # no ~/.local/bin on PATH, so `command -v claude` reported MISSING on a
        # host where the CLI was installed and working. The proxy does not use
        # PATH either: its unit sets WC_CLAUDE_PATH to ~/.local/bin/claude.
        # Asking the PATH question answered something nobody had asked and
        # raised a false alarm on a healthy host -- which is how a check earns
        # being switched off (CLAUDE.md §8 records the same trap breaking every
        # turn once already).
        Check(
            "claude CLI",
            claude != "MISSING" and bool(claude),
            claude if claude != "MISSING" else "not found at ~/.local/bin/claude or on PATH",
            remedy="install Claude Code for this user on the remote host",
        ),
        Check(
            "python3",
            python != "MISSING" and python.startswith("Python"),
            python,
            remedy="install python3 on the remote host",
        ),
        Check(
            f"proxy on 127.0.0.1:{port}",
            listening.isdigit() and int(listening) > 0,
            "listening" if listening.isdigit() and int(listening) > 0
            else f"nothing listening (service: {service})",
            remedy="press Init, or run bin/wc-deploy-proxy.sh <transport>",
        ),
        Check(
            "proxy token matches",
            bool(local_sha) and remote_sha == local_sha,
            "matches this host's token" if remote_sha == local_sha and local_sha
            else f"differs or absent (remote {remote_sha})",
            remedy="press Init; it rewrites the token from the database",
        ),
    ]
    return checks


async def check_transport(
    ssh_host: str, ssh_user: str, ssh_key_path: str, *, port: int, local_token: str
) -> Readiness:
    """Run the probe over a one-shot SSH connection and report.

    Read-only by construction: the probe runs `command -v`, `python3 -V`,
    `ss`, `wc`, `sha256sum` and `systemctl is-active`. It creates nothing and
    starts nothing -- installing is Init's job, deliberately a separate,
    separately-clicked operation.
    """
    import os

    result = Readiness()
    key = os.path.expanduser(ssh_key_path or "")
    if not ssh_host:
        result.error = "transport has no ssh_host"
        return result
    if not key or not os.path.isfile(key):
        result.error = f"ssh key not readable: {ssh_key_path}"
        return result

    script = probe_script(port)
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-i", key, f"{ssh_user or 'kali'}@{ssh_host}", script,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        raw, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        result.error = "SSH probe timed out after 30s"
        return result
    except OSError as exc:
        result.error = f"could not run ssh: {exc}"
        return result

    if proc.returncode != 0 and not raw.strip():
        # stderr can carry a locale warning on a perfectly good connection, so
        # only a non-zero exit *with no output at all* means unreachable.
        result.error = (err.decode("utf-8", "replace").strip() or "SSH failed")[:200]
        return result

    result.reachable = True
    result.checks = parse_probe(
        raw.decode("utf-8", "replace"), port=port, local_token=local_token
    )
    return result
