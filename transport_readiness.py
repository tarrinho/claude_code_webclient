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


async def _probe_forward(
    ssh_host: str, ssh_user: str, key: str, *, port: int, local_token: str,
) -> Check:
    """Can THIS host actually open a working `-L` forward to *port*, right now?

    The four checks in `parse_probe` predict success but cannot prove it: SSH
    reaching the host and the far side listening on the right port with a
    matching token all say nothing about whether the remote sshd will actually
    let this connection *forward* traffic. `AllowTcpForwarding no` (or an
    equivalent restriction) passes every one of those checks and then refuses
    every real tunnel -- invisible until now, because nothing before this
    attempted a forward at all.

    A second, dedicated SSH connection (`-N`, no remote command) rather than
    piggy-backing on the diagnostic one above: that one's script prints its
    output and exits, which would tear the forward down before anything on
    this end could use it. `ExitOnForwardFailure=yes` makes ssh exit
    immediately, non-zero, if the remote refuses to bind the forward, rather
    than sitting there looking healthy with nothing to show for it.

    Transient by construction: the forward exists only for the handshake
    below, and the ssh process is always killed in `finally`. Nothing is left
    running, matching this module's read-only contract -- opening and closing
    a connection is not "creating" anything the way Init's deploy is.
    """
    import contextlib
    import json
    import socket

    # ssh -L needs a literal port; bind-then-close is the standard way to ask
    # the OS for one that is free right now. The gap between closing this
    # socket and ssh binding the same number is the same race every such
    # allocator accepts -- and losing it fails this one check, not a real
    # tunnel.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
        probe_sock.bind(("127.0.0.1", 0))
        local_port = probe_sock.getsockname()[1]

    ssh_proc = None
    try:
        ssh_proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ExitOnForwardFailure=yes",
            "-N", "-L", f"127.0.0.1:{local_port}:127.0.0.1:{port}",
            "-i", key, f"{ssh_user or 'kali'}@{ssh_host}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(1.0)   # give it a moment to bind, or fail fast
        if ssh_proc.returncode is not None:
            stderr = (await ssh_proc.stderr.read()).decode("utf-8", "replace").strip()
            return Check(
                "tunnel forward", False,
                stderr[:200] or "ssh exited before the forward opened",
                remedy="check AllowTcpForwarding in the remote sshd_config",
            )

        # The forward exists; confirm claude_proxy.py actually answers through
        # it, with the exact frame a real tunnel sends -- a bound socket that
        # nothing useful is behind is not a working tunnel.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", local_port), timeout=5)
        try:
            frame = {"type": "handshake", "protocol": "webconsole-v1", "token": local_token}
            writer.write((json.dumps(frame) + "\n").encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5)
            reply = json.loads(raw)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        if reply.get("type") == "ack":
            return Check(
                "tunnel forward", True,
                "forward opens and the proxy accepted the handshake",
            )
        return Check(
            "tunnel forward", False, f"proxy responded but not with ack: {reply}",
            remedy="press Init; it rewrites the token from the database",
        )
    except asyncio.TimeoutError:
        return Check(
            "tunnel forward", False,
            "forward opened but the proxy did not respond in time",
            remedy="check wc-proxy.service logs on the remote host",
        )
    except (ConnectionError, OSError, ValueError, EOFError) as exc:
        # ValueError covers a malformed (non-JSON, or no trailing newline)
        # reply -- the proxy answered but not sensibly, which is still a real
        # finding and not this function's own bug. EOFError (its concrete
        # subclass here, asyncio.IncompleteReadError) is claude_proxy.py's own
        # rejection path: a bad or missing handshake closes the socket with
        # zero bytes written, and readuntil() reports that as EOF rather than
        # a connection error -- caught here or a bad-token forward reads as
        # this function crashing instead of the real finding it is.
        return Check(
            "tunnel forward", False, f"forward did not reach a working proxy: {exc}",
            remedy="check AllowTcpForwarding in the remote sshd_config",
        )
    finally:
        if ssh_proc and ssh_proc.returncode is None:
            ssh_proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ssh_proc.wait(), timeout=5)
            if ssh_proc.returncode is None:
                ssh_proc.kill()


async def check_transport(
    ssh_host: str, ssh_user: str, ssh_key_path: str, *, port: int, local_token: str
) -> Readiness:
    """Run the probe over a one-shot SSH connection and report.

    Read-only by construction: the probe runs `command -v`, `python3 -V`,
    `ss`, `wc`, `sha256sum` and `systemctl is-active`. It creates nothing and
    starts nothing -- installing is Init's job, deliberately a separate,
    separately-clicked operation. The one exception is the forward test below,
    and it is transient by construction: see `_probe_forward`.
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

    # Only meaningful once SSH itself is proven (reachable=True, just set) and
    # there is a real token to hand over -- with none configured, "token
    # matches" above has already failed and correctly explains why, and
    # attempting a handshake with an empty token would just add a second,
    # more confusing failure about the same missing prerequisite.
    if local_token:
        try:
            forward_check = await asyncio.wait_for(
                _probe_forward(
                    ssh_host, ssh_user, key, port=port, local_token=local_token,
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            forward_check = Check(
                "tunnel forward", False, "forward probe timed out after 20s",
                remedy="check AllowTcpForwarding in the remote sshd_config",
            )
        result.checks.append(forward_check)

    return result
