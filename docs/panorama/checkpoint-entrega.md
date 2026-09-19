# Panorâmica ao vivo — escopo separado, 19/09/2026

Implementação e contratos em [Panorâmica ao vivo](panoramica-ao-vivo.md). Projeção de fotografia, testes determinísticos, navegação real e teleobjetiva foram verificados. Observação diurna de 19/09 às 11:59 UTC confirmou alinhamento vivo intermitente: 47 alternâncias em 25 segundos, associadas a esperas breves do vídeo principal e invalidação imediata do registro. A causa da irregularidade do transporte e a inação dos cliques ainda precisam de validação causal; não há aceite de estabilidade ou apontamento físico. Nenhum deslocamento ou nova aquisição nesta observação.

Pedido vigente: somente diagnóstico e plano. Plano, tarefas, evidências, contadores e critérios adicionais estão na seção 17 do guia externo `/Users/c/Downloads/codex-goals/toposync-panorama-ao-vivo-goal.md`. Nenhum código de produto alterado nesta etapa de planejamento; os estados e limites da aquisição abaixo permanecem históricos.

---

# Checkpoint atual — calibração assistida, 18/09/2026

- Simulação adicional solicitada: oito pontos preenchidos pela interface, conclusão/ativação e três locais independentes projetados nos dois sentidos. Cenário sintético aprovado; erro máximo 1,423 mm no chão idealizado e 0,0109 pixel no sentido inverso. Mais 53 testes de processamento/localização aprovados. Não certifica a Frente real. Relatório: `tasks/calibracao-assistida/simulacao-completa.md`.

- Uma entrada “Calibrar câmera” no editor. A panorâmica compatível abre diretamente o assistente; rascunhos retomam as marcações. Sem imagem compatível, a preparação permanece no assistente e continua após validar a imagem publicada; captura exige ação explícita.
- Seis pontos de ajuste válidos, dois de conferência independente e “Concluir calibração”. Revisão, pontos extras, desfazer e diagnósticos ficam recolhidos. Resultado concluído compacto; apontamento físico é uma ação separada.
- Retomada idempotente valida composição, elemento, fonte, artefato e revisão. “Revisar calibração” cria rascunho separado, sem editar o ativo ou reutilizar o registro anterior protegido. Ativação e restauração mantêm os controles de conflito. Vistas antigas e trabalho mecânico paralelo preservados.
- Validação: 42 testes da API, 24 testes Node e 16 cenários Playwright aprovados; typechecks da extensão, frontend e plugin API aprovados; bundle da extensão compilado (aviso de tamanho já existente). Testes usam câmera sintética, não certificam cobertura nem apontamento físico.
- Chrome real: Frente Reolink abriu diretamente a panorâmica 4096 × 2048 e recuperou o par pendente do navegador. Nenhum ponto foi confirmado, nenhuma calibração ativada, nenhuma captura ou movimentação solicitada. Comparação do arquivo de configuração antes/depois: idêntico.
- Instância local reiniciada com autenticação normal, após verificar ausência de capturas em andamento. Evidências privadas: `ignore/calibracao-assistida-20260918/`. Plano, tarefas e validação: `tasks/calibracao-assistida/`.

---

# Histórico — navegação reutilizável, 18/09/2026

- `host.ui.NavigableViewport` disponível para conteúdo DOM, imagens, SVG e canvas. A planta compartilha os cálculos de rotação, arraste e zoom; suas ferramentas especializadas foram preservadas.
- Integrado na prévia de configurações, imagem de mapeamento e editor da área útil. Roda ancorada no cursor, arraste, pinça, teclado e ajuste à área. Navegar preserva as coordenadas; marcadores mantêm alvos de 44 pixels.
- Redimensionar após navegar conserva centro e escala absoluta. Botões, pontos e recorte continuam editáveis; dois dedos cancelam somente o gesto de edição em andamento. Previsões temporárias desaparecem ao iniciar navegação.
- 24 testes Node e oito cenários Playwright focados aprovados, incluindo mapeamento completo sintético, teclado, previsão bidirecional, cancelamento de recorte por pinça e payload canônico do recorte. Typechecks e builds aprovados; avisos de tamanho de bundles permanecem.
- Chrome real confirmou zoom pela roda, arraste e ajuste na Frente; editor abriu e fechou sem salvar. Compilação final recarregada. Sem novas capturas, comandos físicos, mudanças no recorte salvo ou no mapeamento real. Backend mecânico e trabalho paralelo preservados.
- Plano e evidências: `tasks/navigable-viewport/`. Contrato de reutilização: `docs/ui/navigable-viewport.md`. Esta entrega de interface não certifica cobertura nem retorno físico da câmera.

---

# Histórico — publicação integrada, 18/09/2026

- Reinício e tomada da instância expressamente autorizados pelo usuário nesta tarefa. Backend anterior 92014 encerrado; backend 29093 serve a porta 8100 a partir do checkout, com autenticação normal. Evidências e backup privado em `ignore/panorama-publication-restart-20260918/`.
- Comparação com `runtime-r07`: 37 arquivos Python idênticos; única diferença de backend é a publicação automática em `source_panorama.py`. Trabalho mecânico paralelo preservado. Nenhuma nova aquisição, montagem ou comando de movimento emitido nesta etapa.
- Frente: abertura autenticada promoveu `6c3a151fc83a4ccaacaac2ba4fa292b2` a ativo, preservou `0e436fe302b24d73a0c36a2d8737c370` como anterior e removeu a candidata. Revisão do ponteiro 77. Chrome confirmou a imagem nova 4096 × 2048 no editor e o botão “Usar panorâmica salva” habilitado; calibração existente preservada.
- Interface concluída compactada também quando existe retorno pendente: cobertura parcial e retorno não confirmado continuam explícitos; resultados detalhados e remontagem ficam em “Detalhes”. Isso não aprova cobertura nem apontamento.
- Validação: 274 testes de backend, três cenários Playwright (incluindo parcial com retorno pendente e promoção legada), três testes de traduções, typecheck e build aprovados. Confirmação adicional no Chrome real e configuração persistida em `verification.json`.

---

# Histórico — Corredor, continuação encerrada em 18/09/2026

- Uma aquisição autorizada e executada (1/1), dentro da janela 18:20:38–18:50:38 UTC. Tracking e patrulha desligados por confirmação humana; nenhuma configuração alterada pelo agente. Nenhuma operação nas outras câmeras.
- Revisão congelada 07, política 4; 38 hashes conferidos antes da captura. Código e trabalho paralelo preservados, sem nova correção ou repetição de testes válidos.
- Trabalho `9e309c08674c4ddb9395d048389d73a2`: referência e apoio inferior, duas fotografias, nenhuma faixa completa. Primeiro bloqueio: confirmação final do segundo tilt recusada por `fresh_frame_unavailable` / `endpoint_identity_unverified`, com troca de transporte e identidade de captura. Houve movimento observado; não é prova de ausência de efeito mecânico.
- Integral parcial `4e18a18380914c81b10c0bee5657ec26`, 4096×2048, máscara, originais e geometria preservados; reaberto pelo endereço do próprio Toposync. Artefato ativo anterior preservado; seleção do candidato no painel principal não demonstrada.
- Aquisição incompleta; reconstrução em revisão; referência preservada e estreito acréscimo inferior, sem expansão bilateral. Parada observada no encerramento; retorno pendente; controlador posteriormente UNKNOWN / geometry_safe=false. Sem aceite de cobertura ou prontidão.
- Resultado: `/Users/c/Downloads/codex-goals/toposync-panoramica-evidencias/20260918-corredor/resultado-corredor.md`. Ledger e validação no mesmo diretório. Rodada encerrada sem segunda aquisição ou nova investigação.

---

# Histórico — emenda de 18/09/2026

Guia canônico: `/Users/c/Downloads/codex-goals/toposync-panoramica-goal.md`.
Resultado e evidências: `/Users/c/Downloads/codex-goals/toposync-panoramica-evidencias/20260918-emenda/resultado-emenda.md`.

- Rodada adicional: 14:54:55–16:54:55 UTC, sem renovar limites por retomada. Ledger separado; Térreo 2/2, demais 0/1.
- Revisão07: qualificação existente de ausência de efeito precede análise tardia dispendiosa; teste de regressão e verificações direcionadas. Térreo exerceu a localização06, mas não adquiriu cobertura inferior/bilateral. Cinco originais preservados, integral parcial `b5a057bfb8b14587ab9221f387558f58`, reconstrução em revisão, parada observada e retorno pendente.
- Corredor depende da confirmação dos modos no aplicativo; Quintal recusou conexão TCP ONVIF. Garagem e Frente anteriores preservadas, sem nova captura. Não há aceite global.
- Trabalho paralelo de interface/publicação do usuário preservado. Backend local usa snapshot externo07 explícito; não transportar aprovação para mudanças do checkout que não foram executadas.

---

Histórico preservado abaixo:

# Checkpoint — pronto para aceitação física

15/09/2026. **Nenhuma câmera acessada ou movimentada nesta etapa. Nenhum processo do produto reiniciado. Nenhuma cobertura aprovada fisicamente.**

- Emenda autorizada registrada no contrato (§5 e §9): não procurar novamente o quadro histórico ausente. Causa visual da recusa histórica da Garagem desconhecida; reprodução negativa histórica não aprovada.
- Revisão base: `0ca8d6bc0b61ad1e746a9c59b7476c6cda087cf3`, com alterações locais preexistentes preservadas. Nenhum commit, staging ou reversão. Inventários antes/depois e hashes em `ignore/coverage-implementation-20260915/`.
- Implementação: `panorama_coverage.py` (novo), `panorama_region.py`, `panorama_scan.py`, `source_panorama.py`, `plugin.py`; interface em `types.ts`, `CameraSourcePanoramaSection.tsx`, `sourcePanoramaTranslations.ts`; testes em `tests/test_camera_panorama_coverage.py`. Documentos: contrato emendado, decisão, este checkpoint e `protocolo-aceitacao-fisica.md`.
- Política 4: referência integral → inferior → semente lateral → lado oposto → extensão lateral restante. Recusa local só permite outra região após localização qualificada. Ledger cumulativo v3, 24 fotos/30 comandos/420 s, reserva 5/150; sem retomada automática da rota interrompida. Históricos v1–v3 preservados.
- Primeiro par recusado conservado sem perdas (NPY), identidades/métricas/comandos, até 64 MiB de imagens e 1 MiB de metadados; indisponibilidade explícita ao exceder. Matcher, critérios geométricos, executores e reconstrutor preservados.
- Validação final: **70 testes direcionados passaram**, 702 fora do escopo, em `tests-final.txt`; TypeScript da extensão passou, `typecheck-result.json`. Inclui 28 casos da nova cobertura, publicação/recorte, incerteza, orçamento real de `_move`, localização, versões anteriores e guards de geração/frescura. Entradas de trajetória são simuladas. O teste positivo com originais da Garagem foi executado, não pulado. `unchanged-components.json` compara os símbolos reutilizados com o baseline anterior às edições.
- Montagem pronta e percurso concluído não aprovam cobertura. Integral, máscara, metadados e geometria seguem o formato existente; ativo anterior permanece e o novo fica candidato. A primeira publicação pode ser parcial explicitamente.
- Próximo passo único: [protocolo de aceitação física](protocolo-aceitacao-fisica.md), uma Garagem e uma Frente, somente após qualificação operacional. Pendências: cobertura nova bilateral/inferior real, preservação das regiões úteis, localização entre visitas, orçamento físico, coerência visual, publicação/reabertura e estados de parada/retorno/controlador. Não iniciar esse protocolo nesta etapa.
