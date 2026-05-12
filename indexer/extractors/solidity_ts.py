"""Tree-sitter Solidity extractor.

Extracts contracts, public/external functions, events, modifiers,
public state variables, inheritance edges, and internal call edges
from Solidity source files.

Uses a locally built grammar (build/solidity.so) compatible with
tree-sitter 0.21.x, since tree_sitter_languages does not include
Solidity.
"""

import os

from indexer.extractors import Edge, Node
from indexer.extractors.treesitter_base import (
    build_scope_map,
    find_scope,
)

_parser = None
_SO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "build",
    "solidity.so",
)

# Solidity built-ins that are not real function calls
_BUILTINS = frozenset({"require", "assert", "revert", "emit"})


def _get_parser():
    """Create and cache the Solidity tree-sitter parser."""
    global _parser
    if _parser is not None:
        return _parser
    try:
        from tree_sitter import Language, Parser

        if not os.path.isfile(_SO_PATH):
            return None
        lang = Language(_SO_PATH, "solidity")
        _parser = Parser()
        _parser.set_language(lang)
        return _parser
    except (ImportError, ValueError, OSError):
        return None


def extract_solidity_ts(source, rel_path, project_root):
    """Extract nodes and edges from Solidity source using tree-sitter.

    Returns (list[Node], list[Edge]) or None if parsing fails.
    """
    parser = _get_parser()
    if parser is None:
        return None

    src_bytes = source.encode("utf-8") if isinstance(source, str) else source
    try:
        tree = parser.parse(src_bytes)
    except (TypeError, ValueError):
        return None

    if tree is None or tree.root_node is None:
        return None

    nodes = [Node(name=rel_path, file=rel_path, type="file", line=1)]
    edges = []

    func_scopes = {}
    _walk_tree(tree.root_node, rel_path, nodes, edges, func_scopes)

    scope_map = build_scope_map(tree.root_node, "function_definition")
    scope_map.update(func_scopes)
    _extract_calls(tree.root_node, rel_path, edges, scope_map)

    return nodes, edges


def _walk_tree(node, rel_path, nodes, edges, func_scopes):
    """Walk the AST and extract nodes and edges."""
    handler = _NODE_HANDLERS.get(node.type)
    if handler is not None:
        handler(node, rel_path, nodes, edges, func_scopes)
        return

    for child in node.children:
        _walk_tree(child, rel_path, nodes, edges, func_scopes)


def _handle_contract(node, rel_path, nodes, edges, func_scopes):
    """Extract contract declaration and walk into body."""
    _extract_contract(node, rel_path, nodes, edges)
    for child in node.children:
        if child.type == "contract_body":
            for member in child.children:
                _walk_tree(member, rel_path, nodes, edges, func_scopes)


def _handle_function(node, rel_path, nodes, _edges, func_scopes):
    """Extract function if public/external."""
    name = _child_text(node, "identifier")
    if not name:
        return
    visibility = _get_visibility(node)
    if visibility in ("public", "external"):
        nodes.append(
            Node(
                name=name, file=rel_path, type="function", line=node.start_point[0] + 1
            )
        )
        func_scopes[(node.start_point[0], node.end_point[0])] = name


def _handle_event(node, rel_path, nodes, _edges, _scopes):
    """Extract event definition."""
    name = _child_text(node, "identifier")
    if name:
        nodes.append(
            Node(name=name, file=rel_path, type="event", line=node.start_point[0] + 1)
        )


def _handle_modifier(node, rel_path, nodes, _edges, _scopes):
    """Extract modifier definition."""
    name = _child_text(node, "identifier")
    if name:
        nodes.append(
            Node(
                name=name, file=rel_path, type="modifier", line=node.start_point[0] + 1
            )
        )


def _handle_state_var(node, rel_path, nodes, _edges, _scopes):
    """Extract public state variable (generates a getter)."""
    if _get_visibility(node) != "public":
        return
    name = _child_text(node, "identifier")
    if name:
        nodes.append(
            Node(
                name=name, file=rel_path, type="variable", line=node.start_point[0] + 1
            )
        )


_NODE_HANDLERS = {
    "contract_declaration": _handle_contract,
    "function_definition": _handle_function,
    "event_definition": _handle_event,
    "modifier_definition": _handle_modifier,
    "state_variable_declaration": _handle_state_var,
}


def _extract_contract(node, rel_path, nodes, edges):
    """Extract contract name and inheritance edges."""
    name = _child_text(node, "identifier")
    if not name:
        return

    nodes.append(
        Node(name=name, file=rel_path, type="class", line=node.start_point[0] + 1)
    )

    for child in node.children:
        if child.type == "inheritance_specifier":
            parent = _inheritance_name(child)
            if parent:
                edges.append(
                    Edge(
                        source=name,
                        target=parent,
                        relation="implements",
                        confidence=1.0,
                        source_line=node.start_point[0] + 1,
                    )
                )


def _extract_calls(node, rel_path, edges, scope_map):
    """Walk the AST and extract call edges."""
    if node.type == "call_expression":
        line = node.start_point[0]
        scope = find_scope(line, scope_map)
        source = scope if scope else rel_path
        callee = _resolve_callee(node)
        if callee:
            edges.append(
                Edge(
                    source=source,
                    target=callee,
                    relation="calls",
                    confidence=1.0,
                    source_line=line + 1,
                )
            )
        for child in node.children:
            if child.type == "call_argument":
                _extract_calls(child, rel_path, edges, scope_map)
        return

    for child in node.children:
        _extract_calls(child, rel_path, edges, scope_map)


def _resolve_callee(node):
    """Extract callee name from a call_expression.

    Simple call: identifier -> name.
    Member call: a.b -> "b" (method name).
    Skips built-ins (require, assert, revert, emit).
    """
    for child in node.children:
        if child.type != "expression":
            continue
        for sub in child.children:
            if sub.type == "identifier":
                name = sub.text.decode("utf-8")
                return None if name in _BUILTINS else name
            if sub.type == "member_expression":
                ids = [
                    c.text.decode("utf-8")
                    for c in sub.children
                    if c.type == "identifier"
                ]
                return ids[-1] if ids else None
    return None


def _get_visibility(node):
    """Get visibility string (public/external/internal/private)."""
    for child in node.children:
        if child.type == "visibility" and child.children:
            return child.children[0].type
    return None


def _inheritance_name(spec):
    """Get name from inheritance_specifier > user_defined_type > identifier."""
    for child in spec.children:
        if child.type == "user_defined_type":
            return _child_text(child, "identifier")
    return None


def _child_text(node, child_type):
    """Get text of the first child matching child_type."""
    for child in node.children:
        if child.type == child_type:
            return child.text.decode("utf-8")
    return None
