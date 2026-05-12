"""Tests for tree-sitter base extractor utilities."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.extractors import Node, Edge


class TestTsParse:
    def test_parse_returns_tree(self):
        from indexer.extractors.treesitter_base import ts_parse

        tree = ts_parse("def foo(): pass", "python")
        assert tree is not None
        assert tree.root_node.type == "module"

    def test_unsupported_language_returns_none(self):
        from indexer.extractors.treesitter_base import ts_parse

        tree = ts_parse("x", "brainfuck")
        assert tree is None

    def test_typescript_parse(self):
        from indexer.extractors.treesitter_base import ts_parse

        tree = ts_parse("function foo() {}", "typescript")
        assert tree is not None
        assert tree.root_node.type == "program"

    def test_rust_parse(self):
        from indexer.extractors.treesitter_base import ts_parse

        tree = ts_parse("fn main() {}", "rust")
        assert tree is not None
        assert tree.root_node.type == "source_file"

    def test_go_parse(self):
        from indexer.extractors.treesitter_base import ts_parse

        tree = ts_parse("package main\nfunc main() {}", "go")
        assert tree is not None
        assert tree.root_node.type == "source_file"

    def test_empty_source(self):
        from indexer.extractors.treesitter_base import ts_parse

        tree = ts_parse("", "python")
        assert tree is not None


class TestExtractFunctions:
    def test_python_functions(self):
        from indexer.extractors.treesitter_base import ts_extract_functions

        nodes = ts_extract_functions("def foo():\n  pass\ndef bar():\n  pass", "python")
        assert {n.name for n in nodes} == {"foo", "bar"}

    def test_typescript_functions(self):
        from indexer.extractors.treesitter_base import ts_extract_functions

        nodes = ts_extract_functions(
            "function foo() {}\nfunction bar() {}", "typescript"
        )
        assert {n.name for n in nodes} == {"foo", "bar"}

    def test_rust_functions(self):
        from indexer.extractors.treesitter_base import ts_extract_functions

        nodes = ts_extract_functions("fn foo() {}\nfn bar() {}", "rust")
        assert {n.name for n in nodes} == {"foo", "bar"}

    def test_go_functions(self):
        from indexer.extractors.treesitter_base import ts_extract_functions

        nodes = ts_extract_functions("package main\nfunc foo() {}\nfunc bar() {}", "go")
        assert {n.name for n in nodes} == {"foo", "bar"}

    def test_line_numbers(self):
        from indexer.extractors.treesitter_base import ts_extract_functions

        nodes = ts_extract_functions("def foo():\n  pass\ndef bar():\n  pass", "python")
        foo = next(n for n in nodes if n.name == "foo")
        bar = next(n for n in nodes if n.name == "bar")
        assert foo.line == 1
        assert bar.line == 3


class TestExtractClasses:
    def test_python_classes(self):
        from indexer.extractors.treesitter_base import ts_extract_classes

        nodes = ts_extract_classes("class Foo:\n  pass\nclass Bar:\n  pass", "python")
        assert {n.name for n in nodes} == {"Foo", "Bar"}

    def test_typescript_classes(self):
        from indexer.extractors.treesitter_base import ts_extract_classes

        nodes = ts_extract_classes("class Foo {}\nclass Bar {}", "typescript")
        assert {n.name for n in nodes} == {"Foo", "Bar"}

    def test_rust_structs(self):
        from indexer.extractors.treesitter_base import ts_extract_classes

        nodes = ts_extract_classes("struct Foo {}\nstruct Bar { x: i32 }", "rust")
        assert {n.name for n in nodes} == {"Foo", "Bar"}

    def test_all_class_type(self):
        from indexer.extractors.treesitter_base import ts_extract_classes

        nodes = ts_extract_classes("class Foo {}", "typescript")
        assert all(n.type == "class" for n in nodes)


class TestExtractCalls:
    def test_simple_calls(self):
        from indexer.extractors.treesitter_base import ts_extract_calls

        edges = ts_extract_calls(
            "function foo() { bar(); }\nfunction bar() {}",
            "typescript",
        )
        calls = [e for e in edges if e.relation == "calls"]
        assert any(e.source == "foo" and e.target == "bar" for e in calls)

    def test_call_line_numbers(self):
        from indexer.extractors.treesitter_base import ts_extract_calls

        edges = ts_extract_calls(
            "function foo() { bar(); }\nfunction bar() {}",
            "typescript",
        )
        call = next(e for e in edges if e.target == "bar")
        assert call.source_line > 0

    def test_unsupported_language_returns_empty(self):
        from indexer.extractors.treesitter_base import ts_extract_calls

        edges = ts_extract_calls("x", "brainfuck")
        assert edges == []
