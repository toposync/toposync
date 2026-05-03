# Pipelines (DAG) — visão geral e uso

Este documento descreve o sistema de **Pipelines globais** do Toposync: modelo de dados, schema do graph, runtime (asyncio + backpressure/drop), operadores, execução local/remota, UI e recomendações de configuração.

> Status: Pipelines substituíram o legado de “detections” das câmeras. Ainda existem itens planejados (ver “Roadmap” no final).

---

## 1) O que é um Pipeline

Um **Pipeline** é uma entidade global executável da plataforma que define um **DAG** (grafo acíclico dirigido) de operadores. Quando `enabled=true`, o orquestrador inicia o runtime (local ou remoto).

Cada pipeline tem:
- **`name`**: obrigatório, único e **compatível com identificador Python** (ex.: `carro_na_frente`, `alertaPortao` não).
- **`enabled`**: controla se o orquestrador executa o pipeline.
- **`processing_server_id`**: onde executar o pipeline (`local` por padrão).
- **`graph`**: JSON com `schema_version` + `nodes` + `edges`.
- **`editor_mode`**: `interactive|json|python` (Python é “one-way”; ver UI).

Implementação: `src/toposync/runtime/config_store.py` (`Pipeline`, validações e CRUD).

---

## 2) Modelo de runtime: `Packet`, `Artifact` e lifecycle

O runtime trafega **Packets** (eventos) entre operadores. Um Packet carrega:

- **`stream_id`**: identifica um fluxo lógico (ex.: uma câmera, ou um objeto trackeado).
- **`payload["event_id"]`**: identifica um **evento lógico** dentro do fluxo (opcional).
  - Em tracking (`vision.track`), é preenchido e fica estável durante `open→update→close` (um por objeto).
  - Em detecção anotada (`vision.detect`), fica **nulo** por design. Lifecycle e identidade temporal pertencem a `vision.track`.
  - Operadores stateful que fazem sentido “por objeto” (ex.: `camera.velocity_estimation`, `camera.best_frame_selector`, `core.throttle`, `core.debounce`, `core.fps_reducer`) usam `event_id` quando existe e fazem fallback para `stream_id` quando não existe.
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

### Regra de ouro: frame nunca vai em `payload`

- **Frame de imagem sempre vai em `artifacts`**, nunca em `payload`.
- Convenção atual:
  - `artifacts["frame_original"]`: frame “imutável” de origem (full frame).
  - `artifacts["frame"]`: frame “corrente” do stream (pode ser alterado por operadores como crop/adjust).

### Orçamento de memória para `Artifact.data` (realtime safe)

Para manter a máquina saudável quando o pipeline começa a gerar **crops/máscaras/frames derivados em cascata**, o runtime aplica limites de memória (com métricas).

**Políticas**
- `core.store_images` por padrão **remove `Artifact.data` após persistir** (`drop_data_after_store=true`), mantendo apenas `Artifact.reference`.
- O runtime mantém métricas de **bytes de artifacts em memória** nas filas (por edge) e limita o crescimento com drop explícito.

**Limites (defaults; configuráveis via env)**
- `TOPOSYNC_ARTIFACT_MAX_BYTES_PER_PACKET` (default `134217728`): limite por Packet; quando estoura, o runtime “evicta” `Artifact.data` de artifacts derivados (mantém `frame_original`/`frame`).
- `TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_PER_PIPELINE` (default `536870912`): limite por runtime (por pipeline/bundle).
- `TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_GLOBAL` (default `1073741824`): limite global do processo (soma de todos os runtimes locais).

Observação: `open/close` são estruturais e não são descartados; sob pressão de memória, o runtime tende a descartar **updates** primeiro.

### Lifecycle invariants (sob drop/backpressure)

Para manter previsibilidade sob carga (e evitar “update sem open” ou “stream aberto para sempre”), o runtime segue invariantes:

- Para um mesmo `stream_id`, **`open` deve preceder `update`**.
- Para um mesmo `stream_id`, **`close` deve sempre ocorrer** (ou ser **sintetizado no shutdown/reload** por operadores stateful, como notificações).
- **`update` pode ser dropado**, mas o estado downstream deve ser “reparável” (ex.: reconstituível a partir do último update recebido).
- **`open`/`close` são mensagens estruturais e não são descartadas** por políticas de drop destinadas a `update`.
  - Na prática: `BoundedChannel` protege `open`/`close` e **descarta apenas `update` quando precisar liberar espaço**.

### Split (2 pessoas simultâneas)

Operadores como `vision.track` **dividem o stream**: de um `stream_id` da câmera surgem múltiplos `stream_id` (um por objeto). Isso permite:
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

### Semântica de canal (global vs keyed)

O runtime usa dois modos de fila, dependendo do ponto do grafo:

- **Global por edge** (default): um `BoundedChannel` único por edge.
  - Bom para fluxos com **um único stream** (ex.: antes do split).
- **Keyed por `stream_id`** (após `split_stream`): edges downstream de operadores com capability `split_stream` usam `KeyedBoundedChannel`, particionado por `Packet.stream_id`.
  - **Scheduler round-robin** entre streams para evitar starvation.
  - **Drop “por chave”** para `update`: quando precisa descartar, tenta descartar updates **do mesmo `stream_id`** antes de afetar outros streams.
  - Isso evita que um objeto “falante” monopolize a fila e cause drop/starvation de outros objetos.

### Regras práticas (realtime)

- **Frames brutos / entrada de visão**: `maxsize=1` + `latest_only`
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
  ],
  "interfaces": {},
  "limits": {},
  "layout": {},
  "meta": {}
}
```

`interfaces`, `limits`, `layout` e `meta` são opcionais e reservados para evolução do graph (ex.: redes/subfluxos com entradas e saídas explícitas). O runtime atual compila `nodes` e `edges`.

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

## 5) Sharing de computação (visão 1x) na prática

### Por que funciona

Nós “pesados” (ex.: `vision.detect` / `vision.track` / `vision.segment_instances`) têm `share_strategy="by_signature"`. Se dois pipelines têm o **mesmo subgrafo** (mesmo operador+config+upstream), o compilador gera a mesma assinatura e o runtime pode executar **uma única instância** alimentando múltiplos consumidores.

### Como é executado localmente

Quando há múltiplos pipelines locais habilitados, o orquestrador tenta:
- compilar todos em conjunto;
- montar um **`PipelineBundleRuntime`** (um DAG “merged”);
- compartilhar nós `shareable` por assinatura;
- manter nós `share_strategy="never"` isolados.

Implementação:
- merge: `src/toposync/runtime/pipelines/shared_runtime.py`
- orquestração: `src/toposync/runtime/pipelines/distributed/orchestrator.py`

Observação importante: o sharing é **node-level**.

- Um nó `shareable` (ex.: `vision.track`) é executado **1x** e sua saída é tratada como um **broadcast interno**: cada consumidor downstream recebe **seu próprio canal bounded** com seu `maxsize/drop_policy`.
- Diferenças de canal downstream **não impedem** que um nó pesado seja compartilhado.
- O que não pode ser compartilhado é um nó `shareable` cuja **entrada** teria políticas de canal diferentes (porque um nó tem um único input buffer). Nesse caso, o bundle **isola o nó downstream** (uma instância por pipeline) e mantém o upstream compartilhado.
- “Edge-level unify” é uma otimização interna apenas para conexões **entre nós já compartilhados** (onde as políticas de canal necessariamente coincidem).

---

## 6) Execução distribuída (origin vs processing)

Um pipeline pode rodar em um **processing server** remoto, mas:
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

### Failure modes e idempotência (notificações “open”)

O runtime foi desenhado para ser **previsível sob falhas** — especialmente para notificações com lifecycle `open/update/close`.

**Garantia de entrega (processing → origin): at-least-once (best effort).**
- O processing server publica eventos projetados com `event_id` e mantém um **buffer bounded** de replay.
- O origin consome via stream HTTP (SSE) e envia **ACK** de `last_event_id`.
- Em falhas de rede/reconnect, **o mesmo evento pode ser reenviado** (duplicado). Portanto, **idempotência é obrigatória** nos sinks.

**Como deduplicar**
- `core.notify` usa `dedupe_key` (único no SQLite) e faz **upsert**: replays/duplicatas **não geram notificações duplicadas**, apenas atualizam a existente.
- Para casos avançados, use `dedupe_key_template` para controlar dedupe por regra (ex.: por `tracking_id`, `correlation_id`, etc).

**Regras de recuperação**
- **Shutdown/reload limpo:** operadores stateful (ex.: `core.notify`) sintetizam `close` no `shutdown()` para garantir o invariant “close must happen” (`reason="shutdown_synthesized"`).
- **Restart/crash do origin:** ao subir, o origin fecha notificações de pipeline que ficaram `status="open"` no store com `reason="runtime_restart"`.
  - Implementação: `src/toposync/runtime/notifications/runtime.py` (`close_open_pipeline_notifications`) chamado no startup em `src/toposync/app.py`.
- **Processing server cai / rede processing→origin falha:**
  - eventos podem ficar no buffer de replay do processing e serem reenviados após reconexão;
  - se o origin ficar offline tempo suficiente para o buffer estourar, alguns `close` podem nunca chegar — nesse caso, o restart do origin fecha itens “open” remanescentes (regra acima).

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
- `vision.track`, `vision.detect`, `vision.segment_instances`
- `camera.object_crop`, `camera.image_resize`
- `camera.camera_mapping`, `camera.area_restriction`, `camera.velocity_estimation`
- `camera.best_frame_selector`

---

## 8) Operadores essenciais (comportamento atual)

### `camera.source` (latest frame wins)

- Função: captura RTSP e emite `Packet` com `artifacts["frame_original"]` + `artifacts["frame"]` e metadados básicos.
- Stream: `stream_id = "camera:<camera_id>"`
- Portas:
  - input opcional: `gate` (permite pausar leitura RTSP)
  - output: `out`
- Backpressure: idealmente `maxsize=1 latest_only` logo após a source.

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py` (`CameraSourceRuntime`), `extensions/cameras/src/toposync_ext_cameras/processing/camera_hub.py` (`CameraHub`), `extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py` (`FrameGrabber`).

### Gates “antes da câmera”

Conecte `core.schedule_gate.out -> camera.source.gate`.
Quando o gate fecha, o `camera.source` **para de consumir** a câmera, preservando CPU/I/O.

**Estratégia escolhida (recomendada): CameraHub por câmera.**

- O processo mantém um **hub por câmera** que gerencia a conexão RTSP (1x) e fornece “latest frame wins”.
- Cada `camera.source` é um consumidor do hub. O hub **só lê RTSP quando há pelo menos um consumidor ativo** (agrega demanda).
- Isso evita:
  - **múltiplas conexões RTSP** quando há dois pipelines para a mesma câmera (ex.: schedules diferentes);
  - um pipeline “desligar” a câmera para o outro (cada consumidor abre/fecha sua própria demanda).

Trade-off:
- **CameraHub por câmera (escolhido):** 1 conexão RTSP por câmera no processo, com demanda agregada por refcount; gates não causam interferência entre pipelines.
- **Share-by-signature apenas:** pode multiplicar conexões RTSP e/ou causar interferência se a source for compartilhada sem agregação correta de gates.

Implementação: `src/toposync/runtime/pipelines/operators_gates.py` + `extensions/cameras/src/toposync_ext_cameras/processing/camera_hub.py` + `camera.source`.

### `camera.motion_gate` (não encerra evento)

- Função: anota motion no payload e filtra frames quando idle (por padrão).
- Importante: não emite `close`/encerramento de tracking. É apenas um gate de frames.

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py` (`MotionGateRuntime`).

### `vision.track` (split por objeto)

Consome `payload["vision"]["detections"]` anotado por `vision.detect` e emite um stream por objeto (`obj:...`) com lifecycle.

Payload adicionado/normalizado (campos principais):
- `tracking_id` (sempre preenchido; pode ser sintético)
- `tracker_track_id` (id nativo do tracker; pode ser `None`)
- `camera_id` (sempre presente no track; futuro multi-câmera)
- `correlation_id` (uuid por track)
- `object_category_label`, `object_confidence`, `object_bbox01`
- `source_stream_id` (stream original)
- `detected_object` (objeto completo)
- `payload["vision"]["tracks"]` em `emit_mode="annotate"`
- opcionalmente: `world_anchor`, `appearance_embedding_artifact_name`, `keypoints`

Config defaults importantes:
- `tracker_id="simple_iou_kalman"`
- `default_interval_seconds=0.2` (evita updates sem limite)
- `close_after_seconds=4.0` (evita “flicker” sob oclusões/drops)

Trackers first-party iniciais:
- `simple_iou_kalman`
- `norfair`

Hooks estruturais já reservados para futuro multi-câmera:
- todo track sai com `camera_id`
- `use_world_anchor=true` permite carregar `world_anchor` quando houver `camera.world` upstream
- `appearance_embedding_artifact_name` pode ser propagado no contrato sem mudar o shape do packet depois

Implementação: `extensions/vision/src/toposync_ext_vision/processing/tasks/tracking.py`.

### `vision.detect` (annotate-first)

Detecção task-oriented sem vendor público:
- anota o packet com `payload["vision"] = {"task": "detection", "model_id", "runtime", "detections": [...]}`.
- mantém os campos compatíveis já usados no ecossistema downstream:
  - `object_category_label`, `object_confidence`, `object_bbox01`
  - `detected_object`, `detected_objects`
- backend first-party atual: ONNX Runtime, resolvido a partir do `ModelManifest`
- família first-party inicial: RTMDet (`rtmdet_det_tiny`, `rtmdet_det_small`, `rtmdet_det_medium`)
- parser RTMDet dedicado: espera saídas `dets + labels` no estilo MMDeploy e reverte o letterbox para bbox em coordenadas do frame de entrada antes de reaplicar crop/warp do pipeline
- o editor escolhe modelos por tarefa usando o catálogo do processing server selecionado, com badges de recomendação e ocultação de modelos indisponíveis no seletor rápido
- no fluxo básico, o usuário escolhe só o que faz sentido para a tarefa; detalhes técnicos de runtime/artifacts ficam no modo avançado
- o usuário pode importar um manifesto customizado no modo avançado; o modelo entra no mesmo catálogo da tarefa, sem criar operador separado
- não cria lifecycle por objeto; isso continua em `vision.track`.

Implementação: `extensions/vision/src/toposync_ext_vision/processing/tasks/detection.py`.

### `vision.segment_instances` (máscara real)

Segmentação de instâncias task-oriented:
- anota o packet com `payload["vision"] = {"task": "segmentation", "model_id", "runtime", "segmentations": [...]}`.
- cada instância inclui `bbox01` + `mask_artifact_name`; `polygon01` é opcional.
- backend first-party atual: ONNX Runtime.
- família first-party inicial: RTMDet-Ins (`rtmdet_ins_tiny`, `rtmdet_ins_small`, `rtmdet_ins_medium`).
- parser RTMDet-Ins dedicado: espera saídas `dets + labels + masks` e reverte o letterbox para bbox/máscara no frame de entrada antes de reaplicar crop/warp do pipeline.
- quando `attach_mask_artifacts=true`, anexa uma máscara binária por instância e publica a principal como image key semântica `mask`.
- pode reconciliar as máscaras com detections/tracks já presentes no packet, sem mexer no lifecycle do objeto.
- o editor usa o mesmo fluxo task-based do `vision.detect`: shortlist recomendada, badges, detalhes avançados e importação de manifesto customizado.

Implementação: `extensions/vision/src/toposync_ext_vision/processing/tasks/segmentation.py`.

### `vision.pose_estimate` (skeleton, ainda não lançado)

Reserva a interface pública task-based para pose sem expor vendor/framework:
- registra `payload["vision"] = {"task": "pose", "model_id", "runtime", "poses": [...]}`.
- define o contrato `PoseObject` com `bbox01`, `keypoints`, `tracking_id` e `metadata`.
- consome hints opcionais de `payload["vision"]["detections"]` e `payload["vision"]["tracks"]`, para que o backend futuro consiga ligar keypoints ao tracking sem quebrar a arquitetura.
- o backend first-party ainda não está habilitado; nesta fase o operador existe como scaffold de contrato/runtime/registry.

Implementação: `extensions/vision/src/toposync_ext_vision/processing/tasks/pose.py`.

### Pós-processamento e artefatos

- `camera.object_crop`: recorte por bbox (gera artifact, default `segmented`); não é segmentação
- downstream pode continuar escolhendo entre bbox crop, máscara de instância e, no futuro, pose/keypoints, sem conflitar semanticamente com `camera.object_crop`
- manifests de visão também podem declarar `capabilities` opcionais, por exemplo `reid`, para preparar catálogos futuros sem trocar o schema do registry
- `camera.image_resize`: redimensiona artifacts em memória (reduce payload/storage)
- `camera.best_frame_selector`: buffer bounded por track para escolher melhor frame (default `best_frame`)

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py`.

### Mapeamento, áreas e velocidade

Pipeline “feliz” para velocidade/área:
`... -> camera.camera_mapping -> camera.velocity_estimation -> camera.area_restriction -> ...`

- `camera.camera_mapping`: projeta `image_uv`/bbox para `world` (x,z) via `control_point_sets`, com seleção opcional por pose PTZ.
- `camera.area_restriction`: filtra ou anota labels de áreas do mundo.
- `camera.velocity_estimation`: calcula velocidade em m/s e km/h; pode filtrar (ex.: `stopped_now`).

Implementação: `extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py`.

---

## 9) Storage e Notificações (sinks na origem)

### `core.store_images` (local, naming rico)

Persistência local (por enquanto):
- escreve arquivos em `files/` e coloca o caminho relativo em `Artifact.reference`
- usa `format=webp` por padrão; `png` e `jpg` seguem disponíveis para precisão sem perda ou máxima compatibilidade
- por padrão, **remove `Artifact.data` após persistir** (`drop_data_after_store=true`) para reduzir uso de memória
- **não** depende de câmera/tracking id “field configurável”: resolve por payload/metadata quando possível
- converte BGR→RGB ao codificar WebP/PNG/JPEG (OpenCV friendly)

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
  - **Python (one-way)**: DSL Python com `|` que **compila para graph**; após salvar em Python, não volta para Interactive/JSON

Frontend:
- tela: `frontend/src/ui/screens/PipelinesScreen.tsx`
- editor interativo: `frontend/src/ui/screens/pipelines/InteractivePipelineEditor.tsx`

### Python DSL (`|`) → Graph (determinístico)

No modo Python, o backend executa a DSL e gera um graph v1 canônico (com defaults normalizados, canais bounded e drop policies).

Regras atuais:
- o código deve definir `PIPELINE` (ou uma variável com o mesmo nome do pipeline)
- o `PIPELINE` deve ser uma expressão de stream construída com o DSL (`camera.*`, `core.*`, `vision.*`, `dist.*`, ou `op("...")`)

Exemplo mínimo:
```py
PIPELINE = (
  core.demo_frame_sequence_source(_id="source")
  | core.notify(_id="notify")
)
```

Endpoints:
- `POST /api/pipelines/compile-python` (retorna `graph` + output compilado + alerts)
- `PUT/POST /api/pipelines` em `editor_mode=python` recompila o `python_source` e persiste o `graph`

### Templates por graph (instanciação multi-câmera)

O endpoint de template usa o `graph` de um pipeline existente para criar N pipelines (um por câmera) sem redesenhar. Como pipelines são executáveis, mantenha pipelines usados apenas como base com `enabled=false`.

- Endpoint: `POST /api/pipelines/templates/apply-cameras`
- Regras:
  - o template precisa ter **exatamente 1** nó `camera.source`
  - para cada `camera_id`, o sistema clona o graph e injeta `camera.source.config.camera_id=<camera_id>` (limpando `rtsp_url/username/password`)
  - nomes são determinísticos: `<template_name>__<camera_id>` (sanitizado para identificador Python)

Observação: reuso avançado/subfluxos com interfaces explícitas não fazem parte do contrato atual de Pipeline. Quando existir, deve ser modelado como uma entidade de graph/subflow própria, não como um campo `type` do pipeline executável.

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

3) **DSL Python com operador `|` → Graph** (Etapa 17) ✅  
   O modo Python compila para graph de forma determinística via backend e permanece “one-way”.

4) **Templates/instanciação multi-câmera** (Etapa 18) ✅  
   Aplicar o graph de um pipeline como template em N câmeras via endpoint.

5) **Pipelines finitos / self-destruct + lixeira** (Etapa 19)  
   Para casos assistidos por LLM (pipelines temporários e auto-removíveis).
