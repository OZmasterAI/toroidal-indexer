"""Tree-sitter Go extractor.

Extracts structural nodes (functions, types), method declarations
(with receiver -> "implements" edges), and call expressions (including
chained calls). Import path resolution via go.mod is left to the regex
extractor in go.py.
"""

from indexer.extractors import Edge, Node
from indexer.extractors.treesitter_base import (
    build_scope_map,
    find_scope,
    ts_parse,
)


def extract_go_ts(source, rel_path, project_root):
    """Extract nodes and edges from Go source using tree-sitter.

    Returns (list[Node], list[Edge]) or None if parsing fails.
    """
    tree = ts_parse(source, "go")
    if tree is None:
        return None

    nodes = []
    edges = []

    nodes.append(Node(name=rel_path, file=rel_path, type="file", line=1))

    func_scopes = {}
    _walk_tree(tree.root_node, rel_path, nodes, edges, func_scopes)

    scope_map = build_scope_map(tree.root_node, "function_declaration")
    _merge_method_scopes(func_scopes, scope_map)
    _extract_calls(tree.root_node, rel_path, edges, scope_map)

    return nodes, edges


def _walk_tree(node, rel_path, nodes, edges, func_scopes):
    if node.type == "function_declaration":
        name = _get_child_text(node, "identifier")
        if name:
            nodes.append(
                Node(
                    name=name,
                    file=rel_path,
                    type="function",
                    line=node.start_point[0] + 1,
                )
            )
            func_scopes[(node.start_point[0], node.end_point[0])] = name

    elif node.type == "method_declaration":
        _extract_method(node, rel_path, nodes, edges, func_scopes)

    elif node.type == "type_declaration":
        _extract_type(node, rel_path, nodes)

    for child in node.children:
        _walk_tree(child, rel_path, nodes, edges, func_scopes)


def _extract_method(node, rel_path, nodes, edges, func_scopes):
    method_name = _get_child_text(node, "field_identifier")
    if not method_name:
        return

    nodes.append(
        Node(
            name=method_name,
            file=rel_path,
            type="function",
            line=node.start_point[0] + 1,
        )
    )
    func_scopes[(node.start_point[0], node.end_point[0])] = method_name

    receiver_type = _get_receiver_type(node)
    if receiver_type:
        edges.append(
            Edge(
                source=method_name,
                target=receiver_type,
                relation="implements",
                confidence=1.0,
                source_line=node.start_point[0] + 1,
            )
        )


def _get_receiver_type(node):
    """Extract the receiver type name from a method_declaration."""
    for child in node.children:
        if child.type == "parameter_list":
            for param in child.children:
                if param.type == "parameter_declaration":
                    for pchild in param.children:
                        if pchild.type == "type_identifier":
                            return pchild.text.decode("utf-8")
                        if pchild.type == "pointer_type":
                            for pt_child in pchild.children:
                                if pt_child.type == "type_identifier":
                                    return pt_child.text.decode("utf-8")
            break
    return None


def _extract_type(node, rel_path, nodes):
    for child in node.children:
        if child.type == "type_spec":
            name = _get_child_text(child, "type_identifier")
            if name:
                nodes.append(
                    Node(
                        name=name,
                        file=rel_path,
                        type="class",
                        line=node.start_point[0] + 1,
                    )
                )


def _merge_method_scopes(func_scopes, scope_map):
    scope_map.update(func_scopes)


def _extract_calls(node, rel_path, edges, scope_map):
    if node.type == "call_expression":
        line = node.start_point[0]
        scope = find_scope(line, scope_map)
        source = scope if scope else rel_path

        callees = _resolve_go_callee(node)
        for callee in callees:
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
            if child.type == "argument_list":
                _extract_calls(child, rel_path, edges, scope_map)
        return

    for child in node.children:
        _extract_calls(child, rel_path, edges, scope_map)


def _resolve_go_callee(node):
    """Resolve callee names from a Go call_expression.

    Go uses selector_expression (not member_expression) for pkg.Func().
    """
    names = []
    if not node.children:
        return names

    callee = node.children[0]

    if callee.type == "identifier":
        names.append(callee.text.decode("utf-8"))
    elif callee.type == "selector_expression":
        for child in callee.children:
            if child.type == "field_identifier":
                names.append(child.text.decode("utf-8"))
            elif child.type == "call_expression":
                names.extend(_resolve_go_callee(child))
    elif callee.type == "call_expression":
        names.extend(_resolve_go_callee(callee))

    return names


def _get_child_text(node, child_type):
    for child in node.children:
        if child.type == child_type:
            return child.text.decode("utf-8")
    return None
