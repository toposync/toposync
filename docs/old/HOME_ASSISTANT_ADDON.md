# Home Assistant Add-on

Este documento e legado. Os comandos praticos de instalacao foram movidos para:

- [Home Assistant add-on](../install/home-assistant-addon.md)
- [Compatibilidade](../install/architecture-support.md)
- [Processing server em Linux/macOS](../install/processing-server-linux-macos.md)
- [Processing server como servico no Windows](../install/processing-server-windows-service.md)

## Contexto Que Continua Valendo

- O add-on vive no repositorio dedicado `toposync/toposync-homeassistant-addon`.
- O add-on publica o Toposync na sidebar do Home Assistant via ingress.
- A porta interna de backend/ingress e `18757`.
- A porta direta opcional para UI/API e `18756/tcp`.
- O modo de autenticacao do add-on e hibrido: sidebar pelo Home Assistant, acesso direto por usuarios locais do Toposync.
- HLS para web/app deve passar pelo proxy HTTP do Toposync em `18756/tcp` quando a porta direta estiver mapeada.
- RTSP e WebRTC sao portas opcionais e devem ser publicados somente quando necessarios.
- O add-on e CPU-only; workloads pesados devem ser delegados para processing server remoto.

## Onde Continuar

Use [Home Assistant add-on](../install/home-assistant-addon.md) para instalacao, atualizacao, portas e troubleshooting rapido. Use [Compatibilidade](../install/architecture-support.md) para HAOS, Raspberry Pi e arquiteturas suportadas.
