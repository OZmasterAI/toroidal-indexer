"""Tests for Bash/Shell extractor (Tier 1 regex-based)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.extractors import Edge, Node
from indexer.extractors.bash import extract_bash


@pytest.fixture
def project(tmp_path):
    """Create a shell project with source chains."""
    (tmp_path / "main.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'source "$SCRIPT_DIR/lib/utils.sh"\n'
        ". /etc/profile.d/env.sh\n\n"
        "function setup() {\n"
        "    echo setup\n"
        "}\n\n"
        "cleanup() {\n"
        "    echo cleanup\n"
        "}\n\n"
        "main() {\n"
        "    setup\n"
        "    cleanup\n"
        "}\n\n"
        "main\n"
    )

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "utils.sh").write_text(
        "#!/bin/bash\n\n"
        "log() {\n"
        '    echo "[$(date)] $1"\n'
        "}\n\n"
        "die() {\n"
        '    log "FATAL: $1"\n'
        "    exit 1\n"
        "}\n\n"
        'source "$SCRIPT_DIR/lib/colors.sh"\n'
    )

    (lib_dir / "colors.sh").write_text(
        '#!/bin/bash\nRED="\\033[0;31m"\nGREEN="\\033[0;32m"\nNC="\\033[0m"\n'
    )

    return tmp_path


class TestBashSourceImports:
    def test_source_with_variable_path(self, project):
        nodes, edges = extract_bash(str(project / "main.sh"), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        assert any("utils.sh" in e.target for e in import_edges)

    def test_dot_source_absolute(self, project):
        nodes, edges = extract_bash(str(project / "main.sh"), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        assert any("/etc/profile.d/env.sh" in e.target for e in import_edges)

    def test_source_line_numbers(self, project):
        nodes, edges = extract_bash(str(project / "main.sh"), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        assert all(e.source_line > 0 for e in import_edges)

    def test_variable_path_lower_confidence(self, project):
        nodes, edges = extract_bash(str(project / "main.sh"), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        var_imports = [e for e in import_edges if "$" in e.target]
        literal_imports = [e for e in import_edges if "$" not in e.target]
        if var_imports:
            assert all(e.confidence < 1.0 for e in var_imports)
        if literal_imports:
            assert all(e.confidence == 1.0 for e in literal_imports)


class TestBashFunctions:
    def test_function_keyword_syntax(self, project):
        nodes, edges = extract_bash(str(project / "main.sh"), str(project))
        func_nodes = [n for n in nodes if n.type == "function"]
        names = {n.name for n in func_nodes}
        assert "setup" in names

    def test_paren_syntax(self, project):
        nodes, edges = extract_bash(str(project / "main.sh"), str(project))
        func_nodes = [n for n in nodes if n.type == "function"]
        names = {n.name for n in func_nodes}
        assert "cleanup" in names
        assert "main" in names

    def test_function_in_sourced_file(self, project):
        nodes, edges = extract_bash(str(project / "lib" / "utils.sh"), str(project))
        func_nodes = [n for n in nodes if n.type == "function"]
        names = {n.name for n in func_nodes}
        assert "log" in names
        assert "die" in names

    def test_function_line_numbers(self, project):
        nodes, edges = extract_bash(str(project / "main.sh"), str(project))
        func_nodes = [n for n in nodes if n.type == "function"]
        assert all(n.line > 0 for n in func_nodes)


class TestBashFileNode:
    def test_file_node_present(self, project):
        nodes, edges = extract_bash(str(project / "main.sh"), str(project))
        file_nodes = [n for n in nodes if n.type == "file"]
        assert len(file_nodes) == 1
        assert file_nodes[0].file == "main.sh"

    def test_nonexistent_file(self, project):
        nodes, edges = extract_bash(str(project / "nope.sh"), str(project))
        assert nodes == []
        assert edges == []


class TestBashEdgeCases:
    def test_empty_file(self, tmp_path):
        (tmp_path / "empty.sh").write_text("")
        nodes, edges = extract_bash(str(tmp_path / "empty.sh"), str(tmp_path))
        assert edges == []

    def test_heredoc_not_matched(self, tmp_path):
        (tmp_path / "heredoc.sh").write_text(
            "#!/bin/bash\n\n"
            "cat <<'EOF'\n"
            "source fake/not_real.sh\n"
            "function not_a_func() {\n"
            "EOF\n\n"
            "real_func() {\n"
            "    echo real\n"
            "}\n"
        )
        nodes, edges = extract_bash(str(tmp_path / "heredoc.sh"), str(tmp_path))
        import_edges = [e for e in edges if e.relation == "imports"]
        assert not any("not_real" in e.target for e in import_edges)
        func_nodes = [n for n in nodes if n.type == "function"]
        names = {n.name for n in func_nodes}
        assert "real_func" in names
        assert "not_a_func" not in names

    def test_commented_source_not_matched(self, tmp_path):
        (tmp_path / "commented.sh").write_text(
            "#!/bin/bash\n# source should/not/match.sh\nsource real/file.sh\n"
        )
        nodes, edges = extract_bash(str(tmp_path / "commented.sh"), str(tmp_path))
        import_edges = [e for e in edges if e.relation == "imports"]
        assert not any("should/not/match" in e.target for e in import_edges)
        assert any("real/file.sh" in e.target for e in import_edges)

    def test_shebang_detected(self, tmp_path):
        (tmp_path / "zsh_script").write_text(
            "#!/usr/bin/env zsh\n\nmy_func() { echo hi; }\n"
        )
        nodes, edges = extract_bash(str(tmp_path / "zsh_script"), str(tmp_path))
        func_nodes = [n for n in nodes if n.type == "function"]
        assert any(n.name == "my_func" for n in func_nodes)

    def test_source_in_conditional(self, tmp_path):
        (tmp_path / "cond.sh").write_text(
            '#!/bin/bash\n[ -f "./config.sh" ] && source "./config.sh"\n'
        )
        nodes, edges = extract_bash(str(tmp_path / "cond.sh"), str(tmp_path))
        import_edges = [e for e in edges if e.relation == "imports"]
        assert any("config.sh" in e.target for e in import_edges)
