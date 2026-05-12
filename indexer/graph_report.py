"""Generate a static GRAPH_REPORT.md from the code graph in SurrealDB.

Called after full_build / incremental_build. Writes to
<project_root>/.claude/GRAPH_REPORT.md so the model can Read it
instead of constructing live MCP queries.
"""

import os
from datetime import datetime, timezone


def _query_rows(db, sql, params=None):
    result = db.query(sql, params) if params else db.query(sql)
    return result if isinstance(result, list) else []


def generate_report(db, project_name, project_root):
    """Query the graph and write GRAPH_REPORT.md. Returns the output path."""
    lines = [
        f"# Code Graph: {project_name}",
        f"_Auto-generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — do not edit._",
        "",
    ]

    # Stats
    node_count = _query_rows(
        db,
        "SELECT count() AS c FROM code_node WHERE project=$p GROUP ALL",
        {"p": project_name},
    )
    file_count = _query_rows(
        db,
        "SELECT count() AS c FROM code_node WHERE project=$p AND type='file' GROUP ALL",
        {"p": project_name},
    )
    nc = node_count[0]["c"] if node_count else 0
    fc = file_count[0]["c"] if file_count else 0
    lines.append(f"**{nc} nodes across {fc} files**")
    lines.append("")

    # Hubs (god nodes)
    hubs = _query_rows(
        db,
        """SELECT name, file,
            array::len(->calls->code_node) + array::len(<-calls<-code_node) +
            array::len(->imports->code_node) + array::len(<-imports<-code_node) +
            array::len(->reads->code_node) + array::len(<-reads<-code_node) +
            array::len(->writes->code_node) + array::len(<-writes<-code_node) +
            array::len(->implements->code_node) + array::len(<-implements<-code_node) AS degree
        FROM code_node WHERE project=$p ORDER BY degree DESC LIMIT 15""",
        {"p": project_name},
    )
    hubs = [h for h in hubs if h.get("degree", 0) > 0]
    if hubs:
        lines.append("## Key Hubs")
        lines.append("| Node | File | Connections |")
        lines.append("|------|------|------------|")
        for h in hubs:
            lines.append(f"| {h['name']} | {h['file']} | {h['degree']} |")
        lines.append("")

    # Clusters
    clusters = _query_rows(
        db,
        "SELECT label, node_count, key_files, key_functions "
        "FROM code_cluster WHERE project=$p ORDER BY node_count DESC",
        {"p": project_name},
    )
    significant = [c for c in clusters if c["node_count"] >= 5]
    if significant:
        lines.append("## Communities")
        for c in significant[:20]:
            label = c["label"]
            count = c["node_count"]
            files = c.get("key_files", [])[:4]
            funcs = c.get("key_functions", [])[:4]
            parts = []
            if files:
                parts.append("files: " + ", ".join(f"`{f}`" for f in files))
            if funcs:
                parts.append("fns: " + ", ".join(f"`{f}`" for f in funcs))
            detail = " | ".join(parts) if parts else ""
            lines.append(f"- **{label}** ({count}) — {detail}")
        lines.append("")

    # File index (top-level directories with file counts)
    all_files = _query_rows(
        db,
        "SELECT file FROM code_node WHERE project=$p AND type='file'",
        {"p": project_name},
    )
    if all_files:
        dirs = {}
        for f in all_files:
            top = f["file"].split("/")[0] if "/" in f["file"] else "."
            dirs[top] = dirs.get(top, 0) + 1
        lines.append("## Directory Map")
        for d in sorted(dirs, key=lambda x: -dirs[x]):
            lines.append(f"- `{d}/` — {dirs[d]} files")
        lines.append("")

    # Footer with tool hints
    lines.append("---")
    lines.append("For deeper queries use `run_tool('indexer', '<tool>', ...)`:")
    lines.append("- `code_search` — fuzzy find by name or keyword")
    lines.append("- `code_callers` / `code_readers` — trace call/read chains")
    lines.append("- `code_blast_radius` — impact analysis for a function")
    lines.append("- `code_clusters` / `code_cluster_detail` — community details")

    report = "\n".join(lines) + "\n"

    out_dir = os.path.join(project_root, ".claude")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "GRAPH_REPORT.md")
    with open(out_path, "w") as f:
        f.write(report)

    return out_path
