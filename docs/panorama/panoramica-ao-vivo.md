# Panorâmica ao vivo

O modo **Panorâmica ao vivo**, no seletor de renderização, combina uma referência panorâmica salva com a fonte de vídeo existente. A disponibilidade de uma imagem panorâmica não certifica alinhamento ou apontamento: esses estados precisam de observação qualificada.

## Interação

Selecione a câmera panorâmica no controle superior, no mesmo lugar em que outras visualizações mostram a composição. A lista usa o modal e os estados visuais da plataforma, mostra somente câmeras com geometria compatível e prefere o panorama ativo; um candidato só é usado quando não existe ativo elegível. Não há um segundo seletor dentro da imagem.

A área útil salva é usada automaticamente quando existe; sem ela, aparece a imagem integral. Arraste, roda do mouse, pinça e botões de ampliação navegam na imagem. Eles não comandam pan, tilt ou zoom óptico. O botão **Parar** só aparece enquanto existe uma operação física em andamento.

Quando um frame atual é localizado, o vídeo em cores é projetado no panorama cinza. Um clique em cobertura válida solicita centralização. O marcador mostra o destino desejado; só a medição visual independente do controlador confirma o centro. Durante movimento ou perda do registro, o mesmo player continua na prévia flutuante. **Parar** cancela o restante da operação própria e o destino pendente; o reconhecimento HTTP não é prova de parada física.

Uma operação física pode estar ativa, com apenas uma intenção pendente substituível. O último clique substitui os anteriores. A intenção vence em dez segundos enquanto espera para começar; sessões sem contato por quinze segundos são encerradas cooperativamente. A operação reutiliza a posse e a confirmação de parada do controlador. Uma parada incerta suspende novos comandos daquela sessão. Cada alvo admite no máximo 16 comandos, incluindo as sondagens necessárias para observar a resposta dos eixos; esse teto não renova o orçamento cumulativo do ensaio e pode produzir uma recusa explícita se o domínio não for alcançável dentro dele.

A rota usa as referências intermediárias até uma observação fresca colocar o alvo final dentro da lente. A aproximação final reutiliza o planejador de pulsos finos do retorno visual. Uma sondagem de identificação sem efeito só admite uma tentativa maior, limitada, quando o scanner comprova a janela causal sem movimento e a parada; ausência de resposta, por si só, não autoriza repetir comandos.

O painel de teleobjetiva só existe quando há uma fonte habilitada e distinta, com papel `zoom`, no mesmo dispositivo. Ele é independente da geometria principal, pode ser movido, redimensionado e minimizado. Cabeçalho, espaçamento, foco e botões seguem os controles da plataforma. Não controla Z.

Ocultar a aba suspende seus consumidores e análises; retornar exige frames novos e, depois de uma pausa longa, uma sessão nova. Minimizar a teleobjetiva suspende somente essa fonte. Mudança na época do controlador invalida o registro mesmo quando o movimento externo terminou entre consultas. Reabrir descarta destino, marcador e respostas da sessão anterior.

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

O localizador normaliza descritores SIFT, agrupa correspondências pela posição óptica e separa ajuste e conferência por identidade estável na referência. Repetir descritores ou reordenar casamentos não cria suporte independente nem muda a partição. A exclusão de texto fixo é compartilhada com a reconstrução e exige movimento coerente da cena próxima. Os limites de erro, distribuição espacial e frescor continuam obrigatórios.

Quando a aparência original não fornece suporte suficiente, há uma segunda tentativa com contraste local normalizado igualmente nas referências e no frame atual. Ela reutiliza o mesmo estimador, limite de pontos e critérios de conferência; nunca substitui uma pose já qualificada nem uma recusa por ambiguidade. As duas representações de descritores são limitadas pelo mesmo número máximo de referências e não alteram fotografias, geometria ou vídeo exibido. A tentativa adicional não garante correspondência entre uma cena noturna e referências diurnas: sem suporte real, a prévia continua sem alinhamento e o apontamento fica indisponível.

Se essas tentativas falharem, o localizador compartilhado pode propor correspondências com DISK e LightGlue, executados localmente pelo ONNX Runtime existente. Os dois modelos da exportação v0.1.0 são fixados por tamanho e SHA256 em `processing/panorama_features.py`; o download verificado ocupa aproximadamente 49 MiB no diretório privado `runtime/cameras/panorama-matching-models`. Nenhuma imagem é enviada à rede. Indisponibilidade, corrupção ou falha do modelo mantém a localização recusada e aplica intervalo antes de tentar novamente. A confiança do modelo apenas seleciona pares; todos os critérios geométricos continuam obrigatórios.

Após reconhecimento qualificado, o fluxo óptico bidirecional compartilhado com a verificação de estabilidade mede esses detalhes nos quadros seguintes antes de repetir o reconhecimento. A imagem âncora e os pixels das fotografias originais permanecem fixos: não há encadeamento de poses. Cada quadro refaz ajuste, conferência independente e comparação de alternativas. Origem, geração, geometria ou intervalo de observação incompatíveis invalidam esse acompanhamento. São mantidas no máximo quatro âncoras em memória.

Quando o acompanhamento perde suporte, a última orientação qualificada pode selecionar até oito fotografias originais próximas, mais a última referência, para uma busca limitada com até quatro comparações simultâneas. Essa orientação serve apenas para escolher fotografias; a aceitação depende de correspondências novas. Se a busca local não qualificar, o quadro é recusado. A âncora pode servir para medir o próximo quadro apenas dentro do prazo original de cinco segundos, sem renovar sua validade ou devolver sua pose. Ambiguidade a elimina; após expiração ou troca de identidade, volta o reconhecimento global. Reconhecimento demorado pode preparar uma âncora, mas não autoriza projeção nem movimento: a interface rejeita a observação acima de 1,5 segundo, e a navegação exige um novo quadro dentro do limite de um segundo. Os testes noturnos reais ainda precisam demonstrar estabilidade prolongada e chegada física; qualificação offline não certifica esses resultados.

A publicação de frames brutos conserva seus intervalos reais de chegada: o limite de frequência configurado não transforma intervalos variáveis em uma linha temporal artificialmente curta. O agendador compartilhado acorda para o próximo prazo das saídas ativas, sem limitá-las ao intervalo de manutenção; saídas ociosas ou alimentadas diretamente por RTSP conservam sua espera, e atrasos não geram rajadas de frames antigos. No player, renovar `media_token` não substitui uma conexão MSE/WebRTC saudável; uma reconexão usa a credencial atual. Alterar fonte, qualidade ou caminho continua substituindo a mídia. O MSE confirma o primeiro quadro decodificado, limita a fila e recupera falhas terminais com a política de tentativas existente.

Após uma operação de navegação, os três últimos inputs exatos do localizador podem ser preservados em `runtime/cameras/live-panorama-localization`, somente depois de Stop. O replay privado inclui imagens, geometria, evidência de captura, hashes e diagnósticos; retém no máximo dois arquivos e 256 MiB. Ele não certifica a captura física e não deve ser publicado junto do código.

Imagem integral, máscara, lente/modelo, revisão e recorte pertencem ao mesmo artefato. Alteração do ponteiro, identidade ou modelo invalida a sessão. Abrir o modo não promove candidato, não cria presets, não inicia aquisição e não retorna automaticamente ao enquadramento anterior.

## Verificação e limite de aceite

```sh
.venv/bin/python -m pytest tests/test_camera_live_panorama.py tests/test_camera_panorama_navigation.py tests/test_camera_panorama_localization.py tests/test_camera_panorama_api.py -q
node --test extensions/cameras/ui/tests/panoramaVideo.test.cjs
npx playwright test --config playwright.live-panorama.config.js
npx playwright test --config playwright.viewport.config.js
npx playwright test --config playwright.stream-player.config.js
```

A fixture de navegador não acessa câmeras: usa o viewport, renderer e adaptador de frames reais com vídeo sintético e API de controle simulada. Testa navegação sem comandos, continuidade do mesmo decodificador, retarget, falha, Stop e reabertura. O replay opcional de fotografia requer manifesto local em `TOPOSYNC_PHOTOGRAPH_FIXTURE`, nunca versiona as fotos e tem aviso visível.

A fixture `stream-player` usa o player do host, MediaSource e decodificação H.264 reais, com segmentos sintéticos e WebSocket controlado. Verifica renovação sem reinício, recuperação com credencial atual, troca efetiva de fonte e suspensão. Não substitui a prova da câmera física.

### Histórico e revalidação

Os resultados abaixo têm alcance histórico. A observação de 19/09/2026 documentada no checkpoint reproduziu oscilação e dois apontamentos interrompidos; invalidou o aceite geral de estabilidade. A retomada implementou as correções acima e confirmou uma centralização interna real, além de janelas estáticas sem perda do registro, suspensão em aba oculta e recuperação de conexão. O aceite geral permanece parcial: fluidez, imagem corrompida já na captura e chegada de destinos externos/substituídos continuam pendentes. Evidências privadas em `live-panorama-validation/correcao-20260919`; o checkpoint registra medições, limites e orçamento consumido. O rótulo de vídeo alinhado certifica o registro geométrico daquele frame, não ausência de corrupção em toda a imagem recebida.

Em 19/09/2026, o núcleo foi validado na Frente Reolink sem nova varredura panorâmica. Um clique distante foi centralizado com três comandos e erro visual final de 1,58 pixel na imagem de análise de 960 pixels. Em outro ensaio, a sequência 1 foi substituída enquanto estava em `moving`, terminou `superseded` com zero comandos e não pintou resultado; somente a sequência 2 executou três comandos e confirmou o centro com erro de 10,04 pixels. A prévia bruta avançou durante toda a operação.

Após corrigir a invalidação causada por uma localização isolada perdida, uma observação estacionária de 137,6 segundos conservou `Vídeo alinhado` nas 550 amostras, com zero alternâncias, pausas, `stalled`, `emptied`, erros ou comandos PTZ. Os 890 eventos `waiting` do MSE permaneceram visíveis na instrumentação e não foram convertidos em uma falsa troca de pose. Navegação, atividade em segundo plano, movimento externo, retarget, Stop, reconexão e reabertura também passaram nos cenários de navegador; o movimento produzido pelo clique foi observado fisicamente, enquanto o caso de movimento iniciado externamente foi validado sem nova atuação física dedicada.

O ledger terminou em 35/40 comandos para esse piloto; o segundo piloto não foi usado. `physical_capture_verified=false` nos resultados significa ausência de horário de exposição certificado pelo sensor, não ausência de deslocamento observado. Evidências privadas estão em `.toposync-data/live-panorama-validation/validacao-20260919/`. Esse aceite não altera a cobertura, o estado ou as cotas da aquisição panorâmica.
