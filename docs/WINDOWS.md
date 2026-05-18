# Instalação no Windows

Este é o caminho recomendado para instalar o Toposync no Windows.

## Recomendação do projeto

- Use `Python 3.12`.
- Use `uv` para criar o ambiente virtual e instalar os pacotes.
- Comece com o bundle padrão em CPU: `toposync`.
- Só depois adicione aceleração:
  - `toposync-vision-directml` para GPU Windows genérica
  - `toposync-vision-cuda` para NVIDIA CUDA

## Por que Python 3.12?

No Windows, combinações mais novas de Python ainda podem cair em build local de dependências nativas da stack de visão, especialmente `onnx` / `ml-dtypes`.

Quando isso acontece, o instalador pode pedir:

- Microsoft Visual C++ Build Tools

Isso não deve ser o caminho padrão para usuário final. A recomendação prática do projeto é usar `Python 3.12`, onde a chance de resolver tudo por wheel pronta é muito maior.

## 1) Instalar `uv`

No PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 2) Instalar Python 3.12

```powershell
uv python install 3.12
```

## 3) Criar e ativar o ambiente virtual

No diretório onde você quer instalar o Toposync:

```powershell
uv venv .venv --python 3.12
.venv\Scripts\Activate.ps1
```

## 4) Instalar o bundle padrão

```powershell
uv pip install --upgrade --refresh toposync==0.4.17
```

Depois rode:

```powershell
toposync serve
```

O esperado é abrir a UI e a API na mesma porta, normalmente:

- `http://127.0.0.1:8000`

Nesse modo:

- `/` serve a UI
- `/api/*` serve a API

Verificações rápidas:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

## 5) Adicionar GPU depois

### DirectML

Para máquinas Windows com GPU compatível via DirectML:

```powershell
uv pip install --upgrade --refresh toposync-vision-directml==0.4.17
```

### CUDA

Para NVIDIA:

```powershell
uv pip install --upgrade --refresh toposync-vision-cuda==0.4.17
```

Observação:

- CUDA exige driver NVIDIA e stack CUDA/cuDNN compatíveis com o `onnxruntime-gpu`.

## 6) Instalar o processing server como serviço

Para provisionar uma máquina Windows como processing server permanente, use o script:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_processing_server.ps1
```

Para o formato "baixar por link e rodar":

```powershell
irm https://SEU_DOMINIO/install_windows_processing_server.ps1 -OutFile $env:TEMP\install-toposync-processing.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\install-toposync-processing.ps1 -Bundle auto
```

O script:

- exige ou reabre PowerShell como Administrador quando possível;
- instala `uv` e Python 3.12 se necessário;
- cria um ambiente em `%ProgramData%\TopoSync\ProcessingServer`;
- instala o bundle `toposync`, `toposync-vision-directml` ou `toposync-vision-cuda`;
- cria regra de firewall para a porta selecionada;
- cria o serviço Windows `TopoSyncProcessingServer`;
- configura restart automático em caso de falha;
- gera senha Basic Auth por padrão;
- salva o payload de registro em `%ProgramData%\TopoSync\ProcessingServer\processing-server-registration.json`.

Por padrão, o script usa a porta `49321`, não `9001`. A porta `9001` é registrada na IANA para outro serviço (`etlservicemgr`), então o instalador evita usá-la como padrão. Se a porta escolhida já estiver ocupada, o script procura a próxima porta livre.

Quando `-HostAddress` fica em `0.0.0.0`, o serviço escuta na rede. Sem `-AdvertiseHost`, o instalador tenta anunciar o primeiro IPv4 de LAN com gateway, por exemplo `http://192.168.0.250:49321`, em vez do hostname curto do Windows. Use `-AdvertiseHost` se quiser forçar um IP ou DNS específico.

O firewall é aberto para os perfis `Domain`, `Private` e `Public`, porque muitas máquinas Windows ficam em rede doméstica marcada como pública.

Exemplos:

```powershell
# Auto: CUDA se nvidia-smi existir; senão DirectML.
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_processing_server.ps1 -Bundle auto

# Forçar DirectML.
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_processing_server.ps1 -Bundle directml

# Forçar CPU.
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_processing_server.ps1 -Bundle cpu

# Informar o IP/hostname que o origin deve usar.
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_processing_server.ps1 -AdvertiseHost 192.168.1.50
```

Depois, registre no servidor origin usando o JSON impresso no final ou o arquivo `processing-server-registration.json`.

## 7) Desinstalar o processing server

Para remover o serviço, a regra de firewall e os arquivos de runtime, preservando dados e logs:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_windows_processing_server.ps1
```

Para remover também `%ProgramData%\TopoSync\ProcessingServer` inteiro:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_windows_processing_server.ps1 -RemoveData
```

Se o serviço estiver preso em parada, use:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_windows_processing_server.ps1 -Force
```

Depois remova o processing server também no Toposync origin, se ele estiver registrado lá.

## 8) Quando preciso do Visual C++ Build Tools?

Na prática: só quando o instalador não encontra wheel pronta e tenta compilar dependências nativas.

Isso costuma aparecer com erros como:

- `Microsoft Visual C++ 14.0 or greater is required`

Se isso acontecer, siga esta ordem:

1. Apague o `.venv`.
2. Recrie o ambiente com `Python 3.12`.
3. Tente instalar de novo.

Na maior parte dos casos, isso é melhor do que transformar a instalação do usuário em um setup de toolchain C/C++.

## 9) E se eu insistir em Python 3.14?

Pode funcionar no futuro, mas não é a recomendação do projeto neste momento.

Se você insistir em `Python 3.14` e o instalador cair em build nativo, aí sim você vai precisar do Visual C++ Build Tools:

- https://visualstudio.microsoft.com/visual-cpp-build-tools/

Esse caminho deve ser tratado como fallback avançado, não como pré-requisito padrão do Toposync.

## Referências oficiais

- `uv`: https://docs.astral.sh/uv/
- ONNX Runtime Python install: https://onnxruntime.ai/docs/get-started/with-python.html
- ONNX Runtime install matrix: https://onnxruntime.ai/docs/install
- Visual C++ Build Tools: https://visualstudio.microsoft.com/visual-cpp-build-tools/
- `ml-dtypes` PyPI: https://pypi.org/project/ml-dtypes/
