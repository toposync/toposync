# Simulação completa — 18/09/2026

Solicitação: percorrer todos os pontos, concluir e verificar se o mapeamento funciona.

Foi usado o cenário sintético isolado de `tests/panorama_browser_fixture.py`, com câmera a 3 metros do piso. Não é uma cópia da imagem nem da planta da Frente. A instância real e sua calibração não foram alteradas.

## Execução e resultado

- Seis correspondências de ajuste e duas de conferência preenchidas pela interface, sem inserir pontos diretamente na API.
- Concluir calibração executado; servidor devolveu mapa ativo e qualidade pronta, sem motivos de reprovação.
- Três lugares adicionais, fora dos oito pontos, verificados pela prévia real da interface em ambos os sentidos. O oráculo usa a geometria conhecida da cena, não a matriz ajustada.
- Pontos e revisão persistidos permaneceram iguais após consultar a prévia. Zero comandos físicos no controlador simulado.

| Posição esperada na planta (metros) | Erro da projeção imagem → planta | Erro planta → imagem |
|---|---:|---:|
| (4, -2) | 0.460 mm | 0.0109 pixels |
| (6, 0) | 0.596 mm | 0.0108 pixels |
| (7.5, 2) | 1.423 mm | 0.0098 pixels |

Esses desvios pertencem a uma cena idealizada com coordenadas conhecidas. Não demonstram precisão física da Frente, localização do vídeo real ou apontamento de motores.

## Validação

- `npx playwright test --config playwright.panorama.config.js --grep 'a saved source panorama starts a point pair'`: 1 cenário completo aprovado, com três verificações adicionais.
- `.venv/bin/python -m pytest -q tests/test_camera_panorama_runtime.py tests/test_camera_panorama_localization.py`: 53 aprovados. Evidência de processamento controlada, distinta de operação real.

Screenshots e medições preservados em `/Users/c/.codex/visualizations/2026/09/18/01a0b4ef-55e6-7171-a1a4-07ece4bae369/`, arquivos `04-simulacao-oito-pontos.png`, `05-simulacao-concluida.png`, `06-simulacao-mapeamento.png` e `simulacao-mapeamento.json`.
