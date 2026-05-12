"""Execution flow detection: find entry points, trace call chains, store as process nodes."""

from collections import deque

from surrealdb import RecordID

from indexer.schema import _node_key


def find_entry_points(db, project, limit=100):
    """Find functions with high outgoing/incoming call ratio (likely entry points).

    Score = outgoing_calls / (incoming_calls + 1). Filter: score > 1.0, outgoing >= 2.
    Excludes test files. Returns sorted by score desc, capped at limit.
    """
    rows = db.query(
        "SELECT id, name, file, line, type, "
        "  array::len(->calls->code_node) AS out_calls, "
        "  array::len(<-calls<-code_node) AS in_calls "
        "FROM code_node WHERE project=$p AND type != 'file'",
        {"p": project},
    )
    if not rows:
        return []

    test_segs = (
        "test/",
        "tests/",
        "spec/",
        "specs/",
        "__tests__/",
        "__test__/",
        "test_",
        ".test.",
        ".spec.",
        "_test.",
        "_spec.",
    )
    candidates = []
    for r in rows:
        out_c = r.get("out_calls", 0) or 0
        in_c = r.get("in_calls", 0) or 0
        if out_c < 2:
            continue
        fpath = (r.get("file") or "").lower()
        if any(seg in fpath for seg in test_segs):
            continue
        score = out_c / (in_c + 1)
        if score > 1.0:
            candidates.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "file": r["file"],
                    "line": r.get("line", 0),
                    "score": score,
                    "out_calls": out_c,
                    "in_calls": in_c,
                }
            )

    candidates.sort(key=lambda x: -x["score"])
    return candidates[:limit]


def trace_from_entry_point(db, entry_rid, max_depth=10, max_branching=3):
    """BFS forward from entry point via ->calls-> edges.

    At each node, follow up to max_branching callees sorted by degree desc.
    Returns list of traces, each trace = list of {name, file, line, id} dicts.
    """
    entry_info = db.query("SELECT name, file, line FROM $id", {"id": entry_rid})
    if not entry_info:
        return []
    entry_node = {
        "name": entry_info[0]["name"],
        "file": entry_info[0]["file"],
        "line": entry_info[0].get("line", 0),
        "id": entry_rid,
    }

    traces = []
    # Each queue item: (current_trace, visited_set)
    queue = deque([([entry_node], {str(entry_rid)})])

    while queue:
        path, visited = queue.popleft()
        if len(path) >= max_depth:
            if len(path) >= 3:
                traces.append(path)
            continue

        current = path[-1]
        rows = db.query(
            "SELECT ->calls->code_node AS callees FROM $id",
            {"id": current["id"]},
        )
        callees_raw = (rows[0].get("callees") or []) if rows else []

        # Resolve callee info and sort by degree (hub preference)
        callees = []
        for c in callees_raw:
            if isinstance(c, dict):
                cid = c.get("id", c)
            else:
                cid = c
            cid_str = str(cid)
            if cid_str in visited:
                continue
            info = db.query(
                "SELECT name, file, line, "
                "  array::len(->calls->code_node) + array::len(<-calls<-code_node) AS degree "
                "FROM $id",
                {"id": cid},
            )
            if info:
                callees.append(
                    {
                        "name": info[0]["name"],
                        "file": info[0]["file"],
                        "line": info[0].get("line", 0),
                        "id": cid,
                        "degree": info[0].get("degree", 0) or 0,
                    }
                )

        callees.sort(key=lambda x: -x["degree"])
        callees = callees[:max_branching]

        if not callees:
            if len(path) >= 3:
                traces.append(path)
            continue

        for callee in callees:
            new_visited = visited | {str(callee["id"])}
            new_path = path + [
                {
                    "name": callee["name"],
                    "file": callee["file"],
                    "line": callee["line"],
                    "id": callee["id"],
                }
            ]
            queue.append((new_path, new_visited))

    return traces


def deduplicate_traces(traces):
    """Remove strict subsets, keep longest per (entry, terminal) pair.

    Works with traces of any element type — uses tuple conversion for set ops.
    """
    if not traces:
        return []

    def _trace_key(t):
        if isinstance(t[0], dict):
            return (t[0].get("name", ""), t[-1].get("name", ""))
        return (t[0], t[-1])

    def _as_set(t):
        if isinstance(t[0], dict):
            return {(n.get("name", ""), n.get("file", "")) for n in t}
        return set(t)

    # Keep longest per (entry, terminal) pair
    best = {}
    for t in traces:
        key = _trace_key(t)
        if key not in best or len(t) > len(best[key]):
            best[key] = t

    remaining = list(best.values())

    # Remove strict subsets
    result = []
    for i, t in enumerate(remaining):
        t_set = _as_set(t)
        is_subset = False
        for j, other in enumerate(remaining):
            if i == j:
                continue
            if len(t) < len(other) and t_set.issubset(_as_set(other)):
                is_subset = True
                break
        if not is_subset:
            result.append(t)

    return result


def make_process_label(trace):
    """Generate label: 'EntryName -> TerminalName'."""
    return f"{trace[0]['name']} → {trace[-1]['name']}"


def detect_processes(db, project):
    """Orchestrate: find entry points, trace, deduplicate, label, flag cross-community."""
    entry_points = find_entry_points(db, project)
    if not entry_points:
        return []

    all_traces = []
    for ep in entry_points:
        rid = RecordID("code_node", _node_key(project, ep["file"], ep["name"]))
        traces = trace_from_entry_point(db, rid)
        all_traces.extend(traces)

    deduped = deduplicate_traces(all_traces)
    if not deduped:
        return []

    # Cap at max(20, node_count // 10)
    node_count_rows = db.query(
        "SELECT count() AS c FROM code_node WHERE project=$p GROUP ALL",
        {"p": project},
    )
    node_count = node_count_rows[0]["c"] if node_count_rows else 0
    cap = max(20, node_count // 10)
    # Sort by length desc to keep longest flows
    deduped.sort(key=lambda t: -len(t))
    deduped = deduped[:cap]

    processes = []
    for trace in deduped:
        label = make_process_label(trace)

        # Check cross-community: query cluster_label on step nodes
        cluster_labels = set()
        for step in trace:
            rows = db.query(
                "SELECT cluster_label FROM $id",
                {"id": step["id"]},
            )
            if rows and rows[0].get("cluster_label"):
                cluster_labels.add(rows[0]["cluster_label"])
        cross_community = len(cluster_labels) > 1

        processes.append(
            {
                "label": label,
                "process_type": "execution_flow",
                "step_count": len(trace),
                "cross_community": cross_community,
                "entry_id": str(trace[0]["id"]),
                "terminal_id": str(trace[-1]["id"]),
                "trace": trace,
            }
        )

    return processes


def store_processes(db, project, processes):
    """Store processes as code_process nodes + step_in_process edges."""
    # Clean existing
    db.query("DELETE code_process WHERE project=$p", {"p": project})
    db.query(
        "DELETE step_in_process WHERE in.project=$p",
        {"p": project},
    )

    for proc in processes:
        proc_key = _node_key(project, proc["entry_id"], proc["label"])
        proc_rid = RecordID("code_process", proc_key)
        db.query(
            "UPSERT $id SET project=$p, label=$label, process_type=$pt, "
            "step_count=$sc, cross_community=$cc, entry_id=$eid, terminal_id=$tid",
            {
                "id": proc_rid,
                "p": project,
                "label": proc["label"],
                "pt": proc["process_type"],
                "sc": proc["step_count"],
                "cc": proc["cross_community"],
                "eid": proc["entry_id"],
                "tid": proc["terminal_id"],
            },
        )

        for order, step in enumerate(proc["trace"]):
            db.query(
                "RELATE $proc->step_in_process->$node SET step_order=$order",
                {
                    "proc": proc_rid,
                    "node": step["id"],
                    "order": order,
                },
            )
