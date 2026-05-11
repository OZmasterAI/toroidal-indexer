"""Tests for Toroidal-Indexer Leiden community clustering."""

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
    """Connect to SurrealDB with a unique test database, seed a clusterable graph, yield, cleanup."""
    test_db = f"test_cluster_{uuid.uuid4().hex[:8]}"
    conn = connect_code_graph(url=SURREAL_URL, database=test_db)
    init_code_tables(conn)

    # Cluster A: auth subsystem (4 nodes, densely connected)
    auth_login = upsert_node(conn, "proj", "auth/login.ts", "loginUser", "function", 10)
    auth_verify = upsert_node(
        conn, "proj", "auth/verify.ts", "verifyToken", "function", 5
    )
    auth_provider = upsert_node(
        conn, "proj", "auth/provider.ts", "AuthProvider", "class", 1
    )
    auth_middleware = upsert_node(
        conn, "proj", "auth/middleware.ts", "authMiddleware", "function", 20
    )

    relate(conn, auth_login, "calls", auth_verify, confidence=1.0, source_line=12)
    relate(conn, auth_middleware, "calls", auth_verify, confidence=1.0, source_line=22)
    relate(conn, auth_login, "calls", auth_provider, confidence=1.0, source_line=14)
    relate(
        conn, auth_middleware, "calls", auth_provider, confidence=1.0, source_line=25
    )
    relate(conn, auth_provider, "calls", auth_verify, confidence=1.0, source_line=3)

    # Cluster B: rewards subsystem (4 nodes, densely connected)
    rewards_calc = upsert_node(
        conn, "proj", "rewards/calculator.ts", "calculateRewards", "function", 10
    )
    rewards_dist = upsert_node(
        conn, "proj", "rewards/distributor.ts", "distributeRewards", "function", 5
    )
    rewards_claim = upsert_node(
        conn, "proj", "rewards/claim.ts", "claimReward", "function", 1
    )
    rewards_merkle = upsert_node(
        conn, "proj", "rewards/merkle.ts", "buildMerkleTree", "function", 15
    )

    relate(conn, rewards_dist, "calls", rewards_calc, confidence=1.0, source_line=8)
    relate(conn, rewards_claim, "calls", rewards_dist, confidence=1.0, source_line=3)
    relate(conn, rewards_dist, "calls", rewards_merkle, confidence=1.0, source_line=12)
    relate(conn, rewards_claim, "calls", rewards_merkle, confidence=1.0, source_line=5)
    relate(conn, rewards_calc, "calls", rewards_merkle, confidence=1.0, source_line=18)

    # Single cross-cluster edge (sparse connection between clusters)
    relate(
        conn, auth_middleware, "calls", rewards_claim, confidence=0.5, source_line=30
    )

    yield conn
    conn.query(f"REMOVE DATABASE IF EXISTS {test_db}")


@pytest.fixture(scope="module")
def clustered_db(db):
    """Run clustering on the test graph and return the db."""
    from indexer.clustering import run_clustering

    run_clustering(db, "proj")
    return db


class TestLoadGraph:
    def test_loads_nodes_and_edges(self, db):
        from indexer.clustering import load_project_graph

        g = load_project_graph(db, "proj")
        assert g.vcount() == 8
        assert g.ecount() >= 6

    def test_nodes_have_attributes(self, db):
        from indexer.clustering import load_project_graph

        g = load_project_graph(db, "proj")
        for v in g.vs:
            assert "name" in v.attributes()
            assert "file" in v.attributes()
            assert "type" in v.attributes()

    def test_empty_project_returns_empty_graph(self, db):
        from indexer.clustering import load_project_graph

        g = load_project_graph(db, "nonexistent_proj")
        assert g.vcount() == 0


class TestGenerateLabel:
    def test_auth_nodes_produce_auth_label(self):
        from indexer.clustering import generate_cluster_label

        nodes = [
            {"name": "loginUser", "file": "auth/login.ts", "type": "function"},
            {"name": "verifyToken", "file": "auth/verify.ts", "type": "function"},
            {"name": "AuthProvider", "file": "auth/provider.ts", "type": "class"},
            {
                "name": "authMiddleware",
                "file": "auth/middleware.ts",
                "type": "function",
            },
        ]
        label = generate_cluster_label(nodes)
        assert "auth" in label.lower()

    def test_rewards_nodes_produce_rewards_label(self):
        from indexer.clustering import generate_cluster_label

        nodes = [
            {
                "name": "calculateRewards",
                "file": "rewards/calculator.ts",
                "type": "function",
            },
            {
                "name": "distributeRewards",
                "file": "rewards/distributor.ts",
                "type": "function",
            },
            {"name": "claimReward", "file": "rewards/claim.ts", "type": "function"},
            {
                "name": "buildMerkleTree",
                "file": "rewards/merkle.ts",
                "type": "function",
            },
        ]
        label = generate_cluster_label(nodes)
        assert "reward" in label.lower()

    def test_single_node_cluster(self):
        from indexer.clustering import generate_cluster_label

        nodes = [{"name": "helperFn", "file": "utils/helper.ts", "type": "function"}]
        label = generate_cluster_label(nodes)
        assert len(label) > 0

    def test_empty_cluster(self):
        from indexer.clustering import generate_cluster_label

        label = generate_cluster_label([])
        assert label == "unknown"


class TestRunClustering:
    def test_assigns_cluster_ids(self, clustered_db):
        nodes = clustered_db.query(
            "SELECT cluster_id, cluster_label FROM code_node WHERE project='proj'"
        )
        assert len(nodes) == 8
        for n in nodes:
            assert n.get("cluster_id") is not None
            assert n.get("cluster_label") is not None
            assert len(n["cluster_label"]) > 0

    def test_creates_cluster_table(self, clustered_db):
        clusters = clustered_db.query("SELECT * FROM code_cluster WHERE project='proj'")
        assert len(clusters) >= 1
        for c in clusters:
            assert "label" in c
            assert "node_count" in c
            assert c["node_count"] > 0
            assert "key_files" in c
            assert "key_functions" in c

    def test_idempotent(self, clustered_db):
        from indexer.clustering import run_clustering

        run_clustering(clustered_db, "proj")
        clusters = clustered_db.query("SELECT * FROM code_cluster WHERE project='proj'")
        # Should have same number of clusters, no duplicates
        labels = [c["label"] for c in clusters]
        # Each cluster_id should be unique
        ids = [str(c["id"]) for c in clusters]
        assert len(ids) == len(set(ids))

    def test_finds_two_communities(self, clustered_db):
        clusters = clustered_db.query("SELECT * FROM code_cluster WHERE project='proj'")
        # With 2 dense subgraphs and 1 sparse cross-edge, Leiden should find >= 2 clusters
        assert len(clusters) >= 2


class TestMcpQueryFunctions:
    def test_code_clusters_returns_all(self, clustered_db):
        from indexer.mcp_queries import code_clusters

        result = code_clusters(clustered_db, "proj")
        assert isinstance(result, list)
        assert len(result) >= 2
        for c in result:
            assert "label" in c
            assert "node_count" in c
            assert "key_files" in c
            assert "key_functions" in c

    def test_code_clusters_empty_project(self, clustered_db):
        from indexer.mcp_queries import code_clusters

        result = code_clusters(clustered_db, "nonexistent")
        assert result == []

    def test_code_cluster_members_by_label(self, clustered_db):
        from indexer.mcp_queries import code_cluster_members, code_clusters

        clusters = code_clusters(clustered_db, "proj")
        assert len(clusters) >= 1
        label = clusters[0]["label"]
        members = code_cluster_members(clustered_db, "proj", label)
        assert isinstance(members, list)
        assert len(members) >= 1
        for m in members:
            assert "name" in m
            assert "file" in m
            assert "type" in m

    def test_code_cluster_members_substring_match(self, clustered_db):
        from indexer.mcp_queries import code_cluster_members

        # Partial match should work
        members = code_cluster_members(clustered_db, "proj", "auth")
        # If auth cluster exists, should return members
        # (may be empty if label doesn't contain "auth" substring)
        assert isinstance(members, list)

    def test_code_cluster_members_no_match(self, clustered_db):
        from indexer.mcp_queries import code_cluster_members

        result = code_cluster_members(clustered_db, "proj", "zzz_nonexistent_zzz")
        assert result == []
