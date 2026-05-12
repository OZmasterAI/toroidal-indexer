"""Toroidal-Indexer: SurrealDB schema for code graph (code_node + RELATE edges)."""

import hashlib
import os

from surrealdb import RecordID, Surreal

SURREAL_URL = os.environ.get("SURREAL_URL", "ws://127.0.0.1:8822")
NAMESPACE = "code_graph"
VALID_RELATIONS = frozenset({"calls", "imports", "reads", "writes", "implements"})


def connect_code_graph(url=None, database="main"):
    conn = Surreal(url or SURREAL_URL)
    conn.signin(
        {
            "username": os.environ.get("SURREAL_USER", "root"),
            "password": os.environ.get("SURREAL_PASS", "root"),
        }
    )
    conn.use(NAMESPACE, database)
    return conn


def init_code_tables(db):
    db.query("DEFINE TABLE IF NOT EXISTS code_node SCHEMALESS")
    for rel in VALID_RELATIONS:
        db.query(f"DEFINE TABLE IF NOT EXISTS {rel} SCHEMALESS")
        db.query(
            f"DEFINE INDEX IF NOT EXISTS {rel}_dedup ON {rel} FIELDS in, out UNIQUE"
        )
    db.query(
        "DEFINE INDEX IF NOT EXISTS code_node_file ON code_node FIELDS project, file"
    )
    db.query(
        "DEFINE INDEX IF NOT EXISTS code_node_name ON code_node FIELDS project, name"
    )
    db.query(
        "DEFINE INDEX IF NOT EXISTS code_node_vec ON code_node FIELDS embedding "
        "HNSW DIMENSION 4096 TYPE F32 DIST COSINE EFC 150 M 12"
    )


def _node_key(project, file, name):
    raw = f"{project}:{file}:{name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def upsert_node(db, project, file, name, node_type, line):
    key = _node_key(project, file, name)
    rid = RecordID("code_node", key)
    db.query(
        "UPSERT $id SET project=$project, file=$file, name=$name, type=$type, line=$line",
        {
            "id": rid,
            "project": project,
            "file": file,
            "name": name,
            "type": node_type,
            "line": line,
        },
    )
    return rid


def relate(db, source_id, relation, target_id, confidence=1.0, source_line=0):
    if relation not in VALID_RELATIONS:
        raise ValueError(
            f"Invalid relation '{relation}'. Must be one of: {VALID_RELATIONS}"
        )
    existing = db.query(
        f"SELECT * FROM {relation} WHERE in=$src AND out=$tgt",
        {"src": source_id, "tgt": target_id},
    )
    if existing:
        edge = existing[0]
        if confidence > edge.get("confidence", 0):
            db.query(
                "UPDATE $id SET confidence=$conf, source_line=$line",
                {"id": edge["id"], "conf": confidence, "line": source_line},
            )
            edge["confidence"] = confidence
            edge["source_line"] = source_line
        return edge
    result = db.query(
        f"RELATE $src->{relation}->$tgt SET confidence=$conf, source_line=$line",
        {
            "src": source_id,
            "tgt": target_id,
            "conf": confidence,
            "line": source_line,
        },
    )
    return result[0] if result else None


def get_callers(db, node_id, depth=1):
    if depth == 1:
        rows = db.query(
            "SELECT <-calls<-code_node.* AS callers FROM $id",
            {"id": node_id},
        )
        if not rows or not rows[0].get("callers"):
            return []
        callers = rows[0]["callers"]
        edges = db.query(
            "SELECT * FROM calls WHERE out=$id",
            {"id": node_id},
        )
        conf_map = {str(e["in"]): e["confidence"] for e in edges}
        line_map = {str(e["in"]): e["source_line"] for e in edges}
        return [
            {
                "name": c["name"],
                "file": c["file"],
                "line": c["line"],
                "confidence": conf_map.get(str(RecordID("code_node", c["id"].id)), 1.0),
                "source_line": line_map.get(str(RecordID("code_node", c["id"].id)), 0),
            }
            for c in callers
        ]
    collected = {}
    frontier = [node_id]
    for _ in range(depth):
        next_frontier = []
        for nid in frontier:
            for caller in get_callers(db, nid, depth=1):
                key = f"{caller['file']}:{caller['name']}"
                if key not in collected:
                    collected[key] = caller
                    cid = RecordID(
                        "code_node",
                        _node_key(
                            db.query("SELECT project FROM $id", {"id": nid})[0].get(
                                "project", ""
                            ),
                            caller["file"],
                            caller["name"],
                        ),
                    )
                    next_frontier.append(cid)
        frontier = next_frontier
    return list(collected.values())


def get_readers(db, node_id):
    rows = db.query(
        "SELECT <-reads<-code_node.* AS readers FROM $id",
        {"id": node_id},
    )
    if not rows or not rows[0].get("readers"):
        return []
    readers = rows[0]["readers"]
    edges = db.query(
        "SELECT * FROM reads WHERE out=$id",
        {"id": node_id},
    )
    conf_map = {str(e["in"]): e["confidence"] for e in edges}
    return [
        {
            "name": r["name"],
            "file": r["file"],
            "line": r["line"],
            "confidence": conf_map.get(str(RecordID("code_node", r["id"].id)), 1.0),
        }
        for r in readers
    ]


def dedup_nodes(db):
    """Merge duplicate nodes (same project/file/name, different IDs).

    Keeps the canonical node (ID matches _node_key), migrates all edges
    from duplicates to the canonical, then deletes duplicates.
    Returns (groups_merged, edges_migrated).
    """
    groups = db.query(
        "SELECT project, file, name, count() AS cnt "
        "FROM code_node GROUP BY project, file, name"
    )
    merged = 0
    migrated = 0
    for g in groups:
        if g.get("cnt", 0) < 2:
            continue
        project, file, name = g["project"], g["file"], g["name"]
        nodes = db.query(
            "SELECT * FROM code_node WHERE project=$p AND file=$f AND name=$n",
            {"p": project, "f": file, "n": name},
        )
        if len(nodes) < 2:
            continue

        canonical_key = _node_key(project, file, name)
        canonical_id = RecordID("code_node", canonical_key)

        canonical = None
        dupes = []
        best_line = 0
        best_type = "function"
        for n in nodes:
            nid_str = n["id"].id if hasattr(n["id"], "id") else str(n["id"])
            if nid_str == canonical_key:
                canonical = n
            else:
                dupes.append(n)
            if n.get("line", 0) > 0:
                best_line = n["line"]
            if n.get("type") in ("class", "field"):
                best_type = n["type"]

        if not canonical:
            canonical = dupes.pop(0)
            db.query(
                "UPSERT $id SET project=$p, file=$f, name=$n, type=$t, line=$l",
                {
                    "id": canonical_id,
                    "p": project,
                    "f": file,
                    "n": name,
                    "t": best_type,
                    "l": best_line,
                },
            )
            dupes.append(canonical)
            canonical = {"id": canonical_id}

        if best_line > 0:
            db.query("UPDATE $id SET line=$l", {"id": canonical_id, "l": best_line})

        for dupe in dupes:
            did = dupe["id"]
            if str(did) == str(canonical_id):
                continue
            for rel in VALID_RELATIONS:
                out_edges = db.query(f"SELECT * FROM {rel} WHERE in=$id", {"id": did})
                for e in out_edges:
                    existing = db.query(
                        f"SELECT id FROM {rel} WHERE in=$canon AND out=$tgt",
                        {"canon": canonical_id, "tgt": e["out"]},
                    )
                    if not existing:
                        db.query(
                            f"RELATE $src->{rel}->$tgt SET confidence=$c, source_line=$l",
                            {
                                "src": canonical_id,
                                "tgt": e["out"],
                                "c": e.get("confidence", 1.0),
                                "l": e.get("source_line", 0),
                            },
                        )
                        migrated += 1
                    db.query("DELETE $id", {"id": e["id"]})

                in_edges = db.query(f"SELECT * FROM {rel} WHERE out=$id", {"id": did})
                for e in in_edges:
                    existing = db.query(
                        f"SELECT id FROM {rel} WHERE in=$src AND out=$canon",
                        {"src": e["in"], "canon": canonical_id},
                    )
                    if not existing:
                        db.query(
                            f"RELATE $src->{rel}->$tgt SET confidence=$c, source_line=$l",
                            {
                                "src": e["in"],
                                "tgt": canonical_id,
                                "c": e.get("confidence", 1.0),
                                "l": e.get("source_line", 0),
                            },
                        )
                        migrated += 1
                    db.query("DELETE $id", {"id": e["id"]})

            db.query("DELETE $id", {"id": did})
        merged += 1
    return merged, migrated


def delete_file_nodes(db, project, file):
    nodes = db.query(
        "SELECT id FROM code_node WHERE project=$proj AND file=$file",
        {"proj": project, "file": file},
    )
    for node in nodes:
        nid = node["id"]
        for rel in VALID_RELATIONS:
            db.query(f"DELETE {rel} WHERE in=$id OR out=$id", {"id": nid})
        db.query("DELETE $id", {"id": nid})
