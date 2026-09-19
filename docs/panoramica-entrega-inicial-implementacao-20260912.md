# Panorâmica: checkpoint de implementação da região inicial

Data: 12 de setembro de 2026. Autor: Mateus Calza.

**Estado: código implementado, validado em fixtures locais e ensaiado fisicamente; aceite amplo pendente.**
Em 12 de setembro, uma captura controlada da Frente Reolink produziu uma região inicial montada. As demais câmeras PTZ foram ensaiadas uma a uma, mas foram interrompidas antes da montagem por falta de evidência visual suficiente. Este documento não representa aprovação de alcance total ou precisão de apontamento.

Referências: plano auditado em `/Users/c/Downloads/toposync-plano-conclusao-panoramica-auditado.md` e revisão em [revisao-plano-conclusao-panoramica-20260912.md](revisao-plano-conclusao-panoramica-20260912.md).

## Entrega implementada

O serviço normal de panorâmica solicita uma política interna versionada `initial_region`. O scanner existente usa a mesma transmissão para observar e selecionar fotografias em sua resolução original, antes da redução analítica. O JPEG é uma nova codificação desse quadro, não uma cópia dos bytes comprimidos da câmera.

O objetivo contém seis vistas: duas alturas e três setores horizontais. A ordem visita a segunda altura imediatamente depois da primeira fotografia, avançando pelas colunas com alternância de altura. Uma lateral sem textura pode interromper o avanço, mas não impede a tentativa inicial de registrar a segunda altura. A referência provisória de retorno continua separada da primeira fotografia qualificada após movimento.

Cada vista precisa de estabilidade, correspondência distribuída, ligação com a vista anterior e deslocamento útil. Fotografias intermediárias podem ser guardadas como pontes, sem incrementar o contador de vistas do objetivo. Até três tentativas observadas por vista; resultado de comando incerto interrompe o percurso. A duração dos pulsos se ajusta pela sobreposição observada, mantendo o detector existente.

O sinal do deslocamento vertical orienta a tentativa para baixo; uma resposta visual oposta permite inverter o sentido uma vez. Isso não reconhece semanticamente o chão nem comprova que a área inferior desejada foi incluída. Essa confirmação pertence ao futuro aceite das fotografias.

## Contratos e limites

| Contrato | Implementação |
|---|---|
| Fotografia | Instância de captura, geração, sequência, horários disponíveis, origem, dimensões, evidência e SHA-256 persistidos |
| Quadro selecionado | Busca da identidade exata nos caminhos normal e de endpoint tardio; ausência recusa a captura, sem substituição silenciosa |
| Imagens | Até 12 fotografias aceitas, 24 MiB por arquivo e 288 MiB de entrada; limite conferido antes da publicação do JPEG |
| Movimentos | Até 18 comandos de movimento, incluindo sondagens e retorno; três reservados para retorno; intenção persistida antes do envio |
| Tempo | 240 segundos ativos acumulados, incluindo retorno; 36 segundos reservados para retorno |
| Fila e montagem | Até 30 segundos em cada fila e 180 segundos de montagem, respeitando limites existentes mais restritivos |
| Parada | Stop e liberação continuam no fluxo de limpeza existente; não são descartados por esgotar o orçamento de movimento |
| Retomada | Exige política compatível, âncora observada, comandos com resultado conhecido e orçamento restante; reconfirma a imagem antes de prosseguir |
| Montagem | Reutiliza processo isolado, cancelamento e encerramento existentes; exige participação das seis vistas para aprovar a região |
| Qualidade | Mantém os critérios existentes; vista ausente acrescenta `required_region_missing` e impede aprovação |
| Persistência | Região inicial permanece `partial` quanto ao alcance total; `region_status` distingue `ready`, `review` e `incomplete` |

Os tetos de tempo governam a operação; limpeza e confirmação de parada permanecem necessárias depois do vencimento. Não são promessa de duração exata de rede ou hardware. Reiniciar ou retomar não renova os limites consumidos.

## Interface e compatibilidade

Português e inglês apresentam o objetivo inicial, contador de vistas e a distinção entre região pronta e alcance total não determinado. O recorte continua usando o editor, persistência, controle de revisão e URLs com suporte a ingress existentes. Visualizar, baixar e recortar não iniciam movimentos.

Trabalhos antigos mantêm seu percurso e leitura. A política nova é persistida por trabalho; não migra cursores anteriores. A configuração interna do serviço aceita `acquisition_policy=None` para integração legada ou rollback deliberado de novas capturas; isso não altera trabalhos já existentes. Os testes legados indicam essa opção explicitamente.

Esta entrega usa capacidades de velocidade ou movimento relativo, com ambos os eixos disponíveis. Câmeras somente com movimento absoluto recebem um diagnóstico antes de adquirir controle. Cobertura de todas as capacidades, expansão ao alcance completo e contingências adicionais pertencem às próximas entregas do plano. Não há atlas, novo mapeamento ou mudança no contrato de precisão.

## E15

O instrumento revisado deixa de dividir novamente o deslocamento por resolução de entrada. Usa o tamanho analítico declarado pelo matcher e separa concordância entre pares verificados de pares rejeitados nos dois caminhos.

- Instrumento anterior preservado: `docs/experimentos-panoramica-ptz/2026-09-12-e15/comparator_scale_consistency-v1.py`.
- Instrumento revisado: `comparator_scale_consistency.py`, revisão 2.
- Saída futura padrão: `report-v2.json`, criada com exclusividade; não sobrescreve relatórios existentes.
- Nova execução registrará hashes do instrumento, módulo do matcher, manifesto e entradas, além da versão do OpenCV.

`report.json` e `manifest.json` históricos não foram reescritos. O instrumento revisado foi executado localmente sobre as fotografias preservadas e gravou `report-v2.json`: 15 pares verificados concordaram; um par foi rejeitado pelos dois caminhos. A conclusão `16 consistentes` exige essa distinção: ela inclui o par em que ambos recusaram a correspondência e não prova 16 correspondências válidas. A aritmética da revisão anterior não comprova retrospectivamente o módulo carregado no E15 histórico. E9P3 continua sem evidência de fotografias persistidas ou panorâmica integrada.

## Validação executada e pendências

Passaram: 229 testes locais de serviço/região, 153 de captura/navegação/retorno, 27 cenários de navegador, TypeScript estrito, Ruff e a compilação da interface de câmeras. A fixture do navegador recebeu apenas metadados sintéticos de cobertura já exigidos pelo serviço para que um resultado sintético pronto pudesse ser promovido; não houve alteração do fluxo de produto.

O conjunto amplo de scanner, montagem e estabilidade teve 360 aprovações e uma falha: `test_terminal_analysis_buffer_never_replaces_the_integral_qualified_capture`. Em uma imagem 2880×1620, o quadro integral escolhido pelo detector podia ser removido do buffer de memória antes de ser gravado. O contrato novo o recusava como `selected_capture_frame_unavailable`, em vez de gravar outro quadro. Isso protegia identidade, mas bloqueava a captura em uma situação de alta resolução. A correção posterior e sua validação limitada estão registradas abaixo.

O navegador validou responsividade em 375, 768 e 1440 pixels, tema, foco por teclado, texto ampliado, cancelamento, retorno, recorte, candidato, reconstrução offline, ingress e mensagens em ambos os idiomas. A fixture deliberadamente usa o percurso legado; os textos e estados específicos de `initial_region` tiveram checagem de tipos e contratos de API, mas ainda não possuem um cenário visual dedicado.

O build emite aviso de tamanho para chunks de 516 KiB e 670 KiB, acima do limite recomendado de 244 KiB. Não há medição anterior nesta execução para atribuir o tamanho à região inicial, mas é um risco de carregamento que deve entrar na próxima rodada de desempenho.

## Correção pontual e validação rápida

A falha foi reproduzida antes da alteração. O detector agora fornece `qualified_frames`, identidades dos quadros da janela aprovada em ordem de nitidez. Depois do descarte por limite de memória, o seletor compartilhado pelos caminhos normal e tardio escolhe o primeiro original disponível dessa lista, verificando instância e geração, e vincula `best_sequence` à fotografia efetiva. Representações analíticas não são elegíveis. Evidência antiga sem essa lista continua exigindo sua sequência exata; ausência de original elegível continua recusando a captura.

O quadro escolhido é mantido pela referência existente até a gravação, sem cópia adicional de imagem. Permanecem os critérios de estabilidade e o limite de 96 MiB do buffer compartilhado; esse limite não representa a memória total do processo.

Validação solicitada apenas rápida: **12 testes direcionados passaram em 15,03 segundos**, além do Ruff nos cinco arquivos Python envolvidos. Incluíram 2880×1620 e 3840×2160, gravação e resolução do JPEG, identidade entre evidência e fotografia, descarte do candidato preferido, ausência de candidato elegível, rejeição de outro decodificador/geração e de representação analítica, recuperação tardia com somente o último original retido e rejeição de movimento/parada/observação inválidos. Não houve execução da suíte completa, navegador, montagem integrada nem ensaio em câmera real nesta correção.

Para a próxima validação extensa:

1. Repetir os conjuntos completos de scanner, estabilidade, região e serviço para verificar regressões além dos 12 testes direcionados desta correção.
2. Adicionar cenário visual de `initial_region`, incluindo contador, estado pronto, área incompleta, qualidade reprovada, seleção e recarregamento do recorte.
3. Medir o carregamento real dos chunks apontados pelo build e reduzir ou justificar o maior deles.
4. Só então realizar o ensaio físico delimitado: exigir fotografias realmente guardadas das seis vistas, diversidade bidimensional, área inferior visível, montagem aprovada, imagem apresentada e recorte persistido.

**Aceite físico continua pendente.** O código não constitui evidência de que qualquer câmera real concluiu esse novo fluxo.

## Validação completa de 12 de setembro

Depois da correção de retenção do quadro selecionado, a validação local completa foi repetida. Passaram **944 testes Python em 136,57 segundos**, cobrindo capacidade ONVIF, API, captura, localização, mapeamento, navegação, montagem, referência, região inicial, execução, scanner, estabilidade, mapeamento de raio no solo, fonte, âncora visual, propriedades de composição e atualização de configuração. Ruff passou nos arquivos Python da panorâmica. TypeScript estrito e a compilação da interface de câmeras também passaram.

O recorte possui **10 testes unitários aprovados**. A suíte visual da origem passou com **28 cenários**, incluindo o resultado de região inicial com seis vistas confirmadas e a mensagem explícita de que o alcance total ainda é indeterminado. A suíte visual de mapeamento passou com **13 cenários**. Quatro expectativas de teste foram alinhadas ao rótulo já presente na interface, `New capture`; não houve mudança de comportamento de produto. As verificações de whitespace dos arquivos alterados passaram.

O build mantém avisos de chunks de 516 KiB e 670 KiB acima da recomendação de 244 KiB. Esta execução não mediu a experiência de carregamento nem atribui o tamanho à entrega atual; o aviso permanece uma pendência de desempenho, não uma reprovação funcional.

### Gate físico e causa observada

A instância principal em 5174/8100 já estava em execução desde antes da correção de retenção do quadro. Ela não foi reiniciada nem substituída. Para `Frente Reolink` / `wide_main`, a saúde da fonte retornou `unreachable`, zero quadros e zero FPS. O último trabalho de confirmação de controle já estava encerrado como `visual_control_unverified`, sem fotografia aceita. Nenhum novo trabalho foi criado e nenhuma câmera foi movimentada nesta validação.

O log do MediaMTX registrou que a fonte RTSP de origem respondeu `401 Unauthorized`. Uma comparação local, sem imprimir credenciais, confirmou que endpoint, usuário e senha configurados para a fonte correspondem exatamente aos valores renderizados no MediaMTX. Portanto, a divergência não está entre a configuração persistida do Toposync e o processo de ingestão: o retransmissor RTSP de origem está recusando essa credencial, ou a credencial deixou de ser aceita por ele.

O aceite físico exige, nesta ordem: restaurar a autenticação da transmissão de origem; reiniciar controladamente a instância principal para carregar a correção atual; e executar uma única captura pelo fluxo normal do Toposync, exigindo seis fotografias reais da região inicial, diversidade bidimensional, montagem aprovada, apresentação da imagem, recorte persistido e retorno reportado pela própria operação. O ensaio a seguir registra exatamente quais desses gates foram satisfeitos e onde a evidência ainda é insuficiente.

## Ensaio físico controlado de 12 de setembro

O retransmissor da casa foi identificado como Frigate para a porta RTSP usada pelas câmeras. O par de credenciais das fontes era histórico e o Frigate o recusava. A credencial RTSP ativa foi lida no serviço autorizado, verificada diretamente na fonte sem expor seu conteúdo, aplicada pelo endpoint de configuração apenas às 15 origens que usam esse retransmissor, e confirmada por leitura. Foi preservado o backup `config.before-frigate-restream-auth-repair-20260912-2315.json`.

Também foram detectados automaticamente vínculos ONVIF ausentes: Térreo 1 tinha três perfis PTZ sem token e a fonte JPEG da Garagem também não tinha token. Cada vínculo foi aceito somente quando identificador, resolução e codec configurados correspondiam exatamente ao perfil reportado pela própria câmera. A atualização persistida possui o backup `config.before-onvif-profile-binding-repair-20260912-2322.json` e confirmação de leitura.

A instância foi reiniciada de modo controlado e as cinco câmeras PTZ foram processadas sequencialmente. Não houve tentativa concorrente ou repetição cega.

| Câmera | Fonte ensaiada | Fotografias aceitas | Resultado |
|---|---:|---:|---|
| Frente Reolink | `wide_main` | 10 | Região inicial pronta, seis de seis vistas qualificadas, montagem e recorte integral persistidos. A qualidade de montagem foi aprovada; o retorno automático não teve confirmação visual, por isso o artefato permanece `partial` quanto ao alcance e ao retorno. |
| Térreo 1 | `profile_3`, depois `profile_1` | 0, depois 1 | A secundária não forneceu referência; a parada de segurança enviada diretamente foi reconhecida. A principal confirmou uma vista e retornou, mas não confirmou uma nova região. Nenhum artefato foi publicado. |
| Corredor | `profile_1` | 0 | A referência e o primeiro deslocamento não tiveram confirmação visual suficiente. Nenhum artefato foi publicado. |
| Garagem | `profile_1` | 1 | Uma vista foi aceita e o retorno foi confirmado; o deslocamento seguinte foi recusado visualmente. Nenhum artefato foi publicado. |
| Quintal | `profile_1` | 1 | Uma vista foi aceita; a região seguinte e o retorno inicial não puderam ser confirmados. Nenhum artefato foi publicado. |

Os quatro resultados sem artefato são falhas seguras do critério de evidência, não panorâmicas incompletas apresentadas como válidas. A Frente Reolink é a única evidência física atual de que a nova seleção de quadro em alta resolução, as seis vistas da região inicial, a persistência, a montagem e o recorte funcionam juntos. A imagem dela está em `.toposync-data/panorama-results-20260912/frente-reolink.png`.
