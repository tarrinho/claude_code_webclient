# Resolve the Python interpreter that has this project's dependencies.
#
# launch.sh started uvicorn with a bare `python3`. The dependencies live in
# .venv, the system python3 is not it, and the two agreed only by luck. On
# 2026-09-12 they stopped: the Design Specs Gallery added nh3 and Markdown,
# they went into .venv, and `python3 -c 'import app'` began failing at
# specs_gallery.py with ModuleNotFoundError. webconsole.service crashed on
# every start, systemd hit its restart limit and gave up, and the console was
# down about two and a half hours -- a service that never starts writes
# nothing to the log anyone is watching, so the outage was invisible until a
# person tried to use the site.
#
# WC_PYTHON rather than prepending to PATH, for the same reason
# wc-resolve-claude-path.sh uses WC_CLAUDE_PATH: the value is then independent
# of PATH ordering, and an explicit name is greppable when someone later asks
# which interpreter a process actually got.
#
# An already-set WC_PYTHON wins, so a deployment that knows its own interpreter
# is never second-guessed -- same contract as WC_CLAUDE_PATH.
#
# Falls back rather than refusing. CLAUDE.md §8's rule for the CLI applies
# here too: "routing is worth a lot, and not more than being able to work." A
# host with no venv should still serve, loudly, instead of not serving at all.
# The warning is what turns the 2.5-hour silent outage into a line at startup.
#
# The markers are not decoration. tests/test_qa_launch_python_path.py lifts
# everything between them and *executes* it, which is the lesson
# wc-resolve-claude-path.sh already records from Registry #48: nine tests read
# a shell block, none ran it, and a block that could never succeed passed all
# nine. The same shape of bug shipped in transcripts.build_remote_reply_command
# and was found only by running it (031e337).
# >>> python-path-block
if [ -z "${WC_PYTHON:-}" ]; then
    # Anchored to this file, not to $PWD. Keying off the caller's working
    # directory meant sourcing it from anywhere else fell silently back to the
    # system interpreter -- which is the precise failure this file exists to
    # end, reintroduced by the fix for it. Measured: sourced from bin/, $PWD
    # resolution returned /usr/bin/python3 with no warning that a venv was
    # sitting one directory up.
    _py_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
    for _py_candidate in \
        "$_py_root/.venv/bin/python" \
        "$_py_root/.venv/bin/python3" \
        "$PWD/.venv/bin/python"
    do
        if [ -x "$_py_candidate" ]; then
            WC_PYTHON="$_py_candidate"
            break
        fi
    done
    # Last resort: whatever PATH offers, so a host without a venv still starts.
    : "${WC_PYTHON:=$(command -v python3 || command -v python || true)}"
    export WC_PYTHON
fi
case "${WC_PYTHON:-}" in
    */.venv/bin/python*) : ;;  # the interpreter the dependencies were installed for
    "")
        echo "WARNING: no Python interpreter found (WC_PYTHON is empty)." >&2
        echo "         The server cannot start." >&2
        ;;
    *)
        echo "WARNING: starting under '${WC_PYTHON}', not this project's .venv." >&2
        echo "         Any dependency installed only in .venv will fail to import," >&2
        echo "         and the server will exit before it logs anything useful." >&2
        ;;
esac
# <<< python-path-block
