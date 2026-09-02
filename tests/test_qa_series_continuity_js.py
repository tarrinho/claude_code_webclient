"""QA: the chart renders a hole as a break, not as a reading of zero.

The Python half of this lives in tests/test_qa_series_continuity.py: the series
now arrive on a continuous bucket spine, and a bucket the sampler never wrote
comes back with null metrics. This half checks the three pure functions that
decide what the browser does with those nulls.

`server.js` used `Number(row[field]) || 0`, which turns null into a reading of
zero. Left alone, the continuous spine would have made the Server page *worse*
than the gap it replaced: instead of an axis that skipped an outage, the chart
would have drawn a confident flat line along 0% CPU and 0% memory for exactly
as long as the sampler was down. A gap that is visible is a smaller lie than a
measurement that was never taken.

The functions are lifted out of the modules and run in chromium, which is how
every other JS harness here works: the page cannot `import` over file://, and
node is not installed on this host.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATS_JS = REPO / "web" / "assets" / "stats.js"
SERVER_JS = REPO / "web" / "assets" / "server.js"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def lift(path: Path, name: str) -> str:
    """The full text of `export function <name>(...)`, braces matched.

    Matched by depth rather than by a closing-brace pattern, because every one
    of these functions contains nested blocks and a regex would stop at the
    first `}` inside the body.
    """
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"export function {re.escape(name)}\s*\(", source)
    if match is None:
        raise AssertionError(
            f"{name}() is not exported from {path.name}. If it was renamed or "
            "inlined, update this harness rather than letting it skip"
        )
    start = match.start()
    open_brace = source.index("{", match.end() - 1)
    depth, pos = 1, open_brace + 1
    while depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos].replace("export function", "function", 1)


# Each entry: a label, and JS that must evaluate truthy.
CHECKS: list[tuple[str, str]] = [
    ("a null splits one line into two",
     "segments([1, null, 2]).length === 2"),
    ("an unbroken run stays one line",
     "segments([1, 2, 3]).length === 1 && segments([1,2,3])[0].length === 3"),
    ("all holes draw nothing",
     "segments([null, null]).length === 0"),
    ("indices survive the split, so x stays aligned to the bucket",
     ("JSON.stringify(segments([1, null, null, 4]).map(r => r.map(p => p[0])))"
      " === '[[0],[3]]'")),
    ("NaN is a hole too, not a point off the canvas",
     "segments([NaN, 1]).length === 1 && segments([NaN,1])[0][0][0] === 1"),
    ("undefined is a hole",
     "segments([undefined, 1]).length === 1"),
    ("zero is a reading, not a hole",
     "segments([0, 0]).length === 1 && segments([0,0])[0].length === 2"),

    # toSeries: the usage side, where an empty bucket really is zero.
    ("the spine adds buckets no row falls into",
     ("toSeries([{bucket:'h1',k:'a',v:5}], 'k', r => r.v,"
      " ['h0','h1','h2']).buckets.length === 3")),
    ("an added bucket is zero-filled for every series",
     ("JSON.stringify(toSeries([{bucket:'h1',k:'a',v:5}], 'k', r => r.v,"
      " ['h0','h1','h2']).series[0].values) === '[0,5,0]'")),
    ("a row outside the spine is kept, not dropped",
     ("toSeries([{bucket:'zz',k:'a',v:1}], 'k', r => r.v,"
      " ['h0']).buckets.indexOf('zz') !== -1")),
    ("no spine still works, for callers that pass none",
     "toSeries([{bucket:'h1',k:'a',v:5}], 'k', r => r.v).buckets.length === 1"),

    # seriesFrom: the host side, where an empty bucket is no measurement.
    ("null survives instead of becoming zero",
     ("seriesFrom([{bucket:'b1',cpu:null}], [{key:'c',field:'cpu'}])"
      ".series[0].values[0] === null")),
    ("a hole does not drag the mean towards zero",
     ("seriesFrom([{bucket:'b1',cpu:10},{bucket:'b2',cpu:null}],"
      " [{key:'c',field:'cpu'}]).series[0].mean === 10")),
    ("a hole does not become the peak",
     ("seriesFrom([{bucket:'b1',cpu:10},{bucket:'b2',cpu:null}],"
      " [{key:'c',field:'cpu'}]).series[0].peak === 10")),
    ("an all-hole series reports no peak rather than crashing",
     ("seriesFrom([{bucket:'b1',cpu:null}], [{key:'c',field:'cpu'}])"
      ".series[0].peak === 0")),
    ("a real zero reading is still zero, not a hole",
     ("seriesFrom([{bucket:'b1',cpu:0}], [{key:'c',field:'cpu'}])"
      ".series[0].values[0] === 0")),
]

# Each check is emitted as a function rather than a string fed to eval(). Not
# only to avoid eval: a malformed expression then fails when the page parses,
# which leaves the title at "pending" and trips the guard below, instead of
# throwing at call time and being counted as an ordinary failed assertion.
PAGE = """<!doctype html><meta charset="utf-8"><title>pending</title>
<script>
%(functions)s
const checks = [
%(checks)s
];
const failed = [];
for (const [label, check] of checks) {
  let ok = false;
  try { ok = check(); } catch (e) { ok = false; }
  if (!ok) failed.push(label);
}
document.title = failed.length ? 'FAIL: ' + failed.join(' | ') : 'OK';
</script>
"""


@unittest.skipUnless(CHROMIUM, "chromium not installed")
class ChartHoleTests(unittest.TestCase):
    """One browser launch for every check, because launching is the slow part."""

    def test_holes_are_breaks_and_zeros_are_readings(self):
        functions = "\n".join([
            lift(STATS_JS, "segments"),
            lift(STATS_JS, "toSeries"),
            lift(SERVER_JS, "seriesFrom"),
        ])
        html = PAGE % {
            "functions": functions,
            "checks": ",\n".join(
                f"  [{json.dumps(label)}, () => ({expr})]"
                for label, expr in CHECKS
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "probe.html"
            page.write_text(html, encoding="utf-8")
            result = subprocess.run(
                [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
                 # Chromium writes a ~126 MB profile per launch and leaves it
                 # behind if it does not exit cleanly. Pointing it at the page
                 # directory puts it where this test's own cleanup removes it.
                 f"--user-data-dir={page.parent}/chrome-profile",
                 "--virtual-time-budget=5000", "--dump-dom", f"file://{page}"],
                capture_output=True, text=True, timeout=120, check=False,
            )
        match = re.search(r"<title>([^<]*)</title>", result.stdout)
        title = match.group(1) if match else ""
        self.assertNotEqual(
            title, "pending",
            "the page never ran its script, so this asserts nothing -- "
            f"chromium said: {result.stderr[-300:]}",
        )
        self.assertEqual(title, "OK", title)


if __name__ == "__main__":
    unittest.main()
