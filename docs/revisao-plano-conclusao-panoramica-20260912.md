# Revisão local do plano de conclusão da panorâmica

Data: 12/09/2026. Autor: Mateus Calza.

Escopo desta revisão: leitura do plano externo e do código atual, mais recálculo aritmético de medidas já registradas. Sem alteração de implementação, reexecução de matching, ensaio físico ou nova consulta às câmeras. O documento externo é uma proposta técnica, não autorização para executar os comandos ou ensaios que descreve.

Plano recebido: [toposync-plano-conclusao-panoramica-auditado.md](/Users/c/Downloads/toposync-plano-conclusao-panoramica-auditado.md).

Base local: branch `main`, HEAD `0ca8d6bc0b61ad1e746a9c59b7476c6cda087cf3`, com alterações não commitadas e arquivos novos. A revisão considera o conteúdo do working tree, não apenas o HEAD. Fontes externas do documento recebido não foram revalidadas nesta revisão; as conclusões adicionais abaixo dependem do código e dos registros locais.

## Decisão recomendada

Adotar o caminho integrado com uma única transmissão para observação e fotografias. A primeira entrega continua sendo seis vistas úteis em duas alturas e três setores horizontais, incluindo chão, com fotografias preservadas, montagem aprovada, apresentação e recorte que sobrevive à recarga. Apresentar um candidato é suficiente; não remover a proteção do artefato ativo para facilitar esse aceite.

Não é necessário abrir outra pesquisa ampla antes da implementação. Os ajustes abaixo tornam o plano mais específico e corrigem uma conclusão experimental sem transformar esta revisão em uma aprovação física.

## 1. E15: defeito confirmado no script atual; atribuição histórica ainda limitada

Em `panorama_scan.py`, `_gray` reduz os dois quadros à largura `ANALYSIS_WIDTH = 960`. `_match` mede os deslocamentos nessa imagem normalizada e devolve `analysis_size`.

Em `comparator_scale_consistency.py`, `_pair` calcula `scale = source_width / 960`. `_summary` e `_same_measurement` dividem por esse fator os deslocamentos do caminho canônico. Isso aplica uma segunda conversão a valores que, sob o comparador atual, já estão em pixels de análise.

Fontes:

- [Comparador integrado](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama_scan.py:1300).
- [Conversão adicional no E15](/Users/c/Projects/toposync-2/docs/experimentos-panoramica-ptz/2026-09-12-e15/comparator_scale_consistency.py:29).
- [Relatório histórico](/Users/c/Projects/toposync-2/docs/experimentos-panoramica-ptz/2026-09-12-e15/report.json).

**Recálculo condicional:** comparando os valores brutos registrados na mesma escala, sem a divisão adicional e preservando a tolerância original, os 16 pares são consistentes pela política do próprio E15: 15 pares verificados por ambos os caminhos e um par recusado por ambos. Isso não significa 16 conexões válidas. O relatório histórico classifica apenas um par como consistente e 15 como inconsistentes.

Persistem diferenças reais entre as saídas: os 15 pares verificados têm contagens de inliers diferentes. A maior diferença bruta de deslocamento é aproximadamente 16,912 px, aceita pela tolerância relativa original de 35%. Portanto, a correção de unidade não prova equivalência exata dos pré-processamentos nem valida tolerâncias de apontamento.

O relatório não registra `analysis_size` para cada comparação nem o hash do comparador executado. Consequentemente:

1. Suspender a conclusão de que o E15 demonstrou 15 inconsistências segundo seu critério de aceitação.
2. Preservar o script, o relatório e o manifesto históricos; não sobrescrevê-los ao repetir a análise.
3. Corrigir o contrato de unidade em uma revisão identificada, incluindo tamanho de análise e hashes dos arquivos efetivamente executados.
4. Validar com um caso de escala conhecido e com as fotografias já preservadas, sem movimentação física. Distinguir novo resultado do que ocorreu historicamente.
5. Não ajustar tolerâncias do controlador com base nas grandezas derivadas inválidas.

O recálculo desta revisão fica em [revisao-e15-unidades-20260912.json](/Users/c/Projects/toposync-2/docs/revisao-e15-unidades-20260912.json). Ele reaplica aritmética a medidas existentes; não refaz SIFT ou homografias e não comprova retrospectivamente a versão carregada pelo experimento.

## 2. Seleção do percurso: tornar o caminho recomendado efetivamente alcançável

O plano recomenda observação contínua como caminho inicial e grade absoluta como contingência. O código atual faz o inverso quando encontra limites: `_acquire_panorama` tenta `_absolute_scan` e só faz fallback contínuo para `pilot_cycle_unconfirmed`.

Fonte: [seleção atual do scanner](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama_scan.py:6144).

Acrescentar à primeira entrega uma política interna explícita e persistida de objetivo/percurso. O fluxo normal de captura deve conseguir solicitar e executar a região bidimensional delimitada. Não basta implementar uma ramificação que a chamada normal nunca alcança. Não usar monkeypatch de constantes globais, nome de câmera ou endpoint de bancada como solução de produto.

A mudança precisa preservar leitura e reconstrução de trabalhos antigos. Cursor incompatível não autoriza reinício ou repetição automática de movimento. A conclusão da região delimitada deve ter estado próprio, sem marcar alcance total como completo.

## 3. Fotografia aceita: completar o vínculo entre quadro e evidência

`_accept` já grava a imagem e seu hash antes de persistir o cursor. Não é necessário criar outro armazenamento. Entretanto, o registro de captura mostrado nessa função contém sequência e geração, mas não `capture_instance`, embora o quadro fornecido por `PanoramaCamera.frame` possua esse campo.

Além disso, o caminho principal de `_move` procura `best_sequence` no buffer e, se não encontrar, usa `frame` como valor padrão. A escolha alternativa não fica explícita como requalificação da fotografia selecionada.

Fontes:

- [Seleção do quadro](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama_scan.py:2492).
- [Persistência da captura](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama_scan.py:2624).
- [Identidade fornecida pelo adaptador](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama_capture.py:541).

A primeira entrega deve definir e testar: identidade composta por instância/geração/sequência; referência ao quadro que fundamenta a decisão; semântica dos horários preservados; e comportamento quando o quadro escolhido foi removido do buffer. A saída deve ser retenção do quadro qualificado, requalificação explícita de outro ou recusa. Testar os caminhos normal e de endpoint tardio.

“Fotografia original” significa o quadro aceito na resolução da transmissão, antes da redução analítica. O JPEG gravado atualmente é uma codificação desse quadro; não prometer identidade byte a byte com o stream comprimido da câmera.

## 4. Percurso vertical: mudança localizada, com teste de aceitação útil

`_continuous_scan` só passa de `pan` para `step` depois de concluir a busca horizontal. Isso confirma a dependência descrita na auditoria: uma lateral inconclusiva pode impedir a segunda altura.

Fonte: [ordem atual do percurso](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/panorama_scan.py:5062).

Testar a alteração com um cenário em que a lateral perde textura antes do limite, enquanto existe conexão vertical observável. O aceite deve comprovar fotos úteis nas duas alturas e preservação da pendência horizontal. Um contador de seis fotos ou quatro microdeslocamentos não comprova diversidade suficiente para a montagem.

## 5. Montagem: isolamento já existente

`SourcePanoramaService._reconstruct` cria um processo com `spawn`, observa timeout e cancelamento, e faz encerramento progressivo com `join`, `terminate` e `kill`. O plano deve tratar o isolamento como implementação existente a validar, não como infraestrutura a criar.

Fonte: [montagem em processo separado](/Users/c/Projects/toposync-2/extensions/cameras/src/toposync_ext_cameras/source_panorama.py:1926).

Permanecem necessários testes do timeout efetivo, encerramento do processo e preservação das imagens/artefato ativo. A existência desse código não comprova ausência de bloqueio em todos os callbacks ou condições do sistema.

## Ordem consolidada da primeira entrega

1. Congelar revisão e orçamento; reconciliar unidades do instrumento e guardar evidência histórica intacta.
2. Garantir seleção do percurso delimitado pelo serviço normal, com política e cursor persistidos.
3. Completar o vínculo quadro/evidência/arquivo e validar perda do quadro selecionado, troca de geração e escrita incompleta.
4. Obter a segunda altura sem depender da conclusão de ambos os extremos horizontais; comprovar o comportamento em integração.
5. Exercitar montagem, qualidade, candidato e recorte com reconstrução offline e teste de navegador.
6. Fazer o ensaio físico delimitado previsto no plano quando essa execução estiver autorizada e preparada. Exigir as fotografias, todas as regiões reservadas e o recorte persistido para aceitar a entrega.

Manter os tetos propostos pelo documento externo como propostas de protocolo, sujeitos aos limites mais restritivos existentes: 12 fotografias, 18 comandos incluindo sondagens/retorno e 240 segundos ativos na primeira entrega. Persistir consumo acumulado; falha, fallback e retomada não renovam o orçamento.

Checkpoint final: artefato aprovado para a região delimitada, ou diagnóstico específico com fotografias e estado preservados. Nenhuma dessas saídas autoriza chamar a panorâmica de cobertura total ou iniciar automaticamente a campanha de expansão.
