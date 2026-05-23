
# Dossiê técnico consolidado: qualidade, estabilidade e operação de streams no Toposync

**Versão:** 2026-05-23
**Objetivo:** transformar o diagnóstico de instabilidade em um plano sólido de engenharia, apoiado em literatura, normas de streaming, evidência do código atual e prioridades práticas por camada: backend/server, frontend/web, app React Native/Expo e Home Assistant add-on.

Este documento atualiza o dossiê técnico original sem perder seus detalhes. A primeira parte é a camada operacional nova: contratos de saúde, prioridades, critérios de aceite, métricas e runbooks. O dossiê original fica preservado no apêndice para manter a rastreabilidade.

---

## 0. Leitura executiva

A melhoria de qualidade dos streams do Toposync não deve começar aumentando resolução, FPS ou bitrate. Isso pode piorar a experiência se a cadeia ainda sofre com stale frame, on-demand agressivo, portas HLS divergentes, pipelines event-only e ausência de monitoramento contínuo da playlist.

A direção sólida é esta:

```text
primeiro: provar que o vídeo está vivo
segundo: impedir freeze silencioso
terceiro: tornar stalls diagnosticáveis
quarto: estabilizar demanda, portas, auth e encoder
quinto: otimizar qualidade visual, ABR e baixa latência
```

A principal tese técnica é: **streaming ao vivo é uma cadeia de relógios**. O usuário só percebe qualidade quando todos avançam juntos.

```text
camera/source clock
  -> packet/media timestamp
  -> selected writer timestamp
  -> bridge frame submit clock
  -> FFmpeg frame counter
  -> MediaMTX path/session clock
  -> HLS media sequence/segment clock
  -> AVPlayer/player buffer clock
  -> app lifecycle clock
```

Quando um relógio para e outro continua repetindo o último estado, o usuário vê “vídeo congelado”, mas a plataforma pode continuar dizendo `running=true`. Esse é o risco mais importante a eliminar.

---

## 0.0. Principios permanentes de streaming

Esta seção é normativa para decisões futuras de produto e engenharia no streaming do Toposync. Ela resume os princípios consolidados durante as investigações de HLS, WebRTC, MSE, JSMpeg, Home Assistant, Frigate, múltiplos streams por câmera, pipelines implícitos e publicação manual de pipelines.

### Modelo mental e UX

- O usuário padrão gerencia **fontes publicáveis** e variantes visíveis, não `Transmission`, `output_id`, `engine_path` ou `quality_profile_id`.
- Para câmeras comuns, o fluxo principal é: adicionar câmera/fonte, marcar `Transmitir`, escolher papel/nome quando necessário, e deixar a extensão reconciliar pipelines, transmissões e outputs.
- `Transmissions` são artefatos técnicos e continuam disponíveis para diagnóstico/avançado, mas não são o caminho primário de uso.
- Para pipelines manuais, o operador `stream.publish_video` deve expressar a intenção **Publicar este vídeo**: nome da transmissão, nome da variante, papel, visibilidade e perfil visual. O usuário não deve precisar criar uma Transmission antes.
- Câmera/fonte/papel é a camada de decisão primária da UI. Transporte e qualidade são políticas operacionais abaixo disso.
- O usuário deve conseguir responder rapidamente: “está publicado?”, “está ao vivo?”, “se não está, qual é a próxima ação?”.

### Estabilidade antes de qualidade

- Nunca marcar como `live` apenas porque FFmpeg, MediaMTX, go2rtc, JSMpeg ou o player estão rodando.
- `Live` exige frame selecionado recente, writer ativo/selecionado, fonte recente quando houver fonte, e output saudável.
- Frame antigo repetido é `stale` ou placeholder; nunca deve ser apresentado como live.
- Stalls de aquecimento podem ser tratados como recuperáveis quando o player volta a tocar, mas não podem mascarar ausência real de pipeline, publisher parado ou fonte velha.
- Se não há writer/pipeline alimentando a transmissão, a causa principal deve ser explícita: `Nenhuma pipeline está alimentando esta transmissão.`
- Placeholder e still recente são estados visuais válidos, mas não são live.

### Política de transporte

- HLS é a base estável e universal, especialmente para app, HA ingress, remoto, fallback e cenários de rede imprevisível.
- MSE é o caminho preferencial para web passivo quando a infraestrutura real está disponível: sidecar `go2rtc` rodando, MediaMTX saudável, output backing correto, codec compatível e proxy assinado funcional.
- WebRTC é contextual: PTZ, zoom, baixa latência explícita, two-way ou escolha fixa em debug. Ele não deve abrir para todos os tiles por padrão.
- JSMpeg é último recurso visual, sem áudio, baixa resolução/FPS, e só deve entrar depois de falha ou indisponibilidade real dos transportes melhores.
- RTSP não é transporte de navegador. Ele continua como contrato interno/ecossistema para HA Core, Frigate/dev, VLC, go2rtc sidecar e diagnóstico.
- Debug por transporte deve ser fixo e honesto. Se o usuário escolheu MSE, a tela testa MSE; se falhar, registra a falha em vez de cair silenciosamente para HLS.

### Home Assistant

- HA ingress/UI continua HLS-first. WebRTC direto do player Toposync fica bloqueado por padrão nesse contexto.
- O caminho correto para Home Assistant Cloud é entidade nativa `camera` do Home Assistant, não o player web do Toposync dentro de iframe/ingress tentando resolver ICE/UDP direto.
- O manifesto HA-native nunca deve expor URL direta da câmera nem credenciais brutas.
- WebRTC nativo HA permanece opt-in até validação real com HA Cloud, TURN/ICE e comportamento de fallback do HA.

### Sob demanda e custo

- Trabalho pesado deve existir apenas enquanto houver demanda real ou lease ativo para aquele stream/output.
- Heartbeat explícito sustenta tile, player, PiP, PTZ, debug e entidade HA enquanto estão ativos.
- MSE/go2rtc, JSMpeg/FFmpeg, publishers e pipelines implícitos não devem virar custo permanente sem viewer relevante.
- Cada sessão JSMpeg cria seu processo FFmpeg e encerra no disconnect. Limites globais e por transmissão impedem que fallback visual vire caminho normal de carga.
- Se JSMpeg aparecer frequentemente em Auto, isso é sinal para investigar HLS/MSE/WebRTC, não para normalizar JSMpeg como principal.

### Diagnóstico e mensagens

- Warning de transporte não selecionado não pode virar causa principal.
- Se HLS/MSE está tocando de forma saudável, warning de WebRTC fica em diagnóstico técnico, não no erro principal.
- O diagnóstico principal deve priorizar: URL/auth bloqueante, runtime sem frame, source stale, publisher/output parado, liveness HLS, transporte ativo e só depois warnings secundários.
- Toda validação importante de transporte deve olhar frame real quando possível, não apenas HTTP 200, WebSocket aberto ou contador de processo.
- Logs da tela de debug devem diferenciar bloqueado esperado, aquecendo, tocando, stall recuperável, erro fatal e encerramento normal.

### Arquitetura e extensões

- O core permanece genérico. Ele pode emitir eventos e prover infraestrutura, mas não conhece `stream.publish_video`, câmeras, MediaMTX, HLS, MSE ou JSMpeg.
- Regras de streaming ficam na extensão `com.toposync.streaming`.
- Nada deve ser implementado como caso especial de Reolink, Tapo, Garagem, Frente ou qualquer câmera específica. As decisões vêm de papéis, publicações, capacidades, saúde e políticas.
- O reconciliador é a autoridade para artefatos gerados: publicações, live views, transmissões, outputs e pipelines implícitos.
- Artefatos gerados podem ser recriados/pruned quando `generated_by` indica posse da extensão. Pipelines manuais do usuário não devem ser apagados por limpeza automática.
- Configuração antiga pode ser descartada enquanto o produto ainda não foi publicado, mas o desenho precisa continuar incremental para migrações futuras.

---

## 0.1. Atualização operacional: Home Assistant, HLS assinado e WebRTC

Em 2026-05-09/2026-05-10, a investigação em ambiente real de Home Assistant mostrou uma causa concreta de fragilidade que não estava suficientemente explícita neste dossiê: **HLS via ingress e HLS via acesso direto precisam ser tratados como um único contrato público de mídia, não como URLs montadas localmente por pedaços independentes**.

### Descobertas

1. **URL HLS por ingress sem prefixo público.**
   Ao acessar Toposync pelo HA ingress, `/api/streams/transmissions/{id}/urls` podia retornar HLS assinado em `http://<host>:8090/api/streams/media/hls/...`, sem `/api/hassio_ingress/<session>/...`. A primeira playlist podia até responder em chamadas internas, mas o player recebia URLs que não eram tocáveis “como estão” a partir do navegador.

2. **Rewrite de playlist também precisa preservar ingress.**
   Mesmo quando a master playlist era acessada com o prefixo correto, o proxy HLS podia reescrever media playlists, segmentos, `EXT-X-MAP` e `EXT-X-KEY` para `/api/streams/media/hls/...` sem o prefixo do HA. Isso causa 404 em media playlist ou tail segment e pode virar tela preta/fallback em loop.

3. **`network_contract_error` falso positivo em HA ingress.**
   No ingress, o request público aparece em `homeassistant.local:8090`, enquanto o contrato de acesso direto do add-on recomenda `18756`. Comparar esses dois valores como causa raiz é incorreto quando `public_hls_mode="proxy"` e a URL efetiva usa `/api/hassio_ingress/...`. A porta direta `18756` continua importante para acesso direto, mas não deve derrubar o diagnóstico do playback via ingress.

4. **WHEP 404 no primeiro acesso é normalmente race de readiness/cold start.**
   `POST http://homeassistant.local:18760/<path>/whep 404` logo após abrir a página pode acontecer quando o dashboard tenta WebRTC antes do publisher/path estar pronto. Se HLS está saudável e Auto cai para HLS, esse evento deve ser debug/fallback, não causa raiz crítica.

5. **Em Home Assistant add-on, Auto deve preferir HLS.**
   Para HA + `signed_proxy`, HLS assinado na porta principal/API é o caminho estável padrão. WebRTC/WHEP deve ficar para PTZ aberto ou escolha explícita “Baixa latência”, porque depende de `18760/tcp`, `18762/udp`, ICE e hosts adicionais corretos.

6. **`18759` deixa de ser porta pública obrigatória.**
   Com HLS assinado/proxy, a porta HLS do MediaMTX é interna. O contrato público mínimo para HLS remoto/local passa a ser a porta principal/API (`18756` no add-on direto ou ingress do HA). `18759` ainda pode existir internamente para MediaMTX/probe, mas não deve ser tratada como porta pública que o app/web precisam alcançar.

7. **HLS precisa de lease explícito, não apenas `viewer_count`.**
   Voltar de aba/background, fallback HLS e reload do player podem deixar `viewer_count=0` por alguns segundos. O frontend web e o app devem renovar `demand/heartbeat` enquanto tile/player/PiP/PTZ estiverem ativos, e falha de heartbeat deve ser telemetria, não motivo para parar o player.

### Decisões consolidadas

- Toda URL HLS entregue ao app/web deve ser tocável exatamente como retornada pela API.
- O helper de URL pública de mídia deve usar `scheme + host + root_path/x-ingress-path` e ser reutilizado em URL resolve, proxy/rewrite de playlists, status e snapshot diagnóstico.
- `network_contract_error` só deve ser causa raiz quando houver `blocking_errors` que afetem o transporte selecionado.
- HLS signed proxy é o fallback estável e canônico no HA; WebRTC é baixa latência explícita/PTZ.
- Add-on deve publicar snapshot das portas mapeadas pelo Supervisor para diagnosticar WebRTC UDP ausente, sem afetar HLS.

### Regressões obrigatórias adicionadas ao plano

| Cenário | Critério |
|---|---|
| HA ingress HLS URL | `/urls` retorna HLS com `/api/hassio_ingress/<id>/api/streams/media/hls/...`. |
| Playlist rewrite via ingress | variantes, segmentos, `EXT-X-MAP` e `EXT-X-KEY` mantêm o prefixo público. |
| HA ingress em `:8090` | não classifica `network_contract_error` contra `18756` quando HLS proxy está ativo. |
| Auto no HA | usa HLS-first; não faz POST WHEP no carregamento inicial. |
| PTZ no HA | pode tentar WHEP; se falhar, cai para HLS com aviso discreto. |
| HLS player ativo | envia `demand/heartbeat` a cada 10s com lease padrão de 45s. |

Nota de leitura: trechos antigos do apêndice preservado ainda mencionam `18759` como HLS público. Eles ficam mantidos por rastreabilidade histórica, mas são supersedidos por esta atualização: em `signed_proxy`, `18759` é porta interna/diagnóstico.

---

## 0.2. Atualização operacional: Playback Plan, Auto e múltiplos streams

Em 2026-05-22, foi entregue a primeira fatia da escada "sempre visualizável" no Toposync. A mudança não troca o engine principal. Ela cria o contrato central que permite a UI parar de decidir por heurísticas espalhadas.

Em 2026-05-23, o MSE deixou de ser apenas um candidato planejado: ele passou a ser executado por um sidecar opcional `go2rtc`, consumindo somente RTSP interno do MediaMTX e expondo ao navegador apenas um WebSocket assinado/proxyado pelo Toposync. No mesmo ciclo, JSMpeg passou a ser fallback visual real: FFmpeg é iniciado por sessão WebSocket assinada, consome apenas o frame selecionado do runtime da Transmission, não usa áudio e encerra quando a conexão fecha.

### O que ficou implementado

1. **Playback Plan API.**
   O backend passa a expor `GET /api/streams/transmissions/{id}/playback-plan?client=web|app&output_id=&quality_profile_id=`.

2. **Plano embutido no live playback.**
   A resposta de live view inclui `playback_plan`, permitindo que o dashboard web use a mesma política do backend ao montar o player.

3. **Escada de transporte declarativa.**
   Para web comum, a ordem de intenção é `MSE -> HLS -> JSMpeg` em telas passivas e `WebRTC -> MSE -> HLS -> JSMpeg` em baixa latência/PTZ. MSE só é selecionado quando MediaMTX, sidecar `go2rtc`, output backing e codec estão aptos; JSMpeg só é selecionado quando FFmpeg está disponível, o output HLS backing existe e os limites de sessão permitem.

4. **Home Assistant e app continuam HLS-first.**
   Em app nativo ou HA com HLS proxy, a ordem efetiva começa por HLS. WebRTC fica bloqueado para app e tratado como baixa latência/PTZ, não como caminho padrão de estabilidade.

5. **Múltiplos streams por câmera/transmission.**
   O plano respeita `output_id` e `quality_profile_id`. Isso é obrigatório porque câmeras podem ter mais de um stream/output, e a política não pode assumir que o primeiro HLS disponível é o stream correto para fullscreen, grid, diagnóstico ou PTZ.

6. **Monitor HLS contínuo no web player.**
   O dashboard passa a monitorar avanço de playlist e disponibilidade do tail segment durante o playback. Se a sequência não avança por janela proporcional ao `TARGETDURATION`, o player registra telemetria, derruba a tentativa corrente e entra no fluxo de recovery/fallback em vez de manter freeze silencioso.

### Estado atual da escada

| Transporte | Estado nesta fatia | Observação |
|---|---|---|
| HLS | ativo | fallback estável e caminho preferido em app/HA. |
| WebRTC | ativo quando disponível | usado para baixa latência/PTZ ou escolha explícita; não é default para app/HA. |
| MSE | ativo quando sidecar está disponível | usa `go2rtc` v1.9.14 consumindo RTSP interno do MediaMTX e proxy assinado pelo Toposync. |
| JSMpeg | ativo como fallback visual sob demanda | WebSocket assinado; FFmpeg por sessão; frame selecionado/runtime ou placeholder; baixa resolução/FPS, sem áudio. |

### Regressões cobertas

- Web comum passivo seleciona MSE quando sidecar e output backing existem; sem sidecar, cai para HLS com motivo claro.
- App/HA seleciona HLS e bloqueia WebRTC como caminho padrão.
- Transmissão com múltiplos HLS por `quality_profile_id` seleciona o output solicitado, incluindo fullscreen, sem cair no primeiro HLS.
- O contrato de tipos aceita URL sintética `mse` derivada de output HLS/RTSP saudável sem gravar `TransmissionOutput(protocol="mse")` persistido.

### Validação desta fatia

- `uv run pytest tests -q`: 502 testes.
- `uv run pytest $(rg --files tests | rg 'test_streaming.*\.py$') -q`: 139 testes.
- `npm --workspace @toposync/frontend run build`: OK, com warning conhecido de tamanho de bundle.
- `npm --workspace @toposync/frontend run test:main2d`: 9 testes.
- `git diff --check`: OK.

### Próximos passos obrigatórios

1. Validar MSE com `go2rtc` em caos real: restart de MediaMTX, restart do sidecar, publisher frio e codec incompatível.
2. Validar JSMpeg em caos real: câmera offline, pipeline fria, queda de WebSocket, limite de sessões e CPU sob múltiplos tiles.
3. Medir custo de FFmpeg por sessão e ajustar defaults de `max_total_sessions`, `max_sessions_per_transmission`, FPS e bitrate.
4. Manter JSMpeg como último recurso visual; se ele aparecer frequentemente no Auto, isso deve abrir investigação de HLS/MSE, não virar caminho normal.

---

## 0.3. Atualização operacional: Home Assistant Cloud e contrato nativo de câmera

Em 2026-05-22, a estratégia do Toposync para Home Assistant foi separada em dois caminhos, seguindo a lição prática observada no Frigate: **player web próprio** e **entidade nativa do Home Assistant** não são o mesmo contrato.

### Decisão

- A UI do Toposync dentro do HA ingress continua HLS-first.
- WebRTC direto do player Toposync fica bloqueado por padrão no `client=ha_ingress`, porque HA ingress/Cloud não resolve por si só ICE, UDP, TURN e reachability do WHEP direto.
- Home Assistant Cloud deve usar entidades `camera` nativas do HA, para que o Core/stream component e o frontend do HA assumam o contrato de playback remoto.
- WebRTC nativo HA existe apenas como scaffold opt-in. A documentação oficial do HA indica que uma câmera que implementa WebRTC nativo passa a ser tratada como WebRTC e não usa o fallback HLS do stream component; por isso o default inicial fica desligado.

### O que ficou implementado

1. **Playback Plan com contexto HA explícito.**
   `GET /api/streams/transmissions/{id}/playback-plan?client=ha_ingress` retorna ordem `HLS -> MSE -> WebRTC -> JSMpeg` e bloqueia WebRTC direto com motivo operacional. `client=ha_entity` não escolhe player web; aponta para o contrato de câmeras HA.

2. **Manifesto HA-native.**
   `GET /api/streams/home-assistant/cameras` retorna as câmeras/live views exportáveis para HA, preservando `transmission_id`, `output_id`, `quality_profile_id`, URL de still, RTSP interno e capacidades. O manifesto opera sobre Transmission/output do Toposync; não retorna URL direta da câmera de origem.

3. **Still endpoint estável.**
   `GET /api/streams/transmissions/{id}/still.jpg?output_id=&quality_profile_id=` retorna JPEG do último frame recente ou placeholder explícito, com cache curto/negado e headers de estado (`x-toposync-frame-state`, idade do frame quando disponível).

4. **Heartbeat HA entity.**
   `demand/heartbeat` aceita `source=home_assistant_entity`, usa lease default de 90s e permite TTL até 300s. O writer bridge também aceita esse TTL maior, evitando que a resposta prometa mais tempo do que o publisher realmente mantém.

5. **Scaffold de integração Home Assistant.**
   `integrations/home_assistant/custom_components/toposync` registra entidades `camera`; `stream_source()` renova demanda e retorna RTSP interno do Toposync/MediaMTX; `async_camera_image()` usa o still endpoint. A opção `enable_native_webrtc` fica `false` por padrão e, quando habilitada, encaminha ofertas para `/api/streams/transmissions/{id}/webrtc/offer`.

### Múltiplos streams

O manifesto HA-native preserva a variante da live view: thumbnail/grid, fullscreen, PTZ e diagnóstico não caem no primeiro output disponível. A seleção carrega `quality_profile_id` e `output_id`; quando WebRTC nativo está habilitado, o offer endpoint também tenta casar o WebRTC companion pelo mesmo `quality_profile_id`.

### Regressões cobertas

| Cenário | Critério |
|---|---|
| HA ingress playback plan | seleciona HLS, bloqueia WebRTC direto e mantém fallback ladder. |
| HA entity playback plan | não escolhe player web e aponta para `/api/streams/home-assistant/cameras`. |
| Manifesto HA com múltiplas variantes | thumbnail usa `quad_grid`; fullscreen usa `fullscreen_quality`; não vaza URL RTSP da câmera de origem. |
| Manifesto HA com WebRTC opt-in | `webrtc_offer_url` preserva `quality_profile_id` da variante. |
| Still endpoint | retorna JPEG válido com frame live ou placeholder explícito. |
| Heartbeat HA entity | usa lease default de 90s e mantém o publisher sob demanda ativo por janela maior. |

### Próximos passos

1. Validar a integração em um Home Assistant real, local e via Nabu Casa, antes de habilitar WebRTC nativo por padrão.
2. Definir como o add-on expõe a URL interna ideal para o HA Core (`TOPOSYNC_HOME_ASSISTANT_RTSP_HOST` e porta RTSP interna/publicada).
3. Adicionar testes de integração com Home Assistant Core quando a dependência de teste estiver disponível.
4. Medir se HA Cloud remoto mantém HLS visualizável via entidade câmera durante restart de publisher/MediaMTX.

---

## 0.4. Atualização operacional: câmeras publicáveis, pipelines implícitos e UX sempre visualizável

Em 2026-05-23, o fluxo de uso foi reposicionado: o usuário não deve criar `Transmission`, escolher output e depois ligar pipeline para uma câmera comum. A intenção primária agora é **publicar uma fonte de câmera**. A extensão de streaming reconcilia automaticamente `CameraLiveView`, `Transmission`, outputs e pipelines implícitos.

Essa mudança reduz a superfície operacional exposta ao usuário e elimina a classe de erro mais comum observada nos testes: câmera existente, stream de origem saudável, mas nenhuma pipeline alimentando a transmissão.

### Decisão de produto

- A tela de câmeras é a fonte de verdade para streams normais de câmera.
- Cada fonte de câmera tem `Transmitir esta fonte`, papel (`main`, `sub`, `zoom`, `custom`) e nome visível.
- Fontes ONVIF de vídeo descobertas podem nascer publicadas por padrão.
- `Transmissions` continuam existindo como contrato técnico, mas viram artefatos gerados e visão avançada/diagnóstico.
- Pipelines implícitos de câmera são contínuos e gerados pelo reconciliador.
- O operador manual `stream.publish_video` deixa de ser uma escolha de transmissão existente e passa a ser uma intenção de publicar uma variante de câmera ou grupo.

### Modelo novo

O modelo de intenção é `StreamPublicationSpec`.

Campos centrais:

| Campo | Papel |
|---|---|
| `id` | ID determinístico da publicação. |
| `owner_kind` | `camera_source` ou `pipeline_output`. |
| `enabled` | Liga/desliga a publicação e seus artefatos gerados. |
| `camera_id` | Grupo/câmera de destino. |
| `camera_source_id` | Fonte concreta da câmera quando aplicável. |
| `pipeline_name` / `publish_node_id` | Origem manual quando a publicação vem de um operador de pipeline. |
| `role` | `main`, `sub`, `zoom` ou `custom`. |
| `label` | Nome visível para seletor/dashboard/HA. |
| `host_server_id` | Host efetivo da publicação, resolvido a partir de ingest/processing. |
| `quality_policy` | Perfil técnico gerado para outputs. |
| `transport_policy` | Política de transporte inicial da publicação. |

IDs determinísticos:

```text
camera:{camera_id}:{source_id}
pipeline:{pipeline_name}:{node_id}
```

Artefatos gerados recebem metadata de rastreabilidade:

```json
{
  "generated_by": "stream_publication",
  "publication_id": "camera:frente:sub",
  "owner_kind": "camera_source",
  "camera_id": "frente",
  "camera_source_id": "sub",
  "role": "sub",
  "camera_live_view_id": "camera:frente"
}
```

### Reconciliação

O reconciliador roda quando:

- uma câmera ou fonte é salva;
- uma descoberta ONVIF adiciona/atualiza fontes de vídeo;
- uma publicação é alterada pela API;
- uma pipeline é salva, habilitada, desabilitada ou removida;
- um operador `stream.publish_video` declara publicação manual;
- `POST /api/streams/reconcile` é chamado.

Para `owner_kind="camera_source"`, o reconciliador:

1. cria ou atualiza uma `CameraLiveView` por câmera;
2. cria uma `Transmission` por fonte publicada;
3. cria outputs HLS/WebRTC/RTSP compatíveis com o perfil da fonte;
4. cria uma pipeline implícita contínua `camera.source -> stream.publish_video`;
5. preserva ingest/centralizador via resolução da fonte de câmera, sem embutir regra específica de domínio no core;
6. remove ou desativa artefatos gerados quando a publicação é desligada.

Para `owner_kind="pipeline_output"`, o reconciliador:

1. lê a intenção declarada no nó `stream.publish_video`;
2. cria a publicação manual como variante de câmera/grupo;
3. cria a `Transmission` técnica correspondente;
4. grava o `transmission_id` gerado de volta no nó quando necessário;
5. desativa a publicação se a pipeline dona for desativada.

Regra importante: pipelines implícitos de câmera nunca devem colocar `stream.publish_video` atrás de motion gate, evento ou detecção event-only. Se houver pipeline manual com gate, ela deve ser classificada como variante manual/event-gated e diagnosticada como tal.

### UX resultante

Fluxo de câmera comum:

1. Usuário adiciona câmera ONVIF ou RTSP.
2. Fontes de vídeo aparecem com papel sugerido: principal, baixa resolução, zoom ou custom.
3. `Transmitir esta fonte` vem ligado para fontes de vídeo publicáveis.
4. Ao salvar, o reconciliador cria live view, transmissões, outputs e pipelines implícitos.
5. O dashboard já exibe a câmera sem o usuário abrir `Streaming avançado` ou `Pipelines`.

Fluxo de variante manual:

1. Usuário monta uma pipeline customizada.
2. No nó `stream.publish_video`, escolhe publicar como variante.
3. Escolhe grupo/câmera de destino, papel, nome visível e perfil.
4. Ao salvar, o reconciliador cria a transmissão gerada e a variante aparece no seletor do dashboard.

`Streaming avançado` continua útil para:

- diagnóstico de URLs, outputs e saúde runtime;
- inspeção de artefatos gerados;
- testes de protocolo;
- integrações externas;
- casos manuais raros.

Na UI normal, artefatos gerados por publicação são read-only e apontam para a fonte dona: câmera/fonte ou operador de pipeline.

### Seleção de fonte por contexto visual

O seletor primário do dashboard passa a escolher papel/fonte, não perfil técnico como "Auto" ou "Stable".

| Contexto | Fonte preferida |
|---|---|
| Grid, thumbnail, dashboard passivo | `sub`, depois `main`. |
| Fullscreen/large | `main`, depois `sub`. |
| PTZ/zoom | `zoom`, depois `main`, depois `sub`. |
| Diagnóstico ou rede ruim | menor fonte publicável, normalmente `sub` ou `custom` low. |
| Home Assistant entity | variante primária do grupo, preferindo `sub/stable` quando existir. |

Qualidade e transporte continuam existindo, mas ficam abaixo dessa camada. O primeiro erro de UX era tentar resolver qualidade/transporte antes de escolher a fonte correta da câmera.

### Política de transporte por contexto

Esta política substitui a ideia anterior de "WebRTC sempre primeiro no navegador".

| Cliente/contexto | Ordem padrão | Observações |
|---|---|---|
| `web`, grid/passivo LAN | `MSE -> HLS -> JSMpeg` | WebRTC não deve abrir para todos os tiles por padrão. |
| `web`, fullscreen sem interação | `MSE -> HLS -> JSMpeg` | Fullscreen troca para fonte `main`; se HEVC/H.265, transcodificar para H.264. |
| `web`, PTZ/baixa latência/two-way | `WebRTC -> MSE -> HLS -> JSMpeg` | WebRTC só vira erro principal quando foi solicitado explicitamente ou não há HLS saudável. |
| `ha_ingress` | `HLS signed proxy -> MSE proxied -> JSMpeg` | WebRTC direto bloqueado por padrão. |
| `ha_entity` / HA Cloud | contrato nativo HA | Toposync exporta `stream_source()` RTSP interno e still; HA decide o player. |
| `app` / PiP | `HLS -> MSE se aplicável -> JSMpeg` | WebRTC apenas em modo explícito de baixa latência. |
| remoto desconhecido/rede ruim | HLS baixo, depois JSMpeg | Se falhar, mostrar still recente ou placeholder com motivo claro. |
| `RTSP` | não é transporte web | Mantido para HA Core, VLC, Frigate/dev, go2rtc e diagnóstico. |

Estado atual:

- HLS, WebRTC e RTSP existem no runtime real via MediaMTX.
- MSE existe no runtime real quando o sidecar `go2rtc` está habilitado e rodando; o navegador usa apenas proxy WebSocket assinado do Toposync.
- JSMpeg existe no runtime real como fallback visual sob demanda, com encoder FFmpeg por sessão e fonte no frame selecionado da Transmission.

### Regras de saúde e diagnóstico

`Live` só é verdadeiro quando estes relógios avançam:

- frame selecionado recente;
- writer selecionado/ativo;
- fonte de câmera recente;
- publisher/output saudável;
- playlist HLS avançando quando o transporte ativo é HLS.

Regras de mensagem principal:

| Condição | Mensagem principal |
|---|---|
| `fallback_reason=no_frame` e sem writer | `Nenhuma pipeline está alimentando esta transmissão.` |
| writer presente, mas publisher/output parado | `Pipeline tem frame, mas publisher HLS/WebRTC não está rodando.` |
| fonte/pipeline velha | Mostrar idade do frame, writer esperado e output selecionado. |
| HLS saudável e WebRTC indisponível | Warning técnico secundário, não causa principal. |
| placeholder/still | Estado visual válido, mas nunca `Live`. |

O objetivo é que o usuário nunca veja freeze silencioso marcado como live.

### APIs relevantes

Publicações:

- `GET /api/streams/publications?camera_id=...`
- `PUT /api/streams/publications/camera-sources/{camera_id}/{source_id}`
- `POST /api/streams/reconcile`

Playback:

- `GET /api/streams/transmissions/{id}/playback-plan?client=web|app|ha_ingress|ha_entity&context=thumbnail|pip|large|fullscreen|ptz&low_latency=true|false`

Na UI, grid/dashboard passivo mapeia para `thumbnail` ou `large` conforme o layout; diagnóstico escolhe a menor variante publicável antes de chamar o plano.

Home Assistant:

- `GET /api/streams/home-assistant/cameras`
- `GET /api/streams/transmissions/{id}/still.jpg?output_id=&quality_profile_id=`
- `POST /api/streams/transmissions/{id}/webrtc/offer` quando WebRTC nativo HA estiver habilitado.

### Regressões cobertas nesta fatia

- Publicação de fonte de câmera gera uma transmissão por fonte, não por contexto visual.
- Desmarcar `Transmitir esta fonte` remove/desativa transmissão e pipeline geradas.
- Reconciliador respeita ingest/centralizador e host efetivo.
- Dashboard mostra papéis de câmera no seletor.
- Grid usa `sub`; fullscreen usa `main`; PTZ pode escolher `zoom`.
- `ha_ingress` continua HLS-first e não promove warning WebRTC quando HLS está saudável.
- Manifesto HA exporta grupos/câmeras e variantes sem URL direta da câmera ou credenciais brutas.
- Pipeline manual com `stream.publish_video` cria publicação de variante.
- Pipeline desativada desativa publicação gerada.

### Validação desta fatia

- `uv run pytest tests/test_streaming_camera_live_views.py tests/test_streaming_camera_ingest.py tests/test_streaming_webrtc.py tests/test_streaming_chaos.py tests/test_streaming_hardening_stage10.py tests/test_pipelines_api.py -q`: 69 testes.
- `npm run build:frontend`: OK, com warning conhecido de tamanho de bundle.
- `npm --workspace @toposync/extension-streaming-ui run build`: OK.
- `npm --workspace @toposync/extension-cameras-ui run build`: OK, com warning conhecido de tamanho de bundle.
- `python -m py_compile` nos módulos alterados de streaming/câmeras: OK.
- `git diff --check`: OK.
- Validação visual no browser local: dashboard, câmeras e streaming settings carregam sem erros de console.

### Pendências explícitas

1. Validar JSMpeg real em caos local: sessão fechando, câmera offline, publisher frio, limite de sessões e custo de CPU.
2. Validar MSE com `go2rtc` em restart de engine/sidecar, codec incompatível e HA ingress antes de tratá-lo como caminho universal.
3. Validar Home Assistant Cloud com entidade câmera em ambiente real antes de habilitar WebRTC HA nativo por padrão.
4. Criar testes de browser cobrindo troca automática de fonte entre grid, fullscreen e PTZ.

---

## 1. Evidências de código que mais importam

| Camada | Evidência atual | Risco prático | Decisão recomendada |
|---|---|---|---|
| Runtime state | `TransmissionRuntimeState` mantém último frame por writer e também último frame selecionado/incoming por transmission. Quando não há writer selecionado, pode retornar fallback com `writer_id=None`. | O player pode ver imagem congelada sem erro, porque o servidor ainda publica um frame antigo. | Introduzir `selected_frame_age_seconds`, `fallback_active`, `stale=true`, `fallback_reason` e política de placeholder/stop após N segundos. |
| Writer bridge | `StreamWriterBridge` usa `on_demand_stop_debounce_s=3.0`, atualiza viewer count a cada 1s e para publisher quando `viewer_count <= 0` sem prime/hint. | HLS pode oscilar em transições de playlist reload, background/PiP, buffering ou contagem intermitente do MediaMTX. | Para HLS ativo, usar heartbeat de demanda e grace maior que 30s. Para stream crítico, manter sempre ativo ou `hlsAlwaysRemux`. |
| App HLS readiness | `waitForHlsPlaybackReadiness` valida playlist inicial, variantes, segmentos, `EXT-X-MAP` e `EXT-X-KEY`. | Isso só prova que o stream estava pronto no começo, não que segue vivo. | Criar monitor HLS contínuo durante playback. |
| App player | `StreamPlayerCard` usa `expo-video`, `contentType: 'hls'`, watchdog de buffering e recovery limitado. | Recovery depende de eventos nativos e timeouts, mas não sabe se a playlist parou de avançar. | Conectar player, HLS liveness e demand heartbeat numa sessão única de playback. |
| App context | `StreamsDashboardContext` prima demanda, resolve URL, rejeita HLS autenticado, normaliza URL e chama readiness. | Fluxo de preparação é bom, mas falta ciclo de vida contínuo da sessão. | Adicionar `playback_session_id`, heartbeat prime e liveness monitor no mesmo contexto. |
| URL normalization | `normalizeStreamingOutputUrl` troca loopback e `.local` quando há IP conhecido. | Ainda pode sobrar porta errada, host remoto inacessível ou IP de container/processing. | Validar URL contra contrato de rede do servidor e expor erro específico. |
| MediaMTX config | Config atual gera `hls: true`, `hlsVariant: mpegts`, `metrics: false`, API interna, RTSP UDP/TCP. | Compatibilidade HLS está boa, mas telemetria MediaMTX fica incompleta. | Ativar métricas controladas e coletar HLS sessions, muxers, readers, jitter e frames descartados. |
| FFmpeg publisher | Publica H.264 via RTSP, usa `yuv420p`, GOP aproximado de 1s, `-sc_threshold 0`, reinicia em falha e faz fallback de hardware. | `frames_sent` pode subir mesmo com frame visualmente repetido; hardware pode falhar em runtime. | Medir repetição de frame e implementar quarentena de encoder por host/output. |
| Camera/pipeline | `camera.source` expõe hints de capture, motion gate default `emit_when_idle=false`, YOLO pode operar em `events` ou `annotate`. | Pipelines de evento/gate podem parecer “stream quebrado” se usados como vídeo contínuo. | Separar branch contínuo de vídeo e branch de analytics/eventos. |
| Home Assistant add-on | Contrato público mínimo: API/direct + HLS proxy em 18756, backend/ingress 18757, RTSP diagnóstico 18758, WHEP 18760 e UDP WebRTC 18762. HLS MediaMTX direto é interno por padrão. | Se a API montar URL sem prefixo de ingress, ou promover warning WebRTC/porta a causa raiz, o usuário vê erro/fallback mesmo com HLS saudável. | Usar helper único de URL pública de mídia, HLS-first em HA, snapshot de portas do Supervisor e `network_contract_error` apenas para erro bloqueante do transporte selecionado. |

---

## 2. Fundamentos de literatura e normas

Esta seção serve como base conceitual para as decisões. Ela não é “acadêmica por estética”; ela explica por que as prioridades estão ordenadas assim.

### 2.1. HLS é playlist viva, não arquivo estático

A especificação HLS define que o cliente consome playlists e segmentos. Em live stream, a playlist precisa avançar e os segmentos anunciados precisam estar disponíveis. Isso sustenta três obrigações práticas:

1. O app deve verificar `EXT-X-MEDIA-SEQUENCE` ou a lista de segmentos durante o playback.
2. O servidor deve alinhar GOP/keyframes à segmentação para reduzir risco de stalls.
3. O diagnóstico deve separar “URL responde” de “mídia está viva”.

**Referências:** RFC 8216, Apple HTTP Live Streaming, Apple HLS Authoring Specification.

### 2.2. QoE de vídeo é dominada por stalls e rebuffering

A literatura de QoE para HTTP Adaptive Streaming e vídeo online costuma modelar qualidade percebida a partir de atraso inicial, duração/frequência de stalls, qualidade visual, variação de qualidade e suavidade. Para produto, a conclusão é direta: **um 720p estável costuma ser melhor que um 1080p que congela**.

**Referências:** ITU-T P.1203; Seufert et al. 2015, survey de QoE em HTTP Adaptive Streaming; Dobrian et al. 2011; Krishnan e Sitaraman 2012.

### 2.3. RTP/RTSP exigem olhar para jitter, perda e transporte

RTSP/RTP são bons para ingest e diagnóstico. A estabilidade depende de transporte, perda, jitter, buffer e reconexão. Em LAN/Wi-Fi doméstica, TCP costuma ser mais previsível para câmeras, embora tenha latência maior que UDP.

**Referências:** RFC 3550, RFC 7826, documentação FFmpeg de protocolos, documentação de fabricantes ONVIF/RTSP.

### 2.4. Observabilidade precisa medir liveness, não só disponibilidade

SRE popularizou sinais como latência, tráfego, erros e saturação. Para vídeo ao vivo, estes sinais precisam ser traduzidos para uma cadeia temporal:

```text
freshness, continuity, availability, decodability, recoverability, quality
```

Um path MediaMTX “ready” ou um FFmpeg “running” não garante que o conteúdo visual é novo.

### 2.5. App nativo não deve depender de cookie JS para mídia

A API pode usar cookie/pairing. O HLS no player nativo deve ser tratado como outro consumidor. Para LAN, HLS aberto na rede local pode ser aceitável. Para remoto, o caminho mais sólido é URL assinada, token de mídia ou proxy HLS controlado.

**Referências:** Expo Video, AVFoundation/AVPlayer, Apple HLS, MediaMTX authentication docs.

---

## 3. Contratos de saúde por camada

Cada camada precisa declarar o que significa “saudável”. Sem esses contratos, a equipe só descobre problemas quando o usuário reclama de freeze.

### 3.1. Contrato da câmera/source

**Estado saudável:** a câmera entrega frames novos dentro do intervalo esperado.

| Métrica | Alerta inicial | Crítico | Interpretação |
|---|---:|---:|---|
| `source_frame_age_seconds` | > 3s | > 10s | Captura travou, câmera parou ou backend está reconectando. |
| `capture_fps` | < 50% do alvo por 10s | 0 por 10s | Pipeline não recebe frames suficientes. |
| `capture_restarts_total` | > 0 em 15min | > 3 em 15min | RTSP/backend instável. |
| `capture_backend` | mudança inesperada | fallback repetido | OpenCV/FFmpeg alternando por falhas. |

**Regra de produto:** nunca mostrar frame velho como se fosse live. Depois de 5 a 10 segundos de source stale, mostrar indicador ou placeholder.

### 3.2. Contrato do pipeline

**Estado saudável:** a branch que alimenta `stream.publish_video` é contínua, mesmo que analytics/eventos sejam intermitentes.

```text
camera.source
  -> branch_continuous_video
       -> fps_reducer / resize
       -> stream.publish_video

  -> branch_analytics
       -> motion_gate / detect / track
       -> events / overlays / metadata
```

**Regra de produto:** motion gate, YOLO em modo `events` e detecção/eventos não devem ser usados como única fonte do stream contínuo, a menos que a UI mostre explicitamente “sem movimento/evento”.

### 3.3. Contrato do runtime/arbitration

**Estado saudável:** há writer ativo, frame selecionado recente e sem fallback silencioso.

| Campo recomendado | Definição |
|---|---|
| `active_writer_id` | writer escolhido pela arbitration. |
| `selected_writer_id` | writer do frame efetivamente publicado. |
| `selected_frame_age_seconds` | idade monotônica do frame publicado. |
| `last_incoming_frame_age_seconds` | idade do último frame recebido de qualquer writer. |
| `fallback_active` | `true` quando o frame vem de último frame preservado. |
| `fallback_reason` | `no_active_writer`, `selected_writer_missing_frame`, `source_stale`, `publisher_placeholder`. |
| `stale` | `true` quando idade excede threshold configurado. |

**Regra:** fallback é aceitável por poucos segundos para UX, mas precisa ser visível no diagnóstico e, depois do threshold, no app.

### 3.4. Contrato do writer bridge/on-demand

**Estado saudável:** se existe player ativo, o publisher não deve ser parado por viewer count momentaneamente 0.

| Situação | Política recomendada |
|---|---|
| HLS ativo no app | heartbeat prime a cada 10s e stop grace > 30s. |
| PiP ativo | manter heartbeat e não suspender publisher. |
| Quad view | reduzir resolução/FPS, mas manter grace 15 a 30s. |
| Câmera favorita/crítica | considerar sempre ativo ou `hlsAlwaysRemux`. |
| Sem viewer real por > grace | parar publisher para economizar CPU. |

### 3.5. Contrato do publisher/FFmpeg

**Estado saudável:** FFmpeg está rodando, enviando frames novos, sem restart e com codec confiável.

| Métrica | Alerta |
|---|---|
| `frames_sent_rate` | abaixo de 50% do FPS alvo. |
| `repeated_frame_ratio` | > 0,8 por 10s. |
| `restart_count` | > 0 em 15min. |
| `active_codec_changed` | qualquer mudança inesperada. |
| `stderr_tail` | erro de device, RTSP publish, broken pipe ou encoder. |
| `last_frame_at_unix_age` | > 3s. |

**Regra:** `frames_sent` sozinho não é saúde. Ele precisa ser interpretado junto com freshness do frame selecionado.

### 3.6. Contrato MediaMTX/HLS

**Estado saudável:** path pronto, sessão HLS ativa, playlist avançando e tail segment baixável.

| Métrica | Alerta |
|---|---|
| `hls_media_sequence_age_seconds` | > 3x `TARGETDURATION`. |
| `tail_segment_http_status` | não 2xx/206. |
| `hls_sessions` | 0 com app ativo. |
| `hls_muxers_outbound_frames_discarded` | crescimento. |
| `path_ready` | false com publisher ativo. |
| `viewer_count_flaps_total` | crescimento durante playback. |

### 3.7. Contrato de API/URL/auth

**Estado saudável:** a URL retornada ao app é alcançável do dispositivo, usa porta exposta e não depende de auth nativa frágil.

URLs proibidas para app nativo:

```text
localhost
127.0.0.1
::1
0.0.0.0
IP de container
.local sem fallback confiável
porta ativa diferente da porta exposta pelo add-on
HLS requiresAuth=true enquanto o app não tiver token/proxy de mídia
```

### 3.8. Contrato do app React Native/Expo

**Estado saudável:** o app prepara, toca, monitora e recupera uma sessão de playback com contexto.

Campos mínimos por sessão:

```text
playback_session_id
transmission_id
output_id
hls_url_host
hls_url_port
app_state
pip_state
player_status
first_frame_at_ms
startup_time_ms
buffering_started_at_ms
stall_count
stall_duration_ms
hls_media_sequence
hls_last_segment_url
hls_last_change_at_ms
recovery_attempt_count
last_error_code
last_error_message
```

### 3.9. Contrato do frontend web

**Estado saudável:** o operador humano consegue responder “onde quebrou?” em menos de 30 segundos.

O dashboard web deve exibir por transmission:

```text
live / degraded / stale / offline
source frame age
selected frame age
active writer
viewer count
publisher running
frames sent rate
restart count
active codec
HLS URL and port status
last playlist sequence age
last error
```

### 3.10. Contrato do Home Assistant add-on

**Estado saudável:** HLS público usa a porta principal/API ou o ingress do HA via proxy assinado; RTSP/WebRTC usam portas próprias apenas quando o usuário habilita esses transportes.

```text
expected_direct_api_port = 18756
expected_backend_port = 18757
expected_rtsp_port = 18758
internal_hls_port = 18759
expected_webrtc_port = 18760
expected_webrtc_udp_port = 18762
public_hls_mode = proxy

if request_has_ingress_prefix and public_hls_mode == "proxy":
  do_not_compare_request_port_8090_to_direct_api_18756_as_root_cause = true

if public_hls_mode == "proxy":
  hls_url_must_be_api_proxy_url_with_ingress_prefix_when_present = true
  internal_hls_port_mismatch_is_diagnostic_not_public_blocker = true
```

Para WebRTC, `18760/tcp` e `18762/udp` continuam contrato público quando baixa latência/PTZ é usada. A ausência de `18762/udp` no snapshot do Supervisor é warning de WebRTC, não erro de HLS.

---

## 4. Prioridades

### Visão geral

| Prioridade | Tema | Resultado esperado | Camadas principais |
|---|---|---|---|
| P0.1 | Verdade de liveness/freshness | A plataforma para de dizer “live” quando está stale. | Server, App, Web |
| P0.2 | Monitor HLS contínuo no app | Freeze de playlist vira recuperação/erro claro. | App, Server |
| P0.3 | On-demand robusto para HLS | Publisher não cai no meio do playback por viewer_count oscilante. | Server, App, Web |
| P0.4 | Contrato de portas no HA/add-on | App não recebe URL inalcançável. | Add-on, Server, App, Web |
| P0.5 | Separar stream contínuo de eventos/gates | Motion/detection não congelam vídeo contínuo. | Server, Web |
| P1.1 | Observabilidade end-to-end | Diagnóstico em 30 segundos. | Server, App, Web |
| P1.2 | Resiliência FFmpeg/encoder | Menos restarts, fallback confiável, hardware controlado. | Server, Web |
| P1.3 | Saúde RTSP/câmera | Causa raiz antes do streaming fica visível. | Server, Web, App |
| P1.4 | Auth de mídia para remoto | HLS remoto seguro sem depender de cookie JS. | Server, App, Web |
| P2.1 | Perfis e ABR simples | Melhor qualidade visual sem sacrificar estabilidade. | Server, App, Web |
| P2.2 | WHEP/WebRTC para baixa latência | Dashboard e PTZ mais responsivos. | Server, Web |
| P2.3 | Testes de caos e aceitação | Regressões de streaming ficam detectáveis. | Server, App, Web, Add-on |

---

## 5. P0.1: verdade de liveness/freshness

### Objetivo

Impedir que um frame antigo seja apresentado como live. Esta é a prioridade mais importante, porque todos os outros diagnósticos dependem dela.

### Por que é P0

O runtime pode devolver fallback do último frame selecionado/incoming mesmo sem writer ativo. Isso melhora UX por alguns segundos, mas mascara falha. O usuário vê uma imagem congelada e a plataforma pode continuar com publisher ativo.

### Mudanças práticas

#### Backend/server

**Arquivos prováveis:**

- `extensions/streaming/src/toposync_ext_streaming/streaming/runtime_state.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/writer_bridge.py`
- `extensions/streaming/src/toposync_ext_streaming/api/models.py`
- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`

**Implementar:**

1. Ampliar `SelectedWriterFrame` com metadados:

```python
@dataclass(frozen=True, slots=True)
class SelectedWriterFrame:
    transmission_id: str
    writer_id: str | None
    frame: numpy.ndarray | None
    lifecycle_state: Lifecycle | None
    writer_priority: int
    frame_ts: float
    updated_at_monotonic: float
    selected_frame_age_seconds: float
    fallback_active: bool
    fallback_reason: str
    stale: bool
```

2. Calcular idade usando monotonic clock:

```python
selected_frame_age_seconds = now_monotonic - updated_at_monotonic
```

3. Adicionar snapshot por transmission:

```json
{
  "active_writer": "pipeline:node",
  "selected_writer": "pipeline:node",
  "selected_frame_age_seconds": 0.42,
  "last_incoming_frame_age_seconds": 0.40,
  "fallback_active": false,
  "fallback_reason": "",
  "stale": false
}
```

4. Adicionar política configurável:

```yaml
stale_policy:
  warning_after_seconds: 3
  placeholder_after_seconds: 8
  stop_publisher_after_seconds: 30
  fallback_mode: placeholder
```

5. No `writer_bridge`, aplicar placeholder quando stale exceder threshold:

```text
if selected.stale and selected.age >= placeholder_after_seconds:
  publish stale placeholder with last live timestamp
else:
  publish selected frame
```

6. Garantir que `frames_sent` não seja interpretado como saúde se `selected_frame_age_seconds` estiver velho.

#### App React Native/Expo

**Arquivos prováveis:**

- `src/features/streams/streams-dashboard-context.tsx`
- `src/features/streams/stream-player-card.tsx`
- novo `src/features/streams/stream-health.ts`

**Implementar:**

1. Consumir health/diagnostics quando playback estiver ativo.
2. Se server reportar `stale=true`, trocar overlay para mensagem específica:

```text
Stream stale. Last live frame: 14:03:22. Recovering...
```

3. Diferenciar `stale_source`, `stale_hls`, `publisher_down`, `unreachable` e `unauthorized`.

#### Frontend/web

**Arquivos prováveis:**

- `frontend/src/.../streaming` ou área de settings/diagnostics da extensão.

**Implementar:**

1. Badge por transmission: `Live`, `Degraded`, `Stale`, `Offline`.
2. Colunas: source age, selected frame age, active writer, fallback, viewer count, publisher status.
3. Botão “baixar pacote de diagnóstico”.

### Critérios de aceite

- Quando câmera/source para por 30s, o app não mostra frame velho como live.
- `/api/streams/runtime/diagnostics` mostra `stale=true` em até 10s.
- `frames_sent` pode subir, mas diagnóstico mostra `selected_frame_age_seconds` velho.
- Web dashboard permite identificar a transmissão stale sem abrir logs.

---

## 6. P0.2: monitor HLS contínuo no app

### Objetivo

Detectar quando a playlist HLS deixa de avançar durante playback, não apenas no startup.

### Por que é P0

O app já valida readiness inicial, mas freeze após alguns minutos exige monitor contínuo. HLS live saudável deve ter playlist e segmentos avançando.

### Mudanças práticas

#### App React Native/Expo

**Arquivos prováveis:**

- `src/features/streams/hls-readiness.ts`
- novo `src/features/streams/hls-liveness.ts`
- `src/features/streams/streams-dashboard-context.tsx`
- `src/features/streams/stream-player-card.tsx`

**Implementar novo módulo `hls-liveness.ts`:**

```ts
export interface HlsLivenessSample {
  mediaPlaylistUrl: string;
  targetDurationSeconds: number;
  mediaSequence?: number;
  tailSegmentUrl?: string;
  playlistChanged: boolean;
  tailSegmentReachable: boolean;
  sampledAtMs: number;
}
```

**Lógica:**

```text
1. Buscar master playlist.
2. Resolver media playlist, se houver variant.
3. Ler #EXT-X-TARGETDURATION.
4. Ler #EXT-X-MEDIA-SEQUENCE.
5. Resolver os 1 ou 2 últimos segmentos.
6. Testar segmento final com Range bytes=0-1.
7. Repetir a cada max(2s, targetDurationSeconds).
8. Se sequence/tail não muda por 3x TARGETDURATION, marcar stale_hls.
9. Se segmento anunciado não responde 2xx/206, marcar hls_tail_unavailable.
10. Acionar recovery com backoff e limite.
```

**Integração no contexto:**

- Criar `playback_session_id` quando state vira `ready`.
- Iniciar monitor quando `shouldStream=true` e `playbackState.status='ready'`.
- Parar monitor no suspend, troca de instância, mudança de URL ou PiP stop.
- Compartilhar resultado com `StreamPlayerCard`.

#### Backend/server

**Arquivos prováveis:**

- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`
- `extensions/streaming/src/toposync_ext_streaming/api/models.py`

**Implementar opcional:**

- Endpoint de probe server-side para debug:

```text
GET /api/streams/transmissions/{id}/hls/probe
```

Isso ajuda quando o dispositivo não consegue fazer curl/diagnóstico direto.

#### Frontend/web

Adicionar painel de HLS probe:

```text
playlist reachable
media sequence
target duration
last segment
last segment HTTP status
last playlist change
```

### Critérios de aceite

- Se MediaMTX para de gerar segmentos, o app detecta em até `3 * TARGETDURATION + 5s`.
- O erro exibido diferencia “playlist travada” de “player deu erro” e “API sem auth”.
- Recovery não gera loop infinito; depois de N tentativas mostra estado estável e acionável.

---

## 7. P0.3: on-demand robusto para HLS ativo

### Objetivo

Evitar que o publisher seja parado no meio da reprodução por flutuação de `viewer_count`.

### Por que é P0

O writer bridge atual para publisher após janela curta sem viewer. Para HLS, viewer count pode oscilar em transições de buffering, reload de playlist, fullscreen, background ou PiP.

### Mudanças práticas

#### Backend/server

**Arquivos prováveis:**

- `extensions/streaming/src/toposync_ext_streaming/streaming/writer_bridge.py`
- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`
- `extensions/streaming/src/toposync_ext_streaming/api/models.py`

**Implementar:**

1. Política por protocolo:

```yaml
on_demand:
  enabled: true
  stop_debounce_seconds:
    hls: 30
    rtsp: 8
    webrtc: 10
  prime_ttl_seconds: 60
  active_playback_heartbeat_ttl_seconds: 20
```

2. Endpoint `demand/heartbeat` com TTL explícito:

```text
POST /api/streams/transmissions/{id}/demand/heartbeat
body: {
  playback_session_id: "...",
  output_id: "hls_stable_apple_tv",
  quality_profile_id: "stable_apple_tv",
  transport: "hls",
  ttl_seconds: 45
}
```

3. Guardar `last_active_playback_heartbeat_at` por publisher/transmission.
4. Não parar publisher HLS enquanto heartbeat estiver válido.
5. Registrar evento quando publisher for parado por idle:

```json
{
  "event": "publisher_stopped_idle",
  "publisher_id": "...",
  "viewer_count": 0,
  "idle_seconds": 31.2,
  "last_heartbeat_age_seconds": 45.8
}
```

#### App React Native/Expo

**Arquivos prováveis:**

- `src/features/streams/streams-dashboard-context.tsx`
- `src/core/networking/streaming-api.ts`

**Implementar:**

1. Enquanto `shouldStream=true`, enviar heartbeat a cada 10s.
2. Pausar heartbeat ao suspender playback, exceto PiP ativo.
3. Incluir `playback_session_id`.
4. Em erro de API heartbeat, não parar o player imediatamente, mas registrar warning e deixar HLS liveness decidir.

#### Frontend/web

- Expor configuração de stop grace por protocolo.
- Mostrar `last heartbeat`, `viewer count` e razão de parada do publisher.

### Critérios de aceite

- Durante reprodução HLS, `viewer_count=0` por poucos segundos não derruba publisher.
- Ao fechar app ou sair da tela sem PiP, publisher para após grace configurado.
- Logs explicam por que publisher parou.
- Erro de heartbeat vira telemetria/diagnóstico e não muda estado do player sozinho.

---

## 8. P0.4: contrato de portas no Home Assistant add-on

### Objetivo

Garantir que a URL HLS entregue ao app/web seja pública e tocável como está, usando proxy assinado na porta principal/API por padrão. Portas RTSP/WebRTC continuam explícitas para diagnóstico e baixa latência.

### Por que é P0

A API pode estar saudável via HA ingress ou `18756`, mas a playlist HLS pode ser reescrita sem o prefixo público correto. Isso gera 404 em media playlist/segmentos e pode virar tela preta, fallback HLS em loop ou falso `network_contract_error`.

### Mudanças práticas

#### Home Assistant add-on

**Arquivos prováveis:**

- `toposync/run_addon.py`
- `toposync/config.yaml`

**Implementar:**

1. Declarar envs de contrato:

```text
TOPOSYNC_EXPECTED_DIRECT_API_PORT=18756
TOPOSYNC_EXPECTED_RTSP_PORT=18758
TOPOSYNC_EXPECTED_WEBRTC_PORT=18760
TOPOSYNC_EXPECTED_WEBRTC_UDP_PORT=18762
TOPOSYNC_FAIL_STREAM_URLS_ON_PORT_MISMATCH=1
TOPOSYNC_STREAMING_HLS_PUBLIC_MODE=proxy
TOPOSYNC_ADDON_NETWORK_SNAPSHOT_PATH=/data/runtime/streaming/addon-network.json
```

2. Não publicar `18759/tcp` como requisito público quando `public_hls_mode=proxy`.
3. No startup, escrever snapshot das portas realmente mapeadas pelo Supervisor.
4. Usar o snapshot para warnings WebRTC, por exemplo UDP `18762/udp` ausente.

#### Backend/server

**Arquivos prováveis:**

- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/engine_manager.py`
- `extensions/streaming/src/toposync_ext_streaming/api/models.py`

**Implementar:**

1. Criar helper único de URL pública de mídia:

```text
media_url_origin = request.scheme + "://" + request.host + request.root_path_or_x_ingress_path
```

2. Usar esse helper em `/transmissions/{id}/urls`, rewrite do proxy HLS, `engine/status` e diagnostic snapshot.
3. Em `public_hls_mode=proxy`, retornar HLS como URL de API/proxy:

```json
{
  "engine_running": true,
  "outputs": [
    {
      "protocol": "hls",
      "url": "http://homeassistant.local:8090/api/hassio_ingress/<id>/api/streams/media/hls/front/index.m3u8?media_token=..."
    }
  ]
}
```

4. Em `network_contract`, separar porta direta de origem pública de mídia:

```json
{
  "network_contract": {
    "public_hls_mode": "proxy",
    "public_base_path": "/api/hassio_ingress/<id>",
    "media_url_origin": "http://homeassistant.local:8090/api/hassio_ingress/<id>",
    "blocking_errors": [],
    "warnings": []
  }
}
```

5. `network_contract_error` só deve ser root cause quando houver `blocking_errors` que afetem o transporte selecionado. Warnings de WebRTC não devem esconder HLS saudável.

#### App React Native/Expo

- Aceitar HLS assinado com `requires_auth=false` e renovar URL antes de expirar.
- Enviar `demand/heartbeat` durante playback/PiP.
- Não derrubar player por erro transitório de health/telemetria/heartbeat.
- Mostrar erro específico apenas quando houver erro bloqueante real:

```text
Secure HLS URL could not be renewed. Check connection and sign in again.
```

#### Frontend/web

- Em Home Assistant + HLS proxy, Auto deve usar HLS-first em qualquer navegador.
- PTZ aberto pode pedir WebRTC temporariamente; falha WHEP cai para HLS sem overlay crítico.
- Antes de entregar HLS ao `<video>`, fazer probe leve de master/media playlist e tail segment.
- Renovar signed URL ao voltar para foreground e em 401/403.
- Enviar `demand/heartbeat` enquanto tile/PiP/PTZ estiver ativo.
- Modal avançada mostra `public_base_path`, `media_url_origin`, resultado do último probe HLS e warnings WHEP/ICE.

### Critérios de aceite

- URL HLS retornada por ingress inclui `/api/hassio_ingress/<id>/...`.
- Playlist reescrita preserva prefixo de ingress em variantes, segmentos, `EXT-X-MAP` e `EXT-X-KEY`.
- Ingress `:8090` não vira mismatch contra `18756`.
- HLS proxy funciona mesmo sem `18760/18762`.
- WebRTC só vira erro raiz quando for forçado ou não houver fallback HLS saudável.
- Web/app mantêm publisher vivo com heartbeat enquanto playback estiver ativo.

---

## 9. P0.5: separar stream contínuo de eventos/gates

### Objetivo

Evitar que pipelines de movimento/detecção sejam confundidos com vídeo contínuo.

### Por que é P0

`motion_gate` pode não emitir quando não há movimento. YOLO em `events` pode filtrar frames sem detecção. Isso é correto para eventos, mas péssimo se a transmission promete vídeo live contínuo.

### Mudanças práticas

#### Backend/server

**Arquivos prováveis:**

- `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`
- `extensions/streaming/src/toposync_ext_streaming/pipelines/operators.py`
- `src/toposync/runtime/pipelines/templates/*`
- `extensions/streaming/src/toposync_ext_streaming/wizard.py`

**Implementar:**

1. Preset `continuous_camera_stream` sempre com branch contínua.
2. Preset `analytics_stream` com branch contínua + branch analytics.
3. Validação de pipeline:

```text
if stream.publish_video is downstream only of motion_gate emit_when_idle=false:
  warn: "This stream may be event-gated and not continuous."
```

4. Para YOLO, preferir `emit_mode='annotate'` quando o output alimenta stream visual contínuo.
5. Se o produto desejar stream event-gated, adicionar estado visual explícito:

```text
No motion detected. Last live frame: 14:03:22.
```

#### Frontend/web

- Wizard deve perguntar: “quer vídeo contínuo ou só eventos?”
- Mostrar warning quando uma transmission é alimentada por gate/event-only.
- Exibir árvore de pipeline ligada à transmission.

#### App React Native/Expo

- Só precisa consumir estado `event_gated_idle` se server expuser.
- Mostrar overlay diferente de erro:

```text
No event currently. Waiting for motion...
```

### Critérios de aceite

- Transmission de câmera padrão nunca congela por ausência de movimento.
- Transmission event-gated tem rótulo claro e não é tratada como falha.
- Usuário consegue ver no web UI qual pipeline/writer alimenta cada stream.

---

## 10. P1.1: observabilidade end-to-end

### Objetivo

Dar ao operador e às IAs que vão planejar mudanças uma visão correlacionada de ponta a ponta.

### Mudanças práticas

#### Backend/server

**Arquivos prováveis:**

- `extensions/streaming/src/toposync_ext_streaming/streaming/mediamtx_config.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/mediamtx_api_client.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/publisher_manager.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/runtime_state.py`
- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`
- `extensions/streaming/src/toposync_ext_streaming/api/models.py`

**Implementar:**

1. Tornar métricas MediaMTX configuráveis:

```yaml
metrics: true
metricsAddress: 127.0.0.1:9998
```

2. Criar endpoint consolidado:

```text
GET /api/streams/runtime/health
```

3. Agregar:

```text
engine status
MediaMTX paths/readers/hls sessions
publisher status
runtime selected frame age
camera source health
demand/heartbeat
network contract
```

4. Logs estruturados com `playback_session_id` quando app enviar.

#### App React Native/Expo

- Gerar `playback_session_id` por tentativa real de playback.
- Enviar em prime/heartbeat e, se houver endpoint, reportar eventos de player.
- Salvar logs locais curtos por sessão para debug.

#### Frontend/web

- Página “Streaming Health”.
- Tabela por transmissão.
- Timeline simples de eventos.
- Exportar pacote JSON/ZIP de diagnóstico.

### Critérios de aceite

- Em freeze, em menos de 30 segundos é possível classificar:
  - source/pipeline stale;
  - publisher down;
  - HLS playlist stale;
  - port mismatch;
  - auth/url error;
  - app/player lifecycle.

---

## 11. P1.2: resiliência FFmpeg/encoder

### Objetivo

Reduzir restarts e tornar hardware encoder seguro por host/output.

### Mudanças práticas

#### Backend/server

**Arquivos prováveis:**

- `extensions/streaming/src/toposync_ext_streaming/streaming/publisher_manager.py`
- `extensions/streaming/src/toposync_ext_streaming/api/models.py`
- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`

**Implementar:**

1. Estado de confiança de encoder:

```text
candidate -> trusted -> quarantined
```

2. Quarentena se:

```text
restart_count > 2 em 10min
ou fallback por erro de device/driver
ou stderr contém erro conhecido de hardware
```

3. Persistir por host:

```json
{
  "host_id": "local",
  "encoder": "h264_videotoolbox",
  "state": "quarantined",
  "until_unix": 1770000000,
  "reason": "runtime_failure"
}
```

4. Perfil conservador default para estabilidade:

```text
libx264, yuv420p, GOP 1s, no B-frames para perfis low latency/WebRTC, bitrate controlado
```

#### Frontend/web

- Mostrar codec ativo e estado de confiança.
- Permitir “forçar libx264” para teste.
- Mostrar `stderr_tail` de forma higienizada.

#### App React Native/Expo

- Não precisa mexer diretamente, exceto consumir estado `publisher_restarting` para overlay.

### Critérios de aceite

- Falha de hardware não causa restart loop prolongado.
- Fallback para CPU é visível no UI.
- Teste de 2h single stream roda com `restart_count=0`.

---

## 12. P1.3: saúde RTSP/câmera

### Objetivo

Identificar se a falha nasce antes do streaming: câmera, RTSP, ONVIF, backend FFmpeg/OpenCV ou rede.

### Mudanças práticas

#### Backend/server

**Arquivos prováveis:**

- `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/camera_hub.py`
- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`

**Implementar:**

1. Métricas por source:

```json
{
  "camera_id": "front",
  "backend": "ffmpeg",
  "source_frame_age_seconds": 0.8,
  "capture_fps": 14.7,
  "target_fps": 15,
  "restarts_total": 1,
  "last_error": "",
  "rtsp_transport": "tcp",
  "used_ingest": true
}
```

2. Expor `source_frame_age_seconds` no pacote/telemetria de pipeline.
3. Preferir substream para quad/detecção.
4. Runbook automático: testar RTSP direto, comparar main/sub, exibir backend atual.

#### Frontend/web

- Tela de câmera com health, backend, FPS, último frame, restarts.
- Botão “testar RTSP” ou “coletar diagnóstico”.

#### App React Native/Expo

- Mostrar erro específico `Camera source stale` quando server reportar.

### Critérios de aceite

- Quando a câmera é desconectada, a causa aparece como `source_stale`, não como erro genérico de player.
- Reconnect da câmera recupera sem restart manual.

---

## 13. P1.4: auth de mídia para remoto

### Objetivo

Permitir HLS remoto seguro sem depender de cookies/headers do player nativo.

### Mudanças práticas

#### Backend/server

**Arquivos prováveis:**

- `src/toposync/runtime/auth.py`
- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`
- `extensions/streaming/src/toposync_ext_streaming/api/models.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/mediamtx_config.py`

**Implementar opções em ordem de maturidade:**

1. HLS signed proxy pela API principal, com token de mídia de TTL curto e rewrite de playlist/segmentos:

```text
http://host:18756/api/streams/media/hls/front/index.m3u8?media_token=...
http://homeassistant.local:8090/api/hassio_ingress/<id>/api/streams/media/hls/front/index.m3u8?media_token=...
```

2. URL assinada por transmission/output/device:

```json
{
  "scope": "stream:hls:read",
  "transmission_id": "front",
  "output_id": "hls-low",
  "expires_at": 1770000000
}
```

3. Modo `open` ou HLS direto apenas como opção explícita de LAN/diagnóstico, com aviso forte.

#### App React Native/Expo

- Aceitar HLS com token na URL.
- Renovar URL antes de expirar quando playback continuar.
- Continuar rejeitando `requiresAuth=true` se depender de header/cookie nativo não garantido.

#### Frontend/web

- Configuração clara: LAN aberto, URL assinada, proxy HLS.
- Aviso forte se usuário expor HLS aberto para internet.

### Critérios de aceite

- App toca HLS remoto seguro sem cookies JS.
- URL expirada falha com erro específico e renovação controlada.

---

## 14. P2.1: perfis e ABR simples

### Objetivo

Melhorar qualidade visual sem sacrificar estabilidade.

### Mudanças práticas

#### Backend/server

**Arquivos prováveis:**

- `extensions/streaming/src/toposync_ext_streaming/api/models.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/writer_bridge.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/publisher_manager.py`

**Perfis iniciais:**

| Perfil | Resolução | FPS | Bitrate | Uso |
|---|---:|---:|---:|---|
| `quad_grid` | 640x360 | 10 | 500 kbps | 4 câmeras simultâneas. |
| `stable_apple_tv` | 1280x720 | 15 | 1800 kbps | padrão de estabilidade. |
| `fullscreen_quality` | 1920x1080 | 15 | 3500 kbps | tela cheia, rede boa. |
| `diagnostic_low` | 426x240 | 5 | 250 kbps | rede ruim/debug remoto. |

**Implementar:**

1. Outputs low/high por transmission.
2. App escolhe perfil por grid mode e device/network.
3. Futuramente, master playlist ABR se o MediaMTX/arquitetura suportar de forma limpa.

#### App React Native/Expo

- Em quad, preferir low output.
- Em fullscreen, trocar para high output se liveness estiver estável.
- Não aumentar qualidade durante recuperação.

#### Frontend/web

- UI de perfis por transmission.
- Mostrar custo estimado de CPU/rede.

### Critérios de aceite

- Quad view roda 1h sem stalls recorrentes.
- Single fullscreen melhora nitidez sem restart de publisher inesperado.

---

## 15. P2.2: WHEP/WebRTC para baixa latência

### Objetivo

Usar baixa latência onde ela realmente importa: dashboard web, PTZ e interatividade.

### Mudanças práticas

#### Backend/server

**Arquivos prováveis:**

- `extensions/streaming/src/toposync_ext_streaming/streaming/mediamtx_config.py`
- `extensions/streaming/src/toposync_ext_streaming/api/models.py`
- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`

**Implementar:**

- `webrtcAdditionalHosts` consistente.
- UDP configurado e validado.
- Fallback quando UDP bloqueado.
- H.264 sem B-frames para WebRTC.

#### Frontend/web

- Em `Auto`, usar HLS-first quando `environment=home_assistant_addon` e `public_hls_mode=proxy`.
- Player WHEP para baixa latência quando usuário força “Baixa latência” ou abre PTZ.
- Tratar WHEP 404/readiness inicial como retry/fallback por janela curta.
- Mostrar ICE state, RTT, packet loss, jitter.

#### App React Native/Expo

- Manter HLS como principal no Apple TV/mobile até haver cliente WebRTC nativo robusto.

### Critérios de aceite

- Dashboard web com latência menor que HLS.
- HLS continua fallback estável.
- Falha WebRTC recuperada por HLS não aparece como causa raiz principal.

---

## 16. P2.3: testes de caos e aceitação

### Objetivo

Transformar os bugs de streaming em cenários reproduzíveis.

### Testes obrigatórios

| Teste | Esperado | Camadas |
|---|---|---|
| Desconectar câmera por 30s | App mostra stale/offline, recupera ao reconectar. | Server, App, Web |
| HA ingress HLS | URL e playlist reescrita preservam `/api/hassio_ingress/<id>`. | Add-on, Server, Web |
| HLS proxy com porta interna divergente | Sistema não exige `18759` publicamente quando `public_hls_mode=proxy`. | Add-on, Server, App, Web |
| Matar FFmpeg | Publisher reinicia ou falha com estado claro. | Server, Web |
| Forçar viewer_count=0 por 5s | HLS ativo não é derrubado. | Server, App |
| Congelar playlist HLS | App detecta `stale_hls` e recupera/avisa. | App, Server |
| WHEP 404 inicial com HLS saudável | Evento fica debug/fallback e não vira causa raiz. | Server, Web |
| Trocar fullscreen/PiP/background | Sem stream fantasma, sem queda indevida. | App, Server |
| Quad 4 streams por 1h | CPU estável, stalls dentro do limite. | Server, App |

### Critérios globais de aceite

```text
single stream 2h: zero freeze silencioso
quad 1h: sem queda sistemática de publisher
selected_frame_age_seconds p99 < 3s quando source saudável
hls_media_sequence_age_seconds p99 < 3 * target_duration
encoder_restart_count = 0 no perfil stable_apple_tv
blocking network contract detectado antes do app tocar
HA ingress nao reporta 8090 vs 18756 como causa raiz quando HLS proxy esta saudavel
```

---

## 17. Matriz de responsabilidades por prioridade

| Prioridade | Backend/server | Frontend/web | App React Native/Expo | Add-on HA |
|---|---|---|---|---|
| P0.1 Freshness/stale | Runtime state, writer bridge, diagnostics API. | Health badges e tabela de diagnóstico. | Overlay stale e consumo de health. | Não aplicável. |
| P0.2 HLS liveness | Probe opcional e exposição de config HLS. | Painel de probe. | Monitor playlist/segmentos durante playback. | Não aplicável. |
| P0.3 On-demand | `demand/heartbeat`, lease TTL, eventos de parada. | Heartbeat do tile e visualização de demand. | Heartbeat enquanto toca/PiP. | Não aplicável. |
| P0.4 Portas/ingress | Helper de URL pública, HLS proxy, contrato bloqueante por transporte. | HLS-first no HA, probe HLS e diagnóstico de ingress. | HLS assinado tocável como retornado, sem falso port mismatch. | Snapshot Supervisor e portas públicas mínimas. |
| P0.5 Pipelines contínuos | Wizard/templates/validação de graph. | UI de pipeline ligado ao stream. | Mensagem event-gated, se houver. | Não aplicável. |
| P1.1 Observabilidade | Métricas MediaMTX, health endpoint, logs estruturados. | Dashboard e pacote diagnóstico. | Playback session telemetry. | Expor info de rede/portas. |
| P1.2 Encoder | Quarentena e perfis FFmpeg. | Mostrar codec/restart/stderr. | Overlay publisher restarting. | Não aplicável. |
| P1.3 Câmera | Capture metrics e runbook RTSP. | Tela de health da câmera. | Mensagem source stale. | Não aplicável. |
| P1.4 Auth mídia | Tokens/URL assinada/proxy HLS. | Configuração de segurança. | Usar URL assinada e renovar. | Rede não expor HLS aberto remoto. |
| P2.1 ABR/perfis | Outputs low/high e perfis. | UI de perfis. | Seleção por grid/fullscreen. | Não aplicável. |
| P2.2 WHEP | Config WebRTC/MediaMTX. | Player WHEP e stats. | Fallback HLS. | Porta UDP/TCP. |
| P2.3 Chaos tests | Testes integração e scripts. | Botões/debug e status. | E2E de lifecycle/player. | Teste porta ocupada. |

---

## 18. Golden signals finais

### 18.1. Server

```text
source_frame_age_seconds
selected_frame_age_seconds
last_incoming_frame_age_seconds
fallback_active
stale
active_writer_id
viewer_count
last_playback_heartbeat_age_seconds
publisher_running
publisher_frames_sent_rate
publisher_restart_count
publisher_active_codec
publisher_hardware_accelerated
repeated_frame_ratio
mediamtx_path_ready
mediamtx_hls_sessions
mediamtx_hls_muxers_outbound_frames_discarded
network_contract_status
```

### 18.2. App

```text
playback_session_id
startup_time_ms
first_frame_time_ms
hls_media_sequence_age_seconds
tail_segment_http_status
stall_count
stall_duration_ms
recovery_attempt_count
player_status
pip_state
app_state
heartbeat_success_rate
```

### 18.3. Web/frontend

```text
health_state_by_transmission
diagnostic_snapshot_downloads
operator_action_reclaim_engine
operator_action_force_cpu_encoder
operator_action_test_rtsp
```

---

## 19. Runbook rápido de classificação de freeze

Quando o Apple TV/mobile congelar:

1. **Playlist HLS avança?**
   - Não: HLS/MediaMTX/publisher/on-demand.
   - Sim: ir para 2.
2. **Imagem igual, mas playlist avança?**
   - Ver `selected_frame_age_seconds`.
   - Se velho: runtime/source stale mascarado.
   - Se recente: cena estática real ou frame repetido por pipeline.
3. **`viewer_count` caiu para 0 antes da parada?**
   - Sim: on-demand agressivo.
4. **`publisher.restart_count` subiu?**
   - Sim: FFmpeg/encoder/MediaMTX.
5. **`source_frame_age_seconds` velho?**
   - Sim: câmera/RTSP/pipeline.
6. **A URL HLS pública contém o prefixo correto de ingress/proxy?**
   - Não: contrato HA/add-on/proxy HLS.
7. **Há warning WebRTC, mas HLS está saudável?**
   - Sim: tratar como fallback/diagnóstico, não causa raiz do playback.
8. **Só o Apple TV/mobile congela, mas VLC/curl seguem?**
   - App lifecycle, AVPlayer, PiP/background ou rede do dispositivo.

---

## 20. Decisões recomendadas de produto

1. **Default para Apple TV:** HLS `mpegts`, 720p, 15 FPS, bitrate moderado, GOP 1s.
2. **Default para quad:** output low, 360p ou 540p, 10 FPS.
3. **Default para câmera crítica:** manter publisher quente ou grace maior.
4. **Default para analytics:** stream contínuo limpo + metadados/eventos separados.
5. **Default para segurança LAN/remoto:** API autenticada e HLS signed proxy pela porta principal.
6. **Default para diagnóstico avançado:** HLS direto/open e RTSP só quando explicitamente habilitados.
7. **Default para UX:** stale explícito após poucos segundos, não freeze silencioso.

---

## 21. Referências normativas, técnicas e de literatura

### Normas e especificações

1. IETF RFC 8216, **HTTP Live Streaming**.
   https://datatracker.ietf.org/doc/html/rfc8216
2. IETF RFC 3550, **RTP: A Transport Protocol for Real-Time Applications**.
   https://datatracker.ietf.org/doc/html/rfc3550
3. IETF RFC 7826, **Real-Time Streaming Protocol Version 2.0**.
   https://datatracker.ietf.org/doc/html/rfc7826
4. Apple Developer, **HTTP Live Streaming**.
   https://developer.apple.com/streaming/
5. Apple Developer, **HLS Authoring Specification for Apple Devices**.
   https://developer.apple.com/documentation/http-live-streaming/hls-authoring-specification-for-apple-devices
6. W3C, **WebRTC Statistics API**.
   https://www.w3.org/TR/webrtc-stats/

### QoE e literatura aplicada

7. ITU-T Recommendation P.1203, **Parametric bitstream-based quality assessment model for progressive download and adaptive audiovisual streaming services**.
8. Seufert, M.; Egger, S.; Slanina, M.; Zinner, T.; Hoßfeld, T.; Tran-Gia, P. **A Survey on Quality of Experience of HTTP Adaptive Streaming**, IEEE Communications Surveys & Tutorials, 2015.
9. Dobrian, F. et al. **Understanding the Impact of Video Quality on User Engagement**, ACM SIGCOMM, 2011.
10. Krishnan, S. S.; Sitaraman, R. K. **Video Stream Quality Impacts Viewer Behavior: Inferring Causality Using Quasi-Experimental Designs**, ACM IMC, 2012.
11. Mok, R. K. P.; Chan, E. W. W.; Chang, R. K. C. **Measuring the Quality of Experience of HTTP Video Streaming**, IFIP/IEEE IM, 2011.

### Documentação de implementação

12. MediaMTX, **Configuration file reference**.
    https://mediamtx.org/docs/references/configuration-file
13. MediaMTX, **Metrics**.
    https://mediamtx.org/docs/features/metrics
14. MediaMTX, **HLS**.
    https://mediamtx.org/docs/usage/read#hls
15. MediaMTX, **WebRTC/WHEP**.
    https://mediamtx.org/docs/usage/read#webrtc
16. FFmpeg, **Protocols documentation**.
    https://ffmpeg.org/ffmpeg-protocols.html
17. FFmpeg, **Codecs documentation**.
    https://ffmpeg.org/ffmpeg-codecs.html
18. Expo, **expo-video**.
    https://docs.expo.dev/versions/latest/sdk/video/
19. Home Assistant Developer Docs, **Ingress and presentation**.
    https://developers.home-assistant.io/docs/apps/presentation/
20. ONVIF, **Network Video Streaming and device interoperability materials**.
    https://www.onvif.org/

---

## 22. Ordem recomendada de execução

A ordem abaixo evita otimizar antes de conseguir diagnosticar.

```text
Semana 1:
  P0.1 Freshness/stale no server
  P0.2 HLS liveness no app
  P0.3 Heartbeat/on-demand HLS
  P0.4 Port mismatch HA

Semana 2:
  P0.5 Presets contínuos
  P1.1 Health dashboard mínimo
  P1.2 Encoder quarantine básico

Semana 3:
  P1.3 Camera/source health
  P1.4 Media token design
  P2.3 Chaos tests básicos

Depois:
  P2.1 ABR/perfis
  P2.2 WHEP/WebRTC
```

A primeira entrega boa não é “mais qualidade visual”. É: **nenhum freeze silencioso**.

---

# Apêndice A: dossiê técnico original preservado

# Dossie tecnico - instabilidade de streams no Toposync

Este documento descreve o caminho completo de uma transmissao de camera ate o
app tvOS/mobile, com foco nos pontos que podem explicar streams que nao
reproduzem, congelam depois de um tempo, perdem autenticacao, falham em PiP ou
nao retomam ao voltar de background.

## Escopo analisado

Repos e estados locais consultados:

- App: `/Users/c/Projects/toposync-app`, `ec6b482`.
- Servidor/origem: `/Users/c/Projects/toposync-2`, `9d0330d`.
- Home Assistant add-on: `/Users/c/Projects/toposync-homeassistant-addon`, `964042c`.

Observacao: o servidor estava com alteracao local nao relacionada em
`frontend/src/ui/styles.css`; este dossie leu o codigo, mas nao alterou esse
repo.

Fontes externas primarias usadas:

- Expo Video: https://docs.expo.dev/versions/latest/sdk/video/
- Apple HLS: https://developer.apple.com/streaming/
- Apple AVPictureInPictureController:
  https://developer.apple.com/documentation/avkit/avpictureinpicturecontroller
- MediaMTX configuration:
  https://mediamtx.org/docs/references/configuration-file
- MediaMTX read/publish usage: https://mediamtx.org/docs/usage/read

## Resumo executivo

O sistema tem tres camadas principais:

1. O servidor recebe frames de cameras/pipelines, arbitra o writer ativo,
   codifica via FFmpeg e publica em paths do MediaMTX.
2. O add-on do Home Assistant expoe a API do Toposync e portas diretas para
   streaming no range `18756-18762`.
3. O app busca transmissoes pela API autenticada, seleciona HLS sem auth,
   valida playlist/segmentos e entrega a URL ao `expo-video`/AVPlayer.

Os primeiros suspeitos de instabilidade, por ordem de probabilidade:

1. **Origem para de emitir frames e o servidor preserva o ultimo frame.** O
   runtime guarda o ultimo frame selecionado/incoming e pode devolve-lo mesmo
   quando nao ha writer elegivel. O player ve uma imagem congelada, nao
   necessariamente um erro.
2. **Pipelines com gate/eventos podem ser intermitentes por desenho.** Motion
   gate com `emit_when_idle=false` e modos de deteccao/evento podem nao emitir
   frames quando nao ha movimento/deteccao.
3. **On-demand pode oscilar.** O writer bridge para publishers apos 3s sem
   `viewer_count`, salvo quando ha prime/hint. Se o MediaMTX contar viewers de
   HLS de forma intermitente durante buffer/background/playlist reload, o
   publisher pode parar no meio da reproducao.
4. **Readiness do app cobre apenas o inicio.** O app valida playlist e os
   ultimos segmentos antes de tocar, mas nao monitora se a playlist continua
   gerando segmentos durante a reproducao.
5. **Porta real pode divergir da porta exposta.** O MediaMTX escolhe porta
   alternativa se a preferida estiver ocupada. No add-on HA, somente o range
   esperado esta exposto; se a HLS mudar, Apple TV pode nao alcancar a URL.
6. **HLS autenticado nao e suportado no app nativo.** O app rejeita outputs HLS
   com `requiresAuth`. A API usa cookies, mas o player nativo nao deve ser
   assumido como compartilhando headers/cookies JS de forma confiavel.
7. **Encoder/hardware pode reiniciar.** O publisher prefere hardware quando
   disponivel; falhas em runtime geram fallback para x264, mas podem causar
   restart/stall.
8. **Captura RTSP da camera pode travar antes do streaming.** O source reacquire
   apos janela de stale, mas ate isso acontecer o pipeline pode parar de emitir
   novos frames e o servidor continua com o ultimo.

## Mapa end-to-end

```text
camera / ONVIF / RTSP
  -> camera.source
       backends: auto, ffmpeg, opencv
       compartilhamento: CameraHub
       saida: Packet + artifact image/raw
  -> operadores opcionais
       fps_reducer, motion_gate, detect, track, segment, gates
       canais com drop_oldest/latest semantics
  -> stream.publish_video
       writer_id = "<pipeline_name>:<node_id>"
       escreve frames no TransmissionRuntimeState
  -> TransmissionRuntimeState
       guarda ultimo frame por writer
       aplica arbitration latest/priority_latest
       preserva ultimo frame selecionado/incoming
  -> StreamWriterBridge
       tick 100ms, settings 1s, viewer count 1s
       on-demand, prime, synthetic no-stream hint
       resize/placeholder
  -> PublisherManager / FFmpeg
       rawvideo_pipe BGR24 -> H.264 RTSP publish
       ou rtsp_pull bypass quando habilitado e elegivel
  -> MediaMTX
       paths por transmission/output
       RTSP, HLS mpegts, WebRTC/WHEP
  -> Home Assistant add-on
       direct proxy API: 18756
       backend ingress: 18757
       RTSP: 18758, HLS: 18759, WebRTC: 18760, API: 18761
  -> app Toposync
       API cookie auth -> prime demand -> resolve URLs -> HLS readiness
       -> expo-video / AVPlayer -> fullscreen / PiP
```

## Backends envolvidos

### 1. Backend de app/API

Codigo principal no app:

- `src/core/networking/api-client.ts`
- `src/core/networking/streaming-api.ts`
- `src/core/auth/session-strategy.ts`
- `src/features/streams/streams-dashboard-context.tsx`
- `src/features/streams/stream-player-card.tsx`
- `src/features/streams/hls-readiness.ts`
- `src/features/streams/streams-dashboard-state.ts`

Responsabilidades:

- Manter a sessao via cookies nativos (`credentials: include`).
- Listar transmissoes por `GET /api/streams/transmissions`.
- Primar demanda por `POST /api/streams/transmissions/{id}/demand/prime`.
- Resolver URLs por `GET /api/streams/transmissions/{id}/urls`.
- Selecionar apenas HLS.
- Preferir HLS sem autenticacao.
- Reescrever host loopback/local quando necessario.
- Esperar readiness HLS antes de entregar a URL ao player.
- Suspender/recriar player em mudancas de tela, instancia, background/foreground
  e PiP.

### 2. Backend Toposync principal

Codigo principal no servidor:

- `src/toposync/app.py`
- `src/toposync/runtime/auth.py`
- `src/toposync/runtime/pipelines/*`
- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`
- `extensions/streaming/src/toposync_ext_streaming/api/models.py`

Responsabilidades:

- Servir API principal e APIs de extensoes.
- Aplicar auth local, ingress ou hybrid.
- Persistir settings/transmissions/pipelines.
- Executar pipelines e operadores.
- Resolver se uma transmission e local ou remota.
- Retornar URLs de streaming e diagnosticos de runtime.

### 3. Backend do Home Assistant add-on

Codigo principal:

- `/Users/c/Projects/toposync-homeassistant-addon/toposync/run_addon.py`
- `/Users/c/Projects/toposync-homeassistant-addon/toposync/config.yaml`

Responsabilidades:

- Rodar `toposync serve` em `18757`.
- Expor acesso direto para apps em `18756`.
- Rodar proxy direto que remove headers de ingress/HA antes de encaminhar.
- Configurar envs de auth hybrid:
  - `TOPOSYNC_AUTH_MODE=home_assistant_hybrid`
  - `TOPOSYNC_AUTH_INGRESS_ROLE=owner`
  - `TOPOSYNC_AUTH_INGRESS_TRUSTED_IPS=127.0.0.1,::1,172.30.32.2,testclient`
  - `TOPOSYNC_AUTH_INGRESS_ENFORCE_TRUSTED=1`
- Configurar portas de streaming:
  - RTSP `18758`
  - HLS `18759`
  - WebRTC/WHEP `18760`
  - MediaMTX API `18761`
  - WebRTC UDP `18762`
- Garantir `expose_to_lan: true` e portas preferidas no settings da extensao.

Risco especifico: se o MediaMTX precisar usar uma porta alternativa porque a
porta preferida esta ocupada, o app pode receber uma URL correta do ponto de
vista do processo, mas incorreta do ponto de vista da rede exposta pelo add-on.

### 4. Backend de processamento remoto

Codigo principal:

- `extensions/streaming/src/toposync_ext_streaming/plugin.py`
- `extensions/streaming/src/toposync_ext_streaming/api/routes.py`
- `src/toposync/runtime/pipelines/distributed/*`

Conceito:

- `Transmission.host_server_id` define onde a transmission e hospedada.
- Pipelines devem ter `processing_server_id` compativel.
- Quando a transmission e remota, o core chama
  `/api/streams/internal/transmissions/{id}/urls` no processing server.
- O servidor remoto retorna URLs; o core ajusta host quando necessario.

Variaveis relevantes:

- `TOPOSYNC_ROLE=processing`
- `TOPOSYNC_PROCESSING_SERVER_ID=<server_id>`
- `TOPOSYNC_STREAMING_SYNC_CORE_URL` ou `TOPOSYNC_CORE_URL`
- `TOPOSYNC_STREAMING_SYNC_BEARER_TOKEN` ou usuario/senha de sync

Riscos especificos:

- Processing server retornando `localhost`, `.local` ou IP nao roteavel para a
  Apple TV.
- Portas do processing server abertas para o core, mas nao para o app.
- `host_server_id` da transmission diferente do `processing_server_id` do
  pipeline.
- HLS port exposta no host remoto diferente da porta usada pelo MediaMTX.

### 5. Backend de streaming MediaMTX

Codigo principal:

- `extensions/streaming/src/toposync_ext_streaming/streaming/engine_manager.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/mediamtx_config.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/mediamtx_api_client.py`

Versao configurada:

- `MEDIAMTX_VERSION = "v1.16.2"`

Configuracao gerada:

- `authMethod: internal`
- API/metrics/pprof restritos a localhost.
- Publish restrito a localhost com credenciais internas por path.
- RTSP ligado, transportes `udp` e `tcp`.
- HLS ligado com `hlsVariant: mpegts`.
- WebRTC ligado quando configurado.
- `all_others: {}` alem dos paths explicitos.

URLs esperadas:

- RTSP: `rtsp://<host>:<rtsp_port>/<path>`
- HLS: `http://<host>:<hls_port>/<path>/index.m3u8`
- WebRTC/WHEP: `http://<host>:<webrtc_port>/<path>/whep`

### 6. Backend de publicacao FFmpeg

Codigo principal:

- `extensions/streaming/src/toposync_ext_streaming/streaming/publisher_manager.py`
- `extensions/streaming/src/toposync_ext_streaming/streaming/ffmpeg_binary.py`

Versao empacotada:

- `FFMPEG_VERSION = "n7.1.1"`

Modos de entrada:

- `rawvideo_pipe` default:
  - o writer bridge escreve frames BGR24 no stdin do FFmpeg.
  - argumentos incluem `-f rawvideo -pix_fmt bgr24 -s WxH -r FPS -i pipe:0`.
- `rtsp_pull` bypass:
  - FFmpeg puxa diretamente RTSP da camera/ingest.
  - exige bypass habilitado por `TOPOSYNC_STREAMING_ENABLE_BYPASS=1`.
  - so e usado em shape simples e quando ha um publisher por transmission.

Saida:

- RTSP publish para MediaMTX.
- H.264 com `libx264` ou encoder hardware quando preferido/disponivel.
- Pixel format `yuv420p` para compatibilidade.
- GOP aproximado de 1 segundo (`-g round(fps)`, `-keyint_min round(fps)`).
- `-tune zerolatency` por padrao.

Riscos especificos:

- Encoder hardware passa no probe, mas falha no stream real.
- Restart limit de publisher pode ser atingido.
- `frames_sent` pode continuar subindo com frame repetido, mascarando origem
  travada.
- `last_error` e `stderr_tail` do publisher sao essenciais no momento do stall.

### 7. Backend de captura de camera

Codigo principal:

- `extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py`
- `extensions/cameras/src/toposync_ext_cameras/processing/camera_hub.py`

Operador `camera.source`:

- Configuracao: `camera_id`, `channel_id`, `rtsp_url`, `username`,
  `password`, `backend`, `fps`, `poll_interval_ms`.
- Backends validos: `auto`, `opencv`, `ffmpeg`.
- Usa `CameraHub` global para compartilhar conexoes RTSP entre pipelines.
- So emite packet quando existe frame novo (`frame_ts > last_ts`).
- Se nao ha novo frame, retorna `None`.

Backends:

- `auto`:
  - para RTSP, prefere FFmpeg.
  - para nao RTSP, prefere OpenCV.
- `ffmpeg`:
  - usa subprocess `ffmpeg`.
  - usa RTSP TCP.
  - decodifica para MJPEG em `image2pipe`.
  - exige FFmpeg no PATH e decoder JPEG via OpenCV/Pillow.
- `opencv`:
  - usa `cv2.VideoCapture`.
  - timeouts default `TOPOSYNC_RTSP_OPEN_TIMEOUT_MS=8000` e
    `TOPOSYNC_RTSP_READ_TIMEOUT_MS=8000`.
  - buffer size 1.
  - reabre se falhas acumulam ou se nao ha frame por ~10s.
  - pode cair de `/stream1` para `/stream2`.

Reacquire/failover no `camera.source`:

- `TOPOSYNC_CAMERA_SOURCE_REACQUIRE_AFTER_S`, default 15s.
- `TOPOSYNC_CAMERA_SOURCE_REACQUIRE_COOLDOWN_S`, default 5s.
- `TOPOSYNC_CAMERA_SOURCE_START_BACKOFF_S`, default 10s.
- `TOPOSYNC_CAMERA_SOURCE_INGEST_BACKOFF_S`, default 90s.
- `TOPOSYNC_CAMERA_SOURCE_BACKEND_FAILOVER_S`, default 180s.
- `TOPOSYNC_CAMERA_SOURCE_BACKEND_FAILOVER_COOLDOWN_S`, default 120s.

Risco central: durante a janela de stale/reacquire, o pipeline para de emitir
frames novos; o streaming runtime ainda pode preservar/publicar o ultimo frame.

### 8. Backend de execucao de pipelines

Codigo principal:

- `src/toposync/runtime/pipelines/runtime.py`
- `src/toposync/runtime/pipelines/execution.py`
- `src/toposync/runtime/pipelines/execution_scheduler.py`
- `extensions/streaming/src/toposync_ext_streaming/pipelines/operators.py`

Comportamento:

- Canais bounded aplicam politicas como `drop_oldest`, `drop_newest`,
  `latest_only` e `keyed_latest_only`.
- Pacotes de lifecycle `OPEN`/`CLOSE` sao preservados com prioridade estrutural.
- Operadores CPU/blocking rodam em pools limitados.
- `stream.publish_video` busca um artifact de imagem e chama
  `TransmissionRuntimeState.update_writer_frame`.
- Se o artifact esperado nao existe, o sink nao atualiza frame.
- Em `Lifecycle.CLOSE`, o writer fica fechado/ineligivel.

Riscos:

- Operador pesado de visao satura CPU e reduz a chegada de frames ao sink.
- Canal com drop descarta updates sob pressao.
- Artifact name incorreto deixa o sink sem frames.
- Pipelines event-only enviam eventos, mas nao frames continuos.

### 9. Backend nativo de video no app

Codigo/config:

- `app.json`: plugin `expo-video` com:
  - `supportsPictureInPicture: true`
  - `supportsBackgroundPlayback: false`
- `package.json`: `expo-video ~55.0.10`, `react-native-tvos`.
- `src/features/streams/stream-player-card.tsx`.

Uso:

- `useVideoPlayer(null)`.
- `VideoView` com `contentFit="contain"`.
- `allowsPictureInPicture` conforme suporte runtime.
- Source entregue como `{ uri, contentType: 'hls' }`.
- O player chama `replaceAsync(...)` e `play()`.

Limites:

- O app usa HLS para reproducao nativa.
- HLS autenticado e rejeitado no app antes de chegar ao player.
- `supportsBackgroundPlayback` esta falso; background normal suspende streams.
- Apenas PiP ativo deve permanecer quando o app vai a background.

## Modelo de dominio de streaming

No app, `TransmissionProtocol = 'hls' | 'rtsp' | 'webrtc'`.

Uma `Transmission` tem:

- `id`
- `name`
- `path`
- `enabled`
- `hostServerId`
- `outputs`
- `cameraControls`

Um output resolvido tem:

- `outputId`
- `protocol`
- `resolvedEnginePath`
- `url`
- `requiresAuth`
- `authenticationUsername`

No servidor, `TransmissionOutput` inclui:

- `protocol`
- `enabled`
- `resolution`
- `fps_limit`
- `bitrate_kbps`
- `latency_profile`
- `authentication`

Path de engine:

- Com um unico output, ele pode compartilhar o path da transmission.
- Com multiplos outputs, so compartilha path quando encoding/auth sao iguais.
- Caso contrario usa variante como `<transmission.path>-<output.id>`.

## API de streaming

Endpoints usados pelo app:

- `GET /api/streams/transmissions`
- `GET /api/streams/transmissions/{id}/urls`
- `POST /api/streams/transmissions/{id}/demand/prime`
- `GET /api/streams/engine/status`
- Endpoints PTZ por transmission quando `camera_controls.enabled=true`.

Endpoints de diagnostico no servidor:

- `GET /api/streams/runtime/outputs`
- `GET /api/streams/runtime/diagnostics`
- `GET /api/streams/transmissions/{id}/demand`
- `GET /api/streams/engine/status`

Quando resolver URLs:

- O route local prima demanda best-effort.
- Se engine esta rodando, usa portas ativas.
- Se nao esta rodando, usa portas preferidas.
- Para host remoto, chama endpoint interno no processing server.

## Formatos e codificacao

Frames internos:

- Numpy array BGR `uint8`.
- Artifact principal de imagem/raw no pipeline.
- `stream.publish_video` escreve o frame no runtime state.
- Writer bridge aplica resize `contain` e padding preto quando necessario.

FFmpeg input:

- `rawvideo_pipe`: `bgr24`, `WxH`, `FPS`, stdin.
- `rtsp_pull`: RTSP TCP direto da origem/ingest.

FFmpeg output:

- H.264.
- `yuv420p`.
- GOP por volta de 1 segundo.
- RTSP publish para MediaMTX.

MediaMTX outputs:

- RTSP: bom para VLC/ffplay/diagnostico, preferir TCP no cliente.
- HLS: usado pelo app nativo, variante `mpegts`, nao LL-HLS.
- WebRTC/WHEP: usado pelo dashboard web quando aplicavel.

Implicacoes de HLS `mpegts`:

- Maior latencia que LL-HLS.
- Stalls podem demorar para virar erro no AVPlayer.
- Se segmentos deixam de aparecer, o player pode ficar em buffering/freeze sem
  um erro claro imediatamente.

## Autenticacao e pairing

### App

O app usa `CookieSessionStrategy` por padrao:

- As requisicoes de API fazem `credentials: include`.
- A estrategia nao injeta header de auth.
- O token strategy existe, mas nao e o caminho principal.

Consequencia:

- Perda de cookie quebra API: status, listagem, prime e URL resolve.
- HLS aberto continua tocando mesmo se a API perde sessao, desde que o player ja
  tenha URL e o stream permaneca sem auth.
- HLS autenticado e bloqueado pelo app nativo, porque nao e seguro assumir que o
  player nativo compartilha cookies/headers JS.

### Servidor

Cookies:

- `toposync_at`
- `toposync_rt`

Modos:

- `ingress`
- `home_assistant_hybrid`
- auth local/enforced

No modo hybrid:

- Requisicoes via ingress confiavel usam principal do Home Assistant.
- Acesso direto usa usuario/senha local ou pairing.
- Refresh cookie pode rotacionar e aplicar novos cookies na resposta.

### Home Assistant add-on

O add-on roda em hybrid:

- Sidebar/ingress autentica pelo HA.
- Acesso direto em `18756` usa auth local do Toposync.
- O proxy direto remove headers de ingress para evitar spoofing.
- Cada request do proxy usa novo `httpx.AsyncClient`, entao nao deve compartilhar
  cookies entre clientes diretos.

### Onde obter pairing code

O pairing code nao aparece magicamente no Apple TV. Ele precisa ser iniciado por
uma sessao ja autenticada com permissao de pairing/access.

Possiveis caminhos:

- Pela UI web autenticada do Toposync/HA, na area de usuarios/acesso, iniciar
  pairing do usuario local/dispositivo.
- Pela API, estando autenticado no navegador/sessao com permissao:

```js
fetch('/api/auth/pair/start', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ device_label: 'Apple TV' }),
})
  .then(async (response) => {
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  })
  .then(console.log);
```

Ha tambem endpoint de acesso por usuario:

- `POST /api/access/users/{user_id}/pair/start`

Depois o app conclui com:

- `POST /api/auth/pair/complete`

## Comportamento inicial de playback no app

Fluxo nominal:

1. App esta conectado a uma instancia.
2. Tela de streams fica ativa.
3. React Query carrega transmissoes.
4. Para cada card visivel, `prepareTransmissionPlayback(id)` roda.
5. App chama `demand/prime` com retry.
6. App chama `transmissions/{id}/urls` com retry.
7. App exige engine running.
8. App seleciona output HLS.
9. App rejeita output HLS autenticado.
10. App normaliza URL:
    - resolve relative URL contra base da instancia.
    - troca loopback por IP/host LAN conhecido.
    - troca `.local` por `lastKnownIp` quando disponivel.
11. App roda HLS readiness:
    - baixa playlist master.
    - se houver variant, baixa media playlist.
    - testa ultimos segmentos.
    - testa `EXT-X-MAP` e `EXT-X-KEY` quando aparecem.
    - usa Range `bytes=0-1` para segmentos.
12. App marca playback `ready`.
13. `stream-player-card` chama `player.replaceAsync({ uri, contentType: 'hls' })`.
14. App chama `player.play()`.

Timeouts/retries relevantes:

- API transmissions/urls/status: em geral 2.5s.
- Prime demand: 2.2s.
- HLS readiness: ate 20s.
- Player preparation watchdog: 25s.
- Buffer watchdog: 10.5s.
- Recovery por card: ate 3 tentativas.

## Comportamento em regime

Enquanto tudo esta saudavel:

- MediaMTX recebe readers/viewers no path.
- `MediaMtxApiClient` le `/v3/paths/list`.
- Writer bridge atualiza `viewer_count` por output.
- Se `viewer_count > 0`, publisher continua vivo.
- Runtime state seleciona writer ativo.
- Writer bridge envia frame selecionado para o publisher no FPS configurado.
- FFmpeg envia RTSP publish para MediaMTX.
- MediaMTX disponibiliza HLS segments.
- AVPlayer consome playlist/segments.

O app nao revalida continuamente a playlist depois do startup. Ele depende dos
eventos nativos (`statusChange`, errors, buffering) e dos watchdogs locais.

## Comportamento final/suspensao

O publisher para quando:

- transmission/output e desabilitado.
- engine e desabilitado.
- no outputs desejados.
- on-demand esta ativo e `viewer_count <= 0` por pelo menos 3s, sem prime/hint.
- config muda e engine/publisher reinicia.

O app suspende/recria quando:

- tela de streams deixa de ficar ativa.
- instancia ativa muda.
- conexao muda.
- app vai a background sem PiP ativo.
- foreground retorna e incrementa revisao de lifecycle.

PiP:

- O app tenta manter somente o stream em PiP enquanto background.
- Se outro PiP comeca, o app para o anterior.
- `supportsPictureInPicture` esta true no config plugin.
- `supportsBackgroundPlayback` esta false; isso torna background normal um caso
  de suspensao, nao de playback continuo.

## Apple TV, PiP e limitacoes praticas

Fatos do app:

- Build tvOS usa `react-native-tvos`.
- `expo-video` e o player nativo usado.
- O app configura PiP no plugin, mas nao background playback.
- Em tvOS, foco remoto e fullscreen/PiP podem alterar AppState/atividade da tela.

Fatos externos:

- Apple recomenda HLS como tecnologia de streaming para seus dispositivos.
- `AVPictureInPictureController` e a API nativa de PiP no stack AVKit.
- Expo documenta `allowsPictureInPicture`, `isPictureInPictureSupported`,
  callbacks de PiP e `contentType: 'hls'`.

Hipotese especifica:

- Se uma transicao de foco/fullscreen/PiP dispara estado equivalente a inativo
  ou background, o app pode suspender streams nao-PiP; ao voltar, ele depende da
  revisao de lifecycle e do recovery para recriar o player.

## Pontos de instabilidade por camada

### App

Sinais:

- `prepareTransmissionPlayback` cai em `unsupported` por HLS auth.
- URL normalizada aponta para host/porta nao alcancavel.
- `waitForHlsPlaybackReadiness` falha em playlist/segmento.
- `player.replaceAsync` ou `player.play` falha.
- `statusChange` entra em error.
- buffer watchdog dispara.
- recovery maximo e atingido.

Riscos:

- A API pode estar autenticada, mas o player HLS nao consegue usar cookies.
- `credentials: include` depende do cookie jar nativo.
- Sem monitoramento continuo de HLS freshness, uma playlist congelada pode virar
  freeze silencioso.

### API/Auth

Sinais:

- `/api/auth/status` retorna unauthenticated para o app.
- Requests de streams retornam 401/403.
- Refresh cookie nao e enviado/rotacionado.
- Direct access e ingress usam identidades diferentes.

Riscos:

- App apontado para instancia remota/direta diferente da sessao autenticada.
- Cookies antigos de outra base/host confundem status.
- Login local funciona, mas pairing nao foi iniciado pela UI/API autenticada.

### Home Assistant add-on

Sinais:

- API em `18756` responde, mas HLS em `18759` nao.
- Engine status mostra porta HLS diferente de `18759`.
- `expose_to_lan` falso ou nao aplicado.
- Logs indicam porta em uso/stale process.

Riscos:

- Mapeamento de porta do add-on nao acompanha porta dinamica escolhida pelo
  MediaMTX.
- Public host/header diferente da rota alcancavel pela Apple TV.

### MediaMTX

Sinais:

- Path nao fica ready.
- Log contem `no stream is available on path`.
- `/v3/paths/list` mostra readers oscilando.
- HLS playlist responde, mas segmentos novos param de aparecer.

Riscos:

- On-demand depende de viewer count; HLS clients podem nao manter contagem da
  forma esperada durante transicoes.
- Path auth pode bloquear player nativo.
- Restart do engine invalida segmentos antigos.

### FFmpeg publisher

Sinais:

- `publisher.running=false`.
- `restart_count` cresce.
- `last_error` preenchido.
- `active_codec` muda de hardware para `libx264`.
- `stderr_tail` contem erro de encoder, RTSP publish ou broken pipe.
- `frames_sent` para de crescer.

Riscos:

- Falha de encoder hardware.
- RTSP publish para MediaMTX cai/reconecta.
- Restart limit.
- CPU insuficiente para codificar multiplos outputs.

### Runtime state / arbitration

Sinais:

- `active_writer=null`.
- Writers com `lifecycle_state=close`.
- `last_frame_monotonic` velho.
- `frame_ts` velho.
- `has_frame=true`, mas sem writer ativo.

Risco principal:

- `get_selected_writer_frame` preserva ultimo frame selecionado/incoming quando
  nao ha writer elegivel. Isso e util para nao mostrar vazio, mas e tambem a
  assinatura de freeze silencioso.

### Pipeline/camera

Sinais:

- `camera.source` logs: stalled, reacquiring, backend failover.
- Capture metrics: `last_frame_ts` velho, `fps=0`, `restarts` crescendo.
- Canais de pipeline com drops/timeouts.
- Motion gate sem movimento e `emit_when_idle=false`.
- Detection/tracking em modo event-only.
- `stream.publish_video` sem artifact esperado.

Riscos:

- Camera RTSP instavel.
- FFmpeg/OpenCV capture travando.
- Operador de visao lento bloqueando fluxo.
- Gate/evento gerando intencionalmente stream intermitente.

## Hipoteses iniciais ranqueadas

### H1 - Freeze por ultimo frame preservado

Confianca: alta.

Evidencia:

- `TransmissionRuntimeState` guarda `_last_selected_frame_by_transmission` e
  `_last_incoming_frame_by_transmission`.
- Quando nao ha `selected_writer_id`, retorna fallback com `writer_id=None`.
- `StreamWriterBridge` publica esse frame se `selected.frame` nao e `None`.

Como confirmar:

- Durante freeze, chamar `GET /api/streams/runtime/diagnostics`.
- Verificar se `active_writer` esta ausente/null.
- Verificar se `last_frame_monotonic`/`frame_ts` nao mudam.
- Verificar se `publisher.frames_sent` ainda sobe.

Se isso ocorrer, o problema esta antes ou no runtime state, nao no AVPlayer.

### H2 - Pipeline de "frente" usa gate/event-only

Confianca: alta se a transmission foi criada com motion/detection/tracking.

Evidencia:

- `motion_gate_stream` pode nao emitir quando nao ha motion.
- `MotionGateRuntime` retorna `[]` se `gate_open=false` e
  `emit_when_idle=false`.
- Alguns operadores YOLO/eventos podem emitir apenas em eventos ou filtrar
  frames sem deteccao.

Como confirmar:

- Identificar pipeline ligado a transmission da frente.
- Conferir preset/config.
- Verificar logs/metrics de operadores.
- Comparar com `simple_stream` continuo da mesma camera.

### H3 - On-demand para publisher durante playback

Confianca: media/alta.

Evidencia:

- Writer bridge para publisher se `viewer_count <= 0` por 3s sem prime/hint.
- HLS readiness ocorre antes de player tocar; depois disso o app nao mantem
  prime ativo explicitamente.
- Viewer count e carregado via API do MediaMTX uma vez por segundo.

Como confirmar:

- Durante freeze, chamar:
  - `/api/streams/transmissions/{id}/demand`
  - `/api/streams/runtime/outputs`
  - `/api/streams/runtime/diagnostics`
- Procurar viewer count caindo a 0 antes do publisher parar.

### H4 - HLS playlist/segmentos deixam de atualizar

Confianca: media.

Evidencia:

- HLS `mpegts` e normal, nao low-latency.
- App valida readiness so na preparacao.
- Congelamento sem erro e comportamento plausivel se segmento novo nao chega.

Como confirmar:

- Amostrar playlist a cada 2s durante o freeze.
- Verificar se os ultimos `.ts` mudam.
- Testar Range nos segmentos novos.
- Comparar timestamp de `EXTINF`/ordem de segmentos com `publisher.frames_sent`.

### H5 - Porta/host errado em HA ou processing remoto

Confianca: media.

Evidencia:

- Add-on expoe range fixo.
- Engine manager pode escolher porta livre alternativa.
- Rotas remotas podem reescrever host, mas nao resolvem firewall/NAT.
- Usuario informou estar apontando para instancia em outro lugar.

Como confirmar:

- Conferir `engine.status.ports.hls`.
- Conferir URL retornada para a transmission.
- Rodar `curl` da mesma rede da Apple TV para playlist e segmento.
- Validar firewall/port forwarding no host remoto.

### H6 - Auth/cookie perdido quebra prepare/retry

Confianca: media.

Evidencia:

- App depende de cookies.
- Direct/ingress/hybrid podem ter sessoes distintas.
- Se foreground/retry tenta API e perde cookie, nao consegue re-preparar stream.

Como confirmar:

- Quando falhar, chamar `/api/auth/status` do mesmo base URL do app.
- Conferir 401/403 em `/api/streams/...`.
- Conferir se HLS output e aberto ou autenticado.

### H7 - Encoder/CPU/restart de publisher

Confianca: media.

Evidencia:

- Publisher prefere hardware.
- Existe fallback para `libx264`, mas com restart.
- Multiples streams/quad aumentam custo.

Como confirmar:

- `runtime/outputs`: `restart_count`, `last_error`, `active_codec`,
  `hardware_accelerated`, `frames_sent`.
- FFmpeg log path do publisher.
- CPU/memoria do add-on/processing host.

### H8 - Captura RTSP da camera trava

Confianca: media.

Evidencia:

- Source tem reacquire after 15s.
- Backends FFmpeg/OpenCV podem reiniciar.
- Durante a janela, nenhum packet novo chega.

Como confirmar:

- Metrics do camera source: `fps`, `last_frame_ts`, `restarts`,
  `capture_backend`, `last_error`.
- Logs de `camera source stalled`.
- Testar RTSP direto da camera com FFmpeg/VLC.

## Primeiros sinais a coletar

| Sinal                                 | Onde ver                         | Interpretacao                                   |
| ------------------------------------- | -------------------------------- | ----------------------------------------------- |
| `active_writer=null` e frame velho    | `/runtime/diagnostics`           | origem/pipeline parou; fallback de ultimo frame |
| `viewer_count=0` antes do stall       | `/demand`, `/runtime/outputs`    | on-demand pode ter parado publisher             |
| `publisher.running=false`             | `/runtime/outputs`               | FFmpeg parado ou on-demand idle                 |
| `restart_count` cresce                | `/runtime/outputs`               | instabilidade FFmpeg/MediaMTX                   |
| `frames_sent` flat                    | `/runtime/outputs`               | writer bridge/publisher sem frames novos        |
| `frames_sent` cresce mas imagem igual | `/runtime/outputs` + diagnostico | ultimo frame sendo republicado                  |
| playlist nao muda                     | curl no HLS                      | MediaMTX/FFmpeg parou de gerar segmentos        |
| segmento novo da 404/timeout          | curl Range no segmento           | path/port/restart/HLS quebrado                  |
| `requires_auth=true`                  | `/transmissions/{id}/urls`       | app nativo rejeita HLS                          |
| porta HLS != 18759 no add-on          | `/engine/status`                 | porta nao exposta para Apple TV                 |
| `last_frame_ts` velho na captura      | pipeline/camera metrics          | camera/source travado                           |
| drops/timeouts em canais              | pipeline snapshot/stats          | backpressure/CPU                                |

## Checklist de diagnostico no momento do freeze

Use a mesma base URL que o app usa. Exemplo:

```bash
BASE="http://<host>:18756"
TID="<transmission_id>"
```

Auth/status:

```bash
curl -i "$BASE/api/auth/status"
```

Transmissoes e URL:

```bash
curl -s "$BASE/api/streams/transmissions" | jq .
curl -s "$BASE/api/streams/transmissions/$TID/urls" | jq .
```

Demand e runtime:

```bash
curl -s "$BASE/api/streams/transmissions/$TID/demand" | jq .
curl -s "$BASE/api/streams/runtime/outputs" | jq .
curl -s "$BASE/api/streams/runtime/diagnostics" | jq .
curl -s "$BASE/api/streams/engine/status" | jq .
```

Prime manual:

```bash
curl -i -X POST "$BASE/api/streams/transmissions/$TID/demand/prime"
```

HLS:

```bash
HLS_URL="<url_do_output_hls>"
curl -i "$HLS_URL"
```

Depois pegue o ultimo segmento da playlist e teste:

```bash
SEGMENT_URL="<url_do_segmento_ts>"
curl -i -H 'Range: bytes=0-1' "$SEGMENT_URL"
```

Amostragem simples de playlist:

```bash
for i in 1 2 3 4 5; do
  date
  curl -fsS "$HLS_URL" | tail -n 12
  sleep 2
done
```

Logs a anexar:

- MediaMTX log em `runtime/streaming/logs/mediamtx-*.log`.
- FFmpeg publisher log indicado por `publisher.log_path`.
- Logs do Toposync contendo:
  - `camera source stalled`
  - `Streaming engine`
  - `no stream is available`
  - `Disabled FFmpeg encoder`
  - erros de pipeline/operator.

Campos mais importantes em `/runtime/diagnostics`:

- `engine.running`
- `engine.ports`
- `engine.warnings`
- `publisher.outputs[*].running`
- `publisher.outputs[*].frames_sent`
- `publisher.outputs[*].restart_count`
- `publisher.outputs[*].last_frame_at_unix`
- `publisher.outputs[*].last_error`
- `publisher.outputs[*].active_codec`
- `publisher.outputs[*].hardware_accelerated`
- `publisher.outputs[*].stderr_tail`
- `runtime.transmissions[tid].active_writer`
- `runtime.transmissions[tid].writers[*].lifecycle_state`
- `runtime.transmissions[tid].writers[*].frame_ts`
- `runtime.transmissions[tid].writers[*].last_frame_monotonic`
- `runtime.transmissions[tid].outputs[*].viewer_count`

## Experimentos recomendados

### Experimento A - isolar app vs servidor

1. Abra o HLS URL em `curl`/VLC enquanto o Apple TV toca.
2. Se os dois congelam juntos, foco no servidor/pipeline.
3. Se so Apple TV congela, foco em app/AVPlayer/AppState/PiP.

### Experimento B - comparar HLS e RTSP

1. Use RTSP direto com VLC/ffplay:

```bash
ffplay -rtsp_transport tcp "rtsp://<host>:18758/<path>"
```

2. Se RTSP segue vivo e HLS congela, foco em MediaMTX HLS/playlist/viewers.
3. Se ambos congelam, foco em FFmpeg publisher/pipeline/camera.

### Experimento C - desabilitar complexidade de pipeline

1. Criar uma transmission `simple_stream` continua da camera da frente.
2. Mesmo output HLS/resolucao/fps.
3. Comparar com pipeline atual.

Se `simple_stream` estabiliza, causa provavel e gate/evento/visao/backpressure.

### Experimento D - fixar backend de captura

1. Testar `source_backend=ffmpeg`.
2. Testar `source_backend=opencv`.
3. Comparar logs de `last_frame_ts`, `fps`, `restarts`.

### Experimento E - testar on-demand

1. Primar manualmente antes e durante playback.
2. Monitorar `viewer_count`.
3. Temporariamente aumentar stop debounce ou manter prime renovado para teste.

Se o freeze some, on-demand/viewer count e suspeito forte.

### Experimento F - forcar CPU encoder

1. Se logs mostram encoder hardware/fallback, testar sem preferencia hardware
   ou em ambiente onde `libx264` seja usado.
2. Comparar `restart_count` e `stderr_tail`.

## Melhorias provaveis apos confirmacao

Estas nao sao correcoes ainda; sao linhas de acao apos coleta:

1. Expor no diagnostico uma metrica `selected_frame_age_s` e
   `selected_writer_id`.
2. Quando nao ha writer elegivel, permitir modo configuravel:
   - publicar placeholder apos N segundos, ou
   - parar publisher, ou
   - manter ultimo frame mas marcar stale no diagnostico.
3. App monitorar HLS freshness periodicamente enquanto toca:
   - playlist sequence mudando.
   - tail segment respondendo.
   - recovery se congelar sem `status=error`.
4. Renovar demand prime enquanto player estiver ativo, ou ajustar on-demand para
   HLS com heuristica mais robusta.
5. No add-on, alertar/erro se porta ativa do MediaMTX divergir da porta exposta.
6. Mostrar no app erro especifico para auth/API perdida vs HLS perdida.
7. Expor no UI/log do servidor qual pipeline/writer alimenta cada transmission.
8. Para streaming continuo, evitar presets event-only ou documentar claramente
   que motion/detection gates podem congelar quando nao ha eventos.

## Perguntas abertas para a analise aprofundada

1. A transmission "da frente" usa qual preset/pipeline exatamente?
2. O output HLS dessa transmission e sem auth?
3. O app aponta para `18756` do add-on, para ingress, ou para processing remoto?
4. A URL HLS resolvida tem host/porta alcancavel pela Apple TV?
5. Durante freeze, `viewer_count` fica >0?
6. Durante freeze, `active_writer` existe?
7. Durante freeze, `frame_ts` muda?
8. Durante freeze, a playlist HLS muda?
9. O problema ocorre mais em quad/multiplos streams do que em single?
10. O problema ocorre depois de background/foreground, fullscreen ou PiP?
11. O `active_codec` e hardware ou `libx264`?
12. A camera RTSP direta fica estavel fora do Toposync?

## Referencias locais principais

App:

- `app.json`
- `package.json`
- `tv.temp.sh`
- `src/core/domain/transmission.ts`
- `src/core/networking/api-client.ts`
- `src/core/networking/streaming-api.ts`
- `src/core/auth/auth-api.ts`
- `src/core/auth/session-strategy.ts`
- `src/core/networking/probe.ts`
- `src/features/streams/streams-dashboard-context.tsx`
- `src/features/streams/stream-player-card.tsx`
- `src/features/streams/hls-readiness.ts`
- `src/features/streams/streams-dashboard-state.ts`

Servidor:

- `/Users/c/Projects/toposync-2/extensions/streaming/README.md`
- `/Users/c/Projects/toposync-2/extensions/streaming/src/toposync_ext_streaming/plugin.py`
- `/Users/c/Projects/toposync-2/extensions/streaming/src/toposync_ext_streaming/api/models.py`
- `/Users/c/Projects/toposync-2/extensions/streaming/src/toposync_ext_streaming/api/routes.py`
- `/Users/c/Projects/toposync-2/extensions/streaming/src/toposync_ext_streaming/streaming/runtime_state.py`
- `/Users/c/Projects/toposync-2/extensions/streaming/src/toposync_ext_streaming/streaming/writer_bridge.py`
- `/Users/c/Projects/toposync-2/extensions/streaming/src/toposync_ext_streaming/streaming/publisher_manager.py`
- `/Users/c/Projects/toposync-2/extensions/streaming/src/toposync_ext_streaming/streaming/engine_manager.py`
- `/Users/c/Projects/toposync-2/extensions/streaming/src/toposync_ext_streaming/streaming/mediamtx_config.py`
- `/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py`
- `/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py`
- `/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/camera_hub.py`
- `/Users/c/Projects/toposync-2/src/toposync/runtime/auth.py`
- `/Users/c/Projects/toposync-2/src/toposync/runtime/pipelines/runtime.py`
- `/Users/c/Projects/toposync-2/src/toposync/runtime/pipelines/execution.py`

Home Assistant add-on:

- `/Users/c/Projects/toposync-homeassistant-addon/toposync/run_addon.py`
- `/Users/c/Projects/toposync-homeassistant-addon/toposync/config.yaml`
- `/Users/c/Projects/toposync-homeassistant-addon/README.md`
