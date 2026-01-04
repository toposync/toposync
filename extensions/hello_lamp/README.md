# TopoSync Extension: Hello Lamp

Exemplo de extensão **prebuilt**: instala via `pip/uv` como pacote Python, e o backend serve o bundle do frontend (`remoteEntry.js`) direto do wheel.

## Desenvolvimento rápido (repo)

1) Instale o core:

```bash
uv sync
```

2) Instale a extensão em modo editável:

```bash
uv pip install -e extensions/hello_lamp
```

3) (Re)build do bundle do frontend da extensão:

```bash
npm --workspace @toposync/extension-hello-lamp-ui run build
```

4) Rode o backend e o frontend host:

```bash
uv run toposync serve
```

```bash
npm --workspace @toposync/frontend run dev
```

## Como testar no app

1) Abra `http://localhost:5173`
2) Clique em **Editar**
3) Em “Elementos disponíveis”, adicione **Lâmpada (Hello Lamp)**
4) Clique em **Voltar**
5) Clique no objeto 3D para abrir o modal de ação (toggle)
