"""Go code graph extractor.

Uses tree-sitter for structural extraction (functions, types, methods,
call edges) and regex for import path resolution (go.mod module paths).
Falls back to pure regex if tree-sitter is unavailable.
"""

import os
import re

from indexer.extractors import Edge, Node

try:
    from indexer.extractors.go_ts import extract_go_ts

    _HAS_TS_EXTRACTOR = True
except ImportError:
    _HAS_TS_EXTRACTOR = False

# --- Regex patterns ---

# Grouped import block: import ( ... )
RE_IMPORT_BLOCK = re.compile(
    r"^import\s*\((.*?)\)",
    re.MULTILINE | re.DOTALL,
)

# Single-line import: import "path" or import alias "path"
RE_IMPORT_SINGLE = re.compile(
    r'^import\s+(?:\w+\s+)?"([^"]+)"',
    re.MULTILINE,
)

# Individual import line inside a block (with optional alias)
RE_IMPORT_LINE = re.compile(
    r'^\s*(?:\w+\s+)?"([^"]+)"',
    re.MULTILINE,
)

# Type definitions: type Foo struct { or type Foo interface {
RE_TYPE = re.compile(
    r"^\s*type\s+(\w+)\s+(?:struct|interface)\s*\{",
    re.MULTILINE,
)

# Function with receiver: func (x Type) Name( or func (x *Type) Name(
RE_METHOD = re.compile(
    r"^\s*func\s+\(\s*\w+\s+\*?(\w+)\s*\)\s+(\w+)\s*\(",
    re.MULTILINE,
)

# Top-level function (no receiver): func Name(
RE_FUNC = re.compile(
    r"^\s*func\s+(\w+)\s*\(",
    re.MULTILINE,
)


def extract_go(file_path: str, project_root: str) -> tuple[list[Node], list[Edge]]:
    """Extract code graph nodes and edges from a Go source file.

    Tree-sitter handles structural extraction (functions, types, calls).
    Regex handles import path resolution via go.mod. Results are merged.
    """
    if not os.path.isfile(file_path):
        return [], []

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return [], []

    rel_path = os.path.relpath(file_path, project_root)

    if not source.strip():
        return [Node(name=rel_path, file=rel_path, type="file", line=1)], []

    import_edges = _extract_import_edges(source, rel_path, project_root)

    if _HAS_TS_EXTRACTOR:
        ts_result = extract_go_ts(source, rel_path, project_root)
        if ts_result is not None:
            ts_nodes, ts_edges = ts_result
            return _merge_results(ts_nodes, ts_edges, import_edges)

    return _extract_regex_only(source, rel_path, import_edges)


def _extract_import_edges(source, rel_path, project_root):
    """Extract import edges using regex (go.mod path resolution)."""
    edges = []
    module_path = _read_module_path(project_root)

    line_starts = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            line_starts.append(i + 1)

    def _lineno(pos):
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    for m in RE_IMPORT_BLOCK.finditer(source):
        block = m.group(1)
        block_start = m.start(1)
        for line_m in RE_IMPORT_LINE.finditer(block):
            import_path = line_m.group(1)
            pos = block_start + line_m.start()
            lineno = _lineno(pos)
            target = _resolve_import(import_path, module_path, project_root)
            edges.append(
                Edge(
                    source=rel_path,
                    target=target,
                    relation="imports",
                    confidence=1.0,
                    source_line=lineno,
                )
            )

    for m in RE_IMPORT_SINGLE.finditer(source):
        import_path = m.group(1)
        lineno = _lineno(m.start())
        target = _resolve_import(import_path, module_path, project_root)
        edges.append(
            Edge(
                source=rel_path,
                target=target,
                relation="imports",
                confidence=1.0,
                source_line=lineno,
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


def _extract_regex_only(source, rel_path, import_edges):
    """Pure regex fallback when tree-sitter is unavailable."""
    nodes: list[Node] = [Node(name=rel_path, file=rel_path, type="file", line=1)]
    edges: list[Edge] = list(import_edges)

    line_starts = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            line_starts.append(i + 1)

    def _lineno(pos):
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    for m in RE_TYPE.finditer(source):
        name = m.group(1)
        lineno = _lineno(m.start())
        nodes.append(Node(name=name, file=rel_path, type="class", line=lineno))

    for m in RE_METHOD.finditer(source):
        receiver_type = m.group(1)
        method_name = m.group(2)
        lineno = _lineno(m.start())
        nodes.append(
            Node(name=method_name, file=rel_path, type="function", line=lineno)
        )
        edges.append(
            Edge(
                source=method_name,
                target=receiver_type,
                relation="implements",
                confidence=1.0,
                source_line=lineno,
            )
        )

    for m in RE_FUNC.finditer(source):
        func_name = m.group(1)
        lineno = _lineno(m.start())
        if not any(n.name == func_name and n.line == lineno for n in nodes):
            nodes.append(
                Node(name=func_name, file=rel_path, type="function", line=lineno)
            )

    return nodes, edges


def _read_module_path(project_root: str) -> str | None:
    """Read the module path from go.mod in project_root."""
    go_mod = os.path.join(project_root, "go.mod")
    if not os.path.isfile(go_mod):
        return None
    try:
        with open(go_mod, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("module "):
                    return line[7:].strip()
    except OSError:
        pass
    return None


def _resolve_import(
    import_path: str, module_path: str | None, project_root: str
) -> str:
    """Resolve a Go import path to a target string.

    Internal imports (matching module_path prefix) are resolved to relative
    directory paths. External/stdlib imports are returned as-is.
    """
    if module_path and import_path.startswith(module_path + "/"):
        relative = import_path[len(module_path) + 1 :]
        candidate = os.path.join(project_root, relative)
        if os.path.isdir(candidate):
            return relative
        return relative
    return import_path
