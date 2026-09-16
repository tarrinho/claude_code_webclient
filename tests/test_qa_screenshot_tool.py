"""QA: bin/wc-screenshot.py's argument surface and its two safety guards.

Everything that needs a real browser or a real database write is out of
scope here -- tests/test_frontend_browser.py already proves Playwright
against this app works, and a screenshot tool that launched Chromium in its
own test suite would be slow for no extra coverage. What this file pins
instead is the part that has no browser dependency and is exactly where a
tool like this goes wrong unsupervised:

* the argument surface and its defaults (registration opt-in chief among
  them),
* the size/existence guard that stands in for "did the capture actually
  work" (see bin/wc-screenshot.py's `_check_capture_ok` docstring for what it
  can and cannot catch),
* that `main()` only ever calls the registration path when `--register` is
  given, and
* that the work_dir/path relationship the registration code accepts is the
  same one `routes/db_images.py`'s `_resolved_inside` accepts -- registering
  something that function would then refuse to serve back would be a row
  pointing at an image the app treats as missing.

The module is loaded from its path because bin/ is not a package and the
file has a hyphenated name -- same recipe as
tests/test_qa_transcript_doctor.py uses for bin/claude-transcript-doctor.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "bin" / "wc-screenshot.py"

_spec = importlib.util.spec_from_file_location("wc_screenshot", TOOL_PATH)
wc_screenshot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wc_screenshot)

from routes import db_images  # noqa: E402  (real function, for the last group)


def _write_png(path: Path, size: int) -> None:
    """A file that merely has PNG-plausible size -- the guard is a byte
    count, not a decoder, so content beyond size does not matter here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * max(size - 8, 0))


class ArgumentSurfaceTests(unittest.TestCase):
    """Defaults, and that registration is off unless asked for."""

    def test_only_out_is_required(self):
        args = wc_screenshot.build_parser().parse_args(["--out", "/tmp/x.png"])
        self.assertEqual(args.out, Path("/tmp/x.png"))

    def test_defaults(self):
        args = wc_screenshot.build_parser().parse_args(["--out", "/tmp/x.png"])
        self.assertEqual(args.path, "/")
        self.assertIsNone(args.selector)
        self.assertIsNone(args.settings_tab)
        self.assertFalse(args.seed_delegation)
        self.assertFalse(args.register)
        self.assertIsNone(args.register_db_path)
        self.assertIsNone(args.work_dir)
        self.assertIsNone(args.chat_title)
        self.assertEqual(args.wait_ms, 800)
        self.assertEqual(args.min_bytes, wc_screenshot.DEFAULT_MIN_BYTES)
        self.assertEqual(
            args.owner_id, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "must match the placeholder owner the existing gallery rows use",
        )
        self.assertEqual(
            args.clear_scroll_clip,
            [".settings-dialog", ".settings-body", "#settingsDialog"],
            "the three selectors known to carry the inline scroll-clip",
        )

    def test_settings_tab_is_restricted_to_known_panels(self):
        with self.assertRaises(SystemExit):
            wc_screenshot.build_parser().parse_args(
                ["--out", "/tmp/x.png", "--settings-tab", "not-a-real-tab"]
            )

    def test_out_is_required(self):
        with self.assertRaises(SystemExit):
            wc_screenshot.build_parser().parse_args([])

    def test_register_requires_chat_title_before_anything_launches(self):
        """main() must refuse --register without --chat-title without ever
        booting a server -- if it required a server first, this check would
        be the last thing to run rather than the first."""
        with mock.patch.object(wc_screenshot, "_Server") as server_cls:
            with self.assertRaises(SystemExit) as ctx:
                wc_screenshot.main(["--out", "/tmp/x.png", "--register"])
            self.assertEqual(ctx.exception.code, 2)
            server_cls.assert_not_called()

    def test_registration_is_off_unless_the_flag_is_given(self):
        """main() must call the registration path only when --register is
        passed, and never call it otherwise -- a capture-only run must not
        touch the gallery at all."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "shot.png"

            fake_server = mock.MagicMock()
            fake_server.db_path = Path(tmp) / "wc.db"

            def fake_capture(args, server):
                _write_png(args.out, wc_screenshot.DEFAULT_MIN_BYTES + 100)

            with mock.patch.object(
                wc_screenshot, "_Server", return_value=fake_server
            ), mock.patch.object(
                wc_screenshot, "_capture", side_effect=fake_capture
            ), mock.patch.object(
                wc_screenshot, "_register", new_callable=mock.AsyncMock
            ) as register_mock:
                rc = wc_screenshot.main(["--out", str(out)])
                self.assertEqual(rc, 0)
                register_mock.assert_not_called()
                fake_server.close.assert_called_once()

            fake_server.close.reset_mock()
            with mock.patch.object(
                wc_screenshot, "_Server", return_value=fake_server
            ), mock.patch.object(
                wc_screenshot, "_capture", side_effect=fake_capture
            ), mock.patch.object(
                wc_screenshot, "_register", new_callable=mock.AsyncMock
            ) as register_mock:
                rc = wc_screenshot.main(
                    ["--out", str(out), "--register", "--chat-title", "t"]
                )
                self.assertEqual(rc, 0)
                register_mock.assert_awaited_once()

    def test_seed_delegation_runs_only_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "shot.png"
            fake_server = mock.MagicMock()
            fake_server.db_path = Path(tmp) / "wc.db"

            def fake_capture(args, server):
                _write_png(args.out, wc_screenshot.DEFAULT_MIN_BYTES + 100)

            with mock.patch.object(
                wc_screenshot, "_Server", return_value=fake_server
            ), mock.patch.object(
                wc_screenshot, "_capture", side_effect=fake_capture
            ), mock.patch.object(
                wc_screenshot, "_seed_delegation"
            ) as seed_mock:
                wc_screenshot.main(["--out", str(out)])
                seed_mock.assert_not_called()

                seed_mock.reset_mock()
                wc_screenshot.main(["--out", str(out), "--seed-delegation"])
                seed_mock.assert_called_once_with(fake_server.db_path)


class CaptureGuardTests(unittest.TestCase):
    """`_check_capture_ok` stands in for "did the capture actually work"."""

    def test_missing_file_is_rejected(self):
        reason = wc_screenshot._check_capture_ok(
            Path("/tmp/does-not-exist-wc-screenshot.png"), 2048,
        )
        self.assertIsNotNone(reason)
        self.assertIn("does not exist", reason)

    def test_zero_byte_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.png"
            path.write_bytes(b"")
            reason = wc_screenshot._check_capture_ok(path, 2048)
            self.assertIsNotNone(reason)
            self.assertIn("0 bytes", reason)

    def test_a_few_hundred_bytes_is_rejected(self):
        """The exact failure mode named in the tool's docstring: a
        plausible-looking but too-small PNG, not only a literally empty
        one."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.png"
            _write_png(path, 300)
            reason = wc_screenshot._check_capture_ok(path, 2048)
            self.assertIsNotNone(reason)
            self.assertIn("300 bytes", reason)

    def test_a_real_sized_file_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.png"
            _write_png(path, 4096)
            self.assertIsNone(wc_screenshot._check_capture_ok(path, 2048))

    def test_a_directory_is_rejected_not_mistaken_for_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            reason = wc_screenshot._check_capture_ok(Path(tmp), 2048)
            self.assertIsNotNone(reason)
            self.assertIn("not a file", reason)


class RegistrationContainmentTests(unittest.TestCase):
    """The work_dir/path relationship _register accepts must be exactly the
    one routes/db_images.py's _resolved_inside accepts -- anything else
    would register a row the app's own read path then refuses to serve."""

    def _args(self, out: Path, work_dir: Path | None, db_path: Path) -> "object":
        parser = wc_screenshot.build_parser()
        argv = ["--out", str(out), "--register", "--chat-title", "t",
                "--register-db-path", str(db_path)]
        if work_dir is not None:
            argv += ["--work-dir", str(work_dir)]
        return parser.parse_args(argv)

    def test_default_work_dir_is_outs_parent_and_resolved_inside_accepts_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gallery" / "shot.png"
            _write_png(out, 4096)
            args = self._args(out, work_dir=None, db_path=Path(tmp) / "fake.db")

            fake_conn = mock.AsyncMock()
            record_mock = mock.AsyncMock()
            with mock.patch.object(
                wc_screenshot.aiosqlite, "connect",
                new=mock.AsyncMock(return_value=fake_conn),
            ), mock.patch.object(
                wc_screenshot.db_images, "generated_image_record", new=record_mock,
            ), mock.patch.object(
                wc_screenshot.db, "close", new=mock.AsyncMock(),
            ):
                asyncio.run(wc_screenshot._register(args))

            record_mock.assert_awaited_once()
            kwargs = record_mock.await_args.kwargs
            self.assertEqual(kwargs["work_dir"], str(out.parent.resolve()))
            self.assertEqual(kwargs["paths"], ["shot.png"])

            # The relationship this just registered must be exactly the one
            # the read path accepts -- checked against the real function,
            # not a mock of it.
            resolved = db_images._resolved_inside(kwargs["work_dir"], "shot.png")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved, out.resolve())

    def test_explicit_work_dir_containing_out_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "project"
            out = work_dir / "sub" / "shot.png"
            _write_png(out, 4096)
            args = self._args(out, work_dir=work_dir, db_path=Path(tmp) / "fake.db")

            record_mock = mock.AsyncMock()
            with mock.patch.object(
                wc_screenshot.aiosqlite, "connect",
                new=mock.AsyncMock(return_value=mock.AsyncMock()),
            ), mock.patch.object(
                wc_screenshot.db_images, "generated_image_record", new=record_mock,
            ), mock.patch.object(
                wc_screenshot.db, "close", new=mock.AsyncMock(),
            ):
                asyncio.run(wc_screenshot._register(args))

            kwargs = record_mock.await_args.kwargs
            self.assertEqual(kwargs["paths"], ["sub/shot.png"])
            resolved = db_images._resolved_inside(kwargs["work_dir"], "sub/shot.png")
            self.assertEqual(resolved, out.resolve())

    def test_work_dir_not_containing_out_is_refused_before_any_db_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "elsewhere" / "shot.png"
            _write_png(out, 4096)
            unrelated_work_dir = Path(tmp) / "unrelated"
            unrelated_work_dir.mkdir()
            args = self._args(
                out, work_dir=unrelated_work_dir, db_path=Path(tmp) / "fake.db",
            )

            record_mock = mock.AsyncMock()
            with mock.patch.object(
                wc_screenshot.aiosqlite, "connect",
                new=mock.AsyncMock(return_value=mock.AsyncMock()),
            ), mock.patch.object(
                wc_screenshot.db_images, "generated_image_record", new=record_mock,
            ):
                with self.assertRaises(SystemExit) as ctx:
                    asyncio.run(wc_screenshot._register(args))
                self.assertIn("does not resolve to a path inside work_dir",
                               str(ctx.exception))
            record_mock.assert_not_awaited()

    def test_register_refuses_a_too_small_capture_before_touching_the_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "shot.png"
            _write_png(out, 50)
            args = self._args(out, work_dir=None, db_path=Path(tmp) / "fake.db")

            record_mock = mock.AsyncMock()
            with mock.patch.object(
                wc_screenshot.db_images, "generated_image_record", new=record_mock,
            ):
                with self.assertRaises(SystemExit) as ctx:
                    asyncio.run(wc_screenshot._register(args))
                self.assertIn("refusing to register", str(ctx.exception))
            record_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
