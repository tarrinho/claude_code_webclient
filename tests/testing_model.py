"""The model id a test should use, unless the specific value is itself the
thing under test.

Resolved once, by tests/conftest.py, before any test file is collected --
this module only reads what conftest.py already set into WC_TESTING_MODEL.
Safe to import from any test file: conftest.py is guaranteed by pytest to
run first, in this directory, for every test it collects.

Usage:

    from tests.testing_model import TESTING_MODEL

    machine = {"model": TESTING_MODEL, ...}

Do NOT use this for a test whose point is a specific model's behaviour or
identity -- e.g. asserting a gateway machine's active_models never contains a
bare "claude-*" id, or that Haiku is priced differently from Opus. Those
literals stay hardcoded; swapping them for TESTING_MODEL would make the test
assert something true by coincidence rather than by the property it exists to
check.
"""
from __future__ import annotations

import os

TESTING_MODEL = os.environ.get("WC_TESTING_MODEL", "claude-opus-5")
