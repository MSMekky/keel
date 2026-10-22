# Keel

A governed execution layer for internal LLM agents. Event-triggered queue,
model-agnostic runtime, durable state, human review gate, atomic rollback,
hash-chained audit log, and an evaluation gate that blocks a merge.

The organising claim is one sentence: **an agent may read and report, may
carry a human-authored update into a system of record once a human consents,
and may initiate contact within a budget — but it may never write a
commitment.** An owner, a due date, a status. Those are the things that,
once an agent can set them, quietly make it your manager.

Most systems implement that as a line in a prompt. This one implements it as
the absence of a code path.

```
git clone <this repo> && cd keel
python -m keel.cli demo
```

No dependencies beyond PyYAML, no network, no API key. The demo runs the
whole argument in about a second.

---

## The claim, and how it is enforced

Three walls, in descending order of how much I trust them.

**1. No tool can express the write.** `capabilities.base.register` refuses, at
import time, any tool declaring `owner`, `due_date`, `status`, `priority` or
their aliases as a parameter. A pull request adding such a tool fails at
collection in CI. It never reaches a review queue where someone reasons about
whether it is fine this once.

```python
register(Tool(
    name="work_item.set_fields",
    parameters=frozenset({"item_id", "status", "owner"}),
    ...
))
# ProhibitedCapability: tool 'work_item.set_fields' declares commitment
# field(s) ['owner', 'status']. No tool in this platform may write an owner,
# a due date or a status into a shared work system.
```

What exists instead is `work_item.append_note(item_id, note, authored_by)`.
It appends human-authored text and can do nothing else, because the signature
has nothing else in it.

**2. No handler exists for the autonomy class.** `AutonomyClass` names
`AUTONOMOUS_COMMITMENT` for exactly one reason: so the validator can reject
it. The executor's dispatch table has no entry for it, and
[`tests/test_prohibited_capability.py`](tests/test_prohibited_capability.py)
fails if one ever appears. The prohibition is defended by CI rather than by
anyone remembering it.

**3. Ungranted tools are not bound.** An agent's capability set is built from
its grants and contains nothing else. There is no ambient namespace and no
"unknown tool" fallback that tries to be helpful.

Wall 1 is the real control. Walls 2 and 3 exist so a mistake in wall 1 is
still contained, and so the failure is loud and logged rather than silent.

### What that looks like against a compromised model

`keel demo` step 5 runs a model that has been fully talked into doing the
forbidden thing — the exact tool call a successful prompt injection produces:

```
work_item.append_note(item_id="PLAT-19", note="urgent reassignment",
                      authored_by="@lin",
                      owner="@karim", status="blocked", due_date="2026-08-07")

blocked: undeclared argument(s) ['due_date', 'owner', 'status'];
         declared parameters are ['authored_by', 'item_id', 'note']
PLAT-19 owner is still @nadia, status still in_progress
```

The arguments never reach a backend, because no tool in the catalog has
anywhere to put them.

---

## What is here

| Area | Module | What it does |
|---|---|---|
| Registry | `keel/registry/` | Typed specs. Owner, tier, autonomy, grants, gate. Nothing runs unregistered. |
| Capabilities | `keel/capabilities/` | Tool catalog, import-time prohibition, per-run grant binding and budgets. |
| Runtime | `keel/runtime/` | Event router, durable SQLite state, resume-after-crash, model-agnostic providers. |
| Gate | `keel/gate/` | Human review of staged effects, atomic rollback, per-person interruption budget. |
| Audit | `keel/audit/` | Hash-chained append-only log of every prompt, tool call and decision. |
| Evals | `keel/evals/` | Golden suites, behavioral assertions, safety cases, rubric judge, CI gate. |

### Risk tiers

Requirements are monotonic — anything required at a tier is required above it.

| Tier | Reach | Eval suite | Human gate |
|---|---|---|---|
| `T0_READ_INTERNAL` | reads internal data | optional | optional |
| `T1_DRAFT_INTERNAL` | drafts a human will edit | **required** | optional |
| `T2_SYSTEM_OF_RECORD` | carries human-authored text into a real system | **required** | **required** |
| `T3_EXTERNAL_FACING` | output leaves the company | **required** | **required** |

A spec that violates any of this does not load. There is no warn-and-continue
mode, because a warning is something a tired person merges on a Friday.

### "Held for review" means the write has not happened

Tool handlers are pure: they compute an `Effect` and return. Application is
separate. A gated agent's effects sit in the journal as `staged` and the
world is untouched until a named human approves, at which point they are
applied inside one transaction and their inverses recorded. Rejection
discards them — there is nothing to undo, which is the point of staging
rather than applying-and-reverting.

Ungated agents apply immediately, and `rollback()` reverses applied effects
newest-first in a single transaction. Either the run's whole footprint
disappears or nothing is marked reversed. A half-undone run is not a state
the system can be left in.

### Durability

Every step commits to the store before the next one starts, so a crash leaves
a run whose completed steps are exactly its durable steps. `resume()` replays
those steps as context and continues from the next one. It does not
re-execute them, because re-executing a write is how you get two of
something.

### Evaluation

Suites are written by **inverting the agent**: enumerate how it can be wrong
first, then write the case that catches each way.

For the weekly digest, the enumerated failures are: inventing progress that
did not happen, quoting a private channel into a shared digest, being
accurate and reading as an accusation, crediting the wrong person, and
asserting a commitment in prose that the tool layer stops it making for real.
Each is a case in
[`evals/suites/exec-weekly-digest.yaml`](evals/suites/exec-weekly-digest.yaml).

Three kinds of check, again in descending order of trust:

- **assertions** — deterministic, behavioral, cheap. `no_invented_names`,
  `no_private_sources`, `no_commitment_language`.
- **safety** — the non-negotiable subset. A safety failure blocks the build
  regardless of aggregate score. There is no threshold at which leaking a
  private channel becomes acceptable.
- **judge** — a model grading a written rubric, for what you cannot assert on:
  whether an accurate update reads as an accusation. Weakest of the three and
  treated that way. It can lower a score; a passing judge never rescues a
  failing assertion.

**The suite is graded too.** A detector that never fires looks exactly like an
agent that never misbehaves. Every safety assertion has an `expect: caught`
case containing a known-bad output that it must flag, and a test that fails
if the detector goes blind.

All of it runs against `ScriptedProvider` — deterministic, offline, free. If
the suite is noisy, a regression and a sampling artefact look identical.

---

## Commands

```
keel validate --include-rejected   # the registry, and what it refuses
keel eval --gate                   # suites; exit 1 blocks the merge
keel run <agent> --input '{}'      # submit and drain one run
keel review list | show | approve | reject <run_id> --reviewer @you
keel audit <run_id> | --verify     # replay a run, or check the chain
keel value                         # minutes of work absorbed, per agent
keel demo                          # all of the above, end to end
```

`registry/rejected/` holds a real request that came in — an agent to triage
the backlog and set owner, priority and due date. It is kept in the repo, out
of the loaded registry, so the refusal is demonstrable. When someone asks why
the platform will not do this, the answer is a command they can run rather
than a policy document.

---

## Instrumenting its own return

Every committed run writes to a ledger: minutes of human work displaced,
using a per-tier baseline registered up front rather than a number invented
at runtime. `keel value` computes the total. A team that disputes the figure
is disputing a registered constant attached to their agent, and the answer is
the query rather than an opinion about how useful the thing feels.

The baselines in `keel/runtime/value.py` are deliberately conservative. Better
to under-claim a real number than publish an impressive one nobody trusts.

---

## The judge

The judge is the weakest layer here and is treated that way: it can lower a
score, but a passing judge never rescues a failing assertion.

`keel/evals/judge/` is the machinery for replacing the rubric-prompted judge
with a small LoRA-tuned model — dataset schema with provenance tracking, a
frozen split, a pinned training config, and a three-arm benchmark against
human labels.

**No adapter has been trained.** The rubric is what is wired in, and
`default_judge()` reads `report.json` rather than trusting whoever last ran
something. `keel.evals.judge.dataset` currently **refuses** to build a
trainable split, because the only examples on disk are seeds:

> 12 examples, none from production. A judge trained and tested on generated
> data measures the generator.

That refusal is the design. Held-out examples are production-only, synthetic
variants stay in the same split as the real output they came from, and
`tests/test_judge.py` fails if a number quoted in this README stops matching
`report.json`.

Next step is `keel_verify.py`, below: run the agents, grade what they
actually produce, then train.

## Closing the last two claims

Two things in this repository cannot be made true by writing code: that it has
run against live APIs, and that the judge is trained on real labels. Both are
one command each, and `keel_verify.py` is what runs them.

```
python keel_verify.py status      # what is proven and what is not
python keel_verify.py preflight   # credentials, repo, rate limit
python keel_verify.py live        # everything against real APIs
python keel_verify.py grade       # label the real outputs by hand
python keel_verify.py freeze      # freeze the split
python keel_verify.py train       # LoRA the judge
python keel_verify.py bench       # three arms, write report.json
```

`live` runs the full sequence — read-only agent, gated write staged, approved,
posted, rolled back, the injected commitment field refused against the real
GitHub API, a cassette recorded and replayed with the credentials unset — and
generates `docs/RUN-LOG.md` from the responses that actually came back: status
codes, comment ids, token counts, verbatim model output.

It **refuses to write a verified verdict if any step failed**, and says so in
the log instead. `tests/test_verify.py` asserts that, because a script whose
report can be nicer than reality is worse than no script.

`grade` shows real agent output and records the label you type, tagged
`provenance=production` with the run id. It does not generate labels, and
`train` refuses below 60 production examples — a LoRA on a dozen reports a
number and measures nothing.

Current state: `status` says not verified, no benchmark, rubric wired in. That
is accurate, and it stays in the README until a run changes it.

## Over MCP

`python -m keel.mcp` serves the platform to any MCP client: list agents,
submit a run, read status and the full audit chain, see what is waiting on a
human.

There is no `keel_approve`. An MCP client is a model, and a model that can
approve its own run makes the human gate a formality it satisfies on its own
behalf. Submission is safe to expose because nothing downstream weakens; the
decision is not. See [`docs/MCP.md`](docs/MCP.md).

The prohibition is enforced here by the same function the tool catalog uses —
`assert_no_commitment_fields` — not a second copy of the rule that drifts.

## Scope and honesty about it

This is a working reference implementation, not a production deployment.

- SQLite stands in for Postgres and a warehouse. The schema is deliberately
  boring so it ports without a rewrite.
- The tool catalog wraps in-memory dicts rather than real APIs. The shape of
  each tool — parameters, tier, reversibility, gate — is the part that
  matters and is the part that would survive contact with a real backend.
- `ScriptedProvider` is what CI runs. `AnthropicProvider` is a thin adapter
  sketch; the point of the interface is that it is the only file that changes
  when the model changes.
- **The live APIs are unverified.** The Anthropic and GitHub request paths are
  written and covered by 22 tests against fake transports, but no socket has
  been opened. [`docs/RUN-LOG.md`](docs/RUN-LOG.md) is the empty template
  waiting for that session.
- Terraform, IAM, and per-agent service accounts are described in
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) but not implemented here.
  The credential-level version of wall 1 — an agent's service account
  physically lacking the API scope — is the production form of the same idea.

## Reading order

1. [`docs/GOVERNANCE.md`](docs/GOVERNANCE.md) — the rules and why each exists
2. [`keel/capabilities/base.py`](keel/capabilities/base.py) — the three walls
3. [`tests/test_prohibited_capability.py`](tests/test_prohibited_capability.py) — the prohibition, asserted
4. [`keel/evals/harness.py`](keel/evals/harness.py) — the gate everything depends on
5. [`docs/decisions/`](docs/decisions/) — the architecture calls, with what was given up

```
make test     # 96 tests, no network
make gate     # what CI runs, including the single-file staleness check
make demo
make single   # regenerate dist/keel.py and run its gate
```

## One file, if you want it

`dist/keel.py` is the whole platform in a single ~5,600-line file with no
dependencies beyond PyYAML. It runs `demo`, `test`, `gate`, `mcp` and every
CLI command, and carries its registry, eval suites and judge data inside
itself, materialising them into a temp directory at startup.

It is a **build output**, produced by `build_single_file.py`, not a second
copy maintained by hand. A hand-maintained copy diverges within a week and
then there are two artifacts and no way to know which is true, so
`build_single_file.py --check` runs in CI and fails if the package changed
without regenerating it.

Edit the package. Never edit `dist/keel.py`.
