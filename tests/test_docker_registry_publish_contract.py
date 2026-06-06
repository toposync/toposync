from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _project_version(path: str) -> str:
    with (ROOT / path).open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _load_registry_smoke_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_docker_registry_image",
        ROOT / "scripts/check_docker_registry_image.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _section(text: str, start: str, end: str | None = None) -> str:
    section = text.split(start, 1)[1]
    if end is not None:
        section = section.split(end, 1)[0]
    return section


def test_registry_dockerfile_installs_public_cpu_streaming_runtime() -> None:
    dockerfile = _read("Dockerfile.registry")
    runtime_cpu = _section(
        dockerfile,
        "FROM python:3.12-slim-bookworm AS runtime-cpu",
        "FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS runtime-cuda",
    )

    assert 'org.opencontainers.image.source="${TOPOSYNC_IMAGE_SOURCE}"' in runtime_cpu
    assert '"toposync-streaming==${TOPOSYNC_VERSION}"' in runtime_cpu
    assert "toposync-vision-cuda" not in runtime_cpu
    for package_name in (
        "ffmpeg",
        "tini",
        "build-essential",
        "cmake",
        "ninja-build",
        "git",
        "pkg-config",
        "curl",
    ):
        assert package_name in runtime_cpu
    assert "python3-dev" not in runtime_cpu
    assert "COPY --from=go2rtc-build /go2rtc/go2rtc /usr/local/bin/go2rtc" in runtime_cpu


def test_registry_dockerfile_installs_cuda_streaming_without_cpu_bundle() -> None:
    dockerfile = _read("Dockerfile.registry")
    runtime_cuda = _section(
        dockerfile,
        "FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS runtime-cuda",
    )

    assert 'org.opencontainers.image.source="${TOPOSYNC_IMAGE_SOURCE}"' in runtime_cuda
    assert '"toposync-vision-cuda==${TOPOSYNC_VERSION}"' in runtime_cuda
    assert '"toposync-ext-streaming==${TOPOSYNC_EXT_STREAMING_VERSION}"' in runtime_cuda
    assert "toposync-streaming==" not in runtime_cuda
    for package_name in ("ffmpeg", "go2rtc", "cmake", "ninja-build", "git", "pkg-config", "curl"):
        assert package_name in runtime_cuda


def test_registry_dockerfile_resolves_go2rtc_from_streaming_extension_wheel() -> None:
    dockerfile = _read("Dockerfile.registry")
    go2rtc_stage = _section(
        dockerfile,
        "FROM python:3.12-slim-bookworm AS go2rtc-build",
        "FROM python:3.12-slim-bookworm AS runtime-cpu",
    )

    assert "ARG GO2RTC_VERSION" in go2rtc_stage
    assert (
        'python -m pip download --no-deps --dest /tmp/streaming-wheel '
        '"toposync-ext-streaming==${TOPOSYNC_EXT_STREAMING_VERSION}"'
    ) in go2rtc_stage
    assert "GO2RTC_VERSION" in go2rtc_stage
    assert "toposync_ext_streaming/streaming/__init__.py" in go2rtc_stage
    assert "go2rtc_linux_{arch}" in go2rtc_stage
    assert 'amd64|x86_64) go2rtc_arch="amd64"' in go2rtc_stage
    assert 'aarch64|arm64) go2rtc_arch="arm64"' in go2rtc_stage


def test_compose_defaults_pull_public_ghcr_images() -> None:
    compose = _read("docker-compose.yml")
    compose_cuda = _read("docker-compose.cuda.yml")
    local_build = _read("docker-compose.local-build.yml")
    app_version = _project_version("packages/toposync/pyproject.toml")

    assert f"image: ${{TOPOSYNC_IMAGE:-ghcr.io/toposync/toposync:{app_version}}}" in compose
    assert f"image: ${{TOPOSYNC_CUDA_IMAGE:-ghcr.io/toposync/toposync:{app_version}-cuda}}" in compose_cuda
    assert "image: ${TOPOSYNC_LOCAL_IMAGE:-toposync:local}" in local_build
    assert "target: ${TOPOSYNC_DOCKER_TARGET:-runtime-cpu}" in local_build
    assert re.search(r"(?m)^\s+build:", compose) is None
    assert re.search(r"(?m)^\s+build:", compose_cuda) is None
    assert re.search(r"(?m)^\s+build:", local_build) is not None


def test_docker_publish_workflow_uses_ghcr_tags_and_attestations() -> None:
    workflow = _read(".github/workflows/docker-publish.yml")

    assert 'tags:\n      - "toposync-v*"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "packages: write" in workflow
    assert "attestations: write" in workflow
    assert "id-token: write" in workflow
    assert "docker/login-action@v3" in workflow
    assert "docker/build-push-action@v6" in workflow
    assert "actions/attest-build-provenance@v2" in workflow
    assert "REGISTRY: ghcr.io" in workflow
    assert "IMAGE_NAME: toposync/toposync" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "platforms: linux/amd64" in workflow
    assert "toposync-streaming" in workflow
    assert "toposync-vision-cuda" in workflow
    assert "${{ steps.release.outputs.version }}-cuda" in workflow
    assert "Verify anonymous GHCR pull" in workflow
    assert 'docker logout "${{ env.REGISTRY }}" || true' in workflow
    assert "GHCR package is not public" in workflow
    assert "docker.io" not in workflow
    assert "gcloud" not in workflow.lower()


def test_registry_public_version_defaults_match_project_versions() -> None:
    app_version = _project_version("packages/toposync/pyproject.toml")
    streaming_version = _project_version("extensions/streaming/pyproject.toml")

    expectations = {
        "Dockerfile.registry": [
            f"ARG TOPOSYNC_VERSION={app_version}",
            f"ARG TOPOSYNC_EXT_STREAMING_VERSION={streaming_version}",
        ],
        ".github/workflows/docker-publish.yml": [f'default: "{app_version}"'],
        "scripts/check_docker_registry_image.py": [f'DEFAULT_IMAGE = "ghcr.io/toposync/toposync:{app_version}"'],
        "docs-site/docs/installation/docker-cpu.mdx": [f"ghcr.io/toposync/toposync:{app_version}"],
        "docs-site/docs/installation/docker-cuda.mdx": [f"ghcr.io/toposync/toposync:{app_version}-cuda"],
        "docs-site/docs/installation/processing-server-docker.mdx": [
            f"ghcr.io/toposync/toposync:{app_version}",
            f"ghcr.io/toposync/toposync:{app_version}-cuda",
        ],
        "docs-site/i18n/pt-BR/docusaurus-plugin-content-docs/current/installation/docker-cpu.mdx": [
            f"ghcr.io/toposync/toposync:{app_version}"
        ],
        "docs-site/i18n/pt-BR/docusaurus-plugin-content-docs/current/installation/docker-cuda.mdx": [
            f"ghcr.io/toposync/toposync:{app_version}-cuda"
        ],
        "docs-site/i18n/pt-BR/docusaurus-plugin-content-docs/current/installation/processing-server-docker.mdx": [
            f"ghcr.io/toposync/toposync:{app_version}",
            f"ghcr.io/toposync/toposync:{app_version}-cuda",
        ],
    }
    for path, required_snippets in expectations.items():
        text = _read(path)
        for snippet in required_snippets:
            assert snippet in text, f"{path} is missing {snippet!r}"
        assert "0.7.6" not in text
        assert "0.4.6" not in text


def test_registry_smoke_treats_connection_reset_as_startup_transient(monkeypatch) -> None:
    smoke = _load_registry_smoke_script()
    health_attempts = iter([ConnectionResetError("connection reset by peer"), {"status": "ok"}])

    def read_json(_url: str, *, timeout: float = 5.0) -> object:
        result = next(health_attempts)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(smoke, "_read_json", read_json)
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    smoke._wait_for_health("http://127.0.0.1:1", timeout_s=5.0)


def test_registry_smoke_uses_disposable_docker_volume_for_data() -> None:
    smoke_script = _read("scripts/check_docker_registry_image.py")

    assert "TemporaryDirectory" not in smoke_script
    assert '"docker", "volume", "create", volume_name' in smoke_script
    assert '"docker", "volume", "rm", "-f", volume_name' in smoke_script
    assert 'f"{volume_name}:/data"' in smoke_script
