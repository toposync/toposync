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
