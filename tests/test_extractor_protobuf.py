"""Tests for Protobuf extractor (Tier 1 regex-based)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.extractors import Edge, Node
from indexer.extractors.protobuf import extract_protobuf


@pytest.fixture
def project(tmp_path):
    """Create a minimal proto project structure."""
    proto_dir = tmp_path / "proto" / "torus" / "governance" / "v1"
    proto_dir.mkdir(parents=True)

    (proto_dir / "governance.proto").write_text(
        'syntax = "proto3";\n'
        "package torus.governance.v1;\n\n"
        'option go_package = "github.com/torus-chain/torus/x/governance/types";\n\n'
        'import "cosmos/msg/v1/msg.proto";\n'
        'import "torus/governance/v1/tx.proto";\n\n'
        "message Proposal {\n"
        "  string title = 1;\n"
        "  string description = 2;\n"
        "  ProposalType proposal_type = 3;\n"
        "  VoteOption status = 4;\n"
        "}\n\n"
        "message ProposalType {\n"
        "  string name = 1;\n"
        "}\n\n"
        "enum VoteOption {\n"
        "  VOTE_OPTION_UNSPECIFIED = 0;\n"
        "  VOTE_OPTION_YES = 1;\n"
        "  VOTE_OPTION_NO = 2;\n"
        "}\n"
    )

    (proto_dir / "tx.proto").write_text(
        'syntax = "proto3";\n'
        "package torus.governance.v1;\n\n"
        'option go_package = "github.com/torus-chain/torus/x/governance/types";\n\n'
        'import "torus/governance/v1/governance.proto";\n\n'
        "service Msg {\n"
        "  rpc SubmitProposal(MsgSubmitProposal) returns (MsgSubmitProposalResponse);\n"
        "  rpc Vote(MsgVote) returns (MsgVoteResponse);\n"
        "}\n\n"
        "message MsgSubmitProposal {\n"
        "  string proposer = 1;\n"
        "  ProposalType proposal_type = 2;\n"
        "}\n\n"
        "message MsgSubmitProposalResponse {\n"
        "  uint64 proposal_id = 1;\n"
        "}\n\n"
        "message MsgVote {\n"
        "  string voter = 1;\n"
        "}\n\n"
        "message MsgVoteResponse {}\n"
    )

    return tmp_path


class TestProtobufImports:
    def test_extracts_imports(self, project):
        proto_file = project / "proto" / "torus" / "governance" / "v1" / "tx.proto"
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        targets = {e.target for e in import_edges}
        assert "torus/governance/v1/governance.proto" in targets

    def test_external_import_kept_as_is(self, project):
        proto_file = (
            project / "proto" / "torus" / "governance" / "v1" / "governance.proto"
        )
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        targets = {e.target for e in import_edges}
        assert "cosmos/msg/v1/msg.proto" in targets

    def test_import_line_numbers(self, project):
        proto_file = project / "proto" / "torus" / "governance" / "v1" / "tx.proto"
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        import_edges = [e for e in edges if e.relation == "imports"]
        assert all(e.source_line > 0 for e in import_edges)


class TestProtobufServices:
    def test_service_node(self, project):
        proto_file = project / "proto" / "torus" / "governance" / "v1" / "tx.proto"
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        class_nodes = [n for n in nodes if n.type == "class"]
        names = {n.name for n in class_nodes}
        assert "Msg" in names

    def test_rpc_methods(self, project):
        proto_file = project / "proto" / "torus" / "governance" / "v1" / "tx.proto"
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        func_nodes = [n for n in nodes if n.type == "function"]
        names = {n.name for n in func_nodes}
        assert "SubmitProposal" in names
        assert "Vote" in names

    def test_rpc_calls_request_message(self, project):
        proto_file = project / "proto" / "torus" / "governance" / "v1" / "tx.proto"
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        call_edges = [e for e in edges if e.relation == "calls"]
        targets = {e.target for e in call_edges}
        assert "MsgSubmitProposal" in targets
        assert "MsgVote" in targets

    def test_rpc_calls_response_message(self, project):
        proto_file = project / "proto" / "torus" / "governance" / "v1" / "tx.proto"
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        call_edges = [e for e in edges if e.relation == "calls"]
        targets = {e.target for e in call_edges}
        assert "MsgSubmitProposalResponse" in targets
        assert "MsgVoteResponse" in targets


class TestProtobufMessages:
    def test_message_nodes(self, project):
        proto_file = (
            project / "proto" / "torus" / "governance" / "v1" / "governance.proto"
        )
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        class_nodes = [n for n in nodes if n.type == "class"]
        names = {n.name for n in class_nodes}
        assert "Proposal" in names
        assert "ProposalType" in names

    def test_message_field_type_references(self, project):
        proto_file = (
            project / "proto" / "torus" / "governance" / "v1" / "governance.proto"
        )
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        reads_edges = [e for e in edges if e.relation == "reads"]
        targets = {e.target for e in reads_edges}
        assert "ProposalType" in targets
        assert "VoteOption" in targets

    def test_enum_node(self, project):
        proto_file = (
            project / "proto" / "torus" / "governance" / "v1" / "governance.proto"
        )
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        class_nodes = [n for n in nodes if n.type == "class"]
        names = {n.name for n in class_nodes}
        assert "VoteOption" in names


class TestProtobufGoPackage:
    def test_go_package_metadata_edge(self, project):
        proto_file = project / "proto" / "torus" / "governance" / "v1" / "tx.proto"
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        impl_edges = [e for e in edges if e.relation == "implements"]
        targets = {e.target for e in impl_edges}
        assert "github.com/torus-chain/torus/x/governance/types" in targets


class TestProtobufFileNode:
    def test_file_node_present(self, project):
        proto_file = project / "proto" / "torus" / "governance" / "v1" / "tx.proto"
        nodes, edges = extract_protobuf(str(proto_file), str(project))
        file_nodes = [n for n in nodes if n.type == "file"]
        assert len(file_nodes) == 1

    def test_nonexistent_file(self, project):
        nodes, edges = extract_protobuf(str(project / "nope.proto"), str(project))
        assert nodes == []
        assert edges == []


class TestProtobufEdgeCases:
    def test_empty_file(self, tmp_path):
        (tmp_path / "empty.proto").write_text("")
        nodes, edges = extract_protobuf(str(tmp_path / "empty.proto"), str(tmp_path))
        assert edges == []

    def test_comments_not_matched(self, tmp_path):
        (tmp_path / "commented.proto").write_text(
            'syntax = "proto3";\n'
            "package test;\n\n"
            '// import "should/not/match.proto";\n'
            "// service Fake {}\n"
            "message Real {\n  string name = 1;\n}\n"
        )
        nodes, edges = extract_protobuf(
            str(tmp_path / "commented.proto"), str(tmp_path)
        )
        import_edges = [e for e in edges if e.relation == "imports"]
        assert not any("should/not/match" in e.target for e in import_edges)
        class_nodes = [n for n in nodes if n.type == "class"]
        names = {n.name for n in class_nodes}
        assert "Real" in names
        assert "Fake" not in names
