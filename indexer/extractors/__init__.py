"""Toroidal-Indexer extractors: language-specific code graph extraction."""

from dataclasses import dataclass


@dataclass
class Node:
    name: str
    file: str
    type: str  # "file", "function", "class", "field"
    line: int


@dataclass
class Edge:
    source: str  # node name (or file-level)
    target: str  # node name (or resolved file path)
    relation: str  # "imports", "calls", "reads", "writes", "implements"
    confidence: float  # 1.0 for AST
    source_line: int
