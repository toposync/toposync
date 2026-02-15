# Pipelines (DAG) — visão geral e uso

Este documento descreve o sistema de **Pipelines globais** do Toposync: modelo de dados, schema do graph, runtime (asyncio + backpressure/drop), operadores, execução local/remota, UI e recomendações de configuração.

> Status: Pipelines substituíram o legado de “detections” das câmeras. Ainda existem itens planejados (ver “Roadmap” no final).

---

## 1) O que é um Pipeline

Um **Pipeline** é uma entidade global da plataforma que define um **DAG** (grafo acíclico dirigido) de operadores.

- **`type=reuse`**: pipeline parcial (subgrafo reutilizável). Não é executado automaticamente.
- **`type=final`**: pipeline executável. Quando `enabled=true`, o orquestrador inicia o runtime (local ou remoto).

Cada pipeline tem:
- **`name`**: obrigatório, único e **compatível com identificador Python** (ex.: `carro_na_frente`, `alertaPortao` não).
- **`processing_server_id`**: onde executar o pipeline final (`local` por padrão).
- **`graph`**: JSON com `schema_version` + `nodes` + `edges`.
- **`editor_mode`**: `interactive|json|python` (Python é “one-way”; ver UI).

Implementação: `src/toposync/runtime/config_store.py` (`Pipeline`, validações e CRUD).

---

## 2) Modelo de runtime: `Packet`, `Artifact` e lifecycle

O runtime trafega **Packets** (eventos) entre operadores. Um Packet carrega:

- **`stream_id`**: identifica um fluxo lógico (ex.: uma câmera, ou um objeto trackeado).
- **`lifecycle`**: `open | update | close`
  - `open`: início de um stream/evento (ex.: “pessoa detectada”)
  - `update`: atualização do stream (ex.: bbox mudou / posição no mundo mudou)
  - `close`: encerramento (ex.: objeto perdido / evento finalizado)
- **`payload`**: dicionário com dados “baratos” (sem blobs grandes).
- **`artifacts`**: dicionário `name -> Artifact` (imagens/derivados), que podem estar:
  - **em memória** (`Artifact.data`) para pós-processamento; e/ou
  - **persistidos** (`Artifact.reference`) após `core.store_images`.
- **`metadata`**: metadados auxiliares (debug/observabilidade/trace).
- **`parent_packet_id`**: cadeia causal (útil em bifurcações/split).

Implementação: `src/toposync/runtime/pipelines/runtime.py` (`Packet`, `Artifact`, `Lifecycle`).

### Split (2 pessoas simultâneas)

Operadores como `vision.object_tracking_yolo` **dividem o stream**: de um `stream_id` da câmera surgem múltiplos `stream_id` (um por objeto). Isso permite:
- manter estado por objeto (debounce/throttle/best-frame) sem “misturar pessoas”
- backpressure/drop com previsibilidade (por stream) em etapas posteriores

Exemplo real:
- Entrada: `camera:9bb5...` (1 stream)
- Saída: `obj:camera:9bb5...:1`, `obj:camera:9bb5...:2` (2 streams independentes)

---

## 3) Backpressure e Drop (requisito: **tudo bounded**)

Toda borda do grafo (`edge`) tem uma fila **bounded** (`maxsize`) e uma política explícita de drop (`drop_policy`).

Implementação: `src/toposync/runtime/pipelines/runtime.py` (`BoundedChannel`, `DropPolicy`, métricas).

### `DropPolicy`

- `block`: bloqueia até haver espaço (útil para fluxos não-realtime).
- `drop_oldest`: descarta o mais antigo para aceitar o novo (**bom default realtime**).
- `drop_newest`: descarta o item novo quando a fila está cheia.
- `latest_only`: “latest wins”: ao encher, limpa a fila e mantém só o item mais recente (**ideal para frames brutos**).

### Regras práticas (realtime)

- **Frames brutos / entrada do YOLO**: `maxsize=1` + `latest_only`
  - mantém baixa latência e evita backlog infinito.
- **Depois de split por objeto** (`split_stream`): evite `maxsize=1 latest_only`
  - senão você perde updates de alguns objetos sob carga.
  - prefira `maxsize` maior + `drop_oldest` (ou estratégia keyed quando existir).

O compilador/recomendador emite alertas para casos ruins (ver “Recomendações”).

---

## 4) Graph JSON: schema e compilação

### Schema (persistido no `Pipeline.graph`)

Formato atual (v1):

```json
{
  "schema_version": 1,
  "nodes": [
    { "id": "source", "operator": "camera.source", "config": { "camera_id": "..." } }
  ],
  "edges": [
    {
      "from": { "node": "source", "port": "out" },
      "to":   { "node": "motion", "port": "in" },
      "maxsize": 1,
      "drop_policy": "latest_only"
    }
  ]
}
```

Implementação/validação: `src/toposync/runtime/pipelines/compiler.py` (`PipelineGraphSpec`).

### Compilação (normalize + validação + assinatura)

Ao compilar, o sistema:
- valida DAG (sem ciclos)
- valida portas (inputs/outputs declarados por operador)
- normaliza config via Pydantic (defaults + validação)
- calcula **`signature`** por nó (hash do operador + config normalizada + assinaturas upstream)
- marca nó como **`shareable`** quando `share_strategy="by_signature"`

Endpoint: `POST /api/pipelines/compile` (retorna `topological_order`, `nodes` compilados e `edges` compiladas).

Implementação: `src/toposync/runtime/pipelines/compiler.py`.

---

## 5) Sharing de computação (YOLO 1x) na prática

### Por que funciona

Nós “pesados” (ex.: YOLO) têm `share_strategy="by_signature"`. Se dois pipelines finais têm o **mesmo subgrafo** (mesmo operador+config+upstream), o compilador gera a mesma assinatura e o runtime pode executar **uma única instância** alimentando múltiplos consumidores.

### Como é executado localmente

Quando há múltiplos pipelines finais locais habilitados, o orquestrador tenta:
- compilar todos em conjunto;
- montar um **`PipelineBundleRuntime`** (um DAG “merged”);
- compartilhar nós `shareable` por assinatura;
- manter nós `share_strategy="never"` isolados.

Implementação:
- merge: `src/toposync/runtime/pipelines/shared_runtime.py`
- orquestração: `src/toposync/runtime/pipelines/distributed/orchestrator.py`

Observação importante: para um edge compartilhado ser unificado, `maxsize` e `drop_policy` precisam ser compatíveis; senão o bundle falha e o orquestrador cai para runtimes separados.

---

## 6) Execução distribuída (origin vs processing)

Um pipeline final pode rodar em um **processing server** remoto, mas:
- **Storage de imagens** e **registro de notificações** ocorrem na **origem** (origin).

### Como o corte é feito hoje

- Operadores com capability `origin_only` rodam na origem.
- O restante roda no processing server.
- A comunicação processing → origin usa um “inbox” + operadores `dist.*`.

Implementação:
- planejamento do split: `src/toposync/runtime/pipelines/distributed/plan.py` (`build_distributed_graphs`)
- runtime no origin: `src/toposync/runtime/pipelines/distributed/orchestrator.py`
- processing server: `src/toposync/runtime/pipelines/distributed/processing_server.py`
- transport HTTP: `src/toposync/runtime/pipelines/distributed/transport.py`

Limitação atual: **não há edges origin → processing** (o split suportado é processing → origin).

### Processing servers (CRUD)

Entidade `ProcessingServer`:
- `id` (regex `^[a-z][a-z0-9_-]{0,63}$`; `local` é reservado)
- `kind`: `inprocess|http`
- `url`, `username`, `password` (opcionais)

API:
- `GET /api/processing-servers`
- `PUT /api/processing-servers/{id}`
- `DELETE /api/processing-servers/{id}`
- `GET /api/processing-servers/{id}/status`

---

## 7) Operadores: contrato, registro e lista (SDK)

Um operador declara:
- `id` (ex.: `camera.source`, `core.notify`)
- `inputs` e `outputs` (ports)
- `config_model` (Pydantic) + `defaults` + JSON schema
- `capabilities` (ex.: `split_stream`, `origin_only`, `heavy_compute`)
- `share_strategy` (`by_signature` ou `never`)

Registro via `OperatorRegistry`, exposto como service:
- `pipelines.register_operator`
- `pipelines.list_operators`

Core do registry: `src/toposync/runtime/pipelines/operator_registry.py`

Lista via API: `GET /api/pipelines/operators`

### Operadores disponíveis (IDs)

Core:
- `core.passthrough`, `core.sink`
- `core.throttle`, `core.debounce`, `core.fps_reducer`
- `core.lifecycle_from_boolean`
- `core.debug`
- `core.store_images`, `core.notify`
- `core.synthetic_source`, `core.demo_frame_sequence_source`
- `core.schedule_gate`, `core.category_gate`

Distribuído:
- `dist.remote_source`, `dist.target_filter`, `dist.project_to_origin`

Extensão `com.toposync.cameras`:
- `camera.source`, `camera.motion_gate`
- `vision.object_tracking_yolo`, `vision.object_detection_yolo`
- `camera.object_segmentation`, `camera.image_resize`
- `camera.camera_mapping`, `camera.area_restriction`, `camera.velocity_estimation`
- `camera.best_frame_selector`

---

## 8) Operadores essenciais (comportamento atual)

### `camera.source` (latest frame wins)

- Função: captura RTSP e emite `Packet` com `payload.frame` e metadados básicos.
- Stream: `stream_id = "camera:<camera_id>"`
- Portas:
  - input opcional: `gate` (permite pausar leitura RTSP)
  - output: `out`
- Backpressure: idealmente `maxsize=1 latest_only` logo após a source.

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py` (`CameraSourceRuntime`, `FrameGrabber`).

### Gates “antes da câmera”

Conecte `core.schedule_gate.out -> camera.source.gate`.
Quando o gate fecha, o `camera.source` **para de ler RTSP** (e fecha o grabber), preservando CPU/I/O.

Implementação: `src/toposync/runtime/pipelines/operators_gates.py` + `camera.source`.

### `camera.motion_gate` (não encerra evento)

- Função: anota motion no payload e filtra frames quando idle (por padrão).
- Importante: não emite `close`/encerramento de tracking. É apenas um gate de frames.

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py` (`MotionGateRuntime`).

### `vision.object_tracking_yolo` (split por objeto)

Emite um stream por objeto (`obj:...`) com lifecycle.

Payload adicionado/normalizado (campos principais):
- `tracking_id` (sempre preenchido; pode ser sintético)
- `tracker_track_id` (id nativo do tracker; pode ser `None`)
- `correlation_id` (uuid por track)
- `object_category_label`, `object_confidence`, `object_bbox01`
- `source_stream_id` (stream original)
- `detected_object` (objeto completo)

Config defaults importantes:
- `confidence_threshold=0.4`, `iou_threshold=0.6`
- `default_interval_seconds=0.2` (evita updates sem limite)
- `close_after_seconds=4.0` (evita “flicker” sob oclusões/drops)

Observação: se o tracker não fornecer `tracking_id`, o runtime usa matching por IoU + ids sintéticos para manter continuidade.

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`.

### `vision.object_detection_yolo` (evento pontual)

Detecção “sem tracking”:
- emite `open` e `close` por detecção (stream `det:<source>:<correlation_id>`)
- suporta throttling por categoria (intervalos)

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`.

### Pós-processamento e artefatos

- `camera.object_segmentation`: recorte por bbox (gera artifact, default `segmented`)
- `camera.image_resize`: redimensiona artifacts em memória (reduce payload/storage)
- `camera.best_frame_selector`: buffer bounded por track para escolher melhor frame (default `best_frame`)

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py`.

### Mapeamento, áreas e velocidade

Pipeline “feliz” para velocidade/área:
`... -> camera.camera_mapping -> camera.velocity_estimation -> camera.area_restriction -> ...`

- `camera.camera_mapping`: projeta `image_uv`/bbox para `world` (x,z) via control points.
- `camera.area_restriction`: filtra ou anota labels de áreas do mundo.
- `camera.velocity_estimation`: calcula velocidade em m/s e km/h; pode filtrar (ex.: `stopped_now`).

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py`.

---

## 9) Storage e Notificações (sinks na origem)

### `core.store_images` (local, naming rico)

Persistência local (por enquanto):
- escreve arquivos em `files/` e coloca o caminho relativo em `Artifact.reference`
- **não** depende de câmera/tracking id “field configurável”: resolve por payload/metadata quando possível
- converte BGR→RGB ao codificar PNG/JPEG (OpenCV friendly)

Implementação: `src/toposync/runtime/pipelines/operators_sinks.py`.

### `core.notify` (open/update/close, sem spam)

Registra notificações no runtime atual (compatível com SSE/UI):
- suporta `priority=low|medium|high`
- templates Mustache-like: `{{object_category_label}}`, `{{area_label}}`, etc.
- seleção de thumbnail: escolhe **entre artifacts já armazenados** (notify não armazena nada)
- dedupe por default: `node_id + camera_id + correlation_id/tracking_id/stream_id`
- rate-limit de updates por `update_interval_seconds`
- evita spam por assinatura (se título/desc/imagem/lifecycle não mudaram, não emite)

Implementação: `src/toposync/runtime/pipelines/operators_sinks.py`.

UI/renderer:
- `frontend/src/ui/notifications/pipelinesNotifications.tsx` (fallback de thumbnail e chips)
- `priority=low` é ocultável por padrão na UI (ver tela de notificações)

---

## 10) Recomendações (“alerts”) para UX

Ao compilar um pipeline, o backend analisa o grafo compilado e devolve `alerts` para guiar o usuário:
- notify sem store_images antes
- thumbnail fallback que não corresponde a artifacts armazenados
- velocidade sem mapping upstream
- velocidade depois de throttle/debounce/fps_reducer
- store_images antes de pós-processamento
- best_frame_selector “inútil” (nada downstream usa)
- canais ruins depois de split (ex.: `maxsize=1 latest_only`)

Implementação: `src/toposync/runtime/pipelines/recommendations.py`

---

## 11) UI (Editor de Pipelines)

Tela fullscreen de pipelines:
- lista de pipelines (como “arquivos”)
- criar/editar/remover
- selecionar `type` e `processing_server`
- modos de edição:
  - **Interactive**: lista reordenável de steps, com forms (selects etc.)
  - **JSON**: edição direta do graph
  - **Python (one-way)**: texto livre armazenado; após salvar em Python, não volta para Interactive/JSON

Frontend:
- tela: `frontend/src/ui/screens/PipelinesScreen.tsx`
- editor interativo: `frontend/src/ui/screens/pipelines/InteractivePipelineEditor.tsx`

Importante: hoje o runtime executa o **graph JSON**; `python_source` é armazenado para flexibilidade e futura compilação (ver Roadmap).

---

## 12) Observabilidade e debug

Status do runtime:
- `GET /api/pipelines/runtime/status` (inclui snapshot de filas e métricas por nó)
- `POST /api/pipelines/runtime/reload` (reconcile imediato)

Debug step:
- `core.debug` imprime o Packet (payload/metadata/artifacts sanitizados) no stdout
- opcionalmente salva imagens do payload/artifacts em diretório temporário do sistema

Implementação: `src/toposync/runtime/pipelines/operators_core.py` (`core.debug`).

---

## 13) Roadmap (próximas etapas sugeridas)

Conforme `current-plan.ignore.md`, ainda faltam/estão parciais:

1) **Backends de captura plugáveis** (Etapa 15) ✅  
   `camera.source` agora suporta `backend=auto|opencv|ffmpeg` com fallback automático, “latest frame wins” e métricas no payload.

2) **`core.filter` genérico (predicado determinístico)** (Etapa 16) ✅  
   Operador `core.filter` com presets e expressão validada por AST seguro, referenciando `payload`/`metadata`/`lifecycle`/`artifacts`.

3) **DSL Python com operador `|` → Graph** (Etapa 17)  
   O modo Python existe como texto “one-way”, mas ainda não compila para graph nem valida a DSL.

4) **Templates/instanciação multi-câmera** (Etapa 18)  
   Aplicar um pipeline `reuse` como template em N câmeras sem redesenhar manualmente.

5) **Pipelines finitos / self-destruct + lixeira** (Etapa 19)  
   Para casos assistidos por LLM (pipelines temporários e auto-removíveis).
