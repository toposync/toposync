ARG BUILD_ARCH
FROM python:3.12-slim-bookworm

ARG TOPOSYNC_PIP_SPEC=toposync==0.3.4
ARG TOPOSYNC_PIP_INDEX_URL=https://pypi.org/simple
ARG TOPOSYNC_EXTRA_INDEX_URL=
ARG TOPOSYNC_APT_PACKAGES=

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV VIRTUAL_ENV=/opt/toposync/.venv
ENV PATH="/opt/toposync/.venv/bin:${PATH}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini ${TOPOSYNC_APT_PACKAGES} \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VIRTUAL_ENV}" \
 && python -m pip install --upgrade pip

RUN /bin/sh -lc '\
  if [ -n "${TOPOSYNC_EXTRA_INDEX_URL}" ]; then \
    python -m pip install --index-url "${TOPOSYNC_PIP_INDEX_URL}" --extra-index-url "${TOPOSYNC_EXTRA_INDEX_URL}" "${TOPOSYNC_PIP_SPEC}"; \
  else \
    python -m pip install --index-url "${TOPOSYNC_PIP_INDEX_URL}" "${TOPOSYNC_PIP_SPEC}"; \
  fi'

COPY run_addon.py /usr/local/bin/run_addon.py

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["tini", "--"]
CMD ["python", "/usr/local/bin/run_addon.py"]
