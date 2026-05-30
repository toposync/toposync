# HAOS ARM64

Este documento e legado. A matriz de suporte foi consolidada em:

- [Compatibilidade](../install/architecture-support.md)
- [Home Assistant add-on](../install/home-assistant-addon.md)
- [Processing server em Linux/macOS](../install/processing-server-linux-macos.md)
- [Processing server em Docker](../install/processing-server-docker.md)

## Contexto Que Continua Valendo

- O alvo suportado para Home Assistant OS em ARM e `aarch64` / `linux/arm64`.
- `armv7`, `armhf` e `i386` ficam fora do alvo de suporte.
- O add-on do Home Assistant e CPU-only.
- Raspberry Pi 5 com 8 GB e NVMe e a referencia pratica para experiencia moderna.
- Raspberry Pi 4 e instalacoes em SD card sao best-effort.
- Vision, OpenCV pesado e multiplas cameras devem ser delegados para um processing server remoto quando houver gargalo.

## Validacao

Use a matriz em [Compatibilidade](../install/architecture-support.md) para decidir o cenario suportado. Use os guias de processing server em [docs/install](../install/README.md) para descarregar processamento do HAOS/Raspberry Pi.
