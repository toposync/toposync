"""Image retrieval over existing, visible notification evidence only."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from .text_embeddings import CORPUS_LIMIT, TextHistorySearch, document_text, validated_vectors

MAX_IMAGE_BYTES = 12 * 1024 * 1024
SCAN_BYTES = 64 * 1024 * 1024
BATCH_SIZE = 8


def evidence_path(root: Path, item: dict[str, Any]) -> Path:
    url = item.get("imageUrl")
    if not isinstance(url, str) or not url.startswith("/files/"):
        raise ValueError("notification_image_unavailable")
    relative = url[len("/files/"):].replace("\\", "/")
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError("invalid_notification_image_path")
    root = root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("notification_image_unavailable")
    return path


def read_evidence(root: Path, item: dict[str, Any]) -> bytes:
    path = evidence_path(root, item)
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("notification_image_bytes_exceed_budget")
    return data


def evidence_hash(item: dict[str, Any], data: bytes) -> str:
    digest = hashlib.sha256(data)
    digest.update((str(item.get("imageUrl")) + "\0" + document_text(item)).encode())
    return digest.hexdigest()


class VisualHistorySearch(TextHistorySearch):
    fallback_reason = "local_visual_embeddings_unavailable"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fingerprints: dict[str, tuple[tuple, str]] = {}

    def documents(self, items: list[dict[str, Any]]) -> tuple[dict, int, set[str]]:
        documents, used, unavailable, deferred = {}, 0, 0, set()
        identifiers = {item["id"] for item in items}
        self.fingerprints = {key: value for key, value in self.fingerprints.items()
                             if key in identifiers}
        for item in items:
            if not item.get("imageUrl"):
                self.fingerprints.pop(item["id"], None)
                continue
            try:
                path = evidence_path(self.config_store.paths.files_dir, item)
                stat = path.stat()
                fingerprint = (str(path), stat.st_dev, stat.st_ino, stat.st_size,
                               stat.st_mtime_ns, stat.st_ctime_ns, item["imageUrl"], document_text(item))
                previous = self.fingerprints.get(item["id"])
                # Reuse only while local file identity, timestamps and notification text agree.
                # Keep digests, never image bytes; the current corpus bounds this cache.
                if previous is not None and previous[0] == fingerprint:
                    documents[item["id"]] = (previous[1], item["imageUrl"])
                    continue
                self.fingerprints.pop(item["id"], None)
                if not 0 < stat.st_size <= MAX_IMAGE_BYTES:
                    raise ValueError("notification_image_bytes_exceed_budget")
                if used + stat.st_size > SCAN_BYTES:
                    deferred.add(item["id"])
                    continue
                data = read_evidence(self.config_store.paths.files_dir, item)
                used += len(data)
                after = path.stat()
                if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != fingerprint[1:6] or evidence_path(self.config_store.paths.files_dir, item) != path:
                    deferred.add(item["id"])
                    continue
                digest = evidence_hash(item, data)
                self.fingerprints[item["id"]] = (fingerprint, digest)
                documents[item["id"]] = (digest, item["imageUrl"])
            except (OSError, ValueError):
                self.fingerprints.pop(item["id"], None)
                unavailable += 1
        return documents, unavailable, deferred

    def discard(self, space: str, identifiers: set[str]) -> None:
        with sqlite3.connect(self.notifications.store.path) as connection:
            connection.executemany("DELETE FROM ai_notification_embedding WHERE space=? AND notification_id=?",
                                   [(space, identifier) for identifier in identifiers])

    async def _semantic(self, query, limit, priorities=None, types=None, created_after=None, created_before=None, data_filter=None, notification_ids=None):
        # Optional extension failures must keep literal history usable, including unsupported runtimes.
        try:
            return await self._visual(query, limit, priorities, types, created_after, created_before, data_filter, notification_ids)
        except Exception as exc:
            raise ValueError("local_visual_embeddings_unavailable") from exc

    async def _visual(self, query, limit, priorities, types, created_after, created_before, data_filter=None, notification_ids=None):
        filters = dict(priorities=priorities, types=types, created_after=created_after, created_before=created_before, data_filter=data_filter, notification_ids=notification_ids)
        items, _ = await self.notifications.list(limit=CORPUS_LIMIT, **filters)
        space = await self.services.call("vision.history_embedding_space")
        if not isinstance(space, str) or not space.startswith("clip-") or len(space) > 256:
            raise ValueError("invalid_visual_embedding_space")
        documents, _, deferred = await asyncio.to_thread(self.documents, items)
        cached = await asyncio.to_thread(self.cache, space, documents)
        await asyncio.to_thread(self.discard, space, {item["id"] for item in items} - documents.keys() - deferred)
        batch = [item for item in items if item["id"] in documents and item["id"] not in cached][:BATCH_SIZE]
        images = {}
        for item in batch:
            data = await asyncio.to_thread(read_evidence, self.config_store.paths.files_dir, item)
            if evidence_hash(item, data) == documents[item["id"]][0]:
                images[item["id"]] = data
        embedded = await self.services.call("vision.history_embeddings", query=query, images=images)
        if embedded["space"] != space:
            raise ValueError("visual_embedding_space_changed")
        query_vector = validated_vectors([embedded["query"]], 1)[0]
        vectors = embedded["images"]
        if len(query_vector) != 512 or not isinstance(vectors, dict) or not set(vectors).issubset(images):
            raise ValueError("invalid_visual_embedding_result")
        for vector in vectors.values():
            if len(validated_vectors([vector], 1)[0]) != 512:
                raise ValueError("visual_embedding_dimensions_mismatch")
        rejected = embedded.get("unavailable", [])
        if not isinstance(rejected, list) or not set(rejected).issubset(images) or set(rejected).intersection(vectors):
            raise ValueError("invalid_visual_embedding_result")
        # An empty vector is a rejection for these exact bytes, not an embedding.
        # This lets later batches advance; changed bytes automatically invalidate it.
        cached = await asyncio.to_thread(self.cache, space, documents, {**vectors, **{key: [] for key in rejected}})
        # Re-read metadata and bytes after inference. Removed or replaced evidence cannot survive through cache.
        current, cursor = await self.notifications.list(limit=CORPUS_LIMIT, **filters)
        current_documents, unavailable, deferred = await asyncio.to_thread(self.documents, current)
        valid = {identifier for identifier in current_documents
                 if identifier in documents and current_documents[identifier] == documents[identifier]}
        removed = {item["id"] for item in items} - {item["id"] for item in current}
        await asyncio.to_thread(self.discard, space, (documents.keys() - valid) | removed)
        rejected = {identifier for identifier in valid if cached.get(identifier) == []}
        ranked = []
        for item in current:
            if item["id"] not in valid or item["id"] not in cached or item["id"] in rejected:
                continue
            vector = validated_vectors([cached[item["id"]]], 1)[0]
            if len(vector) == len(query_vector):
                ranked.append({**item, "similarity": sum(a * b for a, b in zip(query_vector, vector))})
        ranked.sort(key=lambda item: item["similarity"], reverse=True)
        older = []
        if len(current) == CORPUS_LIMIT and cursor is not None:
            older, _ = await self.notifications.list(before=cursor, limit=1, **filters)
        pending = len(current_documents) - len(ranked) - len(rejected) + len(deferred)
        unavailable += len(rejected)
        return {"notifications": ranked[:limit], "search": {
            "mode": "semantic_visual", "status": "partial" if pending or unavailable else "ready", "space": space,
            "indexed": len(ranked), "pending": pending, "unavailable_images": unavailable,
            "without_images": sum(not item.get("imageUrl") for item in current),
            "returned_count": min(len(ranked), limit), "results_truncated": len(ranked) > limit,
            "corpus_truncated": bool(older), "scope": "recent_notifications", "scope_limit": CORPUS_LIMIT,
            "visual_similarity": True, "identity_confirmation": False,
            "time_basis": "notification_created_at", "created_after": created_after, "created_before": created_before,
        }}
