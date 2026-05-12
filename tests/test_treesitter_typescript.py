"""Tests for tree-sitter TypeScript extractor."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.extractors import Edge, Node


class TestExtractFunctions:
    def test_extracts_exported_functions(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "export function handleClick(e: Event) { return true; }"
        nodes, edges = extract_typescript_ts(src, "app/page.tsx", "project")
        names = {n.name for n in nodes if n.type == "function"}
        assert "handleClick" in names

    def test_extracts_non_exported_functions(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function helper() { return 1; }"
        nodes, edges = extract_typescript_ts(src, "utils.ts", "project")
        names = {n.name for n in nodes if n.type == "function"}
        assert "helper" in names

    def test_extracts_arrow_functions(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "const greet = (name: string) => console.log(name);"
        nodes, edges = extract_typescript_ts(src, "utils.ts", "project")
        names = {n.name for n in nodes if n.type == "function"}
        assert "greet" in names

    def test_extracts_async_functions(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "export async function fetchData() { return await fetch('/api'); }"
        nodes, edges = extract_typescript_ts(src, "api.ts", "project")
        names = {n.name for n in nodes if n.type == "function"}
        assert "fetchData" in names


class TestExtractClasses:
    def test_extracts_classes_and_methods(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "export class UserService {\n  getUser() { return null; }\n}"
        nodes, edges = extract_typescript_ts(src, "service.ts", "project")
        assert any(n.name == "UserService" and n.type == "class" for n in nodes)
        assert any(n.name == "getUser" and n.type == "function" for n in nodes)

    def test_class_method_has_line_number(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "class Foo {\n  bar() {}\n  baz() {}\n}"
        nodes, edges = extract_typescript_ts(src, "foo.ts", "project")
        bar = next(n for n in nodes if n.name == "bar")
        baz = next(n for n in nodes if n.name == "baz")
        assert bar.line < baz.line


class TestExtractCalls:
    def test_extracts_call_edges(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function foo() { bar(); }\nfunction bar() {}"
        nodes, edges = extract_typescript_ts(src, "utils.ts", "project")
        calls = [e for e in edges if e.relation == "calls"]
        assert any(e.source == "foo" and e.target == "bar" for e in calls)

    def test_extracts_chained_calls(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function process() { foo.bar().baz(); }"
        nodes, edges = extract_typescript_ts(src, "utils.ts", "project")
        call_targets = {e.target for e in edges if e.relation == "calls"}
        assert "bar" in call_targets
        assert "baz" in call_targets

    def test_extracts_method_calls(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function run() { console.log('hi'); }"
        nodes, edges = extract_typescript_ts(src, "app.ts", "project")
        call_targets = {e.target for e in edges if e.relation == "calls"}
        assert "log" in call_targets

    def test_call_inside_callback(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function setup() { items.forEach(item => process(item)); }"
        nodes, edges = extract_typescript_ts(src, "app.ts", "project")
        call_targets = {e.target for e in edges if e.relation == "calls"}
        assert "process" in call_targets
        assert "forEach" in call_targets


class TestExtractJSX:
    def test_extracts_jsx_component_calls(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function App() { return <Button onClick={handleClick} />; }"
        nodes, edges = extract_typescript_ts(src, "App.tsx", "project")
        names = {n.name for n in nodes if n.type == "function"}
        assert "App" in names


class TestFallback:
    def test_returns_none_on_parse_failure(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        result = extract_typescript_ts("", "empty.ts", "project")
        assert result is not None
        nodes, edges = result
        assert isinstance(nodes, list)

    def test_file_node_present(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function foo() {}"
        nodes, edges = extract_typescript_ts(src, "utils.ts", "project")
        file_nodes = [n for n in nodes if n.type == "file"]
        assert len(file_nodes) == 1
        assert file_nodes[0].file == "utils.ts"


class TestMergeWithRegex:
    def test_regex_fallback_when_no_treesitter(self, monkeypatch, tmp_path):
        from indexer.extractors import typescript

        src = 'import { foo } from "./bar";\nexport function baz() {}'
        f = tmp_path / "test.ts"
        f.write_text(src)

        nodes, edges = typescript.extract_typescript(str(f), str(tmp_path))
        assert isinstance(nodes, list)
        assert any(n.name == "baz" for n in nodes)

    def test_merged_output_has_imports_and_calls(self, tmp_path):
        from indexer.extractors.typescript import extract_typescript

        src = (
            'import { helper } from "./lib";\n'
            "function main() { helper(); process(); }\n"
            "function process() {}\n"
        )
        f = tmp_path / "app.ts"
        f.write_text(src)

        nodes, edges = extract_typescript(str(f), str(tmp_path))
        relations = {e.relation for e in edges}
        assert "imports" in relations
        assert "calls" in relations
