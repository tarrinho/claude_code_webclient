# Resolve the Claude binary to an absolute path, because systemd --user starts
# both callers of this file with a PATH that does not include ~/.local/bin --
# where the CLI actually lives. Without it `shutil.which("claude")` in
# claude_proxy.py, or a bare `claude` spawn anywhere else in the app, resolves
# to nothing and the caller fails with "claude binary not found" or "Could not
# start claude" -- surfaced to a user only as a failed turn or a failed Test
# click, never as the actual cause.
#
# WC_CLAUDE_PATH rather than an `export PATH=` prefix, deliberately: the value
# is then independent of PATH ordering, and every reader of it
# (`os.environ.get("WC_CLAUDE_PATH", "claude")` in claude_proxy.py and in
# routes/machines.py's Test-button probe) already expects exactly this name.
#
# Sourced by both bin/wc-proxy-run.sh (the proxy process) and launch.sh (the
# main app process): the main app started spawning `claude` directly too, for
# the machine Test button, which needed this same resolution a second time.
# Extracted here instead of duplicated, because duplicating it is exactly the
# drift this file's own history already warns about -- see below.
#
# This has now been broken twice by the same mechanism, before this file
# existed at all. The first fix was an `export PATH=...` line that lived only
# in the shared working tree, was never committed, and was silently reverted
# by another session's checkout -- so the regression lay dormant until the
# next proxy restart and then broke every turn with no error anywhere.
# Registry #49 and #59. The third occurrence (the Test button spawning
# `claude` from the main app process, which never had WC_CLAUDE_PATH set for
# it at all) is what caused this file to be split out of bin/wc-proxy-run.sh:
# a second inline copy in launch.sh would have been a second place to forget.
# Pinned by tests/test_qa_proxy_claude_path.py so a fourth loss fails the
# suite instead of the site.
#
# The markers are not decoration: tests/test_qa_proxy_claude_path.py lifts
# everything between them and executes it under a minimal PATH, the way
# systemd starts both callers. Registry #48 is the reason -- nine tests there
# read a shell block and none ran it, so a block that could never succeed
# passed every one.
# >>> claude-path-block
if [ -z "${WC_CLAUDE_PATH:-}" ]; then
    for _candidate in \
        "$HOME/.local/bin/claude" \
        "/usr/local/bin/claude" \
        "/usr/bin/claude"
    do
        if [ -x "$_candidate" ]; then
            WC_CLAUDE_PATH="$_candidate"
            break
        fi
    done
    # Last resort: whatever PATH can find, so a machine that installs the CLI
    # somewhere else still starts rather than refusing to run.
    : "${WC_CLAUDE_PATH:=$(command -v claude || true)}"
    export WC_CLAUDE_PATH
fi
if [ -z "${WC_CLAUDE_PATH:-}" ] || [ ! -x "$WC_CLAUDE_PATH" ]; then
    # Said loudly at startup rather than discovered one failed turn at a time.
    echo "WARNING: no executable claude binary found (WC_CLAUDE_PATH='${WC_CLAUDE_PATH:-}')." >&2
    echo "         Every turn will fail with 'claude binary not found'." >&2
fi
# <<< claude-path-block
