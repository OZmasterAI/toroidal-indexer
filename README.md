# Toroidal-Indexer

Structural code graph builder for the [Torus Framework](https://github.com/OZmasterAI/torus-framework). Indexes codebases into a SurrealDB graph database, enabling AI-powered code navigation queries like "who calls this function?" and "what's the blast radius of changing this file?"

## Architecture

Three-tier indexing pipeline, each tier adding higher-confidence edges:

| Tier | Method | Confidence | Script |
|------|--------|-----------|--------|
| T1 — Static | AST extraction (Python, TypeScript, Rust) | 1.0 | `indexer.build` |
| T2 — LSP | Language server analysis (pyright, typescript-language-server, rust-analyzer) | 0.9 | `scripts/lsp_pass.py` |
| T3 — AI | LLM-based semantic analysis for edges AST/LSP can't resolve | 0.7 | `scripts/ai_pass.py` |

### Graph Schema

- **Nodes**: `code_node` records with `{project, file, name, type, line}`
- **Edges**: `calls`, `imports`, `reads`, `writes`, `implements` — each with `confidence` and `source_line`
- **Storage**: SurrealDB (`ws://127.0.0.1:8822`, namespace `code_graph`)

## Installation

```bash
pip install -e ./toroidal-indexer
```

Requires SurrealDB running locally. Optional: pyright, typescript-language-server, rust-analyzer for Tier 2.

## Usage

### Build a code graph (Tier 1)

```python
from indexer import connect_code_graph, init_code_tables, full_build

db = connect_code_graph()
init_code_tables(db)
full_build(db, "/path/to/project", "my-project")
```

### Run LSP pass (Tier 2)

```bash
python3 scripts/lsp_pass.py --project /path/to/project
python3 scripts/lsp_pass.py --project /path/to/project --incremental --languages python,rust
```

### Run AI pass (Tier 3)

```bash
python3 scripts/ai_pass.py --project /path/to/project
```

### Query the graph

```python
from indexer import connect_code_graph, code_callers, code_blast_radius, code_path

db = connect_code_graph()

# Who calls this function?
callers = code_callers(db, "my-project", "schema.py", "upsert_node", depth=2)

# What's affected if this file changes?
blast = code_blast_radius(db, "my-project", "schema.py", depth=3)

# Shortest call path between two functions
path = code_path(db, "my-project", "api.py", "handle_request", "db.py", "execute_query")
```

### MCP Server

Exposes 5 query tools via MCP (Model Context Protocol):

```bash
python3 indexer_server.py --http --port 8748
```

Tools: `code_callers`, `code_readers`, `code_path`, `code_blast_radius`, `code_hubs`

## Package Structure

```
indexer/
  schema.py          # SurrealDB schema, connect, upsert, relate
  build.py           # Full + incremental build orchestrator
  lsp.py             # LSP-to-graph edge storage
  lsp_client.py      # LSP JSON-RPC client with message queue
  lsp_configs.py     # Per-language server configs
  mcp_queries.py     # Graph query functions (callers, path, blast radius)
  extractors/
    python.py        # Python AST extractor
    typescript.py    # TypeScript AST extractor
    rust.py          # Rust AST extractor
scripts/
  lsp_pass.py        # Tier 2 CLI
  ai_pass.py         # Tier 3 CLI
  dedup.py           # Node deduplication utility
tests/               # 14 test files, ~120 tests
indexer_server.py    # MCP server entry point
```

## Testing

```bash
pytest tests/ -q
```

LSP integration tests (`test_lsp_client.py`, `test_lsp_queries.py`) require pyright to be installed.

## License

Apache 2.0
