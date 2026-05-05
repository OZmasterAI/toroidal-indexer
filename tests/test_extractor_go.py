"""Tests for Go extractor (Tier 1 regex-based)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.extractors import Edge, Node
from indexer.extractors.go import extract_go


@pytest.fixture
def project(tmp_path):
    """Create a minimal Go project in tmp_path."""
    (tmp_path / "go.mod").write_text(
        "module github.com/torus-chain/torus\n\ngo 1.21\n\n"
        "require (\n\tgithub.com/cosmos/cosmos-sdk v0.50.0\n)\n"
    )
    (tmp_path / "main.go").write_text(
        "package main\n\n"
        "import (\n"
        '\t"fmt"\n'
        '\t"os"\n'
        "\n"
        '\t"github.com/torus-chain/torus/x/governance"\n'
        ")\n\n"
        'func main() {\n\tfmt.Println("hello")\n}\n'
    )
    xdir = tmp_path / "x" / "governance"
    xdir.mkdir(parents=True)
    (xdir / "keeper.go").write_text(
        "package governance\n\n"
        "import (\n"
        '\tsdk "github.com/cosmos/cosmos-sdk/types"\n'
        ")\n\n"
        "type Keeper struct {\n\tdb Database\n}\n\n"
        "type MsgServer interface {\n\tSubmitProposal(ctx sdk.Context) error\n}\n\n"
        "func NewKeeper(db Database) *Keeper {\n\treturn &Keeper{db: db}\n}\n\n"
        "func (k Keeper) SubmitProposal(ctx sdk.Context) error {\n\treturn nil\n}\n\n"
        "func (k *Keeper) Vote(ctx sdk.Context) error {\n\treturn nil\n}\n"
    )
    return tmp_path


class TestGoImports:
    def test_stdlib_imports(self, project):
        nodes, edges = extract_go(str(project / "main.go"), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        targets = {e.target for e in import_edges}
        assert "fmt" in targets
        assert "os" in targets

    def test_internal_package_import_resolved(self, project):
        nodes, edges = extract_go(str(project / "main.go"), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        targets = {e.target for e in import_edges}
        assert "x/governance" in targets or any("x/governance" in t for t in targets)

    def test_aliased_import(self, project):
        nodes, edges = extract_go(
            str(project / "x" / "governance" / "keeper.go"), str(project)
        )
        import_edges = [e for e in edges if e.relation == "imports"]
        targets = {e.target for e in import_edges}
        assert "github.com/cosmos/cosmos-sdk/types" in targets

    def test_import_line_numbers(self, project):
        nodes, edges = extract_go(str(project / "main.go"), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        assert all(e.source_line > 0 for e in import_edges)


class TestGoFunctions:
    def test_top_level_function(self, project):
        nodes, edges = extract_go(str(project / "main.go"), str(project))
        func_nodes = [n for n in nodes if n.type == "function"]
        names = {n.name for n in func_nodes}
        assert "main" in names

    def test_standalone_function(self, project):
        nodes, edges = extract_go(
            str(project / "x" / "governance" / "keeper.go"), str(project)
        )
        func_nodes = [n for n in nodes if n.type == "function"]
        names = {n.name for n in func_nodes}
        assert "NewKeeper" in names

    def test_method_with_receiver(self, project):
        nodes, edges = extract_go(
            str(project / "x" / "governance" / "keeper.go"), str(project)
        )
        func_nodes = [n for n in nodes if n.type == "function"]
        names = {n.name for n in func_nodes}
        assert "SubmitProposal" in names
        assert "Vote" in names

    def test_function_line_numbers(self, project):
        nodes, edges = extract_go(
            str(project / "x" / "governance" / "keeper.go"), str(project)
        )
        func_nodes = [n for n in nodes if n.type == "function"]
        assert all(n.line > 0 for n in func_nodes)


class TestGoTypes:
    def test_struct_definition(self, project):
        nodes, edges = extract_go(
            str(project / "x" / "governance" / "keeper.go"), str(project)
        )
        class_nodes = [n for n in nodes if n.type == "class"]
        names = {n.name for n in class_nodes}
        assert "Keeper" in names

    def test_interface_definition(self, project):
        nodes, edges = extract_go(
            str(project / "x" / "governance" / "keeper.go"), str(project)
        )
        class_nodes = [n for n in nodes if n.type == "class"]
        names = {n.name for n in class_nodes}
        assert "MsgServer" in names


class TestGoMethodReceivers:
    def test_value_receiver_implements_edge(self, project):
        nodes, edges = extract_go(
            str(project / "x" / "governance" / "keeper.go"), str(project)
        )
        impl_edges = [e for e in edges if e.relation == "implements"]
        pairs = {(e.source, e.target) for e in impl_edges}
        assert ("SubmitProposal", "Keeper") in pairs

    def test_pointer_receiver_implements_edge(self, project):
        nodes, edges = extract_go(
            str(project / "x" / "governance" / "keeper.go"), str(project)
        )
        impl_edges = [e for e in edges if e.relation == "implements"]
        pairs = {(e.source, e.target) for e in impl_edges}
        assert ("Vote", "Keeper") in pairs


class TestGoFileNode:
    def test_file_node_always_present(self, project):
        nodes, edges = extract_go(str(project / "main.go"), str(project))
        file_nodes = [n for n in nodes if n.type == "file"]
        assert len(file_nodes) == 1
        assert file_nodes[0].file == "main.go"

    def test_nonexistent_file_returns_empty(self, project):
        nodes, edges = extract_go(str(project / "nope.go"), str(project))
        assert nodes == []
        assert edges == []


class TestGoModulePath:
    def test_resolves_internal_import_via_go_mod(self, project):
        """Internal imports matching module path get resolved to relative dirs."""
        nodes, edges = extract_go(str(project / "main.go"), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        resolved = [
            e
            for e in import_edges
            if "/" in e.target and not e.target.startswith("github.com")
        ]
        assert any("governance" in e.target for e in resolved)


class TestGoEdgeCases:
    def test_empty_file(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.21\n")
        (tmp_path / "empty.go").write_text("")
        nodes, edges = extract_go(str(tmp_path / "empty.go"), str(tmp_path))
        assert nodes == [] or (len(nodes) == 1 and nodes[0].type == "file")
        assert edges == []

    def test_syntax_garbage(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.21\n")
        (tmp_path / "bad.go").write_text("this is not go code {{{}}}")
        nodes, edges = extract_go(str(tmp_path / "bad.go"), str(tmp_path))
        file_nodes = [n for n in nodes if n.type == "file"]
        assert len(file_nodes) == 1

    def test_build_tags_not_confuse(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.21\n")
        (tmp_path / "tagged.go").write_text(
            '//go:build linux\n\npackage main\n\nimport "fmt"\n\nfunc hello() {}\n'
        )
        nodes, edges = extract_go(str(tmp_path / "tagged.go"), str(tmp_path))
        func_nodes = [n for n in nodes if n.type == "function"]
        assert any(n.name == "hello" for n in func_nodes)

    def test_multiline_import_block(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.21\n")
        (tmp_path / "multi.go").write_text(
            'package main\n\nimport (\n\t"fmt"\n\t"os"\n\t"strings"\n)\n\nfunc run() {}\n'
        )
        nodes, edges = extract_go(str(tmp_path / "multi.go"), str(tmp_path))
        import_edges = [e for e in edges if e.relation == "imports"]
        targets = {e.target for e in import_edges}
        assert "fmt" in targets
        assert "os" in targets
        assert "strings" in targets
