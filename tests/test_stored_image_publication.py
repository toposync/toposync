from toposync.runtime.pipelines.images import add_stored_image_entry
from toposync.runtime.pipelines.runtime import Artifact, Packet


def test_stored_image_keeps_media_position_separate_from_civil_publication():
    artifact = Artifact(name="main", metadata={"published_at_ms": 1700000000000})
    packet = add_stored_image_entry(Packet.create(stream_id="replay"), key="main", artifact=artifact,
                                   rel_path="image.webp", stored_ts_ms=101000)
    entry = packet.payload["stored_images"]["main"][0]
    assert entry["stored_ts_ms"] == 101000
    assert entry["published_at_ms"] == 1700000000000
    legacy = add_stored_image_entry(Packet.create(stream_id="legacy"), key="main", artifact=Artifact(name="main"),
                                   rel_path="legacy.webp", stored_ts_ms=101000)
    assert "published_at_ms" not in legacy.payload["stored_images"]["main"][0]
