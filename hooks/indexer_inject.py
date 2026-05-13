#!/usr/bin/env python3
"""PreToolUse hook: inject structural context from Toroidal-Indexer code graph.

Handles two flows:
  Edit/Write — identifies function being edited, injects callers/readers.
  Grep/Bash(grep) — extracts search term, returns matching graph nodes
    so the instance gets exact file:line hits without scanning.
Always exits 0 (fail-open, never blocks).
"""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from typing import Any

warnings.filterwarnings("ignore", category=FutureWarning)

RAMDISK_DIR = f"/run/user/{os.getuid()}/claude-hooks"


def _query_rows(
    db: Any, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    result = db.query(sql, params) if params else db.query(sql)
    return result if isinstance(result, list) else []


def _detect_project():
    """Derive project name and root from env or cwd (matches indexer_commit.py logic)."""
    project_root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project_name = os.environ.get("INDEXER_PROJECT") or os.path.basename(project_root)
    return project_name, project_root


def _to_relative(file_path, project_root):
    """Convert absolute file_path to project-relative path. Returns None if outside root."""
    if not os.path.isabs(file_path):
        return file_path
    try:
        rel = os.path.relpath(file_path, project_root)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return rel


_EXT_TO_LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".sh": "bash",
    ".bash": "bash",
    ".proto": "proto",
}

_FUNC_NODE_TYPES = {
    "function_definition",
    "function_declaration",
    "function_item",
    "method_definition",
    "method_declaration",
    "arrow_function",
}

_NAME_CHILD_TYPES = {
    "identifier",
    "property_identifier",
    "field_identifier",
    "name",
    "word",
}


def _find_innermost_func(node, target_line):
    """Walk tree-sitter AST to find the innermost function containing target_line (0-indexed)."""
    best = None
    if node.type in _FUNC_NODE_TYPES:
        if node.start_point[0] <= target_line <= node.end_point[0]:
            best = node
    for child in node.children:
        candidate = _find_innermost_func(child, target_line)
        if candidate is not None:
            if best is None or candidate.start_point[0] >= best.start_point[0]:
                best = candidate
    return best


def _get_node_name(node):
    """Extract the function/method name from a tree-sitter node."""
    for child in node.children:
        if child.type in _NAME_CHILD_TYPES:
            return child.text.decode()
    return None


def _identify_function(file_path, old_string):
    """Parse source with tree-sitter to find which function/method contains old_string.

    Supports Python, TypeScript, TSX, JavaScript, Rust, Go, Bash.
    Returns the function name (str) or None.
    """
    ext = os.path.splitext(file_path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if not lang:
        return None
    try:
        from tree_sitter_languages import get_parser
    except ImportError:
        return None
    try:
        source = open(file_path, "rb").read()
    except (OSError, IOError):
        return None
    if old_string.encode() not in source:
        return None

    parser = get_parser(lang)
    tree = parser.parse(source)

    source_lines = source.split(b"\n")
    first_old_line = old_string.splitlines()[0].strip()
    if not first_old_line:
        return None

    target_line = None
    for i, line in enumerate(source_lines):
        if first_old_line.encode() in line:
            target_line = i
            break

    if target_line is None:
        return None

    func_node = _find_innermost_func(tree.root_node, target_line)
    return _get_node_name(func_node) if func_node else None


def _format_caller(c):
    """Format a single caller dict, prefixing with ~ if AI-sourced."""
    prefix = "~" if c.get("confidence", 1.0) < 1.0 else ""
    name = c.get("name", "?")
    fpath = os.path.basename(c.get("file", "?"))
    line = c.get("source_line") or c.get("line", 0)
    return f"{prefix}{name} ({fpath}:L{line})"


def _format_reader(r):
    """Format a single reader dict, prefixing with ~ if AI-sourced."""
    prefix = "~" if r.get("confidence", 1.0) < 1.0 else ""
    name = r.get("name", "?")
    fpath = os.path.basename(r.get("file", "?"))
    line = r.get("line", 0)
    return f"{prefix}{name} ({fpath}:L{line})"


_GREP_RE = re.compile(
    r"""(?:grep|rg|ag|ack)\s+(?:.*?\s)?(?:-[A-Za-z]*\s+)*['"]?([A-Za-z_][\w.\-]*(?:\|[A-Za-z_][\w.\-]*)*)['"]?""",
)


def _extract_search_term(event):
    """Extract a code-identifier search term from Grep or Bash(grep) tool input.

    Returns the term (str) or None if this isn't a code search.
    """
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})

    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        if pattern and re.match(r"^[A-Za-z_][\w.\-]*$", pattern):
            return pattern
        return None

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not any(g in cmd for g in ("grep", "rg ", "ag ", "ack ")):
            return None
        m = _GREP_RE.search(cmd)
        if m:
            term = m.group(1)
            if "|" in term:
                parts = [
                    p for p in term.split("|") if re.match(r"^[A-Za-z_][\w\-]*$", p)
                ]
                return parts[0] if parts else None
            if re.match(r"^[A-Za-z_][\w.\-]*$", term):
                return term
        return None

    return None


def _format_node(n):
    return f"{n.get('name', '?')} ({n.get('file', '?')}:L{n.get('line', 0)}, {n.get('type', '?')})"


def _query_term(term, session_id):
    """Look up a single identifier in the graph. Returns formatted string or None."""
    dedup_key = f"search:{term}"
    seen = _load_dedup(session_id)
    if dedup_key in seen:
        return None
    seen.add(dedup_key)

    from indexer.schema import connect_code_graph, get_callers

    db_name = os.environ.get("INDEXER_DB", "main")
    project, _ = _detect_project()

    db = connect_code_graph(database=db_name)

    nodes = (
        _query_rows(
            db,
            "SELECT * FROM code_node WHERE project=$proj AND name=$name",
            {"proj": project, "name": term},
        )
        if project
        else []
    )
    if not nodes:
        nodes = _query_rows(
            db,
            "SELECT * FROM code_node WHERE name=$name LIMIT 10",
            {"name": term},
        )
    if not nodes:
        _save_dedup(session_id, seen)
        return None

    parts = []
    for n in nodes[:10]:
        loc = _format_node(n)
        callers = get_callers(db, n["id"])
        if callers:
            caller_strs = [_format_caller(c) for c in callers[:5]]
            parts.append(f"{loc} ← called by: {', '.join(caller_strs)}")
        else:
            parts.append(loc)

    _save_dedup(session_id, seen)
    return (
        f"STOP: Graph already has '{term}'. Do NOT run grep — use these results directly:\n"
        + "\n".join(f"  • {p}" for p in parts)
    )


_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _camel_to_kebab(name):
    """Convert CamelCase/camelCase to kebab-case: TorusVisualization → torus-visualization."""
    return _CAMEL_RE.sub("-", name).lower()


def _handle_search(event):
    """Handle Grep/Bash search: look up term in graph, return context string or None."""
    term = _extract_search_term(event)
    if not term:
        return None
    session_id = os.environ.get("TORUS_SESSION_ID") or str(os.getppid())
    return _query_term(term, session_id)


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _extract_identifiers_from_text(text, max_terms=5):
    """Extract likely code identifiers from free text (agent prompts, etc.)."""
    stop = {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "are",
        "was",
        "not",
        "but",
        "have",
        "has",
        "its",
        "into",
        "also",
        "use",
        "how",
        "find",
        "look",
        "check",
        "where",
        "what",
        "which",
        "does",
        "can",
        "should",
        "would",
        "could",
        "need",
        "want",
        "get",
        "set",
        "all",
        "any",
        "each",
        "file",
        "code",
        "function",
        "class",
        "method",
        "agent",
        "explore",
        "search",
        "implementation",
        "setup",
        "logic",
        "create",
        "update",
        "delete",
        "read",
        "write",
        "run",
        "test",
    }
    words = _IDENT_RE.findall(text)
    seen = set()
    result = []
    for w in words:
        wl = w.lower()
        if wl in stop or wl in seen:
            continue
        if w[0].isupper() or "_" in w or any(c.isupper() for c in w[1:]):
            seen.add(wl)
            result.append(w)
            if len(result) >= max_terms:
                break
    if len(result) < max_terms:
        for w in words:
            wl = w.lower()
            if wl in stop or wl in seen:
                continue
            if len(w) >= 5:
                seen.add(wl)
                result.append(w)
                if len(result) >= max_terms:
                    break
    return result


def _handle_agent(event):
    """Handle Agent tool: extract identifiers from prompt, return graph hits."""
    tool_input = event.get("tool_input", {})
    prompt = tool_input.get("prompt", "")
    if not prompt:
        return None

    session_id = os.environ.get("TORUS_SESSION_ID") or str(os.getppid())
    dedup_key = f"agent:{hash(prompt) & 0xFFFFFFFF:08x}"
    seen = _load_dedup(session_id)
    if dedup_key in seen:
        return None
    seen.add(dedup_key)

    terms = _extract_identifiers_from_text(prompt)
    if not terms:
        _save_dedup(session_id, seen)
        return None

    from indexer.schema import connect_code_graph, get_callers

    db_name = os.environ.get("INDEXER_DB", "main")
    project, _ = _detect_project()
    db = connect_code_graph(database=db_name)

    all_parts = []
    for term in terms:
        tl = term.lower()
        stem = tl[: max(4, len(tl) // 2)] if len(tl) > 5 else tl
        candidates = [tl]
        if stem != tl:
            candidates.append(stem)
        short = tl[:4] if len(tl) > 4 else None
        if short and short not in candidates:
            candidates.append(short)
        kebab = _camel_to_kebab(term)
        if kebab != tl and kebab not in candidates:
            candidates.append(kebab)

        nodes = []
        for c in candidates:
            if nodes:
                break
            nodes = (
                _query_rows(
                    db,
                    "SELECT * FROM code_node WHERE project=$proj AND (string::lowercase(name) CONTAINS $term OR string::lowercase(file) CONTAINS $term) LIMIT 5",
                    {"proj": project, "term": c},
                )
                if project
                else _query_rows(
                    db,
                    "SELECT * FROM code_node WHERE (string::lowercase(name) CONTAINS $term OR string::lowercase(file) CONTAINS $term) LIMIT 5",
                    {"term": c},
                )
            )
        if not nodes:
            continue
        for n in nodes[:3]:
            loc = _format_node(n)
            callers = get_callers(db, n["id"])
            if callers:
                caller_strs = [_format_caller(c) for c in callers[:3]]
                all_parts.append(f"{loc} ← {', '.join(caller_strs)}")
            else:
                all_parts.append(loc)

    _save_dedup(session_id, seen)
    if not all_parts:
        return None
    return (
        "STOP: Graph already has these locations. Do NOT explore — Read the files directly:\n"
        + "\n".join(f"  • {p}" for p in all_parts[:12])
    )


def _handle_glob(event):
    """Handle Glob tool: match file patterns against indexed files."""
    tool_input = event.get("tool_input", {})
    pattern = tool_input.get("pattern", "") or tool_input.get("glob", "")
    if not pattern:
        return None

    session_id = os.environ.get("TORUS_SESSION_ID") or str(os.getppid())
    dedup_key = f"glob:{pattern}"
    seen = _load_dedup(session_id)
    if dedup_key in seen:
        return None
    seen.add(dedup_key)

    from indexer.schema import connect_code_graph

    db_name = os.environ.get("INDEXER_DB", "main")
    project, _ = _detect_project()
    db = connect_code_graph(database=db_name)

    stem = os.path.basename(pattern).replace("*", "").replace("?", "").rstrip(".")
    if not stem or len(stem) < 2:
        _save_dedup(session_id, seen)
        return None

    nodes = (
        _query_rows(
            db,
            "SELECT file, project FROM code_node WHERE project=$proj AND file CONTAINS $stem LIMIT 30",
            {"proj": project, "stem": stem},
        )
        if project
        else []
    )
    if not nodes:
        nodes = _query_rows(
            db,
            "SELECT file, project FROM code_node WHERE file CONTAINS $stem LIMIT 30",
            {"stem": stem},
        )

    _save_dedup(session_id, seen)
    if not nodes:
        return None

    files = sorted(
        set(
            f"{n['project'] if isinstance(n.get('project'), str) else n.get('project', ['?'])[0]}/{n['file']}"
            for n in nodes
        )
    )
    return (
        f"STOP: Graph already has files matching '{stem}'. Use these paths directly:\n"
        + "\n".join(f"  • {f}" for f in files[:12])
    )


def _handle_read(event):
    """Handle Read tool: inject file structure (functions/exports) on first read."""
    tool_input = event.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return None

    session_id = os.environ.get("TORUS_SESSION_ID") or str(os.getppid())
    dedup_key = f"read:{file_path}"
    seen = _load_dedup(session_id)
    if dedup_key in seen:
        return None
    seen.add(dedup_key)

    from indexer.schema import connect_code_graph, get_callers

    db_name = os.environ.get("INDEXER_DB", "main")
    project, project_root = _detect_project()
    if not project:
        _save_dedup(session_id, seen)
        return None

    rel_path = _to_relative(file_path, project_root)
    if not rel_path:
        _save_dedup(session_id, seen)
        return None

    db = connect_code_graph(database=db_name)

    nodes = _query_rows(
        db,
        "SELECT * FROM code_node WHERE project=$proj AND file=$file",
        {"proj": project, "file": rel_path},
    )
    if not nodes:
        nodes = _query_rows(
            db,
            "SELECT * FROM code_node WHERE file=$file LIMIT 20",
            {"file": rel_path},
        )

    _save_dedup(session_id, seen)
    if not nodes:
        return None

    parts = []
    has_functions = False
    for n in nodes:
        ntype = n.get("type", "")
        if ntype == "function":
            has_functions = True
            callers = get_callers(db, n["id"])
            if callers:
                caller_strs = [_format_caller(c) for c in callers[:4]]
                parts.append(
                    f"fn:{n['name']}(L{n.get('line', '?')}) ← {', '.join(caller_strs)}"
                )
            else:
                parts.append(f"fn:{n['name']}(L{n.get('line', '?')})")
        elif ntype in ("class", "export", "field"):
            parts.append(f"{ntype}:{n['name']}(L{n.get('line', '?')})")

    if not parts and not has_functions:
        imports = [n for n in nodes if n.get("type") == "import"]
        exports = [n for n in nodes if n.get("type") == "export"]
        if imports or exports:
            for imp in imports[:5]:
                parts.append(f"imports: {imp.get('name', '?')}")
            for exp in exports[:5]:
                parts.append(f"exports: {exp.get('name', '?')}")
        else:
            parts.append(f"({len(nodes)} nodes indexed, no function-level detail yet)")

    if not parts:
        return None
    return f"[graph] Structure of {rel_path}:\n" + "\n".join(
        f"  • {p}" for p in parts[:15]
    )


def _load_dedup(session_id):
    """Load the dedup set for this session from ramdisk."""
    path = os.path.join(RAMDISK_DIR, f"indexer_dedup_{session_id}.json")
    try:
        with open(path) as f:
            return set(json.load(f))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def _save_dedup(session_id, seen):
    """Save the dedup set for this session to ramdisk."""
    path = os.path.join(RAMDISK_DIR, f"indexer_dedup_{session_id}.json")
    try:
        os.makedirs(RAMDISK_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(list(seen), f)
    except OSError:
        pass


def _emit(context_text):
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(output))


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        return

    event = json.loads(raw)
    tool_name = event.get("tool_name", "")

    if tool_name in ("Grep", "Bash"):
        result = _handle_search(event)
        if result:
            _emit(result)
        return

    if tool_name == "Agent":
        result = _handle_agent(event)
        if result:
            _emit(result)
        return

    if tool_name == "Glob":
        result = _handle_glob(event)
        if result:
            _emit(result)
        return

    if tool_name == "Read":
        result = _handle_read(event)
        if result:
            _emit(result)
        return

    tool_input = event.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    old_string = tool_input.get("old_string", "")

    if not file_path:
        return

    # Identify which function is being edited
    func_name = _identify_function(file_path, old_string) if old_string else None

    # Session and dedup
    session_id = os.environ.get("TORUS_SESSION_ID") or str(os.getppid())
    dedup_key = f"{file_path}:{func_name}" if func_name else f"{file_path}:__file__"
    seen = _load_dedup(session_id)
    if dedup_key in seen:
        return
    seen.add(dedup_key)

    # Connect to SurrealDB
    from indexer.schema import (
        _node_key,
        connect_code_graph,
        get_callers,
        get_readers,
    )
    from surrealdb import RecordID

    db_name = os.environ.get("INDEXER_DB", "main")
    project, project_root = _detect_project()
    if not project:
        return

    rel_path = _to_relative(file_path, project_root)
    if not rel_path:
        return

    db = connect_code_graph(database=db_name)

    # Fallback: if auto-detected project has no nodes for this file, find the real owner.
    # Tries exact rel_path first, then suffix match for different index roots.
    probe = _query_rows(
        db,
        "SELECT project, file FROM code_node WHERE project=$proj AND file=$file LIMIT 1",
        {"proj": project, "file": rel_path},
    )
    if not probe:
        candidates = _query_rows(
            db,
            "SELECT project, file FROM code_node WHERE file=$file LIMIT 1",
            {"file": rel_path},
        )
        if not candidates:
            basename = os.path.basename(rel_path).replace(
                os.path.splitext(rel_path)[1], ""
            )
            candidates = _query_rows(
                db,
                "SELECT project, file FROM code_node WHERE name=$name AND type=$type LIMIT 1",
                {
                    "name": func_name or basename,
                    "type": "function" if func_name else "file",
                },
            )
        if candidates:
            project = candidates[0]["project"]
            rel_path = candidates[0]["file"]

    parts = []

    if func_name:
        # Query callers of this specific function
        node_key = _node_key(project, rel_path, func_name)
        node_id = RecordID("code_node", node_key)
        callers = get_callers(db, node_id)
        if callers:
            caller_strs = [_format_caller(c) for c in callers]
            parts.append(
                f"Editing fn:{func_name} -- called by: {', '.join(caller_strs)}"
            )

        # Check if any fields in this file have readers
        fields = _query_rows(
            db,
            "SELECT * FROM code_node WHERE project=$proj AND file=$file AND type='field'",
            {"proj": project, "file": rel_path},
        )
        for field in fields:
            fid = field["id"]
            readers = get_readers(db, fid)
            if readers:
                reader_strs = [_format_reader(r) for r in readers]
                parts.append(
                    f"field '{field['name']}' read by: {', '.join(reader_strs)}"
                )
    else:
        # File-level fallback: show callers of all functions in the file
        nodes = _query_rows(
            db,
            "SELECT * FROM code_node WHERE project=$proj AND file=$file",
            {"proj": project, "file": rel_path},
        )
        for node in nodes:
            nid = node["id"]
            if node.get("type") == "function":
                callers = get_callers(db, nid)
                if callers:
                    caller_strs = [_format_caller(c) for c in callers]
                    parts.append(
                        f"fn:{node['name']} called by: {', '.join(caller_strs)}"
                    )
            elif node.get("type") == "field":
                readers = get_readers(db, nid)
                if readers:
                    reader_strs = [_format_reader(r) for r in readers]
                    parts.append(
                        f"field '{node['name']}' read by: {', '.join(reader_strs)}"
                    )

    _save_dedup(session_id, seen)
    if parts:
        _emit("Editing: " + " | ".join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
