# Troubleshooting

- **Module Federation + HMR**: se aparecer `Shared module is not available for eager consumption`, o host deve inicializar `__webpack_init_sharing__("default")` antes de importar React (o projeto já usa o padrão `bootstrap`).
- **Mudou UI da extensão e não refletiu**: rode o build de novo do workspace da extensão e dê refresh (o backend só serve arquivos estáticos).
