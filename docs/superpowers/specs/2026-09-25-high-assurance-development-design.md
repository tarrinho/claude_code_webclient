# High-Assurance Development: Design

**Date:** 2026-09-25
**Status:** Draft for review
**Author:** Pedro Tarrinho

## 1. Where this comes from

Two sources, and they agree.

The first is external: the talks at <https://webappsec.dev/slides/>, by Lukas
Weichselbaum of Google. Seven decks between 2019 and 2026, all arguing one
thing — per-application fixes do not scale, so the fix belongs in the platform
where it applies by default.

The measurements that matter:

- Automated tools bypass **over 95% of allowlist-based CSPs**. An allowlist is
  not a weaker policy than a nonce; it is close to no policy at all.
- Strict CSP blocked **60-80% of externally reported XSS** at Google's
  sensitive domains. Over 100 applications running nonce CSP together with
  Trusted Types reported **zero XSS in 2021**.
- In a controlled build experiment across nine models, the unconstrained arm
  executed XSS payloads in **9 of 53 builds**; the arm with compile-time
  checks, strict CSP and Trusted Types enforced executed them in **0 of 54** —
  every model, no exceptions. The harness cost 1.2-2x more model turns and
  produced a lower cost per working build.
- An attack agent run against Google's own infrastructure found hundreds of
  XSS, and **none of them originated in safe-by-design framework code**.

The line worth keeping is theirs: *better models raise the average, only the
environment sets the floor.*

The second source is this repository's own record, and it is the reason this
spec exists rather than a link to the talks. Over 2026-09-22 and 2026-09-23 a
single defect class appeared six times in two days:

1. Ordering tests that asserted on source text via `assertIn`. Moving the
   filter to a position where it was a permanent no-op left all three green
   while children reached the persisted order.
2. A test reading a function by fixed character window, `SOURCE[start:start +
   900]`. A five-line comment pushed the assertion target to 1023 characters
   and the test failed against correct code. The opposite error — a window too
   large — would have read into the next function and passed on a call
   belonging to something else.
3. `bin/run-suite-chunked.sh` piped through `tail`, twice, in two different
   sessions. The pipe replaced the runner's exit 75 with the pipe's 0, so a run
   that executed no tests reported success. Both were caught only by opening
   the log.
4. A dedupe migration with four passing checks — row counts, redundancy to
   zero, idempotency, and no two real chat ids collapsed — that destroyed
   **1,393 attributions**, because none of its four checks asked whether the
   surviving row kept what the deleted row knew.
5. A test failing on state a previous run left behind: a zero-byte
   `data/webconsole.db`, created by an earlier chunk, made
   `test_an_impossible_floor_still_starts_the_cli` fail while the wrapper was
   behaving correctly.
6. A preflight stage reporting "zero innerHTML assignments with interpolation"
   while `web/assets/remote-stats.js:51` interpolates seven values into
   `innerHTML`. Every one passes through `esc()`, so there is no vulnerability
   — but the stage stated a property it never checked.

Every one is the same shape: **a check that reports on something other than
what it claims to measure.** Four reported success falsely, two reported
failure falsely, and the direction is not the interesting part. What matters is
that in each case a human or an agent read the report instead of the thing.

This is precisely the gap the talks address. Their answer is not more review.
It is to make the invariant machine-checkable, enforce it where it cannot be
argued with, and put it outside the reach of whoever is writing the code.

## 2. What this changes

Nothing about what we build. Everything about what the environment refuses to
let us build wrongly.

Three principles, in priority order.

**Deterministic invariants beat review.** Where a rule can be expressed as
something a machine decides — a header, a compile-time ban, a test that fails
on mutation — it is expressed that way. Prose in `rules.md` is a last resort,
not a first one.

**A check must fail when the thing it guards breaks.** A check that cannot be
shown to fail has not been shown to check anything. This is a requirement on
new checks, not an aspiration.

**Guardrails live outside the agent's reach.** Response headers, build
configuration and the enforcement pipeline are not things a session edits to
make its own work pass. An exemption is a reviewed change to the guardrail, not
a local edit.

## 3. The Content Security Policy

`middleware.py:221` currently emits, unconditionally:

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; connect-src 'self'; frame-ancestors 'self';
base-uri 'self'; form-action 'self'
```

This is already far better than the gated-on-a-nonce-nobody-set policy it
replaced, which shipped no CSP at all. It is not yet strict, and the gaps are
specific.

**3.1 `object-src 'none'`.** Absent. `default-src 'self'` covers `object-src`
by fallback, but `'self'` is not `'none'` — a same-origin upload or reflected
path can still be embedded as a plugin document. `object-src 'none'` is
unconditional in every strict-CSP recommendation and costs nothing here,
because this application embeds no plugin content.

**3.2 Trusted Types.** Absent. Add `require-trusted-types-for 'script'` and a
`trusted-types` directive naming the policies we actually create. This is the
half of the Google result that pairs with CSP: the 100-plus applications
reporting zero XSS ran both, not one.

**3.3 `style-src 'unsafe-inline'`.** Present, and not free. It does not enable
script execution, but it does permit CSS-based exfiltration of DOM content
through attribute selectors and it weakens any future nonce story. Removal is
gated on an inventory of inline `style=` attributes, which is work, so this is
staged last rather than first.

**3.4 Reporting.** No `report-to` or `report-uri`. Without a report sink, a CSP
violation in production is invisible, and every tightening below has to be
deployed blind. This lands first, in report-only mode, because it is what makes
the rest measurable rather than hopeful.

**3.5 Keep `script-src 'self'` and do not add a nonce yet.** The talks
recommend nonce plus `'strict-dynamic'` where script delivery is not fully
controlled. Here it is: both templates load only same-origin external scripts
and carry no inline handlers, which the existing comment records. A nonce would
add plumbing and no assurance. Revisit if a third-party script is ever
introduced — and treat that introduction as the trigger, in the guardrail
itself, not as something to remember.

## 4. Trusted Types, and the sinks that actually exist

Enforcement without an inventory produces a broken application and a rolled-
back header. The inventory, measured rather than assumed:

- 19 files under `web/assets/` mention `innerHTML`. Most are comments recording
  that `textContent` was chosen deliberately — `conversation.js:745`,
  `transcript.js:13`, `chat-list.js:32`, the last documenting a stored XSS that
  already happened here.
- `remote-stats.js:37` and `:40` assign constant strings. Safe, and no Trusted
  Types policy is needed for a literal.
- `remote-stats.js:51` interpolates seven values, each through `esc()`.
- `specs.js:204` assigns the output of `window.DOMPurify.sanitize(html)`, on
  markdown already sanitised server-side by `nh3`.

So there is exactly one templating sink and one sanitiser sink. Both are
expressible as named Trusted Types policies, which is a small enough surface to
enforce rather than report on.

The order is: report-only, confirm zero violations in production for a week,
then enforce. Not the reverse.

## 5. Isolation headers

`X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` and HSTS are present and correct.

Missing, and worth adding in this order:

**5.1 Fetch Metadata.** Reject cross-site state-changing requests by inspecting
`Sec-Fetch-Site`. The talks describe this as a few lines at the server edge,
and here it would sit in the same middleware that already sets the headers
above. It defends the same class the `wc_csrf` token defends, from a different
direction and without per-form plumbing — defence in depth, not a replacement.

**5.2 `Cross-Origin-Opener-Policy: same-origin`.** Isolates this window from
cross-origin openers. The console opens no cross-origin popups.

**5.3 `Cross-Origin-Resource-Policy: same-origin`.** Refuses cross-origin
embedding of our own responses.

All three are one-line additions to the same response hook, and all three are
verified by a test asserting on the response headers of a real request — not by
grepping `middleware.py` for the string.

## 6. Checks that can fail

This is the section that addresses our own six incidents, and it is the one
with the most value per line of work.

**6.1 Every new check ships with its mutation.** A check is accepted when its
author can state what they broke to make it fail, and the test suite
demonstrates it. This is already how the best work in this repository is done —
the corrected usage keep-rule was verified by reverting it and observing
exactly one test fail, and the attribution repair was validated against 9
values stored before the incident, not against its own arithmetic. The rule
makes that the default rather than the exception.

**6.2 Source-text assertions are a last resort, and are scoped when used.**
Where a behaviour can be executed, execute it — `node` is present, and
`chat-list.js` deliberately carries no top-level imports so its pure functions
can be run directly. Where source inspection is genuinely the only option,
extract by structure, never by character count. `_function_body` in
`tests/test_qa_chat_active_float.py` is the pattern: count braces from the
signature, and the number disappears from the test.

**6.3 A check's summary states what it measured.** The §4 preflight line is the
example: "zero innerHTML assignments with interpolation" would have been true
as "zero unescaped interpolations into innerHTML", which is what it actually
establishes. A reader who verifies one over-claim and finds it false learns to
skim the whole stage, which is how §11's four false positives on `_*timer`
names already train people to ignore it.

**6.4 Never read a runner's status through a pipe.** Invoke it on its own line
and read `$?` directly. This cost two sessions a false green in one day, in two
different wrappers. A lint rule over `bin/` and the rules-file command blocks
catches it mechanically.

**6.5 Aggregates report what did not run.** A chunked run that skips a phase
must say so and exit non-zero, rather than presenting survivors as a total.
This is already implemented for browser-phase admission; the requirement is
that it stays true of any future runner.

**6.6 Failures are matched by assertion text, never by chunk index.** Chunk
numbers shift whenever a file is added anywhere `pytest` collects. Between two
runs a day apart, the same two failures moved from `plain-16`/`plain-46` to
`plain-16`/`plain-47`; matching on index would have reported one new failure
and one fix, both false.

**6.7 Tests own their state.** The zero-byte `data/webconsole.db` case:
`config.DB_PATH` resolves relative to the working directory, so each worktree
has its own, and one suite run leaves a file that makes the next run's test
fail for an unrelated reason. A test that needs a database creates and removes
one it controls.

## 7. Guardrails outside reach

Three things stop being ordinary code:

- the security header block in `middleware.py`
- the enforcement configuration for Trusted Types policies
- the admission and reporting logic in the suite runners

Changing them is permitted. Changing them *in the same commit as the work they
would have blocked* is what this prevents. Mechanically: a dedicated
`CODEOWNERS` entry, and a preflight stage that fails when a diff touches both a
guardrail file and application code, requiring the guardrail change to land
separately and first.

This is the "immutable guardrails" recommendation from the 2026 talk, reduced
to what a repository of this size can actually enforce.

## 8. What this is not

**Not a CSP nonce rollout.** Section 3.5 gives the reason: no inline scripts,
no third-party scripts, so a nonce is plumbing without assurance today.

**Not a rewrite of `rules.md`.** Two of its stages are demonstrably weaker than
they read (§5's `TEST_ONLY` set omits `pytest_asyncio` and tells the reader to
add a dependency that must not be added; §11's grep excludes `_*timer` and so
reports four correct handles every run). Those are bugs in specific stages,
fixed as such. The document's structure is sound.

**Not a policy of more review.** The evidence in section 1 is that review was
present throughout and did not catch any of the six. Four of them were caught
by execution — running the test against the broken version, running the
composer over real rows, opening the log. That is where the effort goes.

**Not applicable to legacy code retroactively.** Following the migration model
in the talks: enforce on new code, let existing code deprecate. A rule that
fails the whole tree on day one is a rule that gets disabled on day two.

## 9. Order of work

Each step is independently valuable and independently revertible.

1. **CSP reporting endpoint, report-only.** Makes everything after it
   measurable. No behaviour change.
2. **`object-src 'none'`.** One directive, no inventory needed, immediate.
3. **Fetch Metadata, COOP, CORP.** Three lines in the existing response hook,
   plus header assertions against real responses.
4. **Trusted Types, report-only**, with the two policies from section 4. One
   week of production reports.
5. **Trusted Types, enforced**, if and only if step 4 reported zero violations.
6. **The check rules from section 6**, as preflight stages with their own
   mutations, plus fixes to §5's `TEST_ONLY` set and §11's grep.
7. **Guardrail separation** — `CODEOWNERS` and the mixed-diff stage.
8. **`style-src` inventory**, then removal of `'unsafe-inline'` if the
   inventory allows.

Steps 1-3 are a single afternoon. Step 8 may never be worth it, and saying so
now is better than leaving it as an open intention.

## 10. How we will know it worked

Not by the absence of findings, which is what every false green in section 1
also looked like.

- Step 1 succeeds when a deliberately-injected violation appears in the report
  sink. Verified by injecting one.
- Steps 2, 3 and 5 succeed when a request that should be refused is refused,
  asserted against a live response rather than against the source of the
  header.
- Step 6 succeeds when each new stage has a recorded mutation that makes it
  fail.
- The whole thing succeeds if, six months from now, the incident register has
  fewer entries of the form "the check passed and the thing was broken". That
  is the only outcome measure that matters, and it is a count we already keep.
