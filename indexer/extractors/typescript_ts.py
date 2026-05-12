"""Tree-sitter TypeScript/JavaScript/TSX/JSX extractor.

Extracts structural nodes (functions, arrow functions, classes, methods)
and call edges. Import map enables cross-file call resolution; member
expression calls are only emitted when the object is a known symbol.
"""

from indexer.extractors import Edge, Node
from indexer.extractors.treesitter_base import (
    build_scope_map,
    find_scope,
    ts_parse,
)


def extract_typescript_ts(source, rel_path, project_root, import_map=None):
    """Extract nodes and edges from TypeScript/JavaScript source using tree-sitter.

    Returns (list[Node], list[Edge]) or None if tree-sitter parsing fails.
    import_map: dict of local_name -> (resolved_file, exported_name) for
    resolving cross-file call targets.
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
    _extract_calls(tree.root_node, rel_path, edges, scope_map, import_map)

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


def _extract_class_method(member, rel_path, nodes, func_scopes):
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


def _extract_methods(class_node, rel_path, nodes, func_scopes):
    for child in class_node.children:
        if child.type == "class_body":
            for member in child.children:
                if member.type == "method_definition":
                    _extract_class_method(member, rel_path, nodes, func_scopes)


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


_JS_GLOBALS = frozenset(
    {
        "fetch",
        "parseInt",
        "parseFloat",
        "encodeURIComponent",
        "decodeURIComponent",
        "encodeURI",
        "decodeURI",
        "setTimeout",
        "setInterval",
        "clearTimeout",
        "clearInterval",
        "alert",
        "confirm",
        "prompt",
        "atob",
        "btoa",
        "isNaN",
        "isFinite",
        "String",
        "Number",
        "Boolean",
        "BigInt",
        "Symbol",
        "Array",
        "Object",
        "Date",
        "Error",
        "TypeError",
        "RangeError",
        "Promise",
        "RegExp",
        "Map",
        "Set",
        "WeakMap",
        "WeakSet",
        "Proxy",
        "Reflect",
        "URL",
        "URLSearchParams",
        "Headers",
        "Request",
        "Response",
        "FormData",
        "Blob",
        "File",
        "AbortController",
        "TextEncoder",
        "TextDecoder",
        "crypto",
        "structuredClone",
        "requestAnimationFrame",
        "queueMicrotask",
    }
)

_TEST_MATCHERS = frozenset(
    {
        "expect",
        "describe",
        "it",
        "test",
        "beforeEach",
        "afterEach",
        "beforeAll",
        "afterAll",
        "jest",
        "vi",
        "toBe",
        "toEqual",
        "toContain",
        "toBeDefined",
        "toBeUndefined",
        "toBeTruthy",
        "toBeFalsy",
        "toBeNull",
        "toBeNaN",
        "toBeGreaterThan",
        "toBeLessThan",
        "toHaveLength",
        "toHaveBeenCalled",
        "toHaveBeenCalledWith",
        "toHaveBeenCalledTimes",
        "toMatchObject",
        "toThrow",
        "toThrowError",
        "toHaveProperty",
        "toContainEqual",
        "toBeInstanceOf",
        "toMatch",
        "toStrictEqual",
        "toHaveReturned",
        "resolves",
        "rejects",
        "not",
    }
)

_SKIP_CALLS = _JS_GLOBALS | _TEST_MATCHERS


def _should_skip_call(name):
    if name in _SKIP_CALLS:
        return True
    return len(name) > 3 and name.startswith("set") and name[3].isupper()


def _is_known_symbol(name, import_map, scope_map):
    """Check if a name is an imported or locally-defined symbol."""
    if import_map and name in import_map:
        return True
    return bool(scope_map and any(v == name for v in scope_map.values()))


def _get_member_parts(member_node):
    """Extract (object_node, property_name) from a member_expression."""
    obj_node = None
    prop_name = None
    for child in member_node.children:
        if child.type == "identifier" and obj_node is None:
            obj_node = child
        elif child.type in ("property_identifier", "field_identifier"):
            prop_name = child.text.decode("utf-8")
    return obj_node, prop_name


def _resolve_chained_call(member_node, import_map, scope_map, prop_name):
    """Handle foo().bar() — emit bar if foo is a known symbol."""
    first_child = member_node.children[0] if member_node.children else None
    if not first_child or first_child.type != "call_expression":
        return None
    inner_callee = first_child.children[0] if first_child.children else None
    if inner_callee and inner_callee.type == "identifier":
        if _is_known_symbol(inner_callee.text.decode("utf-8"), import_map, scope_map):
            return prop_name, 0.8
    return None


def _resolve_member_call(member_node, import_map, scope_map):
    """Resolve obj.method() — returns (target, confidence) or None to skip.

    Only emits when the object is a known symbol (imported or in scope).
    Skips calls on unknown objects (parameters, builtins) to avoid orphans.
    """
    obj_node, prop_name = _get_member_parts(member_node)
    if not prop_name or _should_skip_call(prop_name):
        return None

    if obj_node:
        obj_name = obj_node.text.decode("utf-8")
        if import_map and obj_name in import_map:
            target_file, _ = import_map[obj_name]
            return f"{target_file}:{prop_name}", 1.0
        if _is_known_symbol(obj_name, None, scope_map):
            return prop_name, 0.9
        return None

    return _resolve_chained_call(member_node, import_map, scope_map, prop_name)


def _extract_calls(node, rel_path, edges, scope_map, import_map=None):
    if node.type == "call_expression":
        line = node.start_point[0]
        scope = find_scope(line, scope_map)
        source = scope if scope else rel_path

        callee_node = node.children[0] if node.children else None

        if callee_node and callee_node.type == "identifier":
            name = callee_node.text.decode("utf-8")
            if import_map and name in import_map:
                target_file, exported_name = import_map[name]
                target = f"{target_file}:{exported_name}"
            elif _should_skip_call(name):
                target = None
            else:
                target = name
            if target:
                edges.append(
                    Edge(
                        source=source,
                        target=target,
                        relation="calls",
                        confidence=1.0,
                        source_line=line + 1,
                    )
                )
        elif callee_node and callee_node.type in (
            "member_expression",
            "field_expression",
        ):
            result = _resolve_member_call(callee_node, import_map, scope_map)
            if result:
                target, confidence = result
                edges.append(
                    Edge(
                        source=source,
                        target=target,
                        relation="calls",
                        confidence=confidence,
                        source_line=line + 1,
                    )
                )

        for child in node.children:
            if child.type == "arguments":
                _extract_calls(child, rel_path, edges, scope_map, import_map)
        return

    for child in node.children:
        _extract_calls(child, rel_path, edges, scope_map, import_map)


def _get_child_text(node, child_type):
    for child in node.children:
        if child.type == child_type:
            return child.text.decode("utf-8")
    return None
