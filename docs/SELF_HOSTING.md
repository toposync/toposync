# Self‑hosting (produção)

Toposync agora tem autenticação/autorização local embutida.

- Primeiro acesso exige criar o usuário `owner` local.
- Depois disso, o navegador mantém sessão e não pede senha a cada requisição.
- Para dev/testes, você pode desabilitar auth com `TOPOSYNC_AUTH_MODE=bypass`.
- Cookies de sessão usam `SameSite=Lax` e `HttpOnly`. Para HTTPS, o flag `Secure` é aplicado automaticamente quando o backend detecta HTTPS (incluindo `X-Forwarded-Proto: https`).

### HTTPS e cookies (`Secure`)

Se você estiver servindo o Toposync atrás de um reverse proxy com TLS (Nginx/Caddy/Traefik):

- Garanta que o proxy envie `X-Forwarded-Proto: https`, **ou**
- Force o cookie como HTTPS‑only com `TOPOSYNC_AUTH_COOKIE_SECURE=true`.

## Opção A) Docker (recomendado)

Pré‑requisitos: Docker + Docker Compose.

1) Copie `docker-compose.yml` e `Dockerfile` (na raiz do repo) para uma pasta no servidor, ou clone o repo.
2) Suba:

```bash
docker compose up -d --build
```

3) Acesse:

- `http://<ip-do-servidor>:8000`

**Persistência**: o diretório `./toposync-data/` é montado no container como `/data` e guarda `config.json`, uploads e notificações.

### Instalar extensões no Docker

Extensões são pacotes Python com entry point em `toposync.extensions`. Para adicionar/remover:

- **Recomendado (imagem customizada)**: edite o `Dockerfile` e adicione `RUN pip install <pacote>` (depois do `uv sync`), depois rode `docker compose up -d --build`.
- **Rápido (em runtime)**: `docker compose exec toposync pip install <pacote>` e reinicie o container (não persiste se recriar a imagem).

Verificação rápida:

```bash
curl http://localhost:8000/api/extensions
```

## Opção B) Python (uv/pip)

Serve para rodar direto em uma VM/host Linux/macOS.

Pré‑requisitos: Python 3.11+ e `uv`.

### 1) Instalar o backend

Em um diretório do servidor:

```bash
uv venv
uv pip install ./packages/toposync
```

### 2) Frontend (UI)

O backend pode servir um frontend já “buildado”. Você tem duas opções:

- **Build no servidor** (requer Node 20 + npm):

```bash
npm install
npm --workspace @toposync/frontend run build
```

E rode o servidor com:

```bash
uv run toposync serve --host 0.0.0.0 --port 8000 --data-dir /var/lib/toposync --frontend-dir ./frontend/dist
```

- **Build em outra máquina**: rode o build do frontend em qualquer lugar e copie a pasta `frontend/dist` para o servidor. Depois aponte `--frontend-dir` para esse caminho.

### 3) Instalar extensões (first‑party e comunidade)

O pacote `toposync` já instala o conjunto padrão:

- `toposync-core`
- `toposync-ext-structural`
- `toposync-ext-models`
- `toposync-ext-home-assistant`
- `toposync-ext-images`
- `toposync-ext-cameras`
- `toposync-ext-vision`

Para adicionar o stack de streaming:

```bash
uv pip install toposync-ext-streaming
```

Para instalar uma extensão extra de comunidade, basta instalar o pacote Python e reiniciar o backend:

```bash
uv pip install toposync-ext-<nome>
```

Para remover uma extensão:

```bash
uv pip uninstall toposync-ext-<nome>
```

Verificação rápida:

```bash
curl http://localhost:8000/api/extensions
```

### 4) Rodar como serviço (systemd – Linux)

Exemplo de unit file (ajuste caminhos/usuário):

```ini
[Unit]
Description=Toposync
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/toposync
Environment=TOPOSYNC_DATA_DIR=/var/lib/toposync
ExecStart=/opt/toposync/.venv/bin/toposync serve --host 0.0.0.0 --port 8000 --frontend-dir /opt/toposync/frontend/dist
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Onde ficam os dados?

- `TOPOSYNC_DATA_DIR` (ou `toposync serve --data-dir ...`) controla onde fica:
  - `config.json`
  - `files/` (uploads)
  - `notifications/`

## Produzir um bundle (wheels + frontend) para instalar sem Node no servidor

Se você quer evitar Node no host final, faça o build em uma máquina de build e copie os artefatos:

1) Build do frontend host + bundles das extensões:

```bash
npm install
npm run build:extensions
npm run build:frontend
```

2) Build dos wheels:

```bash
uv build --wheel
uv build extensions/structural --wheel
uv build extensions/models --wheel
uv build extensions/home_assistant --wheel
uv build extensions/images --wheel
# opcional:
uv build extensions/cameras --wheel
```

3) No servidor, instale os wheels e aponte `--frontend-dir` para o `frontend/dist` copiado.
