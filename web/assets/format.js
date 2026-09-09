/** Shared display formatting for figures that appear on more than one page.
 *
 * This file exists because the orchestrator page needed to print a token
 * count, and the rule for how to print one already existed as a private
 * `_abbrev` inside usage.js. Copying it would have given the two pages their
 * own definitions of "12.3 K", and they would agree only until either was
 * touched -- the same argument `classification.classify_chat` and
 * `shared.backend_kind` are single functions rather than a rule reimplemented
 * per surface.
 *
 * The orchestrator page is a separate document with its own module tree
 * (web/assets/orchestrator/), so this is deliberately dependency-free: no
 * DOM, no state, no imports. Anything that needs an element or a store does
 * not belong here.
 */

/** A token count, shortened for a line where the exact number does not fit.
 *
 * Callers that show this should keep the exact figure available -- usage.js
 * puts it in a `title` -- because "1.2 M" is a reading aid, not the number.
 */
export function abbrevTokens(n) {
  const value = Number(n) || 0;
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)} B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)} M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)} K`;
  return String(value);
}

/** A dollar figure, or null when there is nothing meaningful to show.
 *
 * Four decimal places rather than usage.js's two: this is used for a single
 * orchestrator run, where a whole run can cost less than a cent and "$0.00"
 * would read as free. usage.js aggregates over days and keeps two, which is
 * right for that figure -- so the precision is the caller's choice and this
 * takes it as an argument rather than picking one for everybody.
 */
export function formatUsd(value, digits = 4) {
  if (value === null || value === undefined) return null;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return null;
  return `$${amount.toFixed(digits)}`;
}
