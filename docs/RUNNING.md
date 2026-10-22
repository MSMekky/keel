# Running this for real

Everything below has been written and unit-tested against fake transports. The
steps marked **UNVERIFIED** have never opened a socket — no network was
available where this was built. They are the steps where you find out whether
I got the API shapes right. Expect one or two things to be wrong; the tests
tell you where to look when they are.

## 0. Requirements

Python 3.11+. PyYAML. That is the whole dependency list.

```bash
git clone <repo> && cd keel
pip install pyyaml
make gate        # 59 tests + registry + scripted evals, all offline
```

If `make gate` is green, the platform's logic is sound. It says nothing about
whether Anthropic and GitHub accept our requests. That is step 3.

## 1. Credentials

### Anthropic

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export KEEL_PROVIDER=anthropic
export KEEL_MODEL=claude-sonnet-5-5
```

### GitHub — and read this bit properly, it is the point

Create a **fine-grained** personal access token (Settings → Developer settings
→ Personal access tokens → Fine-grained), scoped to the one repository you are
testing against, with exactly:

| Permission | Level | Why |
|---|---|---|
| Contents | Read | reading merged PRs |
| Issues | **Read and write** | posting and deleting our own comments |
| Pull requests | Read | reading PR metadata |

Do **not** grant anything else. In particular, note what this token still
allows that we do not want: `Issues: write` on GitHub is a single permission
that covers both commenting *and* `PATCH /issues/{n}`, which sets assignees,
state and milestone.

**So the credential alone does not enforce the prohibition here — the code
does.** That is an honest gap, and the mitigation is:

- No function in `keel/capabilities/github.py` issues that PATCH.
- `register()` refuses any tool declaring `assignees`, `state`, `milestone` or
  any other commitment field, at import, in CI.
- `tests/test_real_integration.py::TestSchemaCannotLeakACommitmentField`
  asserts this over the entire catalog on every run.

If you want the credential-level wall as well — and in a real deployment you
should — the shape is a GitHub App with a permission set that grants
`issues:write` through a proxy you control, and the proxy rejects PATCH. That
is a real piece of work and it is not in this repo. Do not claim it is.

```bash
export GITHUB_TOKEN=github_pat_...
export KEEL_REPO=your-org/your-repo
```

## 2. First real run — **UNVERIFIED**

Start with the read-only agent. Nothing it does can be wrong in a way that
matters.

```bash
python -m keel.cli run repo-weekly-digest \
    --input "{\"repo\": \"$KEEL_REPO\", \"since\": \"2026-07-01\"}"
```

Expected: `awaiting_review`, one staged `draft.save` effect. Then:

```bash
python -m keel.cli review list
python -m keel.cli review show <run_id>          # read what it actually wrote
python -m keel.cli review approve <run_id> --reviewer "@you"
python -m keel.cli audit <run_id>                # every prompt and tool call
python -m keel.cli audit --verify                # chain intact
```

**When it breaks, look here first:**

- `HTTP 400 ... tools.0.name` → tool name mangling. `keel/runtime/providers.py`,
  `_mangle`. Names must match `^[a-zA-Z0-9_-]{1,64}$`.
- `HTTP 400 ... messages` → the tool_use / tool_result pairing in
  `_to_anthropic`. Every `tool_result` needs the matching `tool_use_id` from
  the assistant turn immediately before it.
- Empty PR list → the search API needs the repo to be visible to the token.

## 3. Record the cassette — **UNVERIFIED**

This is the step that turns the evaluation layer from theatre into
measurement. Until now the suites graded strings I wrote by hand. After this
they grade what the model actually produced.

```bash
KEEL_RECORD=1 python -m keel.cli eval --live-only --suite repo-weekly-digest
```

That runs the real agent against the real APIs and writes every exchange to
`evals/cassettes/repo-weekly-digest.json`. From then on:

```bash
python -m keel.cli eval --live        # replays; no key, no network, free
```

**Read the cassette before you trust it.** It contains the model's real output.
If the digest is bad, the cassette records a bad digest and the suite will
happily assert that the agent reliably produces it. A cassette is a
measurement, not a certificate.

Re-record when: the agent's purpose or grants change (the runner detects this
via `prompt_fingerprint` and fails the case), you change models, or a month
has passed. Model drift should be caught deliberately, on a schedule, not
discovered in production.

## 4. The gated write — **UNVERIFIED**

Only after step 2 works. Use a throwaway repo the first time.

```bash
python -m keel.cli run pr-note-carrier \
    --input "{\"repo\": \"$KEEL_REPO\", \"number\": 1,
              \"note\": \"Rebased on main.\", \"authored_by\": \"@you\"}"

python -m keel.cli review show <run_id>     # the exact comment text, unposted
python -m keel.cli review approve <run_id> --reviewer "@you"
```

Check the repo. The comment should exist, attributed to you, posted by keel.
Then verify rollback actually deletes it:

```python
from keel.gate.review import rollback   # see tests for the full call
```

## 5. Scheduling

Simplest thing that works, and genuinely fine for months:

```cron
0 8 * * 1  cd /srv/keel && /usr/bin/python3 -m keel.cli run repo-weekly-digest \
             --input '{"repo":"your-org/your-repo","since":"'$(date -d '7 days ago' +\%Y-\%m-\%d)'"}' \
             >> /var/log/keel.log 2>&1
```

Do not reach for Cloud Run, Pub/Sub and Terraform until cron has annoyed you
for a specific reason. The infrastructure in `docs/ARCHITECTURE.md` is the
shape it takes when it needs to; building it first is how you get a demo with
better plumbing.

## 6. Postgres — not done

`Store` is SQLite. The schema is plain SQL with no SQLite-specific types, so
the port is mechanical: swap `sqlite3` for `psycopg`, change `?` to `%s`, and
replace `claim_next`'s transaction with `SELECT ... FOR UPDATE SKIP LOCKED`.
Roughly an afternoon. Not worth doing until more than one worker exists.

## What is real, and what is not

**Real:** the runtime, the registry, the capability walls, the staged-effect
gate, rollback, the audit chain, the HTTP layer, the Anthropic and GitHub
request construction, 59 offline tests.

**Not verified:** that the live APIs accept these requests. One session with
real credentials settles it.

**Not built:** Postgres, a deployed scheduler, per-agent service accounts,
tracing, the meta-layer agents.

**Not real until you do it:** operational history. The thing that makes this a
system rather than a repository is running it every Monday for two months,
having it embarrass you once, and fixing that. Nobody can hand you that part.
