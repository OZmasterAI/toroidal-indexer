"""Leiden community clustering for the toroidal-indexer code graph.

Loads nodes + edges from SurrealDB into an igraph graph, runs Leiden
community detection, generates human-readable labels, and stores results.
"""

import argparse
import os
import sys
from collections import Counter

import igraph as ig
import leidenalg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.schema import VALID_RELATIONS, connect_code_graph


def load_project_graph(db, project: str) -> ig.Graph:
    """Load all nodes + edges for a project into an igraph Graph."""
    nodes = db.query(
        "SELECT id, name, file, type, line FROM code_node WHERE project=$proj",
        {"proj": project},
    )
    if not nodes:
        return ig.Graph(directed=False)

    node_id_to_idx = {}
    g = ig.Graph(directed=False)

    for i, node in enumerate(nodes):
        rid = node["id"]
        rid_str = str(rid)
        node_id_to_idx[rid_str] = i
        g.add_vertex(
            name=node.get("name", ""),
            file=node.get("file", ""),
            type=node.get("type", ""),
            line=node.get("line", 0),
            rid=rid_str,
        )

    for rel in VALID_RELATIONS:
        edges = db.query(
            f"SELECT in, out FROM {rel} WHERE in.project=$proj OR out.project=$proj",
            {"proj": project},
        )
        if not edges:
            continue
        for edge in edges:
            src_str = str(edge["in"])
            tgt_str = str(edge["out"])
            src_idx = node_id_to_idx.get(src_str)
            tgt_idx = node_id_to_idx.get(tgt_str)
            if src_idx is not None and tgt_idx is not None and src_idx != tgt_idx:
                if not g.are_adjacent(src_idx, tgt_idx):
                    g.add_edge(src_idx, tgt_idx)

    return g


def generate_cluster_label(nodes: list[dict]) -> str:
    """Generate a human-readable label from cluster members.

    Uses most common directory path segments and node name fragments.
    """
    if not nodes:
        return "unknown"

    dir_counts: Counter = Counter()
    name_fragments: Counter = Counter()

    for node in nodes:
        file_path = node.get("file", "")
        parts = file_path.replace("\\", "/").split("/")
        for part in parts[:-1]:
            cleaned = part.strip().lower()
            if (
                cleaned
                and len(cleaned) >= 2
                and cleaned
                not in (
                    "src",
                    "lib",
                    "app",
                    "components",
                    "pages",
                    "utils",
                    "shared",
                    "common",
                    "index",
                )
            ):
                dir_counts[cleaned] += 1

        name = node.get("name", "")
        name_lower = name.lower()
        for fragment in _split_camel_case(name_lower):
            if len(fragment) >= 3 and fragment not in (
                "get",
                "set",
                "use",
                "the",
                "new",
                "init",
                "handle",
                "default",
                "create",
            ):
                name_fragments[fragment] += 1

    if dir_counts:
        top_dir = dir_counts.most_common(1)[0][0]
        return top_dir

    if name_fragments:
        top_frag = name_fragments.most_common(1)[0][0]
        return top_frag

    first_name = nodes[0].get("name", "unknown")
    return _split_camel_case(first_name.lower())[0] if first_name else "unknown"


def _split_camel_case(name: str) -> list[str]:
    """Split camelCase/PascalCase into fragments."""
    fragments = []
    current = []
    for ch in name:
        if ch.isupper() and current:
            fragments.append("".join(current).lower())
            current = [ch]
        elif ch in ("_", "-", "."):
            if current:
                fragments.append("".join(current).lower())
                current = []
        else:
            current.append(ch)
    if current:
        fragments.append("".join(current).lower())
    return fragments if fragments else [name]


def run_clustering(db, project: str, resolution: float = 1.0) -> dict:
    """Run Leiden clustering on the project graph, store results in SurrealDB.

    Returns summary dict with cluster count and sizes.
    """
    g = load_project_graph(db, project)
    if g.vcount() == 0:
        return {"clusters": 0, "nodes": 0}

    partition = leidenalg.find_partition(
        g,
        leidenalg.RBConfigurationVertexPartition,
        resolution_parameter=resolution,
    )

    db.query("DEFINE TABLE IF NOT EXISTS code_cluster SCHEMALESS")
    db.query("DELETE FROM code_cluster WHERE project=$proj", {"proj": project})

    cluster_summaries = []
    for cluster_id, members in enumerate(partition):
        member_nodes = []
        for idx in members:
            v = g.vs[idx]
            member_nodes.append(
                {
                    "name": v["name"],
                    "file": v["file"],
                    "type": v["type"],
                    "rid": v["rid"],
                }
            )

        label = generate_cluster_label(member_nodes)

        # Deduplicate labels by appending index if needed
        existing_labels = [s["label"] for s in cluster_summaries]
        if label in existing_labels:
            label = f"{label}-{cluster_id}"

        key_files = list(dict.fromkeys(n["file"] for n in member_nodes))[:5]
        key_functions = [
            n["name"] for n in member_nodes if n["type"] in ("function", "class")
        ][:5]

        from surrealdb import RecordID

        cluster_rid = RecordID("code_cluster", f"{project}_{cluster_id}")
        db.query(
            "UPSERT $id SET project=$proj, label=$label, cluster_id=$cid, "
            "node_count=$cnt, key_files=$files, key_functions=$fns",
            {
                "id": cluster_rid,
                "proj": project,
                "label": label,
                "cid": cluster_id,
                "cnt": len(member_nodes),
                "files": key_files,
                "fns": key_functions,
            },
        )

        for node in member_nodes:
            db.query(
                "UPDATE $rid SET cluster_id=$cid, cluster_label=$label",
                {
                    "rid": RecordID("code_node", node["rid"].split(":")[1])
                    if ":" in node["rid"]
                    else RecordID("code_node", node["rid"]),
                    "cid": cluster_id,
                    "label": label,
                },
            )

        cluster_summaries.append(
            {
                "cluster_id": cluster_id,
                "label": label,
                "node_count": len(member_nodes),
                "key_files": key_files,
                "key_functions": key_functions,
            }
        )

    return {
        "clusters": len(cluster_summaries),
        "nodes": g.vcount(),
        "summaries": cluster_summaries,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run Leiden clustering on a code graph project"
    )
    parser.add_argument("--project", required=True, help="Project name to cluster")
    parser.add_argument(
        "--resolution", type=float, default=1.0, help="Leiden resolution parameter"
    )
    parser.add_argument(
        "--database",
        default=None,
        help="SurrealDB database name (default: from INDEXER_DB or 'main')",
    )
    args = parser.parse_args()

    db_name = args.database or os.environ.get("INDEXER_DB", "main")
    db = connect_code_graph(database=db_name)

    result = run_clustering(db, args.project, resolution=args.resolution)
    print(f"Clustered {result['nodes']} nodes into {result['clusters']} communities:")
    for s in result.get("summaries", []):
        print(
            f"  [{s['cluster_id']}] {s['label']}: {s['node_count']} nodes — {', '.join(s['key_functions'][:3])}"
        )


if __name__ == "__main__":
    main()
