# Toposync: panorâmica, mapeamento e calibração PTZ

Dossiê para pesquisa por IAs. Autor: Mateus Calza. Consolidado em **11 de setembro de 2026**.

**Objetivo:** explicar o sistema existente, suas evidências e seus bloqueios. Permitir questionar a arquitetura sem repetir investigações ou confundir testes aprovados com sucesso físico.

## 1. Leia primeiro

- A montagem visual funciona com imagens qualificadas. O último ensaio gerou uma panorâmica parcial com erro visual p95 de **2,132 pixels**, na escala de análise de 960 pixels.
- A aquisição automática ainda falha em percorrer a área desejada. Nesse ensaio: **12 fotos, nenhuma faixa completa**, duas fotos conectadas em outra altura.
- O detector ainda reporta `motion_not_observed` em tentativas com diferenças visuais entre imagens inicial e final. A causa remanescente não foi demonstrada.
- Retorno por preset não tem repetibilidade certificada: **2,321 pixels** em um ensaio; **4,011 pixels** no seguinte. O limite atual é 3 pixels.
- Existe outro bloqueio estrutural: a panorâmica automática **não fornece geometria verificada do atuador**. O wizard aceita correspondências visuais, mas esse caminho não permite apontamento nem ativação física.
- Há testes automatizados substanciais. Falta aceite físico completo, incluindo continuação com novas fotos, alcance útil, calibração métrica e apontamento repetível.

**Não reduzir o problema a “a costura está ruim”, “falta tilt” ou “faltam testes”.** São problemas diferentes: observar imagens, navegar, reconstruir, relacionar com a planta e comandar motores.

### Proveniência e limites deste retrato

`Código` significa comportamento inspecionado no checkout. `Observado` significa artefato de execução. `Hipótese` significa explicação ainda não demonstrada. `Proposta` significa trabalho não implementado.

Repositório: `/Users/c/Projects/toposync-2`. HEAD: `0ca8d6bc0b61ad1e746a9c59b7476c6cda087cf3`, branch `main`, com trabalho não commitado. HEAD sozinho não identifica toda a implementação. Os **15 arquivos** do manifesto final de validação mantinham hashes iguais durante esta consolidação; isso não equivale a um manifesto de todo o repositório.

Últimos ensaios físicos: **10/09/2026**, noturnos, somente **Frente Reolink / wide_main**. Neste dossiê houve leitura de código e evidências; nenhum movimento de câmera, reinício ou nova execução dos testes. Estado operacional e números de testes abaixo pertencem à rodada anterior.

O documento é autossuficiente para análise inicial. Links locais servem à auditoria; outra IA pode não ter acesso. Não contém credenciais, endereços de câmeras nem imagens privadas incorporadas.

## 2. Objetivo do produto e restrições

O usuário quer escolher câmera e stream, gerar automaticamente uma panorâmica ampla e desenhar sua área útil. Depois, associar lugares dessa imagem a lugares da composição/floorplan. A relação deve servir à visualização, ao mapeamento de detecções e, quando validada, ao apontamento PTZ.

Na Frente, o resultado esperado é ver a rua dos dois lados. No Quintal, também importa o chão, exigindo expansão vertical útil. “Completa” significa alcance útil observado da câmera, não preencher artificialmente uma esfera de 360° × 180°.

Restrições já estabelecidas:

- **Zero campos geométricos obrigatórios:** não pedir distância focal, limites angulares, orientação, tolerâncias ou velocidades. O antigo formulário de aproximadamente 18 campos foi rejeitado pelo usuário.
- Alguns minutos de captura são aceitáveis quando o processo é autônomo, compreensível e recuperável. Tempo aceitável ainda não foi medido com usuários.
- Panorâmica pertence à **fonte da câmera**, podendo ser reutilizada fora de uma composição. Lentes e streams não são intercambiáveis automaticamente.
- Wizard orientado a pontos; um par de correspondências por passo; prévia bidirecional com pontos fantasmas; interface minimalista.
- Compatibilidade por capacidade observada, sem ajustes exclusivos para a câmera do desenvolvedor.
- Resultado computacional reproduzível. Nenhuma IA corrige manualmente cada enquadramento em produção.
- Preservar configurações, presets alheios, composições, mapeamentos legados, traduções e caminhos de Home Assistant ingress.

Fora da implementação atual: reconstrução geral de cena 3D, SLAM, preenchimento generativo, novos protocolos de fabricantes, controle cloud-only, servo visual global para qualquer atuador e inferência completa da geometria física do motor.

## 3. Como chegamos aqui

1. **Mapeamento legado:** homografia, quadrilátero da vista e refinamentos. Usuário sente “mexe aqui, distorce lá”; quatro cantos podem ajustar perfeitamente os próprios exemplos e produzir extrapolações absurdas.
2. **Correspondências no chão:** evolução para pontos explícitos, lente, região válida e verificações independentes. Melhor separação entre imagem, mundo e edição da apresentação.
3. **Panorâmicas assistidas:** usuário aprovou fortemente as imagens das câmeras reais. A beleza dessas imagens não certificou aquisição automática nem precisão repetível dos motores. Os procedimentos iniciais não foram reconstruídos integralmente nesta consolidação.
4. **Panorâmica automática na fonte:** estima lente/rotações, captura por observação de movimento e permite recorte. Remove a necessidade de preencher geometria na entrada.
5. **Wizard por ponto:** importa a panorâmica; oferece correspondências, rascunhos, prévia bidirecional, suporte espacial e verificações.
6. **Correções operacionais sucessivas:** temporização, limites, recuperação, referências, cursores, capacidades ONVIF e limpeza. A última entrega corrigiu defeitos reais, mas não passou no aceite físico completo.

**Inferência sobre o ciclo de retrabalho:** melhorias locais são necessárias, porém não demonstram a cadeia completa. O produto pode avançar visualmente enquanto aquisição e controle físico continuam bloqueados. Falta um conjunto reproduzível que atravesse essas fronteiras e permita comparar alternativas.

## 4. Cinco camadas que não devem compartilhar uma única aprovação

| Camada | O que precisa provar | Estado atual |
| --- | --- | --- |
| Aquisição | Imagens novas, estáveis, da fonte certa; avanço e cobertura | Parcial na Frente |
| Reconstrução | Imagens geometricamente compatíveis; lente e rotações observáveis | Montagem visual aprovada no último subconjunto aceito |
| Mapeamento | Correspondência panorâmica/planta consistente dentro de uma região | Solver e wizard implementados; precisão real não certificada |
| Atuador | Comandos ou posições produzem os raios desejados | Geometria automática ausente; retorno real inconsistente |
| Uso final | Detecções e apontamento usam revisão, fonte, óptica e pose válidas | Consumidores protegidos; caminho automático bloqueado para ativação |

Exemplo real: `report.status="ready"`, `quality.status="ready"`, `positioning_status="not_validated"`, `coverage.acquisition_complete=false`. Não existe contradição: a montagem está disponível, a captura está incompleta e o motor não está calibrado.

### Persistência e contratos

- `source.metadata.panorama`: referências a revisões/artefatos ativos, anteriores e candidatos; recorte da fonte.
- Recorte: `{u_start, u_width, v_start, v_height}`, normalizado sobre a panorâmica completa; admite travessia da emenda horizontal. Muda apresentação, não raios, cobertura física ou calibração.
- Jobs da fonte: `runtime/cameras/source-panorama/jobs/{job_id}`. Artefatos: `runtime/cameras/source-panorama/artifacts/{artifact_id}`.
- Jobs da calibração em composição: `runtime/cameras/panorama/{job_id}`. Seus identificadores e revisões não são os da aquisição.
- Mapeamento ativo: `element.props.panorama_mapping = {job_id, revision, source_id, status: "ready"}`. `previous_panorama_mapping` permite restaurar o anterior.
- `calibrated_views` continua existindo para vistas e mapeamentos legados. Inclui referência de pose, escopo do stream, qualidade e, conforme o caminho, quadrilátero/refinamentos/assinaturas visuais.

### Operações principais da API

Sob o base path da aplicação:

```text
GET   /api/cameras/cameras/{camera_id}/sources/{source_id}/panorama
POST  /api/cameras/cameras/{camera_id}/sources/{source_id}/panorama/jobs
GET   /api/cameras/panorama-jobs/{job_id}
POST  /api/cameras/panorama-jobs/{job_id}/{stop|resume|return|reconstruct|cleanup}
PATCH /api/cameras/cameras/{camera_id}/sources/{source_id}/panorama/crop
GET   /api/cameras/panorama-artifacts/{artifact_id}
```

Criação recebe chave de idempotência; recorte exige revisão esperada. `reconstruct` reutiliza fotos sem movimentar. `cleanup` remove apenas recursos próprios verificáveis, sem Stop ou movimento. Reiniciar a aplicação não retoma movimentos automaticamente.

## 5. UX atual e dívida de experiência

### Configuração da câmera e do stream

- Ação principal para gerar panorâmica; fases **Capturar, Montar, Resultado**; contagem real de fotos e cobertura.
- Parar acessível durante aquisição. Interromper não dispara retorno silencioso. Retorno ao enquadramento original tem ação e resultado próprios.
- Resultado distingue imagem, cobertura e estado físico. Fotografias válidas sobrevivem a falhas; nova montagem não exige nova captura.
- Zero, uma e várias fotos têm mensagens diferentes. O antigo texto “fotografias guardadas” com zero fotos foi corrigido.
- Continuação só aparece com caminho estrutural de recuperação; razões indisponíveis são traduzidas. A execução ainda precisa confirmar câmera e referência.
- Detalhes abrigam diagnóstico e limpeza de recursos temporários. Recorte tem suporte a teclado, toque e emenda horizontal.
- Textos PT-BR e inglês; controles e estados seguem os componentes existentes do Toposync.

### Wizard de correspondências

- Organização geral de preparação, associação, verificação e uso. Dentro da associação, cada passo representa um lugar/par de pontos.
- Pode começar pela panorâmica ou pela planta. Navegação por pontos, edição, exclusão, desfazer e pares incompletos preservados.
- Revisão do servidor prevalece. Rascunhos locais permitem recuperação sem sobrescrever alterações concorrentes silenciosamente.
- Pontos confirmados preenchidos; pontos fantasmas vazados. Projeção local por matriz, com `requestAnimationFrame`; hover não chama IA, servidor de cálculo ou motor.
- Prévia exige job/fonte/revisão/artefato compatíveis, máscara válida e ausência de edição pendente. Desaparece fora da região suportada.
- Prévia provisória pode aparecer quando faltam apenas as verificações independentes. Falhas geométricas não recebem essa exceção.
- Não mostrar sugestão fantasma durante marcação de pontos de verificação: evita contaminar a validação com a resposta prevista.

### O que continua difícil para o usuário

**Código:** uma panorâmica automática pode ser importada e receber pontos, mas não pode ser ativada fisicamente. `physical_blockers=["source_actuator_geometry_unavailable"]` bloqueia o passo final. A interface comunica o bloqueio; ainda não oferece solução automática para ele.

**Inferência de UX:** permitir investimento em muitos pontos antes de tornar esse limite compreensível pode reforçar a sensação de trabalho perdido. A separação técnica dos estados precisa se traduzir em próximos passos úteis.

Não há estudo com participantes provando facilidade, confiança, abandono, tempo por ponto ou compreensão de “parcial”. Testes de navegador verificam interação, não satisfação.

Princípios já incorporados: exposição progressiva, reconhecimento por pontos, feedback factual, recuperação, contenção da extrapolação e controles consistentes. A gamificação permanece discreta: marcos reais de progresso. Não usar percentuais inventados, troféus ou aparência de conclusão para compensar bloqueios.

## 6. Aquisição, navegação e segurança operacional

### Descoberta por capacidades

| Capacidade utilizável | Estratégia existente | Limitação |
| --- | --- | --- |
| Posição absoluta confiável | Destinos absolutos e percurso absoluto existente | Chegada ainda requer imagem compatível |
| Contínuo/relativo e presets | Varredura visual, destinos temporários próprios | Repetibilidade depende de confirmação visual |
| Absoluto incompleto e presets | Presets nos destinos, movimento disponível no percurso | Eixos desconhecidos permanecem desconhecidos |
| Contínuo/relativo sem destino global | Exploração local com âncora confirmada | Sem promessa universal de retorno/retomada |
| Apenas pan | Varredura horizontal e limites/fechamento observados | Não prometer cobertura vertical |
| Apenas tilt | Identificação da limitação | Scanner exclusivamente vertical não implementado |
| Duas lentes, streams ou canais | Vínculo explícito de fonte, perfil, nó, configuração e óptica | Não transferir calibração por nome genérico |

O adaptador conserva espaços e unidades ONVIF. Coordenada normalizada não vira grau/radiano por conveniência. Pan, tilt e zoom ausentes não recebem zero. O caminho nativo Reolink já existente pode complementar capacidades; o scanner não contém uma tabela de amplitudes por marca.

Identificar controle digital/físico universalmente continua limitado. Fontes da mesma câmera compartilham exclusão de controle; cadastros duplicados do mesmo atuador físico não possuem identificação universal garantida. A concessão do Toposync também não bloqueia aplicativos externos, rondas ou rastreamento da câmera.

### Três referências distintas

1. **Original:** imagem/destino antes dos movimentos de preparação. Usado para retorno final.
2. **Trabalho:** referência qualificada da faixa inicial. Usada para recuperar a navegação e explorar outros ramos.
3. **Âncora local:** última imagem qualificada que sustenta um deslocamento/conexão.

Antes, o scanner mandava voltar ao original e comparava com a imagem de trabalho. Em um ensaio diferiam **81,29 pixels**, apesar de 88,18% de sobreposição. Separar os destinos corrigiu esse contrato; não resolveu automaticamente estabilidade, limites nem repetibilidade mecânica.

No máximo dois presets próprios por job. Intenção persistida antes de `SetPreset`; respostas perdidas são reconciliadas com inventário. Uso/remoção verifica propriedade e vínculo. Presets anteriores do usuário não são removidos. Destino ainda necessário ao retorno pode permanecer registrado após falha.

### Percurso e continuação

O scanner procura referência útil, explora ambos os sentidos horizontais com sobreposição adaptativa e tenta conexões em outras alturas. Certifica faixas usando evidências de seus extremos ou fechamento, não apenas número de fotos. Sinal positivo de tilt não significa universalmente chão ou céu.

Falha de textura, vídeo atrasado ou comando sem efeito observado não prova limite mecânico. Uma fotografia em outra altura não prova uma faixa vertical completa.

Cursor **versão 4** conserva etapa, intenção, fotos, referência e orçamento. Não repete cegamente pulsos relativos/contínuos cujo resultado ficou incerto. Cursores antigos permanecem consultáveis, mas podem ser irretomáveis. Perda de controle, parada incerta ou óptica incompatível impede exploração adicional.

Limites atuais: **256 fotos; 1.200 segundos de captura ativa; 64 passos por busca; 12 faixas; 12 segundos por tentativa de observação**. Retomar não renova orçamento. Há limites de memória/armazenamento e filas; não confundir esses valores com os limites menores do caminho legado de captura na composição.

### Comparação de vistas e retorno

`panorama_scan._match` usa SIFT, razão 0,70, homografia robusta com limiar de 2,5 pixels, ao menos 24 correspondências/inliers, quatro células de uma grade de 12 e fecho convexo ocupando ao menos 12% da imagem. Avalia até três modelos de consenso espacial.

Para homografia `H` e amostra `p`, `d(p)=||project(Hp)-p||`. O deslocamento reportado é **p95 em uma grade fixa 3 × 3**, posições relativas 0,2/0,5/0,8. Não é erro médio de todos os pixels nem erro angular do motor.

- Relocalização: comparação verificada, sobreposição ≥ 0,85, deslocamento ≤ 15 pixels, imagem recente; limite de idade de 1 segundo após comparação.
- Retorno exato: limite específico de 3 pixels.
- Comando sem progresso: usa comparação local e limite de 0,5 pixel. Não reutiliza a tolerância de 15 pixels da âncora histórica.
- Correção absoluta fina existente usa resposta local observada `e ≈ J Δu`, com ajustes limitados e confirmação de melhora. Não existe equivalente global certificado para qualquer comando contínuo/preset.

Todos esses pixels pertencem à análise de largura 960. Alta sobreposição não implica enquadramento idêntico.

## 7. Quando fotografar: matemática e lacuna temporal

`VisualStabilityDetector` observa movimento global robusto por fluxo óptico, consistência de rastros, deriva, distribuição espacial e nitidez. Mede deslocamento antes de compensar a transformação: estabilizar digitalmente a imagem não pode esconder movimento do motor.

| Parâmetro | Valor atual, análise em 960 pixels |
| --- | --- |
| Observação mínima / janela estável | 0,5 s / 0,8 s; ao menos cinco imagens |
| Intervalo máximo entre imagens | 1 s |
| Velocidade estável com tempo de mídia válido | ≤ 1 pixel/s |
| Deslocamento estável com apenas observação local | ≤ 0,15 pixel |
| Deriva máxima | 0,5 pixel |
| Transição de movimento | ≥ 0,35 pixel; também ≥ 3 pixels/s quando há base temporal de mídia |
| Suporte | ≥ 80 rastros; seis de 12 células; fração de inliers ≥ 0,55 |
| Erro ida/volta dos rastros / erro do modelo | ≤ 0,75 / 0,8 pixel |
| Nitidez relativa mínima | 0,75 |
| Timeout da tentativa | 12 s |

Há recuperação por correspondências SIFT quando o rastreamento da transição foi perdido. Essa recuperação exige âncora, é limitada a duas buscas por segundo e reinicia a janela de estabilidade. Não substitui a comprovação posterior de parada.

`motion_transition_unobserved` significa que não houve evidência temporal suficiente da transição exigida. **Não significa que o motor comprovadamente ficou imóvel.** Reconexão, geração, sequência, lacunas temporais e imagens repetidas participam dessa decisão.

### Lacuna confirmada no caminho de vídeo

`CaptureFrameSample` distingue publicação, recebimento local e exposição física. Os backends atuais OpenCV e FFmpeg **não fornecem timestamp físico verificado**. Seus campos físicos permanecem vazios; o sample padrão também não expõe `media_time`. O adaptador admite esse campo se outro backend o fornecer.

O buffer oferece a **última imagem**; consumidores podem não observar todas as imagens decodificadas. O caminho FFmpeg extrai JPEGs e publica imagens com horários locais. Horário de publicação ou fim de decodificação não prova exposição posterior ao comando.

Consequência: nesses caminhos, a captura depende de evidência visual da transição para distinguir imagem nova de vídeo atrasado. Não basta `MoveStatus=IDLE`, sequência crescente ou uma janela local imóvel.

### Defeito corrigido e resultado restante

Antes, uma janela imóvel podia encerrar a decisão em aproximadamente 1,6–1,7 s, antes de o vídeo mostrar o movimento. A correção espera o orçamento completo de 12 s para concluir ausência de movimento; uma transição seguida de estabilidade ainda termina cedo. Isso não prolonga o pulso do motor.

Regressão sintética registrada: Stop em **0,6 s**, transição em **2,6 s**, captura estável em **3,8 s**. Caso sem movimento observado consome **12 s**. A correção passou; a captura física posterior ainda encontrou transições não qualificadas.

**Questão de pesquisa:** como separar latência, perda de amostras, falta de textura, objetos móveis e movimento mecânico sem exigir metadados que muitas câmeras não entregam?

## 8. Reconstrução da panorâmica: o que realmente é estimado

Algoritmo atual: `automatic_rotation_brown_v1`. Análise em largura 960; renderização típica **4096 × 2048**, equiretangular.

Para fotografia `i`, ponto `p_i`, intrínsecos `K` e distorção `D`:

```text
r_i = unit(undistort(K⁻¹ p_i, D))
R_i r_i ≈ R_j r_j              para correspondências da mesma direção
```

A implementação ajusta lente compartilhada e rotações relativas `R_i ∈ SO(3)`, minimizando resíduos robustos de reprojeção bidirecional. Primeira rotação fixa remove a liberdade de orientação global. Não estima translação de cada fotografia nem profundidade da cena.

Modelo de lente:

```text
fx = exp(a)
fy = fx · exp(b)
cx = (largura − 1) / 2
cy = (altura − 1) / 2
radial(r) = 1 + k1 r² + k2 r⁴
D = [k1, k2, 0, 0, 0]
```

Quatro parâmetros intrínsecos livres, mais `3(N−1)` parâmetros de rotação. Centro óptico fixado no centro da imagem; distorções tangenciais e coeficientes adicionais não são inferidos. `k2` usa uma parametrização restrita; a validação também exige derivada radial `1+3k1r²+5k2r⁴ > 0,05` no domínio observado.

### Correspondências e aprovação visual

- SIFT/FLANN, razão 0,70; homografia RANSAC com limiar de 3 pixels; ao menos 20 inliers e fração ≥ 0,20.
- Suporte espacial nas duas imagens. Correspondências concentradas em um detalhe não bastam.
- Grafo conectado; fotos desconectadas podem ser omitidas e são reportadas. Não inventar ligação ou pixels.
- Separação de ajuste e verificação por trilhas de características, evitando dividir observações da mesma trilha entre os conjuntos.
- Ajuste robusto com limites, Jacobiano esparso e verificação de convergência/observabilidade. A análise de intrínsecos remove a contribuição das poses antes de avaliar condicionamento.
- p95 independente acima de **8 pixels em 960** exige revisão. Convergência, limites, suporte e condicionamento também importam.
- Ganhos de exposição e mistura multibanda melhoram apresentação. Máscara e índice da foto de origem preservam procedência. Mistura visual não altera os raios canônicos para esconder erro.

O último modelo estimou `fx≈2320,03`, `fy≈2314,00`, `k1≈−0,33058`, `k2≈0,08091` na resolução 3840 × 2160. São parâmetros **estimados para panorâmica visual**, não calibração física certificada.

### Hipóteses físicas do modelo e limites

Rotação aproximadamente central, óptica constante e cena suficientemente rígida. Paredes próximas, centro óptico deslocado do eixo PTZ, duas lentes, rolling shutter, foco, zoom e objetos móveis podem violar essas hipóteses. Isso é uma lista de mecanismos possíveis, não diagnóstico comprovado da Frente.

Boa reprojeção em correspondências aceitas não mede regiões excluídas, pontos sem textura, chão não fotografado ou exatidão do motor. Orientação de apresentação não fornece norte ou gravidade certificados; o relatório real registra `gravity_verified=false`.

## 9. Mapeamento e calibração: três modelos diferentes

### 9.1 Homografia legada

Para ponto de chão `(X,Z)` e imagem `(u,v)`:

```text
λ [u,v,1]ᵀ = H [X,Z,1]ᵀ
q = H⁻¹ [u,v,1]ᵀ
(X,Z) = (q0/q2, q1/q2)
```

Quatro pares não colineares podem determinar `H`. Erro baixo nos mesmos quatro pares não valida generalização. Quando `q2` se aproxima de zero, pequenas mudanças produzem coordenadas enormes. Existe guarda numérica; ela não transforma uma região próxima do horizonte em mapeamento confiável.

Refinamentos locais/de contorno do caminho legado corrigem resíduos sobre a transformação global. Podem introduzir acoplamento perceptível na edição; não equivalem a uma calibração física da lente ou do motor.

### 9.2 `camera_ray_ground_v2`

Desdistorce pontos usando perfil de lente, estima homografia no plano ideal e restringe uso aos polígonos suportados. Não aplica deformação local. O caminho suporta perfis Brown e `fisheye_kb4_v1`; isso **não significa** que a reconstrução automática estime universalmente ambos.

Guardas atuais incluem seis pontos de ajuste/inliers, fração de inliers ≥ 0,80, distribuição espacial, estabilidade numérica e ao menos duas verificações. Nesse modelo, verificações usam limite de **0,5 unidade da composição**; metros apenas se a planta estiver definida em metros.

Detecções normalmente usam a base central da caixa como aproximação de contato com o chão. Isso não localiza automaticamente objetos suspensos nem elimina erro de detecção. Consumidores geram `world_anchor` apenas quando encontram mapeamento e contexto válidos.

### 9.3 Correspondências entre panorâmica e planta

Panorâmica normalizada, com `u,v ∈ [0,1]`:

```text
α = 2π(u − 0,5)
β = π(0,5 − v)
r = (cos β cos α, cos β sin α, sin β)
```

Nesse referencial, os dois primeiros eixos são horizontais e o terceiro aponta para cima. A composição chama suas coordenadas de chão de `X,Z`; não confundir nomes dos eixos entre referenciais.

O solver ajusta matriz projetiva `A`, tamanho 3 × 3:

```text
p = [X,Z,1]ᵀ
r_previsto = unit(Ap)
[r_observado]× Ap = 0
erro_angular = atan2(||r_previsto × r_observado||,
                    r_previsto · r_observado)
```

DLT normalizado/SVD, ajuste robusto por subconjuntos e refinamento angular. Guardas rejeitam degenerescência, condicionamento ruim e raios com sentido incompatível. Limites: até 64 pontos; amostragem robusta limitada a 512 subconjuntos.

Qualidade exige pelo menos **seis inliers de ajuste**, proporção ≥ 80% e **duas verificações independentes**. Limiar angular padrão: **1° = π/180 radiano**. Pontos de verificação não participam do ajuste e precisam ficar dentro do suporte dos pontos de ajuste.

Inversa:

```text
q = A⁻¹ r
exigir q2 > 10⁻¹²
(X,Z) = (q0/q2, q1/q2)
exigir ponto dentro da região suportada
```

Máscara de cobertura e região válida impedem previsão em buracos, atrás do raio ou fora da área validada. A matriz `A` é uma relação projetiva plano/raios; não foi obtida impondo uma decomposição extrínseca métrica rígida completa.

**Não interpretar 1° como precisão física certificada.** Erro em metros depende de distância, inclinação e proximidade do horizonte. Mesmo uma relação angular consistente depende de planta correta, superfície aproximadamente plana e pontos realmente correspondentes.

Para compreender a sensibilidade, suponha chão horizontal, altura conhecida `h` e depressão do raio `γ` abaixo do horizonte: `distância = h·cot(γ)`. Pequeno erro angular produz `|δdistância| ≈ h·csc²(γ)·|δγ|`, com ângulos em radianos. O erro cresce perto do horizonte. Essa relação analítica não representa uma altura já inferida pelo sistema. Rampas, degraus e superfícies em alturas diferentes também podem exigir mais de um plano.

## 10. A calibração do atuador ainda ausente

O antigo perfil geométrico fornecia relação entre posição do dispositivo e ângulo, lente, limites, zoom e tolerâncias. Uma aproximação usada nesse caminho é:

```text
θ(p) = θ_min + (p − p_min)/(p_max − p_min) · (θ_max − θ_min)
```

Ela depende de unidades conhecidas e relação linear utilizável por eixo. Não deve ser aplicada a espaços desconhecidos, eixos ausentes, posições normalizadas arbitrárias ou tilt invertido.

Na importação de panorâmica automática, `panorama.py` registra `source_actuator_geometry_unavailable`. `_target` e `activate` recusam o caminho. O solver visual não preenche silenciosamente o perfil antigo.

**Falta aprender ou substituir a função entre comandos/estado do motor e direções ópticas.** Rotações relativas inferidas de fotos não dizem, sozinhas, qual comando reproduz uma orientação.

Hipóteses de pesquisa, ainda não implementadas:

- Identificação de modelo mecânico com telemetria confiável e excitação observável por eixo.
- Modelo local aprendido de resposta a comandos, com condicionamento, limites e atualização após inversão/zoom.
- Navegação por referências visuais e correção em malha fechada, sem depender de coordenadas absolutas.
- Uso visual do mapeamento desacoplado da certificação de apontamento, sem permitir projeção de detecções em pose desconhecida.

Qualquer alternativa precisa tratar atraso, zona morta, folga mecânica, acoplamento entre eixos, pan cíclico, presets imprecisos e mudanças ópticas. Não assumir que todos os equipamentos tornam o mesmo modelo identificável.

## 11. Evidência física mais recente

Equipamento: **Reolink TrackMix PoE**, firmware `v3.0.0.5428_2509171972`. Fonte `camera_reolink_frente / wide_main`, imagem 3840 × 2160, perfil/configuração/nó `000`.

Nos ensaios, pan/tilt/zoom normalizados não forneceram uma pose completa utilizável. Havia pan nativo; tilt/zoom permaneciam desconhecidos. Destinos de trabalho e original usaram presets. O caminho da fonte admitiu fallback direto para o mesmo perfil quando o relay falhou, sem editar permanentemente a configuração.

As tentativas recentes foram iniciadas pela interface do Toposync em 5174. **Nenhuma pose ou costura foi otimizada manualmente pelo agente.**

| Job | Fotos / cobertura | p95 visual em 960 | Retorno original | Conclusão |
| --- | --- | --- | --- | --- |
| `4f1992bfb7c74a1391c17f1e629c08ff` | 12; somente faixa inicial | 1,731 px | 5,878 px | Cursor 3; referência de recuperação errada; continuação acrescentou zero fotos |
| `399375323fb14091b022c70a55f96343` | 1; sem artefato montado | Não aplicável | 3,552 px | Primeiro cursor 4; `working_reference_changed`; causa específica sem diagnóstico suficiente |
| `c9c653a335ed48b6939f6c01c50233e1` | 11; zero faixas completas | 2,094 px | **2,321 px, confirmado** | 145,02 s; recuperação de trabalho confirmada; descoberta da decisão temporal prematura |
| `fd6bfe95d23c49849e976eee3db42759` | **12: dez na faixa 0; duas conexões na faixa 1; zero faixas completas** | **2,132 px** | **4,011 px, não confirmado** | Código final; 222,24 s; ambos os ramos verticais encerrados |

Diferentes capturas/correspondências impedem tratar essa tabela como comparação controlada de qualidade ou velocidade.

### Último artefato

Identificador: `e3693725936741cab21c90f2be7bc3d6`. Montagem 4096 × 2048; 12 fotos usadas; 48 pares verificados; 1.240 observações de verificação e 5.228 de ajuste. Condicionamento intrínseco reportado: aproximadamente 21,78; lente monotônica no domínio verificado.

Cobertura da máscara: **19,9678% dos pixels do canvas equiretangular**. Não significa 19,9678% do alcance físico, nem fração equivalente de ângulo sólido. O denominador inclui direções nunca alcançadas; pixels equiretangulares têm pesos angulares diferentes por latitude.

Estado: montagem visual `ready`, aquisição incompleta, posicionamento `not_validated`, câmera parada, retorno não confirmado. Há imagem útil, não panorâmica total certificada.

### O paradoxo aparente do movimento

Diagnóstico contém `motion_not_observed` com comparação terminal verificada e deslocamentos de aproximadamente **67,522**, **35,015** e **15,971 pixels**.

Esses números medem pares inicial/final. O detector exige evidência temporal distribuída, causal e seguida de estabilidade. Um par diferente não prova sozinho que todas essas condições ocorreram. Ainda falta explicar por que a sequência não passou.

Outro par foi recusado por `correspondences_not_distributed`: candidato com 120 inliers, seis células e fecho convexo de **0,113166** da área; o scanner exige 0,12. Outras alternativas tiveram suporte menor. Isso demonstra o gate que recusou o par, não que baixar o gate seja correto.

### Dados faltantes e preservação

Foram preservados oito pares rejeitados, cursor e telemetria limitada. Não há vídeo completo sincronizado de cada tentativa para reproduzir todas as decisões. Diagnósticos mantêm até 32 tentativas e 128 amostras por tentativa; registros antigos podem sair da janela.

O ponto planejado para testar Parar/Continuar era completar outra faixa. Não ocorreu. Portanto, **interromper e continuar com novas fotos no mesmo job não foi validado fisicamente** nesta rodada.

Na conferência de 10/09: 14 presets anteriores intactos; dois novos presets originais retidos porque o retorno exato não foi confirmado. Trabalho encerrado, sem jobs ativos. Configuração, exceto metadados esperados de panorâmica, e composições preservadas. Frontend 5174/backend 8100 disponíveis naquele momento; disponibilidade não foi reconsultada para este dossiê.

### Outras câmeras e marcas

Corredor e Quintal têm capturas históricas. Não foram revalidados nesta revisão. Uma costura histórica da Corredor utilizou 94 de 123 fotos e permaneceu parcial; dados antigos não certificam aquisição atual nem toda uma marca.

Reolink, Tapo/VIGI, Axis, Hikvision, Dahua, Hanwha, Uniview, Bosch e Amcrest figuram como candidatos à matriz de interoperabilidade. **Não são uma lista de marcas aprovadas.** O objetivo deve ser cobertura de classes de capacidade e comportamento, seguida de evidência por modelo/firmware.

## 12. O que os testes já provaram

Resultados registrados em 10/09, não executados novamente neste dossiê:

- Gate conjunto: **401 testes Python**. A suíte da API passou depois com **127**, incluindo cinco novos casos. Mais **195** testes de consumidores/controle/estabilidade. **601 casos distintos** nesses conjuntos finais; não somar 401 + 127 + 195.
- Navegador: 25 cenários da fonte, 12 de mapeamento; cinco verificações direcionadas após limpeza com sobreposição ao conjunto anterior.
- TypeScript, Ruff, build e verificação dirigida de espaços aprovados. Build mantém aviso conhecido de tamanho dos arquivos.
- Cobertura inclui referências separadas, intenção de preset, resposta perdida, propriedade, limpeza, espaços/eixos ONVIF, zoom, cancelamento, cursor, orçamento, conflitos de revisão, máscaras e previsões.
- Regressões temporais usam cenas sintéticas e deslocamento visível; não apenas confirmação de chamada a mock.

Esses testes sustentam contratos e regressões. Não demonstram distribuição real de latência, paralaxe, textura noturna, repetibilidade de presets, precisão métrica, cobertura total ou facilidade para usuários.

**Critério ausente:** demonstração contínua do objetivo do usuário, da geração automática ao resultado utilizável, com limites de qualidade definidos antes do ensaio.

## 13. Problemas abertos e hipóteses falsificáveis

| Problema confirmado | Hipótese a testar | Experimento discriminante proposto |
| --- | --- | --- |
| Diferença inicial/final sem transição qualificada | Consumidor perde a transição usando apenas última imagem | Replay da sequência completa versus amostragem equivalente à execução; registrar descartes e decisões |
| Mesmo sintoma | Fluxo óptico mantém consenso quase imóvel; recuperação SIFT só entra quando o rastreamento falha | Registrar rastros/modelos; executar ambos os classificadores sobre os mesmos quadros, sem alterar a decisão online |
| Mesmo sintoma | Lacuna de tempo/geração invalida âncora e causalidade | Linha temporal de resets, imagens e comandos; localizar primeiro evento que elimina a evidência |
| Parede/textura restrita interrompe exploração | Modelo dominante representa região insuficiente ou paralaxe | Comparar suporte, profundidade aparente, máscaras e modelos alternativos no mesmo corpus |
| Retorno oscila perto do limite | Preset tem dispersão mecânica; direção de aproximação influencia | Repetições com origem, aproximação e óptica controladas; medir distribuição, não uma chegada |
| Panorâmica automática não ativa | Falta relação observável entre atuador e raios | Avaliar identificabilidade por classe de capacidade antes de escolher solver/servo |
| Usuário termina com resultado parcial sem caminho útil | Captura total como pré-requisito concentra risco e tempo | Comparar fluxo atual com região útil progressiva e uso visual separado do apontamento |

As hipóteses não são mutuamente exclusivas. Escuridão, rede e falha mecânica não foram estabelecidas como causa única. Medir CPU, latência e frequência de amostragem é relevante; atribuir causalidade sem registro sincronizado não é.

### Evitar na próxima investigação

- Aumentar tolerâncias apenas para aprovar a Frente.
- Interpretar `ready`, comando aceito, câmera parada e retorno restaurado como sinônimos.
- Trocar o montador antes de demonstrar que ele é o gargalo dominante.
- Acrescentar testes que reproduzem apenas a implementação e não o fenômeno físico.
- Comparar p95 de corpora diferentes como prova isolada de melhora.
- Reintroduzir 18 campos, tabelas de ângulos por modelo ou intervenção de IA em cada movimento.
- Prometer “qualquer câmera” sem descrever capacidades mínimas, impossibilidades e degradação explícita.

## 14. Pesquisa que pode destravar a próxima decisão

Esta seção é **proposta**, não novo plano aprovado nem trabalho executado.

### Primeiro: tornar a falha reproduzível

Uma nova captura curta e delimitada deveria preservar a sequência suficiente para replay: imagens antes/durante/depois, horários de comando/Stop, geração, sequência, recebimento/publicação, PTS quando disponível, estados do detector, modelos, suporte, resets e transições do cursor.

Preservar captura original e decisões rejeitadas, com limites de armazenamento. Sanitizar endpoints/credenciais. O replay não pode usar o resultado final como informação disponível no passado.

Comparar no mesmo corpus: detector atual; observação com buffer temporal; detecção multiescala; evidência por referência anterior ao comando; tratamento explícito de latência. Medir falsos aceites de imagem antiga/móvel e falsas recusas de imagem utilizável. Não otimizar apenas tempo médio.

### Segundo: decidir o modelo e a fronteira do produto

Comparar poucas alternativas completas, incluindo a atual corrigida:

1. Aquisição visual com referências e estados temporais mais observáveis.
2. Identificação de atuador onde telemetria torna isso possível; navegação visual onde não torna.
3. Experiência por região útil progressiva, com panorâmica parcial utilizável e expansão posterior.

Perguntas decisivas:

- Quais parâmetros de lente, pose e atuador são identificáveis com os dados disponíveis? Qual movimento de calibração os torna observáveis?
- Quando rotação central é suficiente? Como detectar que paralaxe/modelo de lente a invalida, antes de confiar no chão?
- Como estimar incerteza espacial, sobretudo perto do horizonte, e comunicar uma região útil sem formulário técnico?
- Captura total precisa preceder todo mapeamento? Pode haver valor seguro na imagem/planta antes de habilitar o motor?
- Como preservar independência dos pontos de verificação diante de sugestões visuais?
- Qual evidência distingue limite mecânico, volta completa, textura insuficiente, comando ineficaz e vídeo atrasado?
- É possível retomar por referências locais e grafo de vistas sem manter presets? Qual custo/risco comparado ao caminho atual?
- Quais classes de câmera exigem capacidade adicional ou assistência mínima? Que assistência tem menor esforço que preencher geometria?

### Validação proposta para uma decisão revisada

Definir critérios antes de implementar. Separar quatro resultados: imagem, cobertura, correspondência com a planta e motor.

- **Replay:** reproduzir a falha preservada; demonstrar qual decisão muda e por quê; testar também cenas que devem continuar recusadas.
- **Captura física:** escolher áreas esperadas verificáveis; percorrer ambos os lados e alturas úteis; reportar lacunas e término de cada ramo.
- **Continuação:** interromper depois de uma foto válida, sem depender de completar uma faixa difícil; retomar o mesmo job; acrescentar fotos sem repetir comando incerto ou renovar orçamento.
- **Retorno:** repetir chegadas de diferentes direções; medir distribuição do erro, tempo, zoom e falhas. Quantidade de repetições e tolerância devem refletir o uso, não ser escolhidas após observar o resultado.
- **Mapeamento:** pontos físicos identificáveis e separados entre ajuste/verificação; medir erro no plano, inclusive bordas e regiões próximas do horizonte. Escala da planta precisa ser conhecida.
- **Apontamento:** alvo previsto somente pelo algoritmo; sem correção manual; imagem fresca confirma posição do alvo em relação ao centro. Variar direção de aproximação e regiões do percurso.
- **Interoperabilidade:** ensaio por classe de capacidade, modelo e firmware; anúncios do protocolo separados de operações verificadas.
- **UX:** participantes completam tarefa sem suporte técnico; observar tempo, retomada, erros de ponto, entendimento de parcial/bloqueado e confiança compatível com a evidência.

Conservar controles, exclusão do atuador, Stop, limites e retorno. Não transformar diagnóstico em varredura longa repetida antes de existir hipótese nova.

## 15. Fontes locais e pontos de entrada

### Código

- [Captura, identidade e destinos](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama_capture.py): frames, `save_return`, original/trabalho, propriedade de presets.
- [Scanner](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama_scan.py): `_match`, `_move`, `_find_reference`, `_return_to_reference_band`, `_restore`, cursor/faixas.
- [Estabilidade](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/panorama_stability.py): `StabilitySettings`, `VisualStabilityDetector`, recuperação de transição.
- [Vídeo e timestamps](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/frame_grabber.py:161): `CaptureFrameSample`, `_LatestFrameBuffer`, backends OpenCV/FFmpeg.
- [Reconstrução](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/panorama_reconstruction.py): `_intrinsics`, `_fit`, correspondências, observabilidade, renderização.
- [Fonte e API](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/source_panorama.py): persistência, `_can_resume`, revisão, crop, fila e limpeza.
- [Calibração na composição](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama.py:795): importação e `physical_blockers`; `_target` em 1254; `activate` em 1422.
- [Geometria da panorâmica](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/panorama_mapping.py): raios, `_solve_rays`, `estimate_panorama_mapping`, inversa e região válida.
- [Mapeamento legado e chão](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/mapping.py): homografia, refinamentos e `GroundPlaneMapper`.
- [Propagação de calibração](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/processing/visual_calibration.py), [resolução de vistas](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/view_resolver.py) e [consumidores](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/pipelines/postprocess.py).
- [Controle PTZ](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/ptz_controller.py), [ONVIF](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/onvif/client.py) e [adaptador Reolink existente](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/onvif/reolink_cgi.py).
- [UI da fonte](/Users/c/Projects/toposync-2/extensions/cameras/ui/src/settings/CameraSourcePanoramaSection.tsx), [wizard](/Users/c/Projects/toposync-2/extensions/cameras/ui/src/elements/CameraPanoramaMappingModal.tsx) e [prévia bidirecional](/Users/c/Projects/toposync-2/extensions/cameras/ui/src/elements/panoramaProjection.ts).

### Evidências prioritárias

- [Relatório final de implementação e ensaios](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/README.md).
- [Tentativas físicas, projeção compacta](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/physical-attempts.json).
- [Regressão temporal antes/depois](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/timing-regression.json).
- [Manifesto do código executado](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/final-code-manifest.json).
- [Estado final e preservação](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/final-state.json).
- [Relatório do artefato final](/Users/c/Projects/toposync-2/.toposync-data/runtime/cameras/source-panorama/artifacts/e3693725936741cab21c90f2be7bc3d6/report.json), [modelo](/Users/c/Projects/toposync-2/.toposync-data/runtime/cameras/source-panorama/artifacts/e3693725936741cab21c90f2be7bc3d6/model.json), [imagem privada](/Users/c/Projects/toposync-2/.toposync-data/runtime/cameras/source-panorama/artifacts/e3693725936741cab21c90f2be7bc3d6/panorama.png) e [máscara](/Users/c/Projects/toposync-2/.toposync-data/runtime/cameras/source-panorama/artifacts/e3693725936741cab21c90f2be7bc3d6/coverage.png).
- [Gate Python](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/automated-final.xml), [consumidores](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/consumers.xml) e [navegador da fonte](/Users/c/Projects/toposync-2/ignore/panorama-reference-implementation-20260910/browser-source.json).
- [Plano vigente e histórico](/Users/c/Projects/toposync-2/docs/plano-panoramica-automatica.md) e [registro de implementação](/Users/c/Projects/toposync-2/docs/panoramica-automatica-implementacao.md). Seções antigas nesses arquivos não representam automaticamente o código atual.

Testes focados ficam em `tests/test_camera_panorama_{scan,capture,stability,reconstruction,mapping,api,runtime}.py`, `tests/test_camera_source_panorama_api.py`, `tests/test_camera_onvif_panorama_capabilities.py` e `e2e/source-panorama.spec.js`.

Contexto histórico de agosto foi recuperado também do resumo local do dossiê anterior. Suas falhas, números e propostas não foram promovidos a resultados atuais. A identidade do último artefato foi confirmada nos dados: **Frente**, não Corredor.

## 16. Prompt para a IA de pesquisa

> Pesquise alternativas para destravar a panorâmica e a calibração PTZ do Toposync usando este dossiê como estado inicial. Não pressuponha acesso aos links locais. Peça apenas os artefatos realmente necessários para discriminar hipóteses.
>
> Separe aquisição temporal, navegação/cobertura, reconstrução visual, mapeamento no chão e identificação/controle do atuador. Distingua defeito demonstrado, hipótese, limite de observabilidade e decisão de produto.
>
> Priorize explicar a diferença entre pares inicial/final deslocados e transição temporal não qualificada. Considere os backends sem timestamp de exposição e o consumo da última imagem. Não afirme causa sem experimento que possa refutá-la.
>
> Avalie também a ponte ausente entre rotações visuais e comandos PTZ. Mostre quais parâmetros podem ser inferidos, quais exigem referência externa e quais câmeras não permitem a mesma solução. Não converta coordenadas ONVIF genéricas em graus sem evidência.
>
> Compare no máximo três alternativas principais com fontes primárias, hipóteses matemáticas, esforço de integração, custo computacional, riscos e degradação por capacidade. Pode questionar a necessidade de panorâmica total antes de gerar valor ao usuário.
>
> Preserve zero campos geométricos obrigatórios, wizard por ponto, verificações independentes, prévia bidirecional, recuperação, recorte, isolamento entre lentes e proteção dos mapeamentos existentes. Nenhuma IA pode ajustar manualmente o motor no uso normal.
>
> Entregue: diagnóstico com nível de evidência; tabela de observabilidade/capacidades; experimentos mínimos com dados e critérios prévios; escolha recomendada com motivos para rejeitar alternativas; fluxo de UX; plano incremental que permita interromper uma abordagem sem perder dados ou quebrar o restante.
>
> Não proponha apenas mais testes, mais tempo de espera, limites relaxados ou ajustes exclusivos para a Frente. Não trate imagem bonita, `ready`, câmera parada e precisão física como equivalentes. Conclua com a menor decisão que precisa ser resolvida antes de escrever mais código.
