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

## 2. Threat model

Google's numbers come from a hostile-internet context. This console is a
single-operator tool behind Tailscale with an API token. Importing their
conclusions without stating our own threat model would be cargo-culting, so:

**Not the threat.** Anonymous internet attackers, hostile registered users,
multi-tenant isolation. There is one operator and no public exposure.

**The actual threat, and it is real.** This console's entire job is to render
text that nobody trusted. Model output, transcript contents, session names,
peer-session messages, remote QA-node payloads, commit text, file contents read
by an agent — every one of these reaches the DOM, and every one is attacker-
influenceable if any repository, web page, ticket or document an agent reads is
attacker-influenceable. `chat-list.js:32` records a stored XSS that already
happened here, from exactly this direction.

So the adversary is not someone attacking the console. It is **content the
console renders on the operator's behalf**, which the operator has no reason to
distrust and every reason to read.

That distinction changes priorities rather than removing them. It is why
section 3 argues against a nonce rollout and for Trusted Types; the injection
risk here is DOM-side, from data, not from an attacker who can place inline
script in our templates.

## 3. The console is itself an agent surface

The 2026 talk names a "lethal trifecta": sensitive data access, untrusted
context, and the capability to act. Applied honestly, this console has all
three.

- **Sensitive data.** The production database, transcripts, API tokens in the
  environment, the whole repository.
- **Untrusted context.** Everything in section 2, plus cross-session messages
  from peer agents, which arrive as text and are acted on.
- **Capability to act.** It spawns the `claude` CLI, deploys releases,
  restarts services and writes to the production database.

We already hold two of the talk's four defences, without having framed them
that way. **Policy enforcement with escalate-to-human** exists as the
permission classifier and the confirmation rules. **Observability** exists as
`requests-log.md`, the SDD ledgers and the transcripts.

Two are weak:

**3.1 Markdown and markup sanitisation on the exfiltration path.** The talk's
worked example is an agent rendering markdown that contains an image URL
carrying stolen data in its query string. `img-src 'self' data:` in the current
CSP already refuses a remote image load, which is a real defence and worth
recording as one — it was not put there for this reason. `connect-src 'self'`
covers the fetch path.

**3.2 Bounded capability.** A session's own reach is bounded by the permission
classifier, but a *peer* session asking for an action denied to it is bounded
only by the receiving agent's judgement. This was exercised on 2026-09-23:
a peer reported that deploy and restart were denied to it and asked another
session to run them. The request was refused and escalated to the operator.
That is the right outcome, and it came from judgement rather than from a
control. A control would be better.

Section 9 does not attempt to fix 3.2, because the design is not obvious and
guessing at one is worse than naming the gap. It is recorded here so it is not
rediscovered as a surprise.

## 4. The Content Security Policy

`middleware.py:221` currently emits, unconditionally:

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; connect-src 'self'; frame-ancestors 'self';
base-uri 'self'; form-action 'self'
```

This is already far better than the gated-on-a-nonce-nobody-set policy it
replaced, which shipped no CSP at all. It is not yet strict, and the gaps are
specific.

**4.1 `object-src 'none'`.** Absent. `default-src 'self'` covers `object-src`
by fallback, but `'self'` is not `'none'` — a same-origin upload or reflected
path can still be embedded as a plugin document. `object-src 'none'` is
unconditional in every strict-CSP recommendation and costs nothing here,
because this application embeds no plugin content.

**4.2 Trusted Types.** Absent. Add `require-trusted-types-for 'script'` and a
`trusted-types` directive naming the policies we actually create. This is the
half of the Google result that pairs with CSP: the 100-plus applications
reporting zero XSS ran both, not one. Given the threat model in section 2 —
injection through rendered data, not through our templates — this is the single
highest-value item in the document.

**4.3 `style-src 'unsafe-inline'`.** Present, and not free. It does not enable
script execution, but it does permit CSS-based exfiltration of DOM content
through attribute selectors, which is the same exfiltration path section 3.1
cares about. Removal is gated on an inventory of inline `style=` attributes,
which is work, so this is staged last rather than dropped.

**4.4 Reporting.** No `report-to` or `report-uri`. Without a report sink, a CSP
violation in production is invisible, and every tightening below has to be
deployed blind. This lands first, in report-only mode, because it is what makes
the rest measurable rather than hopeful.

**4.5 Keep `script-src 'self'`; do not add a nonce.** The talks recommend nonce
plus `'strict-dynamic'` where script delivery is not fully controlled. Here it
is: `web/index.html` loads two scripts, both same-origin, and neither template
carries an inline handler. A nonce would add plumbing and no assurance against
the threat in section 2. The trigger for revisiting is the introduction of a
third-party or inline script — and that trigger belongs in the guardrail check,
not in anyone's memory.

**4.6 `script-src 'self'` does not cover the scripts we already ship.** Two
vendored minified libraries sit under `web/assets/` — `d3.min.js` (279 KB) and
`purify.min.js` (29 KB). Both are same-origin, so CSP permits them
unconditionally, and neither `<script>` tag carries an `integrity` attribute. A
modification to either file — by a compromised dependency update, or by an
agent with write access to this tree — executes with full privileges and no
control notices. `purify.min.js` is the sanitiser guarding the `specs.js` sink,
so an attacker who can edit it owns the sanitiser too.

This is a supply-chain gap, not an XSS gap, and it is the one item here that
strict CSP genuinely cannot help with. Fix: Subresource Integrity hashes on
both tags, plus a preflight stage that fails when a vendored asset's hash
changes without a corresponding hash update. That stage is the point — an SRI
attribute nobody regenerates is another check that stops checking.

## 5. Trusted Types, and the sinks that actually exist

Enforcement without an inventory produces a broken application and a rolled-
back header. The inventory, measured across first-party code (excluding the two
vendored bundles):

**Script-execution sinks: none.** No `eval(`, no `new Function`, no
`document.write`, no `srcdoc`, no `javascript:` URL construction anywhere in
`web/assets/` or the templates. This is worth stating positively, because it
means Trusted Types enforcement has no script-URL surface to break.

**HTML sinks: four, in two files.** No `outerHTML`, no `insertAdjacentHTML`.

- `remote-stats.js:37` and `:40` — constant strings. No policy needed for a
  literal.
- `remote-stats.js:51` — interpolates seven values, each through `esc()`.
- `specs.js:204` — assigns `window.DOMPurify.sanitize(html)`, over markdown
  already sanitised server-side by `nh3`.

Of the 19 files that mention `innerHTML`, the rest are comments recording that
`textContent` was chosen deliberately: `conversation.js:745`,
`transcript.js:13`, and `chat-list.js:32`, the last documenting the stored XSS
that already happened here.

So: one templating sink and one sanitiser sink, needing two named policies. A
surface this small should be enforced rather than reported on indefinitely.

Order: report-only, confirm zero violations in production for a week, then
enforce. Not the reverse.

## 6. Isolation headers

`X-Frame-Options: SAMEORIGIN`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` and HSTS are present and correct.

Missing, in this order:

**6.1 Fetch Metadata.** Reject cross-site state-changing requests by inspecting
`Sec-Fetch-Site`. A few lines in the middleware that already sets the headers
above. It defends the class `wc_csrf` defends, from a different direction and
without per-form plumbing.

Named risk, because this one can break things rather than merely tighten them:
the remote QA node, the proxy and any `curl`-based tooling send no
`Sec-Fetch-*` headers at all. The rule must treat *absent* as allowed and only
refuse an explicit `cross-site`, or the first deploy takes out the remote
execution path. That is a real trade — it means a non-browser client can always
opt out — and it is the correct one here, where browser-originated CSRF is the
threat and the token guards the rest.

**6.2 `Cross-Origin-Opener-Policy: same-origin`.** Isolates this window from
cross-origin openers. The console opens no cross-origin popups.

**6.3 `Cross-Origin-Resource-Policy: same-origin`.** Refuses cross-origin
embedding of our own responses.

All three are verified by asserting on the headers of a real response, never by
grepping `middleware.py` for the string.

## 7. Checks that can fail

This section addresses our own six incidents, and carries the most value per
line of work.

**7.1 Every new check ships with its mutation.** A check is accepted when its
author can state what they broke to make it fail, and the suite demonstrates
it. The best work here already does this — the corrected usage keep-rule was
verified by reverting it and observing exactly one test fail; the attribution
repair was validated against nine values stored before the incident rather than
against its own arithmetic. The rule makes that the default.

**7.2 Source-text assertions are a last resort, and are scoped when used.**
Where a behaviour can be executed, execute it — `node` is present, and
`chat-list.js` deliberately carries no top-level imports so its pure functions
run directly. Where source inspection is the only option, extract by structure,
never by character count. `_function_body` in `tests/test_qa_chat_active_float.py`
is the pattern: count braces from the signature, and the magic number
disappears.

**7.3 A check's summary states what it measured.** The §4 preflight line is the
example: "zero unescaped interpolations into innerHTML" is true and is what it
established; "zero innerHTML assignments with interpolation" is false. A reader
who tests one over-claim and finds it false learns to skim the whole stage —
which is how §11's four false positives on `_*timer` names already train people
to ignore it.

**7.4 Never read a runner's status through a pipe.** Invoke on its own line,
read `$?` directly. Two sessions, two wrappers, one day, two false greens. A
lint over `bin/` and the rules-file command blocks catches it mechanically.

**7.5 Aggregates report what did not run.** A run that skips a phase says so
and exits non-zero rather than presenting survivors as a total. Already true of
browser-phase admission; the requirement is that it stays true of any future
runner.

**7.6 Failures are matched by assertion text, never by chunk index.** Chunk
numbers shift whenever a file is added anywhere `pytest` collects. Across two
runs a day apart the same two failures moved from `plain-16`/`plain-46` to
`plain-16`/`plain-47`; matching on index reports one new failure and one fix,
both false.

**7.7 Tests own their state.** `config.DB_PATH` resolves relative to the
working directory, so every worktree has its own and one suite run leaves a
file that makes the next run fail for an unrelated reason. A test needing a
database creates and removes one it controls.

## 8. Guardrails, and an honest note on enforcement

Three things stop being ordinary code: the security-header block in
`middleware.py`, the Trusted Types policy configuration, and the admission and
reporting logic in the suite runners.

Changing them is permitted. Changing them *in the same commit as the work they
would have blocked* is what this prevents.

**The enforcement mechanism has to match how this repository actually works.**
There is no `CODEOWNERS` file, no pull-request flow, and no branch protection:
commits reach `main` by direct push, including from agent sessions. Proposing
`CODEOWNERS` here would be proposing a control that never fires — the same
defect this document is about, committed in the document itself.

What is enforceable today is a preflight stage that fails when one diff touches
both a guardrail file and application code, requiring the guardrail change to
land separately and first. That is weaker than review — it orders changes, it
does not gate them — and it is worth having precisely because it runs.

Recording the gap rather than papering over it: organisational policy requires
human review before merge, enforced by branch protection and `CODEOWNERS`.
Neither exists in this repository. Whether to close that gap is an operator
decision, not one this spec should make silently, and it is larger than the
subject of this document.

## 9. What this is not

**Not a CSP nonce rollout.** Section 4.5 gives the reason.

**Not a rewrite of `rules.md`.** Two stages are demonstrably weaker than they
read — §5's `TEST_ONLY` set omits `pytest_asyncio` and so tells the reader to
add a dependency that must not be added, and §11's grep excludes `_*timer` and
reports four correct handles every run. Those are bugs in stages, fixed as
such. The structure is sound.

**Not a policy of more review.** Review was present for all six incidents in
section 1 and caught none. Four were caught by execution — running the test
against the broken version, running the composer over real rows, opening the
log. That is where effort goes.

**Not retroactive.** Following the migration model in the talks: enforce on new
code, let existing code deprecate. A rule that fails the whole tree on day one
is a rule that gets disabled on day two.

**Not a fix for section 3.2.** Cross-session capability laundering is named,
not solved.

## 10. Order of work

Each step is independently valuable and independently revertible.

1. **CSP reporting endpoint, report-only.** Makes everything after it
   measurable. No behaviour change.
2. **`object-src 'none'`.** One directive, no inventory, immediate.
3. **SRI on the two vendored bundles, plus the hash-drift stage.** The only
   item strict CSP cannot cover, and the sanitiser is one of the two files.
4. **Fetch Metadata, COOP, CORP**, with the absent-header rule from 6.1, and
   header assertions against real responses.
5. **Trusted Types, report-only**, with the two policies from section 5. One
   week of production reports.
6. **Trusted Types, enforced**, if and only if step 5 reported zero violations.
7. **The check rules from section 7**, as preflight stages with their own
   mutations, plus fixes to §5's `TEST_ONLY` set and §11's grep.
8. **Guardrail ordering stage** from section 8.
9. **`style-src` inventory**, then removal of `'unsafe-inline'` if it allows.

Steps 1-4 are a single afternoon. Step 9 may never be worth it, and saying so
now is better than leaving it as an open intention.

## 11. How we will know it worked

Not by the absence of findings, which is what every false green in section 1
also looked like.

- Step 1 succeeds when a deliberately-injected violation appears in the report
  sink. Verified by injecting one.
- Steps 2, 4 and 6 succeed when a request or an assignment that should be
  refused is refused, asserted against live behaviour rather than against the
  source of the header.
- Step 3 succeeds when editing a byte of `purify.min.js` fails the build.
  Verified by editing a byte.
- Step 7 succeeds when each new stage has a recorded mutation that makes it
  fail.
- The whole thing succeeds if, six months from now, the incident register has
  fewer entries of the form "the check passed and the thing was broken". That
  is the only outcome measure that matters, and it is a count we already keep.
