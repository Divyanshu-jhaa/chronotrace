"""Focused regression test for tree-sitter parse failure isolation (iter-4)."""

from pathlib import Path
import tempfile

from chronotrace.core.static_analysis.parser import parse_file
from chronotrace.core.static_analysis.models import ParseResult, ParseFailure


def test_broken_python_returns_parse_failure():
    p = Path(tempfile.mktemp(suffix=".py"))
    p.write_text("def foo(\n", encoding="utf-8")
    result = parse_file(p)
    assert isinstance(result, ParseFailure), f"expected ParseFailure got {result}"
    assert result.language == "python"
    assert "SyntaxError" in result.reason
    assert result.lineno == 1


def test_valid_python_returns_parse_result():
    p = Path(tempfile.mktemp(suffix=".py"))
    p.write_text("import os\nfrom . import sibling\n", encoding="utf-8")
    result = parse_file(p)
    assert isinstance(result, ParseResult)
    assert len(result.raw_imports) == 2
    assert result.language == "python"
    assert result.success is True


def test_broken_js_with_valid_import_is_failure_isolated():
    # tree-sitter recovers partial imports but has_error still marks file as failed per spec
    p = Path(tempfile.mktemp(suffix=".js"))
    p.write_text('import x from "./utils";\n syntax error ???\n', encoding="utf-8")
    result = parse_file(p)
    # File contains syntax error -> should be ParseFailure (has_error True)
    # This verifies error isolation does not return success
    assert isinstance(result, ParseFailure)
    assert result.language == "javascript"


def test_per_file_isolation_via_service():
    from chronotrace.core.static_analysis.service import analyze_repository

    td = Path(tempfile.mkdtemp())
    (td / "good.py").write_text("import b\n", encoding="utf-8")
    (td / "b.py").write_text("x=1\n", encoding="utf-8")
    (td / "broken.py").write_text("def foo(\n", encoding="utf-8")

    class FakeGraph:
        def __init__(self):
            self.edges = []

        def replace_imports(self, repo, edges):
            self.edges = edges
            return len(edges)

    g = FakeGraph()
    res = analyze_repository(td, "repo1", g, enabled_languages={"python"})
    # good.py + b.py parsed, broken.py failed -> 3 scanned, 2 parsed, 1 failed
    assert res.files_scanned == 3
    assert res.files_parsed == 2
    assert res.files_failed == 1
    assert len(res.diagnostics.parse_failures) == 1
    assert res.diagnostics.parse_failures[0].path == "broken.py" or "broken.py" in str(res.diagnostics.parse_failures[0].path)
    # Valid edge still persisted
    assert ("good.py", "b.py") in [(e[0], e[1]) for e in g.edges] or ("good.py", "b.py", "python") in g.edges
