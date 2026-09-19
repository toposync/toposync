# Tarefas

- [x] 1. Retomada idempotente por câmera/composição/elemento/fonte/artefato; revisão separada do ativo. API, cliente e testes de API (3 arquivos). Aceite: duplicatas reutilizadas, incompatíveis recusados, ativo intacto.
- [x] 2. Entrada única e abertura direta. Editor, assistente, traduções (3 arquivos); depende de 1. Aceite: remover entrada antiga, retomar ponto pendente, abrir imagem existente sem confirmação redundante.
- [x] Checkpoint: testes de API e typecheck.
- [x] 3. Preparação integrada. Assistente e seção de panorama (2 arquivos); depende de 2. Aceite: captura explícita, reaproveitar serviço, avançar após validar artefato compatível.
- [x] 4. Marcação linear e conclusão. Assistente e traduções (2 arquivos); depende de 2. Aceite: seis ajustes, duas conferências, conclusão controlada pelo servidor; ações secundárias recolhidas.
- [x] Checkpoint: fluxo no navegador com câmera simulada.
- [x] 5. Revisão segura e estados dependentes. Assistente, editor e testes (3 arquivos); depende de 4. Aceite: ativo preservado, rascunho editável, conflitos mantidos, sem dizer que mapa pronto está ausente.
- [x] 6. Documentação e validação final. Documentação, checkpoint e testes focados (até 5 arquivos); depende de 3–5. Aceite: typechecks/builds e cenários relevantes aprovados, limites documentados.
