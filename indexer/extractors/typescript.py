"""TypeScript/JavaScript code graph extractor.

Uses tree-sitter for structural extraction (functions, classes, methods,
call edges) and regex for import path resolution (ES modules, CommonJS,
@/ aliases). Falls back to pure regex if tree-sitter is unavailable.
"""

import os
import re
from typing import Tuple

from indexer.extractors import Edge, Node

try:
    from indexer.extractors.typescript_ts import extract_typescript_ts

    _HAS_TS_EXTRACTOR = True
except ImportError:
    _HAS_TS_EXTRACTOR = False

# --- Regex patterns ---

# import { X, Y } from 'path'  /  import X from 'path'  /  import * as X from 'path'
RE_IMPORT = re.compile(
    r"""^import\s+"""
    r"""(?:"""
    r"""(?:\{[^}]*\})|"""  # { named }
    r"""(?:\*\s+as\s+\w+)|"""  # * as namespace
    r"""(?:\w+)"""  # default
    r""")"""
    r"""\s+from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# require('path') or require("path")
RE_REQUIRE = re.compile(
    r"""require\(\s*['"]([^'"]+)['"]\s*\)""",
)

# export function name(
RE_EXPORT_FUNCTION = re.compile(
    r"""^export\s+(?:async\s+)?function\s+(\w+)""",
    re.MULTILINE,
)

# export class name
RE_EXPORT_CLASS = re.compile(
    r"""^export\s+class\s+(\w+)""",
    re.MULTILINE,
)

# export default X  (captures the identifier after 'default')
RE_EXPORT_DEFAULT = re.compile(
    r"""^export\s+default\s+(?:function\s+|class\s+)?(\w+)""",
    re.MULTILINE,
)

# export { A, B, C }
RE_EXPORT_LIST = re.compile(
    r"""^export\s+\{([^}]+)\}""",
    re.MULTILINE,
)

# Top-level function (non-exported) — for export default matching
RE_FUNCTION_DECL = re.compile(
    r"""^(?:async\s+)?function\s+(\w+)""",
    re.MULTILINE,
)

# Extension candidates for resolving bare relative imports
_EXTENSIONS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]


def _resolve_relative(import_path: str, source_dir: str, project_root: str) -> str:
    """Resolve a relative import to an actual file path.

    Tries: exact path, path + extensions, path/index + extensions.
    Returns the resolved absolute path, or the original import_path if unresolved.
    """
    base = os.path.normpath(os.path.join(source_dir, import_path))

    # 1. Exact match (already has extension)
    if os.path.isfile(base):
        return base

    # 2. Try adding extensions
    for ext in _EXTENSIONS:
        candidate = base + ext
        if os.path.isfile(candidate):
            return candidate

    # 3. Try as directory with index file
    if os.path.isdir(base):
        for ext in _EXTENSIONS:
            candidate = os.path.join(base, "index" + ext)
            if os.path.isfile(candidate):
                return candidate

    # Unresolved — return normalized path as-is
    return base


def extract_typescript(file_path: str, project_root: str) -> Tuple[list, list]:
    """Extract nodes and edges from a TypeScript/JavaScript file.

    Tree-sitter handles structural extraction (functions, classes, calls).
    Regex handles import path resolution (@/ aliases, relative paths).
    Results are merged with deduplication.
    """
    if not os.path.isfile(file_path):
        return [], []

    filename = os.path.basename(file_path)
    source_dir = os.path.dirname(file_path)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return [], []

    rel_path = os.path.relpath(file_path, project_root)

    import_edges = _extract_import_edges(content, filename, source_dir, project_root)

    if _HAS_TS_EXTRACTOR:
        ts_result = extract_typescript_ts(content, rel_path, project_root)
        if ts_result is not None:
            ts_nodes, ts_edges = ts_result
            return _merge_results(ts_nodes, ts_edges, import_edges)

    return _extract_regex_only(content, filename, rel_path, import_edges)


def _extract_import_edges(content, filename, source_dir, project_root):
    """Extract import edges using regex (path resolution)."""
    edges = []

    for match in RE_IMPORT.finditer(content):
        module_path = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        target = _resolve_import(module_path, source_dir, project_root)
        edges.append(
            Edge(
                source=filename,
                target=target,
                relation="imports",
                confidence=1.0,
                source_line=line_no,
            )
        )

    for match in RE_REQUIRE.finditer(content):
        module_path = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        if any(e.source_line == line_no and e.relation == "imports" for e in edges):
            continue
        target = _resolve_import(module_path, source_dir, project_root)
        edges.append(
            Edge(
                source=filename,
                target=target,
                relation="imports",
                confidence=1.0,
                source_line=line_no,
            )
        )

    return edges


def _merge_results(ts_nodes, ts_edges, import_edges):
    """Merge tree-sitter nodes/edges with regex import edges, dedup by key."""
    all_edges = list(ts_edges)
    seen = {(e.source, e.target, e.relation) for e in all_edges}
    for e in import_edges:
        key = (e.source, e.target, e.relation)
        if key not in seen:
            all_edges.append(e)
            seen.add(key)
    return ts_nodes, all_edges


def _extract_regex_only(content, filename, rel_path, import_edges):
    """Pure regex fallback when tree-sitter is unavailable."""
    nodes: list[Node] = []
    edges: list[Edge] = list(import_edges)

    nodes.append(Node(name=filename, file=rel_path, type="file", line=1))

    for match in RE_EXPORT_FUNCTION.finditer(content):
        name = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        nodes.append(Node(name=name, file=rel_path, type="function", line=line_no))

    for match in RE_EXPORT_CLASS.finditer(content):
        name = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        nodes.append(Node(name=name, file=rel_path, type="class", line=line_no))

    for match in RE_EXPORT_DEFAULT.finditer(content):
        name = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        existing_names = {n.name for n in nodes if n.type in ("function", "class")}
        if name not in existing_names:
            is_func = any(
                m.group(1) == name for m in RE_FUNCTION_DECL.finditer(content)
            )
            node_type = "function" if is_func else "field"
            nodes.append(Node(name=name, file=rel_path, type=node_type, line=line_no))

    for match in RE_EXPORT_LIST.finditer(content):
        names_str = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        for raw_name in names_str.split(","):
            name = raw_name.strip().split(" as ")[0].strip()
            if name and name.isidentifier():
                existing_names = {n.name for n in nodes if n.type != "file"}
                if name not in existing_names:
                    nodes.append(
                        Node(name=name, file=rel_path, type="field", line=line_no)
                    )

    return nodes, edges


def _resolve_import(module_path: str, source_dir: str, project_root: str) -> str:
    """Resolve an import path to a target string.

    Relative paths (starting with . or ..) are resolved to relative file paths.
    @/ aliases (Next.js convention, maps to project root) are resolved similarly.
    Package imports are returned as-is (the package name).
    """
    if module_path.startswith("."):
        resolved = _resolve_relative(module_path, source_dir, project_root)
        return os.path.relpath(resolved, project_root)
    if module_path.startswith("@/"):
        resolved = _resolve_relative("./" + module_path[2:], project_root, project_root)
        return os.path.relpath(resolved, project_root)
    return module_path
