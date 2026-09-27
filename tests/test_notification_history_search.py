from pathlib import Path

from toposync.runtime.notifications.runtime import NotificationsRuntime


def test_history_text_metadata_retrieval_and_original_evidence(tmp_path: Path) -> None:
    """Consultas literais não afirmam compreensão semântica nem identidade confirmada."""
    runtime = NotificationsRuntime(data_dir=tmp_path)
    store = runtime.store
    cat, _ = store.upsert(type="pipelines.event", title="Gato observado", description="Bebedouro",
        image_path="evidence/cat.jpg", payload={"data": {"camera_name": "Pátio", "area_label": "Água"}}, dedupe_key="cat")
    person, _ = store.upsert(type="pipelines.event", title="Pessoa com pacote", description="Entrada",
        payload={"data": {"source": {"source_name": "Garagem"}}})
    store.upsert(type="pipelines.event", title="Gato", description="Caixa de areia",
        payload={"priority": "silent", "data": {"area_label": "Água"}})
    # Resultados esperados abrangem título, descrição e metadados, sem inferir sinônimos.
    queries = {"gato bebedouro": [cat.id], "PATIO agua": [cat.id], "pacote garagem": [person.id],
        "gato garagem": [], "%": [], "ontem": [], "identity_id": []}
    for query, expected in queries.items():
        items, _ = runtime.list_sync(query=query)
        assert [item["id"] for item in items] == expected
    items, _ = runtime.list_sync(query="gato bebedouro")
    assert items[0]["imageUrl"] == "/files/evidence/cat.jpg"
    assert items[0]["payload"]["data"]["camera_name"] == "Pátio"
    store.upsert(type="pipelines.event", title="Gato observado", description="Caixa de areia", dedupe_key="cat")
    assert runtime.list_sync(query="bebedouro")[0] == []
    # Não existe índice separado que possa reter ocorrências excluídas.
    store._conn.execute("DELETE FROM notification WHERE id = ?", (cat.id,))
    assert runtime.list_sync(query="patio")[0] == []


def test_history_search_ignores_nontext_metadata(tmp_path: Path) -> None:
    runtime = NotificationsRuntime(data_dir=tmp_path)
    runtime.store.upsert(type="pipelines.event", title="Objeto",
        payload={"data": {"camera_name": 998877, "area_label": {"nested": "injetado"},
                          "source": {"source_name": ["indevido"]}}})
    for query in ("998877", "nested", "injetado", "indevido"):
        assert runtime.list_sync(query=query)[0] == []
