"""Tests for cross-repo contract detection, matching, and cross-project impact."""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.schema import (
    connect_code_graph,
    init_code_tables,
    relate,
    upsert_node,
)

SURREAL_URL = "ws://127.0.0.1:8822"


@pytest.fixture(scope="module")
def db():
    """Connect to SurrealDB with a unique test database, seed a graph for contract tests."""
    test_db = f"test_contracts_{uuid.uuid4().hex[:8]}"
    conn = connect_code_graph(url=SURREAL_URL, database=test_db)
    init_code_tables(conn)

    # --- Project A: "backend" (API provider) ---
    # Next.js-style API routes
    get_users = upsert_node(
        conn, "backend", "app/api/users/route.ts", "GET", "function", 5
    )
    post_users = upsert_node(
        conn, "backend", "app/api/users/route.ts", "POST", "function", 20
    )
    get_user_by_id = upsert_node(
        conn, "backend", "app/api/users/[id]/route.ts", "GET", "function", 5
    )
    delete_user = upsert_node(
        conn, "backend", "app/api/users/[id]/route.ts", "DELETE", "function", 30
    )
    get_health = upsert_node(
        conn, "backend", "app/api/health/route.ts", "GET", "function", 1
    )

    # Internal helpers (not API boundaries)
    db_connect = upsert_node(conn, "backend", "lib/db.ts", "connectDB", "function", 10)
    auth_fn = upsert_node(conn, "backend", "lib/auth.ts", "requireAdmin", "function", 5)

    relate(conn, get_users, "calls", db_connect)
    relate(conn, post_users, "calls", db_connect)
    relate(conn, post_users, "calls", auth_fn)
    relate(conn, get_user_by_id, "calls", db_connect)
    relate(conn, delete_user, "calls", auth_fn)

    # --- Project B: "frontend" (API consumer) ---
    fetch_users = upsert_node(
        conn, "frontend", "lib/api.ts", "fetchUsers", "function", 10
    )
    create_user = upsert_node(
        conn, "frontend", "lib/api.ts", "createUser", "function", 25
    )
    delete_user_fe = upsert_node(
        conn, "frontend", "lib/api.ts", "deleteUser", "function", 40
    )

    page_comp = upsert_node(
        conn, "frontend", "app/users/page.tsx", "UsersPage", "function", 1
    )
    relate(conn, page_comp, "calls", fetch_users)
    relate(conn, page_comp, "calls", delete_user_fe)

    # --- Project C: "shared-lib" (library provider) ---
    shared_util = upsert_node(
        conn, "shared-lib", "src/index.ts", "formatDate", "function", 5
    )

    # --- Flask/FastAPI provider (project D) ---
    flask_route = upsert_node(
        conn, "pybackend", "app/routes/users.py", "get_users", "function", 10
    )
    fastapi_route = upsert_node(
        conn, "pybackend", "app/routes/items.py", "create_item", "function", 15
    )

    yield conn
    conn.query(f"REMOVE DATABASE IF EXISTS {test_db}")


# ═══════════════════════════════════════════════════════════════
# Task 1: Schema
# ═══════════════════════════════════════════════════════════════


class TestContractSchema:
    def test_contract_table_exists(self, db):
        result = db.query("INFO FOR TABLE code_contract")
        assert result is not None

    def test_contract_link_table_exists(self, db):
        result = db.query("INFO FOR TABLE contract_link")
        assert result is not None

    def test_project_group_table_exists(self, db):
        result = db.query("INFO FOR TABLE project_group")
        assert result is not None

    def test_contract_project_index(self, db):
        info = db.query("INFO FOR TABLE code_contract")
        assert "contract_project" in str(info)

    def test_contract_id_index(self, db):
        info = db.query("INFO FOR TABLE code_contract")
        assert "contract_id_idx" in str(info)

    def test_contract_link_dedup_index(self, db):
        info = db.query("INFO FOR TABLE contract_link")
        assert "contract_link_dedup" in str(info)

    def test_group_name_index(self, db):
        info = db.query("INFO FOR TABLE project_group")
        assert "group_name" in str(info)

    def test_contract_link_in_valid_relations(self):
        from indexer.schema import VALID_CONTRACT_RELATIONS

        assert "contract_link" in VALID_CONTRACT_RELATIONS


# ═══════════════════════════════════════════════════════════════
# Task 2: Pattern Registry
# ═══════════════════════════════════════════════════════════════


class TestPatternRegistry:
    def test_registry_loads(self):
        from indexer.contract_patterns import load_contract_patterns

        registry = load_contract_patterns()
        assert "http" in registry
        assert "lib" in registry
        assert len(registry["http"]["patterns"]) > 0

    def test_http_provider_match_nextjs_app_router(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["http"]
        result = match_pattern(
            patterns, file="app/api/users/route.ts", name="GET", node_type="function"
        )
        assert result is not None
        assert result["role"] == "provider"
        assert "http::get" in result["contract_id"]

    def test_http_provider_match_nextjs_param(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["http"]
        result = match_pattern(
            patterns,
            file="app/api/users/[id]/route.ts",
            name="GET",
            node_type="function",
        )
        assert result is not None
        assert "{param}" in result["contract_id"]

    def test_http_provider_match_flask(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["http"]
        result = match_pattern(
            patterns,
            file="app/routes/users.py",
            name="get_users",
            node_type="function",
            source_content='@app.route("/api/users", methods=["GET"])',
        )
        assert result is not None
        assert result["role"] == "provider"

    def test_http_consumer_match_fetch(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["http"]
        result = match_pattern(
            patterns,
            file="lib/api.ts",
            name="fetchUsers",
            node_type="function",
            callees=["fetch"],
        )
        assert result is not None
        assert result["role"] == "consumer"

    def test_no_match_for_internal_function(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["http"]
        result = match_pattern(
            patterns, file="lib/utils.ts", name="formatDate", node_type="function"
        )
        assert result is None

    def test_normalize_http_path(self):
        from indexer.contract_patterns import normalize_contract_id

        assert (
            normalize_contract_id("http::GET::/api/Users/[id]", "http")
            == "http::get::/api/users/{param}"
        )
        assert (
            normalize_contract_id("http::POST::/api/users/:userId/", "http")
            == "http::post::/api/users/{param}"
        )
        assert (
            normalize_contract_id("http::GET::/api/items/{itemId}", "http")
            == "http::get::/api/items/{param}"
        )

    def test_normalize_lib(self):
        from indexer.contract_patterns import normalize_contract_id

        assert (
            normalize_contract_id("lib::@Torus/Shared-Utils", "lib")
            == "lib::@torus/shared-utils"
        )

    def test_normalize_topic(self):
        from indexer.contract_patterns import normalize_contract_id

        assert (
            normalize_contract_id("topic::User.Created", "topic")
            == "topic::user.created"
        )

    def test_registry_covers_multiple_frameworks(self):
        from indexer.contract_patterns import load_contract_patterns

        registry = load_contract_patterns()
        http_patterns = registry["http"]["patterns"]
        assert any("app/api" in str(p.get("file_re", "")) for p in http_patterns)

    def test_lib_patterns_cover_manifests(self):
        from indexer.contract_patterns import load_contract_patterns

        registry = load_contract_patterns()
        lib_patterns = registry["lib"]["patterns"]
        manifests = [p.get("manifest", "") for p in lib_patterns]
        assert "package.json" in manifests
        assert "Cargo.toml" in manifests
        assert "pyproject.toml" in manifests
        assert "go.mod" in manifests


# ═══════════════════════════════════════════════════════════════
# Task 3: Structural Contract Extractor
# ═══════════════════════════════════════════════════════════════


class TestStructuralExtractor:
    def test_extract_http_routes(self, db):
        from indexer.contract_extractor import extract_contracts_structural

        contracts = extract_contracts_structural(db, "backend")
        providers = [
            c
            for c in contracts
            if c["role"] == "provider" and c["contract_type"] == "http"
        ]
        assert len(providers) >= 3
        assert any("GET" in c["contract_id"].upper() for c in providers)

    def test_extract_param_routes(self, db):
        from indexer.contract_extractor import extract_contracts_structural

        contracts = extract_contracts_structural(db, "backend")
        param_routes = [c for c in contracts if "{param}" in c["contract_id"]]
        assert len(param_routes) >= 1

    def test_no_internal_functions_as_contracts(self, db):
        from indexer.contract_extractor import extract_contracts_structural

        contracts = extract_contracts_structural(db, "backend")
        names = [c["symbol_name"] for c in contracts]
        assert "connectDB" not in names
        assert "requireAdmin" not in names

    def test_contract_structure(self, db):
        from indexer.contract_extractor import extract_contracts_structural

        contracts = extract_contracts_structural(db, "backend")
        for c in contracts:
            assert "contract_id" in c
            assert "contract_type" in c
            assert "role" in c
            assert "symbol_name" in c
            assert "symbol_file" in c
            assert "confidence" in c
            assert c["project"] == "backend"

    def test_extract_returns_empty_for_unknown_project(self, db):
        from indexer.contract_extractor import extract_contracts_structural

        contracts = extract_contracts_structural(db, "nonexistent")
        assert contracts == []


# ═══════════════════════════════════════════════════════════════
# Task 6: Contract Matching Engine
# ═══════════════════════════════════════════════════════════════


class TestContractMatcher:
    def test_exact_match(self):
        from indexer.contract_matcher import match_contracts

        providers = [
            {
                "contract_id": "http::get::/api/users/{param}",
                "project": "backend",
                "contract_type": "http",
                "role": "provider",
            }
        ]
        consumers = [
            {
                "contract_id": "http::get::/api/users/{param}",
                "project": "frontend",
                "contract_type": "http",
                "role": "consumer",
            }
        ]
        links = match_contracts(providers, consumers)
        assert len(links) == 1
        assert links[0]["match_type"] == "exact"

    def test_no_self_match(self):
        from indexer.contract_matcher import match_contracts

        providers = [
            {
                "contract_id": "http::get::/api/foo",
                "project": "A",
                "contract_type": "http",
                "role": "provider",
            }
        ]
        consumers = [
            {
                "contract_id": "http::get::/api/foo",
                "project": "A",
                "contract_type": "http",
                "role": "consumer",
            }
        ]
        links = match_contracts(providers, consumers)
        assert len(links) == 0

    def test_wildcard_lib_match(self):
        from indexer.contract_matcher import match_contracts

        providers = [
            {
                "contract_id": "lib::@torus/shared-utils",
                "project": "shared-lib",
                "contract_type": "lib",
                "role": "provider",
            }
        ]
        consumers = [
            {
                "contract_id": "lib::@torus/shared-utils",
                "project": "frontend",
                "contract_type": "lib",
                "role": "consumer",
            }
        ]
        links = match_contracts(providers, consumers)
        assert len(links) == 1

    def test_multiple_consumers_one_provider(self):
        from indexer.contract_matcher import match_contracts

        providers = [
            {
                "contract_id": "http::get::/api/users",
                "project": "backend",
                "contract_type": "http",
                "role": "provider",
            }
        ]
        consumers = [
            {
                "contract_id": "http::get::/api/users",
                "project": "frontend",
                "contract_type": "http",
                "role": "consumer",
            },
            {
                "contract_id": "http::get::/api/users",
                "project": "mobile",
                "contract_type": "http",
                "role": "consumer",
            },
        ]
        links = match_contracts(providers, consumers)
        assert len(links) == 2

    def test_link_structure(self):
        from indexer.contract_matcher import match_contracts

        providers = [
            {
                "contract_id": "http::get::/api/x",
                "project": "A",
                "contract_type": "http",
                "role": "provider",
            }
        ]
        consumers = [
            {
                "contract_id": "http::get::/api/x",
                "project": "B",
                "contract_type": "http",
                "role": "consumer",
            }
        ]
        links = match_contracts(providers, consumers)
        assert len(links) == 1
        link = links[0]
        assert "consumer" in link
        assert "provider" in link
        assert "match_type" in link
        assert "confidence" in link
        assert "contract_id" in link

    def test_no_matches_different_ids(self):
        from indexer.contract_matcher import match_contracts

        providers = [
            {
                "contract_id": "http::get::/api/users",
                "project": "A",
                "contract_type": "http",
                "role": "provider",
            }
        ]
        consumers = [
            {
                "contract_id": "http::get::/api/items",
                "project": "B",
                "contract_type": "http",
                "role": "consumer",
            }
        ]
        links = match_contracts(providers, consumers)
        assert len(links) == 0


# ═══════════════════════════════════════════════════════════════
# Task 7: Store Contracts + Cross-Links
# ═══════════════════════════════════════════════════════════════


class TestStoreContracts:
    def test_store_contracts(self, db):
        from indexer.contract_extractor import store_contracts

        contracts = [
            {
                "contract_id": "http::get::/api/test",
                "contract_type": "http",
                "role": "provider",
                "symbol_name": "GET",
                "symbol_file": "api/test/route.ts",
                "confidence": 0.9,
                "project": "testproj",
                "meta": {"method": "GET"},
            }
        ]
        store_contracts(db, "testproj", contracts)
        rows = db.query("SELECT * FROM code_contract WHERE project='testproj'")
        assert len(rows) >= 1
        assert rows[0]["contract_id"] == "http::get::/api/test"

    def test_store_is_idempotent(self, db):
        from indexer.contract_extractor import store_contracts

        contracts = [
            {
                "contract_id": "http::get::/api/idem",
                "contract_type": "http",
                "role": "provider",
                "symbol_name": "GET",
                "symbol_file": "api/idem/route.ts",
                "confidence": 0.9,
                "project": "idemproj",
                "meta": {},
            }
        ]
        store_contracts(db, "idemproj", contracts)
        store_contracts(db, "idemproj", contracts)
        rows = db.query("SELECT * FROM code_contract WHERE project='idemproj'")
        assert len(rows) == 1

    def test_store_cross_links(self, db):
        from indexer.contract_extractor import store_contracts, store_cross_links

        store_contracts(
            db,
            "linkprov",
            [
                {
                    "contract_id": "http::get::/api/link",
                    "contract_type": "http",
                    "role": "provider",
                    "symbol_name": "GET",
                    "symbol_file": "api/link/route.ts",
                    "confidence": 0.9,
                    "project": "linkprov",
                    "meta": {},
                }
            ],
        )
        store_contracts(
            db,
            "linkcons",
            [
                {
                    "contract_id": "http::get::/api/link",
                    "contract_type": "http",
                    "role": "consumer",
                    "symbol_name": "fetchLink",
                    "symbol_file": "lib/api.ts",
                    "confidence": 0.8,
                    "project": "linkcons",
                    "meta": {},
                }
            ],
        )
        prov_rows = db.query("SELECT * FROM code_contract WHERE project='linkprov'")
        cons_rows = db.query("SELECT * FROM code_contract WHERE project='linkcons'")
        links = [
            {
                "consumer": cons_rows[0]["id"],
                "provider": prov_rows[0]["id"],
                "match_type": "exact",
                "confidence": 0.9,
                "contract_id": "http::get::/api/link",
            }
        ]
        store_cross_links(db, links)
        edges = db.query("SELECT * FROM contract_link")
        assert len(edges) >= 1

    def test_cross_link_direction(self, db):
        """contract_link edges: consumer->contract_link->provider."""
        edges = db.query("SELECT in, out FROM contract_link LIMIT 5")
        for e in edges:
            in_data = db.query("SELECT role FROM $id", {"id": e["in"]})
            out_data = db.query("SELECT role FROM $id", {"id": e["out"]})
            if in_data and out_data:
                assert in_data[0]["role"] == "consumer"
                assert out_data[0]["role"] == "provider"


# ═══════════════════════════════════════════════════════════════
# Task 8: Orchestrator + Build Integration
# ═══════════════════════════════════════════════════════════════


class TestOrchestrator:
    def test_detect_contracts(self, db):
        from indexer.contract_extractor import detect_contracts

        result = detect_contracts(db, "backend")
        assert result["contracts"] > 0

    def test_detect_contracts_returns_summary(self, db):
        from indexer.contract_extractor import detect_contracts

        result = detect_contracts(db, "backend")
        assert "contracts" in result
        assert "providers" in result
        assert "consumers" in result

    def test_sync_group(self, db):
        from indexer.contract_extractor import sync_group

        db.query(
            "UPSERT project_group:test_group SET name='test_group', "
            "members=[{project: 'backend', path: '/tmp/backend'}, "
            "{project: 'frontend', path: '/tmp/frontend'}], "
            "detect={http: true, lib: true}"
        )
        result = sync_group(db, "test_group")
        assert "links_created" in result


# ═══════════════════════════════════════════════════════════════
# Task 9: MCP Tools
# ═══════════════════════════════════════════════════════════════


class TestMCPContractTools:
    def test_code_contracts_query(self, db):
        from indexer.contract_extractor import detect_contracts
        from indexer.mcp_queries import code_contracts

        detect_contracts(db, "backend")
        result = code_contracts(db, "backend")
        assert len(result) > 0
        assert "contract_id" in result[0]

    def test_code_contracts_filter_by_type(self, db):
        from indexer.mcp_queries import code_contracts

        result = code_contracts(db, "backend", contract_type="http")
        for r in result:
            assert r["contract_type"] == "http"

    def test_code_contracts_filter_by_role(self, db):
        from indexer.mcp_queries import code_contracts

        result = code_contracts(db, "backend", role="provider")
        for r in result:
            assert r["role"] == "provider"

    def test_code_group_impact(self, db):
        from indexer.mcp_queries import code_group_impact

        db.query(
            "UPSERT project_group:impact_group SET name='impact_group', "
            "members=[{project: 'backend', path: '/tmp/be'}, "
            "{project: 'frontend', path: '/tmp/fe'}], "
            "detect={http: true, lib: true}"
        )
        result = code_group_impact(
            db, "impact_group", file="lib/db.ts", function="connectDB"
        )
        assert "local" in result
        assert "cross_repo" in result


# ═══════════════════════════════════════════════════════════════
# Task 10: Enriched Existing Tools
# ═══════════════════════════════════════════════════════════════


class TestEnrichedTools:
    def test_detect_changes_has_cross_project(self, db):
        from indexer.mcp_queries import code_detect_changes

        result = code_detect_changes(db, "backend", "/nonexistent/path")
        assert "cross_project_impact" in result


# ═══════════════════════════════════════════════════════════════
# Task 11: Group Management
# ═══════════════════════════════════════════════════════════════


class TestGroupManagement:
    def test_create_group(self, db):
        from indexer.mcp_queries import create_group, list_groups

        members = [
            {"project": "backend", "path": "/tmp/be"},
            {"project": "frontend", "path": "/tmp/fe"},
        ]
        create_group(db, "test-ecosystem", members)
        groups = list_groups(db)
        assert any(g["name"] == "test-ecosystem" for g in groups)

    def test_list_groups(self, db):
        from indexer.mcp_queries import list_groups

        groups = list_groups(db)
        assert isinstance(groups, list)

    def test_group_status(self, db):
        from indexer.mcp_queries import create_group, group_status

        members = [{"project": "backend", "path": "/tmp/be"}]
        create_group(db, "status-test", members)
        status = group_status(db, "status-test")
        assert "name" in status
        assert "members" in status


# ═══════════════════════════════════════════════════════════════
# Pattern False-Positive Prevention
# ═══════════════════════════════════════════════════════════════


class TestPatternFalsePositives:
    """Verify tightened topic/gRPC patterns reject common false positives."""

    def test_topic_rejects_bare_send(self):
        """Internal send() calls should NOT match topic producer."""
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["topic"]
        result = match_pattern(
            patterns,
            file="lib/utils.ts",
            name="pad10",
            node_type="function",
            callees=["send"],
        )
        assert result is None

    def test_topic_rejects_describe_callback(self):
        """Test files should be excluded by file_exclude_re."""
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["topic"]
        result = match_pattern(
            patterns,
            file="test/helper.spec.ts",
            name="describe",
            node_type="function",
            callees=["emit"],
        )
        assert result is None

    def test_topic_rejects_httpPost(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["topic"]
        result = match_pattern(
            patterns,
            file="lib/http.ts",
            name="httpPost",
            node_type="function",
            callees=["send"],
        )
        assert result is None

    def test_topic_rejects_write(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["topic"]
        result = match_pattern(
            patterns,
            file="lib/io.ts",
            name="write",
            node_type="function",
            callees=["push"],
        )
        assert result is None

    def test_topic_matches_kafka_publish(self):
        """Real Kafka publish call SHOULD match."""
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["topic"]
        result = match_pattern(
            patterns,
            file="services/events.ts",
            name="publishUserCreated",
            node_type="function",
            callees=["kafka.publish"],
        )
        assert result is not None
        assert result["role"] == "provider"

    def test_topic_matches_redis_publish(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["topic"]
        result = match_pattern(
            patterns,
            file="services/cache.py",
            name="notify_change",
            node_type="function",
            callees=["redis.publish"],
        )
        assert result is not None
        assert result["role"] == "provider"

    def test_topic_consumer_matches_kafka_subscribe(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["topic"]
        result = match_pattern(
            patterns,
            file="workers/consumer.ts",
            name="processEvents",
            node_type="function",
            callees=["kafka.subscribe"],
        )
        assert result is not None
        assert result["role"] == "consumer"

    def test_topic_consumer_rejects_bare_handle(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["topic"]
        result = match_pattern(
            patterns,
            file="lib/init.ts",
            name="handleInit",
            node_type="function",
            callees=["handle"],
        )
        assert result is None

    def test_topic_consumer_rejects_bare_on(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["topic"]
        result = match_pattern(
            patterns,
            file="lib/events.ts",
            name="onClick",
            node_type="function",
            callees=["on"],
        )
        assert result is None

    def test_grpc_rejects_handleInit(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["grpc"]
        result = match_pattern(
            patterns,
            file="lib/init.ts",
            name="handleInit",
            node_type="function",
            callees=["handleInit"],
        )
        assert result is None

    def test_grpc_rejects_handleUninstall(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["grpc"]
        result = match_pattern(
            patterns,
            file="lib/lifecycle.ts",
            name="handleUninstall",
            node_type="function",
            callees=["handleUninstall"],
        )
        assert result is None

    def test_grpc_rejects_HttpClient(self):
        """HttpClient is HTTP, not gRPC."""
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["grpc"]
        result = match_pattern(
            patterns,
            file="lib/api.ts",
            name="fetchData",
            node_type="function",
            callees=["HttpClient"],
        )
        assert result is None

    def test_grpc_matches_real_service_stub(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["grpc"]
        result = match_pattern(
            patterns,
            file="services/user.ts",
            name="UserServiceClient",
            node_type="function",
            callees=["UserServiceStub"],
        )
        assert result is not None
        assert result["role"] == "consumer"

    def test_grpc_matches_grpc_client(self):
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["grpc"]
        result = match_pattern(
            patterns,
            file="services/rpc.py",
            name="grpc_channel",
            node_type="function",
            callees=["grpc.insecure_channel"],
        )
        assert result is not None
        assert result["role"] == "consumer"

    def test_solidity_function_matches_as_grpc_provider(self):
        """Solidity public functions should register as on-chain API providers."""
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["grpc"]
        result = match_pattern(
            patterns,
            file="contracts/TRSDistributor.sol",
            name="claim",
            node_type="function",
        )
        assert result is not None
        assert result["role"] == "provider"
        assert result["pattern_name"] == "solidity_onchain"

    def test_grpc_matches_viem_onchain_consumer(self):
        """viem createPublicClient calls are on-chain contract consumers."""
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["grpc"]
        result = match_pattern(
            patterns,
            file="lib/pool-balance.ts",
            name="getClient",
            node_type="function",
            callees=["createPublicClient"],
        )
        assert result is not None
        assert result["role"] == "consumer"

    def test_grpc_matches_ethers_contract_consumer(self):
        """ethers getContractFactory calls are on-chain contract consumers."""
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["grpc"]
        result = match_pattern(
            patterns,
            file="scripts/deploy.ts",
            name="deployContract",
            node_type="function",
            callees=["ethers.getContractFactory"],
        )
        assert result is not None
        assert result["role"] == "consumer"

    def test_existing_http_patterns_unchanged(self):
        """Verify HTTP patterns still work correctly."""
        from indexer.contract_patterns import load_contract_patterns, match_pattern

        patterns = load_contract_patterns()["http"]
        # Next.js app router
        result = match_pattern(
            patterns, file="app/api/users/route.ts", name="GET", node_type="function"
        )
        assert result is not None
        assert result["role"] == "provider"
        # HTTP consumer
        result = match_pattern(
            patterns,
            file="lib/api.ts",
            name="fetchUsers",
            node_type="function",
            callees=["fetch"],
        )
        assert result is not None
        assert result["role"] == "consumer"
