"""Python AST extractor for Toroidal-Indexer code graph.

Extracts nodes (files, functions, classes) and edges (imports, calls,
reads, writes, implements) from a Python source file using the ast module.
"""

import ast
import os
from typing import Optional

from indexer.extractors import Edge, Node

# Patterns that indicate a file path string argument
_FILE_OPENERS = frozenset({"open", "Path"})


def extract_python(file_path: str, project_root: str) -> tuple[list[Node], list[Edge]]:
    """Extract code graph nodes and edges from a Python file.

    Args:
        file_path: Absolute path to the .py file.
        project_root: Absolute path to the project root (for module resolution).

    Returns:
        (nodes, edges) where nodes are definitions and edges are relationships.
    """
    if not os.path.isfile(file_path):
        return [], []

    rel_path = os.path.relpath(file_path, project_root)

    try:
        with open(file_path) as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return [Node(name=rel_path, file=rel_path, type="file", line=0)], []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return [Node(name=rel_path, file=rel_path, type="file", line=0)], []

    nodes: list[Node] = [Node(name=rel_path, file=rel_path, type="file", line=0)]
    edges: list[Edge] = []

    visitor = _ScopeVisitor(rel_path, file_path, project_root, nodes, edges)
    visitor.visit(tree)

    return nodes, edges


# ---------------------------------------------------------------------------
# Scope-tracking AST visitor
# ---------------------------------------------------------------------------


class _ScopeVisitor(ast.NodeVisitor):
    """Walk AST tracking the enclosing function for edge sources."""

    def __init__(self, rel_path, file_path, project_root, nodes, edges):
        self.rel_path = rel_path
        self.file_path = file_path
        self.project_root = project_root
        self.nodes = nodes
        self.edges = edges
        self.import_resolved: dict[str, str] = {}
        self._scope_stack: list[str] = []

    @property
    def _source(self) -> str:
        return self._scope_stack[-1] if self._scope_stack else self.rel_path

    def visit_Import(self, node):
        _handle_import(
            node, self.rel_path, self.project_root, self.import_resolved, self.edges
        )
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        _handle_import_from(
            node,
            self.rel_path,
            self.file_path,
            self.project_root,
            self.import_resolved,
            self.edges,
        )
        self.generic_visit(node)

    def _visit_func(self, node):
        self.nodes.append(
            Node(name=node.name, file=self.rel_path, type="function", line=node.lineno)
        )
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_func(node)

    def visit_ClassDef(self, node):
        _handle_class(node, self.rel_path, self.nodes, self.edges)
        self.generic_visit(node)

    def visit_Call(self, node):
        _handle_call(node, self._source, self.import_resolved, self.edges)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        _handle_subscript(node, self._source, self.edges)
        self.generic_visit(node)

    def visit_Assign(self, node):
        _handle_assign(node, self._source, self.edges)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Import handling
# ---------------------------------------------------------------------------


def _handle_import(
    node: ast.Import,
    rel_path: str,
    project_root: str,
    import_resolved: dict,
    edges: list[Edge],
) -> None:
    for alias in node.names:
        module = alias.name
        resolved = _resolve_module(module, project_root)
        target = resolved if resolved else module
        if resolved:
            import_resolved[alias.asname or alias.name] = resolved
        edges.append(
            Edge(
                source=rel_path,
                target=target,
                relation="imports",
                confidence=1.0,
                source_line=node.lineno,
            )
        )


def _handle_import_from(
    node: ast.ImportFrom,
    rel_path: str,
    file_path: str,
    project_root: str,
    import_resolved: dict,
    edges: list[Edge],
) -> None:
    module = node.module or ""
    level = node.level or 0

    if level > 0:
        anchor = os.path.dirname(file_path)
        for _ in range(level - 1):
            anchor = os.path.dirname(anchor)
        if module:
            resolved = _resolve_module(module, project_root, anchor_dir=anchor)
        else:
            resolved = None
    else:
        resolved = _resolve_module(module, project_root)

    target = resolved if resolved else module

    if node.names and resolved:
        for alias in node.names:
            import_resolved[alias.asname or alias.name] = resolved

    edges.append(
        Edge(
            source=rel_path,
            target=target,
            relation="imports",
            confidence=1.0,
            source_line=node.lineno,
        )
    )


# ---------------------------------------------------------------------------
# Class handling
# ---------------------------------------------------------------------------


def _handle_class(
    node: ast.ClassDef,
    rel_path: str,
    nodes: list[Node],
    edges: list[Edge],
) -> None:
    nodes.append(Node(name=node.name, file=rel_path, type="class", line=node.lineno))
    for base in node.bases:
        base_name = _name_from_node(base)
        if base_name:
            edges.append(
                Edge(
                    source=node.name,
                    target=base_name,
                    relation="implements",
                    confidence=1.0,
                    source_line=node.lineno,
                )
            )


# ---------------------------------------------------------------------------
# Call handling
# ---------------------------------------------------------------------------


def _handle_call(
    node: ast.Call,
    source: str,
    import_resolved: dict,
    edges: list[Edge],
) -> None:
    func_name = _name_from_node(node.func)
    if not func_name:
        return

    short_name = func_name.split(".")[-1]
    if short_name in _FILE_OPENERS and node.args:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            path_val = arg.value
            if "/" in path_val:
                edges.append(
                    Edge(
                        source=source,
                        target=path_val,
                        relation="reads",
                        confidence=1.0,
                        source_line=node.lineno,
                    )
                )

    if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            var_name = _name_from_node(node.func.value)
            if var_name:
                field = f"{var_name}.{arg.value}"
                edges.append(
                    Edge(
                        source=source,
                        target=field,
                        relation="reads",
                        confidence=1.0,
                        source_line=node.lineno,
                    )
                )

    resolved_target = func_name
    base_name = func_name.split(".")[0]
    if base_name in import_resolved:
        resolved_target = f"{import_resolved[base_name]}:{func_name}"

    edges.append(
        Edge(
            source=source,
            target=resolved_target,
            relation="calls",
            confidence=1.0,
            source_line=node.lineno,
        )
    )


# ---------------------------------------------------------------------------
# Subscript / field access handling
# ---------------------------------------------------------------------------


def _handle_subscript(
    node: ast.Subscript,
    source: str,
    edges: list[Edge],
) -> None:
    """Detect state['key'] reads (not inside Assign targets)."""
    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        var_name = _name_from_node(node.value)
        if var_name:
            field = f"{var_name}.{node.slice.value}"
            edges.append(
                Edge(
                    source=source,
                    target=field,
                    relation="reads",
                    confidence=1.0,
                    source_line=node.lineno,
                )
            )


def _handle_assign(
    node: ast.Assign,
    source: str,
    edges: list[Edge],
) -> None:
    """Detect state['key'] = ... writes."""
    for target in node.targets:
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and isinstance(target.slice.value, str)
        ):
            var_name = _name_from_node(target.value)
            if var_name:
                field = f"{var_name}.{target.slice.value}"
                edges.append(
                    Edge(
                        source=source,
                        target=field,
                        relation="writes",
                        confidence=1.0,
                        source_line=node.lineno,
                    )
                )


# ---------------------------------------------------------------------------
# Module resolver
# ---------------------------------------------------------------------------


def _resolve_module(
    module_path: str,
    project_root: str,
    anchor_dir: Optional[str] = None,
) -> Optional[str]:
    """Resolve a dotted module path to a relative file path within the project.

    Args:
        module_path: Dotted module name (e.g. "shared.state").
        project_root: Absolute path to project root.
        anchor_dir: For relative imports, the anchor directory.

    Returns:
        Relative path (e.g. "shared/state.py") or None if not in project.
    """
    parts = module_path.split(".")
    base = anchor_dir or project_root

    # Try as file: base/a/b/c.py
    as_file = os.path.join(base, *parts) + ".py"
    if os.path.isfile(as_file):
        return os.path.relpath(as_file, project_root)

    # Try as package: base/a/b/c/__init__.py
    as_pkg = os.path.join(base, *parts, "__init__.py")
    if os.path.isfile(as_pkg):
        return os.path.relpath(as_pkg, project_root)

    return None


# ---------------------------------------------------------------------------
# AST name helpers
# ---------------------------------------------------------------------------


def _name_from_node(node: ast.expr) -> Optional[str]:
    """Extract a dotted name string from an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value_name = _name_from_node(node.value)
        if value_name:
            return f"{value_name}.{node.attr}"
        return node.attr
    return None
