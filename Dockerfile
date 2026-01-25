FROM node:20-bookworm-slim AS node-build

WORKDIR /app
COPY . .

RUN npm ci
RUN npm run build:extensions
RUN npm run build:frontend


FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=node-build /app /app

RUN uv sync --frozen --no-dev --no-editable

EXPOSE 8000
VOLUME ["/data"]

CMD ["toposync", "serve", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data", "--frontend-dir", "/app/frontend/dist"]
