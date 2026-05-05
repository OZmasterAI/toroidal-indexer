"""Bash/Shell regex-based code graph extractor.

Extracts source/dot-source imports and function definitions from shell scripts.
Handles heredoc skipping to avoid false matches inside heredoc blocks.

Known limitations:
  - Variable expansion in source paths: emitted as-is with lower confidence
  - eval/dynamic sourcing: not captured
  - Functions defined inside conditionals: captured (may not always execute)
"""

import os
import re

from indexer.extractors import Edge, Node

# --- Regex patterns ---

# source "path" or source path or . "path" or . path
# Must not be preceded by # (comment)
RE_SOURCE = re.compile(
    r"""(?:^|&&\s*|;\s*|\|\|\s*)source\s+["']?([^"'\s;#]+)["']?"""
    r"""|(?:^|&&\s*|;\s*|\|\|\s*)\.\s+["']?([^"'\s;#]+)["']?""",
    re.MULTILINE,
)

# function name() { or function name {
RE_FUNC_KEYWORD = re.compile(
    r"^\s*function\s+(\w[\w-]*)\s*(?:\(\s*\))?\s*\{",
    re.MULTILINE,
)

# name() {
RE_FUNC_PAREN = re.compile(
    r"^\s*(\w[\w-]*)\s*\(\s*\)\s*\{",
    re.MULTILINE,
)

# Heredoc start: <<'DELIM' or <<DELIM or <<"DELIM" or <<-DELIM
RE_HEREDOC_START = re.compile(
    r"<<-?\s*['\"]?(\w+)['\"]?",
)


def _find_heredoc_ranges(source: str) -> list[tuple[int, int]]:
    """Find character ranges that are inside heredoc blocks."""
    ranges = []
    lines = source.split("\n")
    line_positions = []
    pos = 0
    for line in lines:
        line_positions.append(pos)
        pos += len(line) + 1

    i = 0
    while i < len(lines):
        m = RE_HEREDOC_START.search(lines[i])
        if m:
            delim = m.group(1)
            start = line_positions[i] + len(lines[i]) + 1
            j = i + 1
            while j < len(lines):
                if lines[j].strip() == delim:
                    end = line_positions[j]
                    ranges.append((start, end))
                    i = j
                    break
                j += 1
        i += 1
    return ranges


def _in_heredoc(pos: int, ranges: list[tuple[int, int]]) -> bool:
    """Check if a character position is inside a heredoc block."""
    for start, end in ranges:
        if start <= pos < end:
            return True
    return False


def _is_commented(source: str, match_start: int) -> bool:
    """Check if a match position is on a commented line."""
    line_start = source.rfind("\n", 0, match_start) + 1
    line_prefix = source[line_start:match_start].lstrip()
    return line_prefix.startswith("#")


def extract_bash(file_path: str, project_root: str) -> tuple[list[Node], list[Edge]]:
    """Extract code graph nodes and edges from a shell script.

    Args:
        file_path: Absolute path to the shell script.
        project_root: Absolute path to the project root.

    Returns:
        (nodes, edges) extracted from the file.
    """
    if not os.path.isfile(file_path):
        return [], []

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return [], []

    if not source.strip():
        rel_path = os.path.relpath(file_path, project_root)
        return [Node(name=rel_path, file=rel_path, type="file", line=1)], []

    rel_path = os.path.relpath(file_path, project_root)
    nodes: list[Node] = [Node(name=rel_path, file=rel_path, type="file", line=1)]
    edges: list[Edge] = []

    heredoc_ranges = _find_heredoc_ranges(source)

    # Line number helper
    line_starts = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            line_starts.append(i + 1)

    def _lineno(pos):
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    # --- Source/dot-source imports ---
    for m in RE_SOURCE.finditer(source):
        if _in_heredoc(m.start(), heredoc_ranges):
            continue
        if _is_commented(source, m.start()):
            continue
        path = m.group(1) or m.group(2)
        if not path:
            continue
        lineno = _lineno(m.start())
        has_variable = "$" in path
        edges.append(
            Edge(
                source=rel_path,
                target=path,
                relation="imports",
                confidence=0.7 if has_variable else 1.0,
                source_line=lineno,
            )
        )

    # --- Function definitions (keyword syntax) ---
    for m in RE_FUNC_KEYWORD.finditer(source):
        if _in_heredoc(m.start(), heredoc_ranges):
            continue
        if _is_commented(source, m.start()):
            continue
        name = m.group(1)
        lineno = _lineno(m.start())
        nodes.append(Node(name=name, file=rel_path, type="function", line=lineno))

    # --- Function definitions (paren syntax) ---
    for m in RE_FUNC_PAREN.finditer(source):
        if _in_heredoc(m.start(), heredoc_ranges):
            continue
        if _is_commented(source, m.start()):
            continue
        name = m.group(1)
        lineno = _lineno(m.start())
        # Skip if already captured by keyword syntax
        if not any(n.name == name and n.line == lineno for n in nodes):
            nodes.append(Node(name=name, file=rel_path, type="function", line=lineno))

    return nodes, edges
