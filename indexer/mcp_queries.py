"""Toroidal-Indexer: MCP query functions for structural code graph.

Standalone query functions that wrap SurrealDB graph traversals.
MCP tool decorators will be added in memory_server.py (Task 10).
"""

from collections import deque

from surrealdb import RecordID

from indexer.schema import (
    _node_key,
    get_callers as _schema_callers,
    get_readers as _schema_readers,
)


def _make_rid(project, file, name):
    """Build a RecordID for a code_node from project/file/name."""
    return RecordID("code_node", _node_key(project, file, name))


def code_callers(db, project, file, function, depth=1):
    """Who calls this function. Returns list of {name, file, line, confidence}."""
    rid = _make_rid(project, file, function)
    return _schema_callers(db, rid, depth=depth)


def code_readers(db, project, file, field):
    """Who reads this field/key. Returns list of {name, file, line, confidence}."""
    rid = _make_rid(project, file, field)
    return _schema_readers(db, rid)


def code_path(db, project, from_file, from_name, to_file, to_name):
    """Shortest path between two nodes via BFS over ->calls-> edges.

    Returns list of {name, file, line} nodes from source to target,
    or empty list if no path exists.
    """
    src = _make_rid(project, from_file, from_name)
    dst = _make_rid(project, to_file, to_name)

    if str(src) == str(dst):
        node = db.query("SELECT name, file, line FROM $id", {"id": src})
        if node:
            return [
                {
                    "name": node[0]["name"],
                    "file": node[0]["file"],
                    "line": node[0]["line"],
                }
            ]
        return []

    # BFS: queue holds node RecordIDs, parent tracks the path
    visited = {str(src)}
    queue = deque([src])
    parent = {}  # str(child_rid) -> parent_rid

    while queue:
        current = queue.popleft()
        # Get forward neighbors via calls edges
        rows = db.query(
            "SELECT ->calls->code_node AS targets FROM $id",
            {"id": current},
        )
        if not rows or not rows[0].get("targets"):
            continue
        for target in rows[0]["targets"]:
            # target may be a RecordID or a dict with id field
            if isinstance(target, dict):
                tid = target.get("id", target)
            else:
                tid = target
            tid_str = str(tid)
            if tid_str in visited:
                continue
            visited.add(tid_str)
            parent[tid_str] = current
            if tid_str == str(dst):
                # Reconstruct path
                path_rids = [dst]
                cur = dst
                while str(cur) in parent:
                    cur = parent[str(cur)]
                    path_rids.append(cur)
                path_rids.reverse()
                # Fetch node details for each rid in path
                result = []
                for rid in path_rids:
                    node = db.query("SELECT name, file, line FROM $id", {"id": rid})
                    if node:
                        result.append(
                            {
                                "name": node[0]["name"],
                                "file": node[0]["file"],
                                "line": node[0]["line"],
                            }
                        )
                return result
            queue.append(tid)

    return []


def code_blast_radius(db, project, file, function, depth=3):
    """Transitive dependents -- everything downstream that could break if this changes.

    Forward traversal via ->calls-> edges up to `depth` hops.
    Returns list of {name, file, line} for all reachable nodes (excludes the source).
    """
    src = _make_rid(project, file, function)
    visited = {str(src)}
    frontier = [src]
    collected = []

    for _ in range(depth):
        next_frontier = []
        for nid in frontier:
            rows = db.query(
                "SELECT ->calls->code_node AS targets FROM $id",
                {"id": nid},
            )
            if not rows or not rows[0].get("targets"):
                continue
            for target in rows[0]["targets"]:
                if isinstance(target, dict):
                    tid = target.get("id", target)
                else:
                    tid = target
                tid_str = str(tid)
                if tid_str in visited:
                    continue
                visited.add(tid_str)
                # Fetch node details
                node = db.query("SELECT name, file, line FROM $id", {"id": tid})
                if node:
                    collected.append(
                        {
                            "name": node[0]["name"],
                            "file": node[0]["file"],
                            "line": node[0]["line"],
                        }
                    )
                next_frontier.append(tid)
        frontier = next_frontier

    return collected


def code_search(db, project, query, limit=15):
    """Fuzzy search: find nodes by substring match on name or file path.

    Splits query into terms and matches any term against node name or file.
    Returns list of {name, file, line, type} sorted by relevance.
    """
    raw_terms = [t.strip().lower() for t in query.split() if len(t.strip()) >= 2]
    if not raw_terms:
        return []

    stop = {
        "the",
        "and",
        "for",
        "how",
        "does",
        "what",
        "show",
        "find",
        "all",
        "this",
        "with",
        "from",
    }
    terms = []
    seen_terms = set()
    for t in raw_terms:
        if t in stop:
            continue
        for candidate in (t, t[: max(4, len(t) // 2)], t[:4]):
            if len(candidate) >= 3 and candidate not in seen_terms:
                seen_terms.add(candidate)
                terms.append(candidate)
    if not terms:
        return []

    conditions = []
    params = {"proj": project, "lim": limit}
    for i, term in enumerate(terms[:8]):
        key = f"t{i}"
        params[key] = term
        conditions.append(
            f"(string::lowercase(name) CONTAINS ${key} OR string::lowercase(file) CONTAINS ${key})"
        )

    where = " OR ".join(conditions)
    rows = db.query(
        f"SELECT name, file, line, type FROM code_node "
        f"WHERE project=$proj AND ({where}) LIMIT $lim",
        params,
    )
    if not rows:
        return []
    seen = set()
    results = []
    for r in rows:
        key = (r["name"], r["file"])
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "name": r["name"],
                "file": r["file"],
                "line": r.get("line", 0),
                "type": r.get("type", "unknown"),
            }
        )
    return results


def code_hubs(db, project, top_n=10):
    """Most-connected nodes in the project, sorted by total edge degree descending.

    Returns list of {name, file, degree}.
    """
    rows = db.query(
        """SELECT id, name, file,
            array::len(->calls->code_node) + array::len(<-calls<-code_node) +
            array::len(->imports->code_node) + array::len(<-imports<-code_node) +
            array::len(->reads->code_node) + array::len(<-reads<-code_node) +
            array::len(->writes->code_node) + array::len(<-writes<-code_node) +
            array::len(->implements->code_node) + array::len(<-implements<-code_node) AS degree
        FROM code_node WHERE project=$p ORDER BY degree DESC LIMIT $n""",
        {"p": project, "n": top_n},
    )
    if not rows:
        return []
    return [
        {"name": r["name"], "file": r["file"], "degree": r["degree"]}
        for r in rows
        if r.get("degree", 0) > 0
    ]


def code_clusters(db, project):
    """All clusters for a project with labels, node counts, and top members.

    Returns list of {label, node_count, key_files, key_functions}.
    """
    rows = db.query(
        "SELECT label, node_count, key_files, key_functions "
        "FROM code_cluster WHERE project=$p ORDER BY node_count DESC",
        {"p": project},
    )
    if not rows:
        return []
    return [
        {
            "label": r["label"],
            "node_count": r["node_count"],
            "key_files": r.get("key_files", []),
            "key_functions": r.get("key_functions", []),
        }
        for r in rows
    ]


def code_cluster_members(db, project, label):
    """All nodes in clusters matching the label (substring match).

    Returns list of {name, file, type, line, cluster_label}.
    """
    rows = db.query(
        "SELECT name, file, type, line, cluster_label "
        "FROM code_node WHERE project=$p AND cluster_label IS NOT NONE "
        "AND string::lowercase(cluster_label) CONTAINS string::lowercase($label)",
        {"p": project, "label": label},
    )
    if not rows:
        return []
    return [
        {
            "name": r["name"],
            "file": r["file"],
            "type": r.get("type", "unknown"),
            "line": r.get("line", 0),
            "cluster_label": r.get("cluster_label", ""),
        }
        for r in rows
    ]
