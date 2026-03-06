# Dossiê técnico — Toposync + planejamento do app (Android/iOS/TV) com Expo

Autor: Mateus Calza  
Data de referência: **2026-02-26**  
Repo: `toposync-2` (core + extensões first‑party)

## Objetivo deste dossiê

Consolidar **toda a informação necessária (do código atual)** para planejar e desenvolver um app Expo (mobile/tablet/TV) que:

- conecta via rede à instância Toposync (LAN‑first) e autentica;
- renderiza uma **tela inicial 3D** (ThreeJS) com interação (Home Assistant + câmeras);
- renderiza um **dashboard de transmissões** (HLS/WebRTC), com fullscreen e PiP;
- consome e exibe **notificações em tempo real** (stream + detalhe);
- no futuro, suporta **cloud** (push por Firebase + acesso remoto) sem se tornar incompatível;
- no futuro, cria composições via **RoomPlan** (edição completa fica para depois);
- mantém experiência **instantânea** com **offline‑first + cache + SWR**.

> Observação: o app não terá acesso ao repo durante o planejamento seguinte; portanto este documento inclui **APIs, modelos, fluxos e detalhes práticos** (incluindo pitfalls).

---

## 1) O que é o Toposync (visão geral)

Toposync é uma plataforma **local‑first** (Python + React + ThreeJS) para um “digital twin” de automação residencial, construída sobre um **runtime de extensões**.

**Ideias centrais**

- **Composição**: um “mapa” da casa com **elementos** (paredes, áreas, câmeras, itens do Home Assistant, modelos 3D etc.) que pode ser renderizado em 2D e 3D.
- **Extensões**: recursos são instalados como pacotes Python (wheel), com UI embutida (Module Federation) e APIs FastAPI opcionais.
- **Pipelines (DAG)**: runtime global para automações/visão computacional (câmeras, YOLO etc.). Pipelines alimentam notificações e (via extensão) streaming.

---

## 2) Arquitetura e tecnologias (o que existe hoje)

### 2.1 Backend (core)

- Linguagem: **Python 3.11+**
- Framework: **FastAPI**
- Tipos/validação: **Pydantic v2**
- Servidor: **Uvicorn**
- Persistência:
  - `<data_dir>/config.json` (config e estado principal, **local‑first**)
  - `<data_dir>/files/` (uploads do usuário: glb/gltf, png etc.)
  - `<data_dir>/notifications/notifications.sqlite3` (notificações)
  - `<data_dir>/auth/auth.sqlite3` (usuários/sessões/grants/pairing)

### 2.2 Frontend (host)

- Linguagem: **TypeScript**
- UI: **React 18**
- 3D: **ThreeJS** (sem react‑three‑fiber)
- Build/dev server: **webpack / webpack-dev-server**

### 2.3 Extensões (runtime)

Uma extensão Toposync é um pacote Python que contém:

- `extension.json` (manifesto)
- entry point Python (`toposync.extensions`)
- UI prebuilt em `static/remoteEntry.js` (Module Federation)
- (opcional) rotas FastAPI e handlers no EventBus

O host faz:

1. `GET /api/extensions`
2. carrega `remoteEntry.js` de cada extensão em runtime: `/extensions/<extension_id>/remoteEntry.js`
3. executa `activate(host)`

---

## 3) Execução local‑first: onde ficam os dados e como o app deve pensar

### 3.1 `TOPOSYNC_DATA_DIR` e arquivos

O backend usa um diretório de dados (por env ou default por SO). Estrutura:

- `config.json` (composições + settings + pipelines)
- `files/` (uploads por diretórios)
- `auth/auth.sqlite3`
- `notifications/notifications.sqlite3`

Defaults (quando `TOPOSYNC_DATA_DIR` não está definido):

- Linux: `$XDG_DATA_HOME/toposync` ou `~/.local/share/toposync`
- macOS: `~/Library/Application Support/Toposync`
- Windows: `%APPDATA%/Toposync`

### 3.2 Modelo do `config.json` (essencial para o app)

Estrutura semântica atual (resumo):

```json
{
  "schema_version": 1,
  "compositions": [
    { "id": "ground", "name": "Terreo", "elements": [] }
  ],
  "active_composition_id": "ground",
  "settings": {
    "core": {
      "processing_servers": []
    },
    "extensions": {
      "com.toposync.home_assistant": { "servers": [] },
      "com.toposync.cameras": { "cameras": [] },
      "com.toposync.streaming": { "engine": {}, "transmissions": [] }
    }
  },
  "pipelines": []
}
```

Implicação para o app: **toda a “casa”** (composição + settings) é recuperável via API e cacheável localmente no device.

---

## 4) Superfície de API (core) — endpoints que o app precisa conhecer

### 4.1 Base URL / portas (dev vs produção)

- Produção/self‑hosting padrão: `http://<host>:8000` (backend serve também o frontend).
- Dev (típico): frontend em `http://<host>:5173` e backend em `http://<host>:8000`.
- Streaming (MediaMTX) usa **portas separadas** (por default):
  - RTSP: `8554`
  - HLS: `8888`
  - WebRTC/WHEP: `8889`
  - API: `9997`

> Para o app tocar streams diretamente, o engine precisa estar acessível no LAN (ver seção Streaming).

### 4.2 Health e status

- `GET /api/health` → `{ "status": "ok" }` (pública)
- `GET /api/auth/status` → status de autenticação (pública)

### 4.3 Composição (base do 3D)

- `GET /api/composition` → composição ativa
- `PUT /api/composition` → salva composição ativa (debounce no web host; útil no futuro para criação via RoomPlan)
- `GET /api/compositions` → lista e id ativo
- `POST /api/compositions` → cria composição
- `POST /api/compositions/{id}/activate` → ativa
- `PATCH /api/compositions/{id}` → renomeia
- `DELETE /api/compositions/{id}` → remove

### 4.4 Arquivos (modelos 3D, imagens, thumbs)

- `GET /api/files/exists?path=<rel>` → `{ exists: boolean }`
- `POST /api/files/upload` (multipart) → salva em `<data_dir>/files/<dir>/...`
- `GET /files/{path}` → baixa arquivo do usuário (protegido por auth)

Notas:

- `/files/*` responde com `Cache-Control: no-store` (o app precisa de cache próprio).
- A UI (extensões) usa `dir` + `filename` para formar URLs tipo `/files/<dir>/<file>`.

### 4.5 Eventos (entrada de ações externas)

O core expõe um endpoint genérico para emitir eventos no `EventBus`:

- `POST /api/events/{event_name}` body: `{ payload: any, context?: object }`

Restrição importante:

- existe uma allowlist por env `TOPOSYNC_AUTH_EVENT_ALLOWLIST`
- default: `device.action_requested,home_assistant.primary_action_requested,home_assistant.service_call`

Isto é **crítico** para o app:

- “toggle” genérico de HA é feito via `home_assistant.primary_action_requested`
- service calls arbitrárias via `home_assistant.service_call`

### 4.6 Notificações (lista + streams SSE)

- `GET /api/notifications?before=<seq>&limit=<n>` → `{ notifications: [...], next_cursor }`
- `GET /api/notifications/stream` → SSE com inserts/updates (feed global)
- `GET /api/notifications/{id}` → detalhe atual
- `GET /api/notifications/{id}/stream` → SSE filtrado por `id` (atualizações da notificação selecionada)

Formato do item público (resumo):

```json
{
  "id": "…",
  "type": "pipelines.…",
  "title": "…",
  "description": "…",
  "imageUrl": "/files/<rel_path>" ,
  "createdAt": "2026-02-26T…Z",
  "updatedAt": "2026-02-26T…Z",
  "payload": { }
}
```

### 4.7 Extensões instaladas (descoberta)

- `GET /api/extensions` → lista de extensões (manifesto público + `remote_entry_url`)
- `GET /extensions/<extension_id>/<path>` → assets estáticos da extensão (protegido)

---

## 5) Autenticação e autorização (todas as maneiras existentes)

### 5.1 Modos

O modo vem de `TOPOSYNC_AUTH_MODE`:

- `enforced` (default): auth obrigatória para APIs e assets protegidos.
- `bypass`: desliga auth (dev). Nesse modo:
  - o principal é sempre “owner” (`bypass`)
  - `POST /api/auth/login` e `POST /api/auth/setup` são desabilitados.

### 5.2 Estado “requires_setup”

Se não há usuários no auth store (`count_users() == 0`), `requires_setup=true` e:

- qualquer chamada em `/api/*`, `/files/*`, `/extensions/*` (exceto algumas) retorna **503** com `{"detail":"Auth setup is required"}`.
- rotas liberadas nesse estado:
  - `GET /api/health`
  - `GET /api/auth/status`
  - `POST /api/auth/setup`

### 5.3 Sessão: cookies `HttpOnly` (método principal)

Após login/setup/pairing, o backend seta cookies:

- access cookie: `toposync_at`
- refresh cookie: `toposync_rt`

Características:

- `HttpOnly=true`
- `SameSite=Lax`
- `Path=/`
- `Secure`:
  - `TOPOSYNC_AUTH_COOKIE_SECURE=auto` (default): Secure só em HTTPS (inclui `X-Forwarded-Proto: https`)
  - `true|false` para forçar.

TTLs (env configuráveis):

- access: `TOPOSYNC_AUTH_ACCESS_TTL_S` (default **30 min**)
- refresh: `TOPOSYNC_AUTH_REFRESH_TTL_S` (default **90 dias**)
- pairing code: `TOPOSYNC_AUTH_PAIRING_TTL_S` (default **5 min**)

Refresh rotation:

- toda vez que um refresh é usado para reautenticar, o token é **rotacionado** (revoga o anterior e emite outro).
- existe uma janela de graça para concorrência: `TOPOSYNC_AUTH_REFRESH_ROTATION_GRACE_S` (default **30s**).

Implicação para o app:

- se você usar um cookie jar, a rotação é transparente.
- se você persistir tokens manualmente (ex.: lendo `Set-Cookie`), você precisa **sempre atualizar** o refresh token quando ele rotacionar.

### 5.4 Bearer token (suportado)

Além de cookies, o core aceita:

`Authorization: Bearer <access_token>`

Observações:

- o access token **não é um JWT padrão**; é um blob assinado (payload JSON base64url + HMAC).
- trate como opaco. O backend valida expiração e invalida quando a senha do usuário muda (`pwd` marker).

### 5.5 Basic auth (escopo específico) — sync interna do Streaming distribuído

Existe Basic auth **apenas** para:

- prefixo: `/api/streams/distributed/settings/<server_id>`

Habilita quando as envs estão setadas:

- `TOPOSYNC_STREAMING_SYNC_USERNAME`
- `TOPOSYNC_STREAMING_SYNC_PASSWORD`

Esse mecanismo é pensado para comunicação service‑to‑service (processing → core) e **não** como auth geral do app.

### 5.6 Fluxos de autenticação (endpoints)

**Setup do owner (primeiro acesso)**

- `POST /api/auth/setup`
  - body: `{ username, password, display_name?, device_label? }`
  - resposta: `{ user: ... }` + `Set-Cookie` (sessão)

**Login por senha**

- `POST /api/auth/login`
  - body: `{ username, password, device_label? }`
  - resposta: `{ user: ... }` + `Set-Cookie`

**Logout**

- `POST /api/auth/logout`
  - revoga refresh token corrente (se existir cookie)
  - limpa cookies

**Pairing (ideal para mobile/TV)**

Objetivo: parear um novo device **sem digitar senha**, usando um código gerado em um device já autenticado.

1) em um device logado (web, por exemplo):
   - `POST /api/auth/pair/start` body `{ device_label? }`
   - resposta: `{ code, expires_at }`
2) no novo device (app):
   - `POST /api/auth/pair/complete` body `{ code, device_label? }`
   - resposta: `{ user }` + cookies de sessão

### 5.7 Autorização (roles, ações, grants)

O core aplica autorização por:

- **role defaults** (owner/admin/*, member/guest com um conjunto fixo)
- **grant rules** opcionais por usuário+ação+resource_type:
  - incluem/excluem seletores (`*`, `com.toposync.*`, `compId.areaId` etc.)
  - se existir grant para aquela ação+resource_type, ele **sobrepõe** o role default.

Roles e defaults relevantes:

- `owner`: `*` (tudo)
- `admin`: `*` (tudo; mas pode ser restringido por grant)
- `member` (importante para app “viewer/control”):
  - `core:extensions:list`, `core:extension:use`
  - `core:compositions:read`
  - `core:files:read`
  - `core:events:emit`
  - `core:devices:read`
  - `core:area:read`, `core:area:control`
  - `core:notifications:read`, `core:notifications:stream`
  - `core:auth:pair`
- `guest`: similar a member, mas sem `core:area:control` e sem `core:events:emit`? (ver defaults do runtime ao criar contas)

Extensões e auth:

- cada extensão pode declarar `capabilities.auth.api_prefixes` (ex.: `/api/cameras`)
- o core intercepta e aplica `core:extension:use` com `resource_selector=<extension_id>`

---

## 6) Modelos de dados essenciais (para render 3D, cache e sincronização)

### 6.1 Composição e elementos (contrato do 3D)

O modelo base vem do backend e é usado pelo frontend:

```ts
type Vector3 = { x: number; y: number; z: number };

type CompositionElement = {
  id: string;
  type: string;
  name: string;
  position: Vector3;   // metros; X/Z no chão; Y = altura
  rotation: Vector3;   // radianos (ThreeJS)
  props: Record<string, unknown>; // schema livre por tipo/extensão
};

type Composition = {
  id: string;
  name: string;
  elements: CompositionElement[];
};
```

Convenções importantes (observadas no host/editor):

- plano do chão é **X/Z** (Y é vertical).
- ferramentas 2D trabalham em coordenadas do “mundo” (X/Z).
- rotações são em **radianos** (ex.: rotação Y usada para orientar objetos no plano).
- unidades são em **metros** (paredes com largura ~0.12, altura default 2.7).

### 6.2 View settings (como o 3D é parametrizado hoje)

No web host, o usuário escolhe (persistido em `localStorage`):

- chave: `toposync.view.v1`
- campos: `{ wall_height_preset, ghost_walls, graphics_quality }`

`wall_height_preset` → altura em metros:

- `low`: 0.6
- `medium`: 1.4
- `high`: 2.7

No app, você pode:

- replicar a mesma ideia (para consistência visual);
- ou fixar um preset no MVP.

### 6.3 Notificações (persistência e paginação)

- Store é SQLite com `seq` auto‑increment (cursor).
- `GET /api/notifications` retorna `next_cursor = seq do último item`.
- use `before=<next_cursor>` para paginação “mais antigas”.

### 6.4 Streaming (Transmissions / Outputs)

O app vai consumir:

- `GET /api/streams/transmissions` → lista de `Transmission`
- `GET /api/streams/transmissions/{id}/urls` → URLs de playback por output/protocolo
- `POST /api/streams/transmissions/{id}/demand/prime` → aumenta confiabilidade do primeiro play

Estruturas (resumo):

```ts
type TransmissionOutput = {
  id: string;
  protocol: "hls" | "rtsp" | "webrtc";
  enabled?: boolean;
  resolution?: { width?: number; height?: number } | null;
  fps_limit?: number | null;
  bitrate_kbps?: number | null;
  latency_profile?: "normal" | "low" | "ultra_low";
  authentication?: { enabled?: boolean; username?: string | null; password?: string | null } | null;
};

type Transmission = {
  id: string;
  name: string;
  path: string;
  enabled?: boolean;
  host_server_id?: string; // "local" ou server id
  outputs: TransmissionOutput[];
};

type TransmissionUrlsResponse = {
  transmission_id: string;
  engine_running: boolean;
  outputs: Array<{
    output_id: string;
    protocol: "hls" | "rtsp" | "webrtc";
    resolved_engine_path: string;
    url: string;
    requires_auth?: boolean;
    auth_username?: string | null;
  }>;
  warnings?: string[];
};
```

---

## 7) 3D hoje: elementos first‑party e como interpretá‑los no app

> O app nativo não vai carregar `remoteEntry.js`. Para renderizar instantâneo, você vai “re‑implementar” um subconjunto dos element types (ou embutir uma versão do host via WebView). Esta seção lista os tipos e seus `props` atuais.

### 7.1 `extensions/structural` (paredes/áreas/piscina)

**IDs**

- parede: `com.toposync.structural.wall`
- área: `com.toposync.structural.area`
- piscina: `com.toposync.structural.pool`

**Wall props (essencial)**

```json
{
  "a": { "x": 0.0, "z": 0.0 },
  "b": { "x": 2.0, "z": 0.0 },
  "width": 0.12,
  "color": "#dcddda",
  "openings": [
    { "id": "…", "kind": "door|window|opening", "center_m": 1.0, "width_m": 0.9, "y_min_m": 0, "y_max_m": 2.1 }
  ]
}
```

Notas:

- a altura do wall vem do `view.wallHeight` (preset).
- `openings` são recortes/cutouts; para um MVP mobile você pode:
  - ignorar openings (render parede sólida), ou
  - implementar apenas doors/windows simples.

**Area props**

```json
{
  "vertices": [ { "x": -1, "z": -1 }, { "x": 1, "z": -1 }, { "x": 1, "z": 1 } ],
  "fill": "#dcddda",
  "opacity": 1.0,
  "texture": "none"
}
```

**Pool props**

```json
{
  "depth_m": 1.4,
  "vertices": [ { "x": -1, "z": -1 }, { "x": 1, "z": -1 }, { "x": 1, "z": 1 } ]
}
```

### 7.2 `extensions/models` (GLB/GLTF como elemento)

**ID**

- `com.toposync.models.gltf`

**Props**

```json
{
  "dir": "abc123",
  "model": "chair.glb",
  "preview": "preview.png",
  "size": { "x": 1, "y": 1, "z": 1 },
  "center": { "x": 0, "y": 0, "z": 0 },
  "min_y": 0,
  "scale": 1
}
```

Semântica:

- `dir` + `model` → arquivo em `/files/<dir>/<model>`.
- `size/center/min_y` são metadados calculados ao importar (alinhamento no chão).
- `scale` é aplicado no `Group.scale`.

Implicação para mobile:

- prefira `.glb` (um arquivo) para cache robusto.
- `.gltf` pode ter dependências (textures/bin) no mesmo `dir`. Seu loader precisa resolver URLs relativas.

### 7.3 `extensions/images` (imagens no chão)

**ID**

- `com.toposync.images.image`

**Props**

```json
{
  "dir": "abc123",
  "file": "floorplan.png",
  "width_m": 4,
  "depth_m": 3,
  "opacity": 0.55,
  "mode": "overlay|tracing",
  "blend": "normal|multiply",
  "pixel_width": 2048,
  "pixel_height": 1536
}
```

Render 3D:

- só aparece quando `mode="overlay"`.
- é um `PlaneGeometry` no chão com leve offset em Y (`0.012`).

### 7.4 `extensions/cameras` (câmera no 3D + snapshots + mapping)

**Elemento**

- type: `com.toposync.cameras.camera`
- props base:

```json
{
  "camera_id": "front_gate",
  "camera_name": "Portão",
  "view_mode": "ceiling",
  "control_point_sets": [ /* opcional; usado para mapping */ ]
}
```

APIs úteis para o app:

- `GET /api/cameras/index` → lista `{ id, name, connection_type }`
- `GET /api/cameras/cameras/{camera_id}/snapshot` → JPEG
- `POST /api/cameras/rtsp/snapshot` → JPEG (URL RTSP avulsa)
- `POST /api/cameras/control_points/map` → mapeia ponto imagem ↔ mundo a partir de um `control_point_set`
- `GET /api/cameras/cameras/{camera_id}/contexts` → onde essa câmera aparece (composições/áreas) e se tem mapping suficiente

### 7.5 `extensions/home_assistant` (entidades/dispositivos no 3D + estados)

**Elemento**

- type: `com.toposync.home_assistant.item`
- props base (resumo):

```json
{
  "server_id": "ha-main",
  "items": [ { "kind": "entity", "id": "light.kitchen", "name": "Cozinha", "domain": "light" } ],
  "icon": "lightbulb",
  "primary_entity_id": "light.kitchen",
  "primary_state": "off",
  "view_mode": "floor|wall|ceiling",
  "special_view": "none|lamp|airflow|model|ceiling_fan",
  "lamp_intensity": 1.0,
  "lamp_color": "#ffe8b0",
  "airflow_intensity": 1.0,
  "airflow_width": 0.72,
  "airflow_mount_y": null,
  "model3d": null
}
```

Se `special_view="model"`, `model3d` é um blob com o **mesmo schema** do Models (dir/model/size/scale etc.), e a animação do GLTF é ativada quando a entidade está ligada.

---

## 8) Integração Home Assistant (backend → HA, app → Toposync)

### 8.1 Configuração

O usuário configura HA no modal global (settings da extensão):

- `settings.extensions["com.toposync.home_assistant"].servers = [{ id, name, host, apiKey }]`

O app **não precisa** (nem consegue) ler o `apiKey` via API pública; ele usa o Toposync como proxy.

### 8.2 APIs expostas pela extensão

- `GET /api/home_assistant/servers`
  - retorna `[{ id, name, host }]` (sem token)
- `GET /api/home_assistant/{server_id}/registry`
  - retorna entidades/dispositivos/relacionamentos (via websocket do HA)
- `POST /api/home_assistant/{server_id}/states`
  - body: `{ entity_ids: string[] }`
  - retorna mapa `entity_id -> state raw`
- `GET /api/home_assistant/{server_id}/stream?entity_ids=a,b,c`
  - SSE com:
    - `event: snapshot` (mapa inicial)
    - `event: state_changed` (updates pontuais)
    - pings (`: ping`)

### 8.3 Ações (controle) via EventBus

O controle é feito emitindo eventos no core:

1) Toggle “primário” (domínios compatíveis):

- `POST /api/events/home_assistant.primary_action_requested`
  - payload: `{ server_id, entity_id }`
  - resultado (best effort): `{ entity_id, state, raw }`

2) Service call arbitrária:

- `POST /api/events/home_assistant.service_call`
  - payload: `{ server_id, domain, service, data }`

Implicação para o “outbox” do app:

- **prefira service calls idempotentes** (`turn_on`/`turn_off`) ao invés de “toggle”.

---

## 9) Streaming (básico + o que importa para o app)

### 9.1 O que é

A extensão `com.toposync.streaming` fornece “pipeline‑rendered streaming”:

- pipelines escrevem frames via operador `stream.write`;
- o runtime decide qual writer está ativo (multi-writer arbitration);
- um engine local **MediaMTX** serve RTSP/HLS/WebRTC (WHEP);
- FFmpeg publishers (1 por output) publicam o vídeo no MediaMTX;
- encoding é **on-demand** (só roda quando há viewer).

### 9.2 Protocolos (para o app)

- **HLS**: o caminho mais simples para `expo-video` + TV + PiP.
- **WebRTC/WHEP**: baixa latência, mas exige implementação WebRTC + signaling HTTP WHEP (mais complexo no RN).
- **RTSP**: mais para ferramentas externas (VLC/NVR).

### 9.3 Engine acessível no LAN (requisito prático)

Por default, o MediaMTX fica bound a `127.0.0.1` (safe-by-default). Para o app em outro device tocar streams:

- habilite `engine.expose_to_lan = true` (bind `0.0.0.0`)
- garanta que as portas do engine estão acessíveis na rede.

### 9.4 URLs e “prime” (primeiro play confiável)

Fluxo recomendado:

1) `GET /api/streams/transmissions/{id}/urls` (descobrir HLS/WebRTC URLs)
2) `POST /api/streams/transmissions/{id}/demand/prime`
3) iniciar playback (com retry/backoff)

### 9.5 Auth no playback (pitfall em mobile)

Cada `TransmissionOutput` pode ter `authentication` (Basic) aplicada no MediaMTX.

No web dashboard:

- WebRTC/WHEP usa header `Authorization: Basic …`
- HLS pode exigir `username:password@` embutido na URL (depende do player)

No mobile (`expo-video`):

- players nativos frequentemente **não permitem** headers custom por segmento HLS.
- portanto, para MVP, o mais prático é:
  - **não habilitar auth por output** (confiar na segurança do LAN) **ou**
  - criar futuramente um **proxy autenticado** (Toposync → MediaMTX) que injeta headers.

### 9.6 Observabilidade útil

Para debug:

- `GET /api/streams/engine/status`
- `GET /api/streams/runtime/outputs`
- `GET /api/streams/runtime/diagnostics`

---

## 10) Pipelines (DAG) — visão para futuro (e impacto indireto hoje)

Mesmo que o app não exponha pipelines agora, elas impactam:

- **notificações** (muitas são geradas por pipelines)
- **streaming** (frames vêm de pipelines)
- **processamento remoto** (processing servers)

Conceitos:

- pipeline é um DAG de operadores; tipos: `reuse` (subgrafo) e `final` (executável).
- runtime trafega `Packet` com `lifecycle: open|update|close`, `payload` e `artifacts` (frames/imagens vão em artifacts).
- execução pode ser local ou remota (processing server).

APIs principais:

- `GET /api/pipelines`
- `GET /api/pipelines/operators`
- `POST /api/pipelines/compile` / `compile-python`
- CRUD `/api/pipelines/{name}`
- runtime status/reload

---

## 11) Blueprint do app Expo (LAN-first, instantâneo, cacheado)

Esta seção traduz o que existe hoje em **decisões práticas** do app.

### 11.1 Connection manager (LAN-first + compatível com cloud)

**Requisito do Toposync hoje:** o app precisa de um `baseUrl` que aponte para o backend (ex.: `http://192.168.0.10:8000`).

Sugestão de estratégia (compatível com futuro cloud):

1) **Last known**:
   - tente o último `baseUrl` bem-sucedido (timeout curto).
2) **Descoberta paralela (mDNS/Bonjour)**:
   - se você padronizar um serviço (ex.: `_toposync._tcp`), descubra IPs na rede.
   - atenção: iOS exige permissões de “Local Network” e configuração de serviços Bonjour.
3) **Fallback cloud (futuro)**:
   - se estiver logado em cloud, resolva o tunnel/relay e use `https://…`.

Importante:

- Auth por cookie é **sensível ao host** (IP/hostname). Se o IP mudar, os cookies não migram.
- Por isso, mDNS + hostname estável ajuda muito (ex.: `toposync.local`).

### 11.2 Autenticação no app (recomendação prática)

**Opção A (mais simples): cookie jar**

- use cookies (`Set-Cookie`) como sessão principal.
- garante refresh rotation automático.

**Opção B: Bearer token**

- o backend aceita `Authorization: Bearer …`, mas o login atual não retorna token no body, só em cookie.
- você teria de capturar `Set-Cookie` e extrair `toposync_at`, e também lidar com rotação (ou trocar o backend para expor tokens explicitamente).

**Pairing é a UX ideal para TV/mobile:**

- o usuário faz login no web (owner/admin), gera código de pairing e digita no app.

### 11.3 Snapshot local (offline-first) — o que cachear

Para “abrir instantâneo”:

- `composition` (JSON) + `activeCompositionId`
- `viewSettings` (wall height preset etc.)
- `home assistant`:
  - lista de servers públicos (`/api/home_assistant/servers`)
  - cache de `registry` por server (para nome/ícone/domínio)
  - últimos states das entidades relevantes
- `streams`:
  - lista de transmissions
  - preferências do usuário (grid 1x1/2x2)
- `notifications`:
  - últimas N notificações (lista)
  - última notificação selecionada (id)
- `assets`:
  - modelos GLB/GLTF usados na composição
  - texturas/imagens do Images
  - thumbs/previews (`preview.png`)
  - (opcional) último snapshot JPEG das câmeras visíveis

Estratégia SWR:

1) renderize do cache imediatamente;
2) em paralelo:
   - `GET /api/auth/status` (detecta sessão/needs setup)
   - `GET /api/composition` + `GET /api/compositions`
   - inicie SSE (notificações, HA states)
   - atualize o snapshot local quando os dados chegarem.

### 11.4 Streams SSE no mobile (notificações e HA)

No RN/Expo, `EventSource` pode exigir polyfill:

- Notificações:
  - `/api/notifications/stream` → `onmessage` com JSON `{ op, notification }`
  - `/api/notifications/{id}/stream` → idem, filtrado
- Home Assistant:
  - `/api/home_assistant/{server}/stream?entity_ids=...`
  - eventos nomeados `snapshot` e `state_changed`

### 11.5 Outbox de comandos (pendente → retry)

Ponto crítico com o backend atual:

- “toggle” não é idempotente (pode duplicar ao retry).

Recomendação para o app:

- modele comandos como “**set**” sempre que possível:
  - `home_assistant.service_call` com `turn_on/turn_off`, `lock/unlock`, `cover.open_cover/close_cover`, `climate.turn_on/turn_off`, etc.
- use “toggle” (`primary_action_requested`) só quando:
  - o app está online e consegue confirmar estado com prioridade.

### 11.6 Render 3D nativo (MVP realista)

Para “render inicial instantâneo”, um MVP viável:

- implementar renderers para:
  - Structural (walls/areas/pool)
  - Models (GLB)
  - Home Assistant item (pelo menos ícone + cor de estado; lamp/airflow/model como incrementos)
  - Cameras (ícone + direção; snapshot como textura depois)
  - Images overlay (plano no chão)
- interações:
  - tap em HA item → `POST /api/events/home_assistant.primary_action_requested` (ou service_call set)
  - tap em câmera → abrir snapshot ou stream transmission associada (se existir no seu UX)
- atualizar estado em background:
  - HA live stream para entidades na cena
  - notificações SSE para overlay/lista

### 11.7 RoomPlan → criação de composição (quando chegar)

Você já tem APIs para:

- `POST /api/compositions` (criar)
- `POST /api/compositions/{id}/activate` (ativar)
- `PUT /api/composition` (salvar elementos)

Mapeamento sugerido (MVP):

- paredes → `com.toposync.structural.wall` com props `a/b/width/openings`
- áreas do chão → `com.toposync.structural.area` com `vertices`
- portas/janelas → virar `openings` no wall (kind `door/window`)

### 11.8 WebView fallback (para extensões/visualizações não suportadas)

No MVP, você pode ter uma visualização “Web (fallback)” que carrega a UI original:

- produção: `http://<host>:8000/` (SPA)
- dev: `http://<host>:5173/`

Cuidados:

- compartilhamento de sessão entre requests nativos e WebView (cookies) pode exigir integração específica.
- se o app usar HTTPS no futuro cloud, a UI web também deve estar em HTTPS (cookies Secure).

### 11.9 Router, deep links e navegação (mobile/tablet/TV)

O Toposync web atual é uma SPA sem um “roteador de URLs” público forte (muita navegação é por estado interno). Para o app, vale tratar roteamento como **contrato de produto** desde o início, mesmo que várias telas ainda sejam MVP.

Requisitos práticos do seu roadmap:

- deep links (abrir em “Streams”, “Notificações”, “3D”, “Web fallback”)
- navegação por controle remoto (TV) + foco previsível
- transições consistentes (mobile/tablet/TV)
- “resumo instantâneo” (abrir já no último estado/view, offline)

Sugestão de abordagem (independente de lib):

- defina uma enum de **Views** persistível:
  - `home3d`, `streams`, `notifications`, `settings`, `web_fallback`, `roomplan_create` (futuro)
- persista `lastView` + parâmetros (ex.: `activeCompositionId`, `selectedTransmissionId`, `selectedNotificationId`, `webFallbackUrl`)
- modele deep links como ações:
  - `toposync://open/streams?transmissionId=...`
  - `toposync://open/notifications?id=...`

Ponto de integração com o backend:

- um push futuro pode carregar só `notificationId` e o app resolve o resto via:
  - cache local instantâneo → abre UI
  - depois `GET /api/notifications/{id}` + SSE detalhe → atualiza

### 11.10 UI minimalista (barra inferior) e consistência com o que já existe

Coisas que o web host já faz e que o app pode reaproveitar como “contrato mental”:

- View settings do 3D (wall height preset, ghost walls, quality) já têm chave `toposync.view.v1`.
- Dashboard de streams já usa conceito de **grid mode** (1x1/2x2) com chave `toposync.streams.grid_mode.v1`.

No app, você pode:

- tratar a barra inferior como “switcher” de views:
  - `3D` / `Streams` / `Notificações` / `Config`
- esconder por inatividade (como o dashboard web faz), mas mantendo acessível por toque/tecla.

### 11.11 “2D capturado do 3D” (futuro de performance)

Nada disso existe no backend; é uma otimização de UI/render.

Estratégia que combina com offline‑first:

- renderize o 3D “de verdade” na entrada (instantâneo a partir do cache local).
- após estabilizar (ex.: alguns frames / ou quando não há interação), capture uma imagem (snapshot) e:
  - troque para um modo 2D (imagem) quando idle, reduzindo consumo.
- ao detectar interação (toque/controle remoto), volte para 3D.

Isso preserva a promessa “sempre pronto” sem manter GPU/CPU no talo em TV.

### 11.12 AR (futuro) e compatibilidade com o modelo atual

O modelo atual (composição em metros no plano X/Z) é compatível com AR, desde que você defina:

- um **anchor**/origem no mundo real (ex.: canto da sala / marker / alinhamento do RoomPlan)
- um mapeamento de escala (1 unidade = 1 metro)

Integração com o que já existe:

- RoomPlan pode gerar a base estrutural (paredes/aberturas/áreas) que você salva como elementos Structural.
- o modo AR pode renderizar **o mesmo conjunto de elementos** (Structural/Models/HA), só mudando câmera/controles.

### 11.13 Push notifications via cloud (futuro) — o que o backend atual implica

Hoje, notificações são locais (SQLite) e chegam ao cliente via SSE.

Para push via cloud (Firebase/APNs), as limitações do modelo atual são:

- `imageUrl` aponta para `/files/...` (host local, protegido por auth) → **não é acessível externamente**.
- para push com imagem, você vai precisar de um caminho cloud que:
  - receba o evento/thumbnail do origin (ou gere uma thumb pequena, como você descreveu)
  - publique em um storage com TTL curto (ex.: 8–24h)
  - envie no push uma URL HTTPS pública (e expira)

Compatibilidade recomendada:

- push contém apenas metadados mínimos:
  - `instanceId` (ou “cloud account id”)
  - `notificationId`
  - (opcional) `title/type/createdAt`
  - (opcional) `thumbnailUrl` (pago)
- ao abrir, o app:
  - renderiza do cache local
  - reconecta (LAN ou cloud) e faz `GET /api/notifications/{id}` + SSE

---

## 12) Apêndice — exemplos rápidos (snippets)

### 12.1 Setup + login (curl)

```bash
# status (público)
curl -s http://localhost:8000/api/auth/status

# setup owner (1ª vez)
curl -i -X POST http://localhost:8000/api/auth/setup \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"senha-super-secreta","display_name":"Admin","device_label":"bootstrap"}'
```

### 12.2 Pairing (mobile)

```bash
# 1) em um device autenticado (envie cookies):
curl -i -X POST http://localhost:8000/api/auth/pair/start \
  -H 'content-type: application/json' \
  -d '{"device_label":"tv"}'

# 2) no app:
curl -i -X POST http://localhost:8000/api/auth/pair/complete \
  -H 'content-type: application/json' \
  -d '{"code":"ABCD1234","device_label":"my-iphone"}'
```

### 12.3 Notificações (SSE)

```bash
curl -N http://localhost:8000/api/notifications/stream
```

### 12.4 Home Assistant: registry + stream de states

```bash
curl -s http://localhost:8000/api/home_assistant/servers
curl -s http://localhost:8000/api/home_assistant/ha-main/registry

curl -N "http://localhost:8000/api/home_assistant/ha-main/stream?entity_ids=light.kitchen,climate.living_room"
```

### 12.5 Controlar Home Assistant via Toposync (event emit)

```bash
curl -s -X POST http://localhost:8000/api/events/home_assistant.service_call \
  -H 'content-type: application/json' \
  -d '{"payload":{"server_id":"ha-main","domain":"light","service":"turn_on","data":{"entity_id":"light.kitchen"}}}'
```

### 12.6 Streaming: pegar URL HLS e primar demanda

```bash
curl -s http://localhost:8000/api/streams/transmissions/<id>/urls
curl -s -X POST http://localhost:8000/api/streams/transmissions/<id>/demand/prime
```

---

## 13) Checklist de pontos que impactam diretamente o desenvolvimento do app

- Auth:
  - cookies `HttpOnly` + refresh rotation (melhor com cookie jar)
  - pairing code é o fluxo mais “app‑friendly”
  - Bearer existe, mas login não entrega token no body
- LAN:
  - IP variável quebra cookies; hostname estável (mDNS) ajuda muito
  - iOS precisa permissões/ATS para HTTP local
- Streaming:
  - engine tem portas próprias e pode estar bound a localhost
  - HLS é o caminho do MVP em RN/TV/PiP
  - auth por output pode ser difícil em players nativos
- 3D:
  - composição é em metros, plano X/Z
  - element types são extensíveis; app nativo precisa suportar subset + fallback WebView
- Offline-first:
  - `/files/*` é `no-store`; cache é responsabilidade do app
  - SSE para notificações e HA melhora “sempre pronto” com SWR
- Outbox:
  - toggle não é idempotente; prefira service calls “set”
