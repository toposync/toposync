"""Match public occurrence references to an already-authorized private selection."""
from __future__ import annotations

import json


def matching_notification_ids(store, scope: dict) -> list[str]:
    if not scope["occurrences"] and not scope.get("activities"):
        return []

    def read(connection):
        # ponytail: scan reference metadata; index these references after measured demand.
        rows = connection.execute("""
            WITH allowed AS (
                SELECT json_extract(value,'$.occurrence_id') AS occurrence_id,
                       json_extract(value,'$.camera_id') AS camera_id FROM json_each(?)
            ), visible AS (
                SELECT id, CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END AS payload
                FROM notification WHERE priority_bucket IN ('low','medium','high')
            )
            SELECT DISTINCT n.id FROM visible n JOIN allowed a
            ON a.camera_id = coalesce(json_extract(n.payload,'$.data.camera_id'),json_extract(n.payload,'$.camera_id'))
            AND (
                a.occurrence_id = coalesce(json_extract(n.payload,'$.data.recognition.occurrence_id'),json_extract(n.payload,'$.recognition.occurrence_id'))
                OR (
                    coalesce(json_extract(n.payload,'$.data.subject.type'),json_extract(n.payload,'$.subject.type')) = 'group_event'
                    AND EXISTS (
                        SELECT 1 FROM json_each(CASE WHEN json_type(n.payload,'$.data.subject.members')='array' THEN json_extract(n.payload,'$.data.subject.members') WHEN json_type(n.payload,'$.subject.members')='array' THEN json_extract(n.payload,'$.subject.members') ELSE '[]' END) m
                        WHERE a.occurrence_id = json_extract(CASE WHEN m.type='object' THEN m.value ELSE '{}' END,'$.recognition_occurrence_id')
                        AND json_extract(CASE WHEN m.type='object' THEN m.value ELSE '{}' END,'$.category') = ?
                    )
                )
            ) ORDER BY n.id
        """, (json.dumps(scope["occurrences"]), scope["species"])).fetchall()
        rows += connection.execute("""
            SELECT n.id FROM notification n JOIN occurrence o ON n.dedupe_key='occurrence:' || o.id
            JOIN json_each(?) a ON o.id=json_extract(a.value,'$.id') AND o.revision=json_extract(a.value,'$.revision')
            WHERE n.priority_bucket IN ('low','medium','high') AND o.status NOT IN ('deleted','expired')
        """, (json.dumps(scope.get('activities', [])),)).fetchall()
        return rows
    return [row["id"] for row in store._run_read(read)]
