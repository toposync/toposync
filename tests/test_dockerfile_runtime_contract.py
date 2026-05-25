from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_images_bundle_go2rtc_for_mse() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim-bookworm AS go2rtc-build" in dockerfile
    assert "ARG GO2RTC_VERSION=" in dockerfile
    assert "toposync_ext_streaming-*.whl" in dockerfile
    assert "go2rtc_linux_{arch}" in dockerfile
    assert 'ENV TOPOSYNC_STREAMING_GO2RTC_PATH=/usr/local/bin/go2rtc' in dockerfile
    assert dockerfile.count("COPY --from=go2rtc-build /go2rtc/go2rtc /usr/local/bin/go2rtc") == 2
    assert 'amd64|x86_64) go2rtc_arch="amd64"' in dockerfile
    assert 'aarch64|arm64) go2rtc_arch="arm64"' in dockerfile
