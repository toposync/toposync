# Dossie para planejamento de novos operadores de movimento

Este documento consolida o que precisamos saber sobre:

- gate de movimento atual;
- pipelines e runtime;
- contratos de payload/artifacts/lifecycle;
- operadores disponiveis;
- pontos de extensao no backend, UI e wizards;
- implicacoes praticas para desenhar novos operadores de deteccao de movimento.

O foco aqui nao e descrever "tudo do TopoSync", e sim tudo que afeta diretamente a criacao de novos operadores de movimento sem quebrar o modelo atual da plataforma.

---

## 1. Visao geral da aplicacao

TopoSync e um host de extensoes com:

- backend Python/FastAPI;
- frontend React carregando extensoes por Module Federation;
- configuracao persistida em `.toposync-data/config.json`;
- pipelines globais como DAGs executados pelo orchestrator local ou remoto.

Arquitetura base:

- `src/toposync/app.py`: sobe a API, registry de operadores, orchestrator de pipelines, telemetry, snapshots e notificacoes.
- `src/toposync/runtime/config_store.py`: persistencia de composicoes, settings, pipelines e processing servers.
- `src/toposync/runtime/pipelines/*`: compilacao, runtime, canais bounded, sharing, distribuicao, recommendations.
- `extensions/cameras/*`: camera source, motion gate, YOLO, mapping, pos-processamento e wizard de pipelines de camera.
- `extensions/streaming/*`: sink `stream.write` e wizard de pipelines de streaming.

Ponto importante de projeto do repositorio:

- TopoSync deve continuar generico.
- Comportamentos especificos de dominio devem ficar em extensoes.
- Nao devemos colocar "gambiarras" no core para acomodar um caso especifico de uma extensao.

Na pratica: se um novo operador de movimento for especifico de camera/visao, o lugar natural dele e a extensao `com.toposync.cameras`, nao o core.

---

## 2. Modelo mental dos pipelines

Um pipeline e uma entidade persistida com:

- `name`: identificador Python valido;
- `enabled`;
- `processing_server_id`;
- `editor_mode`: `interactive`, `json` ou `python`;
- `graph`: `schema_version`, `nodes`, `edges`.

Arquivos centrais:

- `src/toposync/runtime/config_store.py`
- `src/toposync/runtime/pipelines/compiler.py`
- `src/toposync/runtime/pipelines/execution.py`
- `src/toposync/runtime/pipelines/runtime.py`

Formato do graph:

```json
{
  "schema_version": 1,
  "nodes": [
    { "id": "source", "operator": "camera.source", "config": { "camera_id": "cam_1" } }
  ],
  "edges": [
    {
      "from": { "node": "source", "port": "out" },
      "to": { "node": "motion", "port": "in" },
      "maxsize": 2,
      "drop_policy": "drop_oldest"
    }
  ]
}
```

Compilacao faz:

- validacao do schema;
- validacao de DAG sem ciclos;
- validacao de portas;
- normalizacao Pydantic do `config`;
- calculo de `signature`;
- marcacao de no como `shareable` quando `share_strategy="by_signature"`.

O backend expoe isso via:

- `GET /api/pipelines`
- `GET /api/pipelines/operators`
- `POST /api/pipelines/compile`
- `POST /api/pipelines/compile-python`
- `POST /api/pipelines/runtime/reload`
- `GET /api/pipelines/runtime/status`

Flexibilidade atual de autoria:

- modo `interactive`: lista de steps no frontend;
- modo `json`: graph editavel diretamente;
- modo `python`: DSL Python compilada para graph.

Tambem existe aplicacao de templates para N cameras:

- `POST /api/pipelines/templates/apply-cameras`

---

## 3. Runtime: Packet, Artifact, lifecycle e filas

O runtime troca `Packet`s entre operadores.

### 3.1 Packet

`Packet` carrega:

- `packet_id`
- `stream_id`
- `lifecycle`: `open`, `update`, `close`
- `payload`
- `artifacts`
- `metadata`
- `parent_packet_id`

Arquivo:

- `src/toposync/runtime/pipelines/runtime.py`

### 3.2 Artifact

`Artifact` carrega:

- `name`
- `data`: blob em memoria
- `reference`: caminho persistido
- `mime_type`
- `metadata`

### 3.3 Regra de ouro

Frame nunca vai em `payload`.

Convencao base atual:

- `artifacts["frame_original"]`: frame original
- `artifacts["frame"]`: frame corrente do stream

O payload usa chaves semanticas para referenciar artifacts:

- `payload["images"]["original"] -> "frame_original"`
- `payload["images"]["treated"] -> "frame"`

Arquivo:

- `src/toposync/runtime/pipelines/images.py`

### 3.4 Lifecycle

`open/update/close` e parte do contrato do runtime, nao um detalhe de um operador.

Invariantes importantes:

- `open` deve vir antes de `update`;
- `close` deve acontecer ou ser sintetizado;
- `update` pode ser dropado;
- `open` e `close` sao estruturais e nao devem ser descartados por pressao de fila.

### 3.5 Filas bounded e drop policy

Toda edge tem fila bounded com `maxsize` e `drop_policy`.

Politicas disponiveis no runtime:

- `block`
- `drop_updates`
- `drop_oldest`
- `drop_newest`
- `latest_only`
- `keyed_latest_only`

Quando um operador tem capability `split_stream`, o downstream passa a usar `KeyedBoundedChannel` por `stream_id`, com round-robin entre chaves. Isso e fundamental para tracking e qualquer futuro operador que abra multiplos substreams.

### 3.6 Orcamento de memoria para artifacts

O runtime protege memoria de artifacts em memoria.

Variaveis principais:

- `TOPOSYNC_ARTIFACT_MAX_BYTES_PER_PACKET`
- `TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_PER_PIPELINE`
- `TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_GLOBAL`

Quando estoura o limite por packet, o runtime tende a remover `Artifact.data` de derivados, preservando `frame_original` e `frame`.

---

## 4. Contratos de payload e artifacts que importam para movimento

### 4.1 Payload basico de `camera.source`

`camera.source` produz:

- `camera_id`
- `camera_name`
- `frame_ts`
- `frame_width`
- `frame_height`
- `capture`
- `images`

De fato, `capture` traz metricas do grabber/backend, e `metadata` tambem recebe:

- `source`
- `camera_id`
- `camera_name`
- `capture_backend`

### 4.2 Payload do `camera.motion_gate`

O operador anexa em `payload["motion"]`:

- `active`
- `score`
- `bboxes01`
- `latency_ms`
- `fps`
- `hold_active`

E em `metadata`:

- `motion_gate_open`

Observacao importante: o contrato formal registrado hoje declara `produces_payload_keys=["motion"]`, mas o runtime tambem depende de `metadata.motion_gate_open` como saida de fato. Isso ja e usado por:

- `core.lifecycle_from_boolean`
- `vision.track` quando `pause_when_gate_closed=true`

### 4.3 Payload de YOLO

`vision.track` e `vision.detect` produzem:

- `event_id`
- `tracking_id`
- `tracker_track_id`
- `correlation_id`
- `source_stream_id`
- `object_category_label`
- `object_confidence`
- `object_bbox01`
- `detected_object`
- `detected_objects`

Diferencas semanticas:

- tracking em `emit_mode="events"` divide stream e emite lifecycle por objeto;
- detection em `emit_mode="events"` emite evento curto por deteccao, normalmente `open` + `close`;
- detection em `emit_mode="filter"` passa apenas frames com deteccao, preservando lifecycle do frame de origem;
- ambos em `emit_mode="annotate"` mantem o packet original e apenas anotam o payload.

### 4.4 Payload de pos-processamento

Varios operadores de imagem anotam:

- `artifact_contract`
- `artifact_names`

Alguns tambem anotam:

- `frame_crop`
- `frame_warp`

`artifact_contract` hoje tem:

- `available_artifact_names`
- `preferred_input_artifact_names`
- `selected_input_artifact_name`
- `latest_artifact_name`

Isso e util para qualquer novo operador que selecione artifacts dinamicamente.

### 4.5 Payload de armazenamento

`core.store_images` preenche:

- `payload["stored_images"]`

Cada entrada traz pelo menos:

- `rel_path`
- `artifact_name`
- `mime_type`
- `stored_ts_ms`
- `confidence` quando disponivel

### 4.6 Payload de mapeamento/velocidade

`camera.camera_mapping` produz:

- `world`
- `mapping`

`camera.velocity_estimation` produz:

- `velocity.speed_mps`
- `velocity.speed_kmh`
- `velocity.distance_m`
- `velocity.elapsed_seconds`
- `velocity.moving`
- `velocity.stopped`
- `velocity.valid`
- `velocity.ever_stopped`
- campos `raw_*`

Esses operadores nao sao de movimento por pixel, mas sao parte do pipeline real de "evento relevante" baseado em camera.

---

## 5. Gate de movimento atual: o que ele faz de verdade

Arquivo principal:

- `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`

Detector base:

- `extensions/cameras/src/toposync_ext_cameras/processing/motion.py`

### 5.1 Semantica

`camera.motion_gate` hoje e um gate de frames, nao um gerador de eventos lifecycle.

Ele:

- le uma imagem de `input_with_fallback`;
- calcula score de movimento por diferenca entre frame atual e frame anterior;
- aplica hold temporal;
- opcionalmente deixa passar frames idle;
- anota payload/metadata;
- nao emite `close`.

O teste `test_motion_gate_uses_hold_without_emitting_close` deixa isso explicito.

### 5.2 Config atual

Defaults reais:

- `input_with_fallback = "segmented,treated,original"`
- `fallback_to_stream_frame = true`
- `threshold = 0.01`
- `hold_seconds = 2.5`
- `activation_frames = 1`
- `emit_when_idle = false`
- `mask_enabled = false`
- `mask_mode = "include"`
- `mask_brush_diameter01 = 0.1`
- `mask_strokes = []`

### 5.3 Como a deteccao funciona

`MotionDetector` atual:

- converte para grayscale;
- aplica blur gaussiano;
- calcula `absdiff` contra o frame anterior;
- aplica threshold binario em 25;
- mede `score = pixels_alterados / total`;
- considera ativo se `score >= threshold`;
- extrai ate `max_blobs` bounding boxes normalizados `bboxes01`.

Comportamento relevante:

- primeira amostra so inicializa baseline;
- se o tamanho do frame muda, o detector reseta o baseline e nao quebra;
- ROI mask pode restringir a area contada no score.

### 5.4 ROI/mask

O gate suporta mascara desenhada pela UI:

- strokes `paint` e `erase`;
- `mask_mode = include|exclude`;
- `mask_brush_diameter01` relativo ao menor eixo do frame;
- cache por `stream_id` e dimensao do frame.

Hoje a UI ja tem modal de desenho para isso em:

- `frontend/src/ui/screens/pipelines/editor/panels/CameraPanels.tsx`

### 5.5 Interacoes importantes com o resto do runtime

1. O gate depende de imagem real em artifact.
   Se upstream nao garantir `frame_original`/`frame` ou mapping em `payload.images`, ele falha semanticamente.

2. O gate e shareable.
   `share_strategy="by_signature"`.

3. O gate roda em `thread_pool`.
   Isso faz sentido para OpenCV/numpy bloqueante.

4. O gate publica telemetry.
   Hoje usa o metric id `motion.score`.

5. O gate agenda snapshots de entrada.
   Quando existe `pipeline_snapshot_store`, ele salva snapshots throttled para tooling/UI.

6. O gate sinaliza abertura/fechamento logico em metadata.
   `metadata.motion_gate_open` e a ponte para eventificacao downstream.

### 5.6 O que ele nao faz

- nao cria `event_id`;
- nao cria stream novo;
- nao tem lifecycle proprio;
- nao distingue tipos de movimento;
- nao rastreia blobs ao longo do tempo;
- nao sabe nada sobre areas, velocidade ou contexto sem operadores adicionais.

### 5.7 Padrao oficial atual para transformar movimento em evento

Quando precisamos lifecycle finito de movimento puro, o padrao existente e:

`camera.source -> camera.motion_gate -> core.lifecycle_from_boolean -> ...`

Isso ja aparece na migracao do legado:

- `src/toposync/runtime/pipelines/migration_legacy_cameras.py`

Esse detalhe e central para o planejamento:

- se o novo operador for um "gate anotador", ele pode seguir esse padrao;
- se ele for um "detector de evento de movimento", talvez faca mais sentido emitir lifecycle diretamente.

---

## 6. Operadores disponiveis hoje

Catalogo real obtido do registry com builtins + cameras + streaming.

### 6.1 Sources e gates de controle

- `camera.source`: source de camera RTSP/ONVIF. Input opcional `gate`. Produz `frame_original`, `frame`, metadados de captura e `payload.images`.
- `core.schedule_gate`: source que emite `open/close` por horario. Pode ser ligado em `camera.source.gate` para pausar captura.
- `core.category_gate`: filtro lifecycle-safe por categoria.
- `core.filter`: filtro deterministico com presets e expressao segura sobre `payload`, `metadata`, `lifecycle` e conjunto de `artifacts`.
- `core.lifecycle_from_boolean`: converte um campo booleano em `open/update/close`.

### 6.2 Controle de taxa e observabilidade

- `core.fps_reducer`: reduz FPS preservando `open/close`.
- `core.throttle`: throttle keyed.
- `core.velocity_throttle`: throttle com intervalos diferentes para movendo/parado.
- `core.debounce`: emite o primeiro e espera quiet period.
- `core.stream_state_snapshot`: side output `snapshot` com estado leve por stream.
- `core.debug`: dump de packet e imagens para stdout/disco temporario.

### 6.3 Movimento e visao

- `camera.motion_gate`: gate de movimento baseado em frame differencing.
- `vision.track`: tracking por objeto, split-stream, lifecycle por objeto em `events`, annotate em `annotate`.
- `vision.detect`: deteccao por frame, eventos curtos por objeto ou annotate.

### 6.4 Pos-processamento e contratos de imagem

- `camera.frame_attach`: anexa frame de outro stream.
- `vision.crop_objects`: crop por bbox, gera `segmented`.
- `camera.image_crop`: crop retangular, pode atualizar stream frame e anota `frame_crop`.
- `camera.image_perspective_crop`: warp perspectivo, pode atualizar stream frame e anota `frame_warp`.
- `camera.image_adjust`: brilho/contraste/gamma/saturacao.
- `camera.local_contrast_clahe`: contraste local.
- `camera.unsharp_mask`: sharpen leve.
- `camera.denoise_luma`: denoise de luminancia.
- `camera.auto_gamma`: gamma automatico com suavizacao temporal.
- `camera.global_stabilize`: estabilizacao translacional.
- `camera.lens_undistort`: correcao de lente.
- `camera.image_resize`: resize em memoria de artifacts selecionados.
- `camera.best_frame_selector`: escolhe melhor frame com buffer bounded por stream.

### 6.5 Contexto espacial e filtragem semantica

- `camera.camera_mapping`: imagem -> mundo.
- `camera.area_restriction`: filtra por poligonos no mundo.
- `camera.velocity_estimation`: estima velocidade e pode filtrar por estado.

### 6.6 Persistencia, notificacao e streaming

- `core.store_images`: persiste artifacts e anexa `reference`.
- `core.notify`: cria/atualiza notificacoes lifecycle-safe com `dedupe_key`.
- `stream.write`: publica frame do pipeline em uma transmissao.

### 6.7 Operadores auxiliares / internos

- `core.source`, `core.passthrough`, `core.sink`, `core.synthetic_source`, `core.demo_frame_sequence_source`
- `dist.remote_source`, `dist.project_to_origin`, `dist.target_filter`

Observacao:

- os operadores `dist.*` fazem parte do mecanismo de execucao distribuida e nao devem ser alvo direto de modelagem de deteccao de movimento.

---

## 7. O que temos de flexibilidade no modelo de operador

Cada operador registrado declara:

- `id`
- `description`
- `inputs` e `outputs`
- `config_model` Pydantic
- `defaults`
- `config_schema`
- `capabilities`
- `share_strategy`
- `execution_mode`
- `max_concurrency`
- `requires_payload_keys`
- `requires_artifacts`
- `produces_payload_keys`
- `produces_artifacts`

Arquivo:

- `src/toposync/runtime/pipelines/operator_registry.py`

Isso nos da varios graus de liberdade para novos operadores de movimento.

### 7.1 Entradas e saidas

Um operador pode ser:

- `source`
- `transform`
- `sink`

E pode ter multiplas portas. Exemplo:

- `camera.source` recebe `gate`;
- `camera.frame_attach` recebe `in` e `frames`;
- `core.stream_state_snapshot` emite `out` e `snapshot`.

### 7.2 Modos de execucao

Disponiveis:

- `in_event_loop`
- `thread_pool`
- `process_pool`
- `external`

No codigo atual, o que vemos na pratica para visao:

- transformacoes simples/stateful leves: `in_event_loop`
- OpenCV/YOLO: majoritariamente `thread_pool`

### 7.3 Sharing

`share_strategy`:

- `by_signature`: o no pode ser compartilhado entre pipelines quando assinatura bate;
- `never`: no isolado.

Regra pratica:

- operadores puros e deterministas: tendem a `by_signature`;
- operadores com side effects, estado acoplado ao pipeline ou IO externo: `never`.

### 7.4 Capability `split_stream`

Essa capability muda a topologia semantica do runtime.

Hoje ela aparece em:

- `vision.track`
- `vision.detect`

Se um futuro operador de movimento gerar N subeventos simultaneos por frame ou por blob, essa capability pode ser relevante.

### 7.5 Contracts leves

O compilador/recommender usa `requires_*` e `produces_*` para alertar a UI.

Exemplos ja cobertos por testes:

- `camera.motion_gate` alerta se upstream nao garante `frame_original`;
- `vision.crop_objects` alerta se upstream nao garante `object_bbox01`.

Isso e importante para novos operadores de movimento porque melhora UX e reduz pipelines invalidos.

---

## 8. Como o frontend trata operadores e steps

O frontend busca a lista de operadores em:

- `GET /api/pipelines/operators`

Arquivos:

- `frontend/src/util/api.ts`
- `frontend/src/ui/screens/pipelines/InteractivePipelineEditor.tsx`
- `frontend/src/ui/screens/pipelines/editor/InteractiveStepCard.tsx`
- `frontend/src/ui/screens/pipelines/editor/panels/OperatorConfigPanel.tsx`

### 8.1 O que acontece automaticamente

Automatico hoje:

- o operador aparece na lista retornada pela API;
- defaults/schema existem no backend;
- o modo advanced mostra JSON de config;
- campos escalares simples ainda aparecem em grade generica.

### 8.2 O que nao acontece automaticamente

Para boa UX no editor interativo, normalmente precisamos:

- adicionar o operador em `PIPELINE_PRESET_OPERATOR_IDS` se quiser botao rapido;
- criar painel dedicado em `OperatorConfigPanel.tsx` se ele tiver UX propria;
- ligar telemetry inspector se houver threshold/metricas novas;
- adicionar draw modals ou selects contextuais se a configuracao depender de snapshot/camera/areas.

Isso importa muito para novos operadores de movimento, porque a experiencia atual do `camera.motion_gate` inclui:

- threshold com histograma (`motion.score`);
- mascara desenhavel;
- campos avancados de fallback de artifacts.

Se um novo operador de movimento tiver UX comparavel, provavelmente vai precisar de painel proprio.

---

## 9. Pipelines reais existentes relacionados a movimento

### 9.1 Wizard de cameras

Arquivo:

- `extensions/cameras/src/toposync_ext_cameras/plugin.py`

Presets principais:

- `people`
- `pets`
- `vehicles_stopped`

Todos comecam por:

- `camera.source`
- `camera.motion_gate`

Depois variam com tracking, mapping, area restriction, velocity, segment, store e notify.

Padrao observado:

- movimento e usado como gate barato antes do resto do pipeline;
- YOLO entra depois do gate;
- storage/notify ficam no fim;
- edge sizes crescem conforme o pipeline fica mais semantico e menos frame-rate sensivel.

### 9.2 Wizard de streaming

Arquivo:

- `extensions/streaming/src/toposync_ext_streaming/wizard/pipeline_builder.py`

Presets:

- `simple_stream`
- `motion_gate_stream`
- `detection_stream`
- `tracking_stream`
- `segmentation_stream`

`motion_gate_stream` mostra outro uso importante:

- movimento como filtro de frames para uma saida de stream, sem lifecycle.

### 9.3 Migracao do legado

Arquivo:

- `src/toposync/runtime/pipelines/migration_legacy_cameras.py`

Para trigger de movimento legado, o graph migrado e:

- `camera.source`
- `camera.motion_gate` com `emit_when_idle=true`
- `core.lifecycle_from_boolean`
- `camera.best_frame_selector`
- `core.store_images`
- `core.notify`

Esse fluxo mostra claramente a separacao atual entre:

- detectar/annotar estado de movimento;
- transformar isso em evento lifecycle;
- selecionar imagem representativa;
- persistir/notificar.

---

## 10. Telemetry, snapshots e observabilidade

Metricas principais ja existentes:

- `motion.score`
- `vision.confidence`
- `store.image`

Arquivo:

- `src/toposync/runtime/pipelines/telemetry.py`

O frontend ja abre histogramas para:

- `camera.motion_gate.threshold` -> `motion.score`
- `vision.*.confidence_threshold` -> `vision.confidence`

Snapshots de step:

- motion gate e debug ja podem agendar snapshots de entrada;
- isso alimenta tooling de desenho/preview.

Arquivo:

- `src/toposync/runtime/pipelines/step_snapshots.py`

Para novos operadores de movimento, telemetry nao e opcional na pratica se houver threshold sensivel. Sem isso, fica dificil calibrar.

---

## 11. Riscos e detalhes de arquitetura que afetam novos operadores

### 11.1 `camera.source` nao e uma source trivial

Ele tem:

- suporte a `camera_id` ou `rtsp_url`;
- backend `auto|opencv|ffmpeg`;
- ONVIF fallback/caching;
- `CameraHub` global para evitar conexoes RTSP duplicadas;
- gate de entrada para pausar leitura.

Ou seja: em geral, nao queremos inventar outra source para resolver problema de movimento, salvo necessidade muito clara.

### 11.2 O gate de movimento atual e barato e anterior ao split

Isso e importante porque:

- reduz custo antes de YOLO;
- opera em stream unico da camera;
- nao entra no dominio de lifecycle por objeto.

Se um novo operador de movimento for mais pesado que o gate atual, ele precisa justificar claramente o custo e a posicao no pipeline.

### 11.3 Side effects e distribuicao

Operadores com `origin_only` hoje:

- `core.store_images`
- `core.notify`
- `dist.remote_source`

O split distribuido suportado hoje e essencialmente processing -> origin.
Entao operadores de movimento puros sao bons candidatos a rodar no processing server; sinks nao.

### 11.4 Sharing pode ser vantagem ou armadilha

Se o operador for:

- puro;
- deterministico;
- configurado so por parametros do proprio no e upstream;

entao `by_signature` pode economizar CPU entre pipelines.

Mas se o operador carregar estado muito contextual ao pipeline ou side effect implicito, o sharing vira fonte de bugs.

### 11.5 Eventificacao e decisao de fronteira

Hoje temos tres semanticas diferentes no produto:

- gate de frame: `camera.motion_gate`
- annotate sem lifecycle: `vision.*` em `emit_mode="annotate"`
- evento lifecycle/split-stream: `vision.*` em `emit_mode="events"` ou `core.lifecycle_from_boolean`

Ao planejar novos operadores de movimento, precisamos decidir explicitamente em qual dessas familias ele cai.

---

## 12. Guia de decisao para novos operadores de movimento

Aqui entram algumas inferencias baseadas no modelo atual do codigo.

### 12.1 Pergunta 1: ele e gate, anotador ou gerador de evento?

Modelos possiveis:

- gate: deixa passar ou segura frames, como `camera.motion_gate`;
- anotador: adiciona score/mapa/atributos, mas mantem stream atual;
- gerador de evento: emite lifecycle proprio;
- split-stream por blob/regiao: cria substreams de movimento concorrentes.

Recomendacao:

- se o objetivo e baratear pipeline antes de YOLO, fique no modelo gate/anotador;
- se o objetivo e notificar "evento de movimento" com abertura/fechamento, considere operador lifecycle proprio ou `camera.motion_gate + core.lifecycle_from_boolean`;
- se o objetivo e rastrear multiplas regioes/blobs em paralelo, o modelo pode precisar de `split_stream`.

### 12.2 Pergunta 2: ele consome qual artifact?

Hoje o ecossistema trabalha bem com:

- `original`
- `treated`
- `segmented`
- `best_frame`

Se o novo operador processar imagem:

- use `input_with_fallback` ou `input_artifact_names`;
- respeite `payload.images`;
- nao coloque frame em `payload`.

### 12.3 Pergunta 3: ele produz o que?

Opcoes compatveis com o modelo existente:

- `payload["motion_*"]` ou um dict especializado;
- novo artifact;
- `metadata` de gate/estado;
- lifecycle novo;
- side output dedicado, se houver caso real.

Se produzir artifact, idealmente anote:

- `artifact_contract`
- `artifact_names`
- `payload.images` se houver chave semantica nova

### 12.4 Pergunta 4: como calibrar?

Se existir threshold, score continuo ou histerese:

- expose config clara no `config_model`;
- publique telemetry numerica;
- pense em snapshot de entrada se a calibracao for visual;
- se a UI interativa for importante, crie painel dedicado.

### 12.5 Pergunta 5: o operador deve ser shareable?

Boa regra:

- `by_signature` para operadores puros de processamento;
- `never` para operadores com side effects ou estado acoplado a instancia/pipeline.

### 12.6 Pergunta 6: onde ele vive?

Pelo principio do repo:

- core so se a abstracao for claramente generica para qualquer dominio;
- cameras se o operador depende de frame/camera/RTSP/visao;
- outra extensao se o dominio for outro.

---

## 13. Checklist tecnico para implementar um novo operador de movimento

### Backend

- criar `config_model` Pydantic com defaults bons;
- registrar operador no registry com contracts corretos;
- escolher `execution_mode`;
- escolher `share_strategy`;
- definir portas;
- implementar runtime respeitando `Packet`/`Artifact`;
- publicar telemetry se houver threshold/score;
- se gerar artifacts, atualizar `payload.images` quando fizer sentido;
- se for stateful, limpar estado em `shutdown` e em `close`.

### UX e editor

- decidir se entra no toolbar (`PIPELINE_PRESET_OPERATOR_IDS`);
- decidir se precisa de painel dedicado;
- decidir se precisa de integracao com telemetry inspector;
- decidir se precisa de snapshot/draw modal.

### Recommendations

- adicionar alerts em `src/toposync/runtime/pipelines/recommendations.py` se houver posicionamento invalido comum;
- registrar `requires_*` e `produces_*` para guidance basica.

### Wizards

- avaliar se entra em presets de camera;
- avaliar se entra em presets de streaming;
- avaliar se precisa de migracao do legado ou template.

### Testes minimos

- contrato do operador no compiler/recommendations;
- comportamento de lifecycle, se houver;
- comportamento sob hold/debounce;
- comportamento com mudanca de tamanho de frame;
- comportamento com artifacts ausentes;
- comportamento com drop/backpressure quando relevante;
- comportamento de memoria se gerar artifacts grandes;
- comportamento de annotate vs event se suportar os dois.

---

## 14. Lacunas atuais que valem notar antes de projetar algo novo

- `camera.motion_gate` so conhece movimento por diferenca simples de frames; nao ha background subtraction mais sofisticado, tracking de blobs nem classificacao de tipo de movimento.
- o contrato formal do registry nao descreve outputs de `metadata`, embora alguns operadores dependam deles de fato.
- a UI interativa funciona melhor para operadores explicitamente suportados; novos operadores sem painel dedicado tendem a cair em experiencia mais crua.
- o sistema atual ja separa bem "gate barato" de "semantica de evento". Misturar tudo num operador unico pode parecer conveniente, mas tende a reduzir composabilidade.

---

## 15. Arquivos-chave para consulta rapida

- `src/toposync/app.py`
- `src/toposync/runtime/config_store.py`
- `src/toposync/runtime/pipelines/operator_registry.py`
- `src/toposync/runtime/pipelines/compiler.py`
- `src/toposync/runtime/pipelines/runtime.py`
- `src/toposync/runtime/pipelines/execution.py`
- `src/toposync/runtime/pipelines/operators_core.py`
- `src/toposync/runtime/pipelines/operators_gates.py`
- `src/toposync/runtime/pipelines/operators_sinks.py`
- `src/toposync/runtime/pipelines/recommendations.py`
- `src/toposync/runtime/pipelines/images.py`
- `src/toposync/runtime/pipelines/telemetry.py`
- `src/toposync/runtime/pipelines/step_snapshots.py`
- `src/toposync/runtime/pipelines/migration_legacy_cameras.py`
- `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/motion.py`
- `extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py`
- `extensions/cameras/src/toposync_ext_cameras/plugin.py`
- `extensions/streaming/src/toposync_ext_streaming/pipelines/operators.py`
- `extensions/streaming/src/toposync_ext_streaming/wizard/pipeline_builder.py`
- `frontend/src/ui/screens/pipelines/constants.ts`
- `frontend/src/ui/screens/pipelines/editor/InteractiveStepCard.tsx`
- `frontend/src/ui/screens/pipelines/editor/panels/OperatorConfigPanel.tsx`
- `frontend/src/ui/screens/pipelines/editor/panels/CameraPanels.tsx`

---

## 16. Conclusao pratica

Se o objetivo imediato e planejar novos operadores de deteccao de movimento, a principal leitura do estado atual e:

1. o produto ja tem um gate barato e composavel (`camera.motion_gate`);
2. o runtime ja suporta bem annotate, lifecycle e split-stream;
3. o modelo de contracts/payload/artifacts esta maduro o suficiente para crescer sem hacks;
4. os proximos operadores devem ser desenhados como pecas reutilizaveis do pipeline, e nao como casos especiais embutidos no core;
5. o maior risco nao e "falta de infraestrutura", e sim escolher a semantica errada para o operador: gate, annotate, evento ou split-stream.

Esse e o ponto de partida correto para definir a proxima leva de operadores.
