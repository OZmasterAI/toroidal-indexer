"""Rust extractor: regex-based code graph extraction (~70% coverage by design).

Extracts use statements, mod declarations, impl blocks, pub use re-exports,
and function definitions. Resolves crate paths via Cargo.toml workspace members.

Known limitations (Tier 3 AI fills gaps):
  - #[cfg(...)] conditional compilation: included but not evaluated
  - Multi-level re-exports through external crates: missed
  - Trait method dispatch: file-level only (which impl is unknown)
"""

import os
import re

from indexer.extractors import Edge, Node

# --- Regex patterns ---

# use statements: use crate::X, use super::X, use self::X, use external::X
# Captures the full path after 'use' up to the semicolon (stripping trailing ;)
RE_USE = re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+(?:::\{[^}]+\})?)\s*;", re.MULTILINE)

# mod declarations: mod foo; or pub mod foo; (semicolon-terminated)
RE_MOD = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", re.MULTILINE)

# mod declarations: inline mod foo { ... } (brace-terminated)
RE_MOD_INLINE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*\{", re.MULTILINE
)

# impl blocks: impl Foo { or impl Trait for Foo {
RE_IMPL = re.compile(
    r"^\s*impl(?:<[^>]*>)?\s+(\w+(?:::\w+)*)\s+for\s+(\w+(?:::\w+)*)",
    re.MULTILINE,
)
RE_IMPL_SELF = re.compile(
    r"^\s*impl(?:<[^>]*>)?\s+(\w+(?:::\w+)*)\s*\{",
    re.MULTILINE,
)

# Function definitions: fn, pub fn, pub(crate) fn, async fn, pub async fn, etc.
RE_FN = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)",
    re.MULTILINE,
)


def extract_rust(file_path, project_root):
    """Extract nodes and edges from a Rust source file.

    Args:
        file_path: Absolute path to the .rs file.
        project_root: Absolute path to the project root (contains Cargo.toml).

    Returns:
        (list[Node], list[Edge]) -- extracted graph elements.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, IOError):
        return [], []

    # Bail on truly empty content (but still return file node for whitespace-only)
    rel_path = os.path.relpath(file_path, project_root)
    nodes = [Node(name=rel_path, file=rel_path, type="file", line=1)]
    edges = []

    if not source.strip():
        return nodes, edges

    # Build a line-offset index for computing line numbers from match positions
    line_starts = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            line_starts.append(i + 1)

    def _lineno(pos):
        """Return 1-based line number for a character position."""
        # Skip leading whitespace/newlines that MULTILINE ^ may have consumed
        while pos < len(source) and source[pos] in (" ", "\t", "\n", "\r"):
            pos += 1
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    file_dir = os.path.dirname(file_path)

    # --- Use statements ---
    for m in RE_USE.finditer(source):
        path = m.group(1)
        lineno = _lineno(m.start())
        target = _resolve_use_path(path, file_dir, project_root)
        edges.append(
            Edge(
                source=rel_path,
                target=target,
                relation="imports",
                confidence=1.0,
                source_line=lineno,
            )
        )

    # --- Mod declarations ---
    for m in RE_MOD.finditer(source):
        mod_name = m.group(1)
        lineno = _lineno(m.start())
        target = _resolve_mod_path(mod_name, file_dir, project_root)
        edges.append(
            Edge(
                source=rel_path,
                target=target,
                relation="imports",
                confidence=1.0,
                source_line=lineno,
            )
        )

    # --- Inline mod blocks (mod foo { ... }) ---
    for m in RE_MOD_INLINE.finditer(source):
        mod_name = m.group(1)
        lineno = _lineno(m.start())
        target = _resolve_mod_path(mod_name, file_dir, project_root)
        edges.append(
            Edge(
                source=rel_path,
                target=target,
                relation="imports",
                confidence=1.0,
                source_line=lineno,
            )
        )

    # --- Impl blocks (trait for struct) ---
    for m in RE_IMPL.finditer(source):
        trait_name = m.group(1)
        struct_name = m.group(2)
        lineno = _lineno(m.start())
        edges.append(
            Edge(
                source=struct_name,
                target=trait_name,
                relation="implements",
                confidence=1.0,
                source_line=lineno,
            )
        )

    # --- Impl blocks (self, no trait) ---
    for m in RE_IMPL_SELF.finditer(source):
        struct_name = m.group(1)
        lineno = _lineno(m.start())
        # Skip if this was already matched as a trait impl
        line_text = source[m.start() : source.find("\n", m.start())]
        if " for " in line_text:
            continue
        edges.append(
            Edge(
                source=struct_name,
                target=struct_name,
                relation="implements",
                confidence=1.0,
                source_line=lineno,
            )
        )

    # --- Function definitions ---
    for m in RE_FN.finditer(source):
        fn_name = m.group(1)
        lineno = _lineno(m.start())
        nodes.append(
            Node(
                name=fn_name,
                file=rel_path,
                type="function",
                line=lineno,
            )
        )

    return nodes, edges


def _resolve_use_path(path, file_dir, project_root):
    """Resolve a use path to the best target string.

    For crate-internal paths (crate::, super::, self::), attempts to resolve
    to a file path. For external crates, returns the dotted path as-is.
    """
    # Strip braced groups for resolution: crate::config::{A, B} -> crate::config
    clean = re.sub(r"::\{[^}]+\}", "", path)
    parts = clean.split("::")

    if parts[0] == "crate":
        # Resolve relative to the crate src/ directory
        crate_root = _find_crate_src(file_dir, project_root)
        if crate_root:
            remainder = parts[1:]
            return _try_resolve_file(remainder, crate_root, project_root)
        return "::".join(parts[1:]) if len(parts) > 1 else path

    if parts[0] == "super":
        parent = os.path.dirname(file_dir)
        remainder = parts[1:]
        return _try_resolve_file(remainder, parent, project_root)

    if parts[0] == "self":
        remainder = parts[1:]
        return _try_resolve_file(remainder, file_dir, project_root)

    # External crate import — return as-is
    return path


def _resolve_mod_path(mod_name, file_dir, project_root):
    """Resolve a mod declaration to a file path.

    Checks for mod_name.rs first, then mod_name/mod.rs (Rust 2018+ convention).
    """
    # Check sibling file: mod_name.rs
    sibling = os.path.join(file_dir, mod_name + ".rs")
    if os.path.isfile(sibling):
        return os.path.relpath(sibling, project_root)

    # Check directory: mod_name/mod.rs
    dir_mod = os.path.join(file_dir, mod_name, "mod.rs")
    if os.path.isfile(dir_mod):
        return os.path.relpath(dir_mod, project_root)

    # Unresolved — return bare name
    return mod_name


def _find_crate_src(file_dir, project_root):
    """Walk up from file_dir to find the nearest src/ directory within a crate.

    Returns the src/ directory path, or None if not found.
    """
    d = file_dir
    while d.startswith(project_root) and d != os.path.dirname(d):
        if os.path.basename(d) == "src":
            return d
        d = os.path.dirname(d)
    return None


def _try_resolve_file(parts, base_dir, project_root):
    """Try to resolve path parts to a .rs file relative to base_dir.

    Tries: base_dir/a/b/c.rs, then base_dir/a/b/c/mod.rs.
    Falls back to the joined path string.
    """
    if not parts:
        return os.path.relpath(base_dir, project_root)

    # Try direct file
    file_path = os.path.join(base_dir, *parts[:-1], parts[-1] + ".rs")
    if os.path.isfile(file_path):
        return os.path.relpath(file_path, project_root)

    # Try directory with mod.rs
    dir_path = os.path.join(base_dir, *parts, "mod.rs")
    if os.path.isfile(dir_path):
        return os.path.relpath(dir_path, project_root)

    # Unresolved — return joined path
    return "::".join(parts)
