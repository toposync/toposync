# Panorâmica automática por transmissão

## Entrega de 10 de setembro: referências por capacidade e vídeo atrasado

Implementados destinos separados para o enquadramento original e a referência de trabalho, com propriedade de presets, reconciliação de criação e cursor versão 4. Descoberta conserva unidades, fonte, perfil, nó, configuração e zoom. A interface informa corretamente retomada indisponível e zero/uma/várias fotos; a limpeza explícita remove somente posições próprias dispensáveis, sem movimentar a câmera.

A validação real identificou uma segunda causa de cobertura incompleta: a janela sem movimento podia terminar antes de o movimento chegar no vídeo. A ausência agora usa o orçamento existente de 12 segundos; movimento observado seguido de estabilidade continua permitindo captura antecipada. Na regressão com atraso de 2,5 segundos, o Stop permanece em 0,6 segundo e a fotografia fica estável em 3,8 segundos. Nenhum limiar geométrico foi aumentado.

A verificação física permanece separada da automatizada. O segundo ensaio da Frente produziu 11 fotos e retorno confirmado em 2,321 pixels, mas não qualificou outra faixa vertical. O ensaio final, após a correção temporal, guardou 12 fotos, duas conectadas em outra altura, mas permaneceu parcial; a câmera terminou parada, com retorno em 4,011 pixels, não confirmado. Não foi atingida a faixa adicional completa necessária para o teste físico de interrupção e retomada. O fechamento está registrado no [relatório da entrega](../ignore/panorama-reference-implementation-20260910/README.md), com código, testes, imagens e limitações. As seções anteriores abaixo preservam o histórico e não substituem esse registro.


## Validação física em 10 de setembro

Após nova autorização do usuário, o Toposync principal executou um ensaio na Frente Reolink Wide e uma tentativa de retomada do mesmo trabalho. As 12 fotografias foram preservadas e montadas com erro visual p95 de 1,731 pixel em 960 pixels, mas o resultado permaneceu parcial. A referência qualificada da varredura difere do enquadramento original de retorno; a recuperação e a retomada não conseguiram validar essa referência. A faixa completa e a expansão vertical **não passaram no aceite físico**. Nenhum critério foi afrouxado.

A câmera terminou parada; retorno exato não confirmado. Configuração e composições foram preservadas e a aplicação ficou rodando em 5174. Outras câmeras não foram ensaiadas. [Relatório, causa e imagem original](../ignore/panorama-frente-live-20260910/README.md).


## Internacionalização em 9 de setembro

Panorâmica e wizard revisados em português do Brasil e inglês: erros e avisos traduzidos por código, singular e plural, números localizados, ajuda sem os antigos campos de coordenadas e rascunhos preservados na troca de idioma. Mensagens técnicas originais continuam disponíveis em detalhes recolhidos. A API acrescenta `issue_codes`, preservando `issues` para compatibilidade.

Validação direcionada: 6 testes Python, 2 testes dos catálogos e 4 cenários de navegador em ambientes sintéticos; TypeScript, Ruff e build aprovados. Nenhuma câmera real foi acessada. O build mantém o aviso de tamanho dos arquivos do webpack. [Manifesto e arquivos verificados](../ignore/panorama-i18n-20260909/validation.json).


Autor: Mateus Calza  
Data: 5 de setembro de 2026

Este documento registra a implementação do [plano de panorâmica automática](plano-panoramica-automatica.md). A panorâmica é um recurso visual da fonte de imagem; não ativa nem substitui a calibração de uma composição.

## Entrega de 9 de setembro: recuperação e wizard por ponto

Implementados os dois pacotes do plano com as estruturas existentes e sem novas dependências nesta entrega:

- O percurso contínuo recupera falhas localizadas nos dois sentidos, usa âncoras confirmadas e conserva lacunas. Uma faixa iniciada pelo centro exige evidência dos dois lados. O cursor versão 3 preserva intenções, tentativas e tempo ativo na retomada; cursores antigos não recebem uma promessa nova de continuação.
- A configuração da fonte mostra separadamente imagem, cobertura da revisão exibida e situação da câmera. A prévia enquadra a área fotografada, preservando recortes explicitamente salvos, imagem original e coordenadas canônicas.
- O editor oferece **Mapear usando uma panorâmica** e **Usar panorâmica salva**. Uma revisão canônica compatível origina o rascunho, sem captura adicional ou formulário de lente.
- Cada passo representa um par de pontos. É possível começar por qualquer imagem, navegar pelo identificador, manter vários pares incompletos, corrigir, remover e desfazer. O salvamento é confirmado pelo servidor; conflitos preservam as marcações para recuperação.
- A prévia bidirecional usa a projeção esférica/plana já existente, com seis inliers, distribuição, qualidade, máscara e suporte verificados. O navegador não faz requisições, ajusta o modelo ou movimenta a câmera durante hover. A marca fantasma é vazada; pontos salvos são sólidos. Conferências independentes não recebem sugestões.
- Teclado e toque têm mira e confirmação explícita. Eventos do wizard não alteram a composição ao fundo. Pan, zoom, rotação e ampliação de painéis preservam as coordenadas; uma previsão fora da janela pode ser trazida à vista por ação explícita.

A revisão e os testes encontraram e corrigiram recuperação inacessível após conflito de revisão, cancelamento incompleto após falha de salvamento, mudança da ordem dos rascunhos, propagação de teclas ao editor e prévias desatualizadas ou ocultas sobre marcadores.

### Verificação executada

| Conjunto | Resultado |
| --- | --- |
| Scanner e API da panorâmica da fonte | 205 testes Python aprovados |
| API do mapeamento e runtime | 61 testes Python aprovados |
| Geometria do mapeamento e elegibilidade da prévia | 35 testes Python aprovados |
| Recorte e projeção no frontend | 15 testes Node aprovados, incluindo vetores independentes produzidos pelo Python |
| Wizard por ponto no navegador | 11 cenários aprovados em execuções direcionadas; os quatro finais passaram após as últimas correções |
| Interface da panorâmica da fonte | 8 cenários aprovados em execuções direcionadas |
| TypeScript, build da extensão, Ruff e verificação direcionada de espaços | Aprovados; o build mantém o aviso de tamanho de assets do webpack |

Os navegadores usaram exclusivamente os fixtures sintéticos em portas próprias. Os cenários verificam também ausência de requisições de escrita e de comandos de câmera durante hover, conservação da composição durante teclado, fonte/revisão divergente, máscara ausente, recorte periódico, telas estreitas, temas e recuperação de falhas. O ensaio com participantes reais não foi realizado.

[Manifesto e hashes dos arquivos](../ignore/panorama-point-wizard-20260909/validation-manifest.json). [Prévia provisória em ambiente sintético](../.toposync-data/panorama-validation/evidence/preview-01-provisional-1440-day.png). [Primeiro par sobre panorâmica salva](../.toposync-data/panorama-validation/evidence/source-01-first-saved-pair-1440-day.png). Os logs de build e scanner foram preservados junto ao manifesto. Execuções que falharam durante o desenvolvimento não são apresentadas como aprovações; o aceite considera suas correções e repetições direcionadas.

### Limites e próxima validação

Por pedido do usuário, **nenhuma câmera real foi usada na validação desta entrega**. Não foi programado ensaio posterior. Alcance vertical real, retorno físico, repetibilidade e precisão de apontamento continuam sem novo aceite de hardware.

A geometria visual de uma panorâmica automática não fornece, por si só, a transformação para os eixos do motor. Um rascunho criado dessa fonte permite salvar pontos e explorar a projeção, mas mantém `source_actuator_geometry_unavailable` enquanto não existir geometria de posicionamento verificada. Conferência física, apontamento e ativação desse rascunho ficam bloqueados; o mapeamento ativo anterior continua preservado. Os caminhos legados mantêm seus contratos e foram testados com câmera sintética, sem afrouxar os critérios de ativação.

## Percurso do usuário

Câmeras → câmera → transmissão → **Gerar panorâmica**. O início exige somente essa intenção; não recebe campos de lente, ângulos ou limites. A interface informa que a câmera será movimentada, acompanha as etapas e permite sair e voltar. O servidor conserva o trabalho.

Ao terminar, a imagem completa continua guardada. **Selecionar área útil** permite dois cantos, arraste ou teclado; salvar o recorte não move a câmera e não refaz a montagem. O recorte pode atravessar a emenda horizontal de 360 graus. Exportação e visualização usam a mesma seleção.

**Parar** interrompe a aquisição sem iniciar um retorno inesperado. O retorno ao enquadramento inicial é uma ação separada após uma interrupção. Na conclusão normal, o scanner anuncia o retorno e verifica visualmente o enquadramento. Um comando aceito não basta para declarar que a câmera voltou ou parou.

Resultados incompletos são apresentados como parciais. Uma nova tentativa parcial conserva uma versão completa anterior como ativa e oferece o novo resultado como candidato. Mapeamentos e rascunhos antigos de composição continuam acessíveis; sua geração manual com campos ópticos foi retirada da interface.

**Repetir montagem** reaproveita as fotografias guardadas no mesmo trabalho, sem movimentar a câmera. Essa ação permite recuperar uma falha de processamento e possui cancelamento próprio. **Retomar captura** é outra ação: volta a usar a câmera para procurar imagens que faltam.

## Código e responsabilidades

| Arquivo na extensão de câmeras | Responsabilidade |
| --- | --- |
| `source_panorama.py` | API autenticada, fila, jobs persistidos, arquivos privados, cotas, revisão do recorte, publicação atômica e processo de reconstrução cancelável |
| `panorama_capture.py` | Capacidades, fonte óptica correta, frames distintos, controlador PTZ com exclusividade e retorno por posição ou preset pertencente ao job |
| `panorama_scan.py` | Piloto visual, passos e cobertura, captura incremental, interrupção, retomada e retorno |
| `processing/panorama_stability.py` | Movimento global, estabilidade, distribuição dos pontos, foco e evidência temporal |
| `processing/panorama_reconstruction.py` | Correspondências, lente e rotações estimadas, validação independente, projeção, mistura e procedência |
| `processing/panorama_mapping.py` | Geometria compartilhada; aceita rotações completas sem mudar a convenção antiga de pan/tilt |
| `ui/src/settings/CameraSourcePanoramaSection.tsx` | Estados e ações da panorâmica na fonte |
| `ui/src/settings/PanoramaCropEditor.tsx` | Seleção e exportação periódica, mouse, toque e teclado |
| `ui/src/settings/CameraPanoramaTelemetry.tsx` | Gráfico e tabela acessível da última observação de movimento, em Detalhes |

O core oferece `ConfigStore.update_extension_settings`, uma atualização síncrona sob o bloqueio existente que publica uma cópia validada, e um filtro genérico para configurações gerenciadas por extensões. A extensão protege `source.metadata.panorama` contra formulários antigos: salvar nome, origem ou outro metadado não apaga a referência ou o recorte publicados enquanto o formulário estava aberto. O digest de geometria legado não é afrouxado; editar o recorte não muda o perfil óptico antigo.

As bibliotecas são as já usadas no processamento de imagem: OpenCV e NumPy, com SciPy declarado na extensão e no lockfile para otimização robusta. Não há serviço de inteligência artificial, dependência de um agente ou treinamento remoto.

## Geometria e evidência

O ajuste reserva tracks visuais completos para validação. O mesmo ponto acompanhado em várias fotos não é repartido entre ajuste e teste. Estimam-se parâmetros ópticos limitados e rotações tridimensionais, com perda robusta e verificações de distorção monotônica, condicionamento e erro de reprojeção. Correspondências desconectadas não recebem ângulos inventados.

O suporte espacial de cada vínculo considera os quantis de 10% a 90% das correspondências nas duas imagens. Essa verificação impede que muitos pontos concentrados no relógio sobreposto e um único ponto distante aparentem cobrir a cena. Ela preserva vistas repetidas com pontos realmente distribuídos; movimento próximo de zero, isoladamente, não invalida uma correspondência. Três regressões cobrem esse defeito e os dois casos válidos.

O resultado é equiretangular, com modelo óptico, rotações, máscara de cobertura e índice da fonte geométrica dominante anterior à mistura multibanda. A orientação de apresentação é estimada automaticamente. Ela não é uma medição de gravidade, norte ou distância.

`positioning_status` permanece `not_validated`. A qualidade visual da montagem, a cobertura da aquisição e o retorno físico são evidências separadas. A imagem não prova altura da câmera, métrica do piso ou repetibilidade de um PT arbitrário.

A interface distingue falta de cobertura de alinhamento reprovado. `independent_alignment_error` apresenta um aviso explícito junto à imagem e no editor de recorte; outros motivos de revisão informam que a montagem não foi totalmente confirmada. Cada versão usa seu próprio diagnóstico. Recortar ou salvar a área útil não remove esses avisos nem aprova a geometria.

## Como a captura espera a câmera

O detector acompanha pontos distribuídos, verifica o fluxo óptico de ida e volta e estima uma transformação global robusta. Avalia o deslocamento antes de compensar a imagem: subtrair primeiro o movimento da câmera e medir somente o residual daria uma falsa parada. Pessoas, árvores e água não precisam dominar o sinal global.

Se um movimento rápido perder o acompanhamento local, o detector tenta recuperar a transição por correspondências SIFT mútuas com a imagem de referência. A homografia trata a mudança de perspectiva de um pan/tilt finito, mantendo os requisitos de quantidade, distribuição, proporção de inliers e erro geométrico. A recuperação comprova somente deslocamento; não estima velocidade nem aprova estabilidade. Uma nova janela do detector habitual ainda precisa passar. As buscas são limitadas a duas por segundo e a referência não atravessa descontinuidade temporal ou reinício do decoder. Mudança óptica continua reprovada.

Há duração mínima, janela de estabilidade, foco, deriva acumulada, limite de tentativas e timeout. Frames repetidos, sequência reordenada, mudança de decoder ou falta de textura não são aceitos como movimento zero.

A chegada informada por uma câmera não encerra um movimento absoluto sozinha. Primeiro é necessário observar movimento e estabilização; depois do Stop, outra janela confirma a imagem. Na validação do Corredor, o alvo apareceu no readback em aproximadamente 0,3 segundo e a primeira transição visual perto de 1 segundo. Isso não separa atraso mecânico de atraso do vídeo, mas demonstra por que interromper somente pelo readback perdia o tilt.

A chegada reportada usa uma tolerância fixa de 0,018 unidades nativas normalizadas. Essa comparação confirma apenas proximidade do comando: a câmera real arredonda as posições, portanto reduzir essa tolerância conforme a distância do comando produzia uma precisão fictícia e impedia capturas que já estavam visualmente estáveis. A tolerância não se converte em graus nem certifica apontamento. Comandos pequenos continuam exigindo movimento observado, e a confirmação de retorno conserva seu limite visual independente de 3 pixels na imagem de análise.

Quando uma tentativa expira, uma janela recente de frames distintos pode confirmar falta de textura distribuída. Nesse caso, o scanner registra a região como não resolvida e evita repetir o mesmo alvo sem informação nova. Não aceita a foto nem infere um limite físico pela ausência de textura. Até 16 observações rejeitadas ficam guardadas privadamente para diagnóstico, separadas das fotografias aproveitadas. Esse histórico usa imagens de análise em 960 pixels e compartilha o orçamento de 96 MiB com os frames integrais; a fotografia qualificada continua na resolução original. A redução evita que streams de alta resolução encurtem a janela temporal necessária. Se não houver evidência temporal suficiente, permanece o tratamento normal de tentativas.

O caminho atual de vídeo não fornece PTS confiável. Por isso existem dois modos explícitos:

- Com tempo de mídia válido, a velocidade pode ser calculada no intervalo de mídia.
- Sem PTS, somente para a panorâmica visual, usa-se deslocamento entre frames e deriva numa janela de observação local, após observar movimento e estabilização. `speed_px_s` fica ausente e a evidência registra `local_observation` e `visual_transition_verified`.

Esse segundo modo não elimina todo risco de atraso acumulado no transporte. Não altera `physical_timestamp_verified`, `geometry_safe` nem autoriza apontamento preciso. Guardar uma referência inicial de retorno também não equivale a aprovar uma captura geométrica.

A abertura inicial do vídeo tem orçamento total de 12 segundos. Uma indisponibilidade transitória pode usar o tempo que ainda resta, sem reiniciar esse relógio; erro de vínculo da fonte continua fatal. Na retomada, as fotografias dos movimentos iniciais continuam entre as referências consultadas mesmo quando a sequência cresce. A amostragem uniforme sozinha podia excluir justamente a vista para a qual a câmera havia retornado. Continuam necessárias duas vistas distintas, consistência entre correspondências e campo de visão compatível.

Em **Detalhes**, o gráfico conserva até 128 amostras da última observação concluída. Deslocamento e deriva usam pixels na imagem de análise; suporte visual tem escala própria. O eixo horizontal é o relógio local desde o comando, e os marcos distinguem resposta, chegada reportada, movimento visto e Stop. A velocidade só aparece quando existe tempo de mídia válido. Valores ausentes continuam ausentes, sem unir curvas através de lacunas; uma tabela permite consultar as mesmas observações com teclado e leitor de tela. O gráfico não simula atualização por frame.

## Persistência, recuperação e limites

Os arquivos ficam em `<data-dir>/runtime/cameras/source-panorama/`, separados em jobs e artefatos. Somente os endpoints autenticados servem imagens e relatórios. Caminhos arbitrários e nomes inválidos são rejeitados; falhas públicas não transportam URLs autenticadas ou credenciais.

A primeira implementação admite uma aquisição e um processamento pesado por servidor, com fila limitada. Há orçamento de 256 fotos e 20 minutos por aquisição, tentativas por posição e reserva de disco. O serviço limita armazenamento por job e global; não apaga a panorâmica ativa para abrir espaço. O processamento lê um original de cada vez e usa saída de resolução limitada, sem acumular todas as imagens de alta resolução em memória.

Reiniciar o servidor marca trabalhos ativos como interrompidos. Não move a câmera automaticamente. Retomar é deliberado e exige identidade óptica compatível e correspondências visuais coerentes. A retomada absoluta reutiliza o plano e revisita lacunas; sem coordenadas verticais, a retomada contínua conserva as fotos e reexplora o alcance após se relocalizar, podendo atingir o orçamento com cobertura parcial. O retorno por preset verifica token e nome ainda pertencentes ao job antes de mover ou excluir. Perda de controle impede que o scanner pare um novo proprietário.

O plano conserva índices visitados e correspondências entre vizinhos horizontais e verticais. Fotografias intermediárias podem ligar componentes quando há textura suficiente, com limite de subdivisões e reserva para os alvos ainda pendentes. Visitar um alvo não equivale a cobri-lo com uma imagem ligada à montagem.

A montagem pode ser repetida com 2 a 256 fotografias confirmadas, reutilizando o mesmo processo cancelável, as verificações de arquivos e a publicação atômica. Não adquire controle PTZ nem executa o scanner. Trabalhos antigos sem resumo de aquisição conservam cobertura não confirmada; reprocessá-los não os promove a uma captura completa. Com 256 fotos, a retomada de aquisição fica indisponível, mas a montagem e o retorno continuam independentes.

## Contrato HTTP

Todos os caminhos abaixo são relativos ao base path do Toposync, inclusive no ingress do Home Assistant.

| Método e recurso | Uso |
| --- | --- |
| `GET /api/cameras/cameras/{camera_id}/sources/{source_id}/panorama` | Ativa, anterior, candidato e último job |
| `POST .../panorama/jobs` | Iniciar; corpo contém somente `idempotency_key` |
| `GET /api/cameras/panorama-jobs/{id}` | Progresso persistido |
| `POST .../{id}/stop`, `/resume`, `/return` | Intervenções explícitas |
| `POST .../{id}/reconstruct` | Repetir a montagem das fotos guardadas, sem movimento |
| `PATCH .../sources/{source_id}/panorama/crop` | Recorte com `artifact_id` e `expected_revision` |
| `GET /api/cameras/panorama-artifacts/{id}` | Artefato e revisão atual do recorte |
| `GET .../{id}/files/{file_id}` | Arquivo privado autorizado |

O recorte canônico é `{u_start, u_width, v_start, v_height}`, normalizado no domínio original. A largura horizontal pode atravessar a emenda; a altura não ultrapassa os polos. Conflito de revisão entre abas conserva a seleção local e exige atualização explícita.

## Verificação reproduzível

Os testes unitários e de integração usam câmeras locais simuladas identificadas como tal. Os testes do scanner exercitam o detector real com sequência de imagens, além de erros de controle e persistência. Os testes de reconstrução verificam o modelo e regressões de geometria; não movimentam hardware.

```sh
.venv/bin/pytest -q tests/test_camera_panorama_stability.py tests/test_camera_panorama_scan.py tests/test_camera_panorama_capture.py tests/test_camera_onvif_panorama_capabilities.py tests/test_camera_panorama_reconstruction.py tests/test_camera_source_panorama_api.py tests/test_extension_settings_update.py
npx tsc --noEmit -p extensions/cameras/ui/tsconfig.json
npx tsc --noEmit -p frontend/tsconfig.json
npx playwright test --config playwright.source-panorama.config.js
uv lock --check --offline
uv build --offline extensions/cameras --wheel --out-dir ignore/panorama-validation-distribution
```

A configuração Playwright usa backend 8108, frontend 5178 e dados isolados. A fixture registra o plugin real e substitui somente aquisição/reconstrução por limites sintéticos identificados. Ela nunca usa a configuração principal nem controla uma câmera real. Evidências visuais ficam em `.toposync-data/source-panorama-validation`.

Os corpora privados de Corredor, Frente e Quintal permitem executar a reconstrução real sem novo movimento. Saídas automáticas ficam em `ignore/panorama-automatic-offline/`. Fotografias produzidas anteriormente com assistência validam a montagem automática; por si sós, não validam a nova aquisição automática.

Na reconstrução automática desses corpora, Frente e Corredor passaram pelo critério visual experimental, com erro de validação p95 de 2,376 e 1,904 pixels na escala de 960 pixels. Quintal permaneceu parcial: 26 de 28 imagens foram usadas e o p95 foi 16,518 pixels. Esses números não medem precisão de apontamento; o resultado do Quintal não foi promovido a completo por aparência ou por ajuste manual.

Na aquisição nova pelo Toposync, uma montagem com 45 originais do Corredor usou 36 fotos conectadas, com p95 de 4,780 pixels e resultado parcial. As paredes próximas eram brancas nos próprios originais; costuras e restos do relógio permaneciam visíveis. Um experimento de ajuste com 170 avaliações adicionais piorou o p95 para 4,902 pixels e não convergiu. Foi rejeitado, sem alterar o solver do produto nem seus critérios. O registro reproduzível está em `ignore/panorama-live-validation/solver-refinement-20260905/`.

O navegador passou 13 casos do fluxo novo, cinco casos de compatibilidade com o mapeamento legado, três verificações suplementares de parcial/estabilização, um caso do gráfico e dois de repetição de montagem. O gráfico usa 128 amostras com tempo de mídia ausente e verifica que a API não publica velocidade inventada. A recuperação verifica o mesmo trabalho e os hashes das fotografias, sem novos comandos de câmera, inclusive quando a resposta do início está pendente e a consulta da fonte falha. O editor e o diagnóstico foram inspecionados em 375 pixels; as evidências preservam também os defeitos encontrados antes das correções. Os testes de interface usam dados sintéticos identificados, não as câmeras da casa.

Outros três testes de navegador verificam os avisos de qualidade, sua persistência após salvar o recorte, a separação entre a versão ativa e a candidata e a continuidade de Stop/retomada. Evidências em `.toposync-data/source-panorama-validation/quality-review-recheck/`.

A revisão integrada da versão 9 passou 361 testes Python, incluindo compatibilidade do mapeamento, controle PTZ, autorização, persistência e reconstrução. Typecheck da extensão e do frontend, testes de recorte, Ruff, verificação do lockfile e geração do wheel também passaram. O pacote e os hashes dos módulos dessa validação estão preservados em `ignore/panorama-live-validation/build-v9/`.

Após a correção final do suporte espacial, os 58 testes de reconstrução e Ruff passaram. O wheel final foi gerado com os avisos de qualidade atualizados; seu manifesto, hashes e pacote estão em `ignore/panorama-live-validation/build-final/`. A montagem final foi solicitada pelo botão **Repetir montagem** na instância principal, reutilizando as 123 fotografias do Corredor. Nenhuma nova aquisição foi iniciada para essa etapa.

### Resultado final no Corredor

O trabalho `c0b55aa778694d44925f1cb9a3ba8f72` terminou com 123 fotografias guardadas. A montagem final `375e5000665943ccaf37889f7b9490b3` aproveitou 94 e deixou 29 desconectadas. Seu erro de validação p95 foi 4,132 pixels em 960 pixels de largura, abaixo do critério visual experimental de 8 pixels. Antes da correção, a montagem das mesmas 123 fotos tinha p95 de 28,532 pixels. O conjunto de correspondências mudou, portanto essa comparação não é uma avaliação pareada dos mesmos pontos nem uma medida de precisão física.

A imagem mostra a fachada, o corredor e o chão. Permanecem costuras, restos de relógio sobreposto e regiões sem imagem. O resultado continua `partial`, com revisão por orçamento do otimizador e fotografias desconectadas. A cobertura de 56,17% refere-se aos pixels do canvas esférico; não significa que essa porcentagem do alcance mecânico foi confirmada. A varredura não confirmou todo o domínio alcançável.

A câmera terminou parada, com Stop confirmado. O retorno exato ao enquadramento inicial não foi confirmado e a interface informa isso. A repetição de montagem não acrescentou fotografias nem modificou seus hashes. A máscara e os índices de procedência são coerentes; não há pixels pintados fora da cobertura ou regiões cobertas sem fonte.

Evidências finais: `ignore/panorama-live-validation/validation-final.json`. A entrega preserva a imagem original, sem retoques, em `ignore/panorama-live-validation/entrega/corredor-panoramica-toposync.png`, acompanhada de relatório e manifesto. A configuração, excluindo apenas a metadata da panorâmica, permaneceu igual ao baseline da versão 7. Alterações independentes de composição/configuração anteriores a esse baseline foram preservadas; não se afirma igualdade com a configuração do começo da sessão.

O percurso sem campos geométricos está implementado. O aceite de uma panorâmica total, com alinhamento e retorno confirmados em todas as câmeras, permanece pendente. Nenhum resultado parcial é certificado como calibração ou apontamento preciso.

Uma validação física deve iniciar o job pelo produto, guardar o identificador, acompanhar capturas/erros, comparar o enquadramento final ao inicial e conferir que configuração e mapeamentos permaneceram iguais, exceto pela metadata da nova panorâmica. Não ajustar lente ou poses em scripts durante esse aceite.

Estudo com usuários, benchmark em hardware mínimo e ensaios de chuva/noite dependem de evidência própria. Testes sintéticos, screenshots e montagem dos corpora não substituem esses ensaios.

## Correção da inicialização na Frente Reolink

A tentativa `cb71e1497c9e42d19a467edb31a6ca6e` encerrou a referência inicial após 12 segundos sem receber um frame. O diagnóstico autenticado encontrou erro 401 no relay configurado e vídeo direto do perfil ONVIF exato em cerca de 3 segundos, com resolução de 3840 × 2160.

A chave do leitor compartilhado identificava câmera, transmissão e backend, mas ignorava o transporte. Enquanto outro consumidor mantinha o relay aberto, a alternativa direta podia receber esse mesmo leitor indisponível. A chave agora inclui um hash da URL autenticada; conexões equivalentes continuam compartilhadas, e o endereço/credenciais não aparecem na chave. A configuração de acesso do usuário não foi alterada. A interface também deixou de afirmar que havia fotografias preservadas quando o total era zero.

O primeiro teste pelo produto confirmou o recebimento do vídeo e revelou perda da transição visual no tilt rápido. Essa evidência motivou a recuperação de perspectiva descrita acima. A regressão da conexão reproduziu o defeito antes da correção; passaram 33 testes de captura, serviço e snapshots, além de 102 testes de estabilidade e scanner. Typecheck, Ruff e geração do wheel passaram. O pacote e as evidências específicas ficam em `ignore/panorama-reolink-fix/`.

Uma tentativa posterior encontrou a parada automática do pulso concorrendo com o Stop do scanner. O adaptador agora admite até três tentativas de Stop, separadas por 100 milissegundos, quando o comando não pôde ser confirmado. Cada tentativa verifica novamente a propriedade e o fence; perda de controle interrompe a repetição. Dois testes adicionais verificam a disputa transitória e a recusa de parar um novo proprietário; os 22 testes do adaptador passaram após essa correção.

A varredura contínua usa velocidade normalizada conservadora de 0,1. A duração dos pulsos continua adaptada ao deslocamento e à sobreposição medidos. Isso reduz a perda de correspondência entre frames nos motores rápidos sem alterar os movimentos absolutos de câmeras como o Corredor nem afrouxar os critérios de fotografia estável.

As comparações de um possível limite estacionário são espaçadas e cessam quando já demonstraram mudança de enquadramento. Repeti-las a cada frame consumia tempo de processamento necessário à observação seguinte.

**Resultado anterior à investigação da janela temporal:** o vídeo e a referência inicial passaram a funcionar, mas a panorâmica da Frente ainda não era produzida. Uma tentativa guardou uma fotografia qualificada; outras confirmaram retorno ao enquadramento inicial. A tentativa `1b5edd460c894fb88cf2f6d54754db03` terminou com falha de estabilidade/transição durante a varredura, sem duas fotografias suficientes para reconstrução. A câmera terminou parada; o retorno exato dessa tentativa não foi confirmado. O histórico desses ajustes iniciais está em `ignore/panorama-reolink-fix/validation.json`. O resultado posterior está registrado abaixo.

### Investigação posterior: janela temporal e leitura de posição

Os diagnósticos mostravam frames com movimento de 0,007 pixel e deriva de 0,026 pixel, muito abaixo dos limites, ainda rejeitados como `settling`. A janela descartava amostras usando somente a duração de 0,8 segundo, mas exigia pelo menos cinco frames. Em cadências baixas, podia conservar apenas três ou quatro indefinidamente. A remoção agora preserva também o mínimo de cinco observações distintas. Os limites de movimento, deriva e duração não foram aumentados; uma cadência menor simplesmente precisa de uma janela mais longa. Contagem e duração também aparecem nos diagnósticos antes da aprovação.

Outra tentativa registrou movimento confirmado e, depois, uma lacuna de observação de 1,249 segundo que invalidou essa evidência. O scanner aguardava consultas de posição durante a leitura dos frames. Na Reolink, cada consulta nativa abre uma sessão, faz login, lê a posição e encerra a sessão. Movimentos contínuos e retornos por preset agora observam o vídeo sem essas consultas repetidas; a posição final continua sendo lida após a estabilidade. Movimentos absolutos conservam sua verificação de chegada.

Os testes reproduziram separadamente a janela incapaz de reunir cinco frames e a perda de observação causada por uma consulta lenta. Após as correções, passaram 107 testes de estabilidade e scanner, incluindo ambas as regressões.

O teste físico `8f12b713920f409490e9956abff067fa`, iniciado pela interface principal do Toposync, capturou 46 fotografias e produziu o artefato `96005bbaa7714fc890a93355d9e33a6b`. A montagem aproveitou todas as 46 imagens; o otimizador convergiu e o p95 de validação foi 2,119 pixels em 960 pixels de largura. A qualidade geométrica visual foi aprovada pelo critério experimental, sem ajuste manual de fotos, lente ou poses. Nas 26 tentativas aceitas conservadas no diagnóstico final, a cadência média de observação foi 11,38 frames por segundo.

O resultado continua parcial: os limites horizontais foram confirmados, mas a extensão vertical não foi totalmente confirmada. A câmera terminou parada; o retorno exato ao enquadramento inicial permaneceu sem confirmação. A imagem tem regiões ausentes, costuras e restos de relógio sobreposto. Esses limites são apresentados pela interface e não anulam a correção do bloqueio de captura, tampouco certificam apontamento PTZ.

Entrega: `ignore/panorama-reolink-window-fix/entrega/frente-panoramica-toposync.png`. O diretório conserva relatório, originais por hash, pacote, testes e manifesto. Máscara e procedência foram verificadas, sem pixels fora da cobertura. A configuração permaneceu igual ao baseline da investigação, excluindo apenas a metadata da panorâmica. A aplicação principal ficou disponível na porta 5174, sem trabalho de captura ativo ao final.

### Correção da continuidade vertical e da verificação final do retorno

O job `8f12b713920f409490e9956abff067fa` comprovou um erro de fluxo: o primeiro pulso vertical não mudou o enquadramento, mas a segunda tentativa produziu movimento estável de 102,86 pixels. O scanner descartava essa segunda fotografia e encerrava a varredura. Agora preserva a conexão verificada e inicia a próxima linha. Duas tentativas estacionárias continuam sem provar um limite quando não houve progresso vertical anterior.

Após uma falha de acompanhamento do retorno, o scanner já enviava Stop e observava uma janela nova. Faltava comparar essa imagem parada com a referência inicial. Agora essa comparação também pode confirmar o retorno e guarda a imagem e o relatório utilizados. Permanecem os requisitos de estabilidade, propriedade do controle e deslocamento máximo de 3 pixels. Nenhuma tolerância foi relaxada.

Três regressões falharam antes da correção. Depois passaram 113 testes de varredura e estabilidade, incluindo segunda tentativa vertical bem-sucedida, retorno após perda de acompanhamento, enquadramento incorreto e perda de controle. O pacote e as evidências ficam em `ignore/panorama-reolink-vertical-return-fix/`.

A repetição real do retorno pelo Toposync confirmou a câmera parada e correspondência com a referência, mas mediu diferença de 12,80 pixels. Por isso o produto manteve o retorno como não confirmado. Essa observação distingue a falha do acompanhamento da imprecisão física do preset; a correção não promete que o preset seja exato.

**Experimento rejeitado no teste físico, removido da versão final:** para presets com pequeno desvio visual, foi testado um refinamento com até seis pulsos locais de correção. Dois eixos são medidos pela imagem, sem depender de posição absoluta; a resposta inclui efeitos cruzados entre pan e tilt. O controle resolve o erro local, reduz a velocidade quando a duração mínima causaria excesso e compara novamente a referência após cada movimento. Pulsos têm duração entre 0,05 e 0,3 segundo e velocidade normalizada de até 0,025. O refinamento exige correspondência verificada, pelo menos 85% de sobreposição e desvio de até 30 pixels; resposta ausente, geometria mal condicionada, perda de controle ou cancelamento impedem novas tentativas. O limite de aceite permanece em 3 pixels. Fotografias de verificação do retorno não são qualificadas como novas capturas da panorâmica.

O teste real desse refinamento terminou com desvio de 23,22 pixels após três pulsos. A resposta aos pequenos comandos não permitiu confirmar a precisão. O código experimental, pacote, testes e observações foram preservados em `ignore/panorama-reolink-vertical-return-fix/refinement/`, mas o refinamento foi retirado do produto. A versão final mantém o retorno ao preset e sua validação visual, sem prometer compensação física precisa.

A nova captura `991cf845edf644cd890ae5e560c0921a` guardou 63 fotografias e avançou à segunda linha de altura, comprovando a correção vertical na Reolink. A captura foi interrompida com `control_lost`; a inspeção identificou que uma renovação pode ser recusada durante a parada automática do pulso, mesmo conservando o mesmo proprietário. A renovação agora admite até três tentativas, separadas por 100 milissegundos, sempre verificando a propriedade e o fence. Perda efetiva, falha persistente, estado de falha do controlador ou mudança de transporte encerram a recuperação. Dois testes reproduziram o comportamento anterior; quatro casos verificam recuperação transitória, novo proprietário, falha do controlador e falha persistente. A captura integral após essa última correção ainda não foi repetida.

O artefato `5fa374c115744a86a85cfd35bb4aa95d` aproveitou todas as 63 fotos. A avaliação visual experimental mediu p95 de 1,97 pixel em 5.080 observações de validação, a 960 pixels de largura. O resultado permanece parcial, com regiões ausentes e emendas visíveis; isso não certifica precisão física do PTZ. Foram conferidos os hashes dos originais, a máscara, a procedência dos pixels e a configuração, excluindo apenas a metadata da panorâmica. A imagem exportada está em `ignore/panorama-reolink-vertical-return-fix/entrega/frente-panoramica-toposync.png`.

A versão final passou em 139 testes de captura, varredura e estabilidade, Ruff, `git diff --check` e geração do wheel. O código experimental de correção fina não está nessa versão. O backend final usa `backend-reolink-final.log`; a interface principal permanece na porta 5174.

Na verificação final pelo Toposync, o retorno ao preset terminou com câmera parada e diferença de 7,98 pixels da referência. O retorno exato continuou não confirmado. A mensagem de perda de controle deixou de afirmar que outro controlador assumiu a câmera, pois uma renovação não confirmada também produz esse estado. Não houve trabalho de captura ativo ao encerramento.

## Percurso genérico a partir da faixa de referência — 5 de setembro de 2026

A captura sem posição absoluta passa a procurar uma referência observável e percorrer sua faixa horizontal antes de explorar outras alturas. A seleção usa movimento visual distribuído e translação no centro da imagem; não presume que tilt zero significa horizonte nem tenta reconhecer semanticamente uma rua. Sondas verticais são limitadas. Câmeras com limites absolutos conhecidos conservam o percurso por posições e priorizam a faixa mais próxima da referência; eixo vertical fixo permite captura horizontal.

O cursor versão 2 persiste faixa, sentido, limites observados e trechos concluídos. Na retomada, reconhecer uma fotografia antiga não basta: a vista atual também deve corresponder ao trecho pendente. Checkpoints contínuos anteriores conservam montagem e retorno, mas exigem nova captura para usar este percurso. A expansão reserva parte do orçamento para a outra direção vertical; falhas visuais recuperáveis preservam o resultado parcial e permitem tentar o outro lado. Permanecem os limites globais de 256 fotografias e 20 minutos; cada busca admite até 64 movimentos.

Movimento relativo tem comando explícito, com deslocamento normalizado e o mesmo lease/fence. Não é tratado como velocidade. O adaptador respeita limites publicados por eixo e recusa default ambíguo. A panorâmica desativa a conversão silenciosa de comando contínuo em relativo; controles existentes conservam a compatibilidade anterior. Perda efetiva de controle nunca permite nova aquisição automática.

A interface informa busca da referência, captura horizontal e faixas concluídas. O marco de referência concluída só aparece após evidência do percurso. Não foram acrescentados campos geométricos. Caminhos de imagens usados pelo checkpoint não fazem parte do relatório público de faixas.

Validação física restrita à Frente Reolink, transmissão Wide, conforme a instrução mais recente. A tentativa `c7614bd9c313492aa2f885a49eda8037`, iniciada pela interface principal, guardou nove fotos na altura da rua e parou por `motion_not_observed`. O consenso entre os enquadramentos antes/depois tinha 4 células e área convexa de 0,121; o detector exige 6 células e área de 0,2. Isso não autoriza reduzir os critérios. O resultado parcial e seus diagnósticos foram preservados. A câmera terminou parada, com retorno exato não confirmado.

A recuperação do percurso reduz velocidade e deslocamento após falha de observação, até duas reduções, e exige nova transição estável e ligação visual com o enquadramento anterior à falha. Uma tentativa recusada não vira fotografia nem prova de limite. As evidências desta revisão ficam em `ignore/panorama-generic-reference/`.

A tentativa seguinte, `6c44fe39333a4ee79886667e2158e3a9`, preservou dez fotos e mostrou outro impedimento: 24 imagens repetidas entre 119 observações apagavam a janela de estabilidade mesmo depois de o movimento ter sido observado. As reduções de pulso foram registradas, mas não bastaram. Esse resultado não foi aceito como faixa concluída. A retenção diagnóstica agora inclui até oito pares antes/depois de movimentos recusados, em resolução de análise, para permitir investigação local sem repetir capturas por falta de evidência.

Antes da correção de duplicatas, passaram 289 testes direcionados de captura, varredura, estabilidade, controlador, API e transporte, além de typecheck, Ruff e geração do pacote. O segundo artefato aproveitou as dez fotografias; hashes, máscara e procedência foram conferidos. A configuração permaneceu idêntica ao baseline, excluindo apenas metadata de panorâmica. Esses gates de integridade não substituem o aceite da extensão horizontal.

Na versão seguinte, duplicatas curtas deixaram de apagar a janela: elas não contam como amostras e não avançam o último quadro distinto. Mais de um segundo sem imagem distinta invalida a causalidade. A captura `2baee51c8fea4c2dabd10ac4d2239650` avançou até pan nativo 437, além do bloqueio anterior, mas encontrou uma parede próxima ocupando grande parte do quadro. O par rejeitado mostrou deslocamento real; não era prova de limite mecânico. Um pulso com velocidade reduzida foi ignorado, portanto a política final conserva a velocidade conhecida e reduz somente a duração. No máximo duas recuperações são permitidas, sempre com nova evidência visual.

Uma falha recuperável na primeira direção horizontal agora preserva essa borda como não confirmada, retorna à referência validada e tenta o outro sentido. Isso impede que uma região sem observabilidade suficiente bloqueie toda a área restante. A retomada pode explorar o outro lado se o enquadramento inicial for reconhecido com consistência entre imagens. O teste físico de retomada da captura acima foi recusado por `relocalization_required`; não houve avanço presumido ou perda das dez fotografias. O prosseguimento automático é validado numa nova execução pelo produto.

Após esse delta, passaram 192 testes de scanner, estabilidade e API. Outros 106 testes de captura, controlador e transporte já haviam passado em suas versões finais: 298 testes distintos no conjunto. Typecheck, Ruff, verificação do diff e empacotamento passaram. O aviso de retomada não recomenda mais um ciclo de retornar e tentar novamente sem evidência de que isso resolverá o enquadramento pendente.

### Resultado físico desta revisão

A execução `c9830a3b90174cae8a42b15a21d94702`, iniciada pelo Toposync na porta 5174, guardou 17 fotografias na altura de referência. Após uma falha de observação no primeiro sentido, retornou à referência e prosseguiu automaticamente pelo outro. A montagem `a42902171362489683dbd86ab76405a3` aproveitou todas as 17 fotos e mostra a rua continuamente nas duas direções, com paredes próximas nas extremidades. Permanecem costuras e restos do relógio sobreposto.

O alinhamento experimental passou com p95 de 2,46 pixels a 960 pixels de largura em 1.745 observações reservadas para validação. Isso mede correspondências da montagem, não precisão física do motor. O resultado permanece `partial`: não houve confirmação dos dois extremos mecânicos nem exploração vertical completa. A câmera terminou parada; o retorno exato não foi confirmado. A faixa não recebeu o marco de cobertura completa na interface.

Hashes dos originais, máscara, índices de procedência e configuração foram conferidos. Nenhuma configuração mudou fora da metadata de panorâmica. A retomada recusada preservou as dez fotos anteriores. Não há captura ativa; o Toposync permanece aberto na instância principal, porta 5174. A validação física ficou restrita à Frente Reolink; nenhum resultado de compatibilidade física é atribuído às outras câmeras.

Imagem original: `ignore/panorama-generic-reference/entrega/frente-panoramica-toposync.png`. Evidências e limitações: `ignore/panorama-generic-reference/validation-final.json`; manifesto e linhagem: `ignore/panorama-generic-reference/validation-manifest.json`. O aceite visual da extensão da rua melhorou e foi verificado. O aceite de captura total do domínio alcançável e da retomada física precisa permanece pendente.
