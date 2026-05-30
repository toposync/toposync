# Processing Server Em Linux/macOS

Instalação direta de um servidor de processamento remoto.

## Para Quem É

Use este caminho quando quiser tirar trabalho pesado do servidor origin ou do Home Assistant.

O processing server executa pipelines distribuídos e responde ao origin pela API HTTP.

Para suporte por sistema, arquitetura e GPU, consulte [Compatibilidade](architecture-support.md).

## Pré-requisitos

- Linux ou macOS.
- Python 3.12 recomendado.
- `uv`.
- Porta TCP liberada entre origin e processing server. O padrão é `49321`.

Instale o `uv` se ainda não tiver:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Instalação

Crie uma pasta para o processing server:

```bash
mkdir -p ~/toposync-processing
cd ~/toposync-processing
```

Crie o ambiente virtual:

```bash
uv python install 3.12
uv venv .venv --python 3.12
source .venv/bin/activate
```

Instale o bundle padrão em CPU:

```bash
uv pip install --upgrade --refresh toposync
```

Se precisar reproduzir uma versão específica:

```bash
uv pip install --upgrade --refresh "toposync==0.7.2"
```

## GPU E Streaming Opcionais

Para Linux com NVIDIA CUDA:

```bash
uv pip install --upgrade --refresh toposync-vision-cuda
```

Para streaming em CPU:

```bash
uv pip install --upgrade --refresh toposync-streaming
```

Para CUDA + streaming no mesmo processing server:

```bash
uv pip install --upgrade --refresh toposync-vision-cuda toposync-ext-streaming
```

Streaming pode exigir FFmpeg disponível no sistema.

## Como Rodar

Sem autenticação, apenas para rede confiável:

```bash
toposync processing-serve --host 0.0.0.0 --port 49321 --data-dir ./toposync-processing-data
```

Com Basic Auth:

```bash
TOPOSYNC_PROCESSING_USERNAME=toposync \
TOPOSYNC_PROCESSING_PASSWORD='<senha-forte>' \
toposync processing-serve --host 0.0.0.0 --port 49321 --data-dir ./toposync-processing-data
```

## Como Acessar

O origin precisa alcançar:

```text
http://<ip-do-processing-server>:49321
```

O processing server não serve a UI principal. Ele serve endpoints de processamento para o origin.

## Como Verificar

Sem Basic Auth:

```bash
curl http://127.0.0.1:49321/api/processing/status
```

Com Basic Auth:

```bash
curl -u toposync:'<senha-forte>' http://127.0.0.1:49321/api/processing/status
```

O esperado é um JSON de status. `active: false` é normal até o origin enviar uma configuração de pipeline.

## Registrar No Origin

No servidor origin, registre o processing server:

```bash
curl -X PUT http://127.0.0.1:8000/api/processing-servers/remote_gpu \
  -H 'content-type: application/json' \
  -d '{
    "id": "remote_gpu",
    "name": "Remote GPU",
    "kind": "http",
    "url": "http://<ip-do-processing-server>:49321",
    "username": "toposync",
    "password": "<senha-forte>"
  }'
```

Depois teste pelo origin:

```bash
curl http://127.0.0.1:8000/api/processing-servers/remote_gpu/status
```

Nos pipelines, use `processing_server_id` igual ao id registrado, por exemplo `remote_gpu`.

## Como Atualizar

Com o ambiente virtual ativo:

```bash
uv pip install --upgrade --refresh toposync
```

Se instalou upgrades, atualize somente os pacotes que você usa:

```bash
uv pip install --upgrade --refresh toposync-vision-cuda
uv pip install --upgrade --refresh toposync-streaming
uv pip install --upgrade --refresh toposync-ext-streaming
```

Depois reinicie o processo `toposync processing-serve`.

## Como Desinstalar

Pare o processo e remova a pasta:

```bash
deactivate 2>/dev/null || true
rm -rf ~/toposync-processing
```

Remova também o processing server registrado no origin.

## Troubleshooting

### `401 Unauthorized`

O usuário ou senha configurado no origin não bate com `TOPOSYNC_PROCESSING_USERNAME` e `TOPOSYNC_PROCESSING_PASSWORD`.

### `Connection refused`

Confirme que o processing server está rodando com `--host 0.0.0.0` e que a porta `49321` está liberada no firewall.

### Processing server sempre idle

O pipeline ainda está usando `processing_server_id: "local"`. Altere o pipeline para o id do servidor remoto.

### Vision não usa GPU

Confirme que instalou `toposync-vision-cuda` e verifique os providers retornados em `/api/processing/status`.
