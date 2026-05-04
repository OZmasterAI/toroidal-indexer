"""LSP-to-graph bridge for Toroidal-Indexer Tier 2.

Converts LSP query results (definition, implementation, call hierarchy)
into SurrealDB RELATE edges with confidence 0.9.
"""

import logging
import os
from urllib.parse import unquote, urlparse

from indexer.schema import relate, upsert_node

logger = logging.getLogger(__name__)

LSP_CONFIDENCE = 0.9


def build_line_to_name_map(symbols: list[dict]) -> dict[int, str]:
    """Map each symbol's start line to its name.

    Handles both SymbolInformation format (location.range.start)
    and DocumentSymbol format (range.start / selectionRange.start).
    """
    mapping: dict[int, str] = {}
    for sym in symbols:
        name = sym.get("name")
        if not name:
            continue
        line = _get_symbol_start_line(sym)
        if line is not None:
            mapping[line] = name
    return mapping


def _get_symbol_start_line(sym: dict) -> int | None:
    if "location" in sym:
        return sym["location"]["range"]["start"]["line"]
    if "selectionRange" in sym:
        return sym["selectionRange"]["start"]["line"]
    if "range" in sym:
        return sym["range"]["start"]["line"]
    return None


def _uri_to_relpath(uri: str, project_root: str) -> str:
    parsed = urlparse(uri)
    abs_path = unquote(parsed.path)
    return os.path.relpath(abs_path, project_root)


def resolve_target_node(
    db,
    project: str,
    target_uri: str,
    target_line: int,
    target_symbols: list[dict],
    project_root: str,
):
    """Resolve an LSP target (uri + line) to a SurrealDB node.

    Uses the symbol list for the target file to find the name at the given line.
    Returns the node RecordID or None if not resolvable.
    """
    line_map = build_line_to_name_map(target_symbols)
    name = line_map.get(target_line)
    if name is None:
        closest = _find_closest_symbol(target_line, line_map)
        if closest is None:
            return None
        name = closest

    rel_path = _uri_to_relpath(target_uri, project_root)
    kind = _get_kind_for_name(name, target_symbols)
    node_type = _kind_to_type(kind)
    return upsert_node(db, project, rel_path, name, node_type, target_line)


def _find_closest_symbol(target_line: int, line_map: dict[int, str]) -> str | None:
    """Find the symbol whose start line is within 3 lines of target_line."""
    best_name = None
    best_dist = 4
    for line, name in line_map.items():
        dist = abs(line - target_line)
        if dist < best_dist:
            best_dist = dist
            best_name = name
    return best_name


def _get_kind_for_name(name: str, symbols: list[dict]) -> int:
    for sym in symbols:
        if sym.get("name") == name:
            return sym.get("kind", 12)
    return 12


def _kind_to_type(kind: int) -> str:
    if kind == 5:
        return "class"
    if kind in (12, 6):
        return "function"
    if kind == 13:
        return "field"
    return "function"


def store_definition_edges(
    db,
    project: str,
    file_path: str,
    definition_results: list[dict],
    target_symbols_cache: dict[str, list[dict]],
    project_root: str,
) -> int:
    """Store import edges from definition resolution results.

    Each result: {source_name, source_line, target_uri, target_line}
    """
    count = 0
    for defn in definition_results:
        source_name = defn["source_name"]
        source_line = defn["source_line"]
        target_uri = defn["target_uri"]
        target_line = defn["target_line"]

        target_symbols = target_symbols_cache.get(target_uri, [])
        if not target_symbols:
            continue

        src_node = upsert_node(db, project, file_path, source_name, "file", 0)
        tgt_node = resolve_target_node(
            db, project, target_uri, target_line, target_symbols, project_root
        )
        if tgt_node is None:
            continue

        relate(
            db,
            src_node,
            "imports",
            tgt_node,
            confidence=LSP_CONFIDENCE,
            source_line=source_line,
        )
        count += 1
    return count


def store_implementation_edges(
    db,
    project: str,
    impl_results: list[dict],
    target_symbols_cache: dict[str, list[dict]],
    project_root: str,
) -> int:
    """Store implements edges from implementation resolution results.

    Each result: {source_name, source_file, target_uri, target_line}
    """
    count = 0
    for impl in impl_results:
        source_name = impl["source_name"]
        source_file = impl["source_file"]
        target_uri = impl["target_uri"]
        target_line = impl["target_line"]

        target_symbols = target_symbols_cache.get(target_uri, [])
        if not target_symbols:
            continue

        src_node = upsert_node(db, project, source_file, source_name, "class", 0)
        tgt_node = resolve_target_node(
            db, project, target_uri, target_line, target_symbols, project_root
        )
        if tgt_node is None:
            continue

        relate(
            db,
            src_node,
            "implements",
            tgt_node,
            confidence=LSP_CONFIDENCE,
            source_line=0,
        )
        count += 1
    return count


def store_call_hierarchy_edges(
    db,
    project: str,
    call_results: list[dict],
) -> int:
    """Store calls edges from call hierarchy results.

    Each result: {caller_file, caller_name, callee_file, callee_name, source_line}
    """
    count = 0
    for call in call_results:
        caller_file = call["caller_file"]
        caller_name = call["caller_name"]
        callee_file = call["callee_file"]
        callee_name = call["callee_name"]
        source_line = call.get("source_line", 0)

        caller_node = upsert_node(db, project, caller_file, caller_name, "function", 0)
        callee_node = upsert_node(db, project, callee_file, callee_name, "function", 0)

        relate(
            db,
            caller_node,
            "calls",
            callee_node,
            confidence=LSP_CONFIDENCE,
            source_line=source_line,
        )
        count += 1
    return count


def enrich_node_types(db, project: str, file_path: str, hover_results: list[dict]):
    """Enrich nodes with type information from hover results.

    Each result: {name, line, type_info}
    """
    for hover in hover_results:
        name = hover["name"]
        type_info = hover.get("type_info", "")
        if type_info:
            nodes = db.query(
                "SELECT * FROM code_node WHERE project=$proj AND file=$file AND name=$name",
                {"proj": project, "file": file_path, "name": name},
            )
            if nodes:
                db.query(
                    "UPDATE $id SET type_info=$info",
                    {"id": nodes[0]["id"], "info": type_info},
                )
