"""Unit tests for tools/gen_case_catalog.py (the CASE catalog generator).

Runs in the standard pytest collect — stdlib only, no service stack, no Docker
(mirrors test_resolve_cross_repo.py). Tests the pure functions directly plus two
real-repo invariants: the catalog builds without drift, and the committed
tests/CASE_CATALOG.json is current.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "tools" / "gen_case_catalog.py"
sys.path.insert(0, str(ROOT / "tools"))
import gen_case_catalog as gcc  # noqa: E402  # type: ignore[import-not-found]


def _write_tests(dirpath: Path, specs: list[tuple[str, str, str | None]]) -> None:
    """specs: list of (case_id, func_name, docstring_or_None)."""
    out = ["import pytest\n\n"]
    for cid, fn, doc in specs:
        out.append(f'@pytest.mark.qa_case("{cid}")\n')
        out.append(f"def {fn}():\n")
        if doc is not None:
            out.append(f'    """{doc}"""\n')
        out.append("    pass\n\n")
    (dirpath / "test_synthetic.py").write_text("".join(out), encoding="utf-8")


# ---- real-repo invariants (the ones CI relies on) -------------------------------

def test_real_repo_builds_clean_with_all_cross_repo_cases():
    cat = gcc.build_catalog()  # raises CatalogError on any marker<->KNOWN drift
    cases = cat["cases"]
    for cid in ("CASE-301", "CASE-302", "CASE-311", "CASE-321", "CASE-401", "CASE-402"):
        assert cid in cases, f"{cid} missing from catalog"
        assert cases[cid]["lane"] == "cross-repo"
    assert cases["CASE-402"]["mode"] == "routing"      # QA must never list it
    assert cases["CASE-401"]["mode"] == "composition"
    assert cases["CASE-301"]["mode"] == "always"
    assert cases["CASE-001"]["lane"] == "fast"
    assert cat["_meta"]["case_count"] == len(cases)


def test_committed_catalog_is_current():
    expected = gcc.serialize(gcc.build_catalog())
    actual = gcc.CATALOG_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "tests/CASE_CATALOG.json is stale — run `python tools/gen_case_catalog.py --write` "
        "and commit the result."
    )


def test_serialize_is_deterministic_and_sorted():
    a = gcc.serialize(gcc.build_catalog())
    b = gcc.serialize(gcc.build_catalog())
    assert a == b
    assert a.endswith("\n")
    obj = json.loads(a)
    assert list(obj["cases"]) == sorted(obj["cases"])  # zero-padded ids => numeric order


# ---- classify() against the real resolver gating --------------------------------

def test_classify_uses_resolver_gating():
    always, comp = gcc._gating_maps(gcc.KNOWN)
    r301 = gcc.classify("CASE-301", always, comp)
    assert (r301["lane"], r301["mode"], r301["repo"]) == ("cross-repo", "always", "jarvis-llm-proxy-api")
    assert gcc.classify("CASE-302", always, comp)["mode"] == "composition"
    assert gcc.classify("CASE-302", always, comp)["repo"] == "jarvis-llm-proxy-api"
    r401 = gcc.classify("CASE-401", always, comp)
    assert (r401["mode"], r401["repo"]) == ("composition", None)
    r402 = gcc.classify("CASE-402", always, comp)
    assert (r402["mode"], r402["repo"]) == ("routing", None)
    assert gcc.classify("CASE-001", always, comp)["lane"] == "fast"
    assert gcc.classify("CASE-311", always, comp)["repo"] == "jarvis-tts"
    assert gcc.classify("CASE-321", always, comp)["repo"] == "jarvis-whisper-api"


# ---- discover_markers() intent extraction ---------------------------------------

def test_intent_prefers_docstring_then_falls_back_to_func_name(tmp_path):
    _write_tests(tmp_path, [
        ("CASE-001", "test_with_doc", "First line of intent.\nSecond line ignored."),
        ("CASE-002", "test_no_doc_here", None),
    ])
    m = gcc.discover_markers(tmp_path)
    assert m["CASE-001"]["intent"] == "First line of intent."
    assert m["CASE-002"]["intent"] == "no doc here"  # humanized func name
    assert m["CASE-001"]["test"].endswith("::test_with_doc")


def test_intent_is_truncated(tmp_path):
    long = "x" * 300
    _write_tests(tmp_path, [("CASE-001", "test_long", long)])
    intent = gcc.discover_markers(tmp_path)["CASE-001"]["intent"]
    assert len(intent) <= 200 and intent.endswith("...")


def test_markers_found_through_skipif_decorator(tmp_path):
    # ast must see the qa_case marker even when other decorators wrap it.
    (tmp_path / "test_synthetic.py").write_text(
        'import pytest\n\n'
        '@pytest.mark.skipif(True, reason="x")\n'
        '@pytest.mark.qa_case("CASE-001")\n'
        'def test_guarded():\n    """Guarded case."""\n    pass\n',
        encoding="utf-8",
    )
    m = gcc.discover_markers(tmp_path)
    assert m["CASE-001"]["intent"] == "Guarded case."


# ---- cross-check drift detection (the core value of the generator) --------------

def test_duplicate_marker_raises(tmp_path):
    _write_tests(tmp_path, [("CASE-001", "test_a", None), ("CASE-001", "test_b", None)])
    with pytest.raises(gcc.CatalogError, match="duplicate"):
        gcc.discover_markers(tmp_path)


def test_known_referenced_case_without_marker_raises(tmp_path):
    # KNOWN references CASE-301 but no test declares it -> hard error (cross-check A).
    _write_tests(tmp_path, [("CASE-401", "test_x", None), ("CASE-402", "test_y", None)])
    known = {"svc": {"always_cases": ["CASE-301"], "composition_cases": []}}
    with pytest.raises(gcc.CatalogError, match="CASE-301"):
        gcc.build_catalog(tmp_path, known)


def test_orphan_cross_repo_marker_raises(tmp_path):
    # A CASE-403 test exists but the resolver doesn't know it -> hard error (cross-check B).
    _write_tests(tmp_path, [
        ("CASE-401", "test_x", None), ("CASE-402", "test_y", None), ("CASE-403", "test_z", None),
    ])
    with pytest.raises(gcc.CatalogError, match="CASE-403"):
        gcc.build_catalog(tmp_path, known={})


def test_fast_lane_marker_below_300_is_allowed_without_resolver(tmp_path):
    # A new fast-lane case (e.g. CASE-216) needs no resolver wiring.
    _write_tests(tmp_path, [
        ("CASE-401", "test_x", None), ("CASE-402", "test_y", None), ("CASE-216", "test_new_smoke", "New smoke."),
    ])
    cat = gcc.build_catalog(tmp_path, known={})
    assert cat["cases"]["CASE-216"]["lane"] == "fast"


# ---- silent-drift holes the adversarial review surfaced (now guarded) ------------

def test_subdir_marker_is_discovered(tmp_path):
    # pytest collects subdirs; the generator must too (rglob) or a tests/behavior/
    # marker is run but invisible to the catalog.
    sub = tmp_path / "behavior"
    sub.mkdir()
    (sub / "test_sub.py").write_text(
        'import pytest\n\n@pytest.mark.qa_case("CASE-001")\n'
        'def test_in_subdir():\n    """Sub case."""\n    pass\n',
        encoding="utf-8",
    )
    assert "CASE-001" in gcc.discover_markers(tmp_path)


def test_multi_id_marker_raises(tmp_path):
    (tmp_path / "test_m.py").write_text(
        'import pytest\n@pytest.mark.qa_case("CASE-001", "CASE-002")\n'
        "def test_two():\n    pass\n",
        encoding="utf-8",
    )
    with pytest.raises(gcc.CatalogError, match="EXACTLY one"):
        gcc.discover_markers(tmp_path)


def test_non_literal_id_raises(tmp_path):
    (tmp_path / "test_n.py").write_text(
        'import pytest\nCID = "CASE-001"\n@pytest.mark.qa_case(CID)\n'
        "def test_var():\n    pass\n",
        encoding="utf-8",
    )
    with pytest.raises(gcc.CatalogError, match="LITERAL"):
        gcc.discover_markers(tmp_path)


def test_kwarg_id_raises(tmp_path):
    (tmp_path / "test_k.py").write_text(
        'import pytest\n@pytest.mark.qa_case(id="CASE-001")\n'
        "def test_kw():\n    pass\n",
        encoding="utf-8",
    )
    with pytest.raises(gcc.CatalogError, match="positional"):
        gcc.discover_markers(tmp_path)


def test_malformed_case_id_raises(tmp_path):
    # "CASE-403-routing" previously slipped the >=300 orphan guard -> must raise.
    (tmp_path / "test_bad.py").write_text(
        'import pytest\n@pytest.mark.qa_case("CASE-403-routing")\n'
        "def test_bad():\n    pass\n",
        encoding="utf-8",
    )
    with pytest.raises(gcc.CatalogError, match="invalid CASE id"):
        gcc.discover_markers(tmp_path)


def test_method_level_marker_raises(tmp_path):
    (tmp_path / "test_cls.py").write_text(
        "import pytest\n\nclass TestThing:\n"
        '    @pytest.mark.qa_case("CASE-001")\n'
        "    def test_method(self):\n        pass\n",
        encoding="utf-8",
    )
    with pytest.raises(gcc.CatalogError, match="MODULE-LEVEL"):
        gcc.discover_markers(tmp_path)


def test_decoy_decorator_is_ignored(tmp_path):
    # `notpytest.qa_case(...)` does not resolve through `.mark` -> not the marker.
    (tmp_path / "test_decoy.py").write_text(
        'import pytest\n@notpytest.qa_case("CASE-999")\n'
        "def test_decoy():\n    pass\n",
        encoding="utf-8",
    )
    assert "CASE-999" not in gcc.discover_markers(tmp_path)


# ---- CLI / exit codes (what the CI lane actually invokes) ------------------------

def test_cli_check_passes_on_committed_catalog():
    r = subprocess.run([sys.executable, str(GEN), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, f"--check failed: {r.stdout}\n{r.stderr}"


def test_cli_check_exit_1_on_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gcc, "CATALOG_PATH", tmp_path / "nope.json")
    monkeypatch.setattr(sys, "argv", ["gen_case_catalog.py", "--check"])
    assert gcc.main() == 1


def test_cli_check_exit_1_on_stale_file(tmp_path, monkeypatch):
    stale = tmp_path / "CASE_CATALOG.json"
    stale.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(gcc, "CATALOG_PATH", stale)
    monkeypatch.setattr(sys, "argv", ["gen_case_catalog.py", "--check"])
    assert gcc.main() == 1
