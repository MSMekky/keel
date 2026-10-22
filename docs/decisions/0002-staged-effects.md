# ADR 0002 — Stage effects rather than apply-and-revert

**Status:** accepted
**Date:** 2026-08

## Decision

Tool handlers are pure. They compute an `Effect` and return without touching
the world. The executor applies effects immediately for an ungated agent; for
a gated one they sit in the journal as `staged` and the review gate applies
them after a named human approves.

## Context

The first implementation had handlers mutate directly and journal an inverse,
with rejection triggering a rollback. It worked and it was simpler.

It was also dishonest. `keel demo` printed "effects held, not applied" while
the draft was already sitting in the store, and the only thing standing
between a rejected run and a permanent change was rollback code executing
correctly. "We can always roll it back" is true right up until the
notification has been read, the digest has been seen, or the row has been
picked up by something downstream.

Staging makes the gate mean what it says. Before approval, nothing happened.

## What was given up

- **Handlers cannot return real results for writes.** `work_item.append_note`
  returns `{"staged_for": ...}`, not a note index, because the index does not
  exist yet. An agent that needs the result of its own write to continue
  cannot be gated. So far nothing needs that, and an agent that did would be
  a design smell.
- **Two code paths** — apply-now and apply-later — both of which must be
  correct. Covered by tests on each.
- Inverses are computed at apply time rather than describe time, since only
  then is the overwritten state known.

## Consequence

Rollback now only ever reverses effects in state `applied`. Staged effects
need no undoing, which is the whole point.
