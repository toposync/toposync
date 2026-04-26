# Home Assistant add-on

TopoSync tem um repositório dedicado para o add-on do Home Assistant:

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
- usa o `SUPERVISOR_TOKEN` e o proxy interno do Core API automaticamente
- não exige configurar `host` e `apiKey` manualmente dentro da extensão `home_assistant`

## Modo de execução

O launcher do add-on fixa estas variáveis de ambiente:

- `TOPOSYNC_AUTH_MODE=home_assistant_hybrid`
- `TOPOSYNC_HOME_ASSISTANT_CONNECTION_MODE=supervisor`
- `TOPOSYNC_DATA_DIR=/data`
- `TOPOSYNC_STREAMING_ENGINE_CACHE_DIR=/data/runtime`

No modo `supervisor`, a extensão `home_assistant` passa a usar:

- REST: `http://supervisor/core/api`
- websocket: `ws://supervisor/core/websocket`
- bearer token: `SUPERVISOR_TOKEN`

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

- `toposync==0.3.6`

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

## Escopo atual

O add-on atual cobre o caminho CPU.

CUDA continua devendo ficar separado. O motivo é operacional:

- o add-on precisa rodar bem no ecossistema do Home Assistant sem assumir host NVIDIA
- o runtime CUDA exige imagem e host específicos
- o projeto já trata `toposync-vision-cuda` como bundle separado

Então, para Home Assistant:

- add-on padrão: CPU
- CUDA: variante futura separada, não misturada no mesmo add-on
