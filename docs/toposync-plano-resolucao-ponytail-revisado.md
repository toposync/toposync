# Toposync: resolução integrada de panorâmica, mapeamento e PTZ

Autor: Mateus Calza. Revisão: **11 de setembro de 2026**. Método: **Ponytail full**.

**Estado:** plano com implementação aplicada em 11/09; **aceite físico integrado ainda pendente**. Resultados e lacunas estão no [relatório de implementação e validação](panoramica-validacao-integrada-20260911.md). As metas abaixo permanecem como critérios de aceite: captura, mapeamento, apontamento, retorno e UX necessária não saem do escopo por uma reprovação.

**Prioridade atual após o ensaio das quatro câmeras:** seguir o [plano de estabilização de panorâmica PTZ](plano-estabilizacao-panoramica-20260911.md). Ele corrige as limitações do protocolo diagnóstico e ordena a próxima execução; não substitui os objetivos integrados ainda pendentes.

Base: [plano recebido](/Users/c/Downloads/toposync-plano-resolucao-ponytail.md), [dossiê](/Users/c/Projects/toposync-2/docs/dossie-panoramica-mapeamento-calibracao-20260911.md), código local e evidências de 10/09. Esta versão é autossuficiente; não depende dos estudos e documentos opcionais citados no anexo, que não foram fornecidos.

## 1. Objetivo, aceite e limites

A pessoa escolhe a fonte, obtém sua área útil, associa lugares à planta e usa detecções mapeadas. Quando o controle permitir, aponta para um lugar e retorna com confirmação visual. Tudo pelo Toposync, sem ajustes manuais do motor pelo executor e sem campos geométricos obrigatórios.

Na Frente, cobertura-alvo inclui os dois lados úteis da rua e o chão necessário ao mapeamento. O Quintal permanece referência para cobertura inferior; a primeira validação desta entrega usa **somente a Frente**. Outras câmeras entram quando acrescentarem evidência de capacidades diferentes e estiverem no escopo autorizado da execução.

**Uma entrega não significa um patch monolítico.** Cada mudança precisa passar no seu teste antes da integração. Resultado parcial pode ser útil, mas não encerra um objetivo pendente. Ausência comprovada de capacidade pode limitar uma câmera; não transforma uma falha da Frente em aprovação da entrega.

Fora de escopo: SLAM, cena 3D geral, cinemática universal, calibração de toda a faixa de zoom, novos protocolos, atlas como serviço, journal genérico, infraestrutura nova de jobs ou redesenho amplo. NumPy, OpenCV, SciPy, controlador, revisões, fotos e componentes existentes são a primeira opção. Expansões indispensáveis exigem falha reproduzível da alternativa menor.

Não prometer alcance físico total a partir da máscara da imagem. Captura exaustiva continua sendo o objetivo de exploração; **aceite da região útil** e **certificação de alcance total** são resultados separados. Recortar não apaga lacunas nem permite excluir uma região difícil depois do ensaio.

## 2. Lacunas fechadas nesta revisão

| Lacuna do anexo | Decisão de desenvolvimento |
| --- | --- |
| “Corrigir o ponto compartilhado” sem separar produtores de frames | Rastrear tanto captura panorâmica quanto `CameraSourceRuntime`; transportar identidade e evidência do mesmo quadro até a detecção |
| “Reconhecer visualmente” sem definir saída | Localizador retorna rotação no referencial da panorâmica, vínculo ao quadro e suporte validado; não apenas identificador de vista semelhante |
| Remover bloqueio de motor poderia abrir caminhos inseguros | Separar validação do mapa, localização do quadro e habilitação de apontamento em todos os consumidores |
| Validação física depende de timestamps indisponíveis | Reusar prova visual temporal com tipo de evidência explícito; não falsificar o contrato de snapshot físico |
| Correção local não explica como alcançar alvo fora da vista | Usar aproximação existente; sem destino reproduzível, navegação visual limitada por sobreposições verificadas |
| Parâmetros e critérios adiados ao executor | Preservar limiares vigentes; definir abaixo critérios propostos para as operações novas e protocolo prévio de medição |
| “Continuação” pode esconder reconstrução de fotos antigas | Aceitar somente novas fotos qualificadas no mesmo job, sem renovação de orçamento nem replay de movimento incerto |
| UI poderia permitir pontos e bloquear somente ao final | Mostrar possibilidades antes da associação; salvar mapa e verificar apontamento são permissões distintas |
| Alteração de reconstrução pode deslocar pontos antigos | Fixar artefato/geometry revision; recorte não muda geometria; nova montagem não reaproveita pontos silenciosamente |

### Constatações do checkout

- `panorama.py` bloqueia fontes automáticas em `_target`, `activate` e `_validated`; `get_active`, `aim` e restauração dependem desse contrato. Corrigir apenas o botão ou uma condição é insuficiente.
- `CameraMappingRuntime._process_panorama_packet` exige `profile` com posições/ângulos e pan/tilt/zoom completos. Ainda não consome localização visual de fonte automática.
- `CameraSourceRuntime` recebe um frame e depois consulta o estado PTZ. A igualdade de épocas locais, isoladamente, não vincula exposição de vídeo atrasado à pose consultada.
- `panorama.py._frame` exige snapshot com evidência física e timestamp posterior à solicitação. Os backends OpenCV/FFmpeg atuais não fornecem essa prova de exposição.
- A reconstrução já estima rotações por raios/SVD em `_initial_rotations`. As relações de sobreposição são calculadas, mas descartadas antes da publicação; não presumir que estejam persistidas.

Esses fatos sustentam o desenho abaixo. Não demonstram a causa final de `motion_not_observed`, que ainda exige sequência reproduzível.

## 3. Contratos mínimos compartilhados

### 3.1. Identidade e temporalidade do quadro

Estender os registros existentes apenas onde necessário. A informação deve acompanhar o frame que chegou ao detector e sobreviver a filas, processamento e transformações.

```text
Vínculo do quadro:
  câmera/fonte + instância de captura + geração + sequência
  dimensões + transformação conhecida até a imagem original
  horários de recebimento/publicação + tempo de mídia, quando disponível
  tipo e intervalo da evidência temporal

Localização visual desse quadro:
  vínculo do quadro + artefato/revisão geométrica + assinatura óptica
  rotação câmera→panorâmica + referências usadas
  suporte espacial + erros medidos + estado/motivo
```

Nomes finais devem seguir os tipos existentes. Não criar identificador paralelo quando já houver um identificador com as mesmas garantias. Não usar apenas `sequence`: outra instância pode repetir o número.

O vínculo entre vídeo e controle inclui perfil de mídia, configuração/nó PTZ e lente/canal quando disponíveis. Streams diferentes não autorizam comandos concorrentes sobre o mesmo atuador. Reusar o escopo conservador do controlador; associação física incerta não justifica paralelismo.

Três fatos distintos: imagem geometricamente localizada; imagem observada recentemente; exposição fisicamente datada. Nenhum implica automaticamente os demais. Sem relógio de exposição confiável, um timeout não prova um limite físico absoluto de idade.

Para o caminho visual, registrar evidência observacional: transição ligada à tentativa, sequência consistente e estabilidade posterior. Ela pode habilitar operações explicitamente compatíveis com esse contrato, após testes. **Nunca preencher `physical_capture_verified` ou timestamps físicos por inferência visual.** O contrato estrito de snapshot físico continua estrito para outros consumidores.

Um quadro antigo pode ser mapeável como histórico. Não pode aparecer como posição atual ou justificar um comando atual. Não consultar a pose mais recente para completar metadados de uma detecção enfileirada.

### 3.2. Validação e ativação sem registro paralelo

Manter `panorama_mapping` como ponteiro canônico da composição. Derivar permissões do job/revisão e das evidências; não persistir booleans redundantes que possam discordar.

- **Mapa validado:** solução geométrica, pontos independentes, fonte, artefato e suporte válidos. Permite salvar/ativar a relação imagem–planta.
- **Quadro mapeável:** mapa validado e localização compatível do quadro real. Permite emitir `world_anchor` naquele quadro.
- **Apontamento habilitado:** mapa validado, controle utilizável, óptica compatível e verificações de apontamento da revisão vigente. Cada solicitação ainda confirma localização atual e caminho alcançável.
- **Retorno confirmado:** operação separada, comparada à referência original. Não é consequência automática do apontamento habilitado.

Permitir verificar fisicamente um rascunho geometricamente válido por ação explícita do wizard; isso evita o ciclo “precisa ativar para verificar, precisa verificar para ativar”. Uso normal de `aim` exige habilitação já obtida.

Revisar os chamadores de `_validated` antes de separar predicados: publicação, leitura ativa, validação, apontamento, restauração e runtime. Adaptar primeiro o consumidor para rejeitar modalidades desconhecidas; só então permitir produzir a nova modalidade. Jobs legados continuam no contrato anterior. Falha num mapeamento ativo aplicável não autoriza fallback silencioso ao legado.

## 4. Captura, cobertura e continuação

### 4.1. Reproduzir antes de escolher buffer ou detector

Preservar uma tentativa curta: imagens antes/durante/depois; sequência decodificada disponível; quadros consumidos; comandos/Stop; tempos; perdas; resets; modelos; suporte espacial; estado do cursor. Registrar a primeira divergência entre movimento presente nos dados e decisão do detector.

Usar artefatos atuais como referência, mas reconhecer seu limite: pares inicial/final não reconstituem a sequência inteira. Nova evidência deve permitir replay determinístico na mesma escala e ordem da execução. Ajustar e aprovar em sequências separadas.

| Resultado observado | Menor correção candidata |
| --- | --- |
| Movimento existe entre imagens que o consumidor perdeu | Leitura sequencial limitada, optativa para os consumidores que precisam dela |
| Quadros chegam, mas reset perde a transição | Corrigir condição de reset/continuidade compartilhada |
| Fluxo mantém consenso incorreto e recuperação não entra | Confrontar consenso com referência pré-comando; corrigir o critério de recuperação |
| Aquisição/decodificação já não preserva a transição | Corrigir o backend responsável ou sua amostragem; buffer posterior não recria frames inexistentes |
| Movimento foi observado, mas suporte/estabilidade reprova | Corrigir a causa demonstrada; manter recusa quando o dado for insuficiente |

Se precisar de buffer: reusar o grabber/capture service, com limite de idade, quadros e bytes; opt-in e descarte ao liberar o consumidor. Preservar a API de última imagem para consumidores existentes. Evicção/perda relevante gera lacuna explícita, nunca uma sequência aparentemente contínua. Evitar cópias de frames integrais por assinante; respeitar o orçamento de memória já existente e medir o pico.

A sequência usada na decisão deve ser auditável. Redução de resolução ou amostragem do diagnóstico não pode impedir reproduzir a falha que motivou a correção. Não adicionar um serviço permanente de gravação.

### 4.2. Critérios temporais e controle

Preservar como baseline: análise em 960 pixels; observação mínima de 0,5 s; janela estável de 0,8 s e cinco imagens; timeout de 12 s por tentativa. Ausência de movimento sem timestamp confiável observa o orçamento completo; transição seguida de estabilidade termina cedo. Não aumentar duração do pulso junto com espera do vídeo.

Stop, cancelamento e renovação da concessão não podem depender de terminar SIFT, reconstrução ou escrita do diagnóstico. Usar timeout de movimento no dispositivo quando suportado, além do Stop do controlador. A especificação ONVIF distingue timeout de `ContinuousMove`, espaços de coordenadas e estados opcionais; descobrir esses valores antes de usá-los. [ONVIF PTZ, §§5.2–5.3 e 5.7](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec.pdf)

Uma resposta perdida não autoriza reenviar o comando por outro protocolo. Reconciliar observação e estado antes de outra tentativa. Se Stop ficar incerto, bloquear novo movimento e informar isso; não declarar “parada” por timeout de uma chamada.

Referência original, referência de trabalho e âncora local permanecem distintas. Guardar uma imagem antes de mover não basta para provar que ela corresponde ao preset original: conferir o vínculo visual, sem reutilizar a referência de trabalho como substituta.

### 4.3. Cobertura e limites

Explorar os dois ramos horizontais e alturas úteis com sobreposição; confirmar direção observada antes de interpretar o sinal do eixo. E-flip, inversão, pan cíclico ou mudança óptica invalidam a resposta local até nova qualificação.

Para cada ramo, registrar um término específico: alcance/fechamento observado, observação insuficiente, suporte insuficiente, comando sem progresso, orçamento ou interrupção. Sem avanço visual isolado não comprova batente. Guardas de textura não podem virar falsas provas de alcance total.

Preservar 256 fotos, 1.200 s ativos, 64 passos por busca e 12 faixas como limites máximos atuais. Aquisição incompleta mantém fotos e lacunas. Não trocar o montador para corrigir fotografias que nunca foram capturadas.

### 4.4. Retomada verificável

Persistir intenção antes do movimento; depois, confirmação e foto aceita. Cursor versão 4 é baseline. A mudança deve manter leitura de jobs antigos; retomada exige evidência compatível, não apenas versão legível.

Ensaiar dois pontos de interrupção: após uma foto qualificada e enquanto uma tentativa está em andamento. Na retomada: parar/reconciliar, localizar âncora ou destino de trabalho, retomar etapa segura; jamais repetir pulso de resultado incerto. Foto nova precisa acrescentar cobertura observada, não duplicar a âncora para aumentar contagem.

Fechar/reabrir UI não perde job ou pontos. Reinício não gera movimento automático. Tempo ativo e tentativas sobrevivem à retomada; finalização e Stop permanecem possíveis após esgotar captura.

## 5. Localizar e mapear o quadro real

### 5.1. Localizador visual mínimo

Reusar fotos originais, intrínsecos e rotações da montagem aprovada. Não fazer matching principal contra a imagem misturada da panorâmica: costuras e ganhos podem alterar aparência e procedência.

1. Consultar primeiro referência anterior compatível e vizinhas prováveis. Cache de descritores por artefato imutável; busca limitada. Em perda de localização, busca mais ampla limitada às fotos existentes, fora do caminho de Stop.
2. Obter correspondências distribuídas e consistentes entre quadro e foto de referência. Suporte restrito, modelos concorrentes ou diferenças ópticas impedem aprovação automática.
3. Desdistorcer ambos com a lente compatível. Ajustar somente a rotação atual; não recalibrar toda a panorâmica em cada quadro.

```text
r_j = unit(undistort(p_atual_j, K, D))
s_j = R_referência · unit(undistort(p_referência_j, K, D))
R_atual = argmin, R ∈ SO(3), Σ w_j ||s_j − R r_j||²
```

Reusar a SVD já presente em `_initial_rotations`, extraindo helper puro somente quando o segundo chamador existir. Alternativa instalada: `Rotation.align_vectors`, que resolve alinhamento por Kabsch. Normalizar vetores; seu `rssd` não é erro em pixels nem probabilidade de localização. [SciPy: `align_vectors`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.spatial.transform.Rotation.align_vectors.html)

Robustez exige seleção de inliers e medição em correspondências reservadas; chamar SVD não rejeita outliers sozinho. Usar as primitivas já existentes. Não aplicar homografia de imagens distorcidas como rotação física universal.

Aprovar apenas quando rotação, suporte e erro permitem a precisão requerida no local de uso. Ambiguidade significa candidatos geometricamente diferentes com evidência compatível; nesse caso, `unlocalized`. Fixar os limiares novos no corpus de desenvolvimento antes do ensaio final. A tolerância de 15 pixels de relocalização do scanner não é tolerância de erro desse solver.

### 5.2. Vincular localização à detecção

Primeira implementação correta: localizar o **mesmo frame**, uma vez, compartilhando resultado entre suas detecções. Se custo for excessivo, selecionar menos frames de análise sem reaproveitar pose entre quadros não verificados. Cache por ID do frame; não cache de “última pose boa” usado indefinidamente.

O runtime usa a lente e `R_atual` para converter pixel original em raio; depois usa `map_ray_to_world`. Preservar caminhos legados que têm pose verificável. Crop, resize, letterbox, rotação ou mirror do detector precisam de inversa explícita até o pixel original. Transformação ausente/desconhecida mantém `unmapped`; não inferir pela dimensão final.

Não aplicar rotação de apresentação duas vezes. Usar o referencial canônico do artefato; rotacionar a planta ou recortar a panorâmica muda a UI, não o significado do raio.

Para cada detecção, verificar região válida e máscara. Limpar `world`, `world_anchor` e metadados antigos quando o contexto falhar, preservando detecções brutas. Registrar motivo e denominador de disponibilidade. Rejeitar tudo é falha funcional, não aprovação de segurança.

### 5.3. Calibração e precisão

Manter o solver atual plano–raios, ao menos seis pontos de ajuste/inliers, proporção de inliers ≥ 80% e duas verificações independentes. Manter o gate angular atual de 1°, sem apresentá-lo como precisão física do motor.

Separar fontes de erro: lente/rotação; planta e escala; correspondência marcada; superfície não plana; âncora da detecção. Primeiro medir pontos estáticos anotados; depois medir a detecção. Uma caixa mal ajustada não deve induzir deformação da planta.

Adotar **0,5 m como teto inicial proposto de erro de posição** na região útil quando houver escala confiável, aproveitando o limite do mapeador de chão existente. Exigir esse teto também em pontos independentes próximos das bordas; registrar distância e direção do erro. Não somar automaticamente uma margem igual para cada etapa.

Sem escala, preservar coordenadas da composição e declarar precisão métrica não validada. Não acrescentar formulário de lente/atuador; aproveitar a escala existente da planta. Medida física ausente é limite de evidência, não um valor a inventar.

Perto do horizonte, erro angular pequeno pode produzir erro métrico grande. Restringir suporte à evidência realmente aprovada. Se uma área-alvo necessária reprovar, corrigir causa/modelo ou registrar o objetivo como pendente; não escondê-la no recorte. Introduzir mais de um plano somente se uma superfície necessária demonstrar essa necessidade.

## 6. Apontamento e retorno: aproximação mais correção limitada

### 6.1. Seleção por capacidade, sem condicionais por marca

| Capacidade observada | Caminho de apontamento/retorno |
| --- | --- |
| Absoluto com vínculo óptico/espacial utilizável | Aproximação absoluta existente e confirmação/correção visual |
| Preset conhecido e compatível | Aproximação pelo preset; confirmação visual; correção disponível |
| Relativo/contínuo, localização visual e sobreposição | Avanços limitados com atualização visual entre comandos |
| Sem localização atual nem destino reproduzível | Não explorar às cegas; conservar mapa e explicar indisponibilidade de apontamento |
| Um eixo apenas | Corrigir componente controlável; alvo fora do conjunto alcançável permanece indisponível |
| Fonte fixa ou PTZ digital | Manter uso visual compatível; não certificar exploração de novas direções físicas a partir de crop/zoom digital |

Preset de trabalho não é destino de toda fotografia. Não criar um preset por foto. Manter o máximo de dois presets próprios por job, inventário reconciliado e propriedade verificada.

### 6.2. Resposta local observada

Durante movimentos já necessários, estimar resposta de cada eixo. Só acrescentar sondagem curta quando essa resposta faltar, dentro da ação explícita de captura/verificação/apontamento. Nunca sondar ao abrir uma tela, mover cursor ou editar ponto.

Para erro visual ou angular local `e`, medir Jacobiano `J` nas unidades reais do comando:

```text
e_novo ≈ e + J Δu
Δu = −g (JᵀJ + λI)⁻¹ Jᵀ e
```

Aqui `J` representa mudança do **erro**, definindo o sinal da correção. Para um eixo, resolver apenas a componente controlável. O ganho `g`, amortecimento e saturação são limites internos; não novos campos obrigatórios.

Em comando contínuo, `Δu` representa pulso com velocidade configurada e duração limitada. Em relativo/absoluto, representa deslocamento na unidade suportada. Não transferir calibração entre modalidades, ópticas, inversões ou regiões sem evidência.

Reaproveitar correção absoluta existente antes de generalizar. Tratar zona morta, inversão e atraso por medição local. Matriz mal condicionada, erro crescente, oscilação ou ausência de observação interrompe/requalifica dentro do orçamento. Cada correção usa imagem nova depois da ação anterior; nenhuma fila de correções sobre frame atrasado.

Preservar limite atual de quatro ajustes finos após aproximação. Não usar a tolerância visual do mapa como tolerância de chegada. Alterações nesses limites exigem evidência anterior ao aceite, não adaptação depois da reprovação.

### 6.3. Alvo fora do campo atual

Uma correção local não garante alcance global. Se houver destino absoluto/preset compatível, usá-lo para aproximação. Caso contrário, usar fotos sobrepostas como referências intermediárias, com o controlador local e nova localização a cada avanço.

Primeiro aproveitar vínculos recuperáveis das capturas. Se insuficientes, persistir apenas IDs das sobreposições já verificadas na montagem, hoje descartadas. Busca de caminho em memória sobre esses IDs basta; não criar serviço de grafo/atlas. Conectividade visual antiga propõe um percurso, não comprova alcançabilidade mecânica atual.

Avançar para uma direção intermediária dentro do suporte observado; confirmar rotação e progresso antes de trocar referência. Não linearizar erro de um alvo atrás da câmera num único pulso. Emenda 360°, singularidade vertical, limite e perda de sobreposição precisam de casos explícitos.

Orçamento total de aproximação não excede limites existentes de navegação; selecionar caminho finito antes de mover e contar todas as tentativas. Se não existir percurso observável, informar alvo indisponível. Se isso impedir um alvo obrigatório da Frente, o objetivo continua reprovado.

### 6.4. Verificação e retorno

Chegada é medida na imagem, comparando o alvo físico com o centro; não pela coordenada prevista pelo próprio modelo. Para alvo sem textura, usar contexto estático ao redor e anotação independente no aceite. Se nem isso distinguir o lugar, não produzir confirmação automática falsa.

Retorno usa exclusivamente imagem/destino original. Preset pode aproximar; correção fina compara diretamente com essa imagem, inclusive quando o original não pertence ao artefato reconstruído. Confirmar óptica e sobreposição, preservando limite atual de **3 pixels na escala de 960**.

Parar e retornar são operações distintas. Cancelamento durante apontamento não dispara retorno oculto. Se a concessão mudou, o finalizador antigo não envia comandos ao novo proprietário. Limpeza remove apenas recursos próprios dispensáveis; restauração de dados não prova restauração física.

## 7. Persistência, reconstrução e compatibilidade

Manter job, revisões candidatas, comparação de revisão e ponteiro ativo existentes. Novos campos geométricos necessários pertencem ao artefato/job, com leitura compatível para ausência em arquivos antigos. Não migrar por suposição.

- **Recorte/zoom da UI:** não alteram coordenadas canônicas nem exigem recalibrar geometria. Fora do recorte não é sinônimo de fora do suporte físico.
- **Nova montagem:** cria novo artefato/geometry revision. Mantém anterior disponível; não desloca pontos silenciosamente nem promove candidato durante uso ativo.
- **Correspondências:** conservar referência canônica e, quando disponível, foto/pixel de origem. Reprojetar para outro artefato somente com vínculo demonstrável; revalidar pontos de verificação e apontamento. Sem origem recuperável, oferecer refazer somente os pontos afetados.
- **Mudança de fonte/lente/zoom/mirror/montagem:** invalida evidência dependente e cache. Fonte com zoom desconhecido requer compatibilidade óptica visual; desconhecido não significa invariável.
- **Limpeza/retenção:** não apagar fotos/modelos necessários à localização de um mapa ativo ou restaurável. Usar referências já existentes para protegê-los; falta de espaço gera ação explícita, não degradação silenciosa.

Permissões de escrita de mapa e de controle físico continuam distintas. Preservar Home Assistant ingress, tradução dos motivos e APIs legadas. Não acrescentar dependência nova nem abstração com uma implementação por antecipação.

## 8. UX mínima, orientada a pontos e estados reais

Aplicar os componentes existentes. Referências: [feedback-loop](/Users/c/.codex/plugins/cache/universal-design-principles/interaction-and-control-principles/1.0.0/skills/feedback-loop/SKILL.md) e [estados e latência](/Users/c/.codex/plugins/cache/universal-design-principles/interaction-and-control-principles/1.0.0/skills/feedback-loop-states-and-latency/SKILL.md). Usar feedback imediato para intenção; sucesso físico somente depois da evidência.

| Momento | Informação e ação principal |
| --- | --- |
| Antes da captura | Fonte correta, região pretendida, movimento esperado e possibilidade de retorno conhecida |
| Captura | Etapa atual, fotos/áreas reais, tempo decorrido e Parar; sem porcentagem de alcance inventada |
| Parcial | Mostrar área obtida e lacunas; Usar área mapeável, Tentar continuar ou Repetir montagem somente quando viáveis |
| Antes dos pontos | Explicar disponibilidade de mapa e de apontamento; bloqueio de motor não surpreende no último passo |
| Associação | Um par por passo; número do lugar, anterior/próximo, editar/excluir/desfazer; preservar par incompleto |
| Prévia | Fantasma bidirecional distinto do ponto confirmado; sem movimento e sem extrapolação |
| Verificação | Pontos independentes sem fantasma/sugestão; imagem atual identificada separadamente da panorâmica histórica |
| Uso | Mapa salvo e apontamento verificado aparecem como estados diferentes; ação de apontar é explícita |
| Interrupção/falha | O que foi preservado, motivo curto e próximo passo possível; detalhes técnicos recolhidos |

Fluxo final: gerar/reutilizar imagem, delimitar área útil, marcar lugares, verificar mapa, salvar; verificar apontamento quando disponível. Uma indisponibilidade física não descarta pontos. A entrega, entretanto, só encerra com os objetivos físicos da Frente demonstrados.

Ao clicar, bloquear duplicação da mesma ação; manter Stop utilizável. Para espera prolongada, etapa e tempo decorrido; estimativa restante apenas se houver base suficiente. Gamificação fica nos marcos reais, sem confete que sugira precisão não medida.

Teclado, toque e mouse têm equivalentes explícitos. Foco volta ao controle correto após diálogo; confirmação não depende só de cor; mensagens importantes usam região acessível sem anunciar cada frame. Respeitar movimento reduzido. Manter PT-BR/inglês, estados de zero/uma/várias fotos e textos sem jargão geométrico.

## 9. Implementação: ordem, arquivos e padrões

Uma entrega com a seguinte ordem interna, sem empurrar motor ou UX necessária para outra fase:

1. **Baseline e reprodução:** conferir código, manifestos e estado; preservar dados; registrar sequência discriminante e critérios do ensaio final.
2. **Observação confiável:** corrigir captura/tempo; provar no replay positivo e negativo; reconciliar produtores de frames.
3. **Navegação e continuação:** corrigir o scanner com a observação nova; provar progresso e preservar retorno/Stop.
4. **Localização e runtime:** reutilizar raios/rotações; transportar evidência do quadro; adaptar consumo antes de habilitar o novo mapa.
5. **Apontamento e retorno:** completar aproximação, correção limitada e verificações; exercitar alvos fora da vista atual.
6. **UX e integração final:** conectar permissões reais; executar percurso físico inteiro pela UI e verificar preservação.

| Responsabilidade | Arquivos existentes a inspecionar/alterar quando necessário |
| --- | --- |
| Quadros e evidência | `processing/frame_grabber.py`, `capture_service.py`, `panorama_capture.py`, `pipelines/operators.py` |
| Estabilidade, percurso e retorno | `processing/panorama_stability.py`, `panorama_scan.py`, `ptz_controller.py` |
| Raios, referências e localização | `processing/panorama_reconstruction.py`, `processing/panorama_mapping.py`, `view_resolver.py` |
| Jobs, permissões e consumidores | `source_panorama.py`, `panorama.py`, `pipelines/postprocess.py` |
| UI | `CameraSourcePanoramaSection.tsx`, `CameraPanoramaMappingModal.tsx`, `panoramaProjection.ts`, tipos/API/traduções relacionados |

Paths Python relativos a `extensions/cameras/src/toposync_ext_cameras`; UI sob `extensions/cameras/ui/src`. Essa lista orienta investigação; não obriga editar todos os arquivos.

Reusar funções antes de extrair módulos. Validar dados finitos, matrizes, unidades, revisões e dimensões na fronteira. Não engolir erro como capacidade inexistente. Limitar processamento pesado e evitar bloquear o event loop. Comentários `ponytail:` apenas para simplificação com limite concreto e condição de evolução.

Sem commits automáticos, publicação, firmware ou alterações gerais de configuração previstas neste plano. Preservar trabalho não commitado. Documentar decisões/evidências nos registros de implementação existentes.

## 10. Critérios numéricos e testes

### 10.1. Critérios antes do ensaio

| Critério | Regra |
| --- | --- |
| Montagem visual | Preservar gates atuais, incluindo p95 independente ≤ 8 px em largura 960; isso não aprova motor nem alcance |
| Referência do scanner | Preservar ≤ 15 px, sobreposição ≥ 0,85 e imagem recente; não usar isso como erro admissível da localização |
| Retorno original | Preservar ≤ 3 px pela métrica atual do matcher; óptica e estabilidade verificadas |
| Centro do apontamento | **Proposta nova:** alvo anotado independentemente a ≤ 3 px do centro em largura 960, com imagem estável. Métrica pontual, diferente da comparação de retorno |
| Calibração do mapa | Preservar seis inliers, 80% e duas verificações; gate angular de 1°; acrescentar teto proposto de 0,5 m onde houver escala |
| Cobertura | Todos os lugares/regiões-alvo registrados antes do ensaio aparecem em fotos válidas e dentro da máscara; lacunas internas medidas explicitamente |
| Mapeamento ao vivo | **Proposta inicial:** ≥ 90% dos quadros elegíveis anotados recebem posição dentro do teto; medir também quantos quadros totais ficaram inelegíveis |
| Recursos e tempo | Manter limites atuais de captura. Medir p50/p95 de latência, memória e descartes; não aprovar se fila cresce continuamente ou Stop depende do processamento pesado |

As propostas novas são metas de engenharia para este aceite, não desempenho já observado ou promessa universal. Fixá-las no registro antes de testar. Se a anotação não tem precisão suficiente para avaliar três pixels, melhorar a evidência; não diminuir o erro subtraindo a incerteza conveniente.

Quadros elegíveis são definidos por anotação externa ao resultado: fonte/óptica corretas, cena estável e ponto visível na região-alvo, fora das janelas de movimento deliberado. Falta de localização pelo algoritmo não torna um quadro inelegível. Reportar cobertura temporal completa para evitar aprovação por recusar as partes difíceis.

### 10.2. Automatização mínima por comportamento

Expandir os testes existentes; sem novo framework. Cada linha abaixo é um comportamento obrigatório, não uma exigência de criar um arquivo ou dezenas de fixtures.

| Área | Prova positiva | Contraprova necessária |
| --- | --- | --- |
| Tempo | Replay da transição antes perdida, com captura correta | Vídeo congelado, atraso, loop/repetição, reset, objeto dominante e lacuna relevante não viram chegada |
| Buffer, se necessário | Entrega ordenada para quem pediu sequência | Limite, evicção, liberação e consumidor lento não vazam memória nem bloqueiam Stop |
| Continuação | Novas fotos no mesmo job após dois tipos de interrupção | Sem replay de pulso incerto, orçamento renovado ou duplicata contada como progresso |
| Localização | Rotação conhecida recuperada de projeções independentes | Textura repetida, óptica trocada, paralaxe incompatível, emenda e suporte concentrado |
| Runtime | Detecção do próprio frame mapeada com transformações conhecidas | Pose posterior, ID/revisão/fonte errados, transformação desconhecida e horizonte recusados |
| Controle | Servo reduz erro num simulador de planta independente | Atraso, folga, inversão, eixo ausente, saturação, perda de concessão e Stop incerto |
| Persistência | Ativação/restauração preservam modalidade e revisões | Remontagem, limpeza e leitura de job antigo não fabricam validação |
| UX | Fluxo completo e recuperação em PT-BR/inglês | Hover/edição/duplo clique não causam movimento ou duplicação |

Oráculos geométricos devem projetar uma câmera/plano conhecidos, sem chamar o mesmo helper sob teste para calcular o esperado. Simulador do motor não deve usar o próprio controlador como modelo do equipamento. Contar falhas e recusas; não testar somente retornos de mocks.

Executar pytest dirigido aos módulos afetados, verificações de tipos/lint e testes de navegador existentes. Rodar build quando UI/tipos/integração exigirem. Ampliar testes somente por mudanças ou riscos novos; a documentação desta revisão não demanda ensaios de hardware.

## 11. Validação física integrada e encerramento

### Preparação

Usar instância principal 5174 quando disponível, com backend e código identificados. Conferir fonte/perfil/lente e região útil antes de mover. Preservar manifestos, configuração sem segredos, composições, presets e referências necessárias ao retorno. Não alterar câmera para fazê-la caber no algoritmo.

Registrar antes do ensaio: regiões esquerda/centro/direita e inferior; pontos de ajuste; pontos independentes; escala; critérios da seção 10. Usar condições nas quais se pretende operar. Se apenas condição diurna passar, não certificar a falha noturna anterior como resolvida.

### Execução e amostragem

1. Reproduzir/validar a correção temporal com trecho curto antes de repetir varredura longa.
2. Capturar pela UI, interromper após foto válida, continuar; exercitar também interrupção durante tentativa e reconciliar com segurança.
3. Demonstrar cobertura-alvo com novas fotos, incluindo outro lado da rua e altura útil; verificar máscara e imagem sem montagem manual.
4. Marcar pontos no wizard; reservar verificações sem sugestões; salvar mapa; demonstrar detecções novas dentro da região validada.
5. Executar **seis ensaios funcionais de apontamento/retorno**: três alvos distribuídos, duas direções de aproximação. Incluir alvo inicialmente fora do campo de visão e variação vertical alcançável. Sem ajuste manual durante execução.
6. Registrar erro de cada alvo e retorno, tentativas, recusas, tempos e estado final. Seis ensaios são cobertura funcional mínima, não estimativa estatística de repetibilidade universal; não publicar p95 físico confiável com essa amostra.
7. Reabrir UI e conferir pontos, revisões, fotos e ações disponíveis. Revalidar configuração/presets e deixar aplicação disponível conforme o contexto da execução.

Para disponibilidade do mapeamento, usar trecho contínuo anotado de pelo menos um minuto por enquadramento de avaliação, em esquerda/centro/direita. Informar contagens e frequência efetiva; quadros correlacionados não são amostras independentes. Medir geometria com pontos estáticos antes de atribuir erro à detecção.

### Encerramento honesto

Aceitar somente com cobertura-alvo, continuação nova, mapa em quadros corretos, apontamento e retorno dentro dos critérios. Uma limitação não exercitada permanece não validada; equipamento não ensaiado não recebe selo de compatibilidade.

Usar tabela curta no relatório existente: **bloqueio; causa/limite demonstrado; alteração; teste; evidência; resultado restante**. Conservar falhas anteriores e comparação no mesmo material. Não chamar fim do orçamento de conclusão nem abrir outra frente sem hipótese nova.

Se faltar acesso físico, concluir o trabalho independente possível e marcar precisamente os aceites não executados. Se uma hipótese falhar, registrar a primeira divergência e testar a alternativa menor seguinte. Uma repetição longa sem dado novo não é avanço.

## 12. Rollback e condições de parada

Reverter apenas alterações desta entrega; não apagar mudanças existentes do usuário. Usar candidato/anterior e restauração já disponíveis. Preservar imagens, pontos e diagnóstico útil.

Parada incerta, perda de concessão, conflito de revisão, óptica incompatível ou localização ambígua suspendem uso dependente. Não encadear correções em cima de estado desconhecido. Nenhum movimento automático ao reiniciar; limpeza não comanda retorno.

Antes de declarar rollout pronto, verificar restauração dos dados anteriores e leitura dos legados. Rollback de dados e retorno da câmera recebem resultados separados.

## Referências de execução

- [Dossiê de estado e matemática](/Users/c/Projects/toposync-2/docs/dossie-panoramica-mapeamento-calibracao-20260911.md).
- [Ensaios e testes anteriores](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/README.md), [tentativas físicas](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/physical-attempts.json) e [regressão temporal](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/timing-regression.json).
- [Calibração e chamadores de validação](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama.py:1482), [runtime](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py:3995), [produtor de frames](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/pipelines/operators.py:1870).
- [Rotações já existentes](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/panorama_reconstruction.py:432), [evidência do grabber](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py:161), [exigência atual de snapshot físico](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama.py:938).
- [Ponytail full](/Users/c/.codex/plugins/cache/ponytail/ponytail/4.9.0/skills/ponytail/SKILL.md): reutilização, menor mudança correta, testes proporcionais e preservação das proteções essenciais.
