# Python em Linux/macOS

Instalação direta para quem quer rodar o Toposync no próprio sistema, sem Docker.

## Para Quem É

Use este caminho em Linux ou macOS.

Este guia instala o bundle padrão em CPU.

Para suporte por arquitetura e GPU, consulte [Compatibilidade](architecture-support.md).

## Pré-requisitos

- Python 3.12 recomendado.
- `uv`.
- Acesso ao terminal.

Instale o `uv` se ainda não tiver:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Feche e abra o terminal se o comando `uv` ainda não aparecer no `PATH`.

## Instalação

Crie uma pasta para o Toposync:

```bash
mkdir -p ~/toposync
cd ~/toposync
```

Instale o Python recomendado e crie o ambiente virtual:

```bash
uv python install 3.12
uv venv .venv --python 3.12
source .venv/bin/activate
```

Instale o Toposync:

```bash
uv pip install --upgrade --refresh toposync
```

Se precisar reproduzir uma versão específica:

```bash
uv pip install --upgrade --refresh "toposync==0.7.2"
```

## Como Rodar

Para uso local:

```bash
toposync serve
```

Para acessar pela rede local:

```bash
toposync serve --host 0.0.0.0 --port 8000
```

Para escolher a pasta de dados:

```bash
toposync serve --data-dir ./toposync-data
```

## Como Acessar

No mesmo computador:

```text
http://127.0.0.1:8000/
```

De outro dispositivo na mesma rede, use o IP do servidor:

```text
http://<ip-do-servidor>:8000/
```

## Como Verificar

Em outro terminal:

```bash
curl -I http://127.0.0.1:8000/
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/extensions
```

O esperado:

- `/` responde `200`;
- `/api/health` responde `200`;
- `/api/extensions` lista as extensões carregadas.

## Como Atualizar

Com o ambiente virtual ativo:

```bash
uv pip install --upgrade --refresh toposync
```

Depois reinicie o processo `toposync serve`.

## Como Desinstalar

Pare o servidor e remova a pasta onde você criou o ambiente:

```bash
deactivate 2>/dev/null || true
rm -rf ~/toposync
```

Se você usou outra pasta de dados, remova também essa pasta.

## Troubleshooting

### `toposync: command not found`

Ative o ambiente virtual:

```bash
source .venv/bin/activate
```

### A UI não abre

Confirme se o servidor está rodando e se `/api/health` responde:

```bash
curl http://127.0.0.1:8000/api/health
```

### Quero streaming

Instale o bundle de streaming:

```bash
uv pip install --upgrade --refresh toposync-streaming
```

O streaming pode exigir FFmpeg disponível no sistema.
