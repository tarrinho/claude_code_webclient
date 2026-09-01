"""Screenshot the supervisor page's markup and CSS at a given size.

WHAT THIS CAN AND CANNOT DO. It verifies layout: panel geometry, which panel is
where, the `body.max-*` maximize rules, behaviour at narrow widths. It CANNOT
verify anything `web/supervisor.js` renders, because with that script loaded
headless chromium never exits and never captures a frame at all.

That was measured, not assumed. With the script present, every one of these
still timed out with no PNG produced:

    animations and transitions disabled            timeout, no PNG
    alert/confirm/prompt stubbed                   timeout, no PNG
    EventSource and setInterval neutralised        timeout, no PNG
    --virtual-time-budget=6000 / 2000 / omitted    timeout, no PNG

So the JS path is opt-in via LIVECHECK_WITH_JS=1 purely to reproduce the hang;
it produces no image. This is the same wall that moved
tests/test_qa_supervisor_ux_shortcuts.py onto playwright, and playwright cannot
run on this host either -- its driver needs /usr/bin/node, which is absent, so
81 browser tests currently skip rather than run. Installing node is the real
unblock for browser-level verification of this page.

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


def build(served: Path, json_dir: Path, action: str, width: int, height: int,
          body_class: str) -> str:
    html = served.read_text(encoding="utf-8")

    payload = {
        "/api/supervisors": json.loads((json_dir / "api_supervisors.json").read_text()),
        "TASKS": json.loads((json_dir / "api_tasks.json").read_text()),
        "MESSAGES": json.loads((json_dir / "api_messages.json").read_text()),
        "MEMBERS": json.loads((json_dir / "api_members.json").read_text()),
    }

    if body_class:
        html = html.replace("<body>", f'<body class="{body_class}">', 1)

    # Kill every animation and transition. This is the difference between a
    # screenshot and a hang: the task status dots carry
    # `animation: pulse-amber 1.5s infinite`, and those elements only exist
    # once supervisor.js has rendered the task list. An infinite animation
    # means the page is never idle, so --virtual-time-budget never exhausts and
    # chromium never exits -- which is why loading supervisor.js under
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
  // Stub before supervisor.js loads. Routed by suffix because the page builds
  // URLs with the supervisor id embedded.
  window.fetch = function (url, opts) {
    var body = {};
    if (url.indexOf("/api/supervisors") === 0 && url.indexOf("/", 17) === -1) {
      body = DATA["/api/supervisors"];
    } else if (/\\/tasks$/.test(url))    { body = DATA.TASKS; }
    else if (/\\/messages$/.test(url))   { body = DATA.MESSAGES; }
    else if (/\\/members$/.test(url))    { body = DATA.MEMBERS; }
    else if (/\\/api\\/settings$/.test(url)) { body = {version: "0.9.5"}; }
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
    html = html.replace('<script src="supervisor.js', stub + '<script src="supervisor.js', 1)

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
        # The page loads `supervisor.js` by RELATIVE path, so it only executes
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
        # cannot verify anything supervisor.js renders.
        if os.environ.get("LIVECHECK_WITH_JS") == "1":
            shutil.copy(
                Path(__file__).resolve().parent.parent / "web" / "supervisor.js",
                Path(tmp) / "supervisor.js",
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
