"""Tree-sitter TypeScript/JavaScript/TSX/JSX extractor.

Extracts structural nodes (functions, arrow functions, classes, methods)
and call edges (including chained calls like a.b().c()). Import path
resolution is left to the regex extractor in typescript.py.
"""

from indexer.extractors import Edge, Node
from indexer.extractors.treesitter_base import (
    build_scope_map,
    find_scope,
    resolve_callee_names,
    ts_parse,
)


def extract_typescript_ts(source, rel_path, project_root):
    """Extract nodes and edges from TypeScript/JavaScript source using tree-sitter.

    Returns (list[Node], list[Edge]) or None if tree-sitter parsing fails.
    """
    lang = "typescript"
    tree = ts_parse(source, lang)
    if tree is None:
        return None

    nodes = []
    edges = []

    filename = rel_path.rsplit("/", 1)[-1] if "/" in rel_path else rel_path
    nodes.append(Node(name=filename, file=rel_path, type="file", line=1))

    func_scopes = {}
    _walk_tree(tree.root_node, rel_path, nodes, edges, func_scopes)

    scope_map = build_scope_map(tree.root_node, "function_declaration")
    _merge_arrow_scopes(func_scopes, scope_map)
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

    elif node.type == "class_declaration":
        name = _get_child_text(node, "type_identifier")
        if name:
            nodes.append(
                Node(
                    name=name, file=rel_path, type="class", line=node.start_point[0] + 1
                )
            )
        _extract_methods(node, rel_path, nodes, func_scopes)

    elif node.type == "variable_declarator":
        _check_arrow_function(node, rel_path, nodes, func_scopes)

    elif node.type == "method_definition":
        name = _get_child_text(node, "property_identifier")
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

    elif node.type == "export_statement":
        for child in node.children:
            _walk_tree(child, rel_path, nodes, edges, func_scopes)
        return

    for child in node.children:
        _walk_tree(child, rel_path, nodes, edges, func_scopes)


def _extract_methods(class_node, rel_path, nodes, func_scopes):
    for child in class_node.children:
        if child.type == "class_body":
            for member in child.children:
                if member.type == "method_definition":
                    name = _get_child_text(member, "property_identifier")
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


def _check_arrow_function(node, rel_path, nodes, func_scopes):
    name = _get_child_text(node, "identifier")
    if not name:
        return
    for child in node.children:
        if child.type == "arrow_function":
            nodes.append(
                Node(
                    name=name,
                    file=rel_path,
                    type="function",
                    line=node.start_point[0] + 1,
                )
            )
            func_scopes[(child.start_point[0], child.end_point[0])] = name
            return


def _merge_arrow_scopes(func_scopes, scope_map):
    scope_map.update(func_scopes)


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

    for child in node.children:
        _extract_calls(child, rel_path, edges, scope_map)


def _get_child_text(node, child_type):
    for child in node.children:
        if child.type == child_type:
            return child.text.decode("utf-8")
    return None
