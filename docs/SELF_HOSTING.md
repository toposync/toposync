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

O Docker agora é organizado em:

- uma imagem CPU de produção (`runtime-cpu`)
- uma variante CUDA separada (`runtime-cuda`)
- extras opcionais por build args, sem multiplicar imagens first-party para cada combinação pequena

No runtime de produção, o `toposync-core` já leva o frontend host embutido. Isso significa que, no container final, a UI e a API são atendidas pelo mesmo processo e pela mesma porta:

- `/` serve a UI
- `/api/*` serve a API
- `/extensions/*` continua exposto pelo mesmo backend

### CPU padrão

1) Copie `docker-compose.yml` e `Dockerfile` (na raiz do repo) para uma pasta no servidor, ou clone o repo.
2) Suba:

```bash
docker compose up -d --build
```

3) Acesse:

- `http://<ip-do-servidor>:8000`

**Persistência**: o diretório `./toposync-data/` é montado no container como `/data` e guarda `config.json`, uploads, notificações e o cache/runtime do MediaMTX quando o stack de streaming estiver ativo.

Verificações rápidas:

```bash
curl -I http://localhost:8000/
curl http://localhost:8000/api/health
curl http://localhost:8000/api/extensions
```

O esperado é:

- `/` responder `200` com `Content-Type: text/html`
- `/api/health` responder `200`
- `/api/extensions` listar as extensões carregadas no ambiente

### Ativar extras na mesma imagem CPU

Extras first-party podem ser ativados por build args.

Exemplo: streaming na imagem CPU:

```bash
TOPOSYNC_APT_PACKAGES=ffmpeg \
TOPOSYNC_EXTRA_WHEELS="/wheelhouse/toposync_ext_streaming-*.whl" \
docker compose up -d --build
```

Nesse caso:

- `ffmpeg` entra como dependência de sistema da imagem
- `toposync-ext-streaming` entra como wheel adicional no mesmo container
- o runtime do MediaMTX passa a persistir em `/data/runtime`

Para uma extensão extra distribuída por pacote Python:

```bash
TOPOSYNC_EXTRA_PIP_PACKAGES="toposync-ext-<nome>" \
docker compose up -d --build
```

### CUDA (Linux + NVIDIA)

Para GPU NVIDIA em container, use o override CUDA:

```bash
docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d --build
```

Para CUDA + streaming:

```bash
TOPOSYNC_APT_PACKAGES=ffmpeg \
TOPOSYNC_EXTRA_WHEELS="/wheelhouse/toposync_ext_streaming-*.whl" \
docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d --build
```

Pré‑requisitos do host para CUDA:

- Linux com GPU NVIDIA
- driver NVIDIA instalado no host
- NVIDIA Container Toolkit / suporte a GPU no Docker

Referências:

- Docker Compose GPU support: https://docs.docker.com/compose/how-tos/gpu-support/
- ONNX Runtime CUDA EP requirements: https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html

Observação: para Windows, a recomendação do projeto continua sendo instalação nativa com `toposync-vision-directml`, não container CUDA.

## Opção A.1) Home Assistant add-on

Se o objetivo é rodar o Toposync dentro do ecossistema do Home Assistant, com:

- instalação/distribuição via add-on
- app na sidebar
- ingress
- execução supervisionada
- acesso interno ao Core API

use o repositório dedicado:

- https://github.com/toposync/toposync-homeassistant-addon

No Home Assistant, adicione este repositório no Add-on Store:

```text
https://github.com/toposync/toposync-homeassistant-addon
```

Guia e detalhes:

- [Home Assistant add-on](/Users/c/Projects/toposync-2/docs/HOME_ASSISTANT_ADDON.md)

Resumo do funcionamento:

- o add-on executa o mesmo `toposync serve`
- frontend e API continuam na mesma porta interna
- auth passa para modo ingress do Home Assistant
- a extensão `home_assistant` usa o `SUPERVISOR_TOKEN` e o proxy interno do Core API, sem pedir `host`/`apiKey` manualmente

Observação importante:

- o add-on atual foi fechado no caminho CPU
- CUDA continua como variante separada futura

### Instalar extensões no Docker

Extensões são pacotes Python com entry point em `toposync.extensions`.

Para first-party já presentes neste repo, a forma recomendada é habilitar o wheel local via build args.

Para pacotes extras publicados separadamente, use:

- `TOPOSYNC_EXTRA_PIP_PACKAGES="toposync-ext-<nome>"`

Depois rode `docker compose up -d --build`.

Evite instalar extensões manualmente dentro do container em runtime, porque isso não fica reprodutível quando a imagem for recriada.

Verificação rápida:

```bash
curl http://localhost:8000/api/extensions
```

## O que foi validado neste fluxo

Os cenários abaixo já foram validados no ambiente de build do projeto:

- install do bundle padrão `toposync` a partir dos wheels de release locais
- startup do app instalado com frontend embutido respondendo em `/`
- healthcheck em `/api/health`
- install do extra `toposync-ext-streaming` no mesmo ambiente
- carregamento da extensão `com.toposync.streaming` em `/api/extensions`

Limite conhecido:

- a variante CUDA foi validada por metadata e dependências do wheel, mas não por execução real neste ambiente de desenvolvimento, porque o host de validação não tinha daemon Docker ativo nem GPU NVIDIA disponível

## Opção B) Python (uv/pip)

Serve para rodar direto em uma VM/host Linux/macOS.

Pré‑requisitos: Python 3.11+ e `uv`.

Se você estiver no Windows, veja o guia dedicado:

- [Instalação no Windows](/Users/c/Projects/toposync-2/docs/WINDOWS.md)

### 1) Instalar o backend

Em um diretório do servidor:

```bash
uv venv
uv pip install ./packages/toposync
```

### 2) Frontend (UI)

O pacote `toposync-core` já leva o frontend host embutido. Depois da instalação, o backend serve a UI por padrão, sem Node no servidor.

Se você quiser sobrescrever a UI host por um bundle externo durante desenvolvimento ou rollout controlado, use `--frontend-dir /caminho/para/frontend/dist` ou `TOPOSYNC_FRONTEND_DIR`.

### 3) Instalar extensões (first‑party e comunidade)

O pacote `toposync` já instala o conjunto padrão:

- `toposync-core`
- `toposync-ext-structural`
- `toposync-ext-models`
- `toposync-ext-home-assistant`
- `toposync-ext-images`
- `toposync-ext-cameras`
- `toposync-ext-vision`
- `onnxruntime`

Esse é o bundle padrão em CPU.

Observação para `vision`: os manifests first-party vão junto no pacote, mas os pesos ONNX oficiais não. Em ambiente instalado, eles ficam no store gerenciado em `TOPOSYNC_DATA_DIR/vision-models/` depois de upload, cópia administrada ou build local assistido.

Para provisionar diretamente um ambiente com aceleração first-party:

```bash
uv pip install ./packages/toposync-vision-cuda
```

ou no Windows com DirectML:

```bash
uv pip install ./packages/toposync-vision-directml
```

Se você já instalou o bundle padrão em CPU e quiser trocar o runtime do mesmo ambiente, remova o bundle CPU e reinstale com o bundle de aceleração:

```bash
uv pip uninstall toposync onnxruntime
uv pip install ./packages/toposync-vision-cuda
```

Para adicionar o stack de streaming:

```bash
uv pip install ./packages/toposync-streaming
```

Esse bundle instala `toposync` + `toposync-ext-streaming`. O runtime de streaming baixa o MediaMTX sob demanda quando a engine é iniciada, e o FFmpeg deve vir do `PATH` ou de `TOPOSYNC_STREAMING_FFMPEG_PATH`.

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
ExecStart=/opt/toposync/.venv/bin/toposync serve --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Onde ficam os dados?

- `TOPOSYNC_DATA_DIR` (ou `toposync serve --data-dir ...`) controla onde fica:
  - `config.json`
  - `files/` (uploads)
  - `notifications/`

## Produzir um bundle (wheels) para instalar sem Node no servidor

Se você quer evitar Node no host final, faça o build em uma máquina de build e copie os artefatos:

1) Build dos bundles das extensões:

```bash
npm install
npm run build:extensions
```

O `uv build` do core já embute o frontend host; se `frontend/dist` não existir, ele tenta gerá-lo durante o build.

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

3) No servidor, instale os wheels. A UI já vai junto no `toposync-core`, sem precisar copiar `frontend/dist`.
