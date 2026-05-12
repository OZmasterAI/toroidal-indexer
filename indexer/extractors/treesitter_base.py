"""Tree-sitter base utilities for structural code graph extraction.

Provides ts_parse(), ts_extract_functions(), ts_extract_classes(), and
ts_extract_calls() for use by language-specific tree-sitter extractors.
Uses tree_sitter_languages (0.21.x API) — NOT the newer 0.23+ API.
"""

from indexer.extractors import Edge, Node

try:
    from tree_sitter_languages import get_parser
except ImportError:
    get_parser = None

LANG_CONFIG = {
    "python": {
        "func": "function_definition",
        "class": "class_definition",
        "call": "call",
    },
    "typescript": {
        "func": "function_declaration",
        "class": "class_declaration",
        "call": "call_expression",
        "method": "method_definition",
        "arrow": "arrow_function",
    },
    "javascript": {
        "func": "function_declaration",
        "class": "class_declaration",
        "call": "call_expression",
        "method": "method_definition",
        "arrow": "arrow_function",
    },
    "rust": {
        "func": "function_item",
        "class": "struct_item",
        "call": "call_expression",
        "enum": "enum_item",
        "impl": "impl_item",
    },
    "go": {
        "func": "function_declaration",
        "class": "type_declaration",
        "call": "call_expression",
        "method": "method_declaration",
    },
}

_PARSER_CACHE = {}


def ts_parse(source, language):
    """Parse source into a tree-sitter Tree. Returns None on failure."""
    if get_parser is None:
        return None
    try:
        if language not in _PARSER_CACHE:
            _PARSER_CACHE[language] = get_parser(language)
        parser = _PARSER_CACHE[language]
        src_bytes = source.encode("utf-8") if isinstance(source, str) else source
        return parser.parse(src_bytes)
    except Exception:
        return None


def ts_extract_functions(source, language, rel_path=""):
    """Extract function nodes from source code."""
    tree = ts_parse(source, language)
    if tree is None:
        return []

    config = LANG_CONFIG.get(language, {})
    func_type = config.get("func")
    if not func_type:
        return []

    nodes = []
    _walk_functions(tree.root_node, func_type, nodes, rel_path)
    return nodes


def ts_extract_classes(source, language, rel_path=""):
    """Extract class/struct/type nodes from source code."""
    tree = ts_parse(source, language)
    if tree is None:
        return []

    config = LANG_CONFIG.get(language, {})
    class_type = config.get("class")
    if not class_type:
        return []

    nodes = []
    _walk_classes(tree.root_node, class_type, nodes, rel_path)
    return nodes


def ts_extract_calls(source, language, rel_path=""):
    """Extract call edges from source code."""
    tree = ts_parse(source, language)
    if tree is None:
        return []

    config = LANG_CONFIG.get(language, {})
    call_type = config.get("call")
    func_type = config.get("func")
    if not call_type:
        return []

    scope_map = build_scope_map(tree.root_node, func_type)
    edges = []
    _walk_calls(tree.root_node, call_type, scope_map, edges, rel_path)
    return edges


def build_scope_map(root, func_type):
    """Build a map of (start_row, end_row) -> function_name for scope resolution."""
    scopes = {}
    if not func_type:
        return scopes

    def walk(node):
        if node.type == func_type:
            name = _get_identifier(node)
            if name:
                scopes[(node.start_point[0], node.end_point[0])] = name
        for child in node.children:
            walk(child)

    walk(root)
    return scopes


def find_scope(line, scope_map):
    """Find the innermost enclosing function name for a given 0-indexed line."""
    best = None
    best_size = float("inf")
    for (start, end), name in scope_map.items():
        if start <= line <= end:
            size = end - start
            if size < best_size:
                best = name
                best_size = size
    return best


def resolve_callee_names(node):
    """Extract callee name(s) from a call_expression node.

    For chained calls like a.b().c(), returns ["b", "c"] by walking
    nested call_expression and member_expression nodes.
    """
    names = []
    if not node.children:
        return names

    callee = node.children[0]

    if callee.type == "identifier":
        names.append(callee.text.decode("utf-8"))
    elif callee.type in ("member_expression", "field_expression"):
        for child in callee.children:
            if child.type in ("property_identifier", "field_identifier"):
                names.append(child.text.decode("utf-8"))
            elif child.type in ("call_expression",):
                names.extend(resolve_callee_names(child))
    elif callee.type == "selector_expression":
        for child in callee.children:
            if child.type == "field_identifier":
                names.append(child.text.decode("utf-8"))
            elif child.type == "call_expression":
                names.extend(resolve_callee_names(child))

    return names


def _get_identifier(node):
    """Get the first identifier/type_identifier/field_identifier child text."""
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "field_identifier"):
            return child.text.decode("utf-8")
    return None


def _walk_functions(node, func_type, results, rel_path):
    if node.type == func_type:
        name = _get_identifier(node)
        if name:
            results.append(
                Node(
                    name=name,
                    file=rel_path,
                    type="function",
                    line=node.start_point[0] + 1,
                )
            )
    for child in node.children:
        _walk_functions(child, func_type, results, rel_path)


def _walk_classes(node, class_type, results, rel_path):
    if node.type == class_type:
        name = _get_identifier(node)
        if name:
            results.append(
                Node(
                    name=name,
                    file=rel_path,
                    type="class",
                    line=node.start_point[0] + 1,
                )
            )
    for child in node.children:
        _walk_classes(child, class_type, results, rel_path)


def _walk_calls(node, call_type, scope_map, edges, rel_path):
    if node.type == call_type:
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
            if child.type in ("arguments", "argument_list"):
                _walk_calls(child, call_type, scope_map, edges, rel_path)
        return

    for child in node.children:
        _walk_calls(child, call_type, scope_map, edges, rel_path)
