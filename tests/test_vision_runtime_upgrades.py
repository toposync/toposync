from __future__ import annotations

from toposync_ext_vision.processing.runtime_upgrades import collect_runtime_upgrade_guidance
import toposync_ext_vision.processing.runtime_upgrades as runtime_upgrades


def test_runtime_upgrade_guidance_recommends_cuda_for_nvidia_cpu_bundle(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(runtime_upgrades, "installed_onnxruntime_packages", lambda: ["onnxruntime"])
    monkeypatch.setattr(
        runtime_upgrades,
        "probe_gpu_adapters",
        lambda **_kwargs: [
            {"name": "NVIDIA GeForce RTX 4060", "vendor": "nvidia", "source": "nvidia-smi"}
        ],
    )

    guidance = collect_runtime_upgrade_guidance(
        system_info={"platform": {"system": "Linux"}},
        execution_providers=["CPUExecutionProvider"],
    )

    assert guidance["current_variant"] == "cpu"
    assert guidance["hardware"]["nvidia_detected"] is True
    suggestions = guidance["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["package_name"] == "toposync-vision-cuda"
    assert suggestions[0]["replacement_required"] is True
    assert "pip uninstall -y toposync onnxruntime" in suggestions[0]["replace_command"]


def test_runtime_upgrade_guidance_recommends_directml_for_windows_gpu(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(runtime_upgrades, "installed_onnxruntime_packages", lambda: ["onnxruntime"])
    monkeypatch.setattr(
        runtime_upgrades,
        "probe_gpu_adapters",
        lambda **_kwargs: [
            {"name": "Intel Iris Xe Graphics", "vendor": "intel", "source": "win32_video_controller"}
        ],
    )

    guidance = collect_runtime_upgrade_guidance(
        system_info={"platform": {"system": "Windows"}},
        execution_providers=["CPUExecutionProvider"],
    )

    assert guidance["current_variant"] == "cpu"
    assert guidance["hardware"]["windows_gpu_detected"] is True
    suggestions = guidance["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["package_name"] == "toposync-vision-directml"
    assert suggestions[0]["provider_id"] == "DmlExecutionProvider"


def test_runtime_upgrade_guidance_skips_cuda_when_already_available(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(runtime_upgrades, "installed_onnxruntime_packages", lambda: ["onnxruntime-gpu"])
    monkeypatch.setattr(
        runtime_upgrades,
        "probe_gpu_adapters",
        lambda **_kwargs: [
            {"name": "NVIDIA GeForce RTX 4060", "vendor": "nvidia", "source": "nvidia-smi"}
        ],
    )

    guidance = collect_runtime_upgrade_guidance(
        system_info={"platform": {"system": "Linux"}},
        execution_providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    assert guidance["current_variant"] == "cuda"
    assert guidance["suggestions"] == []
