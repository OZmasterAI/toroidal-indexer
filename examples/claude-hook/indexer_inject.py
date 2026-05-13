#!/usr/bin/env python3
"""PreToolUse hook: inject structural context from Toroidal-Indexer code graph.

Intercepts Grep, Bash(grep), Agent, Glob, Read, Edit, and Write tool calls.
When the graph has relevant data, injects it as additionalContext so Claude
uses indexed results instead of scanning the filesystem.

Always exits 0 (fail-open, never blocks).

Setup: copy this file and register it in ~/.claude/settings.json:

  {
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "Edit|Write|Grep|Bash|Agent|Glob|Read",
          "hooks": [
            {
              "type": "command",
              "command": "python3 /path/to/indexer_inject.py",
              "timeout": 2
            }
          ]
        }
      ]
    }
  }

Requires: surrealdb, toroidal-indexer on PYTHONPATH.
Optional: tree_sitter_languages (for Edit/Write function identification).
"""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from typing import Any

warnings.filterwarnings("ignore", category=FutureWarning)

SURREAL_URL = os.environ.get("SURREAL_URL", "ws://127.0.0.1:8822")
SURREAL_DB = os.environ.get("INDEXER_DB", "main")


def _query_rows(
    db: Any, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    result = db.query(sql, params) if params else db.query(sql)
    return result if isinstance(result, list) else []


def _detect_project():
    project_root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project_name = os.environ.get("INDEXER_PROJECT") or os.path.basename(project_root)
    return project_name, project_root


def _to_relative(file_path, project_root):
    if not os.path.isabs(file_path):
        return file_path
    try:
        rel = os.path.relpath(file_path, project_root)
    except ValueError:
        return None
    return None if rel.startswith("..") else rel


def _connect():
    from indexer.schema import connect_code_graph

    return connect_code_graph(database=SURREAL_DB)


# ── Formatting ──


def _format_node(n):
    return f"{n.get('name', '?')} ({n.get('file', '?')}:L{n.get('line', 0)}, {n.get('type', '?')})"


def _format_caller(c):
    prefix = "~" if c.get("confidence", 1.0) < 1.0 else ""
    name = c.get("name", "?")
    fpath = os.path.basename(c.get("file", "?"))
    line = c.get("source_line") or c.get("line", 0)
    return f"{prefix}{name} ({fpath}:L{line})"


def _format_reader(r):
    prefix = "~" if r.get("confidence", 1.0) < 1.0 else ""
    name = r.get("name", "?")
    fpath = os.path.basename(r.get("file", "?"))
    line = r.get("line", 0)
    return f"{prefix}{name} ({fpath}:L{line})"


# ── Search (Grep / Bash) ──

_GREP_RE = re.compile(
    r"""(?:grep|rg|ag|ack)\s+(?:.*?\s)?(?:-[A-Za-z]*\s+)*['"]?([A-Za-z_][\w.\-]*(?:\|[A-Za-z_][\w.\-]*)*)['"]?""",
)


def _extract_search_term(event):
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


def _handle_search(event):
    term = _extract_search_term(event)
    if not term:
        return None

    from indexer.schema import get_callers

    project, _ = _detect_project()
    db = _connect()

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
        return None

    parts = []
    for n in nodes[:10]:
        loc = _format_node(n)
        callers = get_callers(db, n["id"])
        if callers:
            caller_strs = [_format_caller(c) for c in callers[:5]]
            parts.append(f"{loc} <- called by: {', '.join(caller_strs)}")
        else:
            parts.append(loc)

    return (
        f"STOP: Graph already has '{term}'. Do NOT run grep -- use these results directly:\n"
        + "\n".join(f"  * {p}" for p in parts)
    )


# ── Agent ──

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_STOP_WORDS = {
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


def _extract_identifiers(text, max_terms=5):
    words = _IDENT_RE.findall(text)
    seen = set()
    result = []
    for w in words:
        wl = w.lower()
        if wl in _STOP_WORDS or wl in seen:
            continue
        if w[0].isupper() or "_" in w or any(c.isupper() for c in w[1:]):
            seen.add(wl)
            result.append(w)
            if len(result) >= max_terms:
                break
    if len(result) < max_terms:
        for w in words:
            wl = w.lower()
            if wl in _STOP_WORDS or wl in seen:
                continue
            if len(w) >= 5:
                seen.add(wl)
                result.append(w)
                if len(result) >= max_terms:
                    break
    return result


def _handle_agent(event):
    prompt = event.get("tool_input", {}).get("prompt", "")
    if not prompt:
        return None

    from indexer.schema import get_callers

    project, _ = _detect_project()
    db = _connect()

    terms = _extract_identifiers(prompt)
    if not terms:
        return None

    all_parts = []
    for term in terms:
        tl = term.lower()
        kebab = _CAMEL_RE.sub("-", term).lower()
        candidates = [tl]
        stem = tl[: max(4, len(tl) // 2)] if len(tl) > 5 else None
        if stem and stem != tl:
            candidates.append(stem)
        if kebab != tl:
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
                all_parts.append(f"{loc} <- {', '.join(caller_strs)}")
            else:
                all_parts.append(loc)

    if not all_parts:
        return None
    return (
        "STOP: Graph already has these locations. Do NOT explore -- Read the files directly:\n"
        + "\n".join(f"  * {p}" for p in all_parts[:12])
    )


# ── Glob ──


def _handle_glob(event):
    pattern = event.get("tool_input", {}).get("pattern", "") or event.get(
        "tool_input", {}
    ).get("glob", "")
    if not pattern:
        return None

    project, _ = _detect_project()
    db = _connect()

    stem = os.path.basename(pattern).replace("*", "").replace("?", "").rstrip(".")
    if not stem or len(stem) < 2:
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
        + "\n".join(f"  * {f}" for f in files[:12])
    )


# ── Read ──


def _handle_read(event):
    file_path = event.get("tool_input", {}).get("file_path", "")
    if not file_path:
        return None

    from indexer.schema import get_callers

    project, project_root = _detect_project()
    if not project:
        return None

    rel_path = _to_relative(file_path, project_root)
    if not rel_path:
        return None

    db = _connect()

    nodes = _query_rows(
        db,
        "SELECT * FROM code_node WHERE project=$proj AND file=$file",
        {"proj": project, "file": rel_path},
    )
    if not nodes:
        return None

    parts = []
    for n in nodes:
        ntype = n.get("type", "")
        if ntype == "function":
            callers = get_callers(db, n["id"])
            if callers:
                caller_strs = [_format_caller(c) for c in callers[:4]]
                parts.append(
                    f"fn:{n['name']}(L{n.get('line', '?')}) <- {', '.join(caller_strs)}"
                )
            else:
                parts.append(f"fn:{n['name']}(L{n.get('line', '?')})")
        elif ntype in ("class", "export", "field"):
            parts.append(f"{ntype}:{n['name']}(L{n.get('line', '?')})")

    if not parts:
        return None
    return f"[graph] Structure of {rel_path}:\n" + "\n".join(
        f"  * {p}" for p in parts[:15]
    )


# ── Edit/Write ──

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
    for child in node.children:
        if child.type in _NAME_CHILD_TYPES:
            return child.text.decode()
    return None


def _identify_function(file_path, old_string):
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


def _handle_edit(event):
    tool_input = event.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    old_string = tool_input.get("old_string", "")
    if not file_path:
        return None

    from indexer.schema import _node_key, get_callers, get_readers
    from surrealdb import RecordID

    project, project_root = _detect_project()
    if not project:
        return None

    rel_path = _to_relative(file_path, project_root)
    if not rel_path:
        return None

    func_name = _identify_function(file_path, old_string) if old_string else None
    db = _connect()

    parts = []
    if func_name:
        node_key = _node_key(project, rel_path, func_name)
        node_id = RecordID("code_node", node_key)
        callers = get_callers(db, node_id)
        if callers:
            caller_strs = [_format_caller(c) for c in callers]
            parts.append(
                f"Editing fn:{func_name} -- called by: {', '.join(caller_strs)}"
            )

        fields = _query_rows(
            db,
            "SELECT * FROM code_node WHERE project=$proj AND file=$file AND type='field'",
            {"proj": project, "file": rel_path},
        )
        for field in fields:
            readers = get_readers(db, field["id"])
            if readers:
                reader_strs = [_format_reader(r) for r in readers]
                parts.append(
                    f"field '{field['name']}' read by: {', '.join(reader_strs)}"
                )
    else:
        nodes = _query_rows(
            db,
            "SELECT * FROM code_node WHERE project=$proj AND file=$file",
            {"proj": project, "file": rel_path},
        )
        for node in nodes:
            if node.get("type") == "function":
                callers = get_callers(db, node["id"])
                if callers:
                    caller_strs = [_format_caller(c) for c in callers]
                    parts.append(
                        f"fn:{node['name']} called by: {', '.join(caller_strs)}"
                    )
            elif node.get("type") == "field":
                readers = get_readers(db, node["id"])
                if readers:
                    reader_strs = [_format_reader(r) for r in readers]
                    parts.append(
                        f"field '{node['name']}' read by: {', '.join(reader_strs)}"
                    )

    if not parts:
        return None
    return "Editing: " + " | ".join(parts)


# ── Output + main ──


def _emit(context_text):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": context_text,
                }
            }
        )
    )


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        return

    event = json.loads(raw)
    tool_name = event.get("tool_name", "")

    handlers = {
        "Grep": _handle_search,
        "Bash": _handle_search,
        "Agent": _handle_agent,
        "Glob": _handle_glob,
        "Read": _handle_read,
        "Edit": _handle_edit,
        "Write": _handle_edit,
    }

    handler = handlers.get(tool_name)
    if handler:
        result = handler(event)
        if result:
            _emit(result)


if __name__ == "__main__":
    main()
