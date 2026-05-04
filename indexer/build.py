"""Toroidal-Indexer: full and incremental index builder."""

import os
import subprocess

from surrealdb import RecordID

from indexer.extractors.python import extract_python
from indexer.extractors.rust import extract_rust
from indexer.extractors.typescript import extract_typescript
from indexer.schema import (
    VALID_RELATIONS,
    _node_key,
    delete_file_nodes,
    relate,
    upsert_node,
)

EXTENSION_MAP = {
    ".py": extract_python,
    ".rs": extract_rust,
    ".ts": extract_typescript,
    ".tsx": extract_typescript,
    ".js": extract_typescript,
    ".jsx": extract_typescript,
    ".mjs": extract_typescript,
}

SOURCE_EXTENSIONS = frozenset(EXTENSION_MAP.keys())


def _is_gitignored(file_path, project_root):
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", file_path],
            cwd=project_root,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _walk_source_files(project_root):
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in ("node_modules", "__pycache__", "target", ".git")
        ]
        for fname in filenames:
            ext = os.path.splitext(fname)[1]
            if ext in SOURCE_EXTENSIONS:
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, project_root)
                yield full, rel


def _store_results(db, project_name, rel_path, nodes, edges):
    node_ids = {}
    for node in nodes:
        nid = upsert_node(db, project_name, rel_path, node.name, node.type, node.line)
        node_ids[node.name] = nid

    file_id = upsert_node(db, project_name, rel_path, rel_path, "file", 0)
    node_ids[rel_path] = file_id

    for edge in edges:
        src_id = node_ids.get(edge.source)
        target = edge.target
        if ":" in target:
            tgt_file, tgt_name = target.split(":", 1)
            tgt_id = upsert_node(db, project_name, tgt_file, tgt_name, "function", 0)
            node_ids[target] = tgt_id
        else:
            tgt_id = node_ids.get(target)
        if src_id is None:
            src_id = file_id
        if tgt_id is None:
            tgt_id = upsert_node(db, project_name, target, target, "file", 0)
            node_ids[target] = tgt_id
        try:
            relate(db, src_id, edge.relation, tgt_id, edge.confidence, edge.source_line)
        except (ValueError, Exception):
            pass


def _batch_delete_project(db, project_name, files):
    """Bulk-delete nodes+edges for a set of files in one pass."""
    for rel in VALID_RELATIONS:
        db.query(
            f"DELETE {rel} WHERE in.project=$p AND in.file IN $files "
            f"OR out.project=$p AND out.file IN $files",
            {"p": project_name, "files": list(files)},
        )
    db.query(
        "DELETE code_node WHERE project=$p AND file IN $files",
        {"p": project_name, "files": list(files)},
    )


def _batch_store(db, project_name, collected):
    """Store extracted nodes+edges with batched upserts."""
    node_ids = {}

    for rel_path, (nodes, edges) in collected:
        file_key = _node_key(project_name, rel_path, rel_path)
        file_rid = RecordID("code_node", file_key)
        db.query(
            "UPSERT $id SET project=$p, file=$f, name=$n, type='file', line=0",
            {"id": file_rid, "p": project_name, "f": rel_path, "n": rel_path},
        )
        node_ids[(rel_path, rel_path)] = file_rid

        for node in nodes:
            key = _node_key(project_name, rel_path, node.name)
            rid = RecordID("code_node", key)
            db.query(
                "UPSERT $id SET project=$p, file=$f, name=$n, type=$t, line=$l",
                {
                    "id": rid,
                    "p": project_name,
                    "f": rel_path,
                    "n": node.name,
                    "t": node.type,
                    "l": node.line,
                },
            )
            node_ids[(rel_path, node.name)] = rid

    edges_stored = 0
    for rel_path, (nodes, edges) in collected:
        file_rid = node_ids[(rel_path, rel_path)]
        for edge in edges:
            if edge.relation not in VALID_RELATIONS:
                continue
            src_id = node_ids.get((rel_path, edge.source), file_rid)
            target = edge.target
            if ":" in target:
                tgt_file, tgt_name = target.split(":", 1)
                tgt_key = (tgt_file, tgt_name)
                if tgt_key not in node_ids:
                    key = _node_key(project_name, tgt_file, tgt_name)
                    rid = RecordID("code_node", key)
                    db.query(
                        "UPSERT $id SET project=$p, file=$f, name=$n, type='function', line=0",
                        {
                            "id": rid,
                            "p": project_name,
                            "f": tgt_file,
                            "n": tgt_name,
                        },
                    )
                    node_ids[tgt_key] = rid
                tgt_id = node_ids[tgt_key]
            else:
                tgt_id = node_ids.get((rel_path, target))
                if tgt_id is None:
                    tgt_key = (target, target)
                    if tgt_key not in node_ids:
                        key = _node_key(project_name, target, target)
                        rid = RecordID("code_node", key)
                        db.query(
                            "UPSERT $id SET project=$p, file=$f, name=$n, type='file', line=0",
                            {
                                "id": rid,
                                "p": project_name,
                                "f": target,
                                "n": target,
                            },
                        )
                        node_ids[tgt_key] = rid
                    tgt_id = node_ids[tgt_key]
            try:
                db.query(
                    f"RELATE $src->{edge.relation}->$tgt SET confidence=$c, source_line=$l",
                    {
                        "src": src_id,
                        "tgt": tgt_id,
                        "c": edge.confidence,
                        "l": edge.source_line,
                    },
                )
                edges_stored += 1
            except Exception:
                pass
    return edges_stored


def _extract_file(project_root, rel_path, full_path):
    ext = os.path.splitext(full_path)[1]
    extractor = EXTENSION_MAP.get(ext)
    if not extractor:
        return None
    try:
        return extractor(full_path, project_root)
    except Exception:
        return None


def full_build(db, project_root, project_name, fast=False):
    collected = []
    skipped = 0
    for full_path, rel_path in _walk_source_files(project_root):
        if _is_gitignored(rel_path, project_root):
            skipped += 1
            continue
        result = _extract_file(project_root, rel_path, full_path)
        if result:
            collected.append((rel_path, result))

    if fast:
        files = {rel_path for rel_path, _ in collected}
        _batch_delete_project(db, project_name, files)
        edges = _batch_store(db, project_name, collected)
        return {
            "files_indexed": len(collected),
            "files_skipped": skipped,
            "edges": edges,
        }

    for rel_path, _ in collected:
        delete_file_nodes(db, project_name, rel_path)
    for rel_path, (nodes, edges) in collected:
        _store_results(db, project_name, rel_path, nodes, edges)
    return {"files_indexed": len(collected), "files_skipped": skipped}


def incremental_build(db, project_root, project_name, changed_files):
    to_index = []
    for rel_path in changed_files:
        full_path = os.path.join(project_root, rel_path)
        ext = os.path.splitext(rel_path)[1]
        if ext not in SOURCE_EXTENSIONS:
            continue
        delete_file_nodes(db, project_name, rel_path)
        if not os.path.exists(full_path):
            continue
        result = _extract_file(project_root, rel_path, full_path)
        if result:
            to_index.append((rel_path, result))
    for rel_path, (nodes, edges) in to_index:
        _store_results(db, project_name, rel_path, nodes, edges)
    return {"files_indexed": len(to_index)}


def get_changed_files(project_root):
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1"],
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
