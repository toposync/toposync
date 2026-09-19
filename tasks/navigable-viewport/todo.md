# Tarefas

- [x] 1. Extrair transformações, arraste e zoom. Aceite: âncora sob cursor e rotações preservadas. Verificação: testes de geometria. Arquivos: engine, teste, Viewport2D. Escopo médio; sem dependências.
- [x] 2. Componente genérico e host.ui. Aceite: mouse, dois dedos, teclado, resize e arbitragem de ferramentas. Verificação: typecheck e testes de navegador. Arquivos: componente, contrato público, App. Depende de 1; escopo médio.
- [x] Checkpoint: núcleo compila e geometria passa.
- [x] 3. Mapeamento da panorâmica. Aceite: zoom/arraste, pontos e previsão corretos após transformação, centro preservado na expansão. Arquivos: modal e estilos. Depende de 2; escopo pequeno.
- [x] 4. Prévia e recorte. Aceite: navegação não altera recorte; edição e cancelamento continuam funcionando. Arquivos: editor, seção, painel, ativação e estilos. Depende de 2; escopo médio.
- [x] Checkpoint: extensões compilam; contratos de coordenadas preservados.
- [x] 5. Validação. Aceite: regressões focadas e gestos passam, build publicado localmente e conferido no Chrome. Arquivos: testes de navegador, documentação de reutilização e checkpoint. Depende de 3 e 4; escopo médio.

## Evidências finais

- 24 testes Node: geometria compartilhada, projeção, recorte e traduções.
- Oito cenários Playwright aprovados: dois de navegação/recorte, três de mapeamento (fluxo completo, teclado e previsão) e três de publicação da panorâmica.
- Typecheck do núcleo, API pública e extensão; builds do núcleo e câmeras aprovados. Webpack mantém os avisos de tamanho dos bundles.
- Chrome real na Frente: roda ampliou de 1 para 1,588; arraste deslocou o conteúdo 80 × 40 pixels; ajuste restaurou o enquadramento. Editor abriu, ampliou e fechou sem salvar. Compilação final recarregada e conferida.
- Nenhuma nova captura, operação física ou alteração de calibração/recorte real. Outros trabalhos locais permaneceram intactos.
- Documentação: `docs/ui/navigable-viewport.md`.
