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
    code_hubs as _code_hubs,
    code_path as _code_path,
    code_readers as _code_readers,
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
def code_hubs(project: str, top_n: int = 10) -> list:
    """Most-connected nodes in the project. Returns list of {name, file, degree}."""
    return _code_hubs(_get_db(), project, top_n=top_n)


# ── Entry point ──

if __name__ == "__main__":
    mcp.run(transport="sse" if _args.http else "stdio")
