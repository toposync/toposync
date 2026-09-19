# Plano de experimentos para panorâmica PTZ

**Estado:** experimentos concluídos ou encerrados com limite explícito em 12 de setembro de 2026. As evidências por experimento estão em `docs/experimentos-panoramica-ptz/2026-09-12-e*/`. A prova física de duas faixas foi concluída em E9P3; as validações integradas de produto permanecem descritas na seção 5.  
**Objetivo:** produzir evidência suficiente para um plano novo, sólido e verificável de panorâmica, mapeamento e controle PTZ no Toposync.

## Registro desta execução

| Experimento | Estado | Decisão baseada na evidência |
| --- | --- | --- |
| E1 | concluído | O replay real reconhece a transição terminal sem aceitar os controles negativos. |
| E2 | parcial, duas vistas horizontais confirmadas | Os perfis ONVIF exatos corresponderam visualmente em duas vistas quietas de pan (0,9930/0,971 px e 0,9945/0,588 px); o retorno novo foi confirmado em 0,095 px. Tilt, zoom, toda a faixa óptica e comportamento durante movimento continuam sem prova. |
| E3 | concluído; correção aplicada | O relay configurado falhava antes do rebind. A associação explícita aos perfis ONVIF foi aplicada com readback; os dois perfis agora entregam quadros decoder-fresh pelo hub compartilhado, a 249–350 ms já aquecido. |
| E4 | concluído; validado também na configuração real | O hub agora eleva o decoder para a maior frequência solicitada sem invalidar os consumidores já conectados. A captura configurada real confirmou 5→12 fps no mesmo proxy, com ambos os consumidores recebendo quadros frescos. |
| E5 | concluído, com escopo limitado | A redução offline para 640×360 preservou as conexões válidas; E2 confirmou o par nativo em duas vistas horizontais e E5R observou o profile_2 continuamente por um pan e retorno (86 frames, lacuna máxima 0,258 s). A evidência vale para Garagem/profile_2 nesse percurso, não para toda câmera ou marca. |
| E6 | concluído com limite de atribuição declarado | A janela quieta confirmou uma volta por preset a 0,081 px. E6T eliminou o falso terminal de E9P2: o controle encerrou o pulso de tilt localmente em cerca de 0,3 s, mas a primeira mudança visual surgiu 3,645 s depois do envio e a volta absoluta foi confirmada a 0,689 px. Em E9P3, o mesmo eixo só atingiu a vista terminal após 6,297 s. O experimento separa aceite/transporte local de observação visual, mas sem timestamp de exposição não atribui o atraso remanescente ao motor, encoder ou transporte. |
| E7 | concluído com limitação declarada; ajuste de direção validado e retorno ainda não qualificado | Pan positivo, pan negativo e tilt positivo responderam opticamente. Presets recém-criados recuperaram dois percursos novos de pan positivo a 0,607, 0,773 e 0,244 px, mas retornos anteriores estabilizaram 30,813 px após pan negativo e 53,152 px após tilt positivo. Em E7R6, a nova sonda inversa escolheu corretamente pan negativo e o comparador canônico reduziu 26,174 para 22,555 px; o refinamento seguinte foi inconsistente, parou em segurança e a vista final ficou a 15,028 px. A leitura de 3,830 px do adaptador foi invalidada por E15 por pré-processar o frame de maneira diferente. Em E7A1, uma volta absoluta com as mesmas coordenadas lidas ficou a 14,761 px; em E7A2, outro percurso voltou a 0,126 px. Coordenada ONVIF e um único retorno não qualificam o resultado: somente a âncora visual pode aceitar uma volta. |
| E8 | concluído | Posição observada pode continuar localmente sem conceder permissão de retorno exato. |
| E9 | concluído para a região física limitada | E9P1 verificou as três ligações, mas não a volta final; E9P2 voltou a 0,504 px, mas aceitou uma janela anterior ao movimento de tilt. E9P3 aguardou partida visual e estabilização após cada pulso e, na mesma execução, verificou `baseline→pan` (26,872 px), `baseline→tilt` (5,223 px) e `tilt→pan` (27,925 px). As voltas após cada faixa ficaram em 0,501 e 0,685 px. Isso qualifica o planejador para a região pequena, não para a faixa total de toda câmera. |
| E10 | concluído | A única lacuna coincide com uma descontinuidade de pan; não há motivo para E13 neste conjunto. |
| E11 | concluído; mitigação aplicada | Sem um referencial de cobertura alinhável, a nova panorâmica fica como candidata; a ativa não é promovida automaticamente. |
| E12 | concluído em simulador | As falhas previstas encerram em estados finitos e persistidos. |
| E13 | não aplicável | E10 não encontrou entrada suficiente para atribuir a falha ao montador. |
| E14 | concluído, associação estática validada | Três pares alto/baixo paralelos foram aceitos por correspondência visual e 18–69 ms de diferença temporal. Um controle visualmente semelhante com 2,10 s foi recusado. A regra deve ser aplicada pela aquisição integrada. |
| E15 | concluído; divergência de comparador corrigida no adaptador | Em 16 pares adjacentes reais, apenas o controle idêntico concordou entre o caminho canônico do controlador e a pré-redução colorida do adaptador. O adaptador agora entrega os frames originais ao comparador do controlador; os dois passam pela mesma normalização. |

## 1. Decisões que os experimentos precisam destravar

Antes de ampliar a implementação, este plano precisa responder:

1. Qual fonte de vídeo oferece informação visual suficiente com o menor custo operacional?
2. O observador consegue reconhecer uma transição quando o rastreamento intermediário se perde?
3. Uma vista reconhecida, mas deslocada da âncora, pode virar referência de continuação sem ser confundida com retorno exato?
4. Qual percurso obtém continuidade horizontal e vertical sem depender de uma extremidade difícil?
5. Qual precisão de apontamento e retorno foi realmente demonstrada pelo atuador?
6. Como preservar cobertura e resultados parciais sem promover uma regressão?

Essas decisões são independentes. Uma reconstrução visualmente boa não aprova retorno. Um retorno bom não prova cobertura. Um stream de baixa resolução mais barato não é útil se perder observabilidade.

## 2. Regras da bancada experimental

Os experimentos reutilizam os componentes do Toposync. Eles não criam um segundo controlador nem uma segunda matemática de controle.

Cada execução terá um manifesto com:

- hipótese e resultado que a confirmaria ou refutaria;
- versão do código e hashes dos insumos;
- câmera, lente, fonte, perfil ONVIF e estado óptico;
- orçamento de tempo, quantidade máxima de movimentos e condição de encerramento;
- saídas, métricas e decisão permitida pelo resultado.

O registro deve separar três fatos que hoje podem se misturar:

```text
quadro decodificado → quadro publicado → quadro consumido pelo algoritmo
```

Para cada quadro consumido, registrar instância de captura, geração, sequência, dimensões, horário de recebimento, horário de publicação, tempo de mídia quando disponível e origem do stream. Para cada movimento, registrar intenção, pré-condição, envio, resposta, Stop, estado físico e efeito visual observado.

Replays servem para testar decisões sobre uma sequência que já aconteceu. Eles não provam o resultado de um comando diferente. Percursos alternativos precisam primeiro de simulador independente e depois de ensaio físico delimitado.

## 3. Experimentos decisivos

### E1 — Reproduzir a transição perdida no ensaio 10

**Pergunta:** o observador reconhece a chegada a uma nova vista quando o rastreamento perde suporte durante o movimento, mas a vista terminal é geometricamente reconhecível?

**Entrada:** replay rejeitado do ensaio 10 da Garagem, com os quadros, sequências, intervalos e eventos do comando gravados.

**Como realizar:** um adaptador de replay chama o detector de estabilidade real na mesma ordem temporal registrada. A comparação entre referência e vista terminal permanece disponível somente no momento em que teria ficado disponível na execução original.

**Controles negativos:** vídeo congelado; imagens repetidas; objeto dominante em movimento; mudança de geração do decoder; troca de lente; correspondência concentrada em uma região pequena.

**Observar:** perda de suporte; tentativa de recuperação; reconhecimento da transição; início da janela estável; decisão final; motivo de toda recusa.

**Passa quando:** reconhece a transição do replay sem aceitar nenhum controle negativo.

**Decisão:** manter ou redesenhar a recuperação da transição visual. Não requer câmera.

### E2 — Descobrir quais streams representam a mesma visão

**Pergunta:** stream principal e secundário correspondem à mesma lente, campo de visão e região do sensor?

**Chamadas previstas:** descoberta ONVIF por `GetProfiles`, leitura de configurações de fonte e encoder e obtenção de URI por `GetStreamUri`.

**Como realizar:** primeiro relacionar metadados de fonte física, bounds, resolução, encoder e perfil de controle. Depois capturar pares de imagens com a câmera parada em mais de uma direção e estimar a relação visual entre baixa e alta resolução. A transformação é ajustada em parte das vistas e verificada em vistas reservadas.

**Observar:** recorte; proporção; orientação; zoom; estabilização digital; OSD; distorção; transformação entre fontes; erro em vistas reservadas.

**Cautela:** ter a mesma câmera não prova equivalência. Wide e Zoom da Frente são lentes distintas. Um perfil secundário sem PTZ próprio pode continuar pertencendo ao mesmo atuador; observação e controle devem ser vinculados separadamente.

**Passa quando:** cada par é classificado como `equivalente`, `equivalente_com_transformacao`, `diferente` ou `inconclusivo` com evidência preservada.

**Decisão:** quais fontes podem exercer observação, prévia e refinamento em alta resolução.

### E3 — Comparar entrada compartilhada, conexão direta e captura pontual

**Pergunta:** qual caminho entrega quadros utilizáveis com menor custo, menor irregularidade e menor atraso observável?

**Como realizar:** com a câmera parada, abrir a mesma fonte por três condições controladas: serviço de captura compartilhado, conexão direta nativa e captura pontual quando disponível. Medir abertura inicial e operação contínua separadamente. Repetir a janela de observação se houver instabilidade.

**Observar:** tempo até a primeira imagem; frequência efetiva; distribuição de intervalos; duplicações; reinícios; CPU; memória; atrasos locais; identidade da fonte.

**Cautela:** a imagem que chega cedo pode representar uma exposição antiga. Um decoder previamente aquecido não pode ser comparado a outro que ainda está abrindo.

**Passa quando:** há uma escolha justificada por métrica e sem redução da verificação de frescor.

**Decisão:** caminho de aquisição padrão e condições do fallback direto.

### E4 — Verificar compartilhamento do decoder e frequência efetiva

**Pergunta:** leitores compartilhados preservam a frequência exigida pela panorâmica sem prejudicar outros consumidores?

**Como realizar:** primeiro em backend simulado: abrir consumidor de baixa frequência, abrir outro de frequência maior, inverter a ordem e liberar consumidores em ordens diferentes. Depois confirmar a condição aprovada em stream real sem movimento.

**Observar:** frequência efetiva por consumidor; número de conexões RTSP; renegociação da taxa; encerramento prematuro; vazamento de leases; impacto em consumidores existentes.

**Passa quando:** o contrato de compartilhamento escolhe explicitamente a frequência necessária ou recusa compartilhar quando não puder garanti-la.

**Decisão:** negociar frequência no hub, criar assinatura própria de observação ou manter conexões separadas em situações delimitadas.

### E5 — Medir observabilidade em baixa resolução

**Pergunta:** o stream leve preserva detalhes suficientes para detectar movimento, estabilização e conexão geométrica?

**Como realizar:** reprocessar fotografias existentes reduzidas offline e, em seguida, repetir a análise com o substream nativo. A redução offline isola resolução; o substream nativo mede também compressão, nitidez e processamento da câmera.

**Observar:** características detectadas; distribuição espacial; ligações corretas e incorretas; erro de registro; perda de rastreamento; custo de processamento; comportamento em parede lisa, padrão repetitivo e chão.

**Cautela:** reampliar 640 pixels para 960 não cria detalhe. Métricas em pixels precisam ser normalizadas pela dimensão de análise.

**Passa quando:** o stream selecionado atende aos mesmos gates de suporte e geometria sem aumento inaceitável de falsos positivos.

**Decisão:** selecionar automaticamente a menor fonte que permanece observável; não relaxar critérios para forçar a escolha de baixa resolução.

### E6 — Separar resposta mecânica, atraso de vídeo e estabilidade

**Pergunta:** em que fronteira surge a demora e quando a imagem passa a representar uma vista estável?

**Sequência:**

```text
controle exclusivo
→ referência estável
→ movimento limitado
→ envio e resposta registrados
→ Stop confirmado
→ observação contínua dos streams
→ mudança e estabilidade avaliadas
→ estado físico final registrado
```

**Como realizar:** usar o controlador existente, uma duração dentro das capacidades descobertas e uma variável por ensaio. O stream principal e o secundário são observados em paralelo apenas se E2 tiver qualificado a relação entre eles.

**Observar:** aceite do comando; primeira mudança visual; duração da mudança; chegada ao repouso; divergência entre streams; lacunas; mudança de geração; qualidade do quadro terminal.

**Cautela:** sem timestamp físico da exposição, o experimento mede a cadeia controle + transporte + decoder. Timestamps de streams distintos não são comparáveis diretamente sem referência temporal comum.

**Passa quando:** cada atraso é classificado como transporte, observação, comando ou inconclusivo, sem inferência além da evidência.

**Decisão:** política de espera, fonte para observação e necessidade de um adaptador de tempo de mídia.

### E7 — Caracterizar resposta local do atuador

**Pergunta:** os movimentos pequenos são previsíveis o bastante para aproximação e correção visual? Qual precisão foi demonstrada por eixo e sentido?

**Como realizar:** pequenos lotes por eixo, sentido e modalidade. Manter velocidade constante enquanto se mede duração; uma nova velocidade exige nova medição. A resposta é medida visualmente nos dois eixos, mesmo quando somente um foi comandado.

**Observar:** zona morta; salto mínimo; variância entre repetições; efeito de inversão; acoplamento pan/tilt; erro antes e depois da correção; readback auxiliar; retorno óptico.

**Cautela:** não reaproveitar uma resposta medida com uma velocidade para outra velocidade. Não confundir uma coordenada ONVIF com encoder óptico preciso.

**Passa quando:** há modelo local observável ou limitação declarada, ambos com intervalo de validade.

**Decisão:** preferência entre aproximação absoluta, relativa ou pulsos; limites de correção; operações que ainda não podem depender do atuador.

### E8 — Continuar a partir de uma âncora deslocada

**Pergunta:** uma vista reconhecida, mas distante da âncora anterior, pode virar referência segura de continuação?

**Como realizar:** aplicar os pares reais do ensaio 10 e exemplos sintéticos com transformações conhecidas. O protótipo de estado registra a nova vista, relações verificadas, suporte espacial e revisão geométrica. Não basta trocar o caminho de uma imagem.

**Observar:** erro de registro; ambiguidade; coerência com mais de um vizinho; comportamento após mudanças sucessivas de referência; preservação da referência original para retorno.

**Controles negativos:** portão e padrões repetitivos; suporte concentrado; lente diferente; transformações incompatíveis entre vizinhos.

**Passa quando:** a continuação aceita somente relações verificadas e mantém retorno exato como operação distinta.

**Decisão:** aprovar ou rejeitar o contrato de `posição observada`.

### E9 — Comparar percursos e obter cobertura bidimensional pequena

**Pergunta:** qual percurso obtém continuidade horizontal e vertical com custo previsível e recuperação finita?

**Como realizar:** comparar em simulador independente uma grade aproximada, passos limitados pela sobreposição observada e exploração de regiões pendentes a partir de vistas conectadas. A alternativa escolhida recebe confirmação física limitada: duas faixas de três vistas, com uma conexão vertical.

**Observar:** área nova por movimento; cobertura em pan e tilt; conectividade entre faixas; redundância; recuperações; tempo; motivos de encerramento.

**Cautela:** não iniciar pela varredura completa. Uma faixa horizontal boa não aprova continuidade vertical.

**Passa quando:** a região pequena forma grafo conectado, preserva todas as fotografias válidas e termina com estado físico conhecido.

**Decisão:** selecionar o planejador de cobertura antes de tentar os limites totais da câmera.

### E10 — Analisar textura difícil, chão próximo e hipótese geométrica

**Pergunta:** determinada lacuna vem de percurso ruim, baixa observabilidade ou hipótese geométrica inadequada?

**Como realizar:** reprocessar material já capturado por região: parede lisa, padrão repetitivo, elementos próximos, regiões distantes e chão. Avaliar correspondências, resíduos, omissões e concentração espacial.

**Observar:** erros sistemáticos por região; conexões falsas; componentes desconectados; regiões omitidas; parallax; correspondências concentradas em elementos fáceis.

**Passa quando:** cada falha recebe classificação de planejamento, observabilidade, geometria ou inconclusiva.

**Decisão:** melhorar percurso, registro ou reconhecer limite do modelo de rotação. Não trocar o montador sem essa evidência.

### E11 — Preservar e comparar cobertura entre execuções

**Pergunta:** uma panorâmica nova preserva ou amplia a área útil de uma panorâmica já ativa?

**Como realizar:** usar os ensaios 8, 9 e 10. Quando houver alinhamento visual confiável, comparar regiões preservadas, acrescentadas e perdidas. Quando não houver referencial comum, simular a decisão de manter ativa ou guardar candidata.

**Observar:** ganho em uma direção compensando perda em outra; diferença entre área visível e alcance físico; vínculo entre artefato, geometria e pontos já mapeados.

**Passa quando:** a política não promove automaticamente artefato com regressão bilateral, evidência ausente ou geometria incompatível.

**Decisão:** regra de promoção, candidato e continuidade de mapeamento.

**Pergunta:** cada interrupção possui uma saída finita, segura e compreensível?

**Como realizar:** injetar falhas no simulador antes do envio, durante a resposta, após Stop e durante persistência. Cobrir desconexão de vídeo, mudança de geração, cancelamento, perda de concessão e indisponibilidade do stream de alta.

**Invariantes:** intenção incerta nunca é repetida automaticamente; orçamento não se renova por restart; fotografias válidas sobrevivem; perda de controle bloqueia novos movimentos; cancelamento não movimenta a câmera para retornar escondidamente.

**Passa quando:** cada falha resulta em estado persistido, motivo específico e próximo passo finito.

**Decisão:** aprovar o contrato operacional antes de ampliar ensaios físicos.

## 4. Experimentos condicionais

### E13 — Comparar o montador atual com uma referência externa

Executar somente se E10 mostrar falha relevante na reconstrução com material de entrada adequado. Aplicar ao mesmo conjunto de fotografias o montador atual e um pipeline de referência, como o módulo de stitching do OpenCV.

Comparar fotos usadas e omitidas, conexões, cobertura, erro em observações reservadas, distorção e custo. Aparência agradável sozinha não decide. Se ambos falharem nas mesmas regiões por falta de informação, a troca de montador não se justifica.

### E14 — Associar baixa e alta resolução durante a aquisição

Executar somente depois de E2, E5 e E6. Uma vista leve estabilizada procura, numa janela limitada, a imagem de alta que corresponda visual e temporalmente. A associação retorna relação verificada ou recusa explícita; nunca seleciona apenas o quadro de horário mais próximo.

Comparar associações corretas e incorretas, atraso adicional, memória, detalhe ganho e falhas de alta resolução. A panorâmica leve deve permanecer válida se esse refinamento falhar.

### E15 — Garantir um único comparador entre ensaio e controlador

Executar antes de atribuir ao atuador qualquer diferença medida pelo adaptador
de ensaio. Reprocessar pares reais preservados pelo caminho canônico do
controlador e pelo caminho do adaptador, sem movimento de câmera. Comparar
verificação, direção do deslocamento e deslocamento normalizado; um controle de
imagem idêntica separa instabilidade do comparador de diferença física.

**Passa quando:** a origem da divergência está identificada, o adaptador usa a
transformação canônica e o replay do adaptador produz as mesmas métricas do
controlador para o mesmo par de frames.

**Decisão:** nenhuma medição de retorno de adaptador pode qualificar ou
desqualificar o controlador se pré-processar os pixels de maneira diferente.

## 5. Validações que pertencem à implementação integrada

Estas validações não bloqueiam o plano investigativo, mas impedem declarar o recurso concluído depois:

- apontamento e retorno para alvos reservados, distribuídos em pan e tilt, por diferentes direções de aproximação;
- compatibilidade entre panorâmica, recorte, localização visual, pontos da planta e revisão geométrica;
- estados de interface para sucesso, lacuna, refinamento indisponível, retorno pendente e resultado parcial;
- confirmação em câmeras que representam capacidade ou cena diferente, sem repetir a varredura completa em todas para cada mudança.

## 6. Ordem recomendada

| Etapa | Experimentos | Resultado necessário |
| --- | --- | --- |
| 1 | E1, E8, E11 e E12 | Contratos de observação, continuação, promoção e recuperação |
| 2 | E2, E3, E4 e E5 | Escolha de fonte e caminho de aquisição |
| 3 | E6, E7 e E15 | Política de tempo, comparador comum e limites observados do controle |
| 4 | E9 | Prova de continuidade horizontal e vertical em região pequena |
| 5 | E10, E13 e E14 quando aplicáveis | Diagnóstico geométrico e refinamento em alta resolução |

Uma mesma coleta curta pode alimentar E2, E3, E5 e E6. O material deve ser preservado e reavaliado offline, evitando repetir movimentos por uma pergunta que os dados existentes já respondem.

## 7. Condições antes de qualquer ensaio físico

Fixar antes de cada ensaio:

- câmera, lente e fonte participantes;
- hipótese única e variável alterada;
- quantidade máxima de comandos e duração total;
- condição de Stop e recuperação autorizada;
- dados que serão preservados;
- critério de êxito, reprovação e inconclusão;
- estado esperado quando o ensaio terminar.

Uma falha não justifica repetir o mesmo ensaio. A repetição só é válida com hipótese nova, variável controlada diferente ou reparo determinístico que o replay já tenha sustentado.

## 8. Referências internas

- [Dossiê técnico de panorâmica, mapeamento e calibração](dossie-panoramica-mapeamento-calibracao-20260911.md)
- [Plano de estabilização de panorâmica](plano-estabilizacao-panoramica-20260911.md)
- [Execução e validação de estabilização](panoramica-estabilizacao-validacao-20260911.md)
- [Plano integrado revisado](toposync-plano-resolucao-ponytail-revisado.md)

## 9. Referências externas

- [ONVIF Media Service Specification](https://www.onvif.org/specs/srv/media/ONVIF-Media-Service-Spec.pdf)
- [ONVIF PTZ Service Specification](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec.pdf)
- [RTP: A Transport Protocol for Real-Time Applications](https://www.rfc-editor.org/rfc/rfc3550.html)
- [Automatic Panoramic Image Stitching using Invariant Features](https://courses.cs.washington.edu/courses/csep576/21au/resources/brown_lowe_panorama_ijcv2007.pdf)
