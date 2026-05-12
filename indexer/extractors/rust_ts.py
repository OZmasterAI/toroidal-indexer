"""Tree-sitter Rust extractor.

Extracts structural nodes (functions, structs, enums), impl blocks
(including trait impls -> "implements" edges), and call expressions
(including chained calls). Use/mod path resolution is left to the
regex extractor in rust.py.
"""

from indexer.extractors import Edge, Node
from indexer.extractors.treesitter_base import (
    build_scope_map,
    find_scope,
    resolve_callee_names,
    ts_parse,
)


def extract_rust_ts(source, rel_path, project_root):
    """Extract nodes and edges from Rust source using tree-sitter.

    Returns (list[Node], list[Edge]) or None if parsing fails.
    """
    tree = ts_parse(source, "rust")
    if tree is None:
        return None

    nodes = []
    edges = []

    nodes.append(Node(name=rel_path, file=rel_path, type="file", line=1))

    func_scopes = {}
    _walk_tree(tree.root_node, rel_path, nodes, edges, func_scopes)

    scope_map = build_scope_map(tree.root_node, "function_item")
    scope_map.update(func_scopes)
    _extract_calls(tree.root_node, rel_path, edges, scope_map)

    return nodes, edges


def _walk_tree(node, rel_path, nodes, edges, func_scopes):
    if node.type == "function_item":
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

    elif node.type == "struct_item":
        name = _get_child_text(node, "type_identifier")
        if name:
            nodes.append(
                Node(
                    name=name, file=rel_path, type="class", line=node.start_point[0] + 1
                )
            )

    elif node.type == "enum_item":
        name = _get_child_text(node, "type_identifier")
        if name:
            nodes.append(
                Node(
                    name=name, file=rel_path, type="class", line=node.start_point[0] + 1
                )
            )

    elif node.type == "impl_item":
        _extract_impl(node, rel_path, nodes, edges, func_scopes)
        return

    for child in node.children:
        _walk_tree(child, rel_path, nodes, edges, func_scopes)


def _extract_impl(node, rel_path, nodes, edges, func_scopes):
    type_ids = []
    has_for = False
    for child in node.children:
        if child.type == "type_identifier":
            type_ids.append(child.text.decode("utf-8"))
        elif child.type == "for":
            has_for = True

    if has_for and len(type_ids) >= 2:
        trait_name = type_ids[0]
        struct_name = type_ids[1]
        edges.append(
            Edge(
                source=struct_name,
                target=trait_name,
                relation="implements",
                confidence=1.0,
                source_line=node.start_point[0] + 1,
            )
        )

    for child in node.children:
        if child.type == "declaration_list":
            for member in child.children:
                if member.type == "function_item":
                    name = _get_child_text(member, "identifier")
                    if name:
                        nodes.append(
                            Node(
                                name=name,
                                file=rel_path,
                                type="function",
                                line=member.start_point[0] + 1,
                            )
                        )
                        func_scopes[(member.start_point[0], member.end_point[0])] = name


def _extract_calls(node, rel_path, edges, scope_map):
    if node.type == "call_expression":
        line = node.start_point[0]
        scope = find_scope(line, scope_map)
        source = scope if scope else rel_path

        callees = resolve_callee_names(node)
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
            if child.type == "arguments":
                _extract_calls(child, rel_path, edges, scope_map)
        return

    if node.type == "macro_invocation":
        return

    for child in node.children:
        _extract_calls(child, rel_path, edges, scope_map)


def _get_child_text(node, child_type):
    for child in node.children:
        if child.type == child_type:
            return child.text.decode("utf-8")
    return None
