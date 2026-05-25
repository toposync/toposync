# syntax=docker/dockerfile:1.7

FROM node:20-bookworm-slim AS frontend-build

WORKDIR /src
COPY . .

RUN npm ci
RUN npm run build:extensions
RUN npm run build:frontend
RUN rm -rf node_modules frontend/node_modules extensions/*/ui/node_modules


FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS wheel-build

WORKDIR /src
COPY --from=frontend-build /src /src

RUN mkdir -p /wheelhouse \
 && uv build --wheel --out-dir /wheelhouse . \
 && uv build --wheel --out-dir /wheelhouse packages/toposync \
 && uv build --wheel --out-dir /wheelhouse packages/toposync-vision-cuda \
 && uv build --wheel --out-dir /wheelhouse extensions/structural \
 && uv build --wheel --out-dir /wheelhouse extensions/models \
 && uv build --wheel --out-dir /wheelhouse extensions/images \
 && uv build --wheel --out-dir /wheelhouse extensions/home_assistant \
 && uv build --wheel --out-dir /wheelhouse extensions/cameras \
 && uv build --wheel --out-dir /wheelhouse extensions/vision \
 && uv build --wheel --out-dir /wheelhouse extensions/streaming


FROM python:3.12-slim-bookworm AS go2rtc-build

ARG BUILD_ARCH
ARG TARGETARCH
ARG GO2RTC_VERSION=

COPY --from=wheel-build /wheelhouse /wheelhouse

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN set -e; \
  build_arch="${BUILD_ARCH:-${TARGETARCH:-$(uname -m)}}"; \
  case "${build_arch}" in \
    amd64|x86_64) go2rtc_arch="amd64" ;; \
    aarch64|arm64) go2rtc_arch="arm64" ;; \
    *) echo "Unsupported architecture for go2rtc: ${build_arch}" >&2; exit 1 ;; \
  esac; \
  GO2RTC_ARCH="${go2rtc_arch}" GO2RTC_VERSION="${GO2RTC_VERSION}" python - <<'PY'
import glob
import os
import re
import shutil
import stat
import urllib.request
import zipfile


def wheel_go2rtc_version() -> str:
    for wheel_path in sorted(glob.glob("/wheelhouse/toposync_ext_streaming-*.whl")):
        with zipfile.ZipFile(wheel_path) as wheel:
            source = wheel.read("toposync_ext_streaming/streaming/__init__.py").decode("utf-8")
        match = re.search(r'^GO2RTC_VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE)
        if match:
            return match.group(1)
    return ""


arch = os.environ["GO2RTC_ARCH"]
version = os.environ.get("GO2RTC_VERSION") or wheel_go2rtc_version() or "v1.9.14"
target = "/go2rtc/go2rtc"
temp_path = f"{target}.download"
url = f"https://github.com/AlexxIT/go2rtc/releases/download/{version}/go2rtc_linux_{arch}"
os.makedirs(os.path.dirname(target), exist_ok=True)
request = urllib.request.Request(
    url,
    headers={"user-agent": "toposync-docker/1.0", "accept": "*/*"},
    method="GET",
)
with urllib.request.urlopen(request, timeout=60.0) as response, open(temp_path, "wb") as writer:
    shutil.copyfileobj(response, writer)
os.replace(temp_path, target)
mode = os.stat(target).st_mode
os.chmod(target, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
PY


FROM python:3.12-slim-bookworm AS runtime-cpu

ARG TOPOSYNC_INSTALL_WHEEL="/wheelhouse/toposync-*.whl"
ARG TOPOSYNC_EXTRA_WHEELS=""
ARG TOPOSYNC_EXTRA_PIP_PACKAGES=""
ARG TOPOSYNC_APT_PACKAGES=""

WORKDIR /srv/toposync
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV VIRTUAL_ENV=/opt/toposync/.venv
ENV PATH="/opt/toposync/.venv/bin:${PATH}"
ENV TOPOSYNC_DATA_DIR=/data
ENV TOPOSYNC_STREAMING_ENGINE_CACHE_DIR=/data/runtime
ENV TOPOSYNC_STREAMING_GO2RTC_PATH=/usr/local/bin/go2rtc

COPY --from=wheel-build /wheelhouse /wheelhouse
COPY --from=go2rtc-build /go2rtc/go2rtc /usr/local/bin/go2rtc

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini ${TOPOSYNC_APT_PACKAGES} \
 && rm -rf /var/lib/apt/lists/* \
 && python -m venv "${VIRTUAL_ENV}" \
 && "${VIRTUAL_ENV}/bin/python" -m pip install --upgrade pip \
 && "${VIRTUAL_ENV}/bin/python" -m pip install --find-links=/wheelhouse ${TOPOSYNC_INSTALL_WHEEL} ${TOPOSYNC_EXTRA_WHEELS} ${TOPOSYNC_EXTRA_PIP_PACKAGES} \
 && rm -rf /wheelhouse

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 CMD "${VIRTUAL_ENV}/bin/python" -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["tini", "--"]
CMD ["toposync", "serve", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data"]


FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS runtime-cuda

ARG TOPOSYNC_INSTALL_WHEEL="/wheelhouse/toposync_vision_cuda-*.whl"
ARG TOPOSYNC_EXTRA_WHEELS=""
ARG TOPOSYNC_EXTRA_PIP_PACKAGES=""
ARG TOPOSYNC_APT_PACKAGES=""

WORKDIR /srv/toposync
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV VIRTUAL_ENV=/opt/toposync/.venv
ENV PATH="/opt/toposync/.venv/bin:${PATH}"
ENV TOPOSYNC_DATA_DIR=/data
ENV TOPOSYNC_STREAMING_ENGINE_CACHE_DIR=/data/runtime
ENV TOPOSYNC_STREAMING_GO2RTC_PATH=/usr/local/bin/go2rtc

COPY --from=wheel-build /wheelhouse /wheelhouse
COPY --from=go2rtc-build /go2rtc/go2rtc /usr/local/bin/go2rtc

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv python3-pip tini ${TOPOSYNC_APT_PACKAGES} \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv "${VIRTUAL_ENV}" \
 && "${VIRTUAL_ENV}/bin/python" -m pip install --upgrade pip \
 && "${VIRTUAL_ENV}/bin/python" -m pip install --find-links=/wheelhouse ${TOPOSYNC_INSTALL_WHEEL} ${TOPOSYNC_EXTRA_WHEELS} ${TOPOSYNC_EXTRA_PIP_PACKAGES} \
 && rm -rf /wheelhouse

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 CMD "${VIRTUAL_ENV}/bin/python" -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["tini", "--"]
CMD ["toposync", "serve", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data"]
