"""QA: every place that states the version agrees with config.VERSION.

The version is the one value in this repository that does not merge. Code with
two authors reconciles; a version with two authors is simply wrong, and then
misreports what is running. Over two days it was set to five different numbers
across six files by four sessions working at once, two tests failed on the
disagreement rather than on any defect, and the same line was overwritten twice
within seconds.

So "bump the version everywhere it is required" is enforced here rather than
remembered. `config.VERSION` is the source; everything below must match it.

Deliberately **not** covered: statements of when something was introduced --
``-- Supervisor orchestration tables (0.9.0)`` in db.py, and the module headers
that say which release a file arrived in. Those are provenance and are correct
as written; rewriting them on every bump would destroy the only record of when
the code appeared.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent.parent

# Files that state the current version to a reader, and how to find it. Add a
# row here when a new surface starts displaying the version -- a missing row is
# how a release half-lands.
STATED = (
    ("ARCHITECTURE.md", r"^\*\*Version:\*\*\s*(\S+)"),
    ("web/index.html", r'<span class="ver" id="ver">([^<]+)</span>'),
    ("web/supervisor.html", r"<title>Supervisor — WebConsole ([^<]+)</title>"),
    ("web/supervisor.html", r'id="topbar-info">([^<]+)</span>'),
    ("web/supervisor.js", r'topbarInfo\.textContent\s*=\s*"([^"]+)"'),
)


def semver() -> str:
    return config.VERSION.removeprefix("WebConsole_")


class VersionConsistencyQA(unittest.TestCase):
    def test_the_constant_is_well_formed(self):
        self.assertTrue(config.VERSION.startswith("WebConsole_"), config.VERSION)
        self.assertRegex(semver(), r"^\d+\.\d+\.\d+")

    def test_every_stated_version_matches_the_constant(self):
        expected = semver()
        for name, pattern in STATED:
            with self.subTest(file=name, pattern=pattern):
                text = (ROOT / name).read_text(encoding="utf-8")
                found = re.search(pattern, text, re.MULTILINE)
                self.assertIsNotNone(
                    found,
                    f"{name}: pattern no longer matches. If the markup moved, "
                    f"update STATED; if the version was removed from this file, "
                    f"delete the row.",
                )
                self.assertEqual(
                    found.group(1).strip(), expected,
                    f"{name} says {found.group(1).strip()!r} but config.VERSION "
                    f"says {expected!r}. A version that disagrees with itself "
                    f"misreports what is running.",
                )

    def test_the_changelog_leads_with_this_release(self):
        """The newest numbered section must be the version being shipped.

        Catches the other half of a half-landed bump: a constant moved forward
        with nothing written down about what changed.
        """
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        sections = re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)
        numbered = [s for s in sections if s[0].isdigit()]
        self.assertTrue(numbered, "no numbered sections in CHANGELOG.md")
        self.assertEqual(
            numbered[0], semver(),
            f"CHANGELOG's newest release is {numbered[0]}, but this build is "
            f"{semver()}.",
        )

    def test_the_changelog_records_each_number_once(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        numbered = [
            s for s in re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)
            if s[0].isdigit()
        ]
        duplicates = {v for v in numbered if numbered.count(v) > 1}
        self.assertFalse(duplicates, f"duplicated release sections: {duplicates}")

    def test_releases_are_listed_newest_first(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        numbered = [
            s for s in re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)
            if s[0].isdigit()
        ]

        def key(value: str) -> tuple[int, ...]:
            return tuple(int(part) for part in re.findall(r"\d+", value)[:3])

        self.assertEqual(
            numbered, sorted(numbered, key=key, reverse=True),
            "CHANGELOG.md is documented newest-first; this ordering broke.",
        )

    def test_no_test_hardcodes_the_release_number(self):
        """Tests must derive the version, not restate it.

        Two did, and every bump broke both -- which with several sessions in one
        tree made the assertion a place to disagree about the number rather than
        a check on the code. Fixture data that merely needs *a* version string is
        exempt: it asserts nothing about this one.
        """
        expected = semver()
        offenders = []
        for path in (ROOT / "tests").glob("test_*.py"):
            if path.name == Path(__file__).name:
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if expected not in line:
                    continue
                if "assert" in line:
                    offenders.append(f"{path.name}:{number}: {line.strip()[:70]}")
        self.assertFalse(
            offenders,
            "these assert on the literal version instead of config.VERSION:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
