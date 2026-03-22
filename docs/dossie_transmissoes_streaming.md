# Dossie Tecnico - Transmissoes (HLS/RTSP/WebRTC) no Toposync

Data de referencia: **2026-02-25**

## 1. Resumo executivo

A ideia e **viavel**, mas vira uma feature de plataforma, nao so um operador novo. O caminho mais seguro para sair do "mad science" para algo operavel e:

1. Criar uma entidade de dominio **Transmission** (transmissao) separada de Pipeline.
2. Criar um novo sink de pipeline `stream.write` que escreve em uma transmissao.
3. Hospedar distribuicao/protocolos em um engine de streaming dedicado (recomendacao: **MediaMTX embarcado e gerenciado pela extensao**).
4. Expor no minimo tres saidas por transmissao: **HLS (mobile + Safari)**, **WebRTC/WHEP (dashboard web de baixa latencia)** e **RTSP (reuso/ecossistema)**.
5. Implementar modo sob demanda (on-demand) baseado em conexoes de viewers, com placeholder cinza quando nao houver frames.

Se quiser reduzir risco inicial:

- Fase 1: HLS + RTSP + autenticação opcional + um sink `stream.write`.
- Fase 2: WebRTC para dashboard e mobile low-latency.
- Fase 3: dashboard avançado, multi-pipeline arbitration, otimizações por GPU e bypass.

---

## 2. O que voce pediu (requisitos consolidados)

### 2.1 Funcionais

- Gerar streams em URLs a partir da ferramenta.
- Uma transmissao deve suportar **multiplos formatos simultaneos** (HLS, RTSP e possivelmente outros).
- Novo local de configuracao para transmissões.
- Wizard opcional para criar pipeline apos criar transmissao, escolhendo camera de entrada.
- Novo step de saida no pipeline apontando para transmissao criada.
- Mais de um pipeline pode escrever na mesma transmissao.
- Placeholder estatico (cinza) quando nao houver frame.
- Resolucao por formato/saida; resize em modo **contain** com fundo preto quando necessario.
- Processar stream apenas quando houver cliente conectado.
- Autenticacao opcional usuario/senha por stream.
- Dashboard de transmissões na UI, com grid/paginacao e UI que aparece em interacao e esmaece quando inativa.
- Pensar futuro iOS/Android com `expo-video` + PiP.

### 2.2 Nao funcionais

- Multiplataforma.
- Evitar exigir instalacao manual de componentes externos.
- Performance previsivel com pipelines pesados (tracking/detection/segmentacao/throttle/debounce/gates).
- Compatibilidade com processing servers remotos (IP diferente e papel critico).
- Escalabilidade gradual (1x1, 2x2, etc.) sem quebrar UX.

---

## 3. Diagnostico do estado atual do Toposync

## 3.1 Pipeline runtime e contrato de frame

Pontos importantes no codigo atual:

- `Packet` tem `stream_id` + `lifecycle` (`open|update|close`) + `artifacts`.
- Frame nao deveria ir em payload; contrato principal usa `artifacts["frame_original"]` e `artifacts["frame"]`.
- Filas bounded com politicas de drop e protecao para mensagens estruturais (`open/close` nao dropa igual `update`).
- Apos operadores `split_stream`, runtime usa `KeyedBoundedChannel` por `stream_id`, reduzindo starvation.
- Existe orcamento de memoria de artifacts por packet/pipeline/global.

Arquivos:

- `src/toposync/runtime/pipelines/runtime.py`
- `src/toposync/runtime/pipelines/execution.py`
- `src/toposync/runtime/pipelines/execution_scheduler.py`

Impacto para transmissao:

- Excelente base para um sink realtime.
- Mas o sink precisa respeitar lifecycle e drop semantics para nao gerar stream "flicker" ou atrasado.

## 3.2 Distribuido (origin vs processing server)

Estado atual:

- Pipeline final pode ser `local` ou remoto (`processing_server_id`).
- Split distribuido atual suporta **processing -> origin**, mas **nao suporta origin -> processing**.
- `origin_only` define operadores que precisam rodar na origem.
- Transporte remoto atual usa HTTP + SSE + ACK.

Arquivos:

- `src/toposync/runtime/pipelines/distributed/plan.py`
- `src/toposync/runtime/pipelines/distributed/orchestrator.py`
- `src/toposync/runtime/pipelines/distributed/transport.py`
- `src/toposync/runtime/pipelines/distributed/processing_server.py`

Impacto para transmissao:

- Se stream sink for `origin_only`, tudo converge na origem (bom para centralizar, ruim para IP remoto).
- Se quiser stream no IP do processing server, sink precisa rodar no processing lado (nao `origin_only`) e configuracao precisa propagar para processing app.
- Juntar writers de pipelines em servidores diferentes na mesma transmissao fica bem mais complexo com arquitetura atual.

## 3.3 Câmeras e visao computacional

Estado atual:

- `camera.source` com backend `auto|opencv|ffmpeg`, gate opcional, e `CameraHub` compartilhado (evita conexoes RTSP duplicadas por camera).
- Tracking/detection YOLO com `split_stream` e lifecycle.
- Resize atual (`camera.image_resize`) e max-edge in-memory, nao e "contain" com letterbox.
- Wizard de camera ja cria pipelines completos com presets.

Arquivos:

- `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`
- `extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/camera_hub.py`
- `extensions/cameras/src/toposync_ext_cameras/plugin.py`

Impacto para transmissao:

- Base forte para fonte e processamento.
- Falta um sink de stream.
- Falta resize "contain + fundo preto".

## 3.4 UI e navegacao

Estado atual:

- Rotas principais: `/settings`, `/settings/pipelines`, `/settings/processing-servers`, `/settings/access`.
- Main screen so alterna modo `3d|2d` atualmente.
- Processing Servers UI ja existe, com diagnosticos de CPU/RAM/torch/opencv/ffmpeg.

Arquivos:

- `frontend/src/ui/App.tsx`
- `frontend/src/ui/screens/SettingsScreen.tsx`
- `frontend/src/ui/screens/MainScreen.tsx`
- `frontend/src/ui/screens/ProcessingServersScreen.tsx`

Impacto para transmissao:

- Ha lugar natural para adicionar:
  - configuracao de transmissões em settings.
  - dashboard de transmissões no Main (novo modo de renderizacao).

## 3.5 Auth da plataforma

Estado atual:

- Auth local com sessoes cookie, refresh, grants por recurso.
- `owner/admin` com wildcard `*`; `member/guest` com acoes limitadas por default.
- Extensoes podem declarar prefixos API protegidos por `core:extension:use`.

Arquivos:

- `src/toposync/runtime/auth.py`
- `src/toposync/extensions/manager.py`
- `src/toposync/app.py`

Impacto para transmissao:

- Ja existe mecanismo para auth de API/config da feature.
- Auth de stream (viewer) e outro plano e deve ser separado da auth da plataforma.

---

## 4. Protocolo por protocolo: o que usar e por que

## 4.1 HLS

Quando usar:

- iOS/iPadOS/tvOS/macOS/Safari e mobile apps com player baseado em AV stack.
- compatibilidade ampla com CDN e caching.

Pro:

- Excelente para app mobile e PiP.
- Robusto para redes variaveis.
- Bom para VOD e live não ultrabaixa latencia.

Contra:

- Latencia maior que WebRTC (mesmo com LL-HLS).
- Multiplexacao de muitos streams simultaneos no browser pesa.

No seu contexto:

- Deve ser obrigatorio como formato principal para futuro app iOS/Expo.

## 4.2 RTSP

Quando usar:

- Reuso em NVR, VLC, FFmpeg, GStreamer, integradores e softwares legados.

Pro:

- Latencia baixa e ecossistema enorme fora do browser.
- Facil integrar com software de video profissional.

Contra:

- Navegador nao toca RTSP diretamente de forma nativa na pratica.

No seu contexto:

- Excelente para "reutilizar" e integracao externa.
- Nao resolve sozinho dashboard web.

## 4.3 WebRTC (WHEP para leitura)

Quando usar:

- Dashboard dentro do browser com baixa latencia e interatividade.

Pro:

- Menor latencia fim a fim.
- Melhor UX para grade "ao vivo".

Contra:

- Mais complexo (ICE/STUN/TURN, codec interop, operacao de conectividade).
- Custo de sessao por viewer pode crescer rapido.

No seu contexto:

- Ideal para "Dashboard de transmissões" em tempo real.
- Pode entrar como fase 2 para reduzir risco inicial.

## 4.4 Recomendacao de portfolio inicial

Para MVP robusto:

- `RTSP` + `HLS` no backend.
- `WebRTC` habilitado para o dashboard web quando estiver estavel.

Matriz resumida:

| Formato | Browser | iOS/Android app | Latencia | Complexidade | Papel recomendado |
|---|---|---|---|---|---|
| HLS | muito bom (nativo Safari; MSE em outros via player) | muito bom | media | media | formato base |
| RTSP | ruim no browser direto | bom via libs nativas/bridge | baixa | baixa-media | integracao/reuso |
| WebRTC/WHEP | muito bom (quando bem configurado) | bom | muito baixa | alta | dashboard realtime |

---

## 5. Engine de streaming: build propria vs embarcar MediaMTX

## 5.1 Opcao A - engine propria (FFmpeg/OpenCV + API propria)

Pro:

- Controle total.
- Menos dependencia externa conceitualmente.

Contra:

- Alto custo de engenharia (sessoes, auth, protocolos, reconexao, observabilidade, controle API).
- Risco alto de virar "produto de streaming" dentro do Toposync.

## 5.2 Opcao B - MediaMTX embarcado (recomendado)

Pro:

- Ja suporta multiprotocolo e conversao entre protocolos.
- Ja tem auth interna/HTTP/JWT.
- Ja oferece WHIP/WHEP, HLS, RTSP, RTMP, SRT.
- Binario unico, sem depender de runtime extra para o usuario.

Contra:

- Precisa estrategia de distribuicao binaria por plataforma/arquitetura.
- Precisa camada de gerenciamento (start/stop/health/config) dentro da extensao.

## 5.3 Recomendacao objetiva

- **Usar MediaMTX como "engine de output"**, gerenciado por uma extensao `com.toposync.streaming`.
- Toposync fica responsavel por:
  - modelo de dominio (Transmission),
  - sink de pipeline (`stream.write`),
  - UX e auth da plataforma,
  - roteamento de configuracao.
- MediaMTX fica responsavel por:
  - serving RTSP/HLS/WebRTC,
  - sessoes/viewers,
  - auth de stream.

---

## 6. Modelo de dominio proposto

## 6.1 Entidade Transmission

Campos sugeridos:

- `id`: gerado automaticamente.
- `name`: nome amigavel.
- `enabled`: bool.
- `host_server_id`: `local` ou ID de processing server onde a transmissao e hospedada.
- `path`: slug de URL.
- `placeholder`: `gray|black|custom` + caminho opcional.
- `arbitration`: regras para multi-writer.
- `outputs[]`: lista de saidas por formato.
- `auth`: opcional (usuario/senha/token), por transmissao e/ou por output.
- `created_at`, `updated_at`.

## 6.2 Entidade TransmissionOutput

Campos sugeridos:

- `id`
- `protocol`: `hls|rtsp|webrtc` (extensivel).
- `enabled`
- `resolution`: `{ width, height }` opcional.
- `fps_limit`: opcional por output.
- `bitrate`: opcional por output.
- `latency_profile`: `normal|low|ultra_low`.
- `audio_mode`: `none|passthrough|generated`.

## 6.3 Entidade StreamWriterBinding (runtime)

Nao persistida (estado runtime):

- `transmission_id`
- `writer_id` (pipeline/node)
- `source_stream_id`
- `last_frame_ts`
- `lifecycle_state`
- `priority`

---

## 7. Integração com pipelines (ponto critico)

## 7.1 Novo operador sink

Criar operador:

- `stream.write`

Contrato:

- Input: `in` (packet com artifacts de frame).
- Output: nenhum (sink).
- Config:
  - `transmission_id` (obrigatorio)
  - `input_with_fallback` (ex.: `frame,best_frame,segmented,frame_original`)
  - `respect_lifecycle` (bool)
  - `writer_priority` (int)
  - `resize_mode` (`none|contain`)

Capabilities sugeridas:

- `sink`
- `realtime`
- **nao marcar `origin_only` por default** (para permitir hosting no processing server quando necessario).

## 7.2 Semantica de lifecycle

- `open`: writer entra ativo na transmissao.
- `update`: atualiza frame candidato.
- `close`: writer sai ativo.
- Se nenhum writer ativo: emissao de placeholder.

## 7.3 Multi-pipeline na mesma transmissao

Regras de arbitragem propostas:

1. Priorizar writer com `lifecycle=open|update` mais recente.
2. Em empate, maior `writer_priority`.
3. Aplicar "sticky window" curta (ex.: 300-700ms) para evitar troca frenética.
4. Se todos expirarem: placeholder.

## 7.4 Impactos em steps criticos existentes

- `vision.track` / `vision.detect`:
  - podem gerar muitos streams e chaves; sink precisa limitar writer cardinality.
- `core.throttle` / `core.fps_reducer` / `core.debounce`:
  - podem reduzir carga; manter no caminho antes de `stream.write` e vital.
- `core.store_images` com `drop_data_after_store`:
  - se stream depender de artifact que foi evicted, sink fica sem frame.
- `camera.image_resize` atual:
  - nao atende contain/letterbox; precisa novo resize especifico da transmissao.

---

## 8. Hosting da transmissao e processing servers (IP diferente)

## 8.1 Problema central

Voce quer que transmissoes possam sair por IP de processing server remoto. Hoje:

- processing executa parte do pipeline,
- origin concentra operadores `origin_only`,
- split origin->processing nao e suportado.

## 8.2 Regra de projeto recomendada

- Cada transmissao tem `host_server_id`.
- `stream.write` so pode escrever em transmissão hospedada no mesmo lado logico do pipeline runtime:
  - pipeline local/origin -> transmission local/origin.
  - pipeline remoto `server X` -> transmission hosteada em `server X`.

## 8.3 O que precisa evoluir

- Payload de config enviado ao processing server deve incluir definicao de transmissões relevantes.
- Processing app precisa inicializar runtime de streaming (MediaMTX sidecar local + bridge writer).
- API de status deve reportar viewers e outputs por transmission.

## 8.4 Cenarios de mistura entre servidores

- "Dois pipelines em servidores diferentes escrevendo na mesma transmissao":
  - evitar no MVP.
  - suportar depois so com hub central (com custo de rede e latencia) ou federation explicita.

---

## 9. Resize, placeholder e composicao de saidas

## 9.1 Resize contain com fundo preto

Implementar no writer bridge:

- So redimensionar quando `src != dst`.
- Modo `contain`:
  - manter aspect ratio,
  - letterbox/pillarbox preto,
  - centralizar.
- Armazenar metadata de transform (`src_wh`, `dst_wh`, offsets`) para diagnostico.

## 9.2 Placeholder cinza

- Frame base 1280x720 cinza medio (ou output resolution nativa).
- Pode ser gerado em memoria (numpy) no inicio.
- Opcionalmente permitir imagem custom no futuro.

## 9.3 Multiplas resolucoes por output

Evitar custo duplicado:

- Gerar um frame base por tick.
- Derivar piramide de resolucao (ex.: 1080p -> 720p -> 480p) conforme outputs ativos.

---

## 10. "Processar so quando houver viewer"

## 10.1 Niveis de economia

N1 (MVP):

- `stream.write` nao codifica/publica quando `viewer_count == 0`.
- Pipeline continua rodando (se tiver outros sinks como notify/storage).

N2:

- `stream.write` gera sinal de demanda.
- Podemos adicionar gate antes de source do ramo de stream para cortar custo de frame path.

N3:

- Para pipelines simples (camera + fps reducer opcional + stream), usar bypass direto e source on-demand.

## 10.2 Como obter viewer_count

Com MediaMTX:

- Usar API/metricas/hook de connect/disconnect para manter contadores por path/output.
- Atualizar estado no runtime do Toposync.

---

## 11. Autenticacao: plataforma vs stream

## 11.1 Auth da plataforma

Criar acoes novas:

- `core:streams:read`
- `core:streams:write`
- `core:streams:dashboard:view`

Com grants por recurso:

- `resource_type = core:stream`
- selector por `stream_id` ou padrao.

## 11.2 Auth de stream (viewer)

Opcional por transmissao/output:

- user/pass basico.
- token bearer/JWT (futuro).

Separar credenciais:

- Nao misturar cookie/session da plataforma com credenciais de playback.
- Para UI web, preferir token efemero emitido por API da plataforma (proxy/bridge) em vez de senha fixa exposta no cliente.

## 11.3 TLS

- HLS/LL-HLS em Safari/Apple tem requisitos práticos de TLS no caminho.
- Para uso serio fora de LAN, considerar TLS obrigatorio para HLS/WebRTC endpoints.

---

## 12. UI proposta

## 12.1 Configuracao de transmissões

Novo item em Settings:

- "Transmissoes"

Tela:

- Lista de transmissões.
- CRUD de transmissao.
- Sublista de outputs (HLS/RTSP/WebRTC) com resolucao/fps/bitrate/auth.
- Preview de URLs e status de viewers.
- Botao "Criar pipeline com esta transmissao".

## 12.2 Wizard pos-criacao

Fluxo:

1. Seleciona camera.
2. Escolhe preset de pipeline.
3. Injeta `stream.write` no final com `transmission_id` selecionado.
4. Opcional: habilita bypass simples.

## 12.3 Dashboard de transmissões

No Main, em "Renderizacao":

- Novo modo: `streams`.
- Grid 1x1/2x2 inicialmente.
- Paginacao discreta com setas.
- UI auto-hide:
  - ao mover mouse, tecla, foco da aba -> mostra overlay.
  - inatividade -> fade e oculta.

Playback strategy no browser:

- HLS nativo quando suportado.
- hls.js quando MSE disponivel.
- WebRTC para baixa latencia quando habilitado.

Protecoes de escala:

- limite de players ativos simultaneos (ex.: 4).
- pausas em tiles fora da pagina.

---

## 13. CPU, GPU e capacidade

## 13.1 Gargalos esperados

- Decodificacao RTSP de origem.
- YOLO/segmentacao.
- Re-encode para saidas multiplas.
- Decodificacao simultanea no dashboard.

## 13.2 Uso de GPU

Estado atual:

- YOLO ja tenta CUDA/MPS/CPU fallback.

Estrategia recomendada:

- Encoders de streaming separados de inferencia.
- Se houver NVENC/VAAPI/VideoToolbox, usar para encode quando disponivel.
- Fallback CPU automatico.

## 13.3 Meta operacional inicial

- Comecar com alvo conservador:
  - 1x1 e 2x2 dashboard.
  - limitar resolucao/fps default por output.
- Expandir com benchmarking real por perfil de hardware.

---

## 14. Bypass para pipelines simples

## 14.1 Quando considerar bypass

Quando topologia e:

- `camera.source` -> (opcional `core.fps_reducer`) -> `stream.write`

Sem tracking/detection/segmentacao/etc.

## 14.2 O que bypass faz

- Em vez de passar por toda malha de operators, liga camera diretamente ao engine de stream com transformacoes minimas.
- Mantem economia de CPU e menor latencia.

## 14.3 Risco

- Duas semanticas de execucao podem confundir debug.

Mitigacao:

- Modo explicito `auto|force_on|force_off` no `stream.write`.
- Telemetria e logs claros dizendo quando bypass foi ativado.

---

## 15. Extensao separada vs core

## 15.1 Recomendacao

Criar extensao isolada:

- `com.toposync.streaming`

Motivos:

- Dependencias pesadas e lifecycle proprio.
- Evolucao rapida sem acoplar forte ao core.
- Coerente com arquitetura de extensoes ja existente.

## 15.2 O que continua no core

- Contratos de pipeline/operator registry (ja existe).
- Auth base e grants.
- Orquestrador distribuido e runtime base.

## 15.3 O que vai para extensao de streaming

- APIs `/api/streams/*`.
- Settings panel e dashboard UI.
- Engine manager (MediaMTX subprocess/supervisao).
- Operador `stream.write`.

---

## 16. Plano de implementacao por fases

## Fase 0 - Spike tecnico (1-2 semanas)

- Prova de conceito local com 1 transmissao.
- Sink `stream.write` simplificado.
- HLS e RTSP funcionando.
- Placeholder cinza e resize contain.

Criterio de saida:

- URL abre em VLC (RTSP) e browser (HLS) com estabilidade basica.

## Fase 1 - Dominio e configuracao (2-3 semanas)

- CRUD de Transmission/Output.
- Auth da plataforma para streams.
- Persistencia em `settings.extensions[com.toposync.streaming]`.
- Wizard pos-criacao de pipeline.

Criterio de saida:

- Usuario cria transmissao via UI e conecta pipeline sem editar JSON manualmente.

## Fase 2 - Distribuido e auth de stream (2-4 semanas)

- `host_server_id` + deploy de runtime em processing servers.
- Auth opcional por stream/output.
- Telemetria de viewers.

Criterio de saida:

- Stream remoto com IP do processing server acessivel e autenticado.

## Fase 3 - Dashboard web (2-4 semanas)

- Modo streams no Main.
- Grid/paginacao/auto-hide UI.
- fallback HLS/WebRTC.

Criterio de saida:

- Dashboard utilizavel com 1x1 e 2x2 em hardware alvo.

## Fase 4 - Otimizacoes e bypass (continuo)

- Auto-bypass para pipelines simples.
- Perf tuning de encoder e limites.
- Testes de stress e failover.

---

## 17. Testes obrigatorios

## 17.1 Backend

- Unit:
  - modelos de transmissao,
  - validacoes de output,
  - auth rules.
- Integracao:
  - pipeline -> `stream.write` -> playback URL.
  - multi-writer arbitration.
  - lifecycle open/update/close + placeholder.

## 17.2 Distribuido

- Pipeline remoto para transmissao remota.
- Reconexao/restart do processing server.
- Queda de rede e retomada.

## 17.3 Frontend

- CRUD de transmissões.
- Wizard cria pipeline com `stream.write`.
- Dashboard com fallback de player e limites.

## 17.4 Carga

- N cameras × M viewers.
- CPU/RAM/GPU e latencia por protocolo.
- Com e sem YOLO.

---

## 18. Riscos principais e mitigacoes

1. Complexidade operacional do engine de stream.
Mitigacao: usar engine pronta (MediaMTX) e encapsular operacao.

2. Incompatibilidade de codec por browser/dispositivo.
Mitigacao: perfil default H264 + AAC para outputs universais.

3. Custos de encode para multiplas resolucoes.
Mitigacao: limites default + escada de resolucao + on-demand.

4. Ambiguidade multi-pipeline no mesmo stream.
Mitigacao: politica de arbitragem explicita + telemetria.

5. Diferenca de IP entre origin e processing quebrar expectativa.
Mitigacao: `host_server_id` obrigatorio e regras de afinidade de writer.

6. Seguranca de credenciais de stream no frontend.
Mitigacao: token efemero e/ou proxy controlado por sessao da plataforma.

---

## 19. Decisoes que voce precisa fechar antes do build

1. MVP inclui WebRTC ja na primeira entrega ou fica para fase 2?
2. Uma transmissao pode aceitar writers de servidores diferentes no MVP? (recomendacao: nao)
3. Auth de stream no MVP sera basica (user/pass) ou ja com token efemero?
4. Qual perfil default de output? (ex.: HLS 720p/15fps + RTSP 720p/15fps)
5. Bypass automatico entra no MVP ou depois de estabilidade?

---

## 20. Conclusao pratica

Seu conceito funciona tecnicamente e combina com a arquitetura atual do Toposync, desde que tratado como subsistema de streaming e nao como "mais um operador" isolado.

A rota com melhor relacao risco/tempo e:

- extensao dedicada de streaming,
- MediaMTX embarcado e gerenciado,
- sink `stream.write` com lifecycle,
- HLS + RTSP primeiro,
- WebRTC para dashboard logo em seguida.

Isso cobre seu objetivo de iOS PiP, reuso RTSP, dashboard web e crescimento futuro para Android/Expo sem exigir instalacao manual do usuario final.

---

## 21. Referencias externas (checadas em 2026-02-25)

### Protocolos, browser e players

- MDN - Live streaming e suporte de protocolos no browser (RTSP nao nativo em geral):
  - https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Audio_and_video_delivery/Live_streaming_web_audio_and_video
- MDN - WebRTC API:
  - https://developer.mozilla.org/docs/Web/API/WebRTC_API
- MDN - WebRTC codecs:
  - https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Formats/WebRTC_codecs
- hls.js (compatibilidade e requisitos MSE):
  - https://github.com/video-dev/hls.js/

### Apple / HLS / mobile

- Apple Streaming portal (HLS docs e LL-HLS):
  - https://developer.apple.com/streaming/
- Apple HTTP Live Streaming docs (visao geral):
  - https://developer.apple.com/documentation/http-live-streaming

### Expo video (iOS/Android/Web)

- Expo `expo-video` (PiP, content type hls/dash, config plugin):
  - https://docs.expo.dev/versions/latest/sdk/video/

### MediaMTX

- Repositorio oficial (features, release atual):
  - https://github.com/bluenviron/mediamtx
- Documentacao de publish:
  - https://mediamtx.org/docs/usage/publish
- Documentacao de read:
  - https://mediamtx.org/docs/usage/read
- WebRTC-specific features:
  - https://mediamtx.org/docs/usage/webrtc-specific-features
- Authentication:
  - https://mediamtx.org/docs/usage/authentication

### Licencas relevantes

- FFmpeg legal/licensing:
  - https://ffmpeg.org/legal.html
  - https://ffmpeg.org/download.html
- OpenCV licensing:
  - https://opencv.org/license/

---

## 22. Referencias internas do repositorio (base para impacto)

- `src/toposync/runtime/pipelines/runtime.py`
- `src/toposync/runtime/pipelines/execution.py`
- `src/toposync/runtime/pipelines/execution_scheduler.py`
- `src/toposync/runtime/pipelines/distributed/plan.py`
- `src/toposync/runtime/pipelines/distributed/orchestrator.py`
- `src/toposync/runtime/pipelines/distributed/transport.py`
- `src/toposync/runtime/pipelines/distributed/processing_server.py`
- `src/toposync/runtime/pipelines/operators_sinks.py`
- `src/toposync/runtime/auth.py`
- `src/toposync/extensions/manager.py`
- `src/toposync/app.py`
- `extensions/cameras/src/toposync_ext_cameras/plugin.py`
- `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`
- `extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/camera_hub.py`
- `frontend/src/ui/App.tsx`
- `frontend/src/ui/screens/MainScreen.tsx`
- `frontend/src/ui/screens/SettingsScreen.tsx`
- `frontend/src/ui/screens/PipelinesScreen.tsx`
- `frontend/src/ui/screens/ProcessingServersScreen.tsx`
- `frontend/src/ui/ProcessingServerModal.tsx`

---

## 23. Baseline tecnico completo do projeto (para quem nao tem acesso ao codigo)

## 23.1 Linguagens, runtime e stacks

- Backend:
  - Python 3.11+
  - FastAPI + Starlette
  - Pydantic
  - SQLite (auth e notifications)
  - asyncio (runtime dos pipelines, event bus, orchestrator)
- Frontend:
  - React + TypeScript
  - Webpack + Module Federation para UI de extensoes
  - ThreeJS (viewport 3D), canvas 2D no editor/main
- Streaming/camera/visao (estado atual):
  - OpenCV (captura/processamento imagem)
  - ffmpeg (captura snapshot e backend opcional de captura RTSP)
  - Ultralytics YOLO + torch (opcional, pesado)

## 23.2 Objetivo da ferramenta (estado atual)

- Plataforma local-first para "digital twin" com extensoes.
- Nucleo oferece:
  - composicoes visuais (2D/3D),
  - pipelines DAG de processamento em tempo real,
  - notificacoes,
  - auth local com grants,
  - distribuicao de pipelines para processing servers remotos.

## 23.3 Extensoes first-party atuais

- `com.toposync.structural`
- `com.toposync.models`
- `com.toposync.images`
- `com.toposync.home_assistant`
- `com.toposync.cameras`

Todas usam `extension.json` + bundle frontend `remoteEntry.js` servido por `/extensions/{extension_id}/...`.

---

## 24. Topologia de processos e rede

## 24.1 Processo origin (principal)

- Comando: `toposync serve`
- Responsabilidades:
  - API principal (`/api/*`)
  - auth de plataforma
  - host de extensoes backend/frontend
  - orchestrator de pipelines locais e distribuidos
  - notificacoes + arquivos locais

## 24.2 Processo processing server (remoto opcional)

- Comando: `toposync processing-serve`
- Responsabilidades:
  - receber config de pipelines distribuidos
  - executar parte "processing" do grafo
  - projetar eventos de volta ao origin via stream + ACK
  - expor diagnosticos (CPU/RAM/torch/opencv/ffmpeg/camera hub)

## 24.3 Fluxo distribuido real

1. Origin compila pipelines finais com `processing_server_id != local`.
2. Split do grafo em:
   - `processing_graph` (remoto),
   - `origin_graph` (origin_only + recepcao de eventos projetados).
3. Origin faz `POST /api/processing/config` no remoto.
4. Processing emite eventos projetados (SSE `/api/processing/events/stream`).
5. Origin recebe, injeta no inbox e faz `ack`.

Limite atual importante:
- **nao ha suporte para edge origin -> processing**.
- So existe processing -> origin na fronteira distribuida.

---

## 25. Superficie de API atual (core) e permissoes

Tabela resumida das rotas centrais relevantes para streaming.

| Metodo | Rota | Acao requerida |
|---|---|---|
| GET | `/api/health` | publica |
| GET | `/api/auth/status` | publica |
| POST | `/api/auth/setup` | publica (quando setup necessario) |
| POST | `/api/auth/login` | publica |
| POST | `/api/auth/logout` | publica |
| POST | `/api/auth/pair/start` | `core:auth:pair` |
| POST | `/api/auth/pair/complete` | publica |
| GET | `/api/extensions` | `core:extensions:list` |
| GET | `/api/settings` | `core:settings:read` |
| PUT | `/api/settings` | `core:settings:write` |
| PATCH | `/api/settings/extensions/{extension_id}` | `core:extension:settings:write` |
| GET | `/api/pipelines` | `core:pipelines:read` |
| GET | `/api/pipelines/operators` | `core:pipelines:read` |
| POST | `/api/pipelines/compile` | `core:pipelines:compile` |
| POST | `/api/pipelines/compile-python` | `core:pipelines:compile` |
| POST | `/api/pipelines` | `core:pipelines:write` |
| PUT | `/api/pipelines/{pipeline_name}` | `core:pipelines:write` |
| DELETE | `/api/pipelines/{pipeline_name}` | `core:pipelines:write` |
| GET | `/api/pipelines/runtime/status` | `core:pipelines:runtime:read` |
| POST | `/api/pipelines/runtime/reload` | `core:pipelines:runtime:write` |
| GET | `/api/processing-servers` | `core:processing_servers:read` |
| PUT | `/api/processing-servers/{server_id}` | `core:processing_servers:write` |
| DELETE | `/api/processing-servers/{server_id}` | `core:processing_servers:write` |
| GET | `/api/processing-servers/{server_id}/status` | `core:processing_servers:read` |
| GET | `/api/composition` | `core:compositions:read` |
| PUT | `/api/composition` | `core:compositions:write` |
| GET | `/api/compositions` | `core:compositions:read` |
| POST | `/api/compositions` | `core:compositions:manage` |
| POST | `/api/compositions/{id}/activate` | `core:compositions:manage` |
| PATCH | `/api/compositions/{id}` | `core:compositions:manage` |
| DELETE | `/api/compositions/{id}` | `core:compositions:manage` |
| GET | `/api/files/exists` | `core:files:read` |
| POST | `/api/files/upload` | `core:files:write` |
| GET | `/files/{path}` | `core:files:read` |
| GET | `/api/notifications` | `core:notifications:read` |
| GET | `/api/notifications/stream` | `core:notifications:stream` |
| GET | `/api/notifications/{id}` | `core:notifications:read` |

## 25.1 API da extensao cameras (hoje)

Todas as rotas abaixo estao no prefixo `/api/cameras` e passam pela auth route da extensao (`core:extension:use` sobre recurso `com.toposync.cameras`):

| Metodo | Rota | Acao extra |
|---|---|---|
| GET | `/api/cameras/index` | - |
| POST | `/api/cameras/control_points/map` | - |
| POST | `/api/cameras/rtsp/snapshot` | - |
| GET | `/api/cameras/cameras/{camera_id}/snapshot` | - |
| GET | `/api/cameras/cameras/{camera_id}/contexts` | leitura de areas filtrada por grants |
| POST | `/api/cameras/cameras/{camera_id}/pipeline-wizard` | `core:pipelines:write` |

## 25.2 API do processing server (remoto)

| Metodo | Rota | Uso |
|---|---|---|
| POST | `/api/processing/config` | recebe config de pipelines |
| GET | `/api/processing/status` | status + diagnosticos |
| GET | `/api/processing/events/stream` | stream SSE de eventos projetados |
| POST | `/api/processing/events/ack` | ACK de evento consumido |

Auth opcional por Basic Auth:
- `TOPOSYNC_PROCESSING_USERNAME`
- `TOPOSYNC_PROCESSING_PASSWORD`

---

## 26. Persistencia e modelos de dados atuais

## 26.1 Arquivos e caminhos

- `TOPOSYNC_DATA_DIR` define base de dados da instancia.
- Estrutura relevante:
  - `config.json`
  - `files/`
  - `auth/auth.sqlite3`
  - `notifications/notifications.sqlite3`

Defaults por SO:
- Linux: `$XDG_DATA_HOME/toposync` ou `~/.local/share/toposync`
- macOS: `~/Library/Application Support/Toposync`
- Windows: `%APPDATA%/Toposync` ou `%LOCALAPPDATA%/Toposync`

## 26.2 Modelo `config.json` (estado atual)

Estrutura semantica:

```json
{
  "schema_version": 1,
  "compositions": [
    {
      "id": "ground",
      "name": "Terreo",
      "elements": []
    }
  ],
  "active_composition_id": "ground",
  "settings": {
    "core": {
      "processing_servers": []
    },
    "extensions": {
      "com.toposync.cameras": {
        "cameras": []
      }
    }
  },
  "pipelines": []
}
```

Pontos tecnicos importantes:
- Escrita atomica com arquivo temporario + `os.replace`.
- Se `config.json` estiver corrompido:
  - arquivo e renomeado para `*.corrupt-<timestamp>.json`
  - default config e recriado.
- `processing_server_id="local"` e reservado:
  - nao pode editar/deletar esse servidor.

## 26.3 Modelo de ProcessingServer

- `id`: regex `^[a-z][a-z0-9_-]{0,63}$`
- `kind`: `inprocess|http`
- `url`: obrigatoria quando `kind=http`
- `username/password`: opcionais (auth com remoto)

## 26.4 Auth store (SQLite)

Tabelas:
- `auth_user`
- `auth_refresh_token`
- `auth_grant`
- `auth_pairing_code`
- `auth_meta`

Caracteristicas:
- hash de senha: scrypt (parametros tunaveis por env)
- token de acesso assinado (HMAC)
- refresh token com rotacao + grace window
- grants por (`action`, `resource_type`) com `include[]` e `exclude[]`.

---

## 27. Contrato de auth e autorizacao (detalhado)

## 27.1 Roles default

- `owner`, `admin`: `*` (tudo)
- `member`: conjunto limitado (extensions list/use, compositions read, files read, events emit, devices read, area read/control, notifications read/stream, auth pair)
- `guest`: mais restrito
- `service`: vazio por default

## 27.2 Acoes configuraveis no UX (catalogo atual)

- `core:extension`: `core:extension:use`, `core:extension:settings:write`
- `core:event`: `core:events:emit`
- `core:area`: `core:area:read`, `core:area:control`, `core:area:edit`

## 27.3 Regras de middleware que impactam streaming

- Em modo `enforced`, qualquer `/api/*` (fora rotas publicas de auth) exige sessao valida.
- Prefixos declarados por extensao em `capabilities().auth.api_prefixes` recebem verificacao adicional (`core:extension:use`).
- Rotas de assets da extensao `/extensions/{extension_id}/...` tambem sao protegidas por acao de extensao.

Implicacao para dashboard de streams:
- Se usar assets/UI de extensao dedicada de streaming, esse mesmo modelo de auth e automaticamente reaproveitavel.

---

## 28. Runtime de pipelines: contratos internos que afetam streams

## 28.1 `Packet`/`Artifact`/`Lifecycle`

- `Packet`:
  - `packet_id`, `stream_id`, `lifecycle(open|update|close)`, timestamps
  - `payload` (dados leves)
  - `artifacts` (frames/imagens)
  - `metadata`
  - `parent_packet_id`

- `Artifact`:
  - `data` (in-memory) e/ou `reference` (persistido)
  - `mime_type`, `metadata`

## 28.2 Fila bounded e politica de drop

- Toda edge do grafo e bounded.
- Politicas:
  - `block`
  - `drop_updates`
  - `drop_oldest`
  - `drop_newest`
  - `latest_only`
  - `keyed_latest_only`
- Mensagens estruturais (`open/close`) nao sao dropadas como updates.

## 28.3 Canais keyed apos `split_stream`

- Apos operadores com capability `split_stream`, runtime usa `KeyedBoundedChannel` por `stream_id`.
- Beneficio:
  - evita starvation entre objetos trackeados no mesmo ramo.
  - melhora previsibilidade para multi-objeto.

## 28.4 Orcamento de memoria dos artifacts

Defaults (env configuravel):
- `TOPOSYNC_ARTIFACT_MAX_BYTES_PER_PACKET=134217728`
- `TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_PER_PIPELINE=536870912`
- `TOPOSYNC_ARTIFACT_MAX_TOTAL_BYTES_GLOBAL=1073741824`

Semantica:
- runtime pode "evictar" `artifact.data` derivado quando estoura limite.
- `frame_original` e `frame` sao preservados preferencialmente.

## 28.5 Scheduler de execucao

- Modos:
  - `in_event_loop`
  - `thread_pool`
  - `process_pool`
  - `external` (nao suportado no scheduler local)
- Concurrency control por semaphore com `concurrency_key` e `max_concurrency`.
- Importante para YOLO e transformacoes pesadas (evitar oversubscription).

---

## 29. Catalogo de operadores atuais (inventario)

## 29.1 Core

- `core.source`
- `core.synthetic_source`
- `core.demo_frame_sequence_source`
- `core.fps_reducer`
- `core.throttle`
- `core.velocity_throttle`
- `core.debounce`
- `core.lifecycle_from_boolean`
- `core.stream_state_snapshot`
- `core.debug`
- `core.passthrough`
- `core.sink`
- `core.schedule_gate`
- `core.category_gate`
- `core.filter`
- `core.store_images`
- `core.notify`

## 29.2 Distribuido

- `dist.remote_source` (`origin_only`, source de inbox)
- `dist.target_filter` (`origin_only`)
- `dist.project_to_origin` (`processing_only`, sink)

## 29.3 Cameras/Visao (extensao)

- `camera.source`
- `camera.motion_gate`
- `vision.track`
- `vision.detect`
- `camera.frame_attach`
- `camera.object_crop`
- `camera.image_crop`
- `camera.image_adjust`
- `camera.image_resize`
- `camera.camera_mapping`
- `camera.area_restriction`
- `camera.velocity_estimation`
- `camera.best_frame_selector`

## 29.4 Steps criticos para a feature de transmissao

1. `camera.source`
- backend `auto|opencv|ffmpeg`
- fps configuravel
- suporte a gate de entrada (`port gate`) para pausar captura
- usa `CameraHub` (1 conexao por camera + refcount)

2. `vision.track`
- split por objeto com lifecycle
- parametros de estabilidade:
  - `close_after_seconds` (default 4.0)
  - `default_interval_seconds` (default 0.2)
  - `pause_when_gate_closed` (default true)
  - `max_paused_seconds` (default 900)

3. `camera.image_resize`
- estado atual: resize por `max_edge_px` (nao e contain/letterbox)
- para sua demanda, nao atende diretamente "contain + fundo preto"

4. `core.store_images`
- por default remove `artifact.data` apos persistir (`drop_data_after_store=true`)
- se stream depender de dado em memoria depois desse ponto, ordem do grafo importa

5. `core.notify`
- idempotencia por `dedupe_key`
- lifecycle open/update/close
- sintetiza close em shutdown para evitar notificacao "presa em open"

---

## 30. Processamento de imagem e GPU/CPU (estado real)

## 30.1 Captura de frame RTSP

`FrameGrabber` (cameras extension):
- escolhe backend conforme preferencia:
  - `opencv` (primeira opcao no auto)
  - `ffmpeg` (fallback)
- timeouts:
  - `TOPOSYNC_RTSP_OPEN_TIMEOUT_MS` (default 8000)
  - `TOPOSYNC_RTSP_READ_TIMEOUT_MS` (default 8000)
- fallback automatico de `/stream1` para `/stream2` quando aplicavel.

## 30.2 CameraHub

- chave por camera/backend
- `acquire()` incrementa refcount
- `release()` decrementa, para grabber quando refcount chega em 0
- evita conexoes RTSP duplicadas quando multiplos pipelines usam mesma camera.

## 30.3 YOLO device selection

Ordem de decisao:
1. `TOPOSYNC_YOLO_DEVICE` (ou config explicita do operator)
2. CUDA (se disponivel)
3. MPS (Apple)
4. CPU fallback

Comportamento:
- se inferencia falhar no device selecionado, tenta CPU.
- diagnosticos incluem:
  - `device_requested`
  - `device_selected`
  - `device_effective`
  - razao da escolha.

## 30.4 Diagnosticos de processing server

`/api/processing/status` agrega:
- system: hostname, role, python, platform, cpu, memoria
- vision: torch/cuda/mps + trackers ativos
- cameras: opencv/ffmpeg + snapshot do camera hub

---

## 31. Funcionamento de extensoes (impacto direto no design da nova feature)

## 31.1 Carregamento

- Descoberta por entry point Python em grupo `toposync.extensions`.
- Plugin pode expor:
  - `manifest()`
  - `setup(app, bus, services)`
  - `capabilities()`
  - `static_root()`

## 31.2 Auth route por extensao

Se `capabilities().auth.api_prefixes` declarar prefixos `/api/...`:
- middleware do core exige acao configurada (`core:extension:use` por default)
- seletor de recurso = `extension_id`

## 31.3 Frontend extension runtime

- Host chama `/api/extensions`.
- Injeta `remoteEntry.js`.
- Inicializa share scope.
- Executa `activate(host)` da extensao.

Uma extensao de streaming dedicada consegue:
- registrar settings panel proprio,
- registrar renderers/elementos,
- adicionar UX sem acoplar ao core de forma invasiva.

---

## 32. Frontend atual: pontos de entrada para streams

## 32.1 Rotas/screen relevantes

- Main: tela principal com `renderMode` atual apenas `3d|2d`
- Settings: painel base + paines de extensao
- Pipelines: editor/compilacao/operadores
- Processing Servers: CRUD + teste + diagnosticos
- Access: usuarios/grants/sessoes

## 32.2 Settings extension storage

- Settings panel de extensao persiste via:
  - `PATCH /api/settings/extensions/{extension_id}`
- blob salvo em:
  - `settings.extensions[extension_id]`

Ideal para guardar configuracao de `Transmission`.

## 32.3 Wizard existente de camera

- Ja cria pipelines por preset:
  - `people`
  - `pets`
  - `vehicles_stopped`
- Endpoint:
  - `POST /api/cameras/cameras/{camera_id}/pipeline-wizard`
- Padrao ideal para reaproveitar:
  - apos criar transmissao, oferecer wizard para injetar `stream.write`.

---

## 33. Matriz de impacto da feature de transmissao

| Dominio | Impacto | Criticidade |
|---|---|---|
| Modelo de dados (`config.json`) | nova entidade `Transmission` + outputs + auth + host_server_id | Alta |
| Runtime pipelines | novo sink `stream.write`, arbitragem multi-writer, lifecycle consistente | Alta |
| Distribuido | decidir onde stream e hospedado (origin vs processing), sincronizacao config | Alta |
| Auth | separar auth da plataforma e auth de playback | Alta |
| Frontend settings | CRUD de transmissoes + wizard pos-criacao | Alta |
| Frontend main | dashboard de streams (grid/paginacao/auto-hide) | Alta |
| Cameras/visao | compatibilidade com split/lifecycle e high cardinality de objetos | Alta |
| Performance | decode+infer+encode multiprotocolo | Alta |
| Operacao cross-platform | embalagem de engine de streaming sem instalacao manual | Alta |
| Observabilidade | viewers por output, bitrate/fps/latencia, erros por writer | Alta |

---

## 34. Arquitetura detalhada recomendada para transmissao

## 34.1 Entidade `Transmission` (persistida)

Campos minimos:
- `id`, `name`, `enabled`
- `host_server_id` (`local` ou id remoto)
- `path` (slug)
- `placeholder_mode` (`gray|black|custom`)
- `placeholder_image_path` (opcional futuro)
- `outputs[]`
- `auth` (opcional)
- `arbitration_policy`
- `created_at`, `updated_at`

`outputs[]`:
- `id`
- `protocol`: `hls|rtsp|webrtc`
- `enabled`
- `resolution`: `{width,height}` opcional
- `fps_limit` opcional
- `bitrate` opcional
- `latency_profile` opcional
- `auth_override` opcional

## 34.2 Operador novo `stream.write`

Contrato proposto:
- input `in` (Packet com frame em artifact)
- sink sem output
- config:
  - `transmission_id`
  - `input_with_fallback` (`frame,best_frame,segmented,frame_original`)
  - `writer_priority`
  - `resize_mode` (`none|contain`)
  - `bypass_mode` (`auto|force_on|force_off`)
  - `emit_placeholder_on_idle` (bool)

Capacidades:
- `sink`
- `realtime`
- **nao** `origin_only` por default.

## 34.3 Arbitragem multi-writer (mesma transmissao)

Politica recomendada:
1. Writers com lifecycle ativo (`open|update`) elegiveis.
2. Ordenar por:
   - maior `writer_priority`
   - maior `last_frame_ts`
3. Aplicar `sticky_window_ms` para evitar alternancia rapida.
4. Sem writer ativo: placeholder.

## 34.4 Resize contain com fundo preto

Implementar no bridge de transmissao:
- manter aspect ratio
- calcular caixa destino
- preencher barras com preto
- redimensionar so quando necessario.

Obs:
- isso nao existe hoje em `camera.image_resize` (que e max-edge).

## 34.5 Processar so com viewer

Niveis:
- N1: parar encode/publicacao sem viewer.
- N2: sinalizar demanda para branch do pipeline.
- N3: bypass simples source->stream.

---

## 35. Servidor de processamento e IP de saida (detalhe critico)

## 35.1 Regra que precisa existir no produto

Uma transmissao deve ter afinidade de host:
- pipeline local escreve em transmissao local
- pipeline remoto `server X` escreve em transmissao hosteada em `server X`

Sem isso, expectativa de IP de saida fica inconsistente.

## 35.2 Consequencia da arquitetura distribuida atual

Como nao existe origin->processing edge:
- se sink ficar no processing, ele precisa estar no processing graph desde o inicio.
- se sink ficar origin_only, stream sai do origin (IP do origin).

## 35.3 Multi-writer entre servidores diferentes

No MVP:
- **nao suportar**.

Para suportar no futuro:
- exigir hub central de composicao (mais latencia/custo),
- ou federation de stream brokers com regra explicita.

---

## 36. Autenticacao da plataforma vs autenticacao da transmissao

## 36.1 Plataforma (ja existente)

Sessao/cookies/grants continua para:
- CRUD de transmissoes
- operacao e observabilidade
- dashboard interno autenticado

## 36.2 Playback auth (novo plano)

Separar totalmente:
- opcao `none|basic|token`
- credenciais de playback nao devem reutilizar cookie da plataforma.

Para browser/dashboard:
- preferir token efemero emitido pelo backend (escopo e TTL curto).

## 36.3 Novas acoes sugeridas

- `core:streams:read`
- `core:streams:write`
- `core:streams:dashboard:view`

E novo `resource_type`:
- `core:stream`

---

## 37. Empacotamento e operacao multiplaforma (sem instalacao manual)

## 37.1 Diretriz operacional

Usuario final nao deve instalar manualmente engine externa.

## 37.2 Opcao recomendada

Extensao dedicada (`com.toposync.streaming`) gerenciando engine embarcada (MediaMTX):
- baixar/incluir binarios por OS/arch
- start/stop supervisionado
- healthcheck
- geracao de config por template
- rotacao de logs

## 37.3 Itens de distribuicao

- macOS (arm64/x86_64)
- Linux (x86_64/arm64)
- Windows (x86_64)

Fallback:
- se binario nao suportado, informar claramente no UI/diagnostico.

---

## 38. Checklist de pesquisa para terceiro sem acesso ao repositorio

## 38.1 Protocolos e playback

1. HLS/LL-HLS para Safari/iOS PiP.
2. RTSP para integracoes externas.
3. WebRTC/WHEP para dashboard web low latency.
4. Compat de players web e mobile (`expo-video`, hls.js, nativo iOS).

## 38.2 Engenharia de stream engine

1. Process manager multiplataforma.
2. Multiprotocolo no mesmo path.
3. Auth por path/output.
4. Viewer hooks e metrica de sessao.
5. Contain resize + placeholder pipeline.

## 38.3 Performance e hardware

1. Decode RTSP concorrente.
2. Inferencia YOLO com CUDA/MPS/CPU fallback.
3. Encode H264/AAC por software vs hardware (NVENC/VAAPI/VideoToolbox).
4. Capacidade por perfil:
   - 1x1, 2x2 dashboard
   - viewers simultaneos
   - latencia e dropped frames.

## 38.4 Seguranca

1. Credenciais playback separadas.
2. Tokens efemeros para UI.
3. TLS para HLS/WebRTC fora de LAN.
4. Polices de rate-limit e lockout.

---

## 39. Blueprint tecnico de implementacao (arquivo por arquivo)

## 39.1 Core/backend

- `src/toposync/runtime/config_store.py`
  - adicionar modelos `Transmission*`
  - CRUD e validacoes
- `src/toposync/app.py`
  - endpoints `/api/streams/*`
  - novas acoes auth
- `src/toposync/runtime/auth.py`
  - catalogo de acoes/recurso `core:stream`
- `src/toposync/runtime/pipelines/operators_sinks.py` (ou nova unidade)
  - registrar `stream.write`
- `src/toposync/runtime/pipelines/recommendations.py`
  - novos alerts para streaming (ordem de steps, saturacao, etc)
- `src/toposync/runtime/pipelines/distributed/*`
  - propagar config de stream para processing
  - status remoto com viewers/outputs

## 39.2 Nova extensao de streaming (recomendado)

- `extensions/streaming/src/toposync_ext_streaming/extension.json`
- `extensions/streaming/src/toposync_ext_streaming/plugin.py`
  - setup API e auth prefixes
  - manager do processo da engine de stream
- `extensions/streaming/ui/src/*`
  - settings panel de transmissoes
  - dashboard panel/mode
  - wizard de pipeline pos-criacao

## 39.3 Frontend host

- `frontend/src/ui/screens/MainScreen.tsx`
  - novo `renderMode="streams"`
  - overlay auto-hide + pagina
- `frontend/src/ui/App.tsx`
  - roteamento/estado para dashboard de streams
- `frontend/src/util/api.ts`
  - tipos e funcoes de `/api/streams/*`

---

## 40. Riscos residuais e validacoes obrigatorias antes de producao

1. Escala de encode multiprotocolo por host.
2. Estabilidade com tracking split de alta cardinalidade.
3. Race conditions de multi-writer na mesma transmissao.
4. Reconexao de processing server sem perder consistencia de lifecycle.
5. Compatibilidade real em iOS/Android (`expo-video`) com PiP.

Validacoes obrigatorias:
- testes de carga com cenarios reais de camera e YOLO,
- soak test longo (8-24h),
- failover/restart de engine e de processing server,
- verificacao de auth e isolamento de credenciais.
