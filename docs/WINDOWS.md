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

Para instalar a partir do TestPyPI:

```powershell
uv pip install --upgrade --refresh `
  --index-url https://test.pypi.org/simple/ `
  --extra-index-url https://pypi.org/simple/ `
  toposync==0.3.6
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
uv pip install --upgrade --refresh `
  --index-url https://test.pypi.org/simple/ `
  --extra-index-url https://pypi.org/simple/ `
  toposync-vision-directml==0.3.6
```

### CUDA

Para NVIDIA:

```powershell
uv pip install --upgrade --refresh `
  --index-url https://test.pypi.org/simple/ `
  --extra-index-url https://pypi.org/simple/ `
  toposync-vision-cuda==0.3.6
```

Observação:

- CUDA exige driver NVIDIA e stack CUDA/cuDNN compatíveis com o `onnxruntime-gpu`.

## 6) Quando preciso do Visual C++ Build Tools?

Na prática: só quando o instalador não encontra wheel pronta e tenta compilar dependências nativas.

Isso costuma aparecer com erros como:

- `Microsoft Visual C++ 14.0 or greater is required`

Se isso acontecer, siga esta ordem:

1. Apague o `.venv`.
2. Recrie o ambiente com `Python 3.12`.
3. Tente instalar de novo.

Na maior parte dos casos, isso é melhor do que transformar a instalação do usuário em um setup de toolchain C/C++.

## 7) E se eu insistir em Python 3.14?

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
