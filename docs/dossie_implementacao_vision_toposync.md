# Dossiê de implementação — Camada de visão agnóstica a modelo no Toposync

Versão: 1.0  
Data de referência: 2026-03-22  
Destinatário principal: agente de implementação de alta capacidade (GPT-5.4 extra high ou equivalente)  
Escopo: reestruturar a camada de visão computacional do Toposync para suportar múltiplos modelos, múltiplos backends e tracking desacoplado, sem preservar legado, antes do lançamento público.

---

## 0. Como usar este dossiê

Este documento deve ser tratado como especificação de produto + arquitetura + execução.

Regras de trabalho para o agente:

1. **Executar fase por fase, em ordem.**
2. **Não manter compatibilidade legada.** O projeto ainda não foi lançado. É permitido quebrar nomes públicos, mover operadores, renomear conceitos e reorganizar extensões.
3. **Não deixar o build oficial dependente de bibliotecas com licença de alto risco para o cenário do produto.**
4. **Não acoplar visão ao vendor do modelo.** A interface pública deve falar em tarefas (`detect`, `track`, `segment_instances`, `pose_estimate`), não em marcas (`yolo`, `ultralytics`, etc.).
5. **Separar claramente captura de câmera, inferência, tracking, pós-processamento e recursos cloud pagos.**
6. **Cada fase precisa terminar com código funcionando, testes passando, docs atualizadas e critérios de aceite atendidos.**
7. **Não introduzir “atalhos temporários” que virem dívida arquitetural.** Se uma camada de abstração for necessária, criar agora.
8. **Toda decisão nova deve respeitar as restrições descritas em “não negociáveis”.**

---

## 1. Objetivo do trabalho

Criar uma camada de visão computacional no Toposync que seja:

- agnóstica ao modelo
- agnóstica ao runtime de inferência
- desacoplada do tracking embutido do detector
- orientada a tarefas
- segura do ponto de vista de licença para um produto open source com recursos cloud pagos em volta
- simples de usar no editor de pipelines
- escalável para:
  - CPU
  - GPU NVIDIA
  - Apple Silicon / MPS
  - Intel / OpenVINO
  - futuro Edge TPU Coral
- preparada para evoluir para:
  - segmentação real de instância
  - pose
  - tracking multi-câmera
  - reidentificação
  - tracking com coordenadas de mundo

No lançamento, o foco funcional é:

1. **detecção**
2. **tracking**
3. **segmentação**
4. manter compatibilidade com o restante do pipeline de câmeras:
   - motion gating
   - mapping
   - area restriction
   - velocity estimation
   - best frame selection
   - notificação e armazenamento

---

## 2. Não negociáveis

### 2.1 Produto / negócio

- O núcleo do produto é **open source**.
- Haverá recursos de **cloud pagos**, mas esses recursos não são “o modelo em si”. Eles são principalmente:
  - notificação enriquecida
  - armazenamento de capturas
  - revisão de eventos
  - experiências melhores ao redor da detecção
- A experiência local deve continuar forte e independente de cloud.
- O produto ainda não foi lançado. Portanto:
  - **não preservar legado**
  - **não criar alias de operadores antigos**
  - **não manter nomes vendor-specific só para compatibilidade**

### 2.2 Arquitetura

- Câmera e visão devem ser extensões separadas.
- Tracking deve ser camada separada do detector.
- Segmentação por máscara deve existir como conceito real, separado de crop por bbox.
- A interface pública deve ser por tarefa, não por tecnologia proprietária ou nome do vendor.
- O build first-party oficial deve usar apenas componentes com política de licença aceitável para o cenário do produto.

### 2.3 Licença / compliance

- Não usar Ultralytics como base first-party padrão.
- Não usar BoxMOT no build oficial.
- Não usar MMYOLO no build oficial.
- Todo modelo suportado oficialmente deve ter metadados formais de licença e proveniência.
- O sistema precisa diferenciar:
  - licença do código
  - licença dos pesos
  - termos do dataset / proveniência
  - política de redistribuição
  - risco comercial

### 2.4 UX

- O usuário não deve escolher “o framework”. O usuário deve escolher a **tarefa** e, no máximo, um modelo recomendado.
- O editor deve sugerir modelos conforme hardware e prioridade:
  - mais rápido
  - equilibrado
  - maior qualidade
- A experiência avançada pode expor mais detalhes, mas o caminho comum precisa ser simples.

---

## 3. Resultado da pesquisa — estado atual do repositório

### 3.1 O que a extensão `cameras` faz hoje

A extensão atual de câmeras já oferece:

- configuração e indexação de câmeras RTSP
- snapshots RTSP
- PTZ / ONVIF
- mapeamento por control points
- operadores de pipeline para captura, motion, visão e mapeamento
- UI/editor para câmeras
- wizard de pipelines

Os operadores registrados hoje na extensão incluem:

- `camera.source`
- `camera.motion_gate`
- `vision.object_tracking_yolo`
- `vision.object_detection_yolo`
- `camera.object_segmentation`
- `camera.camera_mapping`
- `camera.area_restriction`
- `camera.velocity_estimation`
- `camera.best_frame_selector`

Conclusão:
- a camada de visão **não está mais fora do runtime de pipelines**
- ela já foi incorporada ao fluxo DAG da plataforma
- isso é bom e deve ser preservado
- porém a camada ainda está **nomeada e organizada em torno de YOLO**

Arquivos-base atuais que o agente deve ler antes de mudar o código:

- `extensions/cameras/README.md`
- `extensions/cameras/src/toposync_ext_cameras/plugin.py`
- `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`
- `extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/yolo.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/camera_hub.py`
- `docs/PIPELINES.md`
- `docs/PROCESSING_SERVER_DEVELOPMENT.md`
- `docs/dossie_transmissoes_streaming.md`
- `tests/test_camera_capture_backends.py`
- `tests/test_camera_yolo_track_id_parsing.py`

### 3.2 Contrato de runtime já existente

O runtime do Toposync já trabalha com:

- `Packet`
- `Artifact`
- `Lifecycle(open|update|close)`
- `stream_id`
- `payload`
- `metadata`
- canais bounded
- políticas de drop
- canais keyed após split por stream

Isso é excelente para visão em tempo real.

Implicações:
- a camada nova de visão deve continuar produzindo/consumindo `Packet`
- o tracking deve continuar respeitando lifecycle
- os operadores devem funcionar bem em cenários com backpressure, drop de `update` e preservação de `open/close`

### 3.3 Existe uma abstração embrionária de backend de visão

No arquivo `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py` já existe:

- `YoloObject`
- `YoloBackend`
- `YoloBackendConfig`
- `yolo_backend_factory`
- `_default_yolo_backend_factory()`

Isso é uma boa notícia, porque significa que o projeto já aceita a ideia de injetar outro backend.

Mas os problemas são:

- a abstração ainda é **nominalmente YOLO**
- a implementação default ainda é **Ultralytics**
- o detector “puro” hoje reaproveita internamente o caminho de tracking
- a camada pública ainda expõe operadores com nome vendor-specific

### 3.4 Tracking hoje está embutido no detector

O backend atual usa a API de tracking do Ultralytics e aceita trackers como:

- `bytetrack`
- `botsort`

O tracking atual é relevante para várias partes do produto:

- lifecycle por objeto
- best frame
- area restriction
- velocity estimation
- notificações
- possível downstream de transmissão

Conclusão:
- tracking **não é um detalhe cosmético**
- ele precisa virar um operador de primeira classe, separado do detector

### 3.5 “Segmentation” hoje não é segmentação real

Hoje o operador `camera.object_segmentation` é, na prática, um **crop por bbox**.  
Ele não produz máscara de instância real.

Isso precisa ser corrigido conceitualmente:

- `camera.object_crop` = crop por bbox
- `vision.segment_instances` = máscara real de instância

### 3.6 Captura RTSP e ingest já estão bem encaminhados

A camada atual de captura possui:

- backend `opencv`
- backend `ffmpeg`
- modo `auto`
- métricas de captura
- reopen / fallback
- `CameraHub` compartilhado para evitar múltiplas conexões RTSP por câmera
- possibilidade de usar ingest via serviço `streaming.ingest.resolve_rtsp_url`

Conclusão:
- a nova camada de visão **não deve reimplementar captura**
- ela deve consumir o frame já produzido por `camera.source`
- `camera.source` deve permanecer na extensão `cameras`

### 3.7 O wizard atual já mostra o fluxo de produto

Os presets atuais (`people`, `pets`, `vehicles_stopped`) deixam claro que visão no produto não é só bbox:

- existe tracking por objeto
- existe mapping para mundo
- existe restrição por área
- existe estimativa de velocidade
- existe seleção de melhor frame
- existe integração com store + notify

Conclusão:
- a nova arquitetura de visão precisa preservar esses encadeamentos
- a decisão de detector e tracker deve ser avaliada pelo encaixe no pipeline inteiro, não apenas mAP/FPS

### 3.8 Restrições distribuídas importantes

O dossiê interno de transmissões e a documentação de pipelines mostram que:

- existem processing servers remotos
- o split distribuído atual suporta bem o fluxo processing -> origin
- origin -> processing ainda não é o caminho forte hoje
- `camera.source` pode se beneficiar de ingest
- sinks como armazenamento e notificação podem acontecer em lados diferentes conforme a arquitetura

Conclusão:
- a nova camada de visão precisa funcionar:
  - localmente
  - em processing server remoto
- a decisão de runtime de inferência precisa ser compatível com este desenho

---

## 4. Resultado da pesquisa — licença e risco

### 4.1 Por que Ultralytics não deve ser first-party oficial

A Ultralytics declara, em sua política pública de licenciamento, que:

- o caminho open source padrão é **AGPL-3.0**
- existe uma licença **Enterprise** para cenários comerciais / proprietários
- os modelos treinados da família deles também entram nesse regime padrão

Para o cenário deste produto, isso cria risco desnecessário, porque:

- o produto terá componentes pagos por rede
- o projeto quer um núcleo open source com extensões comunitárias e privadas
- não é desejável depender juridicamente de uma licença copyleft forte para a stack first-party principal

Importante:
- isso **não é** parecer jurídico definitivo
- mas é uma base suficiente para uma decisão técnica conservadora:
  - **não usar Ultralytics no build first-party oficial**
  - permitir Ultralytics apenas como extensão opcional separada

### 4.2 Componentes com política de licença favorável

Stack preferencial para o build oficial:

- **ONNX Runtime** — MIT
- **OpenCV 4.5+** — Apache 2.0
- **MMDetection** — Apache 2.0
- **MMPose** — Apache 2.0
- **OpenVINO** — Apache 2.0
- **PyCoral** — Apache 2.0
- **YOLOX** — Apache 2.0
- **Norfair** — BSD-3-Clause
- **BoT-SORT** — MIT

### 4.3 Componentes a evitar no build oficial

- **Ultralytics** — AGPL-3.0 / Enterprise
- **BoxMOT** — AGPL-3.0
- **MMYOLO** — GPL-3.0

### 4.4 Política de compliance obrigatória

Todo modelo suportado oficialmente precisa ter um manifesto com:

- licença do código
- licença dos pesos
- origem dos pesos
- origem do dataset / observações de proveniência
- se redistribuição é permitida
- se uso comercial é permitido
- se o modelo pode entrar no build oficial
- se o modelo só pode ser baixado sob aceite explícito do usuário
- hash do artefato

**Não assumir que “licença do código = licença dos pesos”.**  
Isso precisa ser tratado como regra do produto.

---

## 5. Decisões arquiteturais já tomadas neste plano

### 5.1 Extensões

#### Permanecem / ficam dedicadas a câmera
- `com.toposync.cameras`

#### Nova extensão first-party oficial
- `com.toposync.vision`

#### Extensões opcionais / separadas
- `com.toposync.vision_ultralytics`
- `com.toposync.vision_yolox`
- `com.toposync.vision_custom_onnx`
- `com.toposync.vision_custom_tflite`
- futuro: `com.toposync.vision_botsort`

### 5.2 Operadores públicos do produto

Operadores públicos novos:

- `vision.detect`
- `vision.track`
- `vision.segment_instances`
- futuro: `vision.pose_estimate`

Operadores que permanecem em `cameras`:

- `camera.source`
- `camera.motion_gate`
- `camera.motion_bgsub_adaptive`
- `camera.motion_sample_bg`
- `camera.camera_mapping`
- `camera.area_restriction`
- `camera.velocity_estimation`
- `camera.best_frame_selector`
- preprocessamentos
- crop de imagem

Operador renomeado:

- `camera.object_segmentation` -> `camera.object_crop`

### 5.3 Backend first-party principal

**Runtime de inferência principal:** ONNX Runtime

Motivos:
- licença permissiva
- execution providers para CPU, CUDA, TensorRT, OpenVINO, CoreML, XNNPACK e outros
- bom caminho multiplataforma
- desacopla o produto do framework de treino

### 5.4 Família de modelos first-party do lançamento

Família principal:

- **RTMDet** para detecção
- **RTMDet-Ins** para segmentação de instância
- **RTMPose** como caminho futuro coerente

Decisão de produto:
- escolher **uma família principal first-party**, não várias
- simplifica UX
- simplifica QA
- simplifica compatibilidade entre detecção, segmentação e pose

### 5.5 Tracking desacoplado

Decisão:
- tracking deixa de ser parte embutida da arquitetura default do detector
- tracking vira operador próprio

Primeiros trackers first-party:

- `simple_iou_kalman`
- `norfair`

Tracker avançado opcional posterior:

- `botsort`

### 5.6 Segmentação real vs crop

Decisão:
- manter crop por bbox como operador de câmera / pós-processamento
- adicionar segmentação real por máscara na nova extensão de visão

### 5.7 Nada de nomes vendor-specific no núcleo

Remover do núcleo oficial:

- `vision.object_tracking_yolo`
- `vision.object_detection_yolo`

Não manter alias.
Não manter compatibilidade.

---

## 6. Arquitetura alvo

### 6.1 Visão geral do pipeline alvo

Fluxo recomendado:

`camera.source -> camera.motion_* -> vision.detect -> vision.track -> camera.camera_mapping -> camera.area_restriction -> camera.velocity_estimation -> camera.best_frame_selector / camera.object_crop / vision.segment_instances -> sinks`

Observação:
- `vision.segment_instances` pode rodar:
  - antes de tracking, se o modelo já produz instâncias por frame
  - depois de tracking, se quiser usar detecções/ROIs como base
- a primeira versão deve priorizar simplicidade:
  - detecção
  - tracking
  - segmentação por instância opcional
  - pose fica para fase futura, mas o contrato deve deixar espaço desde já

### 6.2 Três camadas distintas

#### Camada 1 — captura
Responsável por:
- RTSP
- ingest
- PTZ
- motion gating
- preprocessamento
- frame artifacts

Extensão:
- `com.toposync.cameras`

#### Camada 2 — percepção visual
Responsável por:
- detectar
- rastrear
- segmentar
- estimar pose (futuro)

Extensão:
- `com.toposync.vision`

#### Camada 3 — raciocínio espacial / temporal do domínio
Responsável por:
- mapping
- área
- velocidade
- melhor frame
- notificação
- armazenamento
- transmissão

Extensões:
- `com.toposync.cameras`
- `core`
- `streaming`

---

## 7. Contratos internos obrigatórios

### 7.1 Conceito central

A nova camada de visão deve trabalhar com contratos explícitos de tarefa.

Ela não deve depender de “como o modelo representa internamente”.

### 7.2 Contrato de detecção

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass(slots=True)
class DetectionObject:
    label: str
    label_id: int | None
    score: float
    bbox01: tuple[float, float, float, float]
    model_id: str
    mask_artifact_name: str | None = None
    keypoints: list[tuple[float, float, float]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

Regras:
- `bbox01` sempre em coordenadas normalizadas no frame corrente
- `score` sempre em `[0,1]`
- `label` sempre normalizado para lowercase no contrato interno
- `mask_artifact_name` opcional
- `keypoints` opcional
- `metadata` guarda detalhes específicos do backend sem poluir o contrato público

### 7.3 Contrato de track

```python
@dataclass(slots=True)
class TrackedObject:
    tracking_id: str
    source_tracking_id: str | None
    label: str
    label_id: int | None
    score: float
    bbox01: tuple[float, float, float, float]
    model_id: str
    tracker_id: str
    mask_artifact_name: str | None = None
    world_anchor: dict[str, float] | None = None
    appearance_embedding_artifact_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

Regras:
- `tracking_id` é o identificador canônico do Toposync
- `source_tracking_id` é opcional e vem do tracker/modelo
- `world_anchor` fica reservado para futura fusão multi-câmera
- embeddings são opcionais e não entram na primeira fase do lançamento

### 7.4 Contrato de instância segmentada

```python
@dataclass(slots=True)
class SegmentationInstance:
    label: str
    label_id: int | None
    score: float
    bbox01: tuple[float, float, float, float]
    mask_artifact_name: str
    polygon01: list[tuple[float, float]] | None = None
    model_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 7.5 Contrato de pose

Reservar desde já, mesmo se não entrar no lançamento.

```python
@dataclass(slots=True)
class PoseObject:
    label: str
    score: float
    bbox01: tuple[float, float, float, float]
    keypoints: list[tuple[float, float, float]]
    model_id: str
    tracking_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 7.6 Representação no `Packet`

#### Modo annotate

`vision.detect` deve anotar o packet com algo como:

```json
{
  "vision": {
    "detections": [
      {
        "label": "person",
        "label_id": 0,
        "score": 0.93,
        "bbox01": [0.12, 0.08, 0.44, 0.91],
        "model_id": "rtmdet_det_small"
      }
    ],
    "task": "detection",
    "model_id": "rtmdet_det_small",
    "runtime": "onnxruntime"
  }
}
```

#### Modo events de tracking

`vision.track` em modo `events` deve produzir `Packet.create(...)` com:

- `stream_id` por objeto
- `lifecycle`
- `tracking_id`
- `source_stream_id`
- `object_category_label`
- `object_confidence`
- `object_bbox01`
- `detected_object`
- `detected_objects`

Importante:
- manter a ergonomia que já funciona no runtime atual
- mas sem depender de nomes “yolo”

### 7.7 Compatibilidade com crop / warp

Os operadores de visão precisam continuar respeitando que o frame pode ter sido alterado por:

- crop
- perspective crop / warp
- resize
- adjust
- CLAHE
- sharpen
- denoise
- auto gamma
- stabilize
- undistort

Portanto:
- o contrato de bbox deve seguir a mesma filosofia atual:
  - bbox referente ao frame corrente
  - helpers de reproject / uncrop / unwarp continuam necessários

---

## 8. Interfaces internas a implementar

### 8.1 Detector backend

```python
class DetectorBackend(Protocol):
    backend_id: str

    def detect(
        self,
        frame: Any,
        *,
        categories: set[str] | None = None,
    ) -> list[DetectionObject]:
        ...
```

### 8.2 Tracker backend

```python
class TrackerBackend(Protocol):
    tracker_id: str

    def reset_stream(self, stream_key: str) -> None:
        ...

    def update(
        self,
        stream_key: str,
        frame: Any,
        detections: list[DetectionObject],
        *,
        frame_ts: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[TrackedObject]:
        ...
```

### 8.3 Segmentation backend

```python
class SegmentationBackend(Protocol):
    backend_id: str

    def segment(
        self,
        frame: Any,
        *,
        detections: list[DetectionObject] | None = None,
        categories: set[str] | None = None,
    ) -> list[SegmentationInstance]:
        ...
```

### 8.4 Pose backend

```python
class PoseBackend(Protocol):
    backend_id: str

    def estimate_pose(
        self,
        frame: Any,
        *,
        detections: list[DetectionObject] | None = None,
    ) -> list[PoseObject]:
        ...
```

### 8.5 Runtime factory

```python
class VisionRuntimeFactory(Protocol):
    def build_detector(self, manifest: "ModelManifest") -> DetectorBackend: ...
    def build_segmenter(self, manifest: "ModelManifest") -> SegmentationBackend: ...
    def build_pose(self, manifest: "ModelManifest") -> PoseBackend: ...
```

---

## 9. Registro de modelos e manifesto

### 9.1 Objetivo

O usuário não deve “subir pesos soltos” sem contexto.

O sistema precisa trabalhar com um registro formal de modelos.

### 9.2 Manifesto obrigatório

Formato recomendado: JSON ou YAML.

Campos mínimos:

```yaml
model_id: rtmdet_det_small
display_name: RTMDet Small
task: detection
runtime: onnxruntime
artifact_format: onnx
artifact_path: models/rtmdet/rtmdet-small.onnx
sha256: "<hash>"
input:
  width: 640
  height: 640
  color_order: rgb
  layout: nchw
  normalization:
    mean: [0.0, 0.0, 0.0]
    std: [255.0, 255.0, 255.0]
postprocess:
  type: mmdet_rtmdet
  confidence_threshold_default: 0.4
  iou_threshold_default: 0.6
classes:
  source: coco80
  labels:
    - person
    - bicycle
    - car
license:
  code_license: Apache-2.0
  weights_license: "<preencher explicitamente>"
  dataset_notes: "<preencher explicitamente>"
  redistribution_allowed: false
  commercial_use_status: review_required
  official_build_allowed: false
hardware_profiles:
  cpu: true
  cuda: true
  openvino: true
  mps: false
recommended_profiles:
  - cpu_balanced
  - cuda_balanced
notes:
  - "usar para detecção geral"
```

### 9.3 Política de build oficial

O build oficial first-party só deve listar como “oficial” modelos cujo manifesto tenha:

- `official_build_allowed: true`
- campos de licença completos
- hash preenchido
- parser e runtime testados
- pelo menos um perfil de hardware validado

### 9.4 Modelos customizados

Modelos customizados do usuário entram pelo mesmo registro.

Caminho:
- usuário importa manifesto
- usuário aponta o artefato
- o modelo aparece nos operadores comuns da tarefa

Não criar operador público separado só para “modelo custom”.

---

## 10. Estratégia de licenciamento do produto

### 10.1 Regra de ouro

**A distribuição oficial do Toposync deve ser limpa do ponto de vista de licença.**

O que isso significa:

- nada de AGPL/GPL no build oficial first-party de visão
- extensões de alto risco só entram:
  - como pacote separado
  - não instalado por padrão
  - com aviso de licença explícito
  - idealmente fora do repositório first-party principal

### 10.2 Política de repositórios

Sugestão:

#### Repositório principal / distribuição oficial
- `toposync`
- contém `cameras`, `vision`, `streaming`, etc.
- somente stacks first-party permissivas

#### Repositório opcional privado ou separado
- `toposync-vision-ultralytics`
- `toposync-vision-boxmot` (se algum dia existir, ainda assim fora da distro oficial)
- qualquer integração juridicamente sensível

### 10.3 Recursos cloud pagos

Política desejada:
- cloud paga **não** executa obrigatoriamente a detecção para o caso comum
- cloud paga acrescenta:
  - notificação
  - armazenamento
  - replay
  - UX melhor
  - coordenação
- isso reduz acoplamento jurídico entre inferência e serviço de rede

---

## 11. Estrutura de pastas proposta

### 11.1 Nova extensão `vision`

```text
extensions/vision/
  pyproject.toml
  README.md
  src/toposync_ext_vision/
    plugin.py
    api/
      models.py
      routes.py
    registry/
      manifests.py
      model_store.py
      recommendations.py
      compatibility.py
    processing/
      contracts.py
      artifact_helpers.py
      diagnostics.py
      runtime_backends/
        __init__.py
        onnxruntime_backend.py
        openvino_backend.py
        tflite_backend.py
      parsers/
        __init__.py
        rtmdet_parser.py
        rtmdet_ins_parser.py
        generic_onnx_boxes_parser.py
      trackers/
        __init__.py
        simple_iou_kalman.py
        norfair_tracker.py
      tasks/
        detection.py
        tracking.py
        segmentation.py
        pose.py
    pipelines/
      operators.py
      schemas.py
    ui/
      src/...
```

### 11.2 Alterações em `cameras`

#### Permanecem
- `camera.source`
- motion
- mapping
- area
- velocity
- best frame
- preprocessamentos

#### Mudanças
- remover registro de `vision.object_tracking_yolo`
- remover registro de `vision.object_detection_yolo`
- renomear `camera.object_segmentation` para `camera.object_crop`

### 11.3 Extensão opcional `vision_ultralytics`

```text
extensions/vision_ultralytics/
  pyproject.toml
  README.md
  src/toposync_ext_vision_ultralytics/
    plugin.py
    runtime_backends/
      ultralytics_detector.py
      ultralytics_segmenter.py
    trackers/
      ultralytics_tracking_adapter.py
```

Observação:
- não é parte do primeiro release oficial
- não deve ser dependência do core oficial
- pode até viver fora do repo principal

---

## 12. Operadores públicos — especificação

### 12.1 `vision.detect`

#### Objetivo
Executar detecção de objetos e produzir eventos finitos, filtro de frames ou anotação no packet.

#### Inputs
- `in`

#### Outputs
- `out`

#### Config
- `model_id: str`
- `emit_mode: "events" | "filter" | "annotate"`
- `categories: list[str]`
- `confidence_threshold: float`
- `iou_threshold: float`
- `max_objects_per_frame: int`
- `inference_interval_seconds: float`
- `input_with_fallback: str`
- `fallback_to_stream_frame: bool`

#### Produz
- `vision.detections`
- `detected_objects` (campo compatível com ecossistema downstream, se útil)
- telemetria de confiança

#### Observações
- `events` emite evento curto `open→close` por detecção.
- `filter` mantém só frames com detecção.
- `annotate` mantém todos os frames com detecções anexadas.
- lifecycle temporal por objeto, movimento e trail continuam em `vision.track`.

### 12.2 `vision.track`

#### Objetivo
Consumir detecções e produzir identidade temporal.

#### Inputs
- `in`

#### Outputs
- `out`

#### Config
- `tracker_id: str`
- `emit_mode: "annotate" | "events"`
- `close_after_seconds: float`
- `pause_when_gate_closed: bool`
- `max_paused_seconds: float`
- `default_interval_seconds: float`
- `category_intervals_seconds: dict[str, float]`
- `use_world_anchor: bool`  
  reservado para futuro; no lançamento pode ser ignorado se não houver `world`

#### Observações
- `annotate`: anota o frame com tracks
- `events`: cria streams por objeto com lifecycle
- precisa continuar compatível com:
  - `camera.best_frame_selector`
  - `camera.velocity_estimation`
  - `camera.area_restriction`
  - notificação
  - armazenamento

### 12.3 `vision.segment_instances`

#### Objetivo
Produzir máscaras reais de instância.

#### Inputs
- `in`

#### Outputs
- `out`

#### Config
- `model_id: str`
- `categories: list[str]`
- `input_with_fallback`
- `fallback_to_stream_frame`
- `attach_mask_artifacts: bool`
- `attach_polygons: bool`
- `max_instances_per_frame: int`

#### Observações
- pode consumir `detections` existentes ou rodar full-frame
- deve produzir artifacts para máscara, quando habilitado
- o lançamento não precisa de recorte “bonitinho” por alpha compositing; máscara binária já basta

### 12.4 `camera.object_crop`

#### Objetivo
Substituir o operador mal nomeado atual.

#### Papel
- recortar por bbox
- não é segmentação
- continuar útil para:
  - best frame
  - notificação
  - armazenamento
  - transmissão

---

## 13. Trackers do lançamento

### 13.1 `simple_iou_kalman`

Objetivo:
- tracker leve
- fácil de manter
- sem dependência extra pesada
- ideal para CPU / cenários simples

Requisitos:
- matching por IoU
- movimento via Kalman
- parâmetros simples
- suporte a `close_after_seconds`
- suporte a pausa por gate

### 13.2 `norfair`

Objetivo:
- tracker principal mais sofisticado e still permissivo
- detector-agnóstico
- espaço para usar bbox hoje e features melhores amanhã

Requisitos:
- adaptador próprio
- modo bbox simples no lançamento
- deixar hooks prontos para:
  - keypoints
  - embeddings
  - world-aware distance

### 13.3 `botsort` posterior / opcional

Objetivo:
- extensão avançada
- melhor robustez de identidade em crowd / oclusão
- não necessário para lançar

Não colocar no caminho crítico do lançamento.

---

## 14. Modelos do lançamento

### 14.1 Modelos first-party recomendados

#### Detecção
- `rtmdet_det_tiny`
- `rtmdet_det_small`
- `rtmdet_det_medium`

#### Segmentação de instância
- `rtmdet_ins_small`
- `rtmdet_ins_medium`

### 14.2 Perfis de recomendação ao usuário

Perfis conceituais:

- `cpu_low`
- `cpu_balanced`
- `cuda_low`
- `cuda_balanced`
- `cuda_quality`
- `openvino_balanced`
- futuro: `coral_edge`

Os modelos devem ser etiquetados para esses perfis.

### 14.3 Política de UI

O usuário escolhe:
1. tarefa
2. prioridade:
   - velocidade
   - equilíbrio
   - qualidade
3. opcionalmente um modelo específico

A plataforma recomenda modelos conforme:
- providers disponíveis
- device detectado
- número de câmeras
- resolução
- categoria de tarefa

---

## 15. UX alvo

### 15.1 Caminho simples

Ao adicionar `vision.detect`:

Perguntar:
- o que detectar?
- onde isso vai rodar?
- qual prioridade?

Exemplo de UX:
- “Pessoas”
- “Neste computador”
- “Equilíbrio”

Então sugerir:
- modelo recomendado
- alternativa mais rápida
- alternativa mais precisa

### 15.2 Caminho avançado

Permitir ver:
- runtime
- provider
- input size
- classes
- licença do código
- licença dos pesos
- status comercial
- uso estimado de recurso

### 15.3 Importação de modelos customizados

Fluxo:
1. importar manifesto
2. apontar artefato
3. validar compatibilidade
4. registrar no model store
5. aparecer automaticamente nos operadores da tarefa

---

## 16. Diagnóstico e recomendação por hardware

### 16.1 O que já existe

O Toposync já tem diagnóstico útil no processing server:
- CPU
- RAM
- torch / CUDA / MPS
- opencv
- ffmpeg
- camera hub

### 16.2 O que precisa ser adicionado

Em `com.toposync.vision`, adicionar diagnóstico de:

- ONNX Runtime instalado?
- execution providers disponíveis?
- OpenVINO disponível?
- TFLite / Edge TPU disponível?
- modelos instalados
- manifestos válidos
- benchmark curto opcional por modelo

### 16.3 Recomendador

Criar um recomendador simples com regras heurísticas, não ML:

Entradas:
- OS / arquitetura
- CPU cores
- RAM
- CUDA / GPU detectada
- OpenVINO
- Coral
- número de câmeras e FPS alvo

Saídas:
- shortlist de modelos por tarefa
- badges:
  - recomendado
  - mais rápido
  - melhor qualidade
  - edge / baixo consumo

---

## 17. Plano de execução por fases

# Fase 0 — cirurgia de arquitetura e nomenclatura

## Objetivo
Separar visão de câmera e eliminar dependência conceitual de YOLO no núcleo.

## Entregas
- criar `extensions/vision`
- mover registro de operadores de visão para essa extensão
- remover operadores públicos `vision.object_tracking_yolo` e `vision.object_detection_yolo`
- renomear `camera.object_segmentation` para `camera.object_crop`
- atualizar docs e wizards
- garantir que nada no núcleo oficial fale em YOLO como tarefa pública

## Mudanças esperadas
- novos arquivos de extensão
- alteração do registro de operadores em `cameras`
- atualização de presets do wizard
- atualização dos docs

## Critérios de aceite
- build sobe sem os operadores antigos
- nenhuma rota/UI principal oficial depende dos nomes antigos
- pipeline editor mostra os novos operadores
- documentação já reflete a nova taxonomia

---

# Fase 1 — contratos de visão e operador `vision.detect`

## Objetivo
Introduzir contratos genéricos e um operador de detecção desacoplado do vendor.

## Entregas
- `DetectionObject`
- `ModelManifest`
- `DetectorBackend`
- model registry
- `vision.detect`
- backend stub / fake para testes
- modo `annotate`

## Mudanças esperadas
- camada de contratos em `extensions/vision/processing/contracts.py`
- parsing de config
- anotação no `Packet`

## Critérios de aceite
- `vision.detect` funciona com backend fake
- payload/anotações documentadas
- testes unitários de contrato e validação
- compatibilidade com preprocessamento de frame

---

# Fase 2 — backend ONNX Runtime

## Objetivo
Criar runtime first-party principal.

## Entregas
- `onnxruntime_backend.py`
- discovery de execution providers
- diagnóstico
- carregamento de manifestos
- parser genérico de detecção por bbox

## Critérios de aceite
- backend sobe em CPU
- provider detection funciona
- é possível carregar ao menos um modelo ONNX simples de teste
- benchmark básico retorna métricas

---

# Fase 3 — RTMDet detecção

## Objetivo
Colocar a primeira família first-party real em produção local.

## Entregas
- parser RTMDet
- manifestos dos modelos de detecção
- shortlist de modelos oficiais
- UX de recomendação inicial

## Critérios de aceite
- detectar pessoas e objetos COCO em fluxo real
- funcionar em CPU pelo menos
- bbox/anotação corretas após crop/warp
- qualidade suficiente para alimentar `camera.object_crop` e downstream

---

# Fase 4 — operador `vision.track`

## Objetivo
Desacoplar tracking do detector.

## Entregas
- `TrackedObject`
- `TrackerBackend`
- `vision.track`
- `simple_iou_kalman`
- `norfair`
- modos `annotate` e `events`

## Critérios de aceite
- tracking funciona com RTMDet
- `events` produz lifecycle correto
- `annotate` produz lista de tracks no packet
- `camera.best_frame_selector` continua operando
- `camera.velocity_estimation` continua operando

---

# Fase 5 — RTMDet-Ins e `vision.segment_instances`

## Objetivo
Adicionar segmentação real de instância.

## Entregas
- parser RTMDet-Ins
- manifestos de segmentação
- `vision.segment_instances`
- artifacts de máscara

## Critérios de aceite
- máscaras produzidas corretamente
- bbox + máscara coexistem
- `camera.object_crop` continua existindo e separado semanticamente
- downstream de armazenamento consegue escolher bbox crop ou máscara

---

# Fase 6 — UX e recomendação por máquina

## Objetivo
Fazer o sistema simples para o usuário comum.

## Entregas
- recomendador por hardware
- badges de recomendação
- UI de escolha por tarefa
- importação de manifesto customizado

## Critérios de aceite
- usuário consegue configurar detecção sem entender framework
- a UI recomenda modelos viáveis para a máquina
- modelos incompatíveis são escondidos ou marcados como indisponíveis

---

# Fase 7 — extensão opcional Ultralytics

## Objetivo
Permitir uso opcional, sem contaminar o build oficial.

## Entregas
- extensão separada
- runtime backend próprio
- documentação e aviso explícito de licença

## Critérios de aceite
- não é dependência do build oficial
- instalação é opcional
- UI mostra claramente que é extensão opcional / licença separada

---

# Fase 8 — preparar pose

## Objetivo
Não lançar pose ainda, mas deixar pronto.

## Entregas
- contrato `PoseObject`
- `vision.pose_estimate` skeleton
- model registry com `task=pose`
- hooks no tracker para keypoints futuros

## Critérios de aceite
- nenhuma quebra estrutural será necessária para adicionar pose depois

---

# Fase 9 — base para tracking multi-câmera futuro

## Objetivo
Não implementar agora, mas deixar os ganchos certos.

## Entregas estruturais
- todo track carrega `camera_id`
- track pode carregar `world_anchor`
- contrato aceita `appearance_embedding_artifact_name`
- model registry aceita capacidade `reid`

## Critérios de aceite
- arquitetura não bloqueia multi-câmera no futuro

---

## 18. O que **não** fazer agora

- não usar Ultralytics como base first-party padrão
- não depender de tracking embutido do detector como arquitetura principal
- não manter operadores antigos por compatibilidade
- não chamar crop por bbox de “segmentação”
- não introduzir open-vocabulary no lançamento oficial principal
- não tentar resolver multi-câmera agora
- não tornar cloud necessária para o fluxo local
- não misturar pesos/modelos oficiais sem manifesto de licença

---

## 19. Mudanças esperadas no wizard de câmeras

### 19.1 Presets

Os presets existentes devem migrar conceitualmente para:

#### people
`camera.source -> motion -> vision.detect(model=...) -> vision.track(tracker=...) -> camera.camera_mapping -> throttle -> camera.object_crop -> store -> notify`

#### pets
igual, com categorias `cat`, `dog`

#### vehicles_stopped
`camera.source -> motion -> vision.detect -> vision.track -> camera.camera_mapping -> camera.area_restriction -> camera.velocity_estimation -> velocity_throttle -> camera.object_crop -> store -> notify`

### 19.2 Regras
- wizard não deve mais inserir operadores vendor-specific
- wizard deve escolher modelo recomendado por perfil da máquina
- wizard deve permitir override avançado

---

## 20. Estratégia de testes

### 20.1 Unitários

Cobrir:

- validação de manifesto
- contratos de detecção / track / segmentação
- parsers de saída
- recomendações por hardware
- normalização de bbox
- reproject / uncrop / unwarp

### 20.2 Integração

Cobrir:

- `camera.source -> vision.detect`
- `camera.source -> vision.detect -> vision.track`
- `... -> camera.camera_mapping -> camera.velocity_estimation`
- `... -> camera.object_crop`
- `... -> vision.segment_instances`
- `... -> store / notify`

### 20.3 Regressão de pipeline

Criar fixtures de pipeline e snapshots de payload/artifacts.

### 20.4 Cross-platform smoke

Validar pelo menos:
- Linux CPU
- macOS Apple Silicon
- Windows CPU
- Linux CUDA

### 20.5 Performance

Benchmarks simples por:
- 1 câmera
- 2 câmeras
- 4 câmeras
- detecção apenas
- detecção + tracking
- detecção + tracking + segmentação

### 20.6 Compliance tests

Verificar:
- manifestos completos
- modelo oficial sem licença -> falha
- extensão opcional AGPL/GPL não entra em build oficial

---

## 21. Estratégia de packaging

### 21.1 Grupos de dependência sugeridos

No `uv` / `pyproject`:

- `vision-core`
  - onnxruntime
  - numpy
  - opencv-python-headless
- `vision-openvino`
  - openvino
- `vision-coral`
  - pycoral / tflite stack
- `vision-dev`
  - ferramentas de benchmark / testes
- `vision-ultralytics` (se existir, fora do core ou opcional explícito)

### 21.2 Política
- instalar `vision-core` por padrão no ambiente de desenvolvimento do core
- providers extras por grupo opcional
- build oficial não deve puxar grupo AGPL/GPL automaticamente

---

## 22. Telemetria e diagnósticos

### 22.1 Novos diagnósticos a expor

Por processing server:

- `vision.backends`
- `vision.execution_providers`
- `vision.models_installed`
- `vision.model_registry_errors`
- `vision.last_benchmark`
- `vision.trackers_available`

### 22.2 Métricas úteis

- latência de inferência
- FPS efetivo
- score médio de detecção
- número de objetos por frame
- número de tracks ativos
- taxa de close por timeout
- taxa de perda de track
- consumo de artifacts em memória
- masks geradas por frame (quando segmentação)

---

## 23. Política de comunidade / extensões

### 23.1 Objetivo

A comunidade deve conseguir criar:

- novos backends
- novos modelos
- novas tarefas
- integrações privadas

### 23.2 Contrato mínimo para extensão de visão

Uma extensão comunitária deve poder registrar:

- runtime backend
- parser
- manifestos
- tracker backend opcional

### 23.3 O que o core oficial precisa fornecer

- APIs de registro
- contratos
- hooks de diagnóstico
- documentação de extensão

---

## 24. Questões abertas que o agente **não** precisa bloquear

Estas questões podem ser registradas, mas não devem impedir as fases iniciais:

- benchmark exato dos melhores tamanhos de RTMDet por hardware real
- se Norfair será o tracker default definitivo ou só o primeiro tracker avançado
- política final de redistribuição de pesos oficiais
- quando pose entra no roadmap público
- quando re-id / global tracking entram

---

## 25. Critério de “pronto para lançar”

A camada nova estará pronta para o lançamento quando, no mínimo:

1. `camera.source` continua robusto
2. `vision.detect` funciona com modelos first-party oficiais permissivos
3. `vision.track` funciona desacoplado do detector
4. `camera.object_crop` substitui corretamente o antigo “segmentation”
5. `vision.segment_instances` funciona com pelo menos um modelo oficial
6. o usuário consegue configurar isso pela UI com recomendação simples
7. não há dependência oficial obrigatória de Ultralytics / BoxMOT / MMYOLO
8. processing server consegue diagnosticar backends e providers
9. docs oficiais e wizard já usam a arquitetura nova
10. não há código legado público exigindo manutenção

---

## 26. Ordem exata que o agente deve seguir

1. Ler os arquivos-base atuais listados na seção 3.
2. Implementar Fase 0 completa.
3. Parar e validar:
   - nomes públicos
   - docs
   - build
4. Implementar Fase 1.
5. Validar contratos e testes.
6. Implementar Fase 2.
7. Validar runtime ONNX.
8. Implementar Fase 3.
9. Validar detecção real.
10. Implementar Fase 4.
11. Validar tracking desacoplado.
12. Implementar Fase 5.
13. Validar segmentação real.
14. Implementar Fase 6.
15. Só então iniciar extensões opcionais.

---

## 27. Instruções finais ao agente

### Faça
- seja conservador com licença
- seja agressivo com remoção de legado
- mantenha operadores pequenos e composáveis
- preserve a semântica boa do runtime atual (`Packet`, lifecycle, stream split)
- use contracts claros
- escreva testes desde a primeira fase
- atualize wizard e docs cedo

### Não faça
- não esconda vendor-specific decisions no core
- não deixe tracking acoplado ao detector por comodidade
- não use nomes antigos por “facilidade temporária”
- não trate weights como se fossem automaticamente livres só porque o repo do código é permissivo
- não amarre cloud ao caminho local

---

## 28. Resumo executivo final

A decisão arquitetural deste plano é:

- separar `cameras` de `vision`
- remover YOLO como conceito público do núcleo
- usar ONNX Runtime como backend first-party principal
- usar RTMDet / RTMDet-Ins como família oficial inicial
- desacoplar tracking do detector
- corrigir o conceito de segmentação
- permitir extensões opcionais para stacks de licença mais delicada
- preparar desde já a evolução para pose e tracking multi-câmera

Isso entrega um núcleo mais:
- limpo
- seguro juridicamente
- multiplataforma
- amigável para usuário
- amigável para comunidade
- pronto para crescer sem retrabalho estrutural

---

## 29. Referências externas usadas para orientar as decisões

Observação:
- estas referências servem como base de licenciamento e stack.
- o agente pode revalidá-las no momento da implementação, mas deve partir das decisões já fixadas neste documento.

### Licenciamento / stacks
- Ultralytics licensing:
  - https://www.ultralytics.com/license
  - https://www.ultralytics.com/legal/enterprise-software-license
- ONNX Runtime:
  - https://github.com/microsoft/onnxruntime
- OpenCV:
  - https://github.com/opencv/opencv
- MMDetection:
  - https://github.com/open-mmlab/mmdetection
- MMPose:
  - https://github.com/open-mmlab/mmpose
- MMYOLO:
  - https://github.com/open-mmlab/mmyolo
- YOLOX:
  - https://github.com/Megvii-BaseDetection/YOLOX
- OpenVINO:
  - https://github.com/openvinotoolkit/openvino
- PyCoral:
  - https://github.com/google-coral/pycoral
- Norfair:
  - https://github.com/tryolabs/norfair
- BoT-SORT:
  - https://github.com/NirAharon/BoT-SORT
- BoxMOT:
  - https://github.com/mikel-brostrom/boxmot

---

## 30. Referências internas do repositório que fundamentam o plano

- `extensions/cameras/README.md`
- `extensions/cameras/src/toposync_ext_cameras/plugin.py`
- `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`
- `extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/yolo.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/camera_hub.py`
- `docs/PIPELINES.md`
- `docs/PROCESSING_SERVER_DEVELOPMENT.md`
- `docs/dossie_transmissoes_streaming.md`
- `tests/test_camera_capture_backends.py`
- `tests/test_camera_yolo_track_id_parsing.py`

---

## 31. Pedido operacional sugerido ao agente

Texto-base sugerido para disparar o trabalho:

> Siga este dossiê como especificação de arquitetura e execução.  
> Trabalhe fase por fase, em ordem, sem manter compatibilidade legada.  
> O projeto ainda não foi lançado.  
> Priorize build oficial com licenças permissivas, ONNX Runtime como backend principal, RTMDet/RTMDet-Ins como família oficial, tracking desacoplado do detector e nova extensão `com.toposync.vision`.  
> A cada fase:
> 1. implemente
> 2. rode testes
> 3. valide critérios de aceite
> 4. atualize docs
> 5. descreva exatamente o que mudou
> Não introduza atalhos arquiteturais temporários.
