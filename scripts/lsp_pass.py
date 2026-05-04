#!/usr/bin/env python3
"""Toroidal-Indexer Tier 2: LSP pass — runs type-resolved analysis on source files.

Spawns language servers (pyright, typescript-language-server, rust-analyzer),
runs a 6-step query sequence per file, and stores edges in SurrealDB with
confidence 0.9.

Usage:
    python3 scripts/lsp_pass.py --project /path/to/project
    python3 scripts/lsp_pass.py --project /path --incremental
    python3 scripts/lsp_pass.py --project /path --dry-run
    python3 scripts/lsp_pass.py --project /path --languages python,rust
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # indexer package

from indexer.lsp import (
    build_line_to_name_map,
    enrich_node_types,
    resolve_target_node,
    store_call_hierarchy_edges,
    store_definition_edges,
    store_implementation_edges,
)
from indexer.schema import connect_code_graph, init_code_tables, upsert_node
from indexer.lsp_client import LSPClient
from indexer.lsp_configs import CONFIGS, get_config_for_file, is_server_available

logger = logging.getLogger(__name__)

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
}


def collect_files(
    project_root: str, languages: set[str] | None = None
) -> dict[str, list[str]]:
    """Walk project and group source files by language."""
    grouped: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            cfg = get_config_for_file(fpath)
            if cfg is None:
                continue
            if languages and cfg.language_id not in languages:
                continue
            grouped.setdefault(cfg.language_id, []).append(fpath)
    return grouped


def get_changed_files(project_root: str) -> set[str]:
    """Get files changed since last LSP pass (via git)."""
    marker = os.path.join(project_root, ".lsp_pass_timestamp")
    if not os.path.exists(marker):
        return set()
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=ACMR",
                f"--since={os.path.getmtime(marker):.0f}",
            ],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~10"],
                capture_output=True,
                text=True,
                cwd=project_root,
            )
        return {
            os.path.join(project_root, f.strip())
            for f in result.stdout.splitlines()
            if f.strip()
        }
    except Exception:
        return set()


def write_pyrightconfig(project_root: str) -> str | None:
    """Write temporary pyrightconfig.json for pyright path resolution."""
    config_path = os.path.join(project_root, "pyrightconfig.json")
    if os.path.exists(config_path):
        return None
    config = {
        "extraPaths": ["hooks", "hooks/shared"],
        "exclude": ["node_modules", ".git", "__pycache__", "target"],
    }
    with open(config_path, "w") as f:
        json.dump(config, f)
    return config_path


def process_file(
    client: LSPClient,
    file_path: str,
    language_id: str,
    db,
    project: str,
    project_root: str,
    symbols_cache: dict[str, list[dict]],
) -> dict:
    """Run 6-step query sequence on a single file. Returns stats."""
    stats = {"definitions": 0, "implementations": 0, "calls": 0, "errors": 0}
    rel_path = os.path.relpath(file_path, project_root)
    uri = Path(file_path).as_uri()

    try:
        client.did_open(file_path, language_id)
        if not client.wait_for_diagnostics(uri, timeout=20.0):
            logger.warning("Diagnostics timeout for %s", rel_path)

        # Step 1: documentSymbol
        symbols = client.document_symbol(file_path)
        if not symbols:
            client.did_close(file_path)
            return stats
        symbols_cache[uri] = symbols

        # Upsert file node
        upsert_node(db, project, rel_path, os.path.basename(file_path), "file", 0)

        # Steps 2-3: definition + implementation
        definition_results = []
        impl_results = []
        for sym in symbols:
            sym_name = sym.get("name", "")
            if "location" in sym:
                pos = sym["location"]["range"]["start"]
            elif "selectionRange" in sym:
                pos = sym["selectionRange"]["start"]
            else:
                continue

            kind = sym.get("kind", 0)
            # Kind 12=Function, 6=Constructor, 5=Class
            if kind in (12, 6):
                offset = 4
            elif kind == 5:
                offset = 6
            else:
                offset = 0

            char = pos["character"] + offset

            # Definition
            locs = client.definition(file_path, line=pos["line"], character=char)
            if locs:
                for loc in locs:
                    loc_uri = loc.get("uri", "")
                    loc_range = loc.get("range", loc.get("targetRange", {}))
                    if loc_uri and loc_uri != uri:
                        definition_results.append(
                            {
                                "source_name": os.path.basename(file_path),
                                "source_line": pos["line"],
                                "target_uri": loc_uri,
                                "target_line": loc_range.get("start", {}).get(
                                    "line", 0
                                ),
                            }
                        )

            # Implementation (for classes)
            if kind == 5:
                impl_locs = client.implementation(
                    file_path, line=pos["line"], character=char
                )
                if impl_locs:
                    for loc in impl_locs:
                        loc_uri = loc.get("uri", "")
                        loc_range = loc.get("range", loc.get("targetRange", {}))
                        if loc_uri and loc_uri != uri:
                            impl_results.append(
                                {
                                    "source_name": sym_name,
                                    "source_file": rel_path,
                                    "target_uri": loc_uri,
                                    "target_line": loc_range.get("start", {}).get(
                                        "line", 0
                                    ),
                                }
                            )

        # Resolve target symbols for definition/implementation edges
        target_uris = set()
        for d in definition_results:
            target_uris.add(d["target_uri"])
        for i in impl_results:
            target_uris.add(i["target_uri"])

        for t_uri in target_uris:
            if t_uri not in symbols_cache:
                t_path = Path(t_uri.replace("file://", ""))
                if t_path.exists():
                    t_syms = client.document_symbol(str(t_path))
                    if t_syms:
                        symbols_cache[t_uri] = t_syms

        # Store edges
        if definition_results:
            stats["definitions"] = store_definition_edges(
                db, project, rel_path, definition_results, symbols_cache, project_root
            )

        if impl_results:
            stats["implementations"] = store_implementation_edges(
                db, project, impl_results, symbols_cache, project_root
            )

        # Steps 4-5: Call hierarchy
        call_results = []
        for sym in symbols:
            if sym.get("kind") not in (12, 6):
                continue
            sym_name = sym.get("name", "")
            if "location" in sym:
                pos = sym["location"]["range"]["start"]
            elif "selectionRange" in sym:
                pos = sym["selectionRange"]["start"]
            else:
                continue

            char = pos["character"] + 4
            items = client.prepare_call_hierarchy(
                file_path, line=pos["line"], character=char
            )
            if not items:
                continue

            # Incoming calls
            incoming = client.incoming_calls(items[0])
            if incoming:
                for call in incoming:
                    from_item = call.get("from", {})
                    from_uri = from_item.get("uri", "")
                    from_name = from_item.get("name", "")
                    if from_uri and from_name:
                        from_path = os.path.relpath(
                            from_uri.replace("file://", ""), project_root
                        )
                        call_results.append(
                            {
                                "caller_file": from_path,
                                "caller_name": from_name,
                                "callee_file": rel_path,
                                "callee_name": sym_name,
                                "source_line": 0,
                            }
                        )

            # Outgoing calls
            outgoing = client.outgoing_calls(items[0])
            if outgoing:
                for call in outgoing:
                    to_item = call.get("to", {})
                    to_uri = to_item.get("uri", "")
                    to_name = to_item.get("name", "")
                    if to_uri and to_name:
                        to_path = os.path.relpath(
                            to_uri.replace("file://", ""), project_root
                        )
                        call_results.append(
                            {
                                "caller_file": rel_path,
                                "caller_name": sym_name,
                                "callee_file": to_path,
                                "callee_name": to_name,
                                "source_line": pos["line"],
                            }
                        )

        if call_results:
            stats["calls"] = store_call_hierarchy_edges(db, project, call_results)

        # Step 6: Hover for type enrichment (lightweight)
        hover_results = []
        for sym in symbols[:10]:  # Limit to avoid slowdown
            sym_name = sym.get("name", "")
            if "location" in sym:
                pos = sym["location"]["range"]["start"]
            elif "selectionRange" in sym:
                pos = sym["selectionRange"]["start"]
            else:
                continue
            char = (
                pos["character"] + 4 if sym.get("kind") in (12, 6) else pos["character"]
            )
            hover = client.hover(file_path, line=pos["line"], character=char)
            if hover and "contents" in hover:
                contents = hover["contents"]
                type_info = ""
                if isinstance(contents, dict):
                    type_info = contents.get("value", "")
                elif isinstance(contents, str):
                    type_info = contents
                if type_info:
                    hover_results.append(
                        {"name": sym_name, "line": pos["line"], "type_info": type_info}
                    )

        if hover_results:
            enrich_node_types(db, project, rel_path, hover_results)

        client.did_close(file_path)

    except Exception as e:
        stats["errors"] = 1
        logger.error("Error processing %s: %s", rel_path, e)
        try:
            client.did_close(file_path)
        except Exception:
            pass

    return stats


def run(
    project_root: str,
    project_name: str,
    incremental: bool = False,
    dry_run: bool = False,
    languages: set[str] | None = None,
):
    """Main orchestrator: collect files, spawn servers, process, store."""
    project_root = os.path.abspath(project_root)
    grouped = collect_files(project_root, languages)

    if incremental:
        changed = get_changed_files(project_root)
        if changed:
            for lang in list(grouped.keys()):
                grouped[lang] = [f for f in grouped[lang] if f in changed]
                if not grouped[lang]:
                    del grouped[lang]

    total_files = sum(len(files) for files in grouped.values())
    print(f"Project: {project_name} ({project_root})")
    print(f"Total files: {total_files}")
    for lang, files in sorted(grouped.items()):
        cfg = CONFIGS.get(lang)
        available = is_server_available(cfg) if cfg else False
        status = "available" if available else "NOT AVAILABLE"
        print(f"  {lang}: {len(files)} files [{status}]")

    if dry_run:
        print("\n[dry-run] Would process the above files. Exiting.")
        return

    # Connect to SurrealDB
    db = connect_code_graph()
    init_code_tables(db)

    total_stats = {"definitions": 0, "implementations": 0, "calls": 0, "errors": 0}
    start_time = time.time()

    for lang, files in grouped.items():
        cfg = CONFIGS.get(lang)
        if not cfg or not is_server_available(cfg):
            print(f"  Skipping {lang}: server not available")
            continue

        # Write pyrightconfig for Python
        pyright_config = None
        if lang == "python":
            pyright_config = write_pyrightconfig(project_root)

        # Spawn LSP server
        full_cmd = cfg.command
        root_uri = f"file://{project_root}"
        try:
            client = LSPClient(
                command=cfg.command,
                args=cfg.args,
                root_uri=root_uri,
            )
        except Exception as e:
            print(f"  Failed to start {lang} server: {e}")
            continue

        print(f"\n  Processing {lang} ({len(files)} files)...")
        symbols_cache: dict[str, list[dict]] = {}

        for i, fpath in enumerate(files):
            rel = os.path.relpath(fpath, project_root)
            stats = process_file(
                client,
                fpath,
                cfg.language_id,
                db,
                project_name,
                project_root,
                symbols_cache,
            )
            for k in total_stats:
                total_stats[k] += stats.get(k, 0)

            if (i + 1) % 20 == 0:
                print(f"    [{i + 1}/{len(files)}] processed...")

        # Shutdown server
        client.shutdown()

        # Cleanup pyrightconfig
        if pyright_config and os.path.exists(pyright_config):
            os.remove(pyright_config)

    elapsed = time.time() - start_time

    # Update timestamp marker
    marker = os.path.join(project_root, ".lsp_pass_timestamp")
    Path(marker).touch()

    print(f"\n--- LSP Pass Complete ---")
    print(f"Duration: {elapsed:.1f}s")
    print(f"Definitions stored: {total_stats['definitions']}")
    print(f"Implementations stored: {total_stats['implementations']}")
    print(f"Call edges stored: {total_stats['calls']}")
    print(f"Errors: {total_stats['errors']}")


def main():
    parser = argparse.ArgumentParser(description="Toroidal-Indexer Tier 2: LSP pass")
    parser.add_argument("--project", required=True, help="Project root path")
    parser.add_argument(
        "--incremental", action="store_true", help="Only process changed files"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show plan without executing"
    )
    parser.add_argument(
        "--languages", help="Comma-separated language filter (e.g. python,rust)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--project-name", help="Override project name (default: basename of --project)"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    langs = set(args.languages.split(",")) if args.languages else None
    project_name = args.project_name or os.path.basename(os.path.abspath(args.project))

    run(
        args.project,
        project_name,
        incremental=args.incremental,
        dry_run=args.dry_run,
        languages=langs,
    )


if __name__ == "__main__":
    main()
