# Python em Windows

Instalação direta para Windows usando PowerShell, `uv` e Python 3.12.

## Para Quem É

Use este caminho em Windows `amd64` quando quiser rodar o Toposync sem Docker.

Este guia começa pelo bundle padrão em CPU. GPU e streaming entram como upgrades depois que a instalação básica funciona.

## Pré-requisitos

- Windows 10/11 `amd64`.
- PowerShell.
- Python 3.12 via `uv`.

## Instalação

Instale o `uv`:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Feche e abra o PowerShell se o comando `uv` ainda não aparecer.

Crie uma pasta para o Toposync:

```powershell
mkdir $env:USERPROFILE\toposync
cd $env:USERPROFILE\toposync
```

Instale Python 3.12 e crie o ambiente virtual:

```powershell
uv python install 3.12
uv venv .venv --python 3.12
.venv\Scripts\Activate.ps1
```

Instale o Toposync:

```powershell
uv pip install --upgrade --refresh toposync
```

Se precisar reproduzir uma versão específica:

```powershell
uv pip install --upgrade --refresh "toposync==0.7.2"
```

## Como Rodar

Para uso local:

```powershell
toposync serve
```

Para acessar pela rede local:

```powershell
toposync serve --host 0.0.0.0 --port 8000
```

Para escolher a pasta de dados:

```powershell
toposync serve --data-dir .\toposync-data
```

## Como Acessar

No mesmo computador:

```text
http://127.0.0.1:8000/
```

De outro dispositivo na mesma rede, use o IP do Windows:

```text
http://<ip-do-windows>:8000/
```

## Como Verificar

Em outro PowerShell:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/
Invoke-RestMethod http://127.0.0.1:8000/api/health
Invoke-RestMethod http://127.0.0.1:8000/api/extensions
```

O esperado:

- `/` responde com a UI;
- `/api/health` responde;
- `/api/extensions` lista as extensões carregadas.

## Upgrades Opcionais

### GPU Windows Via DirectML

Use em máquinas Windows com GPU compatível com DirectML:

```powershell
uv pip install --upgrade --refresh toposync-vision-directml
```

### NVIDIA CUDA

Use somente se a máquina tiver NVIDIA, driver e runtime compatíveis:

```powershell
uv pip install --upgrade --refresh toposync-vision-cuda
```

### Streaming

Para adicionar streaming:

```powershell
uv pip install --upgrade --refresh toposync-streaming
```

O MediaMTX e o go2rtc são baixados sob demanda pelo runtime de streaming. FFmpeg deve estar disponível no sistema ou configurado por variável de ambiente.

## Como Atualizar

Com o ambiente virtual ativo:

```powershell
uv pip install --upgrade --refresh toposync
```

Se instalou upgrades, atualize somente os pacotes que você usa:

```powershell
uv pip install --upgrade --refresh toposync-vision-directml
uv pip install --upgrade --refresh toposync-vision-cuda
uv pip install --upgrade --refresh toposync-streaming
```

Depois reinicie o processo `toposync serve`.

## Como Desinstalar

Pare o servidor, saia do ambiente virtual e remova a pasta:

```powershell
deactivate
cd $env:USERPROFILE
Remove-Item -Recurse -Force .\toposync
```

Se você usou outra pasta de dados, remova também essa pasta.

## Troubleshooting

### `toposync` não é reconhecido

Ative o ambiente virtual:

```powershell
.venv\Scripts\Activate.ps1
```

### PowerShell bloqueou o script de ativação

Abra PowerShell como usuário normal e rode:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Depois tente ativar o ambiente novamente.

### Erro pedindo Microsoft Visual C++ Build Tools

Esse erro normalmente significa que alguma dependência nativa tentou compilar localmente.

Faça primeiro:

```powershell
deactivate
Remove-Item -Recurse -Force .venv
uv venv .venv --python 3.12
.venv\Scripts\Activate.ps1
uv pip install --upgrade --refresh toposync
```

Instalar Visual C++ Build Tools deve ser fallback avançado, não o caminho padrão.
