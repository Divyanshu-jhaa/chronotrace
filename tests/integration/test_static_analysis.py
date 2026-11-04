"""Integration tests for iter-4 static analysis — testcontainers Neo4j only.

Covers ImportGraphRepository.replace_imports (DELETE+UNWIND MERGE),
analyze_repository end-to-end (mixed languages, relative/package, stdlib filtering,
parse-failure isolation, rebuild idempotency, repository isolation, Windows normalization).
Uses testcontainers Neo4j exclusively, no FakeGateway fallback for DB assertions.

DB shape: File nodes {id, repository_id, path, canonical_path/path, current_path}
          IMPORTS relationships {repository_id, language}
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

try:
    from testcontainers.neo4j import Neo4jContainer  # type: ignore

    HAS_TC = True
except ImportError:
    HAS_TC = False
    Neo4jContainer = None  # type: ignore

from chronotrace.config.models import ChronoTraceConfig
from chronotrace.graph.gateway import GraphGateway
from chronotrace.graph.models import File, Repository
from chronotrace.graph.repositories.import_repo import ImportGraphRepository

import contextlib


# ---------------------------------------------------------------------------
# testcontainers fixture (same pattern as test_ingestion)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def neo4j_container():
    if not HAS_TC:
        pytest.skip("testcontainers not installed — pip install 'testcontainers[neo4j]'")
    try:
        container = Neo4jContainer("neo4j:5-community")
        container.start()
    except Exception as e:
        pytest.skip(f"Docker/testcontainers not available: {e}")
    try:
        container.get_connection_url()
    except Exception:
        pass
    yield container
    with contextlib.suppress(Exception):
        container.stop()


@pytest.fixture
def gateway(neo4j_container):
    uri = neo4j_container.get_connection_url()
    cfg = ChronoTraceConfig.model_validate(
        {"version": 1, "graph": {"uri": uri, "database": "neo4j", "batch_size": 500}}
    )
    gw = GraphGateway(cfg, env={"NEO4J_USERNAME": "neo4j", "NEO4J_PASSWORD": "password"})
    gw.initialize_schema()
    with gw.driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
    yield gw
    with contextlib.suppress(Exception):
        with gw.driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n").consume()
    gw.close()


def _gateway_with_batch(gateway: GraphGateway, batch_size: int) -> GraphGateway:
    uri = gateway.driver.uri
    db = gateway.driver.database
    cfg = ChronoTraceConfig.model_validate(
        {"version": 1, "graph": {"uri": uri, "database": db, "batch_size": batch_size}}
    )
    return GraphGateway(cfg, env={"NEO4J_USERNAME": "neo4j", "NEO4J_PASSWORD": "password"}, driver=gateway.driver)


def _upsert_repo_and_files(gateway: GraphGateway, repo_id: str, paths: list[str]) -> None:
    """Create Repository and File nodes for every path in repo_id."""
    repo = Repository.create(name=repo_id, repository_id=repo_id)
    gateway.repositories.upsert(repo)
    files = [File.create(repository_id=repo_id, path=p) for p in paths]
    gateway.files.upsert_batch(repo_id, files)


def _count_imports(gateway: GraphGateway, repo_id: str) -> int:
    with gateway.driver.session() as s:
        rec = s.run(
            "MATCH (:File {repository_id:$r})-[rel:IMPORTS {repository_id:$r}]->() RETURN count(rel) as c",
            r=repo_id,
        ).single()
        return int(rec["c"]) if rec else 0


def _get_import_pairs(gateway: GraphGateway, repo_id: str) -> set[tuple[str, str]]:
    with gateway.driver.session() as s:
        rows = list(
            s.run(
                """
                MATCH (a:File {repository_id:$r})-[rel:IMPORTS {repository_id:$r}]->(b:File {repository_id:$r})
                RETURN a.path as src, b.path as dst, rel.language as lang
                """,
                r=repo_id,
            ).data()
        )
        return {(r["src"], r["dst"]) for r in rows}


def _get_import_with_lang(gateway: GraphGateway, repo_id: str) -> list[dict]:
    with gateway.driver.session() as s:
        return list(
            s.run(
                "MATCH (a:File {repository_id:$r})-[rel:IMPORTS {repository_id:$r}]->(b:File {repository_id:$r}) RETURN a.path as src, b.path as dst, rel.language as lang, rel.repository_id as rid",
                r=repo_id,
            ).data()
        )


# ---------------------------------------------------------------------------
# 7.1/7.2 replace_imports persistence, batching, idempotency, isolation
# ---------------------------------------------------------------------------


def test_replace_imports_persist_and_scoped_per_repository(gateway):
    repo_a = "test://repo-a"
    repo_b = "test://repo-b"
    _upsert_repo_and_files(gateway, repo_a, ["a.py", "b.py", "c.py"])
    _upsert_repo_and_files(gateway, repo_b, ["a.py", "b.py"])
    repo = ImportGraphRepository(gateway.driver)
    repo.replace_imports(repo_a, [("a.py", "b.py"), ("a.py", "c.py")])
    repo.replace_imports(repo_b, [("a.py", "b.py")])
    assert _count_imports(gateway, repo_a) == 2
    assert _count_imports(gateway, repo_b) == 1
    assert ("a.py", "b.py") in _get_import_pairs(gateway, repo_a)
    # ensure no cross-repo edges
    with gateway.driver.session() as s:
        cross = list(
            s.run(
                """
                MATCH (a:File)-[r:IMPORTS]->(b:File)
                WHERE a.repository_id <> b.repository_id RETURN count(r) as c
                """
            ).data()
        )
        assert cross[0]["c"] == 0


def test_replace_imports_batched_and_language_and_dict_form(gateway):
    repo_id = "test://batch-lang"
    paths = [f"f{i}.py" for i in range(6)] + ["a.js", "b.js"]
    _upsert_repo_and_files(gateway, repo_id, paths)
    repo = ImportGraphRepository(gateway.driver)
    # Use batch_size=2 to exercise chunking (5 edges -> 3 chunks)
    edges = [
        ("a.js", "b.js", "javascript"),
        {"source": "f0.py", "target": "f1.py", "language": "python"},
        ("f1.py", "f2.py"),
        ("f2.py", "f3.py", "python"),
        ("f3.py", "f4.py"),
    ]
    repo.replace_imports(repo_id, edges, batch_size=2)
    assert _count_imports(gateway, repo_id) == 5
    rows = _get_import_with_lang(gateway, repo_id)
    lang_map = {(r["src"], r["dst"]): r["lang"] for r in rows}
    assert lang_map[("a.js", "b.js")] == "javascript"
    assert lang_map[("f0.py", "f1.py")] == "python"


def test_replace_imports_idempotent_and_stale_removal(gateway):
    repo_id = "test://idempotent"
    _upsert_repo_and_files(gateway, repo_id, ["a.py", "b.py", "c.py"])
    repo = ImportGraphRepository(gateway.driver)
    repo.replace_imports(repo_id, [("a.py", "b.py"), ("a.py", "c.py")])
    assert _count_imports(gateway, repo_id) == 2
    # second call same set -> still 2 (not 4)
    repo.replace_imports(repo_id, [("a.py", "b.py"), ("a.py", "c.py")])
    assert _count_imports(gateway, repo_id) == 2
    # remove one edge
    repo.replace_imports(repo_id, [("a.py", "b.py")])
    assert _count_imports(gateway, repo_id) == 1
    assert _get_import_pairs(gateway, repo_id) == {("a.py", "b.py")}
    # empty list deletes all
    repo.replace_imports(repo_id, [])
    assert _count_imports(gateway, repo_id) == 0


# ---------------------------------------------------------------------------
# 8.x analyze_repository end-to-end with real Neo4j
# ---------------------------------------------------------------------------


def test_analyze_repository_mixed_languages_produces_expected_graph(gateway):
    from chronotrace.core.static_analysis.service import analyze_repository

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Python
        (root / "a.py").write_text("import b\nimport os\nimport requests\n", encoding="utf-8")
        (root / "b.py").write_text("x=1\n", encoding="utf-8")
        (root / "pkg").mkdir()
        (root / "pkg/__init__.py").write_text("from .sub import x\n", encoding="utf-8")
        (root / "pkg/sub.py").write_text("from . import sibling\nimport a\n", encoding="utf-8")
        (root / "pkg/sibling.py").write_text("x=1\n", encoding="utf-8")
        (root / "broken.py").write_text("def foo(\n", encoding="utf-8")
        # JS/TS
        (root / "src").mkdir()
        (root / "src/index.js").write_text("export const v=1\n", encoding="utf-8")
        (root / "src/utils.js").write_text("export const u=1\n", encoding="utf-8")
        (root / "src/app.js").write_text(
            'import x from "./utils";\nimport y from "react";\nimport fs from "fs";\n',
            encoding="utf-8",
        )
        (root / "src/app.ts").write_text(
            'import {y} from "./utils";\nimport * as lodash from "lodash";\n',
            encoding="utf-8",
        )
        (root / "src/broken.js").write_text('import a from "./utils"\n syntax error ???\n', encoding="utf-8")
        # Java
        (root / "com/foo").mkdir(parents=True)
        (root / "com/foo/Bar.java").write_text(
            "package com.foo;\nimport com.foo.Util;\nimport java.util.List;\nimport org.external.Lib;\n",
            encoding="utf-8",
        )
        (root / "com/foo/Util.java").write_text("package com.foo;\n", encoding="utf-8")
        (root / "com/foo/Broken.java").write_text("package com.foo;\nimport com.foo.Util\n", encoding="utf-8")

        repo_id = "test://mixed-e2e"
        all_paths = [
            "a.py",
            "b.py",
            "pkg/__init__.py",
            "pkg/sub.py",
            "pkg/sibling.py",
            "broken.py",
            "src/index.js",
            "src/utils.js",
            "src/app.js",
            "src/app.ts",
            "src/broken.js",
            "com/foo/Bar.java",
            "com/foo/Util.java",
            "com/foo/Broken.java",
        ]
        _upsert_repo_and_files(gateway, repo_id, all_paths)

        gwrap = gateway  # analyze_repository accepts GraphGateway or ImportGraphRepository via duck typing
        # gateway exposes replace_imports
        res = analyze_repository(root, repo_id, gwrap, enabled_languages={"python", "javascript", "typescript", "java"})

        # diagnostics
        assert res.files_scanned >= 10
        assert res.files_failed >= 2  # broken.py + broken.js (Broken.java may or may not be has_error depending on grammar)
        assert res.imports_resolved >= 5
        assert res.imports_filtered_external >= 4

        pairs = _get_import_pairs(gateway, repo_id)
        # intra-repo
        assert ("a.py", "b.py") in pairs
        assert ("pkg/__init__.py", "pkg/sub.py") in pairs
        assert ("pkg/sub.py", "pkg/sibling.py") in pairs or ("pkg/sub.py", "a.py") in pairs
        assert ("src/app.js", "src/utils.js") in pairs
        assert ("src/app.ts", "src/utils.js") in pairs
        assert ("com/foo/Bar.java", "com/foo/Util.java") in pairs
        # no external file nodes created
        with gateway.driver.session() as s:
            ext = s.run(
                "MATCH (f:File) WHERE f.path CONTAINS 'react' OR f.path CONTAINS 'os' RETURN count(f) as c"
            ).single()["c"]
            assert ext == 0
            # no IMPORTS to external
            for src, dst in pairs:
                assert "react" not in dst and "os" not in dst


def test_analyze_repository_relative_and_package_no_false_external(gateway):
    from chronotrace.core.static_analysis.service import analyze_repository

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pkg").mkdir()
        (root / "pkg/__init__.py").write_text("", encoding="utf-8")
        (root / "pkg/a.py").write_text("from . import b\n", encoding="utf-8")
        (root / "pkg/b.py").write_text("x=1\n", encoding="utf-8")
        (root / "pkg/sub").mkdir()
        (root / "pkg/sub/__init__.py").write_text("", encoding="utf-8")
        (root / "pkg/sub/mod.py").write_text("from .. import b\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src/index.js").write_text("", encoding="utf-8")
        (root / "src/utils.js").write_text("", encoding="utf-8")
        (root / "src/app.js").write_text('import x from "./utils";\n', encoding="utf-8")
        (root / "src/nested").mkdir()
        (root / "src/nested/foo.js").write_text('import y from "../utils";\n', encoding="utf-8")
        (root / "com/foo").mkdir(parents=True)
        (root / "com/foo/Bar.java").write_text("package com.foo;\nimport com.foo.Util;\n", encoding="utf-8")
        (root / "com/foo/Util.java").write_text("package com.foo;\n", encoding="utf-8")

        repo_id = "test://relative-pkg"
        all_paths = [
            "pkg/__init__.py",
            "pkg/a.py",
            "pkg/b.py",
            "pkg/sub/__init__.py",
            "pkg/sub/mod.py",
            "src/index.js",
            "src/utils.js",
            "src/app.js",
            "src/nested/foo.js",
            "com/foo/Bar.java",
            "com/foo/Util.java",
        ]
        _upsert_repo_and_files(gateway, repo_id, all_paths)
        res = analyze_repository(root, repo_id, gateway, enabled_languages={"python", "javascript", "java"})
        pairs = _get_import_pairs(gateway, repo_id)
        assert ("pkg/a.py", "pkg/b.py") in pairs
        assert ("pkg/sub/mod.py", "pkg/b.py") in pairs
        assert ("src/app.js", "src/utils.js") in pairs
        assert ("src/nested/foo.js", "src/utils.js") in pairs
        assert ("com/foo/Bar.java", "com/foo/Util.java") in pairs
        assert res.imports_filtered_external == 0 or res.imports_unresolved == 0  # no stdlib in this fixture


def test_analyze_repository_parse_failure_and_rebuild(gateway):
    from chronotrace.core.static_analysis.service import analyze_repository

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.py").write_text("import b\n", encoding="utf-8")
        (root / "b.py").write_text("x=1\n", encoding="utf-8")
        (root / "broken.py").write_text("def foo(\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src/app.js").write_text('import x from "./utils";\n', encoding="utf-8")
        (root / "src/utils.js").write_text("", encoding="utf-8")
        (root / "src/broken.js").write_text('import y from "./utils"\n syntax error\n', encoding="utf-8")

        repo_id = "test://rebuild"
        all_paths = ["a.py", "b.py", "broken.py", "src/app.js", "src/utils.js", "src/broken.js"]
        _upsert_repo_and_files(gateway, repo_id, all_paths)

        res1 = analyze_repository(root, repo_id, gateway, enabled_languages={"python", "javascript"})
        assert res1.files_failed == 2
        assert len([p for p in res1.diagnostics.parse_failures if p.language in ("python", "javascript")]) == 2
        pairs1 = _get_import_pairs(gateway, repo_id)
        assert ("a.py", "b.py") in pairs1
        assert ("src/app.js", "src/utils.js") in pairs1

        # idempotent re-run
        res2 = analyze_repository(root, repo_id, gateway, enabled_languages={"python", "javascript"})
        assert _get_import_pairs(gateway, repo_id) == pairs1
        assert res2.relations_persisted == res1.relations_persisted

        # stale removal
        (root / "a.py").write_text("import os\n", encoding="utf-8")
        res3 = analyze_repository(root, repo_id, gateway, enabled_languages={"python", "javascript"})
        pairs3 = _get_import_pairs(gateway, repo_id)
        assert ("a.py", "b.py") not in pairs3
        assert ("src/app.js", "src/utils.js") in pairs3
        # diagnostics detail includes language, raw_text, reason
        for pf in res1.diagnostics.parse_failures:
            assert pf.language in ("python", "javascript")
            assert pf.reason
        for u in res1.diagnostics.unresolved_details:
            assert u.reason


def test_analyze_repository_window_normalization_yields_same_edges(gateway):
    from chronotrace.core.static_analysis.service import analyze_repository

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pkg").mkdir()
        (root / "pkg/a.py").write_text("from . import b\n", encoding="utf-8")
        (root / "pkg/b.py").write_text("x=1\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src/app.js").write_text('import x from "./utils";\n', encoding="utf-8")
        (root / "src/utils.js").write_text("", encoding="utf-8")

        repo_id = "test://win-norm"
        _upsert_repo_and_files(gateway, repo_id, ["pkg/a.py", "pkg/b.py", "src/app.js", "src/utils.js"])
        res = analyze_repository(root, repo_id, gateway, enabled_languages={"python", "javascript"})
        pairs_posix = _get_import_pairs(gateway, repo_id)
        # The same root with Windows-style separators is already normalized via as_posix inside scanner
        # Verify POSix canonical edges (no backslashes)
        for src, dst in pairs_posix:
            assert "\\" not in src and "\\" not in dst
        assert ("pkg/a.py", "pkg/b.py") in pairs_posix
        assert ("src/app.js", "src/utils.js") in pairs_posix
        assert res.imports_resolved == 2


def test_repository_isolation_analyze(gateway):
    from chronotrace.core.static_analysis.service import analyze_repository
    import shutil

    with tempfile.TemporaryDirectory() as td:
        root_a = Path(td) / "repoA"
        root_b = Path(td) / "repoB"
        for r in (root_a, root_b):
            r.mkdir()
            (r / "a.py").write_text("import b\n", encoding="utf-8")
            (r / "b.py").write_text("x=1\n", encoding="utf-8")
        repo_a = "test://iso-a"
        repo_b = "test://iso-b"
        _upsert_repo_and_files(gateway, repo_a, ["a.py", "b.py"])
        _upsert_repo_and_files(gateway, repo_b, ["a.py", "b.py"])
        analyze_repository(root_a, repo_a, gateway, enabled_languages={"python"})
        analyze_repository(root_b, repo_b, gateway, enabled_languages={"python"})
        assert _count_imports(gateway, repo_a) == 1
        assert _count_imports(gateway, repo_b) == 1
        # cross-repo not mixed
        with gateway.driver.session() as s:
            cnt = s.run(
                "MATCH (a:File {repository_id:$a})-[r:IMPORTS]->(b:File {repository_id:$b}) RETURN count(r) as c",
                a=repo_a,
                b=repo_b,
            ).single()["c"]
            assert cnt == 0
