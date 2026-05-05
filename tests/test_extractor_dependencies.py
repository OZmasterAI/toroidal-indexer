"""Tests for dependency file extractor (go.mod, Cargo.toml, pyproject.toml)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.extractors import Edge, Node
from indexer.extractors.dependencies import extract_dependencies


@pytest.fixture
def go_project(tmp_path):
    """Create a Go project with go.mod."""
    (tmp_path / "go.mod").write_text(
        "module github.com/torus-chain/torus\n\n"
        "go 1.21\n\n"
        "require (\n"
        "\tgithub.com/cosmos/cosmos-sdk v0.50.0\n"
        "\tgithub.com/cometbft/cometbft v0.38.0\n"
        "\tgoogle.golang.org/grpc v1.60.0\n"
        ")\n\n"
        "require (\n"
        "\tgithub.com/indirect/dep v1.0.0 // indirect\n"
        ")\n\n"
        "replace github.com/cosmos/cosmos-sdk => ../cosmos-sdk-fork\n"
    )
    return tmp_path


@pytest.fixture
def cargo_project(tmp_path):
    """Create a Rust project with Cargo.toml."""
    (tmp_path / "Cargo.toml").write_text(
        "[package]\n"
        'name = "my-crate"\n'
        'version = "0.1.0"\n\n'
        "[dependencies]\n"
        'serde = { version = "1.0", features = ["derive"] }\n'
        'tokio = { version = "1", features = ["full"] }\n'
        'local-lib = { path = "../local-lib" }\n\n'
        "[dev-dependencies]\n"
        'tempfile = "3"\n'
    )
    return tmp_path


@pytest.fixture
def python_project(tmp_path):
    """Create a Python project with pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "my-package"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        '    "surrealdb>=1.0.0",\n'
        '    "numpy>=1.24",\n'
        '    "requests",\n'
        "]\n\n"
        "[project.optional-dependencies]\n"
        'dev = [\n    "pytest>=7.0",\n    "ruff",\n]\n'
    )
    return tmp_path


class TestGoMod:
    def test_extracts_direct_dependencies(self, go_project):
        nodes, edges = extract_dependencies(str(go_project / "go.mod"), str(go_project))
        import_edges = [e for e in edges if e.relation == "imports"]
        targets = {e.target for e in import_edges}
        assert "github.com/cosmos/cosmos-sdk" in targets
        assert "github.com/cometbft/cometbft" in targets
        assert "google.golang.org/grpc" in targets

    def test_indirect_deps_marked_lower_confidence(self, go_project):
        nodes, edges = extract_dependencies(str(go_project / "go.mod"), str(go_project))
        import_edges = [e for e in edges if e.relation == "imports"]
        indirect = [e for e in import_edges if "indirect" in e.target]
        direct = [e for e in import_edges if e.target == "github.com/cosmos/cosmos-sdk"]
        assert len(direct) == 1
        assert direct[0].confidence == 1.0
        indirect_dep = [
            e for e in import_edges if e.target == "github.com/indirect/dep"
        ]
        assert len(indirect_dep) == 1
        assert indirect_dep[0].confidence < 1.0

    def test_module_name_as_file_node(self, go_project):
        nodes, edges = extract_dependencies(str(go_project / "go.mod"), str(go_project))
        file_nodes = [n for n in nodes if n.type == "file"]
        assert any("github.com/torus-chain/torus" in n.name for n in file_nodes)

    def test_replace_directive_captured(self, go_project):
        nodes, edges = extract_dependencies(str(go_project / "go.mod"), str(go_project))
        impl_edges = [e for e in edges if e.relation == "implements"]
        assert any(
            "cosmos/cosmos-sdk" in e.source and "cosmos-sdk-fork" in e.target
            for e in impl_edges
        )

    def test_versions_not_in_target(self, go_project):
        nodes, edges = extract_dependencies(str(go_project / "go.mod"), str(go_project))
        import_edges = [e for e in edges if e.relation == "imports"]
        for e in import_edges:
            assert " v" not in e.target
            assert not e.target.endswith("v0.50.0")


class TestCargoToml:
    def test_extracts_dependencies(self, cargo_project):
        nodes, edges = extract_dependencies(
            str(cargo_project / "Cargo.toml"), str(cargo_project)
        )
        import_edges = [e for e in edges if e.relation == "imports"]
        targets = {e.target for e in import_edges}
        assert "serde" in targets
        assert "tokio" in targets

    def test_path_dependency_resolved(self, cargo_project):
        nodes, edges = extract_dependencies(
            str(cargo_project / "Cargo.toml"), str(cargo_project)
        )
        import_edges = [e for e in edges if e.relation == "imports"]
        local = [e for e in import_edges if e.target == "local-lib"]
        assert len(local) == 1

    def test_dev_dependencies_lower_confidence(self, cargo_project):
        nodes, edges = extract_dependencies(
            str(cargo_project / "Cargo.toml"), str(cargo_project)
        )
        import_edges = [e for e in edges if e.relation == "imports"]
        dev = [e for e in import_edges if e.target == "tempfile"]
        assert len(dev) == 1
        assert dev[0].confidence < 1.0

    def test_package_name_as_file_node(self, cargo_project):
        nodes, edges = extract_dependencies(
            str(cargo_project / "Cargo.toml"), str(cargo_project)
        )
        file_nodes = [n for n in nodes if n.type == "file"]
        assert any("my-crate" in n.name for n in file_nodes)


class TestPyprojectToml:
    def test_extracts_dependencies(self, python_project):
        nodes, edges = extract_dependencies(
            str(python_project / "pyproject.toml"), str(python_project)
        )
        import_edges = [e for e in edges if e.relation == "imports"]
        targets = {e.target for e in import_edges}
        assert "surrealdb" in targets
        assert "numpy" in targets
        assert "requests" in targets

    def test_optional_deps_lower_confidence(self, python_project):
        nodes, edges = extract_dependencies(
            str(python_project / "pyproject.toml"), str(python_project)
        )
        import_edges = [e for e in edges if e.relation == "imports"]
        dev = [e for e in import_edges if e.target == "pytest"]
        assert len(dev) == 1
        assert dev[0].confidence < 1.0

    def test_package_name_as_file_node(self, python_project):
        nodes, edges = extract_dependencies(
            str(python_project / "pyproject.toml"), str(python_project)
        )
        file_nodes = [n for n in nodes if n.type == "file"]
        assert any("my-package" in n.name for n in file_nodes)

    def test_version_specs_stripped(self, python_project):
        nodes, edges = extract_dependencies(
            str(python_project / "pyproject.toml"), str(python_project)
        )
        import_edges = [e for e in edges if e.relation == "imports"]
        for e in import_edges:
            assert ">=" not in e.target
            assert "==" not in e.target


class TestDependencyEdgeCases:
    def test_nonexistent_file(self, tmp_path):
        nodes, edges = extract_dependencies(str(tmp_path / "nope.toml"), str(tmp_path))
        assert nodes == []
        assert edges == []

    def test_unsupported_file_type(self, tmp_path):
        (tmp_path / "random.txt").write_text("hello")
        nodes, edges = extract_dependencies(str(tmp_path / "random.txt"), str(tmp_path))
        assert nodes == []
        assert edges == []

    def test_malformed_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[[[invalid toml content")
        nodes, edges = extract_dependencies(str(tmp_path / "Cargo.toml"), str(tmp_path))
        file_nodes = [n for n in nodes if n.type == "file"]
        assert len(file_nodes) <= 1
