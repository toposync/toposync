# Panorâmica: execução do plano de estabilização

Autor: Mateus Calza. Período: 11 e 12 de setembro de 2026.

**Estado mais recente em 12 de setembro: a captura 9 da Garagem comprovou o retorno ao último enquadramento conectado e a emissão de um único meio passo depois de uma lacuna geométrica. Ela guardou 11 fotografias conectadas somente no lado negativo de pan e retornou à referência com 1,897 pixel de erro. A aquisição permaneceu parcial: o detector temporal não confirmou o movimento do meio passo sobre a parede lisa, o estado ficou preso em `retry_pending` e o lado oposto não foi percorrido. O artefato menor substituiu a captura 8 como ativo, revelando uma regressão separada no critério de publicação. As correções de estado são prospectivas; o trabalho histórico não foi reescrito.**

**Estado preservado após a captura 8:** a Garagem comprovou o envio seguro de pulsos contínuos de 1,2 segundo com timeout de proteção ONVIF inteiro, guardou e conectou todas as 17 fotografias adquiridas e ampliou a cobertura horizontal observada. A aquisição ainda era parcial: nenhuma extremidade física foi confirmada, nenhuma banda de tilt foi iniciada e o retorno terminou 16,754 pixels distante da referência. A precisão de apontamento continuou sem validação.**

**Estado preservado após a captura 5:** captura ampla e reconstrução parcial foram comprovadas na Garagem. A câmera percorreu os limites horizontais publicados e o limite inferior de tilt, guardou 91 fotografias e produziu um mosaico geometricamente consistente no componente principal. O retorno óptico terminou 5,081 pixels distante da referência e a precisão de apontamento não foi validada. Sob o código executado naquele ensaio, o primeiro artefato parcial foi promovido a ativo apesar do status `review`; a política posterior foi corrigida prospectivamente, sem reclassificar silenciosamente esse dado histórico.**

Este documento registra a execução do [plano desta etapa](plano-estabilizacao-panoramica-20260911.md). A evidência privada fica em `ignore/panorama-stabilization-20260911/` e em `.toposync-data/runtime/cameras/source-panorama/`. Não compartilhar esses diretórios inteiros: eles contêm imagens residenciais e uma cópia protegida da configuração.

## Como interpretar o resultado

Há três aceites independentes:

1. **Captura e mosaico:** a câmera consegue percorrer uma área, adquirir fotografias úteis e uni-las com evidência geométrica.
2. **Retorno óptico:** depois da operação, o enquadramento volta à imagem de referência com deslocamento de até 3 pixels na escala de análise 960 e sobreposição mínima de 85%.
3. **Apontamento:** um alvo escolhido na panorâmica ou na planta produz, sem auxílio humano, o enquadramento esperado na câmera real.

A captura 5 da Garagem avançou o primeiro item. Ela não aprovou o segundo nem o terceiro. Erro baixo do otimizador mede coerência entre fotografias verificadas; não mede precisão do atuador e não constitui verdade de campo independente.

## Histórico útil de 11 de setembro

As primeiras execuções nas câmeras Frente, Corredor, Garagem e Quintal reproduziram falhas reais de temporização, persistência e retorno. Os valores abaixo são pixels na largura de análise 960, comparados à referência inicial do respectivo trabalho.

| Câmera | Ensaio inicial | Última medição daquele ensaio | Interpretação histórica |
| --- | --- | --- | --- |
| Frente Reolink | Primeiro pan positivo observado: 9,49 pixels; retorno e um ajuste fino | 4,01 pixels; sobreposição 99,27% | Parada confirmada; retorno acima do limite |
| Corredor | Primeiro pan positivo observado: 17,02 pixels; retornos limitados contra a mesma referência | 16,12 pixels; sobreposição 97,85% | Aproximação absoluta insuficiente e pulso fino sem transição confirmada |
| Garagem | Primeiro pan positivo observado: 18,25 pixels; retorno e correções observadas | 24,23 pixels; sobreposição 97,29% | Resposta inconsistente; o erro chegou a 7,60 pixels antes de crescer novamente |
| Quintal | Pan positivo e retorno aprovados; pan negativo observado | 1,08 pixel; sobreposição 99,61%, após recuperação explícita | Referência daquele trabalho recuperada; repetibilidade completa não aprovada |

Tilt e as repetições restantes não foram ensaiados naquela rodada, porque o protocolo interrompia na primeira perda de confirmação. Isso não demonstrava ausência de tilt. Também não demonstrava incompatibilidade das câmeras: demonstrava que o protocolo não tinha evidência suficiente para aprovar seus retornos.

As correções daquela etapa continuam válidas:

| Falha reproduzida | Correção aplicada |
| --- | --- |
| O watchdog contava novamente a duração completa depois da resposta do comando | Descontar o intervalo entre envio e resposta; prazo esgotado agenda Stop imediato, serializado e protegido pela concessão |
| Preparação ONVIF era indistinguível de envio ao motor | Medir a chamada de transporte depois da descoberta e autenticação; scanner e watchdog usam o mesmo orçamento restante |
| Uma interrupção transitória encerrava cedo a observação da referência | Permitir que a referência use o orçamento global restante, sem reiniciar o prazo e sem reduzir os critérios visuais |
| O filtro de dados privados apagava identificadores conhecidos de coordenadas ONVIF | Preservar somente os quatro identificadores normalizados reconhecidos; continuar removendo endpoints e variantes desconhecidas |
| Uma direção reutilizava a resposta da direção oposta | Aprender por eixo e sentido, interrompendo resposta incoerente, erro crescente ou falta de confirmação |

As cópias `baseline/` e `initial-qualification/` preservam o estado anterior. Os trabalhos de Corredor, Garagem e Quintal tiveram somente identificadores de sistemas de coordenadas reparados a partir de seus manifests originais. `repair_coordinate_identifiers.py` exige igualdade dos demais campos e registra hashes antes e depois. Não altera pan, tilt, zoom, câmera, fonte, identidade ou referência fotográfica.

## Causa do controle perdido e tentativa 3 da Garagem

A tentativa 2 da Garagem falhou com `stop_unconfirmed` e `ownership_lost`. A causa foi localizada na interação entre o pulso finito e a concessão do controlador: o watchdog mudava o estado para `stopping`; nesse intervalo, a renovação da mesma concessão cercada e um Stop redundante podiam ser recusados como se outro proprietário tivesse assumido a câmera.

A correção mantém a mesma concessão válida durante o Stop do watchdog, serializa Stops concorrentes e permite que um Stop completo recupere uma falha transitória do mesmo proprietário. Ela não permite que um proprietário antigo interrompa um controlador novo.

Depois dessa correção, a tentativa 3 (`b6c026e6744b47298434f10bb2d9029d`) não perdeu controle nem confirmação de Stop:

| Movimento | Deslocamento observado | Retorno observado |
| --- | ---: | ---: |
| Pan positivo | 4,106 pixels | 0,228 pixel |
| Pan negativo | 39,342 pixels | 4,153 pixels após o retorno automático |

Um retorno explícito posterior terminou em **1,484 pixel**, dentro do limite de 3 pixels. Esse sucesso isolado não estabelece repetibilidade. Entre observações da tentativa, a mesma coordenada ONVIF correspondeu a enquadramentos separados por aproximadamente **14,07 pixels**. Portanto, o readback de posição não pode ser tratado como encoder óptico nem como prova de que o enquadramento voltou.

## Correção fina adaptativa e salvaguardas

O retorno fino passou a escolher entre velocidades normalizadas **0,025, 0,05 e 0,1**, mantendo **50 ms** como menor duração autorizada pela aplicação. O produto entre velocidade e duração (`V × t`) é apenas uma previsão para escolher um único pulso limitado. O efeito realmente aprendido vem da imagem observada depois do comando.

Uma resposta só atualiza o modelo quando a comparação é finita, geometricamente verificada, tem pelo menos 85% de sobreposição, sentido coerente, resposta mínima de 2 pixels e melhora compatível com a previsão. Sentido invertido, razão entre resposta observada e prevista fora de 0,4 a 2,5 ou erro que piora mais de 20% interrompem o aprendizado.

As salvaguardas seguintes são cumulativas para a operação inteira:

- no máximo quatro comandos de correção fina no total, compartilhados entre modalidades absolutas, contínuas e relativas e entre retomadas do mesmo trabalho; um estado `planned` persistido antes do comando já consome orçamento;
- a imagem usada para autorizar o movimento precisa ser a mesma referência esperada pelo comando, recente e ainda válida imediatamente antes do envio;
- deslocamento, eixos e duração precisam ser finitos; direção aceita somente `-1` ou `1`; duração não pode ser maior que 2 segundos;
- controle contínuo autônomo só é usado quando a câmera publica um intervalo de timeout ONVIF finito e positivo cuja menor duração não excede 2 segundos; se a descoberta da conexão usada no comando não confirmar essa garantia, o `ContinuousMove` estrito não é enviado;
- Stop, perda de concessão, cancelamento ou comparação insuficiente não são reinterpretados como retorno aprovado.

As velocidades 0,025 e 0,05 e a seleção adaptativa têm validação automatizada, mas ainda não foram exercitadas por um ensaio físico preservado. Na tentativa 4, a única correção fina registrada usou velocidade 0,1 por 80 ms, sem direção ou velocidade previamente medidas e sem `prediction_basis`; seu resultado foi qualificado pela imagem depois do movimento.

## Tentativa 4 de verificação da Garagem

A tentativa 4 (`4bd59343a3704bdd9b6ef80ab89b5e05`) validou duas direções consecutivas de pan:

| Movimento | Deslocamento observado | Retorno observado | Resultado do ciclo |
| --- | ---: | ---: | --- |
| Pan positivo | 3,554 pixels | 0,374 pixel | Aprovado |
| Pan negativo | 40,636 pixels | 2,988 pixels | Aprovado, próximo ao limite |

No terceiro gate, a comparação da nova referência foi recusada como `reference_unconfirmed`. O ensaio revelou duas falhas de registro e estado, ambas corrigidas sem ampliar o limite de 3 pixels:

1. a comparação que fundamentava `reference_unconfirmed` não era anexada ao check antes de persistir; agora o manifest conserva essa evidência limitada;
2. o estado `restored` herdado do ciclo anterior ficava obsoleto quando a nova janela não confirmava a referência; agora a janela fresca prevalece e o estado passa a `stopped`.

O arquivo histórico da tentativa 4 ainda contém o estado antigo porque é evidência imutável do código executado. As correções foram cobertas por testes depois do ensaio físico; não há uma nova execução física que permita atribuir a elas outro resultado mecânico.

## Falha da captura 4 e correção genérica

A captura 4 (`652e8bb3fdbb46e0bb48548f3f1c7bf2`) terminou com zero fotografias, `frame_acquisition_timeout` e referência de retorno indisponível. Os diagnósticos registraram quadros antes do timeout; zero fotografias não significava ausência total de vídeo. O detector apagava a janela de estabilidade quando imagens duplicadas apareciam intercaladas com imagens distintas, e um timeout posterior a quadros já observados era classificado como falha inicial de aquisição.

A correção não contém regra por câmera ou fabricante:

- uma duplicata intercalada preserva a janela formada por quadros distintos, mas nunca a amplia nem conta como tempo estável;
- uma sequência prolongada de duplicatas ainda invalida a transição e esvazia a janela;
- timeout antes de qualquer quadro permanece `frame_acquisition_timeout`;
- timeout depois de quadros observados passa a representar falha de observação/estabilidade, preservando a evidência adquirida.

Assim, imagens repetidas não podem criar estabilidade falsa, e também não apagam progresso visual válido apenas por estarem intercaladas.

## Captura 5 e reconstrução da Garagem

A captura 5 (`4758ae63e1fe4e48b043ad7efc0bfcf1`) executou o fluxo normal do produto com timeout nominal de **1.200 segundos**. O trabalho guardou **91 de 145 fotografias planejadas** e produziu o artefato parcial `016ea455eaef48059a6cf540d30865b2`.

| Medida | Resultado |
| --- | ---: |
| Fotografias usadas no componente principal | 47 |
| Fotografias omitidas da reconstrução | 44 |
| Pares geométricos verificados | 166 |
| Componentes no mosaico escolhido | 1 componente principal com 47 fotografias |
| Cobertura por pixels | 37,9404% |
| Cobertura por ângulo sólido | 48,1157% |
| Erro de holdout p95 | 4,353 pixels / 0,343° |
| Estado do otimizador | Sucesso |
| Modelo de lente | Monotônico; número de condição 8,431 |

O status de qualidade é `review`, com motivo `disconnected_captures`. O componente usado é internamente conectado, mas 44 fotografias aceitas não se ligaram com evidência suficiente e foram omitidas. O holdout usa trilhas completas de correspondências verificadas; ele evita avaliar exatamente os mesmos pontos usados no ajuste, mas ainda não é verdade física independente.

A aquisição confirmou os limites publicados de **pan mínimo**, **pan máximo** e **tilt mínimo** por limite anunciado e chegada observada. O intervalo de tilt efetivamente observado foi de **−1 a 0,109375**. A varredura atingiu o orçamento antes de completar todas as bandas; por isso a cobertura continua parcial, mesmo tendo alcançado as duas extremidades horizontais e o limite inferior vertical.

O trabalho terminou com avisos de movimento não observado, textura insuficiente, ligações não confirmadas, estabilidade, orçamento e retorno. Eles não foram ocultados pelo mosaico bem-sucedido. Mesmo assim, o inventário posterior mostra `active.id` igual a `016ea455eaef48059a6cf540d30865b2`, `candidate: null` e `previous: null`; o inventário anterior tinha `active: null`. Portanto, o artefato foi gravado e promovido automaticamente como primeira panorâmica ativa da Garagem. O status `review` descreve sua qualidade e a apresentação na interface, mas não atuou como gate de promoção.

### Por que 44 fotografias ficaram desconectadas

A análise do grafo separou falha de aquisição, falta de textura e planejamento inadequado. As 91 fotografias eram distintas; não houve congelamento do vídeo nem repetição de hash. Antes da reconstrução, 87 nós de grade ou ponte formavam apenas 41 arestas e 47 componentes. O maior componente do scanner tinha 39 fotografias. A reconstrução chegou a 47 ao incorporar quatro pilotos e recuperar outras quatro imagens por correspondências adicionais, sem inventar ligações para as demais.

| Evidência | Resultado |
| --- | ---: |
| Mediana de pontos SIFT nas fotografias usadas | 884 |
| Mediana de pontos SIFT nas fotografias omitidas | 78 |
| Fotografias omitidas com menos de 100 pontos | 34 de 44 |
| Fotografias de ponte capturadas | 40 |
| Pontes usadas / omitidas | 17 / 23 |
| Pares de ponte conectados / não resolvidos / sem textura / sem desfecho | 5 / 19 / 16 / 10 |
| Subdivisões planejadas / capturadas / sem movimento observado | 57 / 40 / 17 |
| Tempo gasto em deslocamentos para pontes depois omitidas | pelo menos 167,38 s |

Os pilotos mediram respostas muito diferentes: aproximadamente 1.726 pixels por unidade de pan e 240 pixels por unidade de tilt, razão de 7,2 para 1. O planejador executado aplicava o mesmo teto de passo de `0,24` aos dois eixos e criou uma grade 10 × 10. A sobreposição prevista era próxima de 60% na horizontal e 90% na vertical. Isso gastou posições onde havia sobreposição vertical excessiva, deixou a horizontal perto do limite de conexão e, com o orçamento consumido por pontes, visitou apenas 56 dos 100 pontos-base. A força bruta sobre todos os 4.095 pares possíveis não resolveu o problema: manteve 47 fotografias e piorou o holdout para 14,5 pixels. A reconstrução recusou corretamente imagens sem suporte; a causa principal estava no percurso e na observabilidade, não no otimizador.

## Implementação posterior à captura 5 e anterior às capturas 6 a 8

As mudanças desta seção têm cobertura automatizada, mas ainda não foram comprovadas por uma nova panorâmica real. A evidência física preservada continua sendo a captura 5, executada com o planejador e a política de promoção anteriores.

O planejador absoluto passou a tratar pan e tilt separadamente e a medir o movimento óptico bidimensional pela homografia verificada. Ele não presume que pan desloca somente X nem que tilt desloca somente Y. A sobreposição considera toda a transformação da imagem, inclusive os termos cruzados entre eixos. A deriva do eixo que não foi comandado deve permanecer abaixo de `min(0,002; 20% do delta principal)` no espaço normalizado.

O primeiro protótipo novo ainda escalava linearmente a homografia do piloto. Um contraexemplo pinhole mostrou a falha: a aproximação previa sobreposição `0,6804`, enquanto a rotação projetiva correspondente produzia `0,6698`. Essa extrapolação foi removida. O passo de cada eixo agora não pode exceder a menor amplitude realmente percorrida nas duas direções e aprovada com sobreposição de pelo menos 68%. Cada eixo pode executar no máximo três ciclos piloto; cada ciclo reserva e contabiliza as duas fotografias necessárias para ida e volta.

O contrato persistido da grade é v3. Ele vincula, por digests SHA-256 de representações canônicas e finitas, o espaço absoluto normalizado, os limites, o alvo de sobreposição, os pontos do plano, a geometria da grade, os passos piloto e a evidência óptica. Na retomada, o contrato é recalculado e comparado antes da aquisição de controle. Plano legado, malformado ou alterado é recusado sem movimento.

Fechamento do piloto exige correspondência verificada, pelo menos 85% de sobreposição com a referência e deslocamento de até 3 pixels. Quando a posição absoluta não demonstra essa repetibilidade, nenhum plano absoluto é publicado. O scanner persiste a mudança para movimento incremental e escolhe `ContinuousMove` com velocidade segura ou `RelativeMove` com deslocamento, conforme as capacidades descobertas. A modalidade é preservada na retomada, que não retorna aos pilotos absolutos. Interrupções durante piloto ou durante essa mudança são tratadas como estados persistidos; uma intenção incerta não autoriza repetir o comando.

O retorno passou a usar epochs persistidos. Cada novo fluxo de movimento confirmado recebe uma identidade antes do primeiro comando; uma retomada do mesmo fluxo somente observa, e um fluxo posterior pode ter um novo recall. As quatro correções finas continuam sendo um orçamento global do trabalho. Correções absolutas usam apenas eixos cujo piloto fechou; um residual em tilt não pode autorizar uma sondagem de tilt com evidência apenas de pan.

A publicação agora avalia o payload bruto da qualidade antes da sanitização. Somente `quality.status: ready` com `quality.reasons` sendo uma lista vazia recebe `quality_approved: true`. Qualidade ausente, malformada, reprovada ou em revisão fica como candidata, inclusive quando ainda não existe panorâmica ativa. Uma aquisição parcial pode tornar-se ativa apenas quando sua reconstrução é aprovada e ela não substitui uma panorâmica completa. Conflito de revisão não expõe um artefato órfão impossível de recortar.

A regra é prospectiva. O ponteiro histórico da Garagem não foi reclassificado: ele continua visível como ativo para inspeção, recorte e download, mas sua qualidade `review` o torna inelegível para novo mapeamento.

## Retorno posterior à captura 5

O retorno explícito final comparou a imagem parada com a referência e encontrou **5,081 pixels** de deslocamento, **98,82%** de sobreposição e 397 inliers. A câmera terminou em estado `stopped`, com `can_return: true`. O limite estrito de 3 pixels continuou reprovado.

Os quatro comandos finos já consumidos pelo trabalho não foram renovados por essa chamada. `can_return` pode continuar verdadeiro para permitir uma nova observação ou concluir trabalho seguro, mas não concede outro recall na mesma época nem abre uma nova série de correções. Isso evita movimento ilimitado diante de zona morta, quantização ou resposta óptica inconsistente.

## Capturas 6 e 7: isolamento da falha no pulso de 1,2 segundo

As capturas 6 e 7 executaram o fluxo normal do produto na mesma câmera e fonte da Garagem. Ambas abandonaram o plano absoluto porque o ciclo piloto não fechou visualmente e usaram movimento contínuo. Elas preservaram mosaicos pequenos e coerentes, mas não percorreram uma extremidade horizontal nem iniciaram tilt.

| Ensaio | Trabalho e artefato | Fotografias usadas / omitidas | Holdout p95 | Cobertura por pixels / ângulo sólido | Estado físico final |
| --- | --- | ---: | ---: | ---: | --- |
| Captura 6 | `a6af12c3347040fbbaf664a8c12ee5f2` / `4477ac9a919b4559bc4cdabc4818a0ec` | 4 / 0 | 1,581 px / 0,128° | 7,308% / 9,906% | `stopped` |
| Captura 7 | `6ff83f849ab547d6ad3223204b941cdd` / `15c6e0f4ea2a42378aca01702f232d4b` | 6 / 0 | 1,446 px / 0,110° | 8,149% / 10,983% | `restored` |

Na captura 6, os pulsos contínuos de 0,3 segundo foram aceitos com timeout de proteção de 1 segundo. O primeiro pulso de 1,2 segundo não produziu recibo de comando; o Stop de limpeza também não foi confirmado naquele movimento. O sistema conservou quatro fotografias e terminou o trabalho como parcial. O retorno absoluto posterior foi aceito, mas não apresentou uma transição visual causal; uma única correção de pan de 80 ms aumentou o erro de aproximadamente 21,750 para **32,650 pixels**, com 96,26% de sobreposição. O resultado reprova o gate de 3 pixels. O trabalho ainda aparecia como retomável no contrato público daquele código, embora a transição pendente não trouxesse uma intenção causal completa.

Depois desse ensaio, a persistência da transição passou a incluir a identidade da instância de captura, geração e sequência do quadro-base. Falhas de PTZ passaram a conservar uma categoria sanitizada, e cada passo ganhou no máximo uma repetição delimitada. Uma intenção pendente ou ambígua deixou de autorizar repetição cega do comando; a retomada precisa reconciliar o estado pela observação.

Na captura 7, pulsos de 0,3 e 0,6 segundo foram aceitos, cada qual com timeout de proteção de 1 segundo. Duas tentativas de 1,2 segundo foram recusadas; ambas ficaram classificadas como `http_error` no estágio `device_response`, sem conteúdo privado da resposta. Entre elas, a repetição delimitada com a última duração comprovada de 0,6 segundo foi aceita e observada. Todos os nove Stops do ensaio foram confirmados. O retorno absoluto terminou em **1,138 pixel**, com 99,53% de sobreposição, dentro do gate óptico; isso comprova somente aquele retorno. A captura terminou parcial, com seis fotografias, sem uma extremidade comprovada. A retomada foi recusada como `continuous_resume_unavailable`, porque o trabalho preservava um percurso contínuo anterior ao contrato atual.

Os dois erros de 1,2 segundo, localizados na resposta do dispositivo enquanto durações menores funcionavam, sustentaram a hipótese operacional de incompatibilidade daquele firmware com o timeout fracionário enviado como proteção. Eles não provam isoladamente a causa interna do firmware nem autorizam uma regra específica para a câmera.

## Timeout de proteção ONVIF compatível

A correção foi feita no adaptador ONVIF, sem alterar a duração lógica do pulso. O controlador continua encerrando o movimento no prazo exato e envia Stop de forma independente. O campo opcional `Timeout` do `ContinuousMove` serve como proteção adicional no dispositivo:

- a duração pedida é limitada ao intervalo anunciado pelo perfil e ao teto de segurança de 30 segundos;
- quando o arredondamento para cima cabe nesse intervalo, o timeout de proteção prefere segundos inteiros: por exemplo, 0,6 vira `PT1S` e 1,2 vira `PT2S`;
- quando o inteiro seguinte excederia o máximo anunciado, a duração limitada é preservada; um intervalo de 0,1 a 1,5 segundo mantém 1,2 segundo;
- limites desconhecidos omitem o campo opcional, e valores não finitos ou não positivos são recusados;
- a serialização preserva decimais finitos necessários e evita arredondar para baixo ou emitir notação científica.

Essa política está em [`continuous_move_timeout`](../extensions/cameras/src/toposync_ext_cameras/onvif/client.py#L1135) e na montagem de [`ContinuousMove`](../extensions/cameras/src/toposync_ext_cameras/onvif/client.py#L882). Os casos de limites, serialização e separação entre watchdog e timeout do dispositivo estão em [`test_camera_onvif_panorama_capabilities.py`](../tests/test_camera_onvif_panorama_capabilities.py#L450).

## Captura 8: transporte corrigido e novo limite de observabilidade

A captura 8 (`5f9d75d35b914b22b09d5c8007a59fb0`) produziu o artefato `7dc4c4354b5042e891242ede7151f0d2`. Os **23 movimentos** foram aceitos e os **23 Stops** foram confirmados. Isso inclui **12 pulsos contínuos de 1,2 segundo**, todos enviados com timeout de proteção de 2 segundos. Os Stops desses pulsos foram solicitados pelo controlador entre aproximadamente 1,25 e 1,31 segundo e confirmados antes do timeout de proteção. Portanto, `PT2S` não prolongou o pulso lógico.

Esse ensaio elimina a falha de transporte observada na captura 7 para a Garagem: não houve `ptz_failure_code`, `http_error`, comando sem confirmação ou Stop sem confirmação. Ele comprova a compatibilidade do formato corrigido com este dispositivo e este perfil. Não comprova que todo firmware ONVIF exige segundos inteiros nem que todas as câmeras aceitam a mesma política; essa generalização continua apoiada por limites publicados, comportamento conservador e testes automatizados.

O scanner guardou **17 fotografias**. Todas foram usadas, nenhuma foi omitida e o grafo final teve um componente com **105 pares verificados**. O artefato recebeu `quality.status: ready`, razões vazias e `quality_approved: true`.

| Medida | Resultado |
| --- | ---: |
| Fotografias usadas / omitidas | 17 / 0 |
| Pares geométricos verificados | 105 |
| Cobertura por pixels | 16,4019% |
| Cobertura por ângulo sólido | 22,2244% |
| Holdout p95 | 1,779 pixels / 0,138° |
| Estado do otimizador | Sucesso |
| Estado de posicionamento | `not_validated` |

A sequência horizontal percorreu primeiro um lado e depois recuperou o centro para percorrer o outro. No lado de pan negativo, a última fotografia aceita estava em pan normalizado aproximado de −0,650794; o próximo enquadramento estável, em −0,726190, não pôde ser ligado ao anterior com pontos suficientemente distribuídos. No lado positivo, a última fotografia aceita estava em aproximadamente 0,210317; o enquadramento seguinte, em 0,301587, falhou pelo mesmo critério. As imagens residuais mostram pouca textura na parede lisa de um lado e padrão repetitivo nas ripas do outro. A falha dominante foi `correspondences_not_distributed`: houve pontos locais, mas não suporte espacial suficiente para provar uma transformação global.

O sistema não interpretou essas falhas como limites mecânicos. `boundaries` permaneceu vazio, as duas extremidades da banda horizontal ficaram `unconfirmed`, nenhuma banda foi concluída, duas regiões ficaram pendentes e o tilt não começou. O trabalho terminou `partial`, `can_resume: false` e `resume_unavailable_code: relocalization_required`. Isso é falha segura: as fotografias conectadas foram preservadas sem inventar continuidade, limite físico ou cobertura vertical.

O retorno começou com erro de **106,986 pixels**. O recall absoluto reduziu o erro para 31,611 pixels. As quatro correções compartilhadas produziram a sequência 31,611 → 35,523 → 36,781 → 36,145 → **16,754 pixels**. A medição final teve 96,88% de sobreposição, 197 inliers distribuídos em 12 células e deslocamento de 8,362 pixels em X e 11,301 pixels em Y. O último pulso respondeu 6,340 vezes o deslocamento previsto e cruzou o residual horizontal; por isso foi marcado `visual_response_inconsistent`. O orçamento acabou e a câmera permaneceu `stopped`, corretamente sem declarar restauração.

No inventário `inventory-1789206913.json`, o artefato da captura 8 é o ativo, o artefato da captura 7 é o anterior e não existe candidato. Isso significa que a reconstrução visual parcial foi aprovada pelo gate de qualidade e publicada. Não significa aquisição completa: o próprio artefato mantém `acquisition_complete: false`, extremidades não confirmadas e `positioning_status: not_validated`.

## Captura 9: meio passo executado e estado terminal ausente

A captura 9 (`4ad36fcbd4b8463da825725aca1b739e`) produziu o artefato `a6bef107861d43fc9c7ed40f53e27c7d`. O fluxo abandonou novamente o plano absoluto e iniciou a varredura contínua no sentido negativo de pan. Foram guardadas **11 fotografias**, todas usadas na reconstrução e nenhuma omitida. O lado positivo não foi percorrido, nenhuma extremidade ou banda foi confirmada e o tilt não começou.

| Medida | Resultado |
| --- | ---: |
| Fotografias usadas / omitidas | 11 / 0 |
| Pares geométricos verificados | 40 |
| Cobertura por pixels | 13,5779% |
| Cobertura por ângulo sólido | 18,5896% |
| Holdout p95 | 1,661 pixels / 0,130° |
| Estado do otimizador | Sucesso |
| Estado de posicionamento | `not_validated` |

Depois de vários passos de 1,2 segundo aceitos e conectados, o passo seguinte moveu e estabilizou a câmera, mas a comparação com a fotografia 11 falhou como `correspondences_not_distributed`. O scanner executou então o protocolo delimitado de recuperação:

1. confirmou o Stop;
2. retornou por posição absoluta ao enquadramento da fotografia 11, o último conectado;
3. confirmou visualmente esse enquadramento de âncora;
4. emitiu uma única repetição na mesma direção com duração reduzida de 1,2 para 0,6 segundo.

O comando de 0,6 segundo foi aceito, usou timeout de proteção de 1 segundo e teve Stop confirmado. A observação posterior chegou a uma comparação geométrica verificada de 47,075 pixels e 94,40% de sobreposição, mas o detector não observou uma transição temporal causal dentro da janela e encerrou o movimento como `motion_not_observed`. Sobre a parede lisa, a evidência espacial apareceu tarde demais para qualificar aquele quadro como fotografia conectada. O sistema recusou corretamente a fotografia e não interpretou o caso como limite mecânico ou ausência física de movimento.

### Defeito de estado revelado pelo ensaio

O movimento aceito e parado deveria ter encerrado a recuperação como uma falha observacional terminal. No código executado, a transição continuou `pending` e `connection_recovery.state` continuou `retry_pending`. O cursor já registrava o passo subdividido, mas não possuía uma identidade durável que ligasse aquela transição ao diagnóstico exato do movimento. A rotina de recuperação, portanto, não conseguia distinguir com prova suficiente o diagnóstico atual de uma tentativa anterior e recusava avançar.

O efeito público foi `continuous_resume_unavailable`, com `can_resume: false`, em vez de arquivar a recuperação como falha confirmada, marcar a extremidade esquerda como não confirmada e tentar o lado oposto. O trabalho terminou `partial` e fisicamente `restored`, mas a cobertura registrada ficou somente no lado negativo. Esse código de retomada descreve o defeito do estado histórico; não demonstra incompatibilidade do dispositivo com movimento contínuo.

### Retorno e reconstrução

O retorno absoluto terminou inicialmente em 33,192 pixels. Uma correção de pan de 80 ms reduziu o erro para **1,897 pixel**, com 99,33% de sobreposição e 168 inliers distribuídos em 12 células. O trabalho terminou `restored`, dentro do gate óptico de 3 pixels. Como nos retornos anteriores, esse resultado aprova somente este ciclo e não estabelece repetibilidade do atuador.

A reconstrução recebeu `quality.status: ready`, razões vazias e `quality_approved: true`. Seu holdout p95 de 1,661 pixels é geometricamente limpo, mas a cobertura é menor e unilateral. No inventário `inventory-1789209631.json`, o artefato da captura 9 aparece como ativo, a captura 8 foi deslocada para anterior e não existe candidato. A cobertura por pixels caiu de 16,4019% para 13,5779%, a cobertura por ângulo sólido caiu de 22,2244% para 18,5896% e o número de fotografias caiu de 17 para 11.

Isso revela uma regressão no critério de publicação: entre dois artefatos parciais aprovados geometricamente, o fluxo promoveu o mais novo sem exigir que ele preservasse ou ampliasse a cobertura útil do ativo. Qualidade do ajuste não implica dominância de cobertura. O inventário histórico prova a troca do ponteiro; não prova que a captura 9 seja melhor para navegação ou mapeamento.

### Correções prospectivas depois da captura 9

Cada tentativa física passou a receber um `movement_id` canônico e persistido tanto na transição quanto no respectivo diagnóstico. Uma falha de observação do meio passo só pode ser terminalizada quando existe exatamente um diagnóstico com a mesma identidade, comando aceito, Stop confirmado, intenção, âncora, eixo, direção, linha, passo e duração coerentes. Um diagnóstico antigo ou uma identidade malformada não pode encerrar a tentativa atual.

Quando esse conjunto de evidências confirma `motion_not_observed` ou `stability_timeout`, a transição passa a `accepted` com resultado `halfstep_observation_failed`; a recuperação passa a `failed` com causa específica. Esse estado terminal não volta a ser interpretado como pulso pendente. A recuperação pode então arquivar o registro, manter a extremidade como não confirmada e seguir somente por uma rota já autorizada pelo scanner. Sem referência de trabalho suficiente, o resultado público é `relocalization_required`, preservando o comportamento sem repetir o movimento.

O gate de interrupção continua fechado: uma queda durante o retorno à âncora ou durante o meio passo conserva `return_pending` ou `retry_pending` e não autoriza repetir, arquivar nem contornar o comando ambíguo. Retomada malformada, adulterada ou sem correspondência causal continua `continuous_resume_unavailable`. Testes de unidade cobrem o estado terminal aceito, a rejeição de diagnóstico com outro `movement_id`, o arquivamento seguro, o bloqueio de repetição e as duas fronteiras de interrupção. Essas correções ainda não têm uma nova execução física registrada neste documento.

## Interface, mapeamento e retomada

A interface continua separando captura, montagem e resultado. Ativo e candidato aparecem como estados distintos, qualidade em revisão não completa falsamente os marcos do wizard e um retorno não confirmado permanece visível como estado físico próprio. O comportamento histórico da captura 5 não foi regravado durante essas alterações.

A implementação não acrescenta campos de geometria para o usuário nem exceções por identificador das câmeras ensaiadas. Posição absoluta, movimento relativo e movimento contínuo continuam modalidades descobertas em tempo de execução, com seus respectivos espaços e limites. Espaços proprietários são preservados, mas não recebem interpretação normalizada inventada.

O mapeamento aceita somente o artefato apontado por `metadata.panorama.active`. Um candidato é recusado mesmo quando sua qualidade é limpa. O ativo também precisa apresentar `quality.status: ready`, razões vazias e, quando o campo existir, `quality_approved: true`. Um ativo legado sem esse campo é aceito somente com qualidade explicitamente limpa. Um ativo inválido nunca recorre ao candidato como fallback. Criação, retomada e apontamento revalidam essa relação.

Um coordenador compartilhado por câmera e fonte fecha a janela entre essa validação e o movimento. A operação visual fixa a referência ativa antes de reservar a câmera e a mantém durante localização, pulsos, captura e Stop. A publicação usa o mesmo coordenador antes de trocar o ponteiro: se ela vence, o apontamento revalida e termina sem comando; se o apontamento vence, a publicação aguarda a conclusão. Exceção e cancelamento liberam o bloqueio, e fontes diferentes permanecem independentes.

A interface não deduz retomada apenas pelo estado do trabalho. O servidor recalcula `can_resume` a partir do cursor, orçamento, referências e evidência persistida. Estados piloto como `planned`, `outward_observed`, `stationary_no_op` ou `cycle_unconfirmed` não exibem uma ação que falharia imediatamente; a interface mostra uma explicação localizada e orienta conferir a imagem ao vivo e gerar uma nova panorâmica. Quando a retomada é segura, a ação aparece como **Retomar** ou **Tentar completar captura**. Com ao menos duas fotografias utilizáveis, **Montar com as fotografias guardadas** continua disponível sem movimento.

## Validação automatizada

Antes das correções finais de contrato, uma rodada consolidada havia passado 842 testes Python, 18 testes Node e 27 cenários Playwright, além de Ruff, typecheck e builds. Depois dos gates de qualidade e retomada, do fallback relativo, do contrato v3 e do coordenador de referência, uma execução intermediária passou **795 testes Python** nos 15 módulos de panorâmica, mapeamento e configuração; **18 testes Node** de recorte, projeção e traduções; **40 cenários Playwright** da origem e do wizard de mapeamento; Ruff; `typecheck:plugin-api`; os builds da interface da extensão, do frontend e da documentação. A contagem Python final será registrada depois do último ajuste estrutural da retomada.

Isso comprova os contratos exercitados e o comportamento histórico da Garagem observado na captura 5. Ainda não comprova fisicamente o planejador v3, o fallback relativo, a política prospectiva de promoção, compatibilidade universal, precisão em todas as poses ou resistência a todo firmware.

Depois dos contratos finais de retomada, publicação e coordenação, a rodada ampla mais recente antes da correção de timeout passou **927 testes Python** e **18 testes Node**, além de Ruff, `git diff --check`, `typecheck:plugin-api` e os builds da extensão, do frontend e da documentação. Depois da correção de timeout ONVIF, a seleção focada de cliente ONVIF e controlador PTZ passou **129 testes**; Ruff e `git diff --check` também passaram. A captura 8 é a validação física preservada dessa correção no dispositivo da Garagem.

Essas contagens precedem a captura 9 e as correções de `movement_id` e estado terminal descritas acima; os testes novos ainda não foram incorporados a uma contagem consolidada neste documento. Testes determinísticos verificam o contrato de segurança e de persistência; somente o ensaio físico mede a resposta mecânica, a textura disponível e a cobertura realmente alcançada.

## Pendências objetivas

1. **Retorno óptico:** repetir ciclos independentes e caracterizar por eixo, sentido e região a distribuição do erro, da resposta mínima e da deriva. A tentativa 4 teve dois retornos aprovados; a captura 5 terminou em 5,081 pixels.
2. **Captura:** executar uma nova captura normal e verificar se o planejador óptico reduz as 44 fotografias desconectadas observadas anteriormente, sem conectar imagens por proximidade presumida.
3. **Cobertura:** completar as bandas que faltaram dentro de um orçamento adaptado à resposta real. Confirmar visualmente a continuidade da rua e do chão; porcentagem global sozinha não prova cobertura sem lacunas na área útil.
4. **Apontamento:** validar alvos físicos independentes em regiões distintas da panorâmica. Readback ONVIF, erro do mosaico e retorno à referência não substituem esse ensaio.
5. **Diversidade:** executar a mesma matriz em outras marcas e modalidades antes de ampliar alegações de compatibilidade. Os quatro dispositivos disponíveis constituem amostra de desenvolvimento, não certificação universal.
6. **Estado de referência:** fazer novo ensaio físico para confirmar que `reference_unconfirmed` persiste sua comparação e revoga `restored` na execução real, embora os contratos automatizados já cubram ambos.
7. **Dado histórico:** decidir se o ativo `review` da Garagem será mantido somente como evidência ou migrado explicitamente; isso é uma decisão sobre os dados existentes, não uma lacuna na política nova.

As capturas 6 a 9 avançaram parcialmente os itens 1 e 2: provaram o transporte corrigido e produziram componentes sem fotografias omitidas. A captura 9 também executou o retorno à âncora e o único meio passo previsto para uma ligação sem distribuição espacial. O movimento menor permaneceu sem confirmação temporal sobre a parede lisa, e o defeito de estado impediu o percurso pelo lado oposto. As correções posteriores permitem arquivar essa falha de modo causal e prosseguir por uma rota segura, mas ainda carecem de comprovação física. O gate geométrico não foi reduzido para transformar parede lisa ou padrão repetitivo em continuidade presumida.

A captura 8 não resolveu o item 1: o retorno final de 16,754 pixels mostra resposta não linear ou dependente de região nos pulsos curtos. A captura 9 aprovou um retorno isolado em 1,897 pixel, assim como a captura 7 havia aprovado um retorno em 1,138 pixel; as capturas 6 e 8 reprovaram. Essa dispersão ainda impede uma alegação de repetibilidade. Também permanecem os itens 3 a 5: faltam cobertura vertical, apontamento por alvos independentes e diversidade física de fabricantes. A regressão de publicação entre as capturas 8 e 9 acrescenta uma pendência: um artefato parcial novo não deve substituir o ativo somente por ter ajuste geométrico limpo quando sua cobertura comprovada é menor.

Não há declaração de resolução total. A execução demonstra que o Toposync consegue capturar uma varredura ampla e construir um mosaico parcial útil na Garagem sem abrir exceção específica para ela. Permanecem separados e pendentes o retorno óptico repetível abaixo de 3 pixels, a validação física do novo percurso, a conclusão da cobertura e o apontamento da panorâmica para a cena real.

## Evidências privadas principais

- `verify_control-garagem-3.json`, `return-garagem-3-1.json` e o diretório do trabalho `b6c026e6744b47298434f10bb2d9029d`;
- `verify_control-garagem-4.json` e o diretório do trabalho `4bd59343a3704bdd9b6ef80ab89b5e05`;
- `capture-garagem-4.json` e o diretório do trabalho `652e8bb3fdbb46e0bb48548f3f1c7bf2`;
- `capture-garagem-5.json`, `return-capture-garagem-5-1.json`, o diretório do trabalho `4758ae63e1fe4e48b043ad7efc0bfcf1` e o artefato `016ea455eaef48059a6cf540d30865b2`;
- [resultado público da captura 6](../ignore/panorama-stabilization-20260911/capture-garagem-6.json), [manifest do trabalho 6](../.toposync-data/runtime/cameras/source-panorama/jobs/a6af12c3347040fbbaf664a8c12ee5f2/scan-manifest.json), [retorno 6](../.toposync-data/runtime/cameras/source-panorama/jobs/a6af12c3347040fbbaf664a8c12ee5f2/return-validation.json) e [panorama 6](../.toposync-data/runtime/cameras/source-panorama/artifacts/4477ac9a919b4559bc4cdabc4818a0ec/panorama.png);
- [resultado público da captura 7](../ignore/panorama-stabilization-20260911/capture-garagem-7.json), [manifest do trabalho 7](../.toposync-data/runtime/cameras/source-panorama/jobs/6ff83f849ab547d6ad3223204b941cdd/scan-manifest.json), [retorno 7](../.toposync-data/runtime/cameras/source-panorama/jobs/6ff83f849ab547d6ad3223204b941cdd/return-validation.json) e [panorama 7](../.toposync-data/runtime/cameras/source-panorama/artifacts/15c6e0f4ea2a42378aca01702f232d4b/panorama.png);
- [resultado público da captura 8](../ignore/panorama-stabilization-20260911/capture-garagem-8.json), [manifest do trabalho 8](../.toposync-data/runtime/cameras/source-panorama/jobs/5f9d75d35b914b22b09d5c8007a59fb0/scan-manifest.json), [retorno 8](../.toposync-data/runtime/cameras/source-panorama/jobs/5f9d75d35b914b22b09d5c8007a59fb0/return-validation.json), [panorama 8](../.toposync-data/runtime/cameras/source-panorama/artifacts/7dc4c4354b5042e891242ede7151f0d2/panorama.png) e [inventário posterior](../ignore/panorama-stabilization-20260911/inventory-1789206913.json);
- [resultado público da captura 9](../ignore/panorama-stabilization-20260911/capture-garagem-9.json), [manifest do trabalho 9](../.toposync-data/runtime/cameras/source-panorama/jobs/4ad36fcbd4b8463da825725aca1b739e/scan-manifest.json), [diagnósticos 9](../.toposync-data/runtime/cameras/source-panorama/jobs/4ad36fcbd4b8463da825725aca1b739e/scan-diagnostics.json), [retorno 9](../.toposync-data/runtime/cameras/source-panorama/jobs/4ad36fcbd4b8463da825725aca1b739e/return-validation.json), [panorama 9](../.toposync-data/runtime/cameras/source-panorama/artifacts/a6bef107861d43fc9c7ed40f53e27c7d/panorama.png) e [inventário posterior](../ignore/panorama-stabilization-20260911/inventory-1789209631.json);
- `inventory-1789184690.json` antes da captura 5 e `inventory-1789187224.json` depois da reconstrução e do retorno;
- `interpreted-results.json`, `coordinate-repair.json`, `reconciled-returns.json` e logs de testes em `ignore/panorama-stabilization-20260911/`;
- `implementation.diff` e `implementation-hashes.json` são snapshots de um recorte anterior. Os hashes já não representam todos os arquivos atuais e não devem ser usados para atestar a implementação final sem regeneração.
