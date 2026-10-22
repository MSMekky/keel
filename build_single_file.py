#!/usr/bin/env python3
"""Amalgamate the package into a single runnable file.

    python build_single_file.py            # -> dist/keel.py
    python build_single_file.py --check    # fail if dist/keel.py is stale

Why a generator rather than a hand-maintained copy: a second copy of five
thousand lines diverges from the first within a week, and then there are two
artifacts and no way to know which is true. This script makes the single file
a *build output*. It is regenerated, never edited, and `--check` in CI fails
the build if someone edits the package without regenerating.

What it does:

- concatenates the modules in dependency order
- strips intra-package imports and `if __name__` blocks, hoists stdlib imports
- renames the handful of module-level names that collide once flattened
- embeds the YAML/JSON data files as strings and materialises them into a
  temp directory at startup, so path-anchored code keeps working unchanged
- appends the tests, so `python keel.py test` runs all of them

What it cannot do: make the single file a good place to work. Edit the
package.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "dist" / "keel.py"

#: Dependency order. Flattening removes the import graph, so this list *is*
#: the import graph now — a module may only use names defined above it.
MODULES = [
    "keel/errors.py",
    "keel/registry/models.py",
    "keel/capabilities/base.py",
    "keel/runtime/http.py",
    "keel/capabilities/catalog.py",
    "keel/capabilities/github.py",
    "keel/registry/loader.py",
    "keel/runtime/state.py",
    "keel/runtime/providers.py",
    "keel/audit/log.py",
    "keel/runtime/value.py",
    "keel/gate/review.py",
    "keel/runtime/executor.py",
    "keel/runtime/queue.py",
    "keel/evals/harness.py",
    "keel/evals/judge/dataset.py",
    "keel/evals/judge/train.py",
    "keel/evals/judge/benchmark.py",
    "keel/evals/live.py",
    "keel/mcp.py",
    "keel/cli.py",
]

TEST_MODULES = [
    "tests/support.py",
    "tests/test_prohibited_capability.py",
    "tests/test_registry_validation.py",
    "tests/test_durability_and_rollback.py",
    "tests/test_interruption_budget.py",
    "tests/test_evaluation_gate.py",
    "tests/test_real_integration.py",
    "tests/test_mcp.py",
    "tests/test_judge.py",
    # tests/test_verify.py is excluded on purpose: it loads keel_verify.py from
    # disk, which the single file does not carry. The companion script is a
    # repo-only artifact — it writes docs/RUN-LOG.md and appends to the judge
    # dataset, neither of which makes sense against a temp directory.
]

#: Data files carried inside the single file and written to a temp directory
#: on startup. Keeps every `ROOT / "registry" / "agents"` path working.
DATA_GLOBS = [
    "registry/agents/*.yaml",
    "registry/rejected/*.yaml",
    "evals/suites/*.yaml",
    "evals/live/*.json",
    "evals/cassettes/*.json",
    "keel/evals/judge/data/*.jsonl",
    "keel/evals/judge/report.json",
    "keel/evals/judge/split.json",
    "README.md",
]

#: Names that collide once the namespaces are flattened.
RENAMES = {
    "keel/evals/judge/train.py": [(r"\bdef main\(", "def train_main("),
                                  (r"\bmain\(argv\)", "train_main(argv)")],
    "keel/evals/judge/benchmark.py": [(r"\bdef main\(", "def benchmark_main("),
                                      (r"\bmain\(argv\)", "benchmark_main(argv)")],
}

#: Path anchors rewritten to point at the materialised data directory.
PATH_FIXES = [
    (r"ROOT = pathlib\.Path\(__file__\)\.resolve\(\)\.parent\.parent",
     "ROOT = _KEEL_ROOT"),
    (r"HERE = pathlib\.Path\(__file__\)\.resolve\(\)\.parent\n",
     "HERE = _KEEL_ROOT / \"keel\" / \"evals\" / \"judge\"\n"),
    (r"CASSETTES = pathlib\.Path\(__file__\)\.resolve\(\)\.parent"
     r"\.parent\.parent / \"evals\" / \"cassettes\"",
     "CASSETTES = _KEEL_ROOT / \"evals\" / \"cassettes\""),
    (r"report_path = \(pathlib\.Path\(__file__\)\.resolve\(\)\.parent\n"
     r"                   / \"judge\" / \"report\.json\"\)",
     "report_path = _KEEL_ROOT / \"keel\" / \"evals\" / \"judge\" / \"report.json\""),
    (r"HERE = pathlib\.Path\(__file__\)\.resolve\(\)\.parent\.parent",
     "HERE = _KEEL_ROOT"),
]

IMPORT_RE = re.compile(r"^\s*(from\s+\.+[\w.]*\s+import|from\s+keel[\w.]*\s+import|"
                       r"import\s+keel[\w.]*)")
FUTURE_RE = re.compile(r"^from __future__ import")
MAIN_RE = re.compile(r"^if __name__ == [\"']__main__[\"']:")


def strip_module(path: pathlib.Path, source: str) -> tuple[str, set[str]]:
    """Return (body, stdlib_import_lines) with package imports removed."""
    imports: set[str] = set()
    out: list[str] = []
    lines = source.splitlines()
    i = 0
    in_docstring = False

    while i < len(lines):
        line = lines[i]

        if MAIN_RE.match(line):
            break  # drop the module's own CLI block

        if FUTURE_RE.match(line):
            i += 1
            continue

        if IMPORT_RE.match(line):
            # Multi-line parenthesised imports.
            if "(" in line and ")" not in line:
                while i < len(lines) and ")" not in lines[i]:
                    i += 1
            i += 1
            continue

        stripped = line.strip()
        if (stripped.startswith("import ") or stripped.startswith("from ")) \
                and not line.startswith(" ") and not in_docstring:
            imports.add(stripped)
            i += 1
            continue

        # Track triple-quoted blocks crudely so import-looking lines inside
        # docstrings are not hoisted.
        in_docstring ^= (stripped.count('"""') % 2 == 1)
        out.append(line)
        i += 1

    body = "\n".join(out)
    for pattern, repl in PATH_FIXES:
        body = re.sub(pattern, repl, body)
    for pattern, repl in RENAMES.get(str(path.relative_to(ROOT)).replace("\\", "/"), []):
        body = re.sub(pattern, repl, body)
    return body.strip("\n"), imports


def collect_data() -> dict[str, str]:
    data: dict[str, str] = {}
    for pattern in DATA_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            data[str(path.relative_to(ROOT)).replace("\\", "/")] = path.read_text()
    return data


PREAMBLE = '''#!/usr/bin/env python3
"""
Keel — a governed execution layer for internal LLM agents, in one file.

    python keel.py demo      # the whole argument, end to end
    python keel.py test      # the full suite
    python keel.py gate      # what CI runs; non-zero blocks the merge
    python keel.py mcp       # serve the platform over MCP on stdio

GENERATED FILE — do not edit. Produced by build_single_file.py from the
package, which is where the work happens. `python build_single_file.py --check`
fails if this file is stale, so the two cannot drift apart.

────────────────────────────────────────────────────────────────────────────
THE CLAIM

An agent may read and report, may carry a human-authored update into a system
of record once a human consents, and may initiate contact within a budget.

It may never write a commitment — an owner, a due date, a status. Those are
the things that, once an agent can set them, quietly make it your manager.

Most systems implement that as a line in a prompt. This one implements it as
the absence of a code path. Four surfaces, one rule:

  1. No tool can express it. `register()` refuses, at import, any tool
     declaring a commitment field. A PR adding one fails at collection in CI.
  2. No handler exists for the autonomy class. The executor's dispatch table
     has no entry for AUTONOMOUS_COMMITMENT, and a test fails if one appears.
  3. Ungranted tools are not bound. A capability set is built from grants and
     contains nothing else, and the JSON schema sent to the model is generated
     from it — a model is never shown a field it cannot use.
  4. The MCP surface is checked by the same function, not a copy of the rule.

GitHub's API does expose the forbidden write: PATCH /issues/{n} sets
assignees, state and milestone in one call. This platform simply never wraps
it. See the github section below.

WHAT IS REAL AND WHAT IS NOT

Real: the runtime, registry, capability walls, staged-effect gate, rollback,
hash-chained audit, HTTP layer, Anthropic and GitHub request construction, the
MCP server, and the tests below.

Not verified: that the live APIs accept these requests. No socket has been
opened. docs/RUN-LOG.md in the package is the template waiting for that.

Not trained: the judge. The dataset tooling deliberately refuses to build a
trainable split from seed data, because a judge trained and tested on
generated data measures the generator.
────────────────────────────────────────────────────────────────────────────
"""
'''

BOOTSTRAP = '''

# ── embedded data ──────────────────────────────────────────────────────────
#
# The package reads its registry, eval suites and judge data from disk. Rather
# than rewrite every path, the single file carries those files and writes them
# to a temp directory at import, so path-anchored code works unchanged.

_EMBEDDED_FILES: dict[str, str] = _EMBEDDED_FILES_PLACEHOLDER


def _materialise() -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp(prefix="keel-"))
    for rel, content in _EMBEDDED_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for rel in ("evals/cassettes", "keel/evals/judge/data"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


_KEEL_ROOT = _materialise()


# ── namespace shims ────────────────────────────────────────────────────────
#
# Flattening removes the module qualifiers, so code written as `st.Store` or
# `mcp.CTX` would break. Rather than rewrite hundreds of call sites — and
# thereby make the single file textually different from the package it is
# built from — each qualifier is aliased to this module. Attribute lookup then
# resolves to the flattened top-level name, and `mcp.CTX = x` still rebinds
# the global the way the package expects.

_self = sys.modules[__name__]
base = catalog = github = st = mcp = bm = _self
'''

FOOTER = '''

# ── entry point ────────────────────────────────────────────────────────────


def _cmd_test(args) -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def _cmd_gate(args) -> int:
    """What CI runs. Each step blocks."""
    steps = [
        ("registry", cmd_validate, argparse.Namespace(include_rejected=False)),
        ("tests", _cmd_test, argparse.Namespace()),
        ("evals", cmd_eval, argparse.Namespace(gate=True, live=False,
                                               live_only=False, suite=None)),
    ]
    for name, fn, ns in steps:
        if fn(ns) != 0:
            print(f"{RED}gate failed at: {name}{OFF}")
            return 1
    print(f"\\n{GREEN}gate passed{OFF}")
    return 0


def _cmd_judge(args) -> int:
    examples = load()
    if args.action == "freeze":
        split = build_split(examples,
                            allow_no_production=args.allow_no_production)
        split.save(_KEEL_ROOT / "keel" / "evals" / "judge" / "split.json")
        print(json.dumps(split.counts, indent=2))
        return 0
    print(f"{len(examples)} examples")
    for p in PROVENANCE:
        print(f"  {p:<12} {sum(1 for e in examples if e.provenance == p)}")
    for problem in check_split(examples):
        print(f"  - {problem}")
    return 0


if __name__ == "__main__":
    # Extra subcommands that only exist in the single-file build.
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.exit(_cmd_test(None))
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        sys.exit(_cmd_gate(None))
    if len(sys.argv) > 1 and sys.argv[1] == "judge":
        ns = argparse.Namespace(
            action="freeze" if "freeze" in sys.argv else "stats",
            allow_no_production="--allow-no-production" in sys.argv)
        sys.exit(_cmd_judge(ns))
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        serve()
        sys.exit(0)
    sys.exit(main())
'''


def build() -> str:
    all_imports: set[str] = set()
    sections: list[tuple[str, str]] = []

    for rel in MODULES + TEST_MODULES:
        path = ROOT / rel
        body, imports = strip_module(path, path.read_text())
        all_imports |= imports
        if body.strip():
            sections.append((rel, body))

    # unittest and tempfile are needed by the bootstrap and the test runner
    all_imports |= {"import unittest", "import tempfile", "import sys",
                    "import pathlib", "import argparse", "import json"}

    plain = sorted(i for i in all_imports if i.startswith("import "))
    froms = sorted(i for i in all_imports if i.startswith("from "))

    parts = [PREAMBLE, "\nfrom __future__ import annotations\n",
             "\n".join(plain), "", "\n".join(froms)]

    data = collect_data()
    parts.append(BOOTSTRAP.replace(
        "_EMBEDDED_FILES_PLACEHOLDER", repr(data)))

    for rel, body in sections:
        header = f"\n\n# {'═' * 72}\n# {rel}\n# {'═' * 72}\n\n"
        parts.append(header + body)

    parts.append(FOOTER)
    return "\n".join(parts) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the built file is stale")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)

    built = build()
    out = pathlib.Path(args.out)

    if args.check:
        if not out.exists():
            print(f"{out} does not exist; run build_single_file.py")
            return 1
        if out.read_text() != built:
            print(f"{out} is stale. The package changed and the single-file "
                  f"build was not regenerated. Run: python build_single_file.py")
            return 1
        print(f"{out} is up to date")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(built)
    print(f"wrote {out} ({len(built.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
