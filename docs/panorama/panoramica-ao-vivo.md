# Panorâmica ao vivo

O modo **Panorâmica ao vivo**, no seletor de renderização, combina uma referência panorâmica salva com a fonte de vídeo existente. A disponibilidade de uma imagem panorâmica não certifica alinhamento ou apontamento: esses estados precisam de observação qualificada.

## Interação

Selecione a câmera panorâmica no controle superior, no mesmo lugar em que outras visualizações mostram a composição. A lista usa o modal e os estados visuais da plataforma, mostra somente câmeras com geometria compatível e prefere o panorama ativo; um candidato só é usado quando não existe ativo elegível. Não há um segundo seletor dentro da imagem.

A área útil salva é usada automaticamente quando existe; sem ela, aparece a imagem integral. Arraste, roda do mouse, pinça e botões de ampliação navegam na imagem. Eles não comandam pan, tilt ou zoom óptico. O botão **Parar** só aparece enquanto existe uma operação física em andamento.

Quando um frame atual é localizado, o vídeo em cores é projetado no panorama cinza. Um clique em cobertura válida solicita centralização. O marcador mostra o destino desejado; só a medição visual independente do controlador confirma o centro. Durante movimento ou perda do registro, o mesmo player continua na prévia flutuante. **Parar** cancela o restante da operação própria e o destino pendente; o reconhecimento HTTP não é prova de parada física.

Uma operação física pode estar ativa, com apenas uma intenção pendente substituível. O último clique substitui os anteriores. A intenção vence em dez segundos enquanto espera para começar; sessões sem contato por quinze segundos são encerradas cooperativamente. A operação reutiliza a posse e a confirmação de parada do controlador. Uma parada incerta suspende novos comandos daquela sessão. Cada alvo admite no máximo 16 comandos, incluindo as sondagens necessárias para observar a resposta dos eixos; esse teto não renova o orçamento cumulativo do ensaio e pode produzir uma recusa explícita se o domínio não for alcançável dentro dele.

O painel de teleobjetiva só existe quando há uma fonte habilitada e distinta, com papel `zoom`, no mesmo dispositivo. Ele é independente da geometria principal, pode ser movido, redimensionado e minimizado. Cabeçalho, espaçamento, foco e botões seguem os controles da plataforma. Não controla Z.

## Componentes e contratos

| Responsabilidade | Implementação reutilizada ou extensão mínima |
|---|---|
| Navegação | `frontend/src/ui/NavigableViewport.tsx`, já usado por `CameraPanoramaMappingModal.tsx`; matemática `viewportNavigation.ts` também consumida por `Viewport2D.tsx`, exposto como `Viewport2DReplica` na calibração de planta |
| Player e transportes | `frontend/src/ui/streams/StreamsDashboard.tsx` e `host.ui.LiveViewPlayer`; `usePresentedFrame.ts` observa o decodificador existente |
| Identidade óptica | Playback expõe `optical_source_resolution` somente para publicação local cujo grafo gerado permanece inalterado e vinculado à fonte original; `content_rect` conserva o letterbox existente |
| Geometria | `processing/panorama_mapping.py`, lente e rotações imutáveis do artefato; `live/panoramaVideo.ts` faz amostragem inversa esférica WebGL, incluindo distorção Brown/racional, máscara e emenda |
| Localização | `PanoramaCalibrationService.reference_localizer` compartilha `PanoramaLocalizer` e fotografias originais; não usa o fundo estilizado como referência |
| Apontamento | `live_panorama.py` coordena `VisualNavigator`, `_Scan` e `PanoramaCamera`, incluindo leases, pulsos limitados, observação e Stop |

O contrato `LiveViewFrame` inclui imagem renderizável, dimensões decodificadas, época, sequência e tempo de mídia. `timingBasis=browser_presented_frame` é observação local do navegador, não horário de exposição no sensor. O estimador recebe amostra de até 960 pixels, em cadência separada dos frames da textura. Uma comparação conservadora entre frames pode invalidar a pose, mas não estabelecer alinhamento.

Os adaptadores HTMLVideoElement usam `requestVideoFrameCallback` com fallback por `requestAnimationFrame`. JSMpeg continua disponível para reprodução pelo player; esta versão não qualifica projeção a partir de seu canvas. Publicações remotas ou com grafo editado não recebem uma associação óptica presumida. Falta de WebGL, geometria da transmissão, permissão, correspondência visual ou fonte utilizável mantém a prévia e explica a limitação.

Imagem integral, máscara, lente/modelo, revisão e recorte pertencem ao mesmo artefato. Alteração do ponteiro, identidade ou modelo invalida a sessão. Abrir o modo não promove candidato, não cria presets, não inicia aquisição e não retorna automaticamente ao enquadramento anterior.

## Verificação e limite de aceite

```sh
.venv/bin/python -m pytest tests/test_camera_live_panorama.py tests/test_camera_panorama_navigation.py tests/test_camera_panorama_localization.py tests/test_camera_panorama_api.py -q
node --test extensions/cameras/ui/tests/panoramaVideo.test.cjs
npx playwright test --config playwright.live-panorama.config.js
npx playwright test --config playwright.viewport.config.js
```

A fixture de navegador não acessa câmeras: usa o viewport, renderer e adaptador de frames reais com vídeo sintético e API de controle simulada. Testa navegação sem comandos, continuidade do mesmo decodificador, retarget, falha, Stop e reabertura. O replay opcional de fotografia requer manifesto local em `TOPOSYNC_PHOTOGRAPH_FIXTURE`, nunca versiona as fotos e tem aviso visível.

Em 19/09/2026, o núcleo foi validado na Frente Reolink sem nova varredura panorâmica. Um clique distante foi centralizado com três comandos e erro visual final de 1,58 pixel na imagem de análise de 960 pixels. Em outro ensaio, a sequência 1 foi substituída enquanto estava em `moving`, terminou `superseded` com zero comandos e não pintou resultado; somente a sequência 2 executou três comandos e confirmou o centro com erro de 10,04 pixels. A prévia bruta avançou durante toda a operação.

Após corrigir a invalidação causada por uma localização isolada perdida, uma observação estacionária de 137,6 segundos conservou `Vídeo alinhado` nas 550 amostras, com zero alternâncias, pausas, `stalled`, `emptied`, erros ou comandos PTZ. Os 890 eventos `waiting` do MSE permaneceram visíveis na instrumentação e não foram convertidos em uma falsa troca de pose. Navegação, atividade em segundo plano, movimento externo, retarget, Stop, reconexão e reabertura também passaram nos cenários de navegador; o movimento produzido pelo clique foi observado fisicamente, enquanto o caso de movimento iniciado externamente foi validado sem nova atuação física dedicada.

O ledger terminou em 35/40 comandos para esse piloto; o segundo piloto não foi usado. `physical_capture_verified=false` nos resultados significa ausência de horário de exposição certificado pelo sensor, não ausência de deslocamento observado. Evidências privadas estão em `.toposync-data/live-panorama-validation/validacao-20260919/`. Esse aceite não altera a cobertura, o estado ou as cotas da aquisição panorâmica.
