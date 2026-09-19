# Validação da calibração assistida

18/09/2026.

- `.venv/bin/python -m pytest -q tests/test_camera_panorama_api.py`: 42 aprovados. Retomada, incompatibilidade, revisão, ativação concorrente e proteção do registro anterior após restauração.
- `npx playwright test --config playwright.panorama.config.js`: 16 aprovados. Entrada direta, preparação integrada, imagem incompatível, sequência de oito pontos, revisão separada do ativo, teclado, rascunhos, falha de gravação, revisão concorrente, projeção provisória, idioma e ingress.
- `node --test frontend/tests/viewportNavigation.test.cjs extensions/cameras/ui/tests/*.test.cjs`: 24 aprovados.
- Typechecks: extensão de câmeras, frontend e plugin API aprovados.
- `node scripts/build-extension-ui.mjs cameras`: aprovado; aviso existente de tamanho do bundle.

No Chrome autenticado, a Frente abriu o assistente com a imagem salva 4096 × 2048 e as marcações pendentes recuperadas. Inspeção visual confirmou os controles secundários recolhidos e Confirmar ponto como ação principal. Fechado sem confirmar pontos. Configuração idêntica ao backup antes da validação. Instância local atualizada; autenticação preservada.

Os testes não certificam captura, cobertura, parada, retorno ou apontamento físicos. A aquisição mecânica e a calibração real não foram modificadas por esta entrega. Revisão e substituição completas foram testadas com fonte sintética.
