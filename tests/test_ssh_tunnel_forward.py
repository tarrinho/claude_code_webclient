"""Functional tests for tunnel_manager_forward -- the local TCP port
forwarder that bridges 127.0.0.1:<local_port> to a destination through an
SSH transport's direct-tcpip channels.

connect() used to call transport.request_port_forward("127.0.0.1",
local_port) and stop there -- paramiko's *remote* forwarding request (the
`-R` direction), the wrong direction entirely, and never given a handler
either, so nothing was ever bound to listen locally. Confirmed live: a
real turn through a real, fully-connected tunnel failed immediately with
"Cannot connect to proxy at 127.0.0.1:<port>".

Tested here without a real SSH server: paramiko.Channel and a real
connected socket both implement the same three methods this module
actually calls on whatever open_channel() returns (sendall, recv,
close) -- so a fake "transport" whose open_channel() opens a real
loopback socket to a real echo server exercises the actual bridging
code path for real, not a mock of it.
"""
from __future__ import annotations

import socket
import threading
import time
import unittest

from tunnel_manager_forward import start_forward, stop_forward


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_echo_server(sock: socket.socket, stop: threading.Event) -> None:
    sock.settimeout(0.2)
    while not stop.is_set():
        try:
            conn, _ = sock.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        with conn:
            conn.settimeout(2)
            try:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    conn.sendall(data)
            except OSError:
                pass


class _FakeTransport:
    """Stands in for a paramiko Transport. open_channel() ignores the
    third argument (the local peer address, which real usage passes
    through for logging on the SSH server side) and opens a real socket
    to *dest* instead of an SSH channel -- everything downstream of that
    only calls sendall/recv/close, which a real socket already
    implements identically."""

    def __init__(self, dest: tuple[str, int]):
        self.dest = dest
        self.opened: list[tuple[str, int]] = []

    def open_channel(self, kind, dest, _local_peer):
        assert kind == "direct-tcpip"
        self.opened.append(dest)
        return socket.create_connection(dest, timeout=5)


class ForwardServerQA(unittest.TestCase):
    def setUp(self):
        self.echo_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.echo_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.echo_sock.bind(("127.0.0.1", 0))
        self.echo_port = self.echo_sock.getsockname()[1]
        self.echo_sock.listen(5)
        self.stop_echo = threading.Event()
        self.echo_thread = threading.Thread(
            target=_run_echo_server, args=(self.echo_sock, self.stop_echo),
            daemon=True,
        )
        self.echo_thread.start()
        self.forward_server = None

    def tearDown(self):
        if self.forward_server is not None:
            stop_forward(self.forward_server)
        self.stop_echo.set()
        self.echo_thread.join(timeout=2)
        with __import__("contextlib").suppress(OSError):
            self.echo_sock.close()

    def test_data_sent_to_the_local_port_reaches_the_forwarded_destination(self):
        """The actual point of the whole module: a client connecting to
        127.0.0.1:<local_port> must have its bytes actually delivered to
        the configured remote destination and get a real reply back --
        not just "the port accepted a connection", which the old
        request_port_forward call never even managed."""
        local_port = _free_port()
        transport = _FakeTransport(("127.0.0.1", self.echo_port))
        self.forward_server = start_forward(
            transport, local_port, "127.0.0.1", self.echo_port
        )
        time.sleep(0.1)  # let the forwarder's accept thread actually start

        with socket.create_connection(("127.0.0.1", local_port), timeout=5) as client:
            client.sendall(b"hello through the tunnel")
            client.settimeout(5)
            reply = client.recv(4096)

        self.assertEqual(reply, b"hello through the tunnel")
        self.assertEqual(transport.opened, [("127.0.0.1", self.echo_port)])

    def test_stop_forward_releases_the_local_port(self):
        """After stop_forward(), nothing should still be listening --
        otherwise every reconnect attempt after a tunnel drop leaks one
        more bound port forever."""
        local_port = _free_port()
        transport = _FakeTransport(("127.0.0.1", self.echo_port))
        self.forward_server = start_forward(
            transport, local_port, "127.0.0.1", self.echo_port
        )
        time.sleep(0.1)

        stop_forward(self.forward_server)
        self.forward_server = None  # tearDown must not stop it twice

        # A fresh bind to the same port must now succeed -- if the old
        # server were still listening this would raise OSError (address
        # in use).
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", local_port))
        finally:
            probe.close()


if __name__ == "__main__":
    unittest.main()
