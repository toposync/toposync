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

COPY --from=wheel-build /wheelhouse /wheelhouse

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

COPY --from=wheel-build /wheelhouse /wheelhouse

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
