"""Test-wide environment defaults, applied before anything imports config.

``config`` resolves ``LOG_FILE`` at import time, so this cannot be a fixture:
by the time a fixture runs, the module-level constant is already bound and the
rotating handler is already pointed at whatever it said. pytest imports
conftest before collecting test modules, which is the only hook early enough.

Why it exists: the suite was writing into the production log. That was fixed
once, for the servers the suite *spawns* -- the browser fixture passes
``WC_LOG_FILE`` into its subprocess -- and the fix was reported as done. But
four test modules drive the app with an in-process ``TestClient``, which imports
``app`` inside the pytest process itself and configures logging there, with
``WC_LOG_FILE`` unset. Those bypassed the subprocess fix entirely and kept
appending: ``ip=testclient``, ``user=alice``, ``chat_id=c1`` and a stored-XSS
probe all appear in logs/webconsole.log, interleaved with real traffic.

The test that was supposed to prevent this asserted the property only for the
redirected-subprocess path, so it passed throughout. A guard that covers one of
two routes reads as covered and is worse than none, because nobody looks again.
"""
from __future__ import annotations

import os
import pathlib
import tempfile

# Not overridden if the caller already set it: test_qa_log_path drives
# WC_LOG_FILE deliberately, and a harness or CI may point it somewhere it
# collects from.
if not os.environ.get("WC_LOG_FILE"):
    _log_dir = pathlib.Path(tempfile.gettempdir()) / "wc-test-logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    # A stable path rather than a fresh directory per run: several sessions run
    # this suite on one machine, and a per-run directory would leave a litter of
    # them. Interleaving between concurrent runs is fine here -- that is the
    # cost this file exists to keep *out* of the production log, not something
    # the test log needs to be protected from.
    os.environ["WC_LOG_FILE"] = str(_log_dir / "suite.log")
