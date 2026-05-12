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


class TestCrossFileCallResolution:
    """Tests for import-aware cross-file call target resolution."""

    def test_imported_function_resolves_to_target_file(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function GET() { verifyToken(); }"
        import_map = {"verifyToken": ("lib/jwt-auth.ts", "verifyToken")}
        nodes, edges = extract_typescript_ts(
            src, "app/api/route.ts", "project", import_map=import_map
        )
        calls = [e for e in edges if e.relation == "calls"]
        assert any(e.target == "lib/jwt-auth.ts:verifyToken" for e in calls)
        assert any(e.source == "GET" for e in calls)

    def test_aliased_import_resolves_to_exported_name(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function handler() { check(); }"
        import_map = {"check": ("lib/auth.ts", "verify")}
        nodes, edges = extract_typescript_ts(
            src, "route.ts", "project", import_map=import_map
        )
        calls = [e for e in edges if e.relation == "calls"]
        assert any(e.target == "lib/auth.ts:verify" for e in calls)

    def test_member_call_not_resolved_via_import_map(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function handler() { response.json(); }"
        import_map = {"json": ("utils.ts", "json")}
        nodes, edges = extract_typescript_ts(
            src, "route.ts", "project", import_map=import_map
        )
        calls = [e for e in edges if e.relation == "calls"]
        assert not any("utils.ts:" in e.target for e in calls)
        assert any(e.target == "json" for e in calls)

    def test_non_imported_call_unchanged(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function foo() { localHelper(); }"
        import_map = {"verifyToken": ("lib/auth.ts", "verifyToken")}
        nodes, edges = extract_typescript_ts(
            src, "app.ts", "project", import_map=import_map
        )
        calls = [e for e in edges if e.relation == "calls"]
        assert any(e.target == "localHelper" for e in calls)
        assert not any(":" in e.target for e in calls)

    def test_no_import_map_preserves_existing_behavior(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function foo() { bar(); }"
        nodes, edges = extract_typescript_ts(src, "app.ts", "project")
        calls = [e for e in edges if e.relation == "calls"]
        assert any(e.target == "bar" for e in calls)

    def test_multiple_imported_calls_in_one_function(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function GET() { verifyToken(); connectDB(); console.log('ok'); }"
        import_map = {
            "verifyToken": ("lib/jwt-auth.ts", "verifyToken"),
            "connectDB": ("lib/mongodb.ts", "connectDB"),
        }
        nodes, edges = extract_typescript_ts(
            src, "route.ts", "project", import_map=import_map
        )
        calls = [e for e in edges if e.relation == "calls"]
        assert any(e.target == "lib/jwt-auth.ts:verifyToken" for e in calls)
        assert any(e.target == "lib/mongodb.ts:connectDB" for e in calls)
        assert any(e.target == "log" for e in calls)

    def test_imported_call_in_callback(self):
        from indexer.extractors.typescript_ts import extract_typescript_ts

        src = "function setup() { items.forEach(item => process(item)); }"
        import_map = {"process": ("lib/utils.ts", "process")}
        nodes, edges = extract_typescript_ts(
            src, "app.ts", "project", import_map=import_map
        )
        calls = [e for e in edges if e.relation == "calls"]
        assert any(e.target == "lib/utils.ts:process" for e in calls)


class TestImportMapExtraction:
    """Tests for _extract_import_map in typescript.py."""

    def test_named_imports(self, tmp_path):
        from indexer.extractors.typescript import _extract_import_map

        content = 'import { verifyToken, requireAdmin } from "./lib/jwt-auth";'
        result = _extract_import_map(content, str(tmp_path), str(tmp_path))
        assert "verifyToken" in result
        assert "requireAdmin" in result
        assert result["verifyToken"][1] == "verifyToken"

    def test_aliased_import(self, tmp_path):
        from indexer.extractors.typescript import _extract_import_map

        content = 'import { verify as check } from "./auth";'
        result = _extract_import_map(content, str(tmp_path), str(tmp_path))
        assert "check" in result
        assert result["check"][1] == "verify"
        assert "verify" not in result

    def test_default_import(self, tmp_path):
        from indexer.extractors.typescript import _extract_import_map

        content = 'import connectDB from "./lib/mongodb";'
        result = _extract_import_map(content, str(tmp_path), str(tmp_path))
        assert "connectDB" in result
        assert result["connectDB"][1] == "connectDB"

    def test_combined_import(self, tmp_path):
        from indexer.extractors.typescript import _extract_import_map

        content = 'import React, { useState, useEffect } from "react";'
        result = _extract_import_map(content, str(tmp_path), str(tmp_path))
        assert "React" in result
        assert "useState" in result
        assert "useEffect" in result

    def test_type_imports_skipped(self, tmp_path):
        from indexer.extractors.typescript import _extract_import_map

        content = 'import type { UserType } from "./models";'
        result = _extract_import_map(content, str(tmp_path), str(tmp_path))
        assert "UserType" not in result

    def test_inline_type_binding_skipped(self, tmp_path):
        from indexer.extractors.typescript import _extract_import_map

        content = 'import { type UserType, connectDB } from "./lib";'
        result = _extract_import_map(content, str(tmp_path), str(tmp_path))
        assert "UserType" not in result
        assert "connectDB" in result

    def test_at_alias_resolution(self, tmp_path):
        from indexer.extractors.typescript import _extract_import_map

        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "auth.ts").write_text("export function verify() {}")
        content = 'import { verify } from "@/lib/auth";'
        result = _extract_import_map(
            content, str(tmp_path / "app" / "api"), str(tmp_path)
        )
        assert "verify" in result
        assert "lib/auth.ts" in result["verify"][0]


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
