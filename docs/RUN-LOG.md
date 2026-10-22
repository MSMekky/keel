# Run log

**Status: not yet run.** This file is a template. Every line below marked
`PASTE` is waiting for real terminal output from a machine with network access
and real credentials. Until then, `RUNNING.md` still carries its **UNVERIFIED**
markers and they are accurate.

Do not fill this in from memory or by editing what you wish had happened. The
value of this document is that it is a transcript, including of the things that
went wrong. A log recording a correction is worth more than one reporting that
everything worked first time — the second reads as fiction to anyone who has
integrated an API.

---

## 0. Offline gate

Date: `PASTE`
Machine / Python version: `PASTE`

```
$ make gate
PASTE
```

Expected: 96 tests, registry valid, scripted evals pass.

---

## 1. `repo-weekly-digest` — read-only, first real model call

Repo used: `PASTE` (throwaway)
Model: `PASTE`

```
$ export ANTHROPIC_API_KEY=... GITHUB_TOKEN=... KEEL_PROVIDER=anthropic
$ python -m keel.cli run repo-weekly-digest \
    --input '{"repo":"...","since":"..."}'
PASTE
```

The digest it actually produced:

```
PASTE
```

Then the audit trail, which is the thing worth showing:

```
$ python -m keel.cli audit <run_id>
PASTE
$ python -m keel.cli audit --verify
PASTE
```

**Anything that broke, and the fix:**

`PASTE — or "nothing", if nothing did.`

---

## 2. `pr-note-carrier` — the full gated path

The sequence that matters: staged → human approves → applied → verified in the
repo → rolled back → verified gone.

```
$ python -m keel.cli run pr-note-carrier --input '{...}'
PASTE

$ python -m keel.cli review show <run_id>
PASTE
```

At this point, check the pull request in a browser. **There should be no
comment.** Screenshot or paste the comment count.

`PASTE`

```
$ python -m keel.cli review approve <run_id> --reviewer "@you"
PASTE
```

Now the comment exists. Paste its URL: `PASTE`

Then roll it back and confirm GitHub returns 204 and the comment is gone:

```
PASTE
```

---

## 3. The eval gate blocking a real regression

Break something on purpose — loosen the agent's purpose so it editorialises,
or point a case at an output you know is bad — and show the gate refusing.

```
$ python -m keel.cli eval --live --gate ; echo "exit=$?"
PASTE
```

Expected: non-zero exit, a named case, a `SAFETY` line.

---

## 4. Cassette recorded

```
$ KEEL_RECORD=1 python -m keel.cli eval --live-only --suite repo-weekly-digest
PASTE
```

Then confirm it replays with no credentials at all:

```
$ unset ANTHROPIC_API_KEY GITHUB_TOKEN
$ python -m keel.cli eval --live
PASTE
```

**Read the cassette before trusting it.** It contains the model's real output;
if that output is bad, the suite will faithfully assert that the agent
reliably produces something bad. A cassette is a measurement, not a
certificate.

---

## 5. Judge dataset — the first production examples

The judge dataset ships with seed examples only, and
`keel.evals.judge.dataset` refuses to build a trainable split from them. That
refusal is deliberate: a judge trained and tested on generated data measures
the generator.

Grade the real outputs from the runs above and append them:

```
$ python -m keel.evals.judge.dataset
PASTE
```

Target before training anything: **at least 60–100 production examples**, from
runs across more than one repository, graded by hand. Then:

```
$ python -m keel.evals.judge.dataset freeze
PASTE
```

---

## What this log changes once it exists

- Every **UNVERIFIED** marker in `RUNNING.md` that is now false gets deleted.
- The README's scope section stops saying the live APIs are unverified.
- The CV line becomes: *verified end-to-end against live model and GitHub APIs*
  — a claim this file is the evidence for.

Until then, none of those changes are true, and making them anyway is the one
thing that would make this repository worse than saying nothing.
