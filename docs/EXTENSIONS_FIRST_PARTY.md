# Extensões inclusas no repo

## `extensions/structural`

- Paredes/áreas: ferramentas 2D + render 3D.

## `extensions/models`

- Importar GLB/GLTF + prévia 2D + render 3D.
- Faz upload para o backend via `POST /api/files/upload` (salva em `<data_dir>/files/<dir>/...`).
- Gera uma prévia PNG top‑down no browser (WebGL) e também salva em `/files`.
- Cria um elemento `com.toposync.models.gltf` com metadados de escala (para 2D ↔ 3D baterem).

## `extensions/home_assistant`

- Scaffold: configurar servidores Home Assistant.
- Elemento “Home Assistant item” com visualizações especiais (luminária, vento climatizado e **modelo 3D**).

## `extensions/cameras`

- RTSP snapshots, ingestão e operadores de pré/pós-processamento para pipelines de câmera.

## `extensions/images`

- Importar imagens como sobreposição ou decalque.

## `extensions/vision`

- Detecção, rastreamento, segmentação de instâncias e pose por tarefa pública.
- Runtime oficial first-party baseado em ONNX Runtime.
- Caminho padrão do produto: ONNX Runtime CPU; aceleração é opcional e explícita.
