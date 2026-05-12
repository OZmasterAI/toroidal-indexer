"""Tests for execution flow detection (process nodes, step edges, MCP integration)."""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.mcp_queries import code_detect_changes, code_processes
from indexer.process_detector import (
    deduplicate_traces,
    detect_processes,
    find_entry_points,
    make_process_label,
    store_processes,
    trace_from_entry_point,
)
from indexer.schema import (
    connect_code_graph,
    init_code_tables,
    relate,
    upsert_node,
)
from surrealdb import RecordID

SURREAL_URL = "ws://127.0.0.1:8822"


@pytest.fixture(scope="module")
def db():
    """Connect to SurrealDB with a unique test database, seed a graph with flows."""
    test_db = f"test_proc_{uuid.uuid4().hex[:8]}"
    conn = connect_code_graph(url=SURREAL_URL, database=test_db)
    init_code_tables(conn)

    # Build a graph with clear entry points and call chains:
    #   handler() --calls--> auth() --calls--> db_query() --calls--> connect()
    #   handler() --calls--> validate()
    #   scheduler() --calls--> calculate() --calls--> db_query()
    #   scheduler() --calls--> notify()
    #   leaf() has no outgoing calls (not an entry point)
    #   test_fn() is in a test file (excluded)
    handler = upsert_node(conn, "proj", "api/route.ts", "handler", "function", 10)
    auth = upsert_node(conn, "proj", "lib/auth.ts", "auth", "function", 5)
    db_query = upsert_node(conn, "proj", "lib/db.ts", "db_query", "function", 20)
    db_connect = upsert_node(conn, "proj", "lib/db.ts", "connect", "function", 1)
    validate = upsert_node(conn, "proj", "lib/validate.ts", "validate", "function", 8)
    scheduler = upsert_node(
        conn, "proj", "jobs/scheduler.ts", "scheduler", "function", 1
    )
    calculate = upsert_node(conn, "proj", "jobs/calc.ts", "calculate", "function", 15)
    notify = upsert_node(conn, "proj", "jobs/notify.ts", "notify", "function", 3)
    respond = upsert_node(conn, "proj", "lib/respond.ts", "respond", "function", 12)
    leaf = upsert_node(conn, "proj", "lib/utils.ts", "leaf", "function", 1)
    test_fn = upsert_node(
        conn, "proj", "tests/test_api.ts", "test_handler", "function", 1
    )

    relate(conn, handler, "calls", auth)
    relate(conn, handler, "calls", validate)
    relate(conn, handler, "calls", respond)
    relate(conn, auth, "calls", db_query)
    relate(conn, db_query, "calls", db_connect)
    relate(conn, scheduler, "calls", calculate)
    relate(conn, scheduler, "calls", notify)
    relate(conn, calculate, "calls", db_query)
    relate(conn, test_fn, "calls", handler)
    relate(conn, test_fn, "calls", auth)

    yield conn
    conn.query(f"REMOVE DATABASE IF EXISTS {test_db}")


class TestSchema:
    def test_process_table_exists(self, db):
        result = db.query("INFO FOR TABLE code_process")
        assert result is not None

    def test_step_table_exists(self, db):
        result = db.query("INFO FOR TABLE step_in_process")
        assert result is not None

    def test_process_project_index(self, db):
        info = db.query("INFO FOR TABLE code_process")
        assert "process_project" in str(info)


class TestFindEntryPoints:
    def test_finds_entry_points(self, db):
        eps = find_entry_points(db, "proj")
        names = [ep["name"] for ep in eps]
        assert "handler" in names
        assert "scheduler" in names

    def test_excludes_test_files(self, db):
        eps = find_entry_points(db, "proj")
        names = [ep["name"] for ep in eps]
        assert "test_handler" not in names

    def test_excludes_leaves(self, db):
        eps = find_entry_points(db, "proj")
        names = [ep["name"] for ep in eps]
        assert "leaf" not in names

    def test_score_calculation(self, db):
        eps = find_entry_points(db, "proj")
        for ep in eps:
            assert ep["score"] > 1.0
            assert ep["out_calls"] >= 2

    def test_sorted_by_score(self, db):
        eps = find_entry_points(db, "proj")
        scores = [ep["score"] for ep in eps]
        assert scores == sorted(scores, reverse=True)

    def test_returns_empty_for_unknown_project(self, db):
        eps = find_entry_points(db, "nonexistent")
        assert eps == []


class TestTraceFromEntryPoint:
    def test_produces_traces(self, db):
        eps = find_entry_points(db, "proj")
        handler_ep = next(ep for ep in eps if ep["name"] == "handler")
        from indexer.schema import _node_key

        rid = RecordID(
            "code_node", _node_key("proj", handler_ep["file"], handler_ep["name"])
        )
        traces = trace_from_entry_point(db, rid)
        assert len(traces) >= 1

    def test_min_3_steps(self, db):
        eps = find_entry_points(db, "proj")
        handler_ep = next(ep for ep in eps if ep["name"] == "handler")
        from indexer.schema import _node_key

        rid = RecordID(
            "code_node", _node_key("proj", handler_ep["file"], handler_ep["name"])
        )
        traces = trace_from_entry_point(db, rid)
        assert all(len(t) >= 3 for t in traces)

    def test_trace_starts_with_entry(self, db):
        eps = find_entry_points(db, "proj")
        handler_ep = next(ep for ep in eps if ep["name"] == "handler")
        from indexer.schema import _node_key

        rid = RecordID(
            "code_node", _node_key("proj", handler_ep["file"], handler_ep["name"])
        )
        traces = trace_from_entry_point(db, rid)
        for t in traces:
            assert t[0]["name"] == "handler"

    def test_respects_max_depth(self, db):
        eps = find_entry_points(db, "proj")
        handler_ep = next(ep for ep in eps if ep["name"] == "handler")
        from indexer.schema import _node_key

        rid = RecordID(
            "code_node", _node_key("proj", handler_ep["file"], handler_ep["name"])
        )
        traces = trace_from_entry_point(db, rid, max_depth=3)
        for t in traces:
            assert len(t) <= 3


class TestDeduplicateTraces:
    def test_removes_subsets(self):
        traces = [[1, 2, 3], [1, 2, 3, 4], [1, 2], [5, 6, 7]]
        deduped = deduplicate_traces(traces)
        assert [1, 2, 3, 4] in deduped
        assert [1, 2] not in deduped
        assert [5, 6, 7] in deduped

    def test_keeps_longest_per_pair(self):
        traces = [[1, 2, 3], [1, 2, 3, 4, 5]]
        deduped = deduplicate_traces(traces)
        assert len(deduped) == 1
        assert [1, 2, 3, 4, 5] in deduped

    def test_keeps_unrelated(self):
        traces = [[1, 2, 3], [4, 5, 6]]
        deduped = deduplicate_traces(traces)
        assert len(deduped) == 2

    def test_empty_input(self):
        assert deduplicate_traces([]) == []

    def test_with_dict_traces(self):
        t1 = [
            {"name": "a", "file": "x"},
            {"name": "b", "file": "y"},
            {"name": "c", "file": "z"},
        ]
        t2 = [
            {"name": "a", "file": "x"},
            {"name": "b", "file": "y"},
            {"name": "c", "file": "z"},
            {"name": "d", "file": "w"},
        ]
        deduped = deduplicate_traces([t1, t2])
        assert len(deduped) == 1
        assert len(deduped[0]) == 4


class TestMakeProcessLabel:
    def test_label_format(self):
        trace = [{"name": "start"}, {"name": "middle"}, {"name": "end"}]
        label = make_process_label(trace)
        assert label == "start → end"

    def test_arrow_in_label(self):
        trace = [{"name": "A"}, {"name": "B"}]
        label = make_process_label(trace)
        assert "→" in label


class TestDetectProcesses:
    def test_detects_processes(self, db):
        procs = detect_processes(db, "proj")
        assert len(procs) >= 1

    def test_process_structure(self, db):
        procs = detect_processes(db, "proj")
        for p in procs:
            assert "label" in p
            assert "step_count" in p
            assert "cross_community" in p
            assert "trace" in p
            assert p["step_count"] >= 3

    def test_returns_empty_for_unknown_project(self, db):
        procs = detect_processes(db, "nonexistent")
        assert procs == []


class TestStoreProcesses:
    def test_stores_processes(self, db):
        procs = detect_processes(db, "proj")
        store_processes(db, "proj", procs)
        rows = db.query("SELECT * FROM code_process WHERE project='proj'")
        assert len(rows) > 0

    def test_stores_steps(self, db):
        procs = detect_processes(db, "proj")
        store_processes(db, "proj", procs)
        steps = db.query("SELECT * FROM step_in_process WHERE in.project='proj'")
        assert len(steps) > 0

    def test_step_has_order(self, db):
        procs = detect_processes(db, "proj")
        store_processes(db, "proj", procs)
        steps = db.query("SELECT * FROM step_in_process WHERE in.project='proj'")
        for s in steps:
            assert "step_order" in s
            assert isinstance(s["step_order"], int)

    def test_idempotent(self, db):
        procs = detect_processes(db, "proj")
        store_processes(db, "proj", procs)
        count1 = db.query(
            "SELECT count() FROM code_process WHERE project='proj' GROUP ALL"
        )
        store_processes(db, "proj", procs)
        count2 = db.query(
            "SELECT count() FROM code_process WHERE project='proj' GROUP ALL"
        )
        assert count1[0]["count"] == count2[0]["count"]

    def test_relate_direction(self, db):
        """step_in_process edges go code_process->step_in_process->code_node."""
        procs = detect_processes(db, "proj")
        store_processes(db, "proj", procs)
        edges = db.query(
            "SELECT in, out FROM step_in_process WHERE in.project='proj' LIMIT 5"
        )
        for e in edges:
            assert "code_process" in str(e["in"])
            assert "code_node" in str(e["out"])


class TestCodeProcesses:
    def test_returns_flows(self, db):
        procs = detect_processes(db, "proj")
        store_processes(db, "proj", procs)
        result = code_processes(db, "proj")
        assert len(result) > 0
        assert "label" in result[0]
        assert "steps" in result[0]

    def test_query_filter(self, db):
        procs = detect_processes(db, "proj")
        store_processes(db, "proj", procs)
        result = code_processes(db, "proj", query="handler")
        for r in result:
            assert "handler" in r["label"].lower()

    def test_limit(self, db):
        procs = detect_processes(db, "proj")
        store_processes(db, "proj", procs)
        result = code_processes(db, "proj", limit=1)
        assert len(result) <= 1

    def test_empty_for_unknown_project(self, db):
        result = code_processes(db, "nonexistent")
        assert result == []


class TestDetectChangesFlowEnrichment:
    def test_has_flows_affected_key(self, db):
        procs = detect_processes(db, "proj")
        store_processes(db, "proj", procs)
        result = code_detect_changes(db, "proj", "/nonexistent/path")
        assert "flows_affected" in result

    def test_summary_has_flows_hit(self, db):
        result = code_detect_changes(db, "proj", "/nonexistent/path")
        assert "flows_hit" in result["summary"]
