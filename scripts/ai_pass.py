#!/usr/bin/env python3
"""Toroidal-Indexer Tier 3: AI-assisted edge discovery.

Three-pass pipeline using Claude Code Agent tool:
  Pass 1 (Haiku fleet): Extract explicit edges from file batches
  Pass 2 (Sonnet fleet): Find implicit coupling given Pass 1 edges
  Pass 3 (Sonnet reviewer): Structural anomaly detection + corrections

Usage:
  python3 scripts/ai_pass.py run --project .claude [--dry-run] [--batch-size 10]
  python3 scripts/ai_pass.py store --stdin --project .claude --pass 1
"""

import argparse
import json
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.schema import (
    VALID_RELATIONS,
    connect_code_graph,
    init_code_tables,
    relate,
    upsert_node,
)


logger = logging.getLogger(__name__)

EDGE_SCHEMA = {
    "type": "object",
    "required": ["source", "target", "relation"],
    "properties": {
        "source": {"type": "string", "description": "file:symbol, e.g. main.py:main"},
        "target": {
            "type": "string",
            "description": "file:symbol, e.g. utils.py:helper",
        },
        "relation": {
            "type": "string",
            "enum": list(VALID_RELATIONS),
        },
        "confidence": {"type": "number", "default": 0.8},
        "line": {"type": "integer", "default": 0},
        "reason": {"type": "string", "description": "Why this edge exists"},
    },
}

REQUIRED_EDGE_FIELDS = {"source", "target", "relation"}


def _validate_edge(entry):
    if not isinstance(entry, dict):
        return False
    if not REQUIRED_EDGE_FIELDS.issubset(entry.keys()):
        return False
    if entry.get("relation") not in VALID_RELATIONS:
        return False
    return bool(entry.get("source")) and bool(entry.get("target"))


def _extract_json_array(text):
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    fence_match = re.search(r"```(?:json)?\s*\n(\[.*?\])\s*\n```", stripped, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    bracket_start = stripped.find("[")
    bracket_end = stripped.rfind("]")
    if bracket_start != -1 and bracket_end > bracket_start:
        try:
            return json.loads(stripped[bracket_start : bracket_end + 1])
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def parse_agent_output(text):
    if not text or not text.strip():
        return []
    raw = _extract_json_array(text)
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if _validate_edge(entry)]


def _find_source_files(project_root, extensions=(".py", ".rs", ".ts", ".tsx", ".js")):
    files = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in ("node_modules", "__pycache__", "target")
        ]
        for fname in filenames:
            if any(fname.endswith(ext) for ext in extensions):
                files.append(
                    os.path.relpath(os.path.join(dirpath, fname), project_root)
                )
    return sorted(files)


def _batch(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def format_edges_for_prompt(edges, max_chars=4000):
    if not edges:
        return "(no edges)"
    lines = []
    chars = 0
    for e in edges:
        line = f"{e.get('source', '?')} --{e.get('relation', '?')}--> {e.get('target', '?')}"
        if chars + len(line) + 1 > max_chars:
            remaining = len(edges) - len(lines)
            lines.append(f"[... and {remaining} more]")
            break
        lines.append(line)
        chars += len(line) + 1
    return "\n".join(lines)


def _pass1_prompt(files, project_root):
    file_list = "\n".join(f"- {f}" for f in files)
    return f"""Read these source files from {project_root} and extract code relationships.

Output ONLY a valid JSON array. No markdown, no explanation, no preamble.

Each edge must have this exact format:
{{"source": "file.py:function_name", "target": "other.py:other_func", "relation": "calls", "confidence": 0.8, "line": 42, "reason": "direct function call"}}

Valid relations: calls, imports, reads, writes, implements

Examples:
[
  {{"source": "main.py:main", "target": "utils.py:helper", "relation": "calls", "confidence": 0.8, "line": 5, "reason": "main() calls helper()"}},
  {{"source": "main.py:main", "target": "utils.py:utils", "relation": "imports", "confidence": 0.8, "line": 1, "reason": "from utils import helper"}},
  {{"source": "config.py:load", "target": "settings.json:settings.json", "relation": "reads", "confidence": 0.8, "line": 10, "reason": "opens settings.json"}}
]

Extract: imports, function calls, class inheritance (implements), file reads/writes, dict key access patterns.

Files:
{file_list}"""


def _pass2_prompt(files, known_edges, project_root):
    edge_text = format_edges_for_prompt(known_edges) if known_edges else "(none yet)"
    file_list = "\n".join(f"- {f}" for f in files)
    return f"""These edges are already known for {project_root}:
{edge_text}

Read these files and find IMPLICIT coupling NOT in the list above.

Look for: shared state access, naming conventions implying coupling, dynamic dispatch (getattr, registry lookups), execution-order dependencies, config key sharing, similar error handling patterns.

Output ONLY a valid JSON array. No markdown, no explanation, no preamble.

Each edge must have this exact format:
{{"source": "file.py:symbol", "target": "other.py:symbol", "relation": "calls|imports|reads|writes|implements", "confidence": 0.8, "line": 0, "reason": "why this coupling exists"}}

Examples:
[
  {{"source": "handler.py:process", "target": "validator.py:validate", "relation": "calls", "confidence": 0.8, "line": 0, "reason": "dynamic dispatch via registry['validate']"}},
  {{"source": "api.py:handler", "target": "config.py:TIMEOUT", "relation": "reads", "confidence": 0.8, "line": 15, "reason": "both modules read TIMEOUT from shared config"}}
]

Files:
{file_list}"""


def _pass3_prompt(graph_summary, project_root):
    summary_text = json.dumps(graph_summary, indent=2)
    return f"""Review this code graph summary for {project_root}:

{summary_text}

Find structural anomalies:
- Isolated nodes (listed above) that should have edges — what do they call or import?
- Hub nodes with suspiciously low degree — are edges missing?
- Asymmetric relationships — A imports B but B has no callers from A's module
- Missing expected connections based on naming conventions

Output ONLY a valid JSON array of correction edges. No markdown, no explanation, no preamble.

Each edge must have this exact format:
{{"source": "file.py:symbol", "target": "other.py:symbol", "relation": "calls|imports|reads|writes|implements", "confidence": 0.8, "line": 0, "reason": "why this edge should exist"}}

Example:
[
  {{"source": "test_utils.py:TestUtils", "target": "utils.py:helper", "relation": "calls", "confidence": 0.8, "line": 0, "reason": "test class name implies it tests utils.helper but no edge exists"}}
]

If no anomalies found, output an empty array: []"""


def _store_single_edge(db, project_name, e, conf, pass_num):
    rel = e["relation"]
    src, tgt = e["source"], e["target"]
    line = int(e.get("line", 0))
    src_file, _, src_name = src.rpartition(":")
    tgt_file, _, tgt_name = tgt.rpartition(":")
    if not src_file:
        src_file, src_name = src, src
    if not tgt_file:
        tgt_file, tgt_name = tgt, tgt
    src_id = upsert_node(
        db, project_name, src_file, src_name or src_file, "function", line
    )
    tgt_id = upsert_node(
        db, project_name, tgt_file, tgt_name or tgt_file, "function", 0
    )
    existing = db.query(
        f"SELECT * FROM {rel} WHERE in=$src AND out=$tgt",
        {"src": src_id, "tgt": tgt_id},
    )
    if existing:
        return "skipped"
    relate(db, src_id, rel, tgt_id, conf, line)
    if pass_num is not None:
        edge_row = db.query(
            f"SELECT * FROM {rel} WHERE in=$src AND out=$tgt",
            {"src": src_id, "tgt": tgt_id},
        )
        if edge_row:
            db.query(
                "UPDATE $id SET pass=$pass", {"id": edge_row[0]["id"], "pass": pass_num}
            )
    return "stored"


def _store_edges(db, project_name, edges, force_confidence=None, pass_num=None):
    result = {"stored": 0, "skipped": 0, "errors": 0, "error_details": []}
    for e in edges:
        rel = e.get("relation", "")
        src = e.get("source", "")
        tgt = e.get("target", "")
        if rel not in VALID_RELATIONS or not src or not tgt:
            result["skipped"] += 1
            continue
        conf = float(
            force_confidence
            if force_confidence is not None
            else e.get("confidence", 0.8)
        )
        try:
            status = _store_single_edge(db, project_name, e, conf, pass_num)
            result[status] += 1
        except Exception as exc:
            result["errors"] += 1
            result["error_details"].append(
                f"{src}->{tgt} ({rel}): {type(exc).__name__}: {exc}"
            )
            logger.warning("Failed to store edge %s->%s (%s): %s", src, tgt, rel, exc)
    return result


def get_edges_for_files(db, project, files):
    if not files:
        return []
    edges = []
    for rel in VALID_RELATIONS:
        rows = db.query(
            f"SELECT *, in.file AS src_file, in.name AS src_name, "
            f"out.file AS tgt_file, out.name AS tgt_name "
            f"FROM {rel} WHERE in.project=$proj AND (in.file IN $files OR out.file IN $files)",
            {"proj": project, "files": files},
        )
        for row in rows:
            edges.append(
                {
                    "source": f"{row.get('src_file', '')}:{row.get('src_name', '')}",
                    "target": f"{row.get('tgt_file', '')}:{row.get('tgt_name', '')}",
                    "relation": rel,
                    "confidence": row.get("confidence", 0.8),
                    "line": row.get("source_line", 0),
                }
            )
    return edges


def get_graph_summary(db, project):
    nodes = db.query(
        "SELECT count() FROM code_node WHERE project=$proj GROUP ALL",
        {"proj": project},
    )
    node_count = nodes[0].get("count", 0) if nodes else 0

    edge_counts = {}
    for rel in VALID_RELATIONS:
        rows = db.query(f"SELECT count() FROM {rel} GROUP ALL")
        cnt = rows[0].get("count", 0) if rows else 0
        if cnt > 0:
            edge_counts[rel] = cnt

    all_nodes = db.query(
        "SELECT file, name FROM code_node WHERE project=$proj",
        {"proj": project},
    )
    degree_map = {}
    for node in all_nodes:
        key = f"{node['file']}:{node['name']}"
        degree_map[key] = 0
    for rel in VALID_RELATIONS:
        edges = db.query(
            f"SELECT in.file AS sf, in.name AS sn, out.file AS tf, out.name AS tn FROM {rel}"
        )
        for e in edges:
            src_key = f"{e.get('sf', '')}:{e.get('sn', '')}"
            tgt_key = f"{e.get('tf', '')}:{e.get('tn', '')}"
            if src_key in degree_map:
                degree_map[src_key] += 1
            if tgt_key in degree_map:
                degree_map[tgt_key] += 1

    sorted_hubs = sorted(degree_map.items(), key=lambda x: x[1], reverse=True)
    top_hubs = [{"name": k, "degree": v} for k, v in sorted_hubs[:20] if v > 0]
    isolated = [k for k, v in sorted_hubs if v == 0]

    return {
        "node_count": node_count,
        "edge_counts": edge_counts,
        "top_hubs": top_hubs,
        "isolated_nodes": isolated,
    }


def run(project_root, project_name, batch_size=10, dry_run=False):
    start = time.time()
    files = _find_source_files(project_root)
    summary = {
        "project": project_name,
        "total_files": len(files),
        "batch_size": batch_size,
        "pass1_edges": 0,
        "pass2_edges": 0,
        "pass3_edges": 0,
        "dry_run": dry_run,
    }

    if dry_run:
        batches = list(_batch(files, batch_size))
        summary["pass1_batches"] = len(batches)
        summary["pass2_batches"] = len(batches)
        summary["pass3_batches"] = 1
        summary["estimated_agents"] = len(batches) * 2 + 1
        summary["duration_s"] = round(time.time() - start, 2)
        return summary

    db = connect_code_graph()
    init_code_tables(db)

    for batch_files in _batch(files, batch_size):
        _pass1_prompt(batch_files, project_root)
        sys.stderr.write(f"[Pass1] Batch of {len(batch_files)} files\n")
        summary["pass1_edges"] += len(batch_files)

    for batch_files in _batch(files, batch_size):
        _pass2_prompt(batch_files, [], project_root)
        sys.stderr.write(f"[Pass2] Batch of {len(batch_files)} files\n")

    _pass3_prompt({}, project_root)
    sys.stderr.write("[Pass3] Reviewer pass\n")

    summary["duration_s"] = round(time.time() - start, 2)
    return summary


def _store_command(args):
    db_name = args.db or "main"
    db = connect_code_graph(database=db_name)
    try:
        init_code_tables(db)
    except Exception as exc:
        logger.debug("init_code_tables skipped (tables likely exist): %s", exc)
    raw = sys.stdin.read()
    edges = json.loads(raw) if raw.strip() else []
    result = _store_edges(
        db, args.project, edges, force_confidence=0.8, pass_num=args.pass_num
    )
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Toroidal-Indexer Tier 3 AI pass")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run AI pass pipeline")
    run_parser.add_argument("--project", required=True, help="Project root path")
    run_parser.add_argument(
        "--dry-run", action="store_true", help="Show plan without executing"
    )
    run_parser.add_argument(
        "--batch-size", type=int, default=10, help="Files per agent batch"
    )
    run_parser.add_argument(
        "--project-name", help="Override project name (default: basename of --project)"
    )

    store_parser = subparsers.add_parser("store", help="Store edges from stdin")
    store_parser.add_argument("--stdin", action="store_true", required=True)
    store_parser.add_argument(
        "--project", required=True, help="Project name for edge tagging"
    )
    store_parser.add_argument("--pass", type=int, dest="pass_num", required=True)
    store_parser.add_argument(
        "--db", default=None, help="Database name (default: main)"
    )

    args = parser.parse_args()

    if args.command == "store":
        _store_command(args)
    elif args.command == "run":
        project_root = os.path.abspath(args.project)
        project_name = args.project_name or os.path.basename(project_root)
        summary = run(project_root, project_name, args.batch_size, args.dry_run)
        json.dump({"summary": summary}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
