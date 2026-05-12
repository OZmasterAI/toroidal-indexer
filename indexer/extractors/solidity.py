"""Solidity code graph extractor.

Uses tree-sitter for structural extraction (contracts, functions, events,
modifiers, inheritance). Falls back to regex if tree-sitter is unavailable.
"""

import os
import re

from indexer.extractors import Edge, Node

try:
    from indexer.extractors.solidity_ts import extract_solidity_ts
except ImportError:
    extract_solidity_ts = None

# Regex patterns for fallback
RE_CONTRACT = re.compile(
    r"^\s*contract\s+(\w+)(?:\s+is\s+([\w,\s]+))?\s*\{", re.MULTILINE
)
RE_FUNCTION = re.compile(
    r"^\s*function\s+(\w+)\s*\([^)]*\)\s*(?:\w+\s*)*(public|external)\s",
    re.MULTILINE,
)
RE_EVENT = re.compile(r"^\s*event\s+(\w+)\s*\(", re.MULTILINE)
RE_MODIFIER = re.compile(r"^\s*modifier\s+(\w+)\s*\(", re.MULTILINE)


def extract_solidity(file_path, project_root):
    """Extract nodes and edges from a Solidity file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, IOError):
        return [], []

    rel_path = os.path.relpath(file_path, project_root)

    if not source.strip():
        return [Node(name=rel_path, file=rel_path, type="file", line=1)], []

    if extract_solidity_ts is not None:
        result = extract_solidity_ts(source, rel_path, project_root)
        if result is not None:
            return result

    return _extract_regex(source, rel_path)


def _extract_regex(source, rel_path):
    """Regex fallback for Solidity extraction."""
    nodes = [Node(name=rel_path, file=rel_path, type="file", line=1)]
    edges = []

    for m in RE_CONTRACT.finditer(source):
        name = m.group(1)
        line = source[: m.start()].count("\n") + 1
        nodes.append(Node(name=name, file=rel_path, type="class", line=line))
        if m.group(2):
            for parent in m.group(2).split(","):
                parent = parent.strip()
                if parent:
                    edges.append(
                        Edge(
                            source=name,
                            target=parent,
                            relation="implements",
                            confidence=1.0,
                            source_line=line,
                        )
                    )

    for m in RE_FUNCTION.finditer(source):
        name = m.group(1)
        line = source[: m.start()].count("\n") + 1
        nodes.append(Node(name=name, file=rel_path, type="function", line=line))

    for m in RE_EVENT.finditer(source):
        name = m.group(1)
        line = source[: m.start()].count("\n") + 1
        nodes.append(Node(name=name, file=rel_path, type="event", line=line))

    for m in RE_MODIFIER.finditer(source):
        name = m.group(1)
        line = source[: m.start()].count("\n") + 1
        nodes.append(Node(name=name, file=rel_path, type="modifier", line=line))

    return nodes, edges
