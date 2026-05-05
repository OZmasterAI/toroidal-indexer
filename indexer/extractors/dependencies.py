"""Dependency file extractor for project-level graph edges.

Parses go.mod, Cargo.toml, and pyproject.toml to extract project→dependency
relationships. Produces "imports" edges at the project level rather than
code-internal edges.

Supported formats:
  - go.mod: require blocks (direct + indirect), replace directives
  - Cargo.toml: [dependencies], [dev-dependencies], path deps
  - pyproject.toml: [project.dependencies], [project.optional-dependencies]
"""

import os
import re
import tomllib

from indexer.extractors import Edge, Node

# --- go.mod patterns ---

RE_GO_MODULE = re.compile(r"^module\s+(\S+)", re.MULTILINE)

RE_GO_REQUIRE_BLOCK = re.compile(
    r"^require\s*\((.*?)\)",
    re.MULTILINE | re.DOTALL,
)

RE_GO_REQUIRE_LINE = re.compile(
    r"^\s*(\S+)\s+v[\w.\-]+(.*)$",
    re.MULTILINE,
)

RE_GO_REPLACE = re.compile(
    r"^replace\s+(\S+)\s+=>\s+(\S+)",
    re.MULTILINE,
)


def _extract_go_mod(source: str, rel_path: str) -> tuple[list[Node], list[Edge]]:
    """Extract dependencies from a go.mod file."""
    nodes: list[Node] = []
    edges: list[Edge] = []

    # Module name as file node
    mod_match = RE_GO_MODULE.search(source)
    mod_name = mod_match.group(1) if mod_match else rel_path
    nodes.append(Node(name=mod_name, file=rel_path, type="file", line=1))

    # Require blocks
    for block_m in RE_GO_REQUIRE_BLOCK.finditer(source):
        block = block_m.group(1)
        block_start_line = source[: block_m.start()].count("\n") + 1
        for line_m in RE_GO_REQUIRE_LINE.finditer(block):
            dep_path = line_m.group(1)
            remainder = line_m.group(2).strip()
            is_indirect = "indirect" in remainder
            dep_line = block_start_line + block[: line_m.start()].count("\n")
            edges.append(
                Edge(
                    source=mod_name,
                    target=dep_path,
                    relation="imports",
                    confidence=0.5 if is_indirect else 1.0,
                    source_line=dep_line,
                )
            )

    # Replace directives
    for m in RE_GO_REPLACE.finditer(source):
        original = m.group(1)
        replacement = m.group(2)
        lineno = source[: m.start()].count("\n") + 1
        edges.append(
            Edge(
                source=original,
                target=replacement,
                relation="implements",
                confidence=1.0,
                source_line=lineno,
            )
        )

    return nodes, edges


def _extract_cargo_toml(data: dict, rel_path: str) -> tuple[list[Node], list[Edge]]:
    """Extract dependencies from parsed Cargo.toml."""
    nodes: list[Node] = []
    edges: list[Edge] = []

    pkg_name = data.get("package", {}).get("name", rel_path)
    nodes.append(Node(name=pkg_name, file=rel_path, type="file", line=1))

    # Regular dependencies
    deps = data.get("dependencies", {})
    for name in deps:
        edges.append(
            Edge(
                source=pkg_name,
                target=name,
                relation="imports",
                confidence=1.0,
                source_line=1,
            )
        )

    # Dev dependencies (lower confidence)
    dev_deps = data.get("dev-dependencies", {})
    for name in dev_deps:
        edges.append(
            Edge(
                source=pkg_name,
                target=name,
                relation="imports",
                confidence=0.5,
                source_line=1,
            )
        )

    # Build dependencies
    build_deps = data.get("build-dependencies", {})
    for name in build_deps:
        edges.append(
            Edge(
                source=pkg_name,
                target=name,
                relation="imports",
                confidence=0.7,
                source_line=1,
            )
        )

    return nodes, edges


def _strip_version_spec(dep_str: str) -> str:
    """Strip version specifiers from a PEP 508 dependency string."""
    # Remove extras: package[extra] -> package
    name = re.split(r"[\[>=<~!;]", dep_str)[0].strip()
    return name


def _extract_pyproject_toml(data: dict, rel_path: str) -> tuple[list[Node], list[Edge]]:
    """Extract dependencies from parsed pyproject.toml."""
    nodes: list[Node] = []
    edges: list[Edge] = []

    project = data.get("project", {})
    pkg_name = project.get("name", rel_path)
    nodes.append(Node(name=pkg_name, file=rel_path, type="file", line=1))

    # Main dependencies
    deps = project.get("dependencies", [])
    for dep in deps:
        name = _strip_version_spec(dep)
        if name:
            edges.append(
                Edge(
                    source=pkg_name,
                    target=name,
                    relation="imports",
                    confidence=1.0,
                    source_line=1,
                )
            )

    # Optional dependencies (lower confidence)
    optional = project.get("optional-dependencies", {})
    for group_deps in optional.values():
        for dep in group_deps:
            name = _strip_version_spec(dep)
            if name:
                edges.append(
                    Edge(
                        source=pkg_name,
                        target=name,
                        relation="imports",
                        confidence=0.5,
                        source_line=1,
                    )
                )

    return nodes, edges


def extract_dependencies(
    file_path: str, project_root: str
) -> tuple[list[Node], list[Edge]]:
    """Extract project-level dependency edges from manifest files.

    Args:
        file_path: Absolute path to go.mod, Cargo.toml, or pyproject.toml.
        project_root: Absolute path to the project root.

    Returns:
        (nodes, edges) representing project→dependency relationships.
    """
    if not os.path.isfile(file_path):
        return [], []

    basename = os.path.basename(file_path)
    rel_path = os.path.relpath(file_path, project_root)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return [], []

    if basename == "go.mod":
        return _extract_go_mod(source, rel_path)

    if basename in ("Cargo.toml", "pyproject.toml"):
        try:
            data = tomllib.loads(source)
        except (tomllib.TOMLDecodeError, ValueError):
            return [Node(name=rel_path, file=rel_path, type="file", line=1)], []

        if basename == "Cargo.toml":
            return _extract_cargo_toml(data, rel_path)
        return _extract_pyproject_toml(data, rel_path)

    return [], []
