"""Busca semântica local de descrições; não identifica pessoas nem compara imagens."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Query, Request

from .assistant import require

MODEL = "qwen3-embedding:0.6b"
FORMAT_VERSION = "notification-text-v1"
CORPUS_LIMIT = 250
BATCH_SIZE = 32


def document_text(item: dict[str, Any]) -> str:
    payload = item.get("payload")
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    source = data.get("source")
    source = source if isinstance(source, dict) else {}
    return "\n".join(value for value in (
        item.get("title"), item.get("description"), data.get("camera_name"),
        data.get("area_label"), source.get("source_name"),
    ) if isinstance(value, str) and value)


def validated_vectors(raw: Any, count: int) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) != count:
        raise ValueError("embedding_count_mismatch")
    vectors = []
    dimensions = None
    for vector in raw:
        if not isinstance(vector, list) or not vector or len(vector) > 16384:
            raise ValueError("invalid_embedding_dimensions")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise ValueError("invalid_embedding_value")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("invalid_embedding_norm")
        if dimensions is not None and len(vector) != dimensions:
            raise ValueError("embedding_dimensions_mismatch")
        dimensions = len(vector)
        vectors.append([value / norm for value in vector])
    return vectors


class TextHistorySearch:
    def __init__(self, notifications: Any, services: Any) -> None:
        self.notifications = notifications
        self.services = services
        # ponytail: uma indexação por processo; particionar somente se a concorrência medida exigir.
        self.lock = asyncio.Lock()

    def cache(self, space: str, documents: dict[str, tuple[str, str]], writes: dict[str, list[float]] | None = None) -> dict[str, list[float]]:
        with sqlite3.connect(self.notifications.store.path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS ai_notification_embedding (
                    notification_id TEXT NOT NULL REFERENCES notification(id) ON DELETE CASCADE,
                    space TEXT NOT NULL, content_hash TEXT NOT NULL, vector_json TEXT NOT NULL,
                    PRIMARY KEY(notification_id, space)
                );
                CREATE TRIGGER IF NOT EXISTS ai_notification_embedding_delete
                AFTER DELETE ON notification BEGIN
                    DELETE FROM ai_notification_embedding WHERE notification_id = OLD.id;
                END;
                CREATE TRIGGER IF NOT EXISTS ai_notification_embedding_update
                AFTER UPDATE ON notification BEGIN
                    DELETE FROM ai_notification_embedding WHERE notification_id = OLD.id;
                END;
            """)
            for identifier, vector in (writes or {}).items():
                content_hash = documents[identifier][0]
                # A ocorrência pode desaparecer enquanto o modelo processa o lote.
                connection.execute("""INSERT INTO ai_notification_embedding
                    SELECT id, ?, ?, ? FROM notification WHERE id = ?
                    ON CONFLICT(notification_id, space) DO UPDATE SET
                    content_hash=excluded.content_hash, vector_json=excluded.vector_json""",
                    (space, content_hash, json.dumps(vector), identifier))
            rows = connection.execute("""SELECT e.notification_id, e.content_hash, e.vector_json
                FROM ai_notification_embedding e JOIN notification n ON n.id=e.notification_id
                WHERE e.space=?""", (space,)).fetchall()
        return {identifier: json.loads(vector) for identifier, digest, vector in rows
                if identifier in documents and documents[identifier][0] == digest}

    async def search(self, *, query: str, limit: int = 10, priorities: list[str] | None = None, types: list[str] | None = None) -> dict[str, Any]:
        query = query.strip()
        if not query or len(query) > 1000 or not 1 <= limit <= 50:
            raise ValueError("invalid_history_query")
        async with self.lock:
            try:
                return await self._semantic(query, limit, priorities, types)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, sqlite3.Error):
                items, _ = await self.notifications.list(query=query, limit=limit, priorities=priorities, types=types)
                return {"notifications": items, "search": {"mode": "literal", "status": "degraded",
                    "reason": "local_text_embeddings_unavailable", "visual_similarity": False}}

    async def _semantic(self, query: str, limit: int, priorities: list[str] | None = None, types: list[str] | None = None) -> dict[str, Any]:
        settings = await self.services.call("ai.settings.get")
        provider = next((provider for provider in settings.providers
                         if provider.enabled and provider.kind == "ollama" and provider.local), None)
        if provider is None:
            raise ValueError("local_provider_unavailable")
        endpoint = urlsplit(provider.host)
        if endpoint.scheme not in {"http", "https"} or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"} or endpoint.username or endpoint.password:
            raise ValueError("loopback_provider_required")
        items, _ = await self.notifications.list(limit=CORPUS_LIMIT, priorities=priorities, types=types)
        documents = {item["id"]: (hashlib.sha256(document_text(item).encode()).hexdigest(), document_text(item)) for item in items}
        async with httpx.AsyncClient(base_url=provider.host.rstrip("/"), timeout=60, trust_env=False) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
            model = next((model for model in response.json()["models"] if model.get("name") == MODEL), None)
            digest = model.get("digest") if isinstance(model, dict) else None
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("model_digest_unavailable")
            response = await client.post("/api/embed", json={"model": MODEL, "input": [query], "truncate": False})
            response.raise_for_status()
            query_vector = validated_vectors(response.json().get("embeddings"), 1)[0]
            space = f"{MODEL}:{digest}:{len(query_vector)}:{FORMAT_VERSION}"
            cached = await asyncio.to_thread(self.cache, space, documents)
            pending = [identifier for identifier in documents if identifier not in cached]
            batch = pending[:BATCH_SIZE]
            if batch:
                response = await client.post("/api/embed", json={"model": MODEL,
                    "input": [documents[identifier][1] for identifier in batch], "truncate": False})
                response.raise_for_status()
                vectors = validated_vectors(response.json().get("embeddings"), len(batch))
                if any(len(vector) != len(query_vector) for vector in vectors):
                    raise ValueError("embedding_dimensions_mismatch")
            # Recusar mudança de modelo entre a consulta e os documentos, inclusive com cache.
            response = await client.get("/api/tags")
            response.raise_for_status()
            if not any(model.get("name") == MODEL and model.get("digest") == digest for model in response.json()["models"]):
                raise ValueError("model_changed_during_indexing")
            if batch:
                cached = await asyncio.to_thread(self.cache, space, documents, dict(zip(batch, vectors)))
        # Releitura garante que alteração, prioridade silenciosa ou exclusão não exponham evidência antiga.
        current, _ = await self.notifications.list(limit=CORPUS_LIMIT, priorities=priorities, types=types)
        ranked = []
        for item in current:
            identifier = item["id"]
            if identifier not in cached or identifier not in documents:
                continue
            if hashlib.sha256(document_text(item).encode()).hexdigest() != documents[identifier][0]:
                continue
            vector = validated_vectors([cached[identifier]], 1)[0]
            if len(vector) != len(query_vector):
                continue
            score = sum(left * right for left, right in zip(query_vector, vector))
            ranked.append({**item, "similarity": score})
        ranked.sort(key=lambda item: item["similarity"], reverse=True)
        return {"notifications": ranked[:limit], "search": {"mode": "semantic_text",
            "status": "partial" if len(ranked) < len(current) else "ready", "space": space,
            "indexed": len(ranked), "pending": len(current) - len(ranked),
            "scope": "recent_notifications", "scope_limit": CORPUS_LIMIT, "visual_similarity": False}}


def install_text_history_routes(app: FastAPI, services: Any) -> None:
    search = TextHistorySearch(app.state.notifications, services)
    services.register("ai.search_notifications", search.search)

    @app.get("/api/ai/history/search")
    async def history(request: Request, query: str = Query(min_length=1, max_length=1000, pattern=r"\S"), limit: int = Query(default=10, ge=1, le=50), priority: list[str] | None = Query(default=None), notification_type: list[str] | None = Query(default=None, alias="type")) -> dict[str, Any]:
        require(request, "core:notifications:read")
        return await search.search(query=query, limit=limit, priorities=priority, types=notification_type)
