# TODO

Open work, newest first. One heading per item; delete the item when it lands.

## Answer a terminal prompt without needing `screen`

`prompts.py` answers a Claude Code `AskUserQuestion` by finding the multiplexer
window the session runs in (`STY`/`WINDOW` for screen, `TMUX_PANE` for tmux) and
sending arrow keys and Enter into it. That works, and it is verified end to end,
but it means **a session is only answerable from the browser if it happens to be
running inside screen or tmux**. A session started in a plain terminal, over
SSH without a multiplexer, or from a desktop launcher cannot be answered at all
— `locate()` returns `None` and the web UI correctly shows the read-only note.

Keystroke injection is also the wrong shape for the job: it depends on the CLI's
current TUI layout, it has to verify each cursor move because a missed keypress
would select the wrong answer, and kernel-level injection is closed off anyway
(`dev.tty.legacy_tiocsti = 0`).

Wanted: a path that does not go through a terminal at all.

Directions worth probing, cheapest first:

- **`--input-format stream-json` on a resumed session.** Already partly
  explored: `AskUserQuestion` is absent from the tool list of every `--print`
  turn, so the CLI never asks in that mode. Re-check whether a newer CLI
  exposes it, or whether a `tool_result` frame for a pending
  `AskUserQuestion` id can be fed back into a resumed session.
- **Own the PTY.** If the WebConsole starts the session itself under a pty it
  allocated, it can write to that pty directly — no multiplexer, no `screen
  stuff`, and the answer path is the same for every session it launched. Costs a
  process-supervision layer, and does nothing for sessions started by hand.
- **A local control socket.** Check whether the CLI exposes, or can be made to
  expose, an out-of-band channel for answering a pending prompt.

Keep the screen/tmux path as the fallback for sessions the console did not
start; the goal is that it stops being the only path.
