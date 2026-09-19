# Toposync: contrato de entrega da panorâmica automática

Atualização: 14/09/2026. Este documento é um complemento de projeto ao `dossie-codex.md`, não uma substituição do guia nem uma prova de implementação. As propostas abaixo precisam ser confrontadas com o código atual. Nenhuma câmera foi acessada nesta análise.

## 1. Objetivo que não muda a cada erro

Gerar pelo Toposync uma panorâmica automaticamente adquirida, abrangente, persistida e reabrível, incluindo expansão horizontal nos dois lados e região inferior observável. Excesso de cobertura é aceitável; o usuário recorta a área útil depois. Não deve precisar preencher geometria ou escolher manualmente cada posição.

Preservar originais, máscara, identidade óptica, comandos, observações PTZ disponíveis e o vínculo de cada fotografia com a geometria reconstruída. Diferenciar posição observada, comando solicitado e orientação estimada. Ausência de tilt, zoom ou timestamp físico não autoriza inventar valores.

Duas faixas, número de fotografias, metas 0,8/0,2 e `reconstruction=ready` não substituem esse objetivo. Duas faixas foram uma etapa de integração, não um limite de produto nem demonstração de cobertura adequada.

Entrega atual: eliminar a dependência de uma faixa unilateral perfeita para adquirir os outros lados e a parte inferior, mantendo a rejeição de conexões inválidas e os controles físicos.

## 2. Evidência atual e limites da análise

### Confirmado em arquivos do ZIP `region-v3-garagem.zip`

- `job-after.json`: aquisição incompleta; três vistas úteis, dois apoios, nenhuma faixa concluída; `vertical_extent=0`, `region_phase=first_row`; `coverage_connection_unverified`.
- `source-after.json`: direção horizontal -1; duração de pan registrada em 2 s; as outras regiões permanecem sem progresso.
- `result-summary.json`: seis comandos de aquisição, dois de retorno, cinco fotografias; 39,349 s ativos; 44,962 s até publicação.
- `source-after.json`: a reconstrução de cinco imagens foi aprovada pelo reconstrutor e o artefato parcial tornou-se ativo. O ativo anterior estava marcado `stale=true`, motivo `source_changed`. Isso não permite concluir que deveria ser reativado automaticamente.
- `control-after.json`: `state=idle`, `move_status=UNKNOWN`, `motion_state=moving`, `geometry_safe=false`. O relatório de retorno visual não deve sobrescrever silenciosamente esses estados.
- `panorama-served.png` e `reference-live.jpg`: há piso visível, mas não houve aquisição de uma faixa inferior. A panorâmica mostra uma região pequena, com estrutura/teto à esquerda. A referência original já contém bastante piso e região à direita; precisa participar da comparação de preservação, não apenas da avaliação de retorno.

### Relatado no README do ZIP, sem reexecução independente

O sexto comando, pan -0,1 por 2 s, terminou estável, mas a relação com a fotografia anterior foi recusada por `correspondences_not_distributed`. Um modelo candidato teve 63 inliers em cinco células, com `hull_fraction=0,102258`; outros modelos também não qualificaram. Esses números não provam que a ligação é válida nem identificam sozinhos a causa da rejeição.

A pré-verificação anterior exigia `freshness=physical` em um snapshot auxiliar. O caminho normal funcionou sem timestamp físico certificado, usando a qualificação visual existente. Não reabrir essa conclusão sem evidência nova.

### Histórico fornecido pelo usuário

- O reconstrutor atual produziu resultado satisfatório com as 14 fotografias assistidas da Frente.
- A aquisição automática v3 da Frente produziu duas faixas, mas foi de pan nativo 1227 a 372 e voltou a 1254. A rua à direita presente nas fotos assistidas de 1436/1580/1744 não foi fotografada.
- Os valores nativos são evidência daquele equipamento, não regras a codificar nem graus universais.

### Não consta neste ZIP

Os cinco originais de aquisição, a observação `transition-b96a02f168004b90b769690c37b10512.jpg`, `scan-manifest.json`, `scan-diagnostics.json`, `model.json`, a máscara integral e o código do scanner não foram incluídos. Os caminhos são citados no README, mas não equivalem a arquivos anexados. O Codex deve recuperá-los no repositório antes de modificar o verificador.

Não foi demonstrado se a correspondência falhou por oclusão, distribuição realmente insuficiente, repetição do portão/piso, passo excessivo, geometria local ou erro do instrumento. Não transformar nenhuma dessas hipóteses em requisito.

## 3. Diagnóstico de processo e mudança de unidade de trabalho

As correções de preparação, tilt, orçamento e pré-verificação resolveram problemas reais. Porém, a entrega foi fragmentada em sintomas: corrigir uma interrupção, repetir a aquisição e encontrar a próxima, enquanto a cobertura bilateral e inferior continuava adiada.

Nova unidade: comportamento integrado de cobertura, não apenas desaparecimento da próxima mensagem de erro. Uma entrega coerente pode envolver mais de um arquivo; menor mudança suficiente não significa menor patch possível.

Não adicionar outra exceção que só force `correspondences_not_distributed` a passar. Corrigir a decisão que transforma um bloqueio local de conexão em cancelamento de todas as regiões, quando houver alternativa operacionalmente qualificada.

Não impor artificialmente que apenas um arquivo pode mudar. Preserve o reconstrutor, executores e critérios geométricos como baseline; só abra exceção quando uma causa demonstrada nesses componentes impedir a entrega, com reprodução e justificativa explícitas.

## 4. Hipótese de solução: cobertura por regiões com alternativas limitadas

Esta seção é uma recomendação a avaliar, não uma descrição de código existente.

Manter no planejador um pequeno conjunto de regiões pendentes: lado esquerdo, lado direito e expansão inferior, vinculadas a vistas conhecidas. Os lados são relativos à referência e à orientação observada; o sinal nativo do comando não identifica universalmente esquerda, direita ou chão.

Adquirir evidência bidimensional cedo, antes de consumir o orçamento inteiro numa extremidade. Nenhuma prioridade unilateral pode tornar todas as outras regiões inalcançáveis por construção. A ordem exata deve aproveitar os caminhos e capacidades já existentes, sem novo framework de planejamento.

Ao recusar uma ligação:

1. Preserve a observação e a recusa. Não aceite uma transformação ambígua.
2. Diferencie falha geométrica local de perda de controle, efeito desconhecido do comando ou vídeo sem evidência suficiente.
3. Quando o estado e o caminho de recuperação forem qualificados, use uma referência existente e uma alternativa limitada: outra conexão visual válida, vista intermediária menor ou outra região.
4. A volta a uma região conhecida não precisa ser igualdade de pixel com a fotografia original. Precisa da localização e das condições operacionais exigidas para a ação seguinte.
5. Sem recuperação verificável ou com comando/parada incertos, encerre com evidência. Não navegar cegamente.

Conectar uma imagem ao conjunto não exige necessariamente conectá-la à fotografia imediatamente anterior. A geometria final deve ser sustentada por ligações verificadas. Reutilize verificadores e referências existentes; não crie uma segunda matemática em scripts auxiliares.

Os comandos absolutos/relativos podem apoiar aproximação e retorno quando a capacidade for demonstrada. Aceite do comando e coordenadas reportadas não substituem observação. Não adicionar regras por fabricante nem presumir que todas as câmeras oferecem o mesmo controle.

Esta política não exige SLAM, atlas geral, detecção semântica de paredes ou troca de algoritmo de stitching. Uma representação pequena de regiões, referências e orçamento pode ser suficiente; confirmar no código.

## 5. Critérios de aceitação que não podem ser substituídos por métricas fáceis

### Antes da implementação

Preparar um conjunto pequeno de referências de avaliação, com imagens conhecidas, para registrar quais regiões devem aparecer. Os marcadores servem ao teste; não viram configuração manual obrigatória do produto nem coordenadas hardcoded.

- Frente: preservar o chão e a região atual e incluir o trecho direito presente na referência assistida, sem apenas deslocar a janela e perder o lado oposto.
- Garagem: preservar as regiões válidas já visíveis na referência inicial e demonstrar expansão nova à direita e abaixo, quando fisicamente observável. Não contar o piso já visto como prova de nova cobertura inferior.
- Compatibilidade: usar a mesma política nas duas câmeras, com adaptação por capacidade, sem ajustes manuais por nome.

Área desconhecida ou oculta por parede não deve ser inventada. A avaliação distingue ausência de evidência, limitação observada e região realmente adquirida. Não inferir cobertura perdida apenas pela área preta do canvas ou por máscaras em referenciais diferentes.

### Testes locais obrigatórios da entrega

1. Região inicial central: adquirir cobertura dos dois lados e inferior, sem depender de terminar um lado inteiro primeiro.
2. Falha geométrica unilateral: manter a ligação inválida recusada; alcançar outra região somente após recuperação qualificada.
3. Caso espelhado: impedir solução dependente de sempre iniciar em -1.
4. Perda de controle, observação incerta e retorno não qualificável: não emitir novos movimentos indevidos.
5. Contabilidade: apoios, avanços, recuperações e retorno consomem o mesmo orçamento finito; não zerar contadores por mudança de região ou retomada.
6. Persistência e apresentação: parcial disponível não equivale a cobertura aprovada; referências e revisões de geometria não são substituídas silenciosamente.

Emenda autorizada em 15/09/2026: o par histórico negativo da Garagem não é requisito de reprodução nesta entrega, pois o quadro posterior não foi preservado. A causa visual exata permanece desconhecida; o teste histórico não está aprovado. Separar testes da política real com recusa simulada, regressões com geometria disponível e aceite físico posterior. Não inventar o quadro, alterar o matcher para passar ou repetir buscas nos locais já examinados. Os critérios finais de cobertura e as proteções operacionais permanecem iguais.

Replay testa as observações que foram realmente gravadas. Não prova imagens de trajetórias alternativas. Para essas decisões, usar simulação com ground truth conhecido e depois validação física limitada; identificar explicitamente a natureza da evidência.

### Ensaio integrado

Uma aquisição automática na Garagem e uma na Frente, sequenciais, usando a mesma versão congelada. Fazer preparação operacional normal e preservar sua proveniência; não esconder reposicionamento assistido.

Mostrar resultado integral, máscara, referências comparadas, regiões incluídas/ausentes, comandos e tempo. Não promover silenciosamente uma captura insuficiente a aprovação de produto. Uma primeira prévia parcial pode ser disponibilizada com esse rótulo; preservar os artefatos e os vínculos de calibração anteriores.

Um mesmo novo bloqueio reproduzido no lote não autoriza continuar capturando em todas as câmeras. Uma falha específica com encerramento seguro pode ser registrada sem impedir automaticamente um caso independente.

## 6. Execução do Codex sem outro ciclo interminável

Abra uma sessão nova quando o contexto antigo estiver impondo as decisões provisórias acima como requisitos. Leve este contrato, o guia fornecido e os caminhos das evidências; não cole toda a conversa nem transforme o guia geral em AGENTS.md.

Uma sessão, uma entrega atual: cobertura bilateral/inferior com tratamento de falha local qualificada. Reconheça o código, faça decisão curta, implemente, teste localmente e realize o ensaio autorizado. Não interrompa depois de cada microcorreção que ainda pertence a essa entrega.

Não confundir a autorização desta entrega com autonomia ilimitada:
- máximo de uma variante de política implementada por rodada;
- no máximo uma aquisição de aceitação por câmera autorizada, Garagem e Frente;
- se dados indispensáveis não existirem, uma coleta diagnóstica curta e explicitamente delimitada, dentro do orçamento de movimentos, somente se autorizada;
- sem repetir um mesmo ensaio com a mesma hipótese;
- no máximo uma revisão independente, somente leitura e com pergunta específica;
- descoberta fora da entrega vai ao backlog, salvo segurança ou integridade indispensável ao trabalho.

Não impedir alterações coerentes com a entrega apenas por atravessarem dois serviços. Não abrir reescrita geral, novo motor de reconstrução, firmware, credenciais, automações externas, apontamento preciso ou mapeamento físico nesta rodada.

A decisão deve justificar por que o próximo ensaio tem chance de cumprir o aceite, usando os testes. Se não houver alternativa segura demonstrável, declare qual capacidade falta. Não remova controles para produzir imagem.

## 7. Memória persistente e estados finais

Sugestão de destino no repositório: `docs/panorama/contrato-de-entrega.md`. Este arquivo foi criado fora do repositório; precisa ser anexado/copied pelo usuário ou salvo pelo Codex.

No AGENTS.md local, basta apontar para este contrato e registrar: cobertura não é `ready`; falta de ligação local não é automaticamente emergência global; histórico não muda por uma execução nova; princípios de segurança continuam válidos. Não duplicar as 57 seções do guia geral.

Atualizar um checkpoint curto com revisão, alterações locais, autoridade vigente, teste, evidência, regiões aprovadas e pendentes e o próximo passo. Não apresentar percentuais de conclusão nem deixar decisões provisórias como invariantes permanentes.

Resultado final separado em: aquisição/cobertura, reconstrução, disponibilidade, calibração de apontamento, parada/retorno e estado do controlador. A confirmação visual de retorno da Garagem não autoriza declarar `geometry_safe=true`.

## 8. Base documental

- Guia fornecido pelo usuário: `dossie-codex.md`, especialmente seções 5–7, 9–15, 16–19, 30–34, 46–50. É a base metodológica; suas recomendações de modelos não foram usadas como benchmark independente.
- ZIP atual: `region-v3-garagem.zip`; fontes de fatos locais indicadas na seção 2. O ZIP é uma seleção, não o repositório completo.
- Histórico da Frente: relatórios colados pelo usuário nesta conversa; conferir versões e origens ao recuperar no repositório.
- Documentação oficial Codex: https://developers.openai.com/codex/learn/best-practices (consulta em 14/09/2026). Instruções persistentes curtas, contexto específico e critério observável.
- Brown e Lowe, 2007, página do autor: https://mattabrown.github.io/autostitch.html . Montagem como correspondência entre múltiplas imagens, não necessariamente uma cadeia temporal. Isso não valida automaticamente uma navegação física alternativa.
- Especificação ONVIF PTZ: https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec.pdf . Sistemas de coordenadas e comandos não bloqueantes; aceite não garante pose ou parada física.

## 9. Etapa local autorizada em 15/09/2026

Implementar e validar localmente a cobertura bilateral/inferior e a preservação limitada da primeira recusa decisiva. Nenhuma câmera deve ser acessada ou movimentada nesta etapa. Encerrar no checkpoint pronto para aceitação física, com protocolo executável para uma aquisição na Garagem e uma na Frente. A autorização de ensaios anteriores não é autorização para executá-los nesta etapa.
