# ADR 0001 — Build the runtime rather than adopt a framework

**Status:** accepted
**Date:** 2026-08

## Decision

Own the run loop. Depend on no agent framework. Keep providers behind a
one-method interface.

## Context

The open decision was whether to build on an existing orchestration framework
or write the loop. The loop itself is not the hard part — roughly two hundred
lines — and it is not where the value is.

The value is in what wraps the loop: capability binding from a registry, the
staged-effect gate, the journal and its inverses, the audit chain, the tier
enforcement. Every one of those needs to sit *inside* the loop, between the
model's tool call and the tool's execution.

Frameworks in this space own that seam. Adopting one means either fighting
its abstractions to insert the gate, or accepting its notion of a tool call —
which typically means a tool is a decorated Python function and its parameters
are whatever the signature says. That is precisely the property this platform
cannot delegate. Wall 1 is a constraint on what may be registered as a tool,
and it only holds if registration goes through code I control.

## What was given up

- Prebuilt integrations, retries and streaming. Rebuilt as needed; the
  provider adapter is the only place vendor behaviour lives.
- Community patterns and hiring familiarity. Mitigated by the runtime being
  small enough to read in one sitting.
- Multi-agent orchestration primitives. Not needed yet, and a graph of agents
  is a governance problem before it is an engineering one.

## Revisit if

A framework appears whose tool-registration path is pluggable enough to host
an import-time constraint, or the runtime grows past roughly a thousand lines
without a corresponding governance reason.
