# Architecture

```
        event / cron
             │
             ▼
      ┌─────────────┐   subscriptions only; no run_now(prompt) back door
      │   Router    │
      └──────┬──────┘
             ▼
      ┌─────────────┐   durable: every step commits before the next begins
      │    Queue    │◄──────────────── resume() on start
      │  (SQLite)   │
      └──────┬──────┘
             ▼
      ┌─────────────────────────────────────────────┐
      │                Executor                     │
      │  1. registry.get(agent)  — or it cannot run │
      │  2. capabilities_for()   — grants only      │
      │  3. dispatch by autonomy — table has a hole │
      │  4. loop: provider ⇄ tool                   │
      └──────┬──────────────────────────────┬───────┘
             │                              │
       staged effects                  audit chain
             │                        (hash-linked)
             ▼
      ┌─────────────┐   world untouched until a named human approves
      │ Review gate │
      └──────┬──────┘
        ┌────┴────┐
     approve    reject
        │          │
     apply +    discard
     record     staged
     inverse       │
        │          ▼
        │      rollback() reverses anything already applied,
        ▼      newest first, one transaction
     committed
```

## Component notes

**Router** (`runtime/queue.py`). Agents subscribe to a cron expression or an
event name. One event fans out to separate durable runs, so one agent failing
does not affect another's handling. This is the only entry point on purpose: a
`run_now(prompt)` convenience is exactly where the registry, the tier and the
gate get skipped.

**Store** (`runtime/state.py`). SQLite in WAL mode standing in for Postgres.
Five tables — `runs`, `steps`, `journal`, `contact_ledger`, `value_ledger` —
all deliberately boring so the schema ports without a rewrite. `claim_next`
moves a queued run to running inside a transaction, so two workers cannot
claim the same run.

**Executor** (`runtime/executor.py`). Binds a capability set from grants,
then loops provider ⇄ tool. Records every step before the next one begins,
which is what makes resume possible. Dispatch is a table keyed by autonomy
class, with `AUTONOMOUS_COMMITMENT` absent.

**Providers** (`runtime/providers.py`). The runtime never imports a vendor
SDK; it speaks to a `Provider` returning text or a tool call. Vendor retry,
token accounting and tool-call parsing live in the adapter, so none of it
leaks into the executor. CI runs `ScriptedProvider`, which is the only way a
golden suite means anything — a noisy suite makes a regression and a sampling
artefact look identical.

**Capabilities** (`capabilities/`). Tool handlers are pure: they describe an
`Effect` and return. Application is the executor's job for an ungated agent
and the gate's job for a gated one. That separation is what lets "held for
review" mean the write has not happened.

**Audit** (`audit/log.py`). Append-only JSONL, each entry carrying the
previous entry's SHA-256. `verify()` walks the chain. Tamper-evident rather
than tamper-proof, which is the right bar for an internal system.

## Production shape

Not implemented here; this is what the same design looks like deployed.

- **Store** → Cloud SQL Postgres for run state, BigQuery for the run log and
  the ledgers. The step and journal schemas move over unchanged.
- **Queue** → Pub/Sub for events, Cloud Scheduler for cron, Cloud Run jobs as
  workers. `claim_next` becomes `SELECT ... FOR UPDATE SKIP LOCKED`.
- **Credentials** → one service account per agent, scoped by its grants, via
  Terraform. This is wall 1 one layer down: the account backing
  `work_item.append_note` holds a token that cannot call the field-update
  endpoint at all. An audit of any agent's credentials then shows it cannot
  perform the writes it is not allowed to perform, which is a stronger
  statement than any test in this repo makes.
- **Secrets** → Secret Manager, mounted per job, never in the registry.
- **Observability** → OpenTelemetry spans per run, per turn and per tool call,
  with token cost attributed per run so `keel value` can be reported against
  spend rather than in isolation.

## Where the platform watches itself

The meta layer is registered like anything else — same tiers, same gate, same
suites. Three agents, all T0:

- **registry drift**: agents with no run in 30 days, grants never exercised,
  suites that have not changed while the agent has
- **eval erosion**: suites whose pass rate is drifting, safety cases that have
  never fired on real output, judge scores diverging from the calibration set
- **contact pressure**: people approaching their weekly cap, agents whose
  messages are consistently ignored

They read and file. They do not fix, because fixing the platform is a
commitment and the platform does not author commitments about itself either.
