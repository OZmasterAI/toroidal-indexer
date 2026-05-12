#!/usr/bin/env python3
"""Toroidal-Indexer MCP Server — structural code graph queries.

Exposes 5 tools for querying the SurrealDB code graph:
code_callers, code_readers, code_path, code_blast_radius, code_hubs.

Run standalone: python3 indexer_server.py --http --port 8748
Used via MCP: routed through toolshed as "indexer" backend.
"""

import argparse
import functools
import os
import sys
import traceback

from mcp.server.fastmcp import FastMCP

_INDEXER_DIR = os.path.dirname(__file__)
if _INDEXER_DIR not in sys.path:
    sys.path.insert(0, _INDEXER_DIR)

from indexer.schema import connect_code_graph
from indexer.mcp_queries import (
    code_blast_radius as _code_blast_radius,
    code_callers as _code_callers,
    code_cluster_members as _code_cluster_members,
    code_clusters as _code_clusters,
    code_detect_changes as _code_detect_changes,
    code_hubs as _code_hubs,
    code_path as _code_path,
    code_processes as _code_processes,
    code_query as _code_query,
    code_readers as _code_readers,
    code_search as _code_search,
)

# ── Transport config ──
_NET_HOST = os.environ.get("INDEXER_HOST", "127.0.0.1")
_NET_PORT = int(os.environ.get("INDEXER_PORT", "8748"))

_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--http", action="store_true", default=True)
_parser.add_argument("--stdio", action="store_true", default=False)
_parser.add_argument("--port", type=int, default=_NET_PORT)
_args, _ = _parser.parse_known_args()

if _args.stdio:
    _args.http = False

if _args.http:
    mcp = FastMCP("indexer", host=_NET_HOST, port=_args.port)
else:
    mcp = FastMCP("indexer")

# ── OAuth discovery stubs (Claude Code does RFC 9728/8414 probing) ──
if _args.http:
    from starlette.requests import Request
    from starlette.responses import Response

    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def _oauth_as_metadata(request: Request) -> Response:
        return Response(status_code=404)

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def _oauth_protected_resource(request: Request) -> Response:
        return Response(status_code=404)

    @mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])
    async def _openid_config(request: Request) -> Response:
        return Response(status_code=404)

    @mcp.custom_route("/register", methods=["POST"])
    async def _oauth_register(request: Request) -> Response:
        return Response(status_code=404)

    @mcp.custom_route("/authorize", methods=["GET"])
    async def _oauth_authorize(request: Request) -> Response:
        return Response(status_code=404)


# ── Crash-proof decorator ──


def crash_proof(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return {"error": traceback.format_exc()}

    return wrapper


# ── Lazy DB connection (connect once on first tool call) ──

_db = None


def _get_db():
    global _db
    if _db is None:
        _db = connect_code_graph()
    return _db


# ── MCP Tools ──


@mcp.tool()
@crash_proof
def code_callers(project: str, file: str, function: str, depth: int = 1) -> list:
    """Who calls this function. Returns list of {name, file, line, confidence}."""
    return _code_callers(_get_db(), project, file, function, depth=depth)


@mcp.tool()
@crash_proof
def code_readers(project: str, file: str, field: str) -> list:
    """Who reads this field/key. Returns list of {name, file, line, confidence}."""
    return _code_readers(_get_db(), project, file, field)


@mcp.tool()
@crash_proof
def code_path(
    project: str,
    from_file: str,
    from_name: str,
    to_file: str,
    to_name: str,
) -> list:
    """Shortest call-chain path between two nodes. Returns list of {name, file, line}."""
    return _code_path(_get_db(), project, from_file, from_name, to_file, to_name)


@mcp.tool()
@crash_proof
def code_blast_radius(project: str, file: str, function: str, depth: int = 3) -> list:
    """Transitive dependents — everything downstream that could break. Returns list of {name, file, line}."""
    return _code_blast_radius(_get_db(), project, file, function, depth=depth)


@mcp.tool()
@crash_proof
def code_search(project: str, query: str, limit: int = 15) -> list:
    """Fuzzy search: find nodes by substring match on name or file path. Accepts natural language like 'auth flow' or 'database connection'."""
    return _code_search(_get_db(), project, query, limit=limit)


@mcp.tool()
@crash_proof
def code_hubs(project: str, top_n: int = 10) -> list:
    """Most-connected nodes in the project. Returns list of {name, file, degree}."""
    return _code_hubs(_get_db(), project, top_n=top_n)


@mcp.tool()
@crash_proof
def code_clusters(project: str) -> list:
    """All clusters for a project with labels, node counts, and top members."""
    return _code_clusters(_get_db(), project)


@mcp.tool()
@crash_proof
def code_cluster_members(project: str, label: str) -> list:
    """All nodes in clusters matching the label (substring match). Returns list of {name, file, type, line}."""
    return _code_cluster_members(_get_db(), project, label)


@mcp.tool()
@crash_proof
def code_query(
    project: str, question: str, mode: str = "bfs", depth: int = 2, budget: int = 2000
) -> str:
    """Answer a codebase question via graph traversal. Returns compact text with nodes and edges within a token budget. Use this for natural language questions instead of multiple search+read calls."""
    return _code_query(
        _get_db(), project, question, mode=mode, depth=depth, budget=budget
    )


@mcp.tool()
@crash_proof
def code_detect_changes(
    project: str,
    project_root: str,
    base_ref: str = "HEAD~1",
    depth: int = 2,
) -> dict:
    """Map git diff to blast radius. Returns changed files, affected symbols, hub impacts, and risk level (NONE/LOW/MEDIUM/HIGH/CRITICAL). Use after commits to see what might break."""
    return _code_detect_changes(
        _get_db(), project, project_root, base_ref=base_ref, depth=depth
    )


@mcp.tool()
@crash_proof
def code_processes(project: str, query: str = "", limit: int = 20) -> list:
    """Detected execution flows (entry point → terminal). Filter by query substring. Returns list of {label, step_count, cross_community, steps}."""
    return _code_processes(_get_db(), project, query=query or None, limit=limit)


# ── MCP Resources (pre-computed summaries, cheaper than GRAPH_REPORT.md) ──


@mcp.resource("indexer://project/{name}/context")
def project_context(name: str) -> str:
    """Project overview: node/edge/file counts, top 5 hubs, last index timestamp. ~150 tokens."""
    db = _get_db()
    nodes = db.query(
        "SELECT count() AS c FROM code_node WHERE project=$p GROUP ALL", {"p": name}
    )
    node_count = nodes[0]["c"] if nodes else 0

    edge_count = 0
    for rel in ("calls", "imports", "reads", "writes", "implements"):
        rows = db.query(
            f"SELECT count() AS c FROM {rel} WHERE in.project=$p GROUP ALL", {"p": name}
        )
        edge_count += rows[0]["c"] if rows else 0

    files = db.query(
        "SELECT count() AS c FROM code_node WHERE project=$p AND type='file' GROUP ALL",
        {"p": name},
    )
    file_count = files[0]["c"] if files else 0

    hubs = _code_hubs(db, name, top_n=5)
    hub_lines = [f"  {h['name']} ({h['file']}) deg={h['degree']}" for h in hubs]

    clusters = db.query(
        "SELECT count() AS c FROM code_cluster WHERE project=$p GROUP ALL", {"p": name}
    )
    cluster_count = clusters[0]["c"] if clusters else 0

    return (
        f"Project: {name}\n"
        f"Nodes: {node_count} | Edges: {edge_count} | Files: {file_count} | Clusters: {cluster_count}\n"
        f"Top hubs:\n" + "\n".join(hub_lines)
    )


@mcp.resource("indexer://project/{name}/clusters")
def project_clusters(name: str) -> str:
    """All clusters with node counts and top 3 key files each. ~300 tokens."""
    db = _get_db()
    rows = db.query(
        "SELECT label, node_count, key_files, key_functions "
        "FROM code_cluster WHERE project=$p ORDER BY node_count DESC",
        {"p": name},
    )
    if not rows:
        return f"No clusters for {name}."
    lines = [f"Clusters for {name} ({len(rows)} total):"]
    for r in rows:
        files = r.get("key_files", [])[:3]
        files_str = ", ".join(files) if files else "(none)"
        lines.append(f"  [{r.get('node_count', 0)}] {r['label']}: {files_str}")
    return "\n".join(lines)


@mcp.resource("indexer://project/{name}/hubs")
def project_hubs(name: str) -> str:
    """Top 10 most-connected nodes with degree and file path. ~200 tokens."""
    db = _get_db()
    hubs = _code_hubs(db, name, top_n=10)
    if not hubs:
        return f"No hubs for {name}."
    lines = [f"Top hubs for {name}:"]
    for h in hubs:
        lines.append(f"  {h['name']} deg={h['degree']} {h['file']}")
    return "\n".join(lines)


@mcp.resource("indexer://project/{name}/processes")
def project_processes(name: str) -> str:
    """Detected execution flows with step counts. ~200 tokens."""
    db = _get_db()
    procs = _code_processes(db, name, limit=20)
    if not procs:
        return f"No processes for {name}."
    lines = [f"Execution flows for {name} ({len(procs)} shown):"]
    for p in procs:
        cross = " [cross-cluster]" if p.get("cross_community") else ""
        lines.append(f"  [{p['step_count']} steps] {p['label']}{cross}")
    return "\n".join(lines)


# ── Entry point ──

if __name__ == "__main__":
    mcp.run(transport="streamable-http" if _args.http else "stdio")
