from __future__ import annotations

from navi.graph import GraphStore
from navi.memory.store import MemoryStore


def _add_memory(
    store: MemoryStore,
    content: str,
    *,
    memory_type: str = "preference",
    status: str = "active",
    metadata: dict | None = None,
):
    return store.add_item(
        memory_type,
        content,
        source="test",
        status=status,
        confidence=0.8,
        metadata=metadata,
        reason="unit test",
        provenance="tests/test_memory_semantic_graph.py",
    )


def test_memory_semantic_graph_sync_indexes_memory_dimensions_and_relations(tmp_path):
    store = MemoryStore(tmp_path)
    graph = GraphStore(tmp_path)
    old = _add_memory(store, "old preference")
    replacement = _add_memory(
        store,
        "new preference",
        metadata={"supersedes": [old.id]},
    )

    facts = store.sync_semantic_graph(graph_store=graph)

    replacement_node = graph.get_by_name("MemoryItem", replacement.id)
    old_node = graph.get_by_name("MemoryItem", old.id)
    type_node = graph.get_by_name("MemoryType", "preference")
    status_node = graph.get_by_name("MemoryStatus", "active")
    scope_node = graph.get_by_name("MemoryScope", "global")
    assert facts["semantic_graph"] == "memory"
    assert facts["synced_count"] == 2
    assert replacement_node is not None
    assert replacement_node.data["memory_type"] == "preference"
    assert replacement_node.data["placeholder"] is False
    assert old_node is not None
    assert type_node is not None
    assert status_node is not None
    assert scope_node is not None

    edges = graph.list_edges(source_id=replacement_node.id, limit=20)
    edge_targets = {(edge.relation, edge.target_id) for edge in edges}
    assert ("has_memory_type", type_node.id) in edge_targets
    assert ("has_memory_status", status_node.id) in edge_targets
    assert ("has_memory_scope", scope_node.id) in edge_targets
    assert ("supersedes", old_node.id) in edge_targets


def test_memory_semantic_graph_sync_replaces_stale_status_edges(tmp_path):
    store = MemoryStore(tmp_path)
    graph = GraphStore(tmp_path)
    item = _add_memory(store, "mutable status fact", memory_type="fact")
    store.sync_semantic_graph(graph_store=graph)
    item_node = graph.get_by_name("MemoryItem", item.id)
    assert item_node is not None
    active_node = graph.get_by_name("MemoryStatus", "active")
    assert active_node is not None
    assert graph.list_edges(
        source_id=item_node.id,
        target_id=active_node.id,
        relation="has_memory_status",
    )

    store.set_status(item.id, "archived")
    store.sync_semantic_graph(graph_store=graph)

    archived_node = graph.get_by_name("MemoryStatus", "archived")
    assert archived_node is not None
    status_edges = graph.list_edges(source_id=item_node.id, relation="has_memory_status")
    assert [(edge.target_id, edge.relation) for edge in status_edges] == [
        (archived_node.id, "has_memory_status")
    ]
