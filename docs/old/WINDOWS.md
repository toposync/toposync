# Windows

Este documento e legado. Os comandos praticos de instalacao foram movidos para:

- [Python em Windows](../install/python-windows.md)
- [Processing server como servico no Windows](../install/processing-server-windows-service.md)
- [Compatibilidade](../install/architecture-support.md)

## Contexto Que Continua Valendo

- O caminho recomendado no Windows e Python 3.12 com `uv`.
- Comece com o bundle padrao `toposync` em CPU.
- DirectML e CUDA entram como upgrades depois que a instalacao basica funciona.
- O processing server permanente no Windows deve ser instalado pelo script `scripts/install_windows_processing_server.ps1`.
- O servico criado pelo script se chama `ToposyncProcessingServer`.
- O arquivo de registro do processing server fica em `%ProgramData%\Toposync\ProcessingServer\processing-server-registration.json`.

## Visual C++ Build Tools

Visual C++ Build Tools deve ser fallback, nao pre-requisito inicial. Se uma dependencia nativa tentar compilar localmente, recrie primeiro o ambiente com Python 3.12 seguindo [Python em Windows](../install/python-windows.md).
