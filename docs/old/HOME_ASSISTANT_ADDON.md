# Home Assistant add-on

Toposync tem um repositório dedicado para o add-on do Home Assistant:

- https://github.com/toposync/toposync-homeassistant-addon

Adicione esse repositório no Add-on Store do Home Assistant:

```text
https://github.com/toposync/toposync-homeassistant-addon
```

## O que esse add-on faz

- publica o Toposync na sidebar do Home Assistant
- usa `ingress: true`, então a UI e a API ficam atrás do ingress do Supervisor
- executa o mesmo `toposync serve` da distribuição normal
- serve frontend e API na porta interna de ingress (`18757`)
- expõe opcionalmente uma porta direta (`18756`) com proxy que remove headers de identidade do Home Assistant
- declara portas opcionais de streaming para RTSP, HLS e WebRTC, mas não publica essas portas no host por padrão
- usa o `SUPERVISOR_TOKEN` e o proxy interno do Core API automaticamente
- não exige configurar `host` e `apiKey` manualmente dentro da extensão `home_assistant`
- permite que a extensão `cameras` use `/network/info` do Supervisor para descobrir o broadcast IPv4 da LAN em buscas ONVIF

## Modo de execução

O launcher do add-on fixa estas variáveis de ambiente:

- `TOPOSYNC_AUTH_MODE=home_assistant_hybrid`
- `TOPOSYNC_HOME_ASSISTANT_CONNECTION_MODE=supervisor`
- `TOPOSYNC_DATA_DIR=/data`
- `TOPOSYNC_STREAMING_ENGINE_CACHE_DIR=/data/runtime`
- `TOPOSYNC_DEPLOYMENT_TARGET=home_assistant_addon`
- `TOPOSYNC_STREAMING_HLS_PUBLIC_MODE=proxy`
- `TOPOSYNC_FAIL_STREAM_URLS_ON_PORT_MISMATCH=1`

No modo `supervisor`, a extensão `home_assistant` passa a usar:

- REST: `http://supervisor/core/api`
- websocket: `ws://supervisor/core/websocket`
- bearer token: `SUPERVISOR_TOKEN`

O add-on também habilita `hassio_api: true` para permitir leitura de `/network/info`. Isso é usado pela extensão `cameras` para enviar WS-Discovery ONVIF também ao broadcast da LAN, sem precisar ativar `host_network`.

## Segurança

O add-on usa `TOPOSYNC_AUTH_MODE=home_assistant_hybrid`.

Nesse modo:

- acesso via sidebar/ingress usa o usuário informado pelo Home Assistant
- acesso direto pela porta mapeada usa usuários locais do Toposync
- a criação inicial de usuário local fica desabilitada no primeiro acesso direto
- usuários locais devem ser criados pela plataforma, acessando o Toposync pela sidebar, ou por configuração de dados

O add-on mantém a porta de ingress separada da porta direta. A porta direta passa por um proxy local que remove `X-Remote-User-*`, `X-Ingress-*`, `X-Hassio-*`, `X-Supervisor-*` e `X-Forwarded-*` antes de encaminhar a requisição para o Toposync. Assim, clientes da porta direta não conseguem simular o usuário do Home Assistant apenas enviando headers.

No `config.yaml`, o add-on também está com:

- `panel_admin: true`

Isso é intencional. Hoje o ingresso do Home Assistant identifica o usuário, mas o Toposync ainda não faz mapeamento fino entre permissões do HA e papéis internos próprios. Então, no add-on, o caminho seguro atual é expor a sidebar só para administradores do HA.

## Distribuição da imagem

O add-on foi desenhado para instalar o pacote publicado de Python, em vez de rebuildar o app inteiro dentro do container do Supervisor.

Isso evita duplicar o runtime do Toposync:

- `pip install toposync`
- imagem Docker de produção
- add-on do Home Assistant

Todos passam a consumir o mesmo bundle publicado.

Por padrão, o Dockerfile do add-on instala:

- `toposync-streaming==0.7.2`

O add-on tem versionamento próprio no Home Assistant. A linha pública atual pode aparecer como `0.7.3` no Add-on Store enquanto instala o pacote Python `toposync-streaming==0.7.2`.

Para testar contra outro índice, ajuste os build args:

- `TOPOSYNC_PIP_INDEX_URL`
- `TOPOSYNC_EXTRA_INDEX_URL`
- `TOPOSYNC_PIP_SPEC`

O código do add-on vive fora deste repo para manter o formato esperado pelo Home Assistant: `repository.yaml` na raiz e uma pasta por add-on.

## Acesso direto

O add-on declara a porta direta `18756/tcp`, mas deixa o host port vazio por padrão. Isso mantém o Toposync disponível apenas pelo ingress do Home Assistant.

Para expor a UI/API para apps móveis ou navegador na rede local, configure a seção `Network` do add-on e mapeie:

```yaml
18756/tcp: 18756
```

Depois acesse:

```text
http://homeassistant.local:18756/
```

## Acesso de streaming

O add-on declara portas de streaming, mas deixa o host port vazio por padrão. Isso mantém RTSP, WHEP e transporte de mídia WebRTC fora da LAN até o usuário habilitar explicitamente.

HLS para web/app móvel usa o proxy HTTP do próprio Toposync pela porta direta `18756`: `http://<home-assistant-ip>:18756/api/streams/media/hls/...`. Com isso o app não recebe URL HLS direta do MediaMTX na porta `18759`, que pode não estar publicada no host.

Para expor streaming na rede local, configure a seção `Network` do add-on e mapeie somente os protocolos necessários:

```yaml
18758/tcp: 18758  # RTSP
18760/tcp: 18760  # WebRTC/WHEP signaling
18762/udp: 18762  # WebRTC media transport
```

`18759/tcp` é o HLS direto avançado/diagnóstico do MediaMTX e não faz parte do contrato público padrão do add-on. A faixa reservada do add-on é `18756-18762`: `18756` para acesso direto e HLS proxied, `18757` para ingress/backend interno, `18758` para RTSP, `18759` para HLS direto interno/diagnóstico, `18760` para WHEP, `18761` para a API interna do MediaMTX e `18762/udp` para mídia WebRTC. As portas `18759` e `18761` permanecem internas e não são declaradas em `ports` por padrão.

Para WebRTC na LAN, `18760/tcp` cobre só a sinalização WHEP; o transporte de mídia ainda precisa de `18762/udp` mapeado, salvo uma configuração futura com TURN/TCP/TLS.

## Home Assistant Cloud

O caminho suportado para Home Assistant Cloud é **entidade nativa `camera` do Home Assistant**, não o player web do Toposync rodando dentro do ingress.

Na prática:

- a UI do Toposync pelo HA ingress continua HLS-first
- `playback-plan?client=ha_ingress` bloqueia WebRTC direto por padrão
- a integração HA `toposync` consome `GET /api/streams/home-assistant/cameras`
- cada entidade `camera` usa `stream_source()` com RTSP interno do Toposync/MediaMTX
- o HA Core fica responsável por transformar esse stream no caminho suportado pelo frontend/Cloud
- `enable_native_webrtc` existe como opção avançada, default `false`

Para a integração custom, o Toposync expõe:

```text
GET  /api/streams/home-assistant/cameras
GET  /api/streams/transmissions/{id}/still.jpg?output_id=...&quality_profile_id=...
POST /api/streams/transmissions/{id}/webrtc/offer
POST /api/streams/transmissions/{id}/demand/heartbeat
```

O manifesto HA-native sempre referencia `Transmission`/`output` do Toposync. Ele não retorna RTSP direto da câmera de origem. Como câmeras podem ter mais de um stream, o manifesto preserva `quality_profile_id` e `output_id` para que thumbnail/grid, fullscreen, PTZ e diagnóstico não caiam no primeiro output disponível.

Quando o HA Core precisa acessar o RTSP do MediaMTX por outro host interno, configure:

```text
TOPOSYNC_HOME_ASSISTANT_RTSP_HOST=<host-alcancavel-pelo-ha-core>
```

WebRTC nativo HA só deve ser habilitado depois de validar TURN/ICE/Cloud em ambiente real. A API do Home Assistant trata câmera WebRTC nativa como caminho WebRTC direto e não como fallback HLS transparente.

## Escopo atual

O add-on atual cobre o caminho CPU em `amd64` e `aarch64`.

Para HAOS em Raspberry Pi e dispositivos ARM similares, o alvo suportado é 64-bit `aarch64` / `linux/arm64`. `armv7`, `armhf` e `i386` ficam fora de suporte.

Raspberry Pi 5 8GB com NVMe é a referência mínima para uma experiência moderna. Pi 4 e instalações em SD card são best-effort para compatibilidade, não baseline de performance. Use processing servers remotos para múltiplas câmeras, OpenCV pesado e ONNX em CPU.

CUDA continua devendo ficar separado. O motivo é operacional:

- o add-on precisa rodar bem no ecossistema do Home Assistant sem assumir host NVIDIA
- o runtime CUDA exige imagem e host específicos
- o projeto já trata `toposync-vision-cuda` como bundle separado

Então, para Home Assistant:

- add-on padrão: CPU
- CUDA: variante futura separada, não misturada no mesmo add-on
