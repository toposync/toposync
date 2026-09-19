from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import numpy as np
import pytest

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.images import (
    resolve_image_artifact_for_data,
    resolve_image_artifact_for_reference,
)
from toposync.runtime.pipelines.operators_core import DebugStdoutRuntime, StreamStateSnapshotRuntime
from toposync.runtime.pipelines.operators_distributed import _deserialize_packet, _serialize_packet
from toposync.runtime.pipelines.operators_sinks import StoreImagesRuntime
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet


def test_private_transport_preserves_bytes_and_rejects_unsafe_envelopes():
    artifact = Artifact(
        name="private", data=b"private-evidence", mime_type="application/octet-stream", private=True
    )
    packet = Packet.create(stream_id="stream", artifacts={"private": artifact})
    encoded = _serialize_packet(packet)
    assert _deserialize_packet(encoded).artifacts["private"] == artifact
    for patch in [
        {"privacy_version": 2},
        {"reference": "/remote/private.jpg"},
        {"encoding": "npy"},
        {"inline_b64": "!invalid"},
        {"inline_b64": "A" * 2_800_000},
    ]:
        modified = {
            **encoded,
            "artifacts": {"private": {**encoded["artifacts"]["private"], **patch}},
        }
        with pytest.raises(ValueError):
            _deserialize_packet(modified)
    with pytest.raises(ValueError):
        _serialize_packet(
            replace(
                packet, artifacts={"private": replace(artifact, reference="/remote/private.jpg")}
            )
        )


def test_private_image_never_enters_generic_image_resolvers():
    artifact = Artifact(name="main", data=np.ones((8, 8, 3)), reference="private.jpg", private=True)
    packet = Packet.create(stream_id="s", artifacts={"main": artifact})
    assert resolve_image_artifact_for_data(packet)[1] is None
    assert resolve_image_artifact_for_reference(packet)[1] is None


def test_private_artifacts_not_logged_saved_or_snapshotted(tmp_path, capsys):
    async def scenario():
        private = Artifact(
            name="secret-name",
            data=np.zeros((8, 8, 3), dtype=np.uint8),
            reference="secret-reference",
            metadata={"secret-metadata": "do-not-print"},
            private=True,
        )
        packet = Packet.create(
            stream_id="s", lifecycle=Lifecycle.OPEN, artifacts={"secret-name": private}
        )
        debug = DebugStdoutRuntime(
            {
                "enabled": True,
                "save_images": True,
                "print_artifacts": True,
                "output_dir": str(tmp_path),
            },
            PipelineRuntimeDependencies(),
        )
        await debug.process_packet(packet, SimpleNamespace(pipeline_name="test", node_id="debug"))
        emitted = []

        async def emit(value, **kwargs):
            emitted.append(value)
            return 1

        snapshot = StreamStateSnapshotRuntime({"interval_seconds": 1.0})
        await snapshot.process_packet(packet, SimpleNamespace(node_id="snapshot", emit=emit))
        assert emitted and emitted[0].artifacts == {}
        assert debug._resolve_snapshot_image(packet) is None

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert (
        "secret-name" not in output
        and "secret-reference" not in output
        and "secret-metadata" not in output
    )
    assert json.loads(output)["artifacts"] == {}
    assert not list(tmp_path.rglob("*.png"))


def test_private_image_store_preserves_flow_without_publishing(tmp_path):
    async def scenario():
        private = Artifact(name="main", data=np.zeros((8, 8, 3), dtype=np.uint8), private=True)
        packet = Packet.create(stream_id="s", artifacts={"main": private})
        runtime = StoreImagesRuntime({}, PipelineRuntimeDependencies(files_dir=tmp_path))
        result = await runtime.process_packet(
            packet, SimpleNamespace(pipeline_name="test", node_id="store")
        )
        assert result == [packet]
        assert not list(tmp_path.rglob("*.webp"))

    asyncio.run(scenario())


def test_private_budget_covers_all_artifacts_and_decoded_padding_boundary():
    import base64

    limit = 2 * 1024 * 1024
    first = Artifact(name="one", data=b"a" * (limit // 2 + 1), private=True)
    second = Artifact(name="two", data=b"b" * (limit // 2), private=True)
    with pytest.raises(ValueError, match="private packet exceeds"):
        _serialize_packet(Packet.create(stream_id="s", artifacts={"one": first, "two": second}))
    envelope = _serialize_packet(Packet.create(stream_id="s", artifacts={"one": first}))
    envelope["artifacts"]["two"] = _serialize_packet(
        Packet.create(stream_id="s", artifacts={"two": second})
    )["artifacts"]["two"]
    with pytest.raises(ValueError, match="private packet exceeds"):
        _deserialize_packet(envelope)
    # Two decoded lengths can share the same base64 length at the rounding boundary.
    envelope["artifacts"] = {
        "one": {
            "private": True,
            "privacy_version": 1,
            "encoding": "bytes",
            "inline_b64": base64.b64encode(b"x" * (limit + 1)).decode(),
        }
    }
    with pytest.raises(ValueError, match="private packet exceeds"):
        _deserialize_packet(envelope)
