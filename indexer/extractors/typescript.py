"""TypeScript/JavaScript regex-based code graph extractor.

Extracts imports (ES modules + CommonJS), exports (functions, classes,
default, named lists) and resolves relative paths to actual files.
"""

import os
import re
from typing import Tuple

from indexer.extractors import Edge, Node

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

    Args:
        file_path: Absolute path to the .ts/.tsx/.js/.jsx file.
        project_root: Project root directory for resolving relative imports.

    Returns:
        (nodes, edges) where nodes are Node dataclasses and edges are Edge dataclasses.
    """
    nodes: list[Node] = []
    edges: list[Edge] = []

    if not os.path.isfile(file_path):
        return nodes, edges

    filename = os.path.basename(file_path)
    source_dir = os.path.dirname(file_path)

    # Read file content, handle binary/encoding errors
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return nodes, edges

    # Normalize to relative path (consistent with python/rust extractors)
    rel_path = os.path.relpath(file_path, project_root)

    # File-level node (always present for readable files)
    nodes.append(Node(name=filename, file=rel_path, type="file", line=1))

    # --- Extract imports (ES module style) ---
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

    # --- Extract require() calls ---
    for match in RE_REQUIRE.finditer(content):
        module_path = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        # Avoid duplicates if the same line was already captured by RE_IMPORT
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

    # --- Extract exported functions ---
    for match in RE_EXPORT_FUNCTION.finditer(content):
        name = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        nodes.append(Node(name=name, file=rel_path, type="function", line=line_no))

    # --- Extract exported classes ---
    for match in RE_EXPORT_CLASS.finditer(content):
        name = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        nodes.append(Node(name=name, file=rel_path, type="class", line=line_no))

    # --- Extract export default ---
    for match in RE_EXPORT_DEFAULT.finditer(content):
        name = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        # Only add if not already captured as an export function/class
        existing_names = {n.name for n in nodes if n.type in ("function", "class")}
        if name not in existing_names:
            # Check if it matches a function declaration in the file
            is_func = any(
                m.group(1) == name for m in RE_FUNCTION_DECL.finditer(content)
            )
            node_type = "function" if is_func else "field"
            nodes.append(Node(name=name, file=rel_path, type=node_type, line=line_no))

    # --- Extract export { A, B, C } lists ---
    for match in RE_EXPORT_LIST.finditer(content):
        names_str = match.group(1)
        line_no = content[: match.start()].count("\n") + 1
        for raw_name in names_str.split(","):
            # Handle 'X as Y' — use original name
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
