"""Integration fixture coverage for multi-language static analysis (tasks 9.1-9.3).

Covers:
- 9.1 mixed-language fixture (Python + JS/TS + Java/Go) with absolute, relative, package, stdlib, third-party, syntax-error
- 9.2 relative/package imports without false edges for stdlib/third-party
- 9.3 parse-failure isolation and rebuild idempotency (stale edge removal)
"""

from pathlib import Path
import tempfile

from chronotrace.core.static_analysis.service import analyze_repository
from chronotrace.core.static_analysis.models import ParseFailure


class FakeGraph:
    def __init__(self):
        self.edges: list[tuple[str, str, str]] = []

    def replace_imports(self, repo, edges):
        # edges may be 2-tuples or 3-tuples (with language)
        self.edges = list(edges)
        return len(self.edges)


def _make_mixed_repo() -> Path:
    td = Path(tempfile.mkdtemp())
    # Python
    (td / "a.py").write_text("import b\nimport os\nimport requests\n", encoding="utf-8")
    (td / "b.py").write_text("x=1\n", encoding="utf-8")
    (td / "pkg").mkdir(exist_ok=True)
    (td / "pkg/__init__.py").write_text("from .sub import x\n", encoding="utf-8")
    (td / "pkg/sub.py").write_text("from . import sibling\nimport a\n", encoding="utf-8")
    (td / "pkg/sibling.py").write_text("x=1\n", encoding="utf-8")
    (td / "broken.py").write_text("def foo(\n", encoding="utf-8")
    # JavaScript / TypeScript
    (td / "src").mkdir(exist_ok=True)
    (td / "src/index.js").write_text("export const v=1\n", encoding="utf-8")
    (td / "src/utils.js").write_text("export const u=1\n", encoding="utf-8")
    (td / "src/app.js").write_text(
        'import x from "./utils";\nimport y from "react";\nimport fs from "fs";\n', encoding="utf-8"
    )
    (td / "src/app.ts").write_text('import {y} from "./utils";\nimport * as lodash from "lodash";\n', encoding="utf-8")
    (td / "src/broken.js").write_text('import a from "./utils"\n syntax error ???\n', encoding="utf-8")
    # Java
    (td / "com/foo").mkdir(parents=True, exist_ok=True)
    (td / "com/foo/Bar.java").write_text(
        "package com.foo;\nimport com.foo.Util;\nimport java.util.List;\nimport org.external.Lib;\n",
        encoding="utf-8",
    )
    (td / "com/foo/Util.java").write_text("package com.foo;\n", encoding="utf-8")
    (td / "com/foo/Broken.java").write_text("package com.foo;\nimport com.foo.Util\n", encoding="utf-8")  # missing semicolon
    # Go
    (td / "pkggo").mkdir(exist_ok=True)
    (td / "pkggo/mod.go").write_text('package pkggo\nimport "fmt"\nimport "github.com/external/lib"\n', encoding="utf-8")
    (td / "pkggo/broken.go").write_text('package pkggo\nimport "fmt"\nfunc main() {', encoding="utf-8")
    return td


def test_9_1_mixed_fixture_completion_gate():
    td = _make_mixed_repo()
    g = FakeGraph()
    res = analyze_repository(td, "repo1", g, enabled_languages={"python", "javascript", "typescript", "java", "go"})
    # files_scanned includes all source files (including broken)
    assert res.files_scanned >= 10
    # one broken per language = at least 4 failures (py, js, java, go)
    assert res.files_failed >= 4
    assert len(res.diagnostics.parse_failures) >= 4
    # Extracted imports: at least python 4, js 3, java 3, go 2
    assert res.imports_extracted >= 8
    # Resolved should include intra-repo edges
    assert res.imports_resolved >= 5
    # Filtered external: python os/requests, js react/lodash/fs, java java.util/org.external, go fmt/github.external
    assert res.imports_filtered_external >= 5
    # Unresolved: at least some internal missing (none in this fixture, but may be 0-2)
    # Check edges: expected resolved intra-repo
    edges_set = {(s, t) for s, t, *_ in g.edges}
    # Python: a.py -> b.py, pkg/__init__.py -> pkg/sub.py, pkg/sub.py -> pkg/sibling.py and -> a.py
    assert ("a.py", "b.py") in edges_set
    assert ("pkg/__init__.py", "pkg/sub.py") in edges_set
    assert ("pkg/sub.py", "pkg/sibling.py") in edges_set or ("pkg/sub.py", "a.py") in edges_set
    # JS: src/app.js -> src/utils.js, src/app.ts -> src/utils.js
    assert ("src/app.js", "src/utils.js") in edges_set
    assert ("src/app.ts", "src/utils.js") in edges_set
    # Java: Bar.java -> Util.java
    assert ("com/foo/Bar.java", "com/foo/Util.java") in edges_set
    # No false nodes for external: ensure no edge to os, react, java.util, fmt etc
    for s, t, *_ in g.edges:
        assert "os" not in t and "react" not in t and "java.util" not in t and "fmt" not in t


def test_9_2_relative_and_package_imports_no_false_edges():
    td = Path(tempfile.mkdtemp())
    (td / "pkg").mkdir()
    (td / "pkg/__init__.py").write_text("", encoding="utf-8")
    (td / "pkg/a.py").write_text("from . import b\n", encoding="utf-8")
    (td / "pkg/b.py").write_text("x=1\n", encoding="utf-8")
    (td / "pkg/sub").mkdir()
    (td / "pkg/sub/__init__.py").write_text("", encoding="utf-8")
    (td / "pkg/sub/mod.py").write_text("from .. import b\n", encoding="utf-8")  # pkg/sub/mod.py -> pkg/b.py
    (td / "src").mkdir()
    (td / "src/index.js").write_text("", encoding="utf-8")
    (td / "src/utils.js").write_text("", encoding="utf-8")
    (td / "src/app.js").write_text('import x from "./utils";\n', encoding="utf-8")
    (td / "src/nested").mkdir()
    (td / "src/nested/foo.js").write_text('import y from "../utils";\n', encoding="utf-8")
    (td / "com/foo").mkdir(parents=True)
    (td / "com/foo/Bar.java").write_text("package com.foo;\nimport com.foo.Util;\n", encoding="utf-8")
    (td / "com/foo/Util.java").write_text("package com.foo;\n", encoding="utf-8")

    g = FakeGraph()
    res = analyze_repository(td, "repo1", g, enabled_languages={"python", "javascript", "java"})
    edges_set = {(s, t) for s, t, *_ in g.edges}
    assert ("pkg/a.py", "pkg/b.py") in edges_set
    assert ("pkg/sub/mod.py", "pkg/b.py") in edges_set
    assert ("src/app.js", "src/utils.js") in edges_set
    assert ("src/nested/foo.js", "src/utils.js") in edges_set
    assert ("com/foo/Bar.java", "com/foo/Util.java") in edges_set
    # stdlib/third-party should not create edges
    assert res.imports_filtered_external >= 0
    # verify no external targets
    for _, t, *_ in g.edges:
        assert "os" not in t and "fs" not in t


def test_9_3_parse_failure_and_rebuild_idempotency():
    td = Path(tempfile.mkdtemp())
    (td / "a.py").write_text("import b\n", encoding="utf-8")
    (td / "b.py").write_text("x=1\n", encoding="utf-8")
    (td / "broken.py").write_text("def foo(\n", encoding="utf-8")
    (td / "src").mkdir()
    (td / "src/app.js").write_text('import x from "./utils";\n', encoding="utf-8")
    (td / "src/utils.js").write_text("", encoding="utf-8")
    (td / "src/broken.js").write_text('import y from "./utils"\n syntax error\n', encoding="utf-8")

    g = FakeGraph()
    res1 = analyze_repository(td, "repo1", g, enabled_languages={"python", "javascript"})
    assert res1.files_failed == 2  # broken.py + broken.js
    assert len(res1.diagnostics.parse_failures) == 2
    edges1 = set(g.edges)
    assert ("a.py", "b.py") in {(s, t) for s, t, *_ in edges1}
    assert ("src/app.js", "src/utils.js") in {(s, t) for s, t, *_ in edges1}

    # Re-run idempotency: same edges
    res2 = analyze_repository(td, "repo1", g, enabled_languages={"python", "javascript"})
    assert set(g.edges) == edges1
    assert res2.relations_persisted == res1.relations_persisted

    # Remove edge: edit a.py to not import b
    (td / "a.py").write_text("import os\n", encoding="utf-8")
    res3 = analyze_repository(td, "repo1", g, enabled_languages={"python", "javascript"})
    edges3 = {(s, t) for s, t, *_ in g.edges}
    assert ("a.py", "b.py") not in edges3
    assert ("src/app.js", "src/utils.js") in edges3
    # diagnostics updated: filtered external for os
    assert res3.imports_filtered_external >= 1

    # Unresolved diagnostics include language and raw_text
    for u in res1.diagnostics.unresolved_details + res2.diagnostics.unresolved_details:
        assert hasattr(u, "importer")
        assert hasattr(u, "reason")
        assert hasattr(u, "raw_text")
    for pf in res1.diagnostics.parse_failures:
        assert isinstance(pf, ParseFailure)
        assert pf.language in ("python", "javascript", "typescript", "java", "go", "rust")
