from __future__ import annotations

import logging
from types import SimpleNamespace

import asyncio

from toposync_ext_streaming.streaming.encoder_state import EncoderTrustStore
from toposync_ext_streaming.streaming import publisher_manager as publisher_manager_module
from toposync_ext_streaming.streaming.publisher_manager import (
    PublisherEncoderPolicy,
    PublisherEncodingSettings,
    PublisherInputSettings,
    PublisherOutput,
    PublisherRuntimeConfig,
    _PublisherRuntime,
)


def test_ffmpeg_encoder_probe_filters_advertised_nvenc_without_cuda(monkeypatch) -> None:
    def fake_run(args, **_kwargs):  # noqa: ANN001, ANN202
        if "-encoders" in args:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Encoders:\n"
                    " V..... h264_nvenc           NVIDIA NVENC H.264 encoder\n"
                    " V..... libx264              libx264 H.264 / AVC encoder\n"
                ),
                stderr="",
            )

        encoder = str(args[args.index("-c:v") + 1])
        if encoder == "libx264":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if encoder == "h264_nvenc":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Cannot load libcuda.so.1\nError while opening encoder\n",
            )
        raise AssertionError(f"unexpected encoder probe: {encoder}")

    monkeypatch.setattr(publisher_manager_module.subprocess, "run", fake_run)

    encoders = publisher_manager_module._probe_ffmpeg_encoders(
        "/usr/bin/ffmpeg",
        logger=logging.getLogger("tests.streaming.publisher"),
    )

    assert encoders == {"libx264"}


def test_auto_hardware_selection_falls_back_to_libx264_when_only_cpu_encoder_is_usable(
    tmp_path,
) -> None:
    runtime = _make_runtime(
        tmp_path,
        supported_encoders={"libx264"},
        encoding=PublisherEncodingSettings(width=64, height=64, fps=10, prefer_hardware=True),
    )

    args, codec, hardware_accelerated = runtime._build_ffmpeg_args()

    assert codec == "libx264"
    assert hardware_accelerated is False
    assert args[args.index("-c:v") + 1] == "libx264"


def test_auto_hardware_selection_disables_runtime_failed_encoder(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _make_runtime(
            tmp_path,
            supported_encoders={"h264_nvenc", "libx264"},
            encoding=PublisherEncodingSettings(width=64, height=64, fps=10, prefer_hardware=True),
        )

        assert runtime._pick_video_codec() == "h264_nvenc"

        runtime.active_codec = "h264_nvenc"
        runtime._stderr_tail.append("Cannot load libcuda.so.1")

        assert await runtime._maybe_quarantine_failed_auto_encoder() is True
        await runtime._refresh_quarantined_encoders()
        assert runtime._pick_video_codec() == "libx264"
        state = await runtime._encoder_store.state_for("h264_nvenc")
        assert state.state == "quarantined"
        assert state.reason == "runtime_failure"

    asyncio.run(scenario())


def test_encoder_trust_store_persists_expires_and_clears_quarantine(tmp_path) -> None:
    clock = {"now": 100.0}

    async def scenario() -> None:
        path = tmp_path / "encoder-state.json"
        store = EncoderTrustStore(path=path, host_id="local", time_func=lambda: clock["now"])
        await store.quarantine(
            "h264_videotoolbox",
            reason="runtime_failure",
            duration_seconds=10,
            output_id="tx:path",
            error="Cannot load driver",
        )

        restored = EncoderTrustStore(path=path, host_id="local", time_func=lambda: clock["now"])
        state = await restored.state_for("h264_videotoolbox")
        assert state.state == "quarantined"
        assert state.until_unix == 110.0
        assert state.last_output_id == "tx:path"

        clock["now"] = 111.0
        expired = await restored.state_for("h264_videotoolbox")
        assert expired.state == "candidate"
        assert expired.reason == "quarantine_expired"

        await restored.quarantine("h264_videotoolbox", reason="runtime_failure", duration_seconds=10)
        assert await restored.clear("h264_videotoolbox") == 1
        assert (await restored.state_for("h264_videotoolbox")).state == "candidate"

    asyncio.run(scenario())


def test_cpu_encoder_mode_never_selects_hardware(tmp_path) -> None:
    runtime = _make_runtime(
        tmp_path,
        supported_encoders={"h264_nvenc", "libx264"},
        encoding=PublisherEncodingSettings(
            width=64,
            height=64,
            fps=10,
            prefer_hardware=True,
            encoder_mode="cpu",
        ),
    )

    assert runtime._pick_video_codec() == "libx264"


def test_restart_threshold_quarantines_active_hardware_encoder(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _make_runtime(
            tmp_path,
            supported_encoders={"h264_nvenc", "libx264"},
            encoding=PublisherEncodingSettings(width=64, height=64, fps=10, prefer_hardware=True),
            encoder_policy=PublisherEncoderPolicy(
                quarantine_after_restarts=2,
                quarantine_window_seconds=600,
                quarantine_duration_seconds=3600,
            ),
        )
        runtime.active_codec = "h264_nvenc"

        await runtime._maybe_quarantine_after_restart_threshold()
        await runtime._maybe_quarantine_after_restart_threshold()
        assert (await runtime._encoder_store.state_for("h264_nvenc")).state == "candidate"

        await runtime._maybe_quarantine_after_restart_threshold()
        assert (await runtime._encoder_store.state_for("h264_nvenc")).state == "quarantined"

    asyncio.run(scenario())


def _make_runtime(
    tmp_path,
    *,
    supported_encoders: set[str],
    encoding: PublisherEncodingSettings,
    encoder_policy: PublisherEncoderPolicy | None = None,
) -> _PublisherRuntime:
    config = PublisherRuntimeConfig(
        output=PublisherOutput(
            output_id="output_test",
            transmission_id="transmission_test",
            protocol="all",
        ),
        engine_path="test-path",
        publish_url="rtsp://127.0.0.1:8554/test-path",
        encoding=encoding,
        input_settings=PublisherInputSettings(),
    )
    return _PublisherRuntime(
        ffmpeg_path="/usr/bin/ffmpeg",
        ffmpeg_source="system",
        supported_encoders=supported_encoders,
        config=config,
        logs_dir=tmp_path,
        encoder_store=EncoderTrustStore(path=tmp_path / "encoder-state.json", host_id="local"),
        encoder_policy=encoder_policy,
    )
