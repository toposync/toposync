# Self-hosting

Este documento e legado. Os comandos praticos de instalacao foram movidos para:

- [Instalacao do Toposync](../install/README.md)
- [Python em Linux/macOS](../install/python-linux-macos.md)
- [Docker CPU](../install/docker-cpu.md)
- [Docker CUDA](../install/docker-cuda.md)
- [Processing server em Linux/macOS](../install/processing-server-linux-macos.md)
- [Processing server em Docker](../install/processing-server-docker.md)
- [Compatibilidade](../install/architecture-support.md)

## Contexto Que Continua Valendo

- Em producao, a UI e a API sao servidas pelo mesmo backend e pela mesma porta.
- O pacote `toposync` e o bundle padrao em CPU.
- Streaming, CUDA e processing servers sao upgrades conforme necessidade.
- O primeiro acesso em modo normal passa pelo setup/login local do Toposync.
- `/api/health` e o endpoint anonimo correto para healthcheck.
- Rotas protegidas, como `/api/extensions`, exigem setup/login antes de responderem normalmente.

## Escolha Rapida

- Para host Linux/macOS direto no Python, use [Python em Linux/macOS](../install/python-linux-macos.md).
- Para container sem GPU, use [Docker CPU](../install/docker-cpu.md).
- Para container Linux com NVIDIA, use [Docker CUDA](../install/docker-cuda.md).
- Para tirar trabalho pesado do origin, use um dos guias de processing server em [docs/install](../install/README.md).
