#!/usr/bin/env bash
# Run the test suite in chunks, each in its own pytest process.
#
# The whole suite in one process is killed by the OOM killer on this box: 3.8G
# total with the live server already resident, and 86 test files' fixtures
# accumulating across a single run. A fresh process per chunk returns the
# memory at every boundary, which a single run never does.
#
# Chunks are deliberately small and the browser files run one at a time --
# chromium is the heaviest tenant and the one that pushes a chunk over.
#
# Per-chunk results land in $OUT/ so a chunk that dies is visible as itself
# rather than as a missing slice of an aggregate (registry #50).
#
# WHAT THIS IS NOT: a chunked run and a single-process run are not equivalent,
# and a green result here does not license the claim that the whole suite is
# green. `logging.config.fileConfig` closes every existing handler, which is
# what once failed 794 tests for no real reason (registry #30), so parts of
# this suite are order- and process-sensitive by construction. Splitting a
# group of tests across a chunk boundary can hide an interaction that a single
# process would expose. Use this to get a trustworthy reading on a box too
# small for one run, and to localise failures -- then confirm anything you are
# about to release on with a single full run when the machine is quiet.
#
# Chunk boundaries are per-FILE, never inside one, so a module's own ordering
# and shared fixture state stay intact. That is the property that makes this
# usable at all; do not "optimise" it into splitting by test id.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PY=.venv/bin/python
OUT="${WC_SUITE_OUT:-/tmp/wc-suite-$$}"
mkdir -p "$OUT"

# The file list comes from PYTEST'S OWN COLLECTION, never from a glob here.
#
# It used to be `ls tests/test_*.py`, which silently omitted the one test file
# that does not live under tests/ -- `test_functional.py`, at the repository
# root. Every chunked run therefore reported 0 failed while a real failure sat
# in it, and 0.9.4 was verified and released against that number. A bare `ls`
# cannot notice a file it was never pointed at, so the runner was wrong in the
# reassuring direction: registry #50 again, in the tool built to prevent it,
# for the second time (the aggregator was the first).
#
# Asking pytest what it would run makes the two lists incapable of diverging.
# If a file is added anywhere pytest collects it, it lands in a chunk without
# anyone remembering to update this script.
ALL_FILES=$("$PY" -m pytest --collect-only -q 2>/dev/null \
  | grep -oE '^[^:]+\.py' | sort -u)

if [ -z "$ALL_FILES" ]; then
  echo "FATAL: pytest collected no files. Refusing to report a green run."
  exit 1
fi

# Browser files run alone; everything else goes in groups of six.
BROWSER_FILES=$(grep -ln 'playwright\|sync_playwright' $ALL_FILES 2>/dev/null | sort)
PLAIN_FILES=$(comm -23 <(echo "$ALL_FILES") <(echo "$BROWSER_FILES"))

# Cross-check the chunk plan against collection, so a file cannot be dropped
# between here and the loops below without the run refusing to start.
planned=$(( $(echo "$PLAIN_FILES" | grep -c .) + $(echo "$BROWSER_FILES" | grep -c .) ))
collected=$(echo "$ALL_FILES" | grep -c .)
if [ "$planned" -ne "$collected" ]; then
  echo "FATAL: $collected files collected but $planned planned -- refusing to run."
  exit 1
fi

echo "collected     : $collected files (from pytest, not a glob)"
echo "browser files : $(echo "$BROWSER_FILES" | grep -c . )"
echo "plain files   : $(echo "$PLAIN_FILES" | grep -c . )"
echo "results dir   : $OUT"
echo

run_chunk() {
  local name="$1"; shift
  local log="$OUT/$name.log"
  "$PY" -m pytest -rs -q "$@" >"$log" 2>&1
  local rc=$?
  local summary
  # The same rule the aggregator below uses: the last line that actually
  # carries counts. The earlier pattern here alternated on the bare words
  # (`|passed|failed|error`), so it would happily print a SKIPPED reason or a
  # traceback line containing one of them as though it were the summary. It
  # agrees with the aggregator on today's logs, which is exactly how two
  # copies of one rule stay wrong together until they do not.
  summary=$(grep -E '[0-9]+ (passed|failed|error|skipped)' "$log" | tail -1)
  printf '%-28s rc=%-3s %s\n' "$name" "$rc" "${summary:-<no summary — killed?>}"
  echo "$rc" >"$OUT/$name.rc"
}

# Plain files, six at a time.
i=0
chunk=()
while read -r f; do
  [ -z "$f" ] && continue
  chunk+=("$f")
  if [ "${#chunk[@]}" -eq 6 ]; then
    i=$((i+1))
    run_chunk "$(printf 'plain-%02d' "$i")" "${chunk[@]}"
    chunk=()
  fi
done <<<"$PLAIN_FILES"
if [ "${#chunk[@]}" -gt 0 ]; then
  i=$((i+1))
  run_chunk "$(printf 'plain-%02d' "$i")" "${chunk[@]}"
fi

# Browser files, one at a time.
while read -r f; do
  [ -z "$f" ] && continue
  run_chunk "browser-$(basename "$f" .py)" "$f"
done <<<"$BROWSER_FILES"

echo
echo "=== aggregate ==="
"$PY" - "$OUT" <<'PY'
import pathlib, re, sys

# The counts live on the LAST line that carries them. Under `-q` pytest does
# not wrap that line in `=== ... ===`, so an earlier version of this scanned
# for the banner form, matched nothing, and printed `passed=0` beside eighteen
# chunks that had in fact passed -- while listing every one of them as
# suspect. It was the registry #50 failure inside the instrument built to
# avoid registry #50: a total that is wrong in the safe-looking direction.
# Parse the last counts-bearing line and treat its absence as the alarm.
out = pathlib.Path(sys.argv[1])
tot = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
rows, bad = [], []
for log in sorted(out.glob("*.log")):
    rc_file = out / f"{log.stem}.rc"
    rc = rc_file.read_text().strip() if rc_file.exists() else "?"
    lines = [ln.strip() for ln in log.read_text(errors="replace").splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines)
                    if re.search(r"\d+ (passed|failed|error|skipped)", ln)), "")
    if not summary:
        # No counts at all: killed (137), crashed, or collection died.
        bad.append((log.stem, rc, "NO SUMMARY -- killed or crashed"))
        continue
    for key in tot:
        m = re.search(rf"(\d+) {key}", summary)
        if m:
            tot[key] += int(m.group(1))
    rows.append((log.stem, rc, summary))
    if rc not in ("0", "5"):          # 5 = no tests collected
        bad.append((log.stem, rc, summary))

width = max((len(n) for n, _, _ in rows + bad), default=20)
for name, rc, summary in rows:
    print(f"  {name:<{width}}  rc={rc:<3} {summary}")
print()
print("TOTAL passed={passed} failed={failed} skipped={skipped} error={error}".format(**tot))
print(f"chunks: {len(rows)} reported, {len(bad)} needing attention")
if bad:
    print("\nNEEDS ATTENTION:")
    for name, rc, summary in bad:
        print(f"  {name} (rc={rc}): {summary}")
    sys.exit(1)
PY
