"""Toroidal-Indexer: MCP query functions for structural code graph.

Standalone query functions that wrap SurrealDB graph traversals.
MCP tool decorators will be added in memory_server.py (Task 10).
"""

import subprocess
from collections import deque

from surrealdb import RecordID

from indexer.schema import (
    _node_key,
    get_callers as _schema_callers,
    get_readers as _schema_readers,
)
from indexer.embed import embed_query

_TEST_SEGMENTS = (
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


def _is_test_file(filepath):
    lower = filepath.lower()
    return any(seg in lower for seg in _TEST_SEGMENTS)


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
        # Get neighbors via all edge types, both directions
        rows = db.query(
            "SELECT "
            "  ->calls->code_node AS fwd_calls, "
            "  <-calls<-code_node AS rev_calls, "
            "  ->imports->code_node AS fwd_imports, "
            "  <-imports<-code_node AS rev_imports, "
            "  ->reads->code_node AS fwd_reads, "
            "  <-reads<-code_node AS rev_reads "
            "FROM $id",
            {"id": current},
        )
        if not rows:
            continue
        all_neighbors = []
        for key in (
            "fwd_calls",
            "rev_calls",
            "fwd_imports",
            "rev_imports",
            "fwd_reads",
            "rev_reads",
        ):
            all_neighbors.extend(rows[0].get(key) or [])
        for target in all_neighbors:
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
    """Transitive dependents -- everything that could break if this changes.

    Reverse traversal via INCOMING edges (callers, importers, readers, etc.)
    up to `depth` hops. Returns list of {name, file, line} for all reachable
    dependents (excludes the source).

    Seed nodes: the function node AND the file node (imports target files,
    not individual symbols).
    """
    src = _make_rid(project, file, function)
    file_rid = _make_rid(project, file, file)
    visited = {str(src), str(file_rid)}
    frontier = [src, file_rid] if str(src) != str(file_rid) else [src]
    collected = []

    for _ in range(depth):
        next_frontier = []
        for nid in frontier:
            rows = db.query(
                "SELECT "
                "  <-calls<-code_node AS callers, "
                "  <-imports<-code_node AS importers, "
                "  <-reads<-code_node AS readers, "
                "  <-writes<-code_node AS writers, "
                "  <-implements<-code_node AS implementors "
                "FROM $id",
                {"id": nid},
            )
            if not rows:
                continue
            targets = []
            for key in ("callers", "importers", "readers", "writers", "implementors"):
                targets.extend(rows[0].get(key) or [])
            for target in targets:
                if isinstance(target, dict):
                    tid = target.get("id", target)
                else:
                    tid = target
                tid_str = str(tid)
                if tid_str in visited:
                    continue
                visited.add(tid_str)
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


def _bm25_seeds(db, project, terms, limit=10):
    """Substring match scored by term frequency. Returns ranked list of nodes."""
    conditions = []
    params = {"proj": project}
    for i, term in enumerate(terms[:6]):
        key = f"t{i}"
        params[key] = term
        conditions.append(
            f"(string::lowercase(name) CONTAINS ${key} OR string::lowercase(file) CONTAINS ${key})"
        )
    where = " OR ".join(conditions)
    rows = db.query(
        f"SELECT id, name, file, line, type FROM code_node "
        f"WHERE project=$proj AND ({where}) LIMIT {limit}",
        params,
    )
    if not rows:
        return []
    scored = []
    for r in rows:
        score = sum(
            1
            for t in terms
            if t in r.get("name", "").lower() or t in r.get("file", "").lower()
        )
        if _is_test_file(r.get("file", "")):
            score *= 0.5
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored]


def _vector_seeds(db, project, question, limit=10):
    """Embed the question and find nearest code_nodes by cosine similarity."""
    qvec = embed_query(question)
    if qvec is None:
        return []
    try:
        rows = db.query(
            f"SELECT id, name, file, line, type, vector::distance::knn() AS dist "
            f"FROM code_node WHERE embedding <|{limit}, COSINE|> $vec "
            f"AND project=$proj ORDER BY dist ASC",
            {"vec": qvec, "proj": project},
        )
        return rows if rows else []
    except Exception:
        return []


def _rrf_fuse(bm25_ranked, vector_ranked, k=60, top_n=5):
    """Reciprocal Rank Fusion: merge two ranked lists into one.

    Applies top-3-per-file aggregation to prevent files with many mediocre
    matches (e.g. test files) from outranking files with one strong hit.
    """
    scores = {}
    node_data = {}
    for rank, node in enumerate(bm25_ranked):
        nid = str(node["id"])
        scores[nid] = scores.get(nid, 0) + 1.0 / (k + rank)
        node_data[nid] = node
    for rank, node in enumerate(vector_ranked):
        nid = str(node["id"])
        scores[nid] = scores.get(nid, 0) + 1.0 / (k + rank)
        node_data[nid] = node

    # Top-3-per-file: only keep the 3 highest-scoring nodes from each file
    by_file = {}
    for nid, score in scores.items():
        fpath = node_data[nid].get("file", "")
        by_file.setdefault(fpath, []).append((score, nid))
    kept = {}
    for fpath, entries in by_file.items():
        entries.sort(key=lambda x: -x[0])
        for score, nid in entries[:3]:
            kept[nid] = score

    ranked = sorted(kept.items(), key=lambda x: -x[1])
    return [node_data[nid] for nid, _ in ranked[:top_n]]


def code_query(db, project, question, mode="bfs", depth=2, budget=2000):
    """Answer a codebase question by traversing the graph.

    Hybrid seed selection: BM25 substring match + vector similarity,
    fused via Reciprocal Rank Fusion. Falls back to BM25-only if
    embeddings are unavailable.
    """
    terms = [t.lower() for t in question.split() if len(t) > 2]
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
        "where",
        "which",
        "that",
        "are",
    }
    terms = [t for t in terms if t not in stop]
    if not terms:
        return "No meaningful search terms found."

    bm25 = _bm25_seeds(db, project, terms, limit=10)
    vec = _vector_seeds(db, project, question, limit=10)

    if bm25 and vec:
        seeds = _rrf_fuse(bm25, vec, top_n=5)
    elif bm25:
        seeds = bm25[:5]
    elif vec:
        seeds = vec[:5]
    else:
        return f"No nodes matching '{question}'."

    seed_ids = [r["id"] for r in seeds]
    visited = {str(s) for s in seed_ids}
    frontier = list(seed_ids)
    all_nodes = {str(r["id"]): r for r in seeds}
    all_edges = []

    for _ in range(depth):
        next_frontier = []
        for nid in frontier:
            rows = db.query(
                "SELECT "
                "  ->calls->code_node AS fwd_c, <-calls<-code_node AS rev_c, "
                "  ->imports->code_node AS fwd_i, <-imports<-code_node AS rev_i, "
                "  ->reads->code_node AS fwd_r, <-reads<-code_node AS rev_r "
                "FROM $id",
                {"id": nid},
            )
            if not rows:
                continue
            edge_labels = [
                ("fwd_c", "calls"),
                ("rev_c", "called_by"),
                ("fwd_i", "imports"),
                ("rev_i", "imported_by"),
                ("fwd_r", "reads"),
                ("rev_r", "read_by"),
            ]
            for key, label in edge_labels:
                for target in rows[0].get(key) or []:
                    tid = (
                        target.get("id", target) if isinstance(target, dict) else target
                    )
                    tid_str = str(tid)
                    if tid_str not in all_nodes:
                        node = db.query(
                            "SELECT name, file, line, type FROM $id", {"id": tid}
                        )
                        if node:
                            all_nodes[tid_str] = node[0]
                    src_name = all_nodes.get(str(nid), {}).get("name", "?")
                    tgt_name = all_nodes.get(tid_str, {}).get("name", "?")
                    all_edges.append(f"{src_name} --{label}--> {tgt_name}")
                    if tid_str not in visited:
                        visited.add(tid_str)
                        next_frontier.append(tid)
        frontier = next_frontier
        if mode == "dfs":
            frontier = frontier[-3:] if frontier else []

    # Render compact text within budget
    char_budget = budget * 3
    lines = [
        f"Query: {question}",
        f"Mode: {mode.upper()} depth={depth} | {len(all_nodes)} nodes, {len(all_edges)} edges",
        "",
    ]

    lines.append("NODES:")
    for _, data in sorted(
        all_nodes.items(),
        key=lambda x: -len([e for e in all_edges if x[1].get("name", "") in e]),
    ):
        lines.append(
            f"  {data.get('name', '?')} [{data.get('type', '?')}] {data.get('file', '')}:{data.get('line', 0)}"
        )

    lines.append("")
    lines.append("EDGES:")
    seen_edges = set()
    for e in all_edges:
        if e not in seen_edges:
            seen_edges.add(e)
            lines.append(f"  {e}")

    output = "\n".join(lines)
    if len(output) > char_budget:
        output = output[:char_budget] + f"\n... (truncated to ~{budget} tokens)"
    return output


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
    scored = []
    for r in rows:
        key = (r["name"], r["file"])
        if key in seen:
            continue
        seen.add(key)
        score = sum(
            1
            for t in terms
            if t in r.get("name", "").lower() or t in r.get("file", "").lower()
        )
        if _is_test_file(r.get("file", "")):
            score *= 0.5
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "name": r["name"],
            "file": r["file"],
            "line": r.get("line", 0),
            "type": r.get("type", "unknown"),
        }
        for _, r in scored
    ]


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


def _git_changed_files(project_root, base_ref="HEAD~1"):
    """Get files changed between base_ref and HEAD."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_ref],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except Exception:
        return []


def code_detect_changes(db, project, project_root, base_ref="HEAD~1", depth=2):
    """Map git diff to affected symbols via blast radius.

    1. git diff base_ref → changed files
    2. Find all graph symbols in those files
    3. Batch reverse-BFS to find all dependents
    4. Return structured impact with risk level
    """
    changed_files = _git_changed_files(project_root, base_ref)
    if not changed_files:
        return {
            "changed_files": [],
            "changed_symbols": [],
            "affected": [],
            "summary": {
                "files_changed": 0,
                "symbols_changed": 0,
                "total_affected": 0,
                "risk": "NONE",
            },
        }

    # Find all graph nodes in changed files (cap at 20 files)
    changed_symbols = []
    for fpath in changed_files[:20]:
        rows = db.query(
            "SELECT id, name, file, line, type FROM code_node "
            "WHERE project=$p AND file=$f AND type != 'file'",
            {"p": project, "f": fpath},
        )
        if rows:
            changed_symbols.extend(rows[:10])

    if not changed_symbols:
        return {
            "changed_files": changed_files,
            "changed_symbols": [],
            "affected": [],
            "summary": {
                "files_changed": len(changed_files),
                "symbols_changed": 0,
                "total_affected": 0,
                "risk": "LOW",
            },
        }

    # Batch reverse-BFS from all changed symbols at once
    seed_ids = []
    for sym in changed_symbols:
        seed_ids.append(sym["id"])
        file_rid = _make_rid(project, sym["file"], sym["file"])
        seed_ids.append(file_rid)

    visited = {str(s) for s in seed_ids}
    frontier = list({str(s) for s in seed_ids})
    affected = []

    for _ in range(depth):
        if not frontier:
            break
        next_frontier = []
        for nid_str in frontier:
            try:
                rows = db.query(
                    "SELECT "
                    "  <-calls<-code_node AS callers, "
                    "  <-imports<-code_node AS importers, "
                    "  <-reads<-code_node AS readers, "
                    "  <-writes<-code_node AS writers, "
                    "  <-implements<-code_node AS implementors "
                    "FROM $id",
                    {
                        "id": (
                            RecordID.parse(nid_str)
                            if isinstance(nid_str, str)
                            else nid_str
                        )
                    },
                )
            except Exception:
                continue
            if not rows:
                continue
            for key in (
                "callers",
                "importers",
                "readers",
                "writers",
                "implementors",
            ):
                for target in rows[0].get(key) or []:
                    tid = (
                        target.get("id", target) if isinstance(target, dict) else target
                    )
                    tid_str = str(tid)
                    if tid_str in visited:
                        continue
                    visited.add(tid_str)
                    node = db.query(
                        "SELECT name, file, line, type FROM $id", {"id": tid}
                    )
                    if node:
                        affected.append(
                            {
                                "name": node[0]["name"],
                                "file": node[0]["file"],
                                "line": node[0]["line"],
                                "type": node[0].get("type", "unknown"),
                            }
                        )
                    next_frontier.append(tid_str)
        frontier = next_frontier

    # Dedupe affected by (name, file)
    seen = set()
    deduped = []
    for a in affected:
        key = (a["name"], a["file"])
        if key not in seen:
            seen.add(key)
            deduped.append(a)

    # Check if any hubs are in the affected set
    hubs = code_hubs(db, project, top_n=10)
    hub_names = {(h["name"], h["file"]) for h in hubs}
    hubs_hit = [a for a in deduped if (a["name"], a["file"]) in hub_names]

    affected_files = {a["file"] for a in deduped}
    n_affected = len(deduped)
    if n_affected > 50 or hubs_hit:
        risk = "CRITICAL"
    elif n_affected > 20:
        risk = "HIGH"
    elif n_affected > 5:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    return {
        "changed_files": changed_files,
        "changed_symbols": [
            {"name": s["name"], "file": s["file"], "type": s.get("type", "unknown")}
            for s in changed_symbols
        ],
        "affected": deduped,
        "hubs_affected": [{"name": h["name"], "file": h["file"]} for h in hubs_hit],
        "summary": {
            "files_changed": len(changed_files),
            "symbols_changed": len(changed_symbols),
            "total_affected": n_affected,
            "affected_files": len(affected_files),
            "risk": risk,
        },
    }
