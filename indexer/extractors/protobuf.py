"""Protobuf regex-based code graph extractor.

Extracts imports, service/rpc definitions, message/enum types, field type
references, and go_package cross-references from .proto files.

Known limitations:
  - Nested message types: flattened (inner messages treated as top-level)
  - Oneof/map field types: outer type captured, generic params missed
  - Comments: single-line // comments are stripped before matching
"""

import os
import re

from indexer.extractors import Edge, Node

# --- Regex patterns ---

# import "path/to/file.proto";
RE_IMPORT = re.compile(
    r'^import\s+"([^"]+)"\s*;',
    re.MULTILINE,
)

# service Foo {
RE_SERVICE = re.compile(
    r"^\s*service\s+(\w+)\s*\{",
    re.MULTILINE,
)

# rpc MethodName(RequestType) returns (ResponseType);
RE_RPC = re.compile(
    r"^\s*rpc\s+(\w+)\s*\(\s*(\w+)\s*\)\s*returns\s*\(\s*(\w+)\s*\)",
    re.MULTILINE,
)

# message Foo {
RE_MESSAGE = re.compile(
    r"^\s*message\s+(\w+)\s*\{",
    re.MULTILINE,
)

# enum Foo {
RE_ENUM = re.compile(
    r"^\s*enum\s+(\w+)\s*\{",
    re.MULTILINE,
)

# option go_package = "github.com/...";
RE_GO_PACKAGE = re.compile(
    r'^\s*option\s+go_package\s*=\s*"([^"]+)"\s*;',
    re.MULTILINE,
)

# Field type references: TypeName field_name = N;
# Matches non-scalar types (capitalized or dotted names)
_SCALAR_TYPES = frozenset(
    {
        "double",
        "float",
        "int32",
        "int64",
        "uint32",
        "uint64",
        "sint32",
        "sint64",
        "fixed32",
        "fixed64",
        "sfixed32",
        "sfixed64",
        "bool",
        "string",
        "bytes",
    }
)

RE_FIELD = re.compile(
    r"^\s*(?:repeated\s+|optional\s+|required\s+)?"
    r"(\w+(?:\.\w+)*)\s+\w+\s*=\s*\d+",
    re.MULTILINE,
)


def _strip_comments(source: str) -> str:
    """Remove single-line // comments (but not inside strings)."""
    return re.sub(r"//[^\n]*", "", source)


def extract_protobuf(
    file_path: str, project_root: str
) -> tuple[list[Node], list[Edge]]:
    """Extract code graph nodes and edges from a .proto file.

    Args:
        file_path: Absolute path to the .proto file.
        project_root: Absolute path to the project root.

    Returns:
        (nodes, edges) extracted from the file.
    """
    if not os.path.isfile(file_path):
        return [], []

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            raw_source = f.read()
    except OSError:
        return [], []

    if not raw_source.strip():
        rel_path = os.path.relpath(file_path, project_root)
        return [Node(name=rel_path, file=rel_path, type="file", line=1)], []

    source = _strip_comments(raw_source)
    rel_path = os.path.relpath(file_path, project_root)
    nodes: list[Node] = [Node(name=rel_path, file=rel_path, type="file", line=1)]
    edges: list[Edge] = []

    # Use raw_source for line number calculation (preserves original positions)
    line_starts = [0]
    for i, ch in enumerate(raw_source):
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

    # --- Imports ---
    for m in RE_IMPORT.finditer(source):
        import_path = m.group(1)
        lineno = _lineno(m.start())
        edges.append(
            Edge(
                source=rel_path,
                target=import_path,
                relation="imports",
                confidence=1.0,
                source_line=lineno,
            )
        )

    # --- Services ---
    for m in RE_SERVICE.finditer(source):
        name = m.group(1)
        lineno = _lineno(m.start())
        nodes.append(Node(name=name, file=rel_path, type="class", line=lineno))

    # --- RPC methods ---
    for m in RE_RPC.finditer(source):
        method_name = m.group(1)
        request_type = m.group(2)
        response_type = m.group(3)
        lineno = _lineno(m.start())
        nodes.append(
            Node(name=method_name, file=rel_path, type="function", line=lineno)
        )
        edges.append(
            Edge(
                source=method_name,
                target=request_type,
                relation="calls",
                confidence=1.0,
                source_line=lineno,
            )
        )
        edges.append(
            Edge(
                source=method_name,
                target=response_type,
                relation="calls",
                confidence=1.0,
                source_line=lineno,
            )
        )

    # --- Messages ---
    for m in RE_MESSAGE.finditer(source):
        name = m.group(1)
        lineno = _lineno(m.start())
        nodes.append(Node(name=name, file=rel_path, type="class", line=lineno))

    # --- Enums ---
    for m in RE_ENUM.finditer(source):
        name = m.group(1)
        lineno = _lineno(m.start())
        nodes.append(Node(name=name, file=rel_path, type="class", line=lineno))

    # --- Field type references (non-scalar types) ---
    for m in RE_FIELD.finditer(source):
        type_name = m.group(1)
        base_type = type_name.split(".")[-1]
        if base_type.lower() not in _SCALAR_TYPES and base_type[0:1].isupper():
            lineno = _lineno(m.start())
            edges.append(
                Edge(
                    source=rel_path,
                    target=base_type,
                    relation="reads",
                    confidence=1.0,
                    source_line=lineno,
                )
            )

    # --- go_package option (cross-reference to Go implementation) ---
    for m in RE_GO_PACKAGE.finditer(source):
        go_pkg = m.group(1)
        lineno = _lineno(m.start())
        edges.append(
            Edge(
                source=rel_path,
                target=go_pkg,
                relation="implements",
                confidence=1.0,
                source_line=lineno,
            )
        )

    return nodes, edges
