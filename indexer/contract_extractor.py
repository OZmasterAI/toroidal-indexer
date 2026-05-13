"""Structural contract extraction: detect API boundaries from indexed code graph.

Graph pass: query code_nodes, match against pattern registry.
Manifest pass: parse dependency files for lib contracts.
"""

import hashlib
import json
import os
import re

from surrealdb import RecordID

from indexer.contract_patterns import (
    load_contract_patterns,
    match_pattern,
    normalize_contract_id,
)
from indexer.schema import _node_key


def _contract_key(project, contract_type, contract_id, role):
    raw = f"{project}:{contract_type}:{contract_id}:{role}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def extract_contracts_structural(db, project, project_root=None):
    """Extract contracts from indexed code_nodes using pattern matching.

    Graph pass: test each node against http, grpc, topic patterns.
    Manifest pass: if project_root given, parse manifests for lib contracts.
    """
    patterns = load_contract_patterns()
    contracts = []

    # Graph pass: query all nodes for this project
    nodes = db.query(
        "SELECT id, name, file, type, line FROM code_node "
        "WHERE project=$p AND type != 'file'",
        {"p": project},
    )
    if not nodes:
        return []

    for node in nodes:
        node_name = node.get("name", "")
        node_file = node.get("file", "")
        node_type = node.get("type", "function")
        node_id = node.get("id")

        # Get callees for callee-based patterns
        callees = None
        for contract_type_key in ("http", "grpc", "topic", "onchain"):
            type_config = patterns.get(contract_type_key)
            if not type_config:
                continue

            # Lazy-load callees only if needed
            needs_callees = any(
                p.get("callee_re")
                for p in type_config["patterns"]
                if "manifest" not in p
            )
            if needs_callees and callees is None:
                callees = _get_callees(db, node_id)

            result = match_pattern(
                type_config,
                file=node_file,
                name=node_name,
                node_type=node_type,
                callees=callees,
            )
            if result:
                contracts.append(
                    {
                        "contract_id": result["contract_id"],
                        "contract_type": contract_type_key,
                        "role": result["role"],
                        "symbol_name": node_name,
                        "symbol_file": node_file,
                        "symbol_node": node_id,
                        "confidence": result["confidence"],
                        "project": project,
                        "meta": {"pattern": result["pattern_name"]},
                    }
                )
                break  # One contract per node

    # Manifest pass
    if project_root:
        lib_contracts = _extract_lib_contracts(db, project, project_root, patterns)
        contracts.extend(lib_contracts)

    return contracts


def _get_callees(db, node_id):
    """Get callee names for a node via ->calls-> edges."""
    rows = db.query(
        "SELECT ->calls->code_node.name AS callees FROM $id",
        {"id": node_id},
    )
    if not rows or not rows[0].get("callees"):
        return []
    return [c for c in rows[0]["callees"] if isinstance(c, str)]


def _extract_lib_contracts(db, project, project_root, patterns):
    """Parse manifest files for library provider/consumer contracts."""
    lib_config = patterns.get("lib")
    if not lib_config:
        return []

    contracts = []
    indexed_projects = _get_indexed_projects(db)

    for pattern in lib_config["patterns"]:
        manifest_name = pattern.get("manifest")
        if not manifest_name:
            continue

        manifest_path = os.path.join(project_root, manifest_name)
        if not os.path.isfile(manifest_path):
            continue

        try:
            deps = _parse_manifest(manifest_path, manifest_name, pattern)
        except Exception:
            continue

        pkg_name = deps.get("_provider_name")
        if pkg_name:
            contracts.append(
                {
                    "contract_id": normalize_contract_id(f"lib::{pkg_name}", "lib"),
                    "contract_type": "lib",
                    "role": "provider",
                    "symbol_name": pkg_name,
                    "symbol_file": manifest_name,
                    "symbol_node": None,
                    "confidence": 1.0,
                    "project": project,
                    "meta": {"manifest": manifest_name},
                }
            )

        for dep_name, conf in deps.get("_dependencies", []):
            cid = normalize_contract_id(f"lib::{dep_name}", "lib")
            is_local = dep_name in indexed_projects
            contracts.append(
                {
                    "contract_id": cid,
                    "contract_type": "lib",
                    "role": "consumer",
                    "symbol_name": dep_name,
                    "symbol_file": manifest_name,
                    "symbol_node": None,
                    "confidence": 1.0 if is_local else conf,
                    "project": project,
                    "meta": {"manifest": manifest_name, "local": is_local},
                }
            )

    return contracts


def _get_indexed_projects(db):
    """Get set of all indexed project names."""
    rows = db.query("SELECT project FROM code_node GROUP BY project")
    return {r["project"] for r in rows} if rows else set()


def _parse_manifest(path, manifest_name, pattern):
    """Parse a manifest file and return provider name + dependencies."""
    result = {"_dependencies": []}

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    if manifest_name == "package.json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return result
        result["_provider_name"] = data.get("name")
        for dep in data.get("dependencies", {}):
            result["_dependencies"].append((dep, 0.8))
        for dep in data.get("devDependencies", {}):
            result["_dependencies"].append((dep, 0.5))

    elif manifest_name == "Cargo.toml":
        import tomllib

        try:
            data = tomllib.loads(content)
        except Exception:
            return result
        result["_provider_name"] = data.get("package", {}).get("name")
        for dep in data.get("dependencies", {}):
            result["_dependencies"].append((dep, 0.8))
        for dep in data.get("dev-dependencies", {}):
            result["_dependencies"].append((dep, 0.5))

    elif manifest_name == "pyproject.toml":
        import tomllib

        try:
            data = tomllib.loads(content)
        except Exception:
            return result
        proj = data.get("project", {})
        result["_provider_name"] = proj.get("name")
        for dep in proj.get("dependencies", []):
            name = re.split(r"[\[>=<~!;]", dep)[0].strip()
            if name:
                result["_dependencies"].append((name, 0.8))

    elif manifest_name == "go.mod":
        mod_match = re.search(r"^module\s+(\S+)", content, re.MULTILINE)
        result["_provider_name"] = mod_match.group(1) if mod_match else None
        for block in re.finditer(r"require\s*\((.*?)\)", content, re.DOTALL):
            for line in block.group(1).splitlines():
                dep = line.strip().split()
                if dep and not dep[0].startswith("//"):
                    result["_dependencies"].append((dep[0], 0.8))

    elif manifest_name == "build.gradle":
        for m in re.finditer(r"['\"]([^'\"]+:[^'\"]+:[^'\"]+)['\"]", content):
            parts = m.group(1).split(":")
            if len(parts) >= 2:
                result["_dependencies"].append((f"{parts[0]}:{parts[1]}", 0.8))

    elif manifest_name == "composer.json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return result
        result["_provider_name"] = data.get("name")
        for dep in data.get("require", {}):
            if dep != "php":
                result["_dependencies"].append((dep, 0.8))

    elif manifest_name == "Gemfile":
        for m in re.finditer(r"gem\s+['\"]([^'\"]+)['\"]", content):
            result["_dependencies"].append((m.group(1), 0.8))

    return result


def store_contracts(db, project, contracts):
    """Store contracts in code_contract table. Idempotent via delete+insert."""
    db.query("DELETE code_contract WHERE project=$p", {"p": project})
    for c in contracts:
        key = _contract_key(project, c["contract_type"], c["contract_id"], c["role"])
        rid = RecordID("code_contract", key)
        db.query(
            "UPSERT $id SET project=$p, contract_id=$cid, contract_type=$ct, "
            "role=$role, symbol_name=$sn, symbol_file=$sf, confidence=$conf, "
            "meta=$meta",
            {
                "id": rid,
                "p": project,
                "cid": c["contract_id"],
                "ct": c["contract_type"],
                "role": c["role"],
                "sn": c["symbol_name"],
                "sf": c["symbol_file"],
                "conf": c["confidence"],
                "meta": c.get("meta", {}),
            },
        )
        # Link to code_node if available
        if c.get("symbol_node"):
            db.query(
                "UPDATE $id SET symbol_node=$node",
                {"id": rid, "node": c["symbol_node"]},
            )


def store_cross_links(db, links):
    """Store contract_link edges. Direction: consumer->contract_link->provider."""
    for link in links:
        try:
            db.query(
                "RELATE $consumer->contract_link->$provider "
                "SET match_type=$mt, confidence=$conf, contract_id=$cid",
                {
                    "consumer": link["consumer"],
                    "provider": link["provider"],
                    "mt": link["match_type"],
                    "conf": link["confidence"],
                    "cid": link["contract_id"],
                },
            )
        except Exception:
            pass


def detect_contracts(db, project, project_root=None):
    """Orchestrator: extract + store contracts, return summary."""
    contracts = extract_contracts_structural(db, project, project_root)
    store_contracts(db, project, contracts)

    providers = [c for c in contracts if c["role"] == "provider"]
    consumers = [c for c in contracts if c["role"] == "consumer"]

    return {
        "contracts": len(contracts),
        "providers": len(providers),
        "consumers": len(consumers),
        "by_type": _count_by_type(contracts),
    }


def _count_by_type(contracts):
    counts = {}
    for c in contracts:
        ct = c["contract_type"]
        counts[ct] = counts.get(ct, 0) + 1
    return counts


def sync_group(db, group_name):
    """Sync all projects in a group: detect contracts, match, store cross-links."""
    from indexer.contract_matcher import match_contracts

    group = db.query(
        "SELECT * FROM project_group WHERE name=$n",
        {"n": group_name},
    )
    if not group:
        return {"error": f"Group '{group_name}' not found"}

    members = group[0].get("members", [])
    all_contracts = []

    for member in members:
        proj = member.get("project", "")
        path = member.get("path")
        contracts = extract_contracts_structural(db, proj, path)
        store_contracts(db, proj, contracts)
        all_contracts.extend(contracts)

    providers = [c for c in all_contracts if c["role"] == "provider"]
    consumers = [c for c in all_contracts if c["role"] == "consumer"]
    links = match_contracts(providers, consumers)

    # Resolve contract dicts to SurrealDB record IDs for storage
    stored_links = []
    for link in links:
        consumer_key = _contract_key(
            link["consumer"]["project"],
            link["consumer"]["contract_type"],
            link["consumer"]["contract_id"],
            "consumer",
        )
        provider_key = _contract_key(
            link["provider"]["project"],
            link["provider"]["contract_type"],
            link["provider"]["contract_id"],
            "provider",
        )
        stored_links.append(
            {
                "consumer": RecordID("code_contract", consumer_key),
                "provider": RecordID("code_contract", provider_key),
                "match_type": link["match_type"],
                "confidence": link["confidence"],
                "contract_id": link["contract_id"],
            }
        )

    # Clear old cross-links for these projects, then store new
    for member in members:
        db.query(
            "DELETE contract_link WHERE in.project=$p OR out.project=$p",
            {"p": member["project"]},
        )
    store_cross_links(db, stored_links)

    return {
        "projects": len(members),
        "total_contracts": len(all_contracts),
        "providers": len(providers),
        "consumers": len(consumers),
        "links_created": len(stored_links),
    }
