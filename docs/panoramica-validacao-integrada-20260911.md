# Panorâmica, mapeamento e PTZ: implementação e validação integrada

Autor: Mateus Calza. Data: 11 de setembro de 2026.

**Estado: implementação aplicada; aceite físico integrado ainda não aprovado.** Este registro acompanha o [plano de resolução](toposync-plano-resolucao-ponytail-revisado.md). Não certifica compatibilidade universal nem precisão em metros.

## Escopo e preservação

- Aplicação principal: `http://localhost:5174`, backend local em 8100. Movimento físico somente na **Frente Reolink, fonte wide_main**; acionamentos pela interface do Toposync.
- Fotografias, panorâmicas e decisões de movimento foram produzidas pelo algoritmo da aplicação. Não houve montagem manual ou correção manual do enquadramento durante um ensaio.
- O repositório já continha alterações extensas. A base desta execução foi preservada em `ignore/panorama-integrated-20260911/baseline/`, com manifesto. Não houve commit ou publicação.
- As composições e a calibração legada foram preservadas. Os pontos usados para o teste isolado do atuador formam uma carta projetiva de uma fotografia: **não representam uma calibração do chão e nunca devem ser ativados na composição real**.
- Configuração privada, imagens e diagnósticos ficam em `ignore/panorama-integrated-20260911/`. O arquivo de configuração contém credenciais: não anexar a pesquisas, relatórios públicos ou chamados.

## Alterações aplicadas

| Componente | Comportamento implementado | Verificação / limite |
| --- | --- | --- |
| Captura | Identidade atômica da instância, geração e sequência acompanha o frame do grabber até captura, panorama e pipeline | Não inventa timestamp de exposição. Metadados observacionais continuam distintos de comprovação física |
| Diagnóstico | Replay privado das primeiras duas tentativas aceitas e duas recusadas; sequência consumida, comandos e decisões | PNG em memória, NPZ/JSON depois de Stop; limites de bytes e quadros; truncamento explicitado |
| Movimento observado | Fluxo distribuído por regiões recupera transição em cena que não admite um único modelo global | Autoriza reconhecer a transição, sem relaxar os critérios globais de estabilidade |
| Referência recente | Uma nova observação quando uma comparação geometricamente válida cruza o limite de idade durante o processamento | Limite de um segundo mantido; repetição limitada |
| Retomada | Reconcilia destino/âncora e tentativa incerta; mantém o mesmo job, fotos e orçamento ativo | Duas retomadas físicas acrescentaram fotografias novas |
| Localização | Compara o frame real às fotografias originais, estima rotação por correspondências robustas e SVD | Reserva correspondências para avaliação; rejeita ambiguidade, suporte concentrado, fonte ou óptica incompatível |
| Transformações de imagem | Metadados explícitos de pixel da imagem até o pixel original, compartilhados por câmera e visão | Crop, resize, perspectiva, ajustes fotométricos e estabilização global; transformações desconhecidas são recusadas |
| Detecções | Cada posição na planta depende da localização do próprio frame; preserva a origem do ponto inferior da detecção | Verifica identidade, revisão, máscara e suporte. Remove coordenadas mundiais antigas ao recusar e preserva detecções brutas |
| Ativação | Mapa visual pode ser validado e salvo antes de verificar o atuador | Controle físico continua condicionado às suas próprias evidências; caminho legado mantido |
| Navegação | Resposta local medida, correções limitadas e percurso por sobreposições verificadas para alvos fora da vista | Nenhum comando novo sobre imagem obsoleta; chegada exige medição fotográfica independente |
| Retorno | Ação explícita, referência original, preset quando disponível e até quatro correções observadas | Confirmar retorno não equivale a confirmar apontamento. Cancelar não ordena retorno oculto |
| ONVIF | Descobre a faixa de timeout do perfil e separa o timeout do dispositivo do Stop curto do controlador | Frente publicou 1–10 s. O dispositivo recebe valor aceito; o controlador conserva a duração solicitada do pulso |
| UX | Um par por passo, navegação por lugares, fantasmas bidirecionais, conferência independente e retorno explícito | Prévia não move câmera; verificações escondem sugestões; erros e ações em português e inglês |

O código reutiliza OpenCV, NumPy, os jobs, o controlador com concessão exclusiva e os componentes existentes. Não adiciona serviço de atlas, framework de jobs ou dependência de visão. O módulo genérico `src/toposync/runtime/pipelines/image_geometry.py` é consumido por câmera e visão; regras específicas de câmera permanecem na extensão.

### Contratos de precisão

- Localização: fotografias originais e lente da montagem; ajuste de rotação, sem recalibrar a panorâmica por frame. Pelo menos 60 correspondências, 40 inliers de ajuste, proporção mínima de 55%; avaliação reservada com pelo menos 12 correspondências, 80% dentro do limite e p95 até 8 pixels na largura 960. Suporte espacial distribuído e rejeição de rotações concorrentes.
- Cache: vinculado à identidade do frame, aos bytes da imagem e à transformação geométrica. Uma instância reiniciada não herda a identidade anterior. Não usa a última pose boa para completar detecções atrasadas.
- Referência do scanner: mantém 15 pixels e sobreposição mínima de 85%. **Esse critério não aprova o centro do apontamento.**
- Apontamento: correlação do contexto fotográfico do alvo com a imagem atual, pico distinguível, erro até 3 pixels na largura 960. O erro angular usado pelo controlador não é o resultado desta medição.
- Retorno: comparação com a imagem original, erro até 3 pixels e sobreposição mínima de 85%.
- Pulsos: mesma velocidade observada. Um pedido abaixo do mínimo de 50 ms do controlador só pode usar esse mínimo se a resposta medida prever redução do erro; caso contrário, recusa. **50 ms é limite da aplicação, não resolução mecânica declarada pela câmera.**
- Navegação: até 64 comandos e quatro correções finas. Matriz de resposta sem suporte, mal condicionada, erro crescente, óptica alterada ou perda de localização encerram a operação.

O suporte a transformações não inclui inversão automática de qualquer operação não linear. Por exemplo, uma correção de lente sem procedência geométrica compatível mantém o frame sem mapeamento. Preservar uma imagem do mesmo tamanho não prova que seus pixels estejam na geometria original.

## Ensaios reais e o que provaram

| Trabalho / artefato | Resultado observado | Interpretação |
| --- | --- | --- |
| Captura `7bcf0c41084a40baab4f5cbe982e7cbe` | 22 fotografias; retomada não acrescentou fotos | Reprovação preservada. Replay demonstrou movimento em parede próxima, recusado pelo modelo global |
| Captura `88361337941a408dbb8bdfc05ab70ef9` | Uma fotografia; referência mudou de idade 0,931 para 1,013 s enquanto era comparada; deslocamento de 0,003 pixel | Causa da recusa temporal demonstrada; motivou uma nova observação limitada, sem ampliar o prazo |
| Captura `454fb2d3c373476a946be6cc526b6692` | 47 fotografias, dois ramos horizontais, faixas 0 e 1; retorno confirmado | Captura parcial: ainda houve movimento não observado e conexão vertical não verificada |
| Artefato `016365a6fc054d66a160a9a12fbe4b3e` | p95 reservado 7,09 pixels; 250 vínculos de sobreposição | Geometria aprovada para a parte capturada. Máscara de 34,35% dos pixels não mede percentual do alcance mecânico |
| Captura `399ca35c432a4344bff75530c4fd5b38` | Interrompida durante tentativa; retomada de 5 para 18 fotos; nova interrupção ao registrar foto; retomada de 18 para 21 | **Continuação comprovada**, no mesmo job. Tempo ativo acumulado 183,46 s; não foi uma remontagem de fotos antigas |
| Artefato `b19a81ca0fd6433db1a8182a6c072663` | 21 fotos, 126 vínculos, p95 reservado 2,45 pixels; retorno confirmado | Fonte ativa ao final da captura. Não certifica cobertura completa; o artefato anterior com 47 fotos permanece disponível |

Os dois primeiros replays recusados atingiram o limite antigo após 129 quadros, aproximadamente 10,75 s. Contêm a transição e a estabilização relevantes; não devem ser apresentados como os 12 s completos. A compressão limitada foi corrigida depois disso. A substituição do modelo global por afim global também foi testada e recusada: não resolveu os exemplos. O detector distribuído encontrou as transições aos 1,198 e 1,148 s, sem mudar o gate de estabilização posterior.

### Apontamento: hipóteses testadas, sem apagar as reprovações

| Estratégia | Resultado | Decisão |
| --- | --- | --- |
| Duração variável e velocidade reduzida abaixo do mínimo de duração | 12 comandos; erro angular caiu e voltou a crescer; chegada não confirmada | A conversão automática de duração em velocidade não foi aceita como calibração equivalente |
| Duração fixa e velocidade variável | Nove comandos; comando de velocidade 0,0146 associado a variação nativa de pan de 1199 para 1396 | Estratégia removida. Unidade nativa desconhecida; não são graus. A causa interna do dispositivo não foi demonstrada |
| Timeout do dispositivo conforme faixa publicada, Stop local curto | Nove movimentos observados; próximo pedido de 22 ms recusado pelo limite do controlador | Corrigiu uma violação concreta do protocolo, mas não aprovou o apontamento |
| Retorno após esse ensaio | Preset aproximou com erro de 13,27 pixels; uma correção observada de 80 ms reduziu para 1,26 pixel | Retorno confirmado pela imagem original |
| Pulso mínimo condicionado à resposta observada | Onze movimentos; limite de quatro ajustes finos atingido sem chegada | O diagnóstico revelou invalidação conjunta de pan/tilt: uma resposta de tilt recente foi descartada por uma referência global antiga. Corrigido por eixo, com teste que reproduz o caso |
| Repetições de retorno após esse ensaio | Duas reprovações preservadas; uma inversão de comando quase não alterou a imagem, mas seu ruído foi interpretado como ganho | Retorno agora usa os eixos já suficientes e recusa deslocamento abaixo de dois pixels como nova resposta de motor; não presume que a folga desapareceu |
| Resposta por eixo, alvo 7 | Localização recusada após quatro movimentos observados | O JPEG regravado localizou no replay, mas regravação altera correspondências: não prova erro da decisão sobre o frame original. O diagnóstico final agora preserva PNG; a recuperação pode observar até três frames recentes, parada, sem baixar critérios |
| Alvo inferior, ponto 9, com todas essas correções | Doze movimentos observados; última medição independente de **6,94 pixels**, correlação 0,949; pulso seguinte de 50 ms sem transição confirmada | **Reprovado**. A medição é da última imagem qualificada antes do pulso recusado, não uma certificação da posição física final |
| Retorno final | Preset aproximou com erro de 7,92 pixels; pulso de pan de 80 ms terminou em 8,00 pixels, sem resposta distinguível do ruído | **Parada confirmada; retorno não confirmado.** A correção encerrou sem transformar ruído em ganho de motor |

A especificação separa o timeout opcional de `ContinuousMove`, a faixa aceita pelo perfil e a operação `Stop`; não garante que uma velocidade normalizada produza resolução angular uniforme. Referência consultada nesta execução: [ONVIF PTZ, seções 5.2.4, 5.3.3 e 5.3.5](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec.pdf).

## Verificação automatizada

Registros em `ignore/panorama-integrated-20260911/`:

- **`final-combined-tests.log`: 742 testes passaram em 138,57 s**, cobrindo os 19 arquivos selecionados após os ajustes finais de navegação, protocolo e ciclo de vida. Ruff passou nos arquivos de produção alterados. Esta é a regressão Python consolidada; os números abaixo registram as etapas e não devem ser somados.
- `final-python-tests.log`: 632 testes de panorama, captura, estabilidade, reconstrução, localização, mapeamento, API, processamento e visão passaram antes do ajuste final do timeout/pulso mínimo.
- `timeout-tests.log`: 131 testes passaram após o ajuste ONVIF. Incluem o corpo SOAP com `PT1S` e o Stop do controlador em 50 ms, sem aumentar o pulso local.
- `visual-lifecycle-tests.log`: 31 testes passaram, incluindo falha/cancelamento na descoberta e falha de persistência com limpeza de propriedade, sem estado falso de conclusão.
- `quantized-navigation-tests.log`: 16 testes passaram. O simulador independente cobre resposta com sinal invertido e acoplamento, percurso por sobreposições e recusa quando o mínimo de pulso impede a precisão. Inclui validade por eixo e retorno diante de folga. A versão antiga do simulador aceitava pulsos abaixo do mínimo real do controlador; esse erro do simulador foi corrigido.
- `navigation-lifecycle-final.log`: 50 testes passaram após a recuperação limitada de localização; inclui recusa imediata de ambiguidade e comprovação de que a recuperação não envia movimentos.
- `browser-tests-final.log`: **13 testes de navegador passaram em 1,2 minuto** com o bundle final atualizado. Cobrem associação, edição, desfazer, teclado, fantasmas, conferência sem sugestões, recuperação, revisão, ativação, tradução, erro de retorno persistido após reabertura e caminho de ingress do Home Assistant.
- `ui-unit-final.log`, `frontend-typecheck-final.log`, `camera-typecheck-final.log`, `camera-build-final.log`: testes da UI, tipos e build; o build mantém aviso de tamanho de chunks.

Essas contagens se sobrepõem; não somá-las como testes únicos. Não foi executada nem aprovada a suíte inteira do repositório. Ao ampliar a verificação, `test_pipeline_yolo_fanout.py` e `test_vision_track_integrations.py` falharam na criação de grafos antigos com `schema_version: 1`, antes do código de detecção, devido ao contrato atual de grafo versão 2. Onze falhas desse conjunto estão registradas; não foram escondidas nem corrigidas por alteração alheia a esta entrega.

## Lacunas para o aceite do plano

| Objetivo | Estado / evidência necessária |
| --- | --- |
| Rua de ambos os lados e chão útil, sem lacunas na região necessária | Capturas horizontais avançaram; continuidade vertical e cobertura integral da área útil ainda não aprovadas |
| Duas formas de interrupção e continuação | Aprovado na Frente: novas fotos no mesmo trabalho, sem renovar orçamento |
| Localização e mapeamento do próprio frame | Contrato integrado e testes automatizados aprovados; falta o ensaio contínuo real anotado |
| Precisão de até 0,5 m | Não validada. Falta escala confirmada e correspondências físicas independentes na planta; os oito pontos antigos não têm imagem/pose vinculada que permita reaproveitamento automático |
| Disponibilidade de pelo menos 90% | Não medida nos três trechos contínuos de um minuto previstos; recusa do algoritmo não pode ser retirada do denominador |
| Seis apontamentos e retornos, com três alvos e duas aproximações | Ainda não aprovados. Os ensaios recusados não contam como chegada bem-sucedida |
| Memória, latência e perdas em execução contínua | Limites implementados; levantamento completo de p50/p95 e pico ainda não realizado |
| Câmeras de outras marcas / noite | Não ensaiadas nesta execução. Testes por capacidade não substituem validação física |

Não aumentar tolerância, converter erro angular em “precisão comprovada” ou retirar regiões difíceis do recorte para conseguir um aceite. O próximo ensaio deve atacar uma divergência registrada e conservar as evidências anteriores. Precisão de planta depende de referências físicas que ainda precisam ser confirmadas; funcionamento do atuador continua sendo um requisito próprio.

### Estado deixado na aplicação

- Aplicação principal mantida em **5174**, com o código desta execução. A Frente está **parada**; o retorno exato não foi confirmado.
- Todos os 16 presets anteriores foram preservados. O único preset adicional, **016**, pertence à referência original do trabalho `4bbd9ec08cca4814a8d78154f49b1292` e foi mantido porque ainda é necessário ao retorno.
- Esse trabalho permanece na **revisão 3, sem pontos técnicos e sem ativação**. O arquivo privado original e o destino de retorno foram preservados. Apagar o trabalho/preset neste estado destruiria uma opção de recuperação.
- As composições permanecem idênticas ao baseline. A calibração legada não foi substituída. Os rascunhos de navegador gerados pelo ensaio foram removidos somente quando vinculados ao identificador deste trabalho. A carta de pontos e os resultados anteriores continuam no arquivo privado de evidências.
- O erro de navegação salvo continua visível ao reabrir o wizard. A fonte panorâmica ativa tem 21 fotos; o artefato anterior de 47 fotos continua disponível.

Evidências: `physical-acceptance.json`, `preservation-final.json`, `presets-before.json`, `presets-final.json`, `aim-sixth-lower-target/` e `final-retained-return-reference/`. A diferença entre limitação mecânica, atraso de comando, folga e observação insuficiente ainda não foi isolada em todos os casos; os resultados não autorizam atribuir toda a falha à marca ou ao hardware.

## Reprodução e reversão

1. Conferir código carregado, fonte `wide_main`, artefato ativo e propriedade de presets. O backend de desenvolvimento não recarrega sozinho; a UI da extensão exige `npm run build:extension-ui -- cameras` e recarregamento do navegador.
2. Para testar software sem câmera, usar os testes dirigidos acima e `npx playwright test --config playwright.panorama.config.js`; a configuração de navegador usa dados e portas isolados.
3. Para reproduzir apontamento, usar fotografia/pixel preservados no manifesto técnico. Nunca ativar a carta de teste como planta real. Acionar pela UI; preservar diagnóstico antes de nova tentativa.
4. Conferir Stop e retorno separadamente. Não reiniciar o processo durante movimento nem mandar comandos ao equipamento por fora do controlador para melhorar o resultado.
5. Reverter somente o delta desta execução contra o baseline preservado. Não usar reset amplo: existe trabalho anterior não commitado. Dados de composição e retorno físico precisam de verificações separadas.

Os manifests e imagens detalhados são locais e privados. O relatório apresenta resultados medidos e limites sem expor endereços autenticados, senhas ou dados de acesso.
