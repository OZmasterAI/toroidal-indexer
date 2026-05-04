"""Toroidal-Indexer: structural code graph for the Torus Framework."""

from indexer.schema import (
    NAMESPACE,
    SURREAL_URL,
    VALID_RELATIONS,
    connect_code_graph,
    dedup_nodes,
    delete_file_nodes,
    get_callers,
    get_readers,
    init_code_tables,
    relate,
    upsert_node,
)
from indexer.build import (
    full_build,
    get_changed_files,
    incremental_build,
)
from indexer.lsp import (
    build_line_to_name_map,
    enrich_node_types,
    resolve_target_node,
    store_call_hierarchy_edges,
    store_definition_edges,
    store_implementation_edges,
)
from indexer.mcp_queries import (
    code_blast_radius,
    code_callers,
    code_hubs,
    code_path,
    code_readers,
)
