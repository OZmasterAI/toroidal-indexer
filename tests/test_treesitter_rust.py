"""Tests for tree-sitter Rust extractor."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.extractors import Edge, Node


class TestExtractFunctions:
    def test_extracts_functions(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "pub fn serve(port: u16) -> Result<()> { Ok(()) }"
        nodes, edges = extract_rust_ts(src, "src/main.rs", "project")
        assert any(n.name == "serve" and n.type == "function" for n in nodes)

    def test_extracts_async_functions(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "pub async fn handle(req: Request) -> Response { todo!() }"
        nodes, edges = extract_rust_ts(src, "src/handler.rs", "project")
        assert any(n.name == "handle" and n.type == "function" for n in nodes)

    def test_function_line_numbers(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "fn foo() {}\n\nfn bar() {}"
        nodes, edges = extract_rust_ts(src, "src/lib.rs", "project")
        foo = next(n for n in nodes if n.name == "foo")
        bar = next(n for n in nodes if n.name == "bar")
        assert foo.line == 1
        assert bar.line == 3


class TestExtractStructs:
    def test_extracts_structs(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "struct Server { port: u16 }"
        nodes, edges = extract_rust_ts(src, "src/server.rs", "project")
        assert any(n.name == "Server" and n.type == "class" for n in nodes)

    def test_extracts_enums(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "enum Color { Red, Green, Blue }"
        nodes, edges = extract_rust_ts(src, "src/color.rs", "project")
        assert any(n.name == "Color" and n.type == "class" for n in nodes)


class TestExtractImpl:
    def test_extracts_trait_impl(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "struct Server {}\nimpl Handler for Server {\n  fn handle(&self) {}\n}"
        nodes, edges = extract_rust_ts(src, "src/server.rs", "project")
        impl_edges = [e for e in edges if e.relation == "implements"]
        assert any(e.source == "Server" and e.target == "Handler" for e in impl_edges)

    def test_extracts_methods_in_impl(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "struct Foo {}\nimpl Foo {\n  fn bar(&self) {}\n  fn baz(&mut self) {}\n}"
        nodes, edges = extract_rust_ts(src, "src/foo.rs", "project")
        func_names = {n.name for n in nodes if n.type == "function"}
        assert "bar" in func_names
        assert "baz" in func_names


class TestExtractCalls:
    def test_extracts_nested_calls(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "fn foo() { let x = bar(baz()); }"
        nodes, edges = extract_rust_ts(src, "src/lib.rs", "project")
        calls = {e.target for e in edges if e.relation == "calls"}
        assert "bar" in calls
        assert "baz" in calls

    def test_extracts_method_calls(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = 'fn main() { server.listen(8080); println!("done"); }'
        nodes, edges = extract_rust_ts(src, "src/main.rs", "project")
        calls = {e.target for e in edges if e.relation == "calls"}
        assert "listen" in calls

    def test_extracts_chained_calls(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "fn run() { builder.config().build().start(); }"
        nodes, edges = extract_rust_ts(src, "src/main.rs", "project")
        calls = {e.target for e in edges if e.relation == "calls"}
        assert "config" in calls
        assert "build" in calls
        assert "start" in calls

    def test_call_scope_attribution(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "fn outer() { inner(); }\nfn inner() {}"
        nodes, edges = extract_rust_ts(src, "src/lib.rs", "project")
        call = next(e for e in edges if e.target == "inner" and e.relation == "calls")
        assert call.source == "outer"


class TestFileNode:
    def test_file_node_present(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        src = "fn main() {}"
        nodes, edges = extract_rust_ts(src, "src/main.rs", "project")
        file_nodes = [n for n in nodes if n.type == "file"]
        assert len(file_nodes) == 1

    def test_empty_source(self):
        from indexer.extractors.rust_ts import extract_rust_ts

        nodes, edges = extract_rust_ts("", "src/empty.rs", "project")
        assert isinstance(nodes, list)
        assert isinstance(edges, list)


class TestMergeWithRegex:
    def test_regex_still_resolves_use_paths(self, tmp_path):
        from indexer.extractors.rust import extract_rust

        src = "use crate::utils::helper;\nfn main() { helper(); }"
        crate_dir = tmp_path / "src"
        crate_dir.mkdir()
        f = crate_dir / "main.rs"
        f.write_text(src)

        nodes, edges = extract_rust(str(f), str(tmp_path))
        imports = [e for e in edges if e.relation == "imports"]
        assert len(imports) >= 1

    def test_merged_output_has_calls(self, tmp_path):
        from indexer.extractors.rust import extract_rust

        src = "use std::io;\nfn run() { process(); }\nfn process() {}"
        f = tmp_path / "main.rs"
        f.write_text(src)

        nodes, edges = extract_rust(str(f), str(tmp_path))
        relations = {e.relation for e in edges}
        assert "imports" in relations
        assert "calls" in relations
