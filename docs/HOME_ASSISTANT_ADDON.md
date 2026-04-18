# Home Assistant add-on

TopoSync agora tem uma estrutura dedicada de add-on em:

- [homeassistant-addon/repository.yaml](/Users/c/Projects/toposync-2/homeassistant-addon/repository.yaml)
- [homeassistant-addon/toposync/config.yaml](/Users/c/Projects/toposync-2/homeassistant-addon/toposync/config.yaml)
- [homeassistant-addon/toposync/Dockerfile](/Users/c/Projects/toposync-2/homeassistant-addon/toposync/Dockerfile)
- [homeassistant-addon/toposync/run_addon.py](/Users/c/Projects/toposync-2/homeassistant-addon/toposync/run_addon.py)

## O que esse add-on faz

- publica o Toposync na sidebar do Home Assistant
- usa `ingress: true`, então a UI e a API ficam atrás do ingress do Supervisor
- executa o mesmo `toposync serve` da distribuição normal
- serve frontend e API na mesma porta interna (`8000`)
- usa o `SUPERVISOR_TOKEN` e o proxy interno do Core API automaticamente
- não exige configurar `host` e `apiKey` manualmente dentro da extensão `home_assistant`

## Modo de execução

O launcher do add-on fixa estas variáveis de ambiente:

- `TOPOSYNC_AUTH_MODE=home_assistant_ingress`
- `TOPOSYNC_HOME_ASSISTANT_CONNECTION_MODE=supervisor`
- `TOPOSYNC_DATA_DIR=/data`
- `TOPOSYNC_STREAMING_ENGINE_CACHE_DIR=/data/runtime`

No modo `supervisor`, a extensão `home_assistant` passa a usar:

- REST: `http://supervisor/core/api`
- websocket: `ws://supervisor/core/websocket`
- bearer token: `SUPERVISOR_TOKEN`

## Segurança

O backend restringe o acesso do modo ingress aos IPs confiáveis do Supervisor.

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

- `toposync==0.3.4`

Para testar contra outro índice, ajuste os build args:

- `TOPOSYNC_PIP_INDEX_URL`
- `TOPOSYNC_EXTRA_INDEX_URL`
- `TOPOSYNC_PIP_SPEC`

## Escopo atual

O add-on atual cobre o caminho CPU.

CUDA continua devendo ficar separado. O motivo é operacional:

- o add-on precisa rodar bem no ecossistema do Home Assistant sem assumir host NVIDIA
- o runtime CUDA exige imagem e host específicos
- o projeto já trata `toposync-vision-cuda` como bundle separado

Então, para Home Assistant:

- add-on padrão: CPU
- CUDA: variante futura separada, não misturada no mesmo add-on
