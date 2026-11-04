"""Tree-sitter parsing without execution — extract imports, isolate failures."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .models import ParseFailure, ParseResult, RawImport

# ---------------------------------------------------------------------------
# Lazy tree-sitter imports
# ---------------------------------------------------------------------------
try:
    from tree_sitter import Language, Parser, Query  # type: ignore
except Exception:  # pragma: no cover
    Language = Parser = Query = None  # type: ignore

# ---------------------------------------------------------------------------
# Helpers: Python text parsing (fallback and tree-sitter post-processing)
# ---------------------------------------------------------------------------

def _parse_python_import_statement_text(raw: str):
    """Parse ``import ...`` raw text into list of (name, asname)."""
    # raw like "import os", "import os as _os", "import os, sys as s"
    # strip leading "import"
    stripped = raw.strip()
    # Remove comments after? Not needed
    if stripped.startswith("import"):
        tail = stripped[len("import"):].strip()
    else:
        tail = stripped
    # Remove any trailing semicolon?
    tail = tail.rstrip(";").strip()
    # Split on commas ignoring parentheses? Simple split
    # Also handle multiline with backslash/newline: normalize whitespace
    # Remove parentheses wrapping not typical for import_statement
    # Split
    parts: list[tuple[str, str | None]] = []
    # Handle parentheses case not for import_statement but just in case
    # Use simple comma split
    for part in tail.split(","):
        p = part.strip()
        if not p:
            continue
        # Remove surrounding parentheses
        p = p.strip("() ").strip()
        if not p:
            continue
        if " as " in p:
            name, asname = [x.strip() for x in p.split(" as ", 1)]
            # name may be dotted like os.path
            parts.append((name, asname))
        else:
            parts.append((p.strip(), None))
    return parts


def _parse_python_import_names(tail: str):
    """Parse names tail after ``import`` in from-statement.

    Handles ``a, b as c, *`` and ``(a, b)`` etc.
    Returns (names, asnames).
    """
    t = tail.strip()
    # Remove surrounding parentheses
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip()
    # Remove any trailing semicolon
    t = t.rstrip(";").strip()
    if t == "*":
        return ["*"], [None]
    names: list[str] = []
    asnames: list[str | None] = []
    for part in t.split(","):
        p = part.strip()
        if not p:
            continue
        p = p.strip("() ").strip()
        if not p:
            continue
        if " as " in p:
            name, alias = [x.strip() for x in p.split(" as ", 1)]
            names.append(name)
            asnames.append(alias)
        else:
            names.append(p.strip())
            asnames.append(None)
    return names, asnames


def _parse_python_from_text(raw: str):
    """Parse ``from ... import ...`` raw text.

    Returns (level, module, names, asnames).
    """
    # Normalize whitespace, remove newlines
    # raw may contain "from . import x" or "from ..utils import helper"
    s = raw.strip()
    # Use regex with DOTALL to handle multiline
    # Pattern: from [dots][module] import names
    # dots: leading .+  module: dotted_name optional  import tail: rest
    m = re.match(
        r"^from\s+(\.+)?([A-Za-z_][A-Za-z0-9_\.]*)?\s+import\s+(.+)$",
        s,
        re.DOTALL,
    )
    if not m:
        # Try alternative handling for case where from ... import stripped has extra spaces?
        # Fallback: attempt simple split
        try:
            # Find " import " from end? Use rsplit
            if " import " not in s:
                return 0, None, [], []
            before, after = s.rsplit(" import ", 1)
            before = before.strip()
            # before is "from X" or "from ..X"
            if not before.startswith("from"):
                return 0, None, [], []
            mod_part = before[len("from"):].strip()
            # mod_part may be " . ", ".utils", "..", "..utils"
            dots = ""
            mod = None
            if mod_part.startswith("."):
                # count leading dots
                idx = 0
                while idx < len(mod_part) and mod_part[idx] == ".":
                    idx += 1
                dots = mod_part[:idx]
                rest = mod_part[idx:].strip()
                if rest:
                    mod = rest
            else:
                mod = mod_part if mod_part else None
            level = len(dots) if dots else 0
            # Need to handle relative without module: from . import x -> dots=".", mod=None
            names, asnames = _parse_python_import_names(after)
            return level, mod, names, asnames
        except Exception:
            return 0, None, [], []

    dots = m.group(1) or ""
    mod = m.group(2)
    tail = m.group(3)
    level = len(dots) if dots else 0
    # If dots empty, this is absolute; else relative. If dots and mod is None, module is None.
    # mod empty string treated as None
    if mod == "":
        mod = None
    # tail may contain parentheses and line continuations
    # Clean tail: replace newlines with space
    tail = tail.replace("\n", " ").strip()
    # Handle trailing comments? strip
    # For "*" case
    names, asnames = _parse_python_import_names(tail)
    return level, mod, names, asnames


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in ('"', "'", "`") and s[-1] == s[0]:
        return s[1:-1]
    return s


def _extract_js_specifier(raw: str) -> str | None:
    m = re.search(r'from\s+["\'`]([^"\'`]+)["\'`]', raw)
    if m:
        return m.group(1)
    # fallback for bare require? Not here
    return None


def _extract_js_import_names(raw: str):
    """Extract imported names from JS import clause.

    Handles:
      import foo from "./mod"
      import {x, y as z} from "./mod"
      import * as foo from "./mod"
      import foo, {bar} from "./mod"
    """
    # Find part between 'import' and 'from'
    m = re.search(r'import\s+(.+?)\s+from\s+["\'`]', raw, re.DOTALL)
    if not m:
        # default import maybe not using from? unlikely
        return [], []
    clause = m.group(1).strip()
    names: list[str] = []
    asnames: list[str | None] = []
    # Handle combined foo, {bar}
    # Remove line breaks
    clause = clause.replace("\n", " ").strip()
    # If clause contains braces
    if "{" in clause:
        # Extract brace content
        brace_match = re.search(r'\{([^}]+)\}', clause)
        if brace_match:
            inner = brace_match.group(1)
            for part in inner.split(","):
                p = part.strip()
                if not p:
                    continue
                if " as " in p:
                    name, alias = [x.strip() for x in p.split(" as ", 1)]
                    names.append(name)
                    asnames.append(alias)
                else:
                    names.append(p.strip())
                    asnames.append(None)
        # Also check for default import before brace: "foo, {bar}"
        # Extract default before comma
        if "," in clause:
            pre = clause.split("{")[0].strip().rstrip(",").strip()
            if pre and pre not in names:
                # pre may be "foo"
                # Could be "foo" default
                names.insert(0, pre.split(",")[0].strip())
                asnames.insert(0, None)
        # Also handle "* as foo"
        if "*" in clause:
            star_match = re.search(r'\*\s+as\s+([A-Za-z0-9_\$]+)', clause)
            if star_match:
                names.append(star_match.group(1))
                asnames.append(None)
    elif clause.startswith("*"):
        star_match = re.search(r'\*\s+as\s+([A-Za-z0-9_\$]+)', clause)
        if star_match:
            names.append(star_match.group(1))
            asnames.append(None)
        else:
            names.append(clause)
            asnames.append(None)
    else:
        # default import: clause is single identifier
        # Could be empty? Check
        clause_clean = clause.strip().split(",")[0].strip()
        if clause_clean:
            names.append(clause_clean)
            asnames.append(None)
    # If no names extracted but clause present, treat as default
    if not names and clause:
        # fallback
        names = [clause.split(",")[0].strip().split()[0]]
        asnames = [None]
    return names, asnames


# ---------------------------------------------------------------------------
# Fallback regex-based extraction when tree-sitter unavailable
# ---------------------------------------------------------------------------

def _fallback_python_parse(raw_text: str, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    # Use line-by-line regex? But handle multiline import with parentheses and backslash.
    # Simpler: Use ast fallback when possible
    try:
        tree = ast.parse(raw_text)
        return _extract_via_ast(tree, language)
    except SyntaxError:
        pass
    except Exception:
        pass
    # Regex fallback for syntax error case: still try to find import lines outside ERROR?
    # Find import statements via regex per line
    for idx, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # import X
        if stripped.startswith("import "):
            aliases = _parse_python_import_statement_text(stripped)
            for name, asname in aliases:
                imports.append(
                    RawImport(
                        module=name,
                        names=[name],
                        asnames=[asname],
                        level=0,
                        lineno=idx,
                        raw_text=stripped,
                        language=language,
                        is_relative=False,
                    )
                )
        elif stripped.startswith("from "):
            level, mod, names, asnames = _parse_python_from_text(stripped)
            if not names:
                continue
            imports.append(
                RawImport(
                    module=mod,
                    names=names,
                    asnames=asnames,
                    level=level,
                    lineno=idx,
                    raw_text=stripped,
                    language=language,
                    is_relative=bool(level > 0),
                )
            )
    return imports


def _fallback_js_parse(raw_text: str, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    lines = raw_text.splitlines()
    # Use regex across full text for multiline? Simple line approach + full text search for require
    # Find all import ... from "..."
    # Use global regex over raw_text to capture import statements possibly spanning lines
    import_pattern = re.compile(
        r'import\s+(?:[^;]*?)\s+from\s+["\'`]([^"\'`]+)["\'`]\s*;?',
        re.DOTALL,
    )
    for m in import_pattern.finditer(raw_text):
        full = m.group(0)
        spec = m.group(1)
        # Compute lineno: count newlines before match
        lineno = raw_text[: m.start()].count("\n") + 1
        raw_clean = full.strip()
        is_rel = spec.startswith(".")
        names, asnames = _extract_js_import_names(raw_clean)
        if not names:
            names = [spec]
            asnames = [None]
        imports.append(
            RawImport(
                module=spec,
                names=names,
                asnames=asnames,
                level=0,
                lineno=lineno,
                raw_text=raw_clean,
                language=language,
                is_relative=is_rel,
            )
        )
    # Also handle side-effect import: import "./mod";
    side_pattern = re.compile(r'import\s+["\'`]([^"\'`]+)["\'`]\s*;')
    for m in side_pattern.finditer(raw_text):
        # Check if already captured as part of previous? If previous pattern captured this, skip duplication
        # Use position check
        start = m.start()
        # If this match is subset of previous import_pattern? Check overlapping
        overlapped = False
        for prev in import_pattern.finditer(raw_text):
            if prev.start() <= start < prev.end():
                overlapped = True
                break
        if overlapped:
            continue
        spec = m.group(1)
        lineno = raw_text[: start].count("\n") + 1
        raw_clean = m.group(0).strip()
        imports.append(
            RawImport(
                module=spec,
                names=[spec],
                asnames=[None],
                level=0,
                lineno=lineno,
                raw_text=raw_clean,
                language=language,
                is_relative=spec.startswith("."),
            )
        )
    # require(...)
    require_pattern = re.compile(r'require\s*\(\s*["\'`]([^"\'`]+)["\'`]\s*\)')
    for m in require_pattern.finditer(raw_text):
        spec = m.group(1)
        lineno = raw_text[: m.start()].count("\n") + 1
        raw_clean = m.group(0).strip()
        # Try to get surrounding line raw_text: use line containing require
        # Use raw_clean as raw_text but also try to capture full call line?
        imports.append(
            RawImport(
                module=spec,
                names=[spec],
                asnames=[None],
                level=0,
                lineno=lineno,
                raw_text=raw_clean,
                language=language,
                is_relative=spec.startswith("."),
            )
        )
    return imports


def _fallback_java_parse(raw_text: str, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    # Java imports: lines starting with import
    for idx, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("import "):
            continue
        m = re.search(r'import\s+(?:static\s+)?([A-Za-z0-9_\.\*]+)\s*;', stripped)
        if not m:
            continue
        spec = m.group(1).rstrip(";").strip()
        # Remove trailing .*?
        spec_clean = spec
        if spec_clean.endswith(".*"):
            spec_clean = spec_clean[:-2]
        imports.append(
            RawImport(
                module=spec_clean,
                names=[spec_clean],
                asnames=[None],
                level=0,
                lineno=idx,
                raw_text=stripped,
                language=language,
                is_relative=False,
            )
        )
    # Also global search for multiline? handle
    return imports


def _fallback_go_parse(raw_text: str, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    # Find "import" blocks
    # Single: import "fmt" or import alias "fmt"
    single_pattern = re.compile(r'import\s+(?:[A-Za-z_][A-Za-z0-9_]*\s+)?["\'`]([^"\'`]+)["\'`]')
    # Block pattern: import ( ... )
    block_pattern = re.compile(r'import\s*\(\s*(.*?)\s*\)', re.DOTALL)
    # First handle block
    for m in block_pattern.finditer(raw_text):
        block_content = m.group(1)
        lineno_block = raw_text[: m.start()].count("\n") + 1
        for sm in single_pattern.finditer(block_content):
            spec = sm.group(1)
            # Compute lineno offset inside block? Approximate to block start + lines before spec
            # Find spec position inside block
            spec_pos = block_content.find(spec)
            pre = block_content[:spec_pos]
            lineno = lineno_block + pre.count("\n")
            raw_clean = sm.group(0).strip()
            imports.append(
                RawImport(
                    module=spec,
                    names=[spec],
                    asnames=[None],
                    level=0,
                    lineno=lineno,
                    raw_text=raw_clean,
                    language=language,
                    is_relative=spec.startswith("."),
                )
            )
    # If no block, handle singles not overlapping block
    # Check if block exists; if so, singles outside block may still be needed
    # Find all singles at top level
    for m in single_pattern.finditer(raw_text):
        # skip if inside a block (already captured)
        block_spans = [ (bm.start(), bm.end()) for bm in block_pattern.finditer(raw_text) ]
        inside = any(bstart <= m.start() < bend for bstart, bend in block_spans)
        if inside:
            continue
        spec = m.group(1)
        lineno = raw_text[: m.start()].count("\n") + 1
        raw_clean = m.group(0).strip()
        imports.append(
            RawImport(
                module=spec,
                names=[spec],
                asnames=[None],
                level=0,
                lineno=lineno,
                raw_text=raw_clean,
                language=language,
                is_relative=spec.startswith("."),
            )
        )
    return imports


def _fallback_rust_parse(raw_text: str, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    for idx, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("use "):
            continue
        m = re.search(r'use\s+([^;]+);', stripped)
        if not m:
            continue
        spec = m.group(1).strip()
        # remove mod alias and braces simplified: spec may be "std::collections::HashMap"
        # Handle "crate::" prefix? Keep as is
        # Remove trailing alias: "std::collections::HashMap as MyMap"
        if " as " in spec:
            spec = spec.split(" as ")[0].strip()
        imports.append(
            RawImport(
                module=spec,
                names=[spec],
                asnames=[None],
                level=0,
                lineno=idx,
                raw_text=stripped,
                language=language,
                is_relative=spec.startswith("crate") or spec.startswith("super") or spec.startswith("self"),
            )
        )
    return imports


def _fallback_parse(raw_text: str, language: str) -> list[RawImport]:
    if language == "python":
        return _fallback_python_parse(raw_text, language)
    if language in ("javascript", "typescript"):
        return _fallback_js_parse(raw_text, language)
    if language == "java":
        return _fallback_java_parse(raw_text, language)
    if language == "go":
        return _fallback_go_parse(raw_text, language)
    if language == "rust":
        return _fallback_rust_parse(raw_text, language)
    return []


# ---------------------------------------------------------------------------
# Tree-sitter based extraction
# ---------------------------------------------------------------------------

def _decode_node_text(node, source_bytes: bytes) -> str:
    try:
        return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="surrogateescape")
    except Exception:
        try:
            return source_bytes[node.start_byte : node.end_byte].decode(errors="surrogateescape")
        except Exception:
            return ""


def _get_captures(query, root_node):
    """Normalize query captures to list[(node, capture_name)]."""
    # Try captures()
    try:
        caps = query.captures(root_node)
        if isinstance(caps, dict):
            result: list[tuple[object, str]] = []
            for cap_name, nodes in caps.items():
                for n in nodes:  # type: ignore
                    result.append((n, cap_name))
            return result
        if isinstance(caps, list):
            result = []
            for item in caps:
                if isinstance(item, tuple) and len(item) == 2:
                    n, cap = item
                    # cap may be string or maybe int? Normalize to string
                    if isinstance(cap, str):
                        result.append((n, cap))
                    else:
                        result.append((n, str(cap)))
                elif isinstance(item, dict):
                    for k, nodes in item.items():
                        for n in nodes:  # type: ignore
                            result.append((n, k))
            if result:
                return result
    except Exception:
        pass
    # Try matches()
    try:
        matches = query.matches(root_node)
        result = []
        for _, capture_dict in matches:  # type: ignore
            # capture_dict may be dict[str, list[Node]]
            if isinstance(capture_dict, dict):
                for cap_name, nodes in capture_dict.items():
                    for n in nodes:  # type: ignore
                        result.append((n, cap_name))
        if result:
            return result
    except Exception:
        pass
    return []


def _extract_python_tree(captures, source_bytes: bytes, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    seen: set[tuple[int, int]] = set()
    for node, cap_name in captures:
        # Only consider import-like nodes captured as imp/import
        if cap_name not in ("imp", "import", "from", "import_statement", "import_from_statement"):
            # query defines @imp for both python imports, so filter by node type
            if node.type not in ("import_statement", "import_from_statement"):  # type: ignore
                continue
        # Ensure node type is expected
        if node.type not in ("import_statement", "import_from_statement"):  # type: ignore
            continue
        key = (node.start_byte, node.end_byte)  # type: ignore
        if key in seen:
            continue
        seen.add(key)
        raw = _decode_node_text(node, source_bytes)  # type: ignore
        raw_stripped = raw.strip()
        lineno = int(node.start_point[0] + 1)  # type: ignore
        if node.type == "import_statement":  # type: ignore
            aliases = _parse_python_import_statement_text(raw_stripped)
            for name, asname in aliases:
                imports.append(
                    RawImport(
                        module=name,
                        names=[name],
                        asnames=[asname],
                        level=0,
                        lineno=lineno,
                        raw_text=raw,
                        language=language,
                        is_relative=False,
                    )
                )
        else:
            level, mod, names, asnames = _parse_python_from_text(raw_stripped)
            if not names:
                continue
            imports.append(
                RawImport(
                    module=mod,
                    names=names,
                    asnames=asnames,
                    level=level,
                    lineno=lineno,
                    raw_text=raw,
                    language=language,
                    is_relative=bool(level > 0),
                )
            )
    # If query produced no captures due to syntax error nodes, fallback to line regex on remaining text?
    # But error isolation says we should still return imports outside errors; our captures already cover those.
    return imports


def _extract_js_tree(captures, source_bytes: bytes, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    # Collect import_statement and call_expression nodes captured as imp
    # Also collect spec nodes for quick linking
    import_nodes: list[object] = []
    spec_nodes: list[object] = []
    for node, cap_name in captures:
        if "spec" in cap_name:
            spec_nodes.append(node)
        if "imp" in cap_name or "import" in cap_name:
            # node may be import_statement or call_expression
            if node.type in ("import_statement", "call_expression"):  # type: ignore
                import_nodes.append(node)
    # Deduplicate import nodes
    seen: set[tuple[int, int]] = set()
    uniq_imports: list[object] = []
    for n in import_nodes:
        key = (n.start_byte, n.end_byte)  # type: ignore
        if key not in seen:
            seen.add(key)
            uniq_imports.append(n)
    for node in uniq_imports:
        raw = _decode_node_text(node, source_bytes)  # type: ignore
        raw_stripped = raw.strip()
        lineno = int(node.start_point[0] + 1)  # type: ignore
        if node.type == "import_statement":  # type: ignore
            spec = _extract_js_specifier(raw_stripped)
            if spec is None:
                # Try to find descendant spec node inside this import
                spec = None
                for sn in spec_nodes:
                    # Check if spec inside import node
                    if sn.start_byte >= node.start_byte and sn.end_byte <= node.end_byte:  # type: ignore
                        spec_text = _decode_node_text(sn, source_bytes)  # type: ignore
                        spec = _strip_quotes(spec_text)
                        break
                if spec is None:
                    continue
            is_rel = spec.startswith(".")
            names, asnames = _extract_js_import_names(raw_stripped)
            if not names:
                names = [spec]
                asnames = [None]
            imports.append(
                RawImport(
                    module=spec,
                    names=names,
                    asnames=asnames,
                    level=0,
                    lineno=lineno,
                    raw_text=raw,
                    language=language,
                    is_relative=is_rel,
                )
            )
        elif node.type == "call_expression":  # type: ignore
            # Likely require(...) — extract spec from arguments string
            # raw like 'require("../lib")' or 'require ("../lib")'
            m = re.search(r'require\s*\(\s*["\'`]([^"\'`]+)["\'`]\s*\)', raw_stripped)
            if not m:
                # Try spec_nodes inside this call
                spec = None
                for sn in spec_nodes:
                    if sn.start_byte >= node.start_byte and sn.end_byte <= node.end_byte:  # type: ignore
                        spec_text = _decode_node_text(sn, source_bytes)  # type: ignore
                        spec = _strip_quotes(spec_text)
                        break
                if spec is None:
                    continue
                # spec derived from child
                raw_text = raw  # keep full raw
                is_rel = spec.startswith(".")
                imports.append(
                    RawImport(
                        module=spec,
                        names=[spec],
                        asnames=[None],
                        level=0,
                        lineno=lineno,
                        raw_text=raw_text,
                        language=language,
                        is_relative=is_rel,
                    )
                )
                continue
            spec = m.group(1)
            is_rel = spec.startswith(".")
            imports.append(
                RawImport(
                    module=spec,
                    names=[spec],
                    asnames=[None],
                    level=0,
                    lineno=lineno,
                    raw_text=raw,
                    language=language,
                    is_relative=is_rel,
                )
            )
    # If captures missed some due to grammar differences, fallback regex for remaining?
    # Ensure we also cover side-effect imports that may have been missed: run fallback on full source and deduplicate
    # We'll merge fallback results not already covered by tree results to improve coverage
    # But to avoid duplicate, only add if fallback not overlapping known import nodes positions
    # Simpler: if tree extraction returned 0 but fallback would return something, union them
    if not imports:
        # No tree imports found — fallback to regex on decoded source
        try:
            raw_text_full = source_bytes.decode("utf-8", errors="surrogateescape")
            fb = _fallback_js_parse(raw_text_full, language)
            imports.extend(fb)
        except Exception:
            pass
    return imports


def _extract_java_tree(captures, source_bytes: bytes, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    seen: set[tuple[int, int]] = set()
    for node, cap_name in captures:
        if node.type != "import_declaration":  # type: ignore
            continue
        key = (node.start_byte, node.end_byte)  # type: ignore
        if key in seen:
            continue
        seen.add(key)
        raw = _decode_node_text(node, source_bytes)  # type: ignore
        raw_stripped = raw.strip()
        lineno = int(node.start_point[0] + 1)  # type: ignore
        m = re.search(r'import\s+(?:static\s+)?([A-Za-z0-9_\.\*]+)\s*;', raw_stripped)
        if m:
            spec = m.group(1)
        else:
            # fallback: extract between import and ;
            spec = raw_stripped.replace("import", "").replace("static", "").replace(";", "").strip()
        spec_clean = spec
        if spec_clean.endswith(".*"):
            spec_clean = spec_clean[:-2]
        imports.append(
            RawImport(
                module=spec_clean,
                names=[spec_clean],
                asnames=[None],
                level=0,
                lineno=lineno,
                raw_text=raw,
                language=language,
                is_relative=False,
            )
        )
    if not imports:
        try:
            raw_text_full = source_bytes.decode("utf-8", errors="surrogateescape")
            fb = _fallback_java_parse(raw_text_full, language)
            imports.extend(fb)
        except Exception:
            pass
    return imports


def _extract_go_tree(captures, source_bytes: bytes, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    seen: set[tuple[int, int]] = set()
    for node, cap_name in captures:
        if node.type != "import_declaration":  # type: ignore
            continue
        key = (node.start_byte, node.end_byte)  # type: ignore
        if key in seen:
            continue
        seen.add(key)
        raw = _decode_node_text(node, source_bytes)  # type: ignore
        lineno = int(node.start_point[0] + 1)  # type: ignore
        # raw may be block: import ("fmt" "os") or single: import "fmt"
        # Extract all quoted strings
        for m in re.finditer(r'"([^"]+)"', raw):
            spec = m.group(1)
            # Determine lineno for this spec: count newlines from start of node to spec
            # Find position of spec inside raw
            pre = raw[: m.start()]
            spec_lineno = lineno + pre.count("\n")
            raw_piece = m.group(0).strip()
            # Check for alias preceding? Handle 'alias "spec"' pattern: look backwards for alias
            # Use slice of raw before m.group(0) to check alias
            before = raw[: m.start()].rstrip()
            # Simple: if before ends with identifier not being 'import' or '(' then alias present, but we ignore alias for module resolution (use spec)
            imports.append(
                RawImport(
                    module=spec,
                    names=[spec],
                    asnames=[None],
                    level=0,
                    lineno=spec_lineno,
                    raw_text=raw_piece if len(re.findall(r'"([^"]+)"', raw)) == 1 else raw_piece,
                    language=language,
                    is_relative=spec.startswith("."),
                )
            )
        # Handle backticks? Go also allows raw string import?
        # Fallback if no quoted strings but import_declaration has spec without quotes? rare
        if not re.search(r'"([^"]+)"', raw):
            # Try to fallback parse
            try:
                raw_text_full = source_bytes.decode("utf-8", errors="surrogateescape")
                fb = _fallback_go_parse(raw_text_full, language)
                imports.extend(fb)
            except Exception:
                pass
    if not imports:
        try:
            raw_text_full = source_bytes.decode("utf-8", errors="surrogateescape")
            fb = _fallback_go_parse(raw_text_full, language)
            imports.extend(fb)
        except Exception:
            pass
    return imports


def _extract_rust_tree(captures, source_bytes: bytes, language: str) -> list[RawImport]:
    imports: list[RawImport] = []
    seen: set[tuple[int, int]] = set()
    for node, cap_name in captures:
        if node.type != "use_declaration":  # type: ignore
            continue
        key = (node.start_byte, node.end_byte)  # type: ignore
        if key in seen:
            continue
        seen.add(key)
        raw = _decode_node_text(node, source_bytes)  # type: ignore
        raw_stripped = raw.strip()
        lineno = int(node.start_point[0] + 1)  # type: ignore
        m = re.search(r'use\s+([^;]+);', raw_stripped)
        if m:
            spec = m.group(1).strip()
            if " as " in spec:
                spec = spec.split(" as ")[0].strip()
            # Remove braces?  use std::{collections, io}; -> spec includes braces, simplified to prefix
            # For now keep full spec trimmed
            spec = spec.strip("{} ").strip()
            imports.append(
                RawImport(
                    module=spec,
                    names=[spec],
                    asnames=[None],
                    level=0,
                    lineno=lineno,
                    raw_text=raw,
                    language=language,
                    is_relative=spec.startswith("crate") or spec.startswith("super") or spec.startswith("self"),
                )
            )
    if not imports:
        try:
            raw_text_full = source_bytes.decode("utf-8", errors="surrogateescape")
            fb = _fallback_rust_parse(raw_text_full, language)
            imports.extend(fb)
        except Exception:
            pass
    return imports


def _extract_tree_sitter_imports(root_node, source_bytes: bytes, language: str) -> list[RawImport]:
    """Dispatch per-language extraction using tree-sitter query captures."""
    # Get query string for language
    try:
        from .language import get_language as _get_lang, get_query as _get_q  # type: ignore
    except Exception:
        return _fallback_parse(source_bytes.decode("utf-8", errors="surrogateescape"), language)

    lang_obj = _get_lang(language) if Language is not None else None
    query_str = _get_q(language) if language else None
    if lang_obj is None or not query_str:
        # fallback to regex
        try:
            raw_text_full = source_bytes.decode("utf-8", errors="surrogateescape")
            return _fallback_parse(raw_text_full, language)
        except Exception:
            return []

    # Build Query
    try:
        query = Query(lang_obj, query_str)  # type: ignore
    except Exception:
        # query syntax maybe unsupported in this tree-sitter version; fallback
        try:
            raw_text_full = source_bytes.decode("utf-8", errors="surrogateescape")
            return _fallback_parse(raw_text_full, language)
        except Exception:
            return []

    captures = _get_captures(query, root_node)
    if not captures:
        # Still fallback? Could be that query captured nothing due to syntax error elsewhere but still have ERROR nodes outside
        # Try fallback in addition to ensure not missing
        try:
            raw_text_full = source_bytes.decode("utf-8", errors="surrogateescape")
            fb = _fallback_parse(raw_text_full, language)
            # If fallback finds imports, return them (error isolation: if tree has ERROR, fallback regex may still find valid imports)
            if fb:
                # If captures empty but fb non-empty, return fb
                return fb
        except Exception:
            pass
        return []

    if language == "python":
        return _extract_python_tree(captures, source_bytes, language)
    if language in ("javascript", "typescript"):
        return _extract_js_tree(captures, source_bytes, language)
    if language == "java":
        return _extract_java_tree(captures, source_bytes, language)
    if language == "go":
        return _extract_go_tree(captures, source_bytes, language)
    if language == "rust":
        return _extract_rust_tree(captures, source_bytes, language)
    # Generic fallback for unknown but query matched
    # Try to decode raw per capture
    imports: list[RawImport] = []
    seen: set[tuple[int, int]] = set()
    for node, cap_name in captures:
        if "imp" not in cap_name and "import" not in cap_name:
            continue
        key = (node.start_byte, node.end_byte)  # type: ignore
        if key in seen:
            continue
        seen.add(key)
        raw = _decode_node_text(node, source_bytes)  # type: ignore
        lineno = int(node.start_point[0] + 1)  # type: ignore
        imports.append(
            RawImport(
                module=raw.strip(),
                names=[raw.strip()],
                asnames=[None],
                level=0,
                lineno=lineno,
                raw_text=raw,
                language=language,
                is_relative=False,
            )
        )
    return imports


# ---------------------------------------------------------------------------
# Backward compat ast helper (still provided)
# ---------------------------------------------------------------------------

def _extract_via_ast(tree: ast.AST, language: str = "python") -> list[RawImport]:
    imports: list[RawImport] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            lineno = int(getattr(node, "lineno", 1))
            for alias in node.names:
                imports.append(
                    RawImport(
                        module=alias.name,
                        names=[alias.name],
                        asnames=[alias.asname],
                        level=0,
                        lineno=lineno,
                        raw_text=f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""),
                        language=language,
                        is_relative=False,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            module: str | None = node.module
            level: int = int(getattr(node, "level", 0))
            lineno = int(getattr(node, "lineno", 1))
            names: list[str] = [a.name for a in node.names]
            asnames: list[str | None] = [a.asname for a in node.names]
            if not names:
                continue
            # Build raw_text approximation
            dots = "." * level
            if module is None:
                raw_text = f"from {dots} import {', '.join(names)}"
            else:
                raw_text = f"from {dots}{module} import {', '.join(names)}"
            imports.append(
                RawImport(
                    module=module,
                    names=names,
                    asnames=asnames,
                    level=level,
                    lineno=lineno,
                    raw_text=raw_text,
                    language=language,
                    is_relative=bool(level > 0),
                )
            )
    return imports


def _extract_raw_imports(
    tree_or_root,
    language: str = "python",
    source_bytes: bytes | None = None,
    **kwargs,
) -> list[RawImport]:
    """Backward compat helper — tree-sitter based but supports ast.

    Handles:
      - ``ast.AST`` → via _extract_via_ast
      - tree-sitter root node + source_bytes + language → via _extract_tree_sitter_imports
      - raw string text + language → fallback regex
    """
    # ast case
    if isinstance(tree_or_root, ast.AST):
        lang = kwargs.get("language", language)
        return _extract_via_ast(tree_or_root, lang)
    # tree-sitter node case (has type, start_byte, etc.)
    if hasattr(tree_or_root, "type") and hasattr(tree_or_root, "start_byte"):
        lang = kwargs.get("language", language)
        src = source_bytes
        if src is None:
            # try to get from kwargs
            src = kwargs.get("source_bytes") or kwargs.get("source")
            if isinstance(src, str):
                src = src.encode("utf-8", errors="surrogateescape")
        if src is None:
            return []
        return _extract_tree_sitter_imports(tree_or_root, src, lang)
    # string case
    if isinstance(tree_or_root, (str, bytes)):
        txt = tree_or_root.decode("utf-8", errors="surrogateescape") if isinstance(tree_or_root, bytes) else tree_or_root  # type: ignore
        lang = kwargs.get("language", language)
        return _fallback_parse(txt, lang)
    return []


# ---------------------------------------------------------------------------
# Main entry: parse_file
# ---------------------------------------------------------------------------

def parse_file(path: Path, language: str | None = None) -> ParseResult | ParseFailure:
    """Parse a file via tree-sitter without executing it.

    - Reads file with ``encoding='utf-8-sig'`` and ``errors='surrogateescape'``
      (handles BOM and undecodable bytes).
    - Detects language from extension if not provided via EXTENSION_MAP.
    - Uses ``tree_sitter.Parser`` + ``Language`` + ``Query`` to extract imports.
    - Tree-sitter ``ERROR`` nodes do not abort; imports outside errors are still returned.
    - On success returns :class:`ParseResult` with ``raw_imports`` and ``raw_text``.
    - On ``OSError`` / ``UnicodeError`` / unsupported language / tree-sitter exception
      returns :class:`ParseFailure` with ``path``, ``reason``, optional ``lineno``,
      ``raw_text`` when available, and ``language`` field.

    No code is executed — pure parsing.
    """
    p = Path(path)

    # --- Language detection ---
    lang = language
    if lang is None:
        try:
            from .language import detect_language  # type: ignore

            lang = detect_language(p)
        except Exception:
            # fallback to suffix map
            suffix = p.suffix.lower()
            try:
                from .language import EXTENSION_MAP as EM  # type: ignore

                lang = EM.get(suffix)
            except Exception:
                lang = None
    # If unknown language
    if lang is None:
        # Unsupported language — return ParseFailure with reason
        # Keep language as None for diagnostic, but include suffix in reason
        suffix = p.suffix.lower() if hasattr(p, "suffix") else "unknown"
        reason = f"unsupported_language: no mapping for extension '{suffix}' (path={p.name})"
        return ParseFailure(path=p, reason=reason, lineno=None, raw_text=None, language=None)

    # Normalize language alias
    # ts -> typescript, js -> javascript etc handled via map but keep as is
    # Ensure language string lower
    lang = str(lang).lower().strip()
    if lang == "js":
        lang = "javascript"
    elif lang in ("ts",):
        lang = "typescript"

    # --- Read file with utf-8-sig + surrogateescape ---
    try:
        raw_text = p.read_text(encoding="utf-8-sig", errors="surrogateescape")
    except (OSError, UnicodeError) as e:
        reason = f"{type(e).__name__}: {e}"
        return ParseFailure(path=p, reason=reason, lineno=None, raw_text=None, language=lang)
    except Exception as e:  # pragma: no cover - safety
        reason = f"{type(e).__name__}: {e}"
        return ParseFailure(path=p, reason=reason, lineno=None, raw_text=None, language=lang)

    # Prepare source bytes for tree-sitter
    try:
        source_bytes = raw_text.encode("utf-8", errors="surrogateescape")
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        return ParseFailure(path=p, reason=reason, lineno=None, raw_text=raw_text, language=lang)

    # Empty file case?
    # If raw_text is empty, return empty result directly (no tree needed)
    if not raw_text.strip():
        return ParseResult(path=p, raw_imports=[], raw_text=raw_text, language=lang)

    # --- Tree-sitter parsing (with error isolation) ---
    # Attempt tree-sitter if available
    tree = None
    root_node = None
    used_tree_sitter = False
    if Parser is not None and Language is not None and Query is not None:  # type: ignore
        try:
            from .language import get_language as _get_lang  # type: ignore

            lang_obj = _get_lang(lang)
            if lang_obj is not None:
                # Build parser
                parser = None
                try:
                    parser = Parser(lang_obj)  # type: ignore
                except Exception:
                    try:
                        parser = Parser()  # type: ignore
                        parser.language = lang_obj  # type: ignore
                    except Exception:
                        parser = None
                if parser is not None:
                    try:
                        tree = parser.parse(source_bytes)  # type: ignore
                        if tree is not None:
                            root_node = tree.root_node  # type: ignore
                            used_tree_sitter = True
                            # Check for syntax errors via has_error / has_error property
                            try:
                                has_err = False
                                # tree_sitter Tree and Node both have has_error
                                if hasattr(tree, "has_error") and bool(getattr(tree, "has_error")):
                                    has_err = True
                                elif hasattr(root_node, "has_error") and bool(getattr(root_node, "has_error")):
                                    has_err = True
                                elif hasattr(root_node, "has_errored") and bool(getattr(root_node, "has_errored")):
                                    has_err = True
                                else:
                                    # Fallback: check type == "ERROR" somewhere in tree
                                    # Quick scan via string representation of S-expression
                                    try:
                                        sexpr = str(root_node.sexp() if hasattr(root_node, "sexp") else "")  # type: ignore
                                        if "ERROR" in sexpr:
                                            has_err = True
                                    except Exception:
                                        pass
                                if has_err:
                                    # Still try to detect if file is essentially import-only with partial error
                                    # For spec compliance, mark as ParseFailure so diagnostics counts it.
                                    # Provide reason with lineno of first ERROR if possible
                                    lineno_err: int | None = None
                                    try:
                                        # Walk to find first ERROR node
                                        stack = [root_node]
                                        while stack:
                                            n = stack.pop()  # type: ignore
                                            if getattr(n, "type", None) == "ERROR":
                                                lineno_err = int(getattr(n, "start_point", (0, 0))[0] + 1)  # type: ignore
                                                break
                                            try:
                                                for child in getattr(n, "children", []):  # type: ignore
                                                    stack.append(child)
                                            except Exception:
                                                break
                                    except Exception:
                                        lineno_err = None
                                    reason = "SyntaxError: tree-sitter parse error"
                                    if lineno_err is not None:
                                        reason = f"SyntaxError: tree-sitter parse error at line {lineno_err}"
                                    return ParseFailure(path=p, reason=reason, lineno=lineno_err, raw_text=raw_text, language=lang)
                            except Exception:
                                pass
                    except Exception as e:
                        # Tree-sitter parse exception -> treat as ParseFailure with language
                        reason = f"{type(e).__name__}: {e}"
                        return ParseFailure(path=p, reason=reason, lineno=None, raw_text=raw_text, language=lang)
        except Exception:
            # Any language lookup failure -> fallback
            pass

    raw_imports: list[RawImport] = []
    if used_tree_sitter and root_node is not None:
        try:
            raw_imports = _extract_tree_sitter_imports(root_node, source_bytes, lang)
        except Exception as e:
            # Catch exceptions per file -> ParseFailure with language, but still isolate?
            # For error isolation, if extraction fails partially, fallback to regex + still return partial?
            # Return ParseFailure wrapping raw_text
            reason = f"{type(e).__name__}: {e}"
            return ParseFailure(path=p, reason=reason, lineno=None, raw_text=raw_text, language=lang)
    else:
        # Fallback path when tree-sitter unavailable or language not supported by tree-sitter
        try:
            raw_imports = _fallback_parse(raw_text, lang)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            return ParseFailure(path=p, reason=reason, lineno=None, raw_text=raw_text, language=lang)

    # Success — error isolation: even if root contains ERROR, we already extracted outside
    return ParseResult(path=p, raw_imports=raw_imports, raw_text=raw_text, language=lang)


def _extract_raw_text(raw: RawImport) -> str:
    """Human-readable raw import text for diagnostics (service compatibility).

    Handles both plural (names/asnames) and singular (name/asname) forms.
    """
    # Prefer stored raw_text if available
    try:
        txt = getattr(raw, "raw_text", "")
        if txt:
            return txt
    except Exception:
        pass
    names = getattr(raw, "names", None)
    asnames = getattr(raw, "asnames", None)
    if not names:
        singular = getattr(raw, "name", None)
        if singular:
            names = [singular]
        else:
            if raw.module:
                names = [raw.module]
            else:
                names = []
    if asnames is None:
        asname_sing = getattr(raw, "asname", None)
        if asname_sing is not None or "asname" in getattr(raw, "__dict__", {}):
            asnames = [asname_sing]
        else:
            asnames = [None] * len(names) if names else []
    if len(asnames) < len(names):
        asnames = list(asnames) + [None] * (len(names) - len(asnames))
    elif len(asnames) > len(names):
        asnames = asnames[: len(names)]

    level = int(getattr(raw, "level", 0) or 0)
    module = getattr(raw, "module", None)

    if level == 0:
        if module is not None and len(names) == 1 and names[0] == module:
            base = f"import {module}"
            alias = asnames[0] if asnames else None
            if alias:
                base += f" as {alias}"
            return base
        if module is not None:
            alias_part = ""
            if names and asnames and asnames[0] and asnames[0] != names[0]:
                alias_part = f" as {asnames[0]}"
            name_part = names[0] if names else ""
            return f"from {module} import {name_part}{alias_part}"
        if names:
            base = f"import {names[0]}"
            if asnames and asnames[0]:
                base += f" as {asnames[0]}"
            return base
        return "import <unknown>"

    dots = "." * level
    if module is None:
        name = names[0] if names else ""
        alias = asnames[0] if asnames else None
        part = f" as {alias}" if alias and alias != name else ""
        return f"from {dots} import {name}{part}"
    name = names[0] if names else ""
    alias = asnames[0] if asnames else None
    alias_part = f" as {alias}" if alias and alias != name else ""
    return f"from {dots}{module} import {name}{alias_part}"


__all__ = ["_extract_raw_imports", "_extract_raw_text", "parse_file"]

