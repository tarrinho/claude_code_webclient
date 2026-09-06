"""Screenshot the orchestrator page's markup and CSS at a given size.

WHAT THIS CAN AND CANNOT DO. It verifies layout: panel geometry, which panel is
where, the `body.max-*` maximize rules, behaviour at narrow widths. It CANNOT
verify anything `web/orchestrator.js` renders, because with that script loaded
headless chromium never exits and never captures a frame at all.

THE CAUSE, bisected. `init()` installs a 30-second `setInterval` to poll the
orchestrator list. `--virtual-time-budget` does not retire while that timer is
outstanding, and the budget is what `--dump-dom` waits on, so chromium never
reaches the point of emitting anything. Three probes on the same binary
(chromium 148.0.7778.178) isolate it:

    web/orchestrator.html, script tag stripped       dumps in 0.6s
    the same page + web/orchestrator.js              never exits
    the same page, that one setInterval neutered   dumps in 1.0s

That last line is the whole finding: one call, not the script as a whole.

This is also why every mitigation tried before that bisection failed, and the
list is kept because the failures were real and correctly measured:

    animations and transitions disabled            timeout, no PNG
    alert/confirm/prompt stubbed                   timeout, no PNG
    EventSource and setInterval neutralised        timeout, no PNG
    --virtual-time-budget=6000 / 2000 / omitted    timeout, no PNG

The third is the instructive one. `setInterval` *was* stubbed — from a script
injected before orchestrator.js — but the page installs its timer through the
reference it had already captured, so the stub never intercepted it and the
budget stayed open. A mitigation that looks like it addresses the cause and
does not is worse than one that obviously misses, because it retires the
hypothesis. Varying the budget could not help either: the budget is not what
fails to expire, it is what never gets the chance to.

So the JS path is opt-in via LIVECHECK_WITH_JS=1 purely to reproduce the hang;
it produces no image. This is the same wall that moved
tests/test_qa_supervisor_ux_shortcuts.py onto playwright.

**Playwright DOES run on this host now.** This paragraph used to say it could
not, and that was correct when written and is worth reading rather than
deleting, because the reason it stopped being correct is that it worked.

It said playwright's driver needs `/usr/bin/node`, which was absent, so 81
browser tests skipped, and installing node was the real unblock. Checked
against the timestamps: this file was committed at 2026-09-01 22:08:12 and
`nodejs` 24.19.0 was installed at 22:12:18 -- four minutes later, evidently in
response. So the diagnosis was right, the recommendation was acted on, and the
text went stale by being taken.

One part was wrong even then, and it is the part worth keeping: the driver does
NOT need the *system* node. Under `.venv` it resolves to playwright's own
bundled `playwright/driver/node`, which was present all along, so the browser
layer was runnable before anyone installed anything -- as `.venv/bin/python -m
pytest` would have shown. That is registry #50: the skips were real under
system `python3` and invisible as skips in an aggregate, which is what made
"cannot run here" the natural reading.

Current state, measured: ux_shortcuts 28 passed; and
tests/test_qa_supervisor_members_picker.py was moved onto playwright and runs
13 tests in ~21s, where `--dump-dom` never finished at all. So this page IS
drivable by playwright if anyone wants to supersede the chromium path below.

`fetch` is stubbed from JSON captured off a running instance, so if the JS ever
does become drivable the data is already wired.

`--user-data-dir` points inside the TemporaryDirectory, so chromium's ~126 MB
profile is removed even when it has to be killed. Without that, each killed run
leaves a `/tmp/org.chromium.Chromium.scoped_dir.*` behind; 81 of them filled a
1.9 GB tmpfs during this work and blocked two sessions.

Usage:
    python bin/livecheck_shot.py <served.html> <json-dir> <out.png> \\
        [action] [width] [height] [body-class]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")

# Derived, never written down. The stubbed /api/settings response used to carry
# a hardcoded "0.9.5", so every screenshot this tool produced showed a version
# the project had left behind -- a layout-check artefact misreporting the build
# it was checking. Reading config.VERSION means it cannot go stale again, which
# is the same reason bin/run-suite-chunked.sh asks pytest for its file list
# instead of keeping a parallel glob.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

VERSION = config.VERSION.split("_", 1)[1]


def build(served: Path, json_dir: Path, action: str, width: int, height: int,
          body_class: str) -> str:
    html = served.read_text(encoding="utf-8")

    payload = {
        "/api/supervisors": json.loads((json_dir / "api_supervisors.json").read_text()),
        "TASKS": json.loads((json_dir / "api_tasks.json").read_text()),
        "MESSAGES": json.loads((json_dir / "api_messages.json").read_text()),
        "MEMBERS": json.loads((json_dir / "api_members.json").read_text()),
        # Travels in the payload rather than being written into the stub, so
        # the screenshot reports the build it is actually checking.
        "SETTINGS": {"version": VERSION},
    }

    if body_class:
        html = html.replace("<body>", f'<body class="{body_class}">', 1)

    # Kill every animation and transition. This is the difference between a
    # screenshot and a hang: the task status dots carry
    # `animation: pulse-amber 1.5s infinite`, and those elements only exist
    # once orchestrator.js has rendered the task list. An infinite animation
    # means the page is never idle, so --virtual-time-budget never exhausts and
    # chromium never exits -- which is why loading orchestrator.js under
    # --dump-dom hangs until its timeout while the same page without the script
    # returns in a second.
    html = html.replace(
        "</style>",
        "\n        *, *::before, *::after { animation: none !important;"
        " transition: none !important; }\n        </style>",
        1,
    )

    stub = """
<script>
(function () {
  var DATA = __PAYLOAD__;
  // Stub before orchestrator.js loads. Routed by suffix because the page builds
  // URLs with the orchestrator id embedded.
  window.fetch = function (url, opts) {
    var body = {};
    if (url.indexOf("/api/supervisors") === 0 && url.indexOf("/", 17) === -1) {
      body = DATA["/api/supervisors"];
    } else if (/\\/tasks$/.test(url))    { body = DATA.TASKS; }
    else if (/\\/messages$/.test(url))   { body = DATA.MESSAGES; }
    else if (/\\/members$/.test(url))    { body = DATA.MEMBERS; }
    else if (/\\/api\\/settings$/.test(url)) { body = DATA.SETTINGS; }
    return Promise.resolve({
      ok: true, status: 200,
      json: function () { return Promise.resolve(body); },
    });
  };
  // Both of these keep the browser alive forever under headless, which is what
  // makes a raw dump-dom of this page hang until its timeout.
  window.EventSource = function () {
    this.close = function () {};
    this.addEventListener = function () {};
  };
  var realSetInterval = window.setInterval;
  window.setInterval = function () { return 0; };
  window.__realSetInterval = realSetInterval;
  document.cookie = "wc_csrf=stub-csrf-token";
})();
</script>
"""
    stub = stub.replace("__PAYLOAD__", json.dumps(payload))
    html = html.replace('<script src="orchestrator.js', stub + '<script src="orchestrator.js', 1)

    after = """
<script>
window.addEventListener("load", function () {
  setTimeout(function () {
    try { __ACTION__ } catch (e) { document.title = "ACTION FAILED: " + e.message; }
  }, 900);
});
</script></body>"""
    after = after.replace("__ACTION__", action or "")
    return html.replace("</body>", after, 1)


def main() -> int:
    served, json_dir, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    action = sys.argv[4] if len(sys.argv) > 4 else ""
    width = int(sys.argv[5]) if len(sys.argv) > 5 else 1440
    height = int(sys.argv[6]) if len(sys.argv) > 6 else 900
    body_class = sys.argv[7] if len(sys.argv) > 7 else ""

    html = build(served, json_dir, action, width, height, body_class)
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html, encoding="utf-8")
        # The page loads `orchestrator.js` by RELATIVE path, so it only executes
        # if a copy sits beside the temp page. It 404s silently otherwise -- a
        # failed script load raises no window error -- and the page then
        # renders its markup and CSS perfectly while running none of its own
        # code, which looks like working software with an empty database.
        #
        # Opt-in, and off by default, because with the script present chromium
        # NEVER EXITS and never even captures a frame. Confirmed here with
        # animations disabled, `alert`/`confirm`/`prompt` stubbed, EventSource
        # and setInterval neutralised, and with and without
        # --virtual-time-budget: every combination times out with no PNG. That
        # is the same wall that moved test_qa_supervisor_ux_shortcuts onto
        # playwright. So this script is good for markup and CSS geometry, and
        # cannot verify anything orchestrator.js renders.
        if os.environ.get("LIVECHECK_WITH_JS") == "1":
            shutil.copy(
                Path(__file__).resolve().parent.parent / "web" / "orchestrator.js",
                Path(tmp) / "orchestrator.js",
            )
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={tmp}/profile",
             f"--window-size={width},{height}",
             "--virtual-time-budget=6000",
             f"--screenshot={out}", f"file://{page}"],
            capture_output=True, text=True, timeout=90, check=False,
        )
    print(f"rc={result.returncode} png={'yes' if out.exists() else 'NO'} "
          f"bytes={out.stat().st_size if out.exists() else 0}")
    if result.returncode != 0:
        print(result.stderr[-400:])
    return 0 if out.exists() else 1


if __name__ == "__main__":
    sys.exit(main())
