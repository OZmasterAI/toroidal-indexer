"""Tests for tree-sitter Go extractor."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.extractors import Edge, Node


class TestExtractFunctions:
    def test_extracts_functions(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = 'package main\nfunc main() { fmt.Println("hi") }'
        nodes, edges = extract_go_ts(src, "main.go", "project")
        assert any(n.name == "main" and n.type == "function" for n in nodes)

    def test_extracts_multiple_functions(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\nfunc foo() {}\nfunc bar() {}"
        nodes, edges = extract_go_ts(src, "app.go", "project")
        func_names = {n.name for n in nodes if n.type == "function"}
        assert "foo" in func_names
        assert "bar" in func_names

    def test_function_line_numbers(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\n\nfunc foo() {}\n\nfunc bar() {}"
        nodes, edges = extract_go_ts(src, "app.go", "project")
        foo = next(n for n in nodes if n.name == "foo")
        bar = next(n for n in nodes if n.name == "bar")
        assert foo.line < bar.line


class TestExtractTypes:
    def test_extracts_struct(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\ntype Server struct{ port int }"
        nodes, edges = extract_go_ts(src, "server.go", "project")
        assert any(n.name == "Server" and n.type == "class" for n in nodes)

    def test_extracts_interface(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\ntype Handler interface{ Serve() }"
        nodes, edges = extract_go_ts(src, "handler.go", "project")
        assert any(n.name == "Handler" and n.type == "class" for n in nodes)


class TestExtractMethods:
    def test_extracts_method_receivers(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\ntype Server struct{}\nfunc (s *Server) Serve() {}"
        nodes, edges = extract_go_ts(src, "server.go", "project")
        assert any(n.name == "Serve" and n.type == "function" for n in nodes)
        impl_edges = [e for e in edges if e.relation == "implements"]
        assert any(e.source == "Serve" and e.target == "Server" for e in impl_edges)

    def test_value_receiver(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\ntype Keeper struct{}\nfunc (k Keeper) Get() {}"
        nodes, edges = extract_go_ts(src, "keeper.go", "project")
        assert any(n.name == "Get" and n.type == "function" for n in nodes)
        impl_edges = [e for e in edges if e.relation == "implements"]
        assert any(e.source == "Get" and e.target == "Keeper" for e in impl_edges)


class TestExtractCalls:
    def test_extracts_function_calls(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = 'package main\nimport "fmt"\nfunc main() { fmt.Println("hi") }'
        nodes, edges = extract_go_ts(src, "main.go", "project")
        call_targets = {e.target for e in edges if e.relation == "calls"}
        assert "Println" in call_targets

    def test_extracts_simple_calls(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\nfunc run() { process(); }\nfunc process() {}"
        nodes, edges = extract_go_ts(src, "app.go", "project")
        calls = [e for e in edges if e.relation == "calls"]
        assert any(e.source == "run" and e.target == "process" for e in calls)

    def test_extracts_chained_calls(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\nfunc setup() { builder.Config().Build() }"
        nodes, edges = extract_go_ts(src, "app.go", "project")
        call_targets = {e.target for e in edges if e.relation == "calls"}
        assert "Config" in call_targets
        assert "Build" in call_targets

    def test_call_scope_attribution(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\nfunc outer() { inner() }\nfunc inner() {}"
        nodes, edges = extract_go_ts(src, "app.go", "project")
        call = next(e for e in edges if e.target == "inner" and e.relation == "calls")
        assert call.source == "outer"


class TestFileNode:
    def test_file_node_present(self):
        from indexer.extractors.go_ts import extract_go_ts

        src = "package main\nfunc main() {}"
        nodes, edges = extract_go_ts(src, "main.go", "project")
        file_nodes = [n for n in nodes if n.type == "file"]
        assert len(file_nodes) == 1
        assert file_nodes[0].file == "main.go"

    def test_empty_source(self):
        from indexer.extractors.go_ts import extract_go_ts

        nodes, edges = extract_go_ts("", "empty.go", "project")
        assert isinstance(nodes, list)


class TestMergeWithRegex:
    def test_regex_still_resolves_go_mod_imports(self, tmp_path):
        from indexer.extractors.go import extract_go

        (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.21\n")
        src = 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println() }\n'
        f = tmp_path / "main.go"
        f.write_text(src)

        nodes, edges = extract_go(str(f), str(tmp_path))
        assert isinstance(edges, list)
        relations = {e.relation for e in edges}
        assert "imports" in relations

    def test_merged_output_has_calls(self, tmp_path):
        from indexer.extractors.go import extract_go

        (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.21\n")
        src = (
            "package main\n\n"
            'import "fmt"\n\n'
            "func run() { process(); fmt.Println() }\n"
            "func process() {}\n"
        )
        f = tmp_path / "app.go"
        f.write_text(src)

        nodes, edges = extract_go(str(f), str(tmp_path))
        relations = {e.relation for e in edges}
        assert "imports" in relations
        assert "calls" in relations
