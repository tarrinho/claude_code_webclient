"""Local TCP port forwarding over an established SSH transport.

Split boundary: tunnel_manager_ssh owns the SSH session itself (connect,
host-key pinning, auth); this file owns bridging local TCP connections
through that session to a fixed destination on the remote side -- the
`ssh -L <local_port>:<remote_host>:<remote_port>` half of the feature.

Why this exists at all: connect() used to call
`transport.request_port_forward("127.0.0.1", local_port)` and stop there.
That is paramiko's *remote* forwarding request (the `-R` direction --
"remote host, please listen and hand connections back to me") with no
handler registered, called for the wrong direction entirely: nothing was
ever bound to listen on `127.0.0.1:<local_port>` on *this* host, which is
what runner.get_proxy_target() actually connects to. Confirmed live: a
real turn through a real, successfully-connected tunnel failed immediately
with "Cannot connect to proxy at 127.0.0.1:<port>" -- nothing was
listening, even though the SSH session itself, and claude_proxy.py on the
far end, were both already up.

Runs a classic thread-per-connection forwarder (paramiko's own documented
pattern for this -- see their demos/forward.py) rather than an
asyncio-native one: paramiko's Channel and Transport are synchronous
throughout, so an asyncio version would still need every read/write
wrapped in `asyncio.to_thread` per chunk, which is more moving parts for
no real benefit here -- this traffic is one Claude Code CLI's
stream-json protocol per tunnel, not a high-connection-count workload.
"""
from __future__ import annotations

import contextlib
import logging
import select
import socketserver
import threading

_log = logging.getLogger("wc.tunnel.forward")


class _ForwardHandler(socketserver.BaseRequestHandler):
    """One instance per accepted local connection. transport/remote_host/
    remote_port are set on the class by start_forward() below, since
    socketserver instantiates this itself and gives no other way to pass
    per-server arguments into it."""

    transport = None
    remote_host = "127.0.0.1"
    remote_port = 0

    def handle(self) -> None:
        try:
            peer = self.request.getpeername()
        except OSError:
            peer = ("127.0.0.1", 0)
        try:
            channel = self.transport.open_channel(
                "direct-tcpip", (self.remote_host, self.remote_port), peer,
            )
        except Exception as exc:
            _log.warning(
                "direct-tcpip channel open failed (target %s:%s): %s",
                self.remote_host, self.remote_port, exc,
            )
            return
        if channel is None:
            _log.warning(
                "direct-tcpip channel refused (target %s:%s) -- "
                "remote end may not be listening there",
                self.remote_host, self.remote_port,
            )
            return
        try:
            while True:
                r, _, _ = select.select([self.request, channel], [], [])
                if self.request in r:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in r:
                    data = channel.recv(65536)
                    if not data:
                        break
                    self.request.sendall(data)
        except Exception:
            pass  # either side closing mid-transfer is normal, not an error
        finally:
            with contextlib.suppress(Exception):
                channel.close()


class _ForwardServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_forward(transport, local_port: int, remote_host: str, remote_port: int):
    """Bind 127.0.0.1:local_port and forward every connection through
    *transport* to remote_host:remote_port. Returns the running server;
    pass it to stop_forward() to tear down. Raises OSError if local_port
    is already in use (the caller already checked with
    _find_available_port(), but that check-then-bind has an inherent, if
    narrow, race -- surfacing it as a real exception rather than silently
    swallowing it is the point)."""

    class _Handler(_ForwardHandler):
        pass

    _Handler.transport = transport
    _Handler.remote_host = remote_host
    _Handler.remote_port = remote_port

    server = _ForwardServer(("127.0.0.1", local_port), _Handler)
    thread = threading.Thread(
        target=server.serve_forever, name=f"tunnel-forward-{local_port}",
        daemon=True,
    )
    thread.start()
    return server


def stop_forward(server) -> None:
    """Stop accepting new local connections and release the port. Existing
    in-flight connections' threads finish on their own (daemon_threads=True
    means they will not block process exit either way)."""
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        _log.exception("forward server shutdown failed")
    try:
        server.server_close()
    except Exception:
        _log.exception("forward server close failed")
