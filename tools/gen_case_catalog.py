#!/usr/bin/env python3
"""Generate (or drift-check) tests/CASE_CATALOG.json — the machine-readable
catalog of QA-plan CASE ids the harness actually executes.

WHY (the agentic dev-loop):
  The QA persona must REFERENCE existing CASE ids (it cannot author harness
  code), and the engineering ready-gate validates every `integration_cases` id a
  QA plan names against THIS catalog — see prds/loop-revival-v2/SHARED-SPEC.md §6.
  The catalog is the single committed join between QA's plan and the executed
  suite, so a plan can never name a case that doesn't run.

It is ALSO a consistency check between two sources that MUST agree:
  1. the `@pytest.mark.qa_case("CASE-NNN")` markers across tests/*.py — parsed via
     `ast` so it is robust to `skipif` decorators / formatting; and
  2. tools/resolve_cross_repo.py's KNOWN map, which decides the gating (which
     cases the cross-repo lane derives for a participant union) + the two lane
     modes (composition vs routing).
A case the resolver references (always/composition, or the 401/402 derivation)
but with NO marker — or a cross-repo-namespace marker (CASE-3xx/4xx) the resolver
does NOT know about — is a hard error: that drift is exactly what would make a QA
plan disagree with the lane (a named case that never runs, or a test that the
lane never selects).

Lane modes (the `mode` field):
  fast        — the integration-runner fast-lane suite (CASE-0xx/1xx/2xx); runs on
                every integration PR, NOT selected per-feature by the resolver.
  always      — cross-repo case that runs in BOTH composition and routing mode
                whenever its owning from-source service builds (e.g. 301/311/321).
  composition — cross-repo case valid only on the MOCK backend (302, and 401 the
                ≥2-service composition signal). QA lists these as integration_cases.
  routing     — cross-repo case valid only on the REST->cloud backend (402), which
                the lane derives automatically when cc+llm build + an OpenAI key is
                present. QA NEVER lists a routing case (it would go not-implemented
                in composition mode).

Stdlib only (mirrors tools/resolve_cross_repo.py and tools/parse_junit.py).

Usage:
  python tools/gen_case_catalog.py --write    # (re)write tests/CASE_CATALOG.json
  python tools/gen_case_catalog.py --check     # exit 1 if the committed file is stale
  python tools/gen_case_catalog.py             # print the catalog to stdout (dry run)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# Import the resolver's KNOWN map (the gating source of truth) without assuming a
# cwd — the generator lives alongside resolve_cross_repo.py in tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from resolve_cross_repo import KNOWN  # noqa: E402  # type: ignore[import-not-found]

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
CATALOG_PATH = TESTS_DIR / "CASE_CATALOG.json"

# The two cross-repo cases the resolver DERIVES (not stored in KNOWN's per-repo
# lists): 401 = the ≥2-service composition signal, 402 = the cc+llm routing probe.
DERIVED = {
    "CASE-401": ("composition", None,
                 "cross-repo composition signal — runs when >=2 from-source services build together"),
    "CASE-402": ("routing", None,
                 "routing-mode only — cc+llm both build from source AND an OpenAI key is present"),
}

# A CASE id at or above this number lives in the cross-repo namespace (3xx
# from-source, 4xx cross-repo) and MUST be wired into the resolver to ever run.
CROSS_REPO_MIN = 300

# Every qa_case id must match this EXACTLY. A strict grammar so the >=300
# cross-repo-namespace test (and the orphan guard) can't be slipped by a
# malformed id like "CASE-403-routing" (which would otherwise parse as fast).
CASE_ID_RE = re.compile(r"^CASE-\d+$")

LANE_MODE_LEGEND = {
    "fast": "integration-runner fast-lane suite (CASE-0xx/1xx/2xx); runs every integration PR, not resolver-selected",
    "always": "cross-repo; runs in BOTH composition and routing mode when the owning from-source service builds",
    "composition": "cross-repo; MOCK backend only — QA may list these as integration_cases",
    "routing": "cross-repo; REST->cloud backend only (cc+llm + OpenAI key); derived automatically, QA never lists it",
}


class CatalogError(Exception):
    """A drift / consistency problem that must fail the generation (and CI)."""


def _case_number(case_id: str) -> int | None:
    """The integer part of 'CASE-301' -> 301, or None if it isn't that shape."""
    try:
        return int(case_id.split("-", 1)[1])
    except (IndexError, ValueError):
        return None


def _rel(path: Path) -> str:
    """Repo-relative display path, or the raw path when outside the repo (tmp/tests)."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _humanize(func_name: str) -> str:
    """Fallback intent from a test function name when it has no docstring."""
    name = func_name[len("test_"):] if func_name.startswith("test_") else func_name
    return name.replace("_", " ").strip()


def _is_qa_case_decorator(dec: ast.expr) -> bool:
    """True only for `<...>.mark.qa_case(...)` — the registered pytest marker.
    Excludes a decoy like `notpytest.qa_case(...)` whose base does not resolve
    through `.mark` (the runtime hook would never fire on it)."""
    if not isinstance(dec, ast.Call):
        return False
    f = dec.func
    return (
        isinstance(f, ast.Attribute) and f.attr == "qa_case"
        and isinstance(f.value, ast.Attribute) and f.value.attr == "mark"
    )


def _marker_id(dec: ast.Call, where: str) -> str:
    """The single CASE id of a confirmed qa_case decorator, or raise. Matches the
    runtime hook (tests/conftest.py reads marker.args[0]): EXACTLY one positional
    string-literal id, matching the strict grammar. Anything else is loud rather
    than a silent static/runtime desync."""
    if len(dec.args) != 1 or dec.keywords:
        raise CatalogError(
            f"{where}: @pytest.mark.qa_case takes EXACTLY one positional id "
            f"(the runtime hook reads args[0] and ignores the rest); got "
            f"{len(dec.args)} positional / {len(dec.keywords)} keyword arg(s)"
        )
    arg = dec.args[0]
    if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
        raise CatalogError(
            f"{where}: @pytest.mark.qa_case id must be a string LITERAL so the "
            f"catalog can see it statically (no variables/expressions)"
        )
    cid = arg.value
    if not CASE_ID_RE.match(cid):
        raise CatalogError(f"{where}: invalid CASE id {cid!r} — must match CASE-<digits>")
    return cid


def discover_markers(tests_dir: Path) -> dict[str, dict]:
    """Recursively walk tests/**/*.py via ast (matching pytest's recursive
    collection — a marker in a subdir like tests/behavior/ must NOT be invisible
    to the catalog) and return {case_id: {intent, test}} for every
    `@pytest.mark.qa_case(...)`-decorated MODULE-LEVEL test function.

    Raises CatalogError on anything that would desync the static catalog from
    what pytest actually runs: a duplicate id, a non-module-level (method/nested)
    marker, or a multi-/non-literal/malformed id."""
    found: dict[str, dict] = {}
    for path in sorted(tests_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            rel = path.name  # a tmp dir outside the repo (unit tests)
        top_level = {
            id(n) for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            qa_decs = [d for d in node.decorator_list if _is_qa_case_decorator(d)]
            if not qa_decs:
                continue
            where = f"{rel}::{node.name}"
            if id(node) not in top_level:
                raise CatalogError(
                    f"{where}: @pytest.mark.qa_case must decorate a MODULE-LEVEL test "
                    f"function, not a method/nested def (the catalog's nodeid would be wrong)"
                )
            if len(qa_decs) > 1:
                raise CatalogError(f"{where}: more than one @pytest.mark.qa_case on one function")
            cid = _marker_id(qa_decs[0], where)  # type: ignore[arg-type]
            doc = ast.get_docstring(node)
            intent = doc.strip().splitlines()[0].strip() if doc else _humanize(node.name)
            if len(intent) > 200:
                intent = intent[:197].rstrip() + "..."
            if cid in found:
                raise CatalogError(
                    f"duplicate qa_case id {cid}: {found[cid]['test']} and {where}"
                )
            found[cid] = {"intent": intent, "test": where}
    return found


def _gating_maps(known: dict[str, dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Reverse KNOWN into {case_id: repo} for always_cases and composition_cases."""
    always: dict[str, str] = {}
    composition: dict[str, str] = {}
    for repo, spec in known.items():
        for cid in spec.get("always_cases", []):
            always[cid] = repo
        for cid in spec.get("composition_cases", []):
            composition[cid] = repo
    return always, composition


def classify(case_id: str, always: dict[str, str], composition: dict[str, str]) -> dict:
    """Return {lane, mode, repo, gating} for a case id, derived from the resolver."""
    if case_id in DERIVED:
        mode, repo, gating = DERIVED[case_id]
        return {"lane": "cross-repo", "mode": mode, "repo": repo, "gating": gating}
    if case_id in always:
        repo = always[case_id]
        return {"lane": "cross-repo", "mode": "always", "repo": repo,
                "gating": f"always (both modes) when {repo} builds from source"}
    if case_id in composition:
        repo = composition[case_id]
        return {"lane": "cross-repo", "mode": "composition", "repo": repo,
                "gating": f"composition-mode only (MOCK backend) when {repo} builds from source"}
    return {"lane": "fast", "mode": "fast", "repo": None,
            "gating": "fast lane (integration-runner suite); not selected by the cross-repo resolver"}


def build_catalog(tests_dir: Path = TESTS_DIR, known: dict[str, dict] = KNOWN) -> dict:
    """Discover markers, cross-check against KNOWN, and assemble the catalog dict.
    Raises CatalogError on any drift between the markers and the resolver."""
    markers = discover_markers(tests_dir)
    always, composition = _gating_maps(known)

    referenced = set(always) | set(composition) | set(DERIVED)
    discovered = set(markers)

    # Cross-check A: every resolver-referenced case must have a real test.
    missing = sorted(referenced - discovered)
    if missing:
        raise CatalogError(
            "resolve_cross_repo.py references CASE id(s) with no @pytest.mark.qa_case "
            f"marker (the lane would mark them not-implemented): {', '.join(missing)}"
        )

    # Cross-check B: every cross-repo-namespace marker must be wired into the
    # resolver, else the lane never selects it and QA can't map it.
    orphan = sorted(
        cid for cid in discovered
        if (_case_number(cid) or 0) >= CROSS_REPO_MIN and cid not in referenced
    )
    if orphan:
        raise CatalogError(
            "cross-repo CASE marker(s) not wired into resolve_cross_repo.py "
            f"(the cross-repo lane will never run them): {', '.join(orphan)}. "
            "Add them to a KNOWN entry's always_cases/composition_cases or the "
            "401/402 derivation."
        )

    cases = {}
    for cid in sorted(discovered, key=lambda c: (_case_number(c) is None, _case_number(c), c)):
        entry = classify(cid, always, composition)
        entry = {"intent": markers[cid]["intent"], **entry, "test": markers[cid]["test"]}
        cases[cid] = entry

    return {
        "_meta": {
            "generated_by": "tools/gen_case_catalog.py",
            "do_not_edit": "Run `python tools/gen_case_catalog.py --write`; CI drift-checks with --check.",
            "source": "@pytest.mark.qa_case markers in tests/*.py + tools/resolve_cross_repo.py KNOWN",
            "spec": "prds/loop-revival-v2/SHARED-SPEC.md §6",
            "lane_modes": LANE_MODE_LEGEND,
            "case_count": len(cases),
        },
        "cases": cases,
    }


def serialize(catalog: dict) -> str:
    """Deterministic JSON (sorted keys, 2-space indent, trailing newline) so the
    --check diff is stable across machines."""
    return json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate/drift-check tests/CASE_CATALOG.json")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help="(re)write the catalog file")
    group.add_argument("--check", action="store_true", help="exit 1 if the committed catalog is stale")
    args = ap.parse_args()

    try:
        text = serialize(build_catalog())
    except CatalogError as exc:
        print(f"::error::CASE catalog: {exc}", file=sys.stderr)
        return 1

    if args.write:
        CATALOG_PATH.write_text(text, encoding="utf-8")
        n = json.loads(text)["_meta"]["case_count"]
        print(f"wrote {_rel(CATALOG_PATH)} ({n} cases)")
        return 0

    if args.check:
        if not CATALOG_PATH.exists():
            print(f"::error::{_rel(CATALOG_PATH)} is missing — run "
                  "`python tools/gen_case_catalog.py --write`", file=sys.stderr)
            return 1
        current = CATALOG_PATH.read_text(encoding="utf-8")
        if current != text:
            print(f"::error::{_rel(CATALOG_PATH)} is STALE — run "
                  "`python tools/gen_case_catalog.py --write` and commit the result.",
                  file=sys.stderr)
            return 1
        print(f"{_rel(CATALOG_PATH)} is up to date "
              f"({json.loads(text)['_meta']['case_count']} cases)")
        return 0

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
