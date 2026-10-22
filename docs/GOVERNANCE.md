# Governance

Every rule below exists because of a specific failure. The rule is written
next to the failure it prevents, because a rule whose reason has been
forgotten is a rule that gets removed by the next person who finds it
inconvenient.

## The line

An agent may:

- **read** internal data and report on it
- **draft** content a human will edit before it lands anywhere
- **carry** a human-authored update into a system of record, after that human
  has consented to the specific text
- **initiate contact** with a person, within a metered budget

An agent may never:

- **author a commitment** — set an owner, a due date, a status or a priority
  on anything in a shared work system

### Why that line and not another

The first four are all reversible or advisory. A wrong report is corrected. A
bad draft is deleted. A note in the wrong place is removed and the person
who approved it knows they approved it.

A commitment is different in kind. The moment an agent can set an owner and a
due date, it is allocating human time, and it does so with the institutional
weight of the system it wrote into. Someone opens their tracker on Monday and
finds work assigned to them by nothing. The damage is not the row in the
database; it is that the org's model of who owes what is now partly authored
by a process nobody can interrogate. That is very hard to walk back and very
easy to stop.

So the line is drawn at authorship of obligation, not at "writes" in general.
An agent that transports a human's words into a tracker is a courier. An
agent that decides who owns something is doing management, and it should not
be doing management.

## How the line is held

Three walls, in descending order of how much I trust them.

### Wall 1 — no tool can express it

`capabilities.base.register` inspects every tool's declared parameters at
import and refuses any that names a commitment field. The check runs when the
module defining the tool is imported, which means a pull request adding one
fails at collection time in CI.

This is the wall that matters. It is not a runtime check that a clever input
might route around; it is a constraint on what can exist in the process at
all. There is no set of arguments, no prompt, no injected instruction and no
model error that produces the forbidden write, because there is no function
whose signature accepts it.

The production form of this wall is the same idea one layer down: the service
account in the agent's tool grant lacks the API scope, so even a compromised
process holding the credential cannot make the call. The catalog check is the
version that fails in CI; the IAM policy is the version that fails at the
edge. Both, in a real deployment.

### Wall 2 — no handler for the autonomy class

`AutonomyClass.AUTONOMOUS_COMMITMENT` exists in the enum so the validator can
name and reject it. `Executor._handlers` has no entry for it. A test asserts
the absence and fails if a handler ever appears.

Naming the prohibited thing is deliberate. A prohibition that is not written
down anywhere in the code is one that gets re-derived from scratch, badly, by
whoever next asks "why can't it just update the ticket?".

### Wall 3 — ungranted tools are not bound

An agent's capability set is built from its registry grants. A tool it was not
granted is absent from the dispatch table, not refused by it. There is no
ambient namespace and no fallback handler.

The distinction matters. "Refused" is a decision made at runtime by code that
could have a bug. "Absent" is not a decision.

## Risk tiers

Requirements are monotonic — anything required at a tier is required above it.

| Tier | Reach | Eval suite | Human gate |
|---|---|---|---|
| `T0_READ_INTERNAL` | reads internal data, internal output | optional | optional |
| `T1_DRAFT_INTERNAL` | drafts a human edits before it lands | required | optional |
| `T2_SYSTEM_OF_RECORD` | carries human-authored text into a real system | required | required |
| `T3_EXTERNAL_FACING` | output leaves the company | required | required |

A spec violating any of this raises and the process stops. There is no
load-with-warnings mode. A warning is something a tired person merges on a
Friday.

## Registration

Nothing runs without:

- a **registered owner** who is an individual, not a team alias. An incident
  needs someone to page, and "the platform team" is not a person.
- a **tier**, which determines everything else
- an explicit **tool grant** list, with per-run call budgets
- a **human gate** decision, forced to `true` at T2 and above
- an **eval suite** at T1 and above

## The human gate is a real hold

Gated effects are staged in the journal and the world is untouched. Approval
applies them inside one transaction, by a named reviewer, recorded in the
audit chain. Rejection discards them — nothing to undo.

This is not the same as applying and reverting. "We can always roll it back"
is true right up until the notification has already been read.

## Contact is metered

A per-person weekly cap, a bundling window, quiet hours, and a requirement to
deliver something useful before asking for anything.

This is a governance rule and not a UX preference. An agent programme dies
from being annoying long before it dies from being wrong: people mute the
channel, then the reports stop being read, then the platform is a cost centre
producing artefacts nobody opens. Every notification spends a limited amount
of trust, and the ledger is there so the spending is visible.

## Audit

Every prompt, tool call, tool result, gate decision and rollback is appended
to a hash-chained log before the next thing happens. Each entry carries the
previous entry's hash, so a deleted or edited line is detectable by
`keel audit --verify` rather than by someone noticing the story does not add
up.

The chain is the answer to "why did the agent do that" — not a summary of the
run, the run itself.

## Dispute resolution

When someone challenges an agent's output, the answer is a test case, not an
argument. If the output was wrong and no case covers it, the fix is a new case
in the suite first and a change to the agent second. The suite is the memory
of every way this agent has been wrong, and it should only ever grow.
