# Persistência (local‑first)

- O backend salva a configuração em um único arquivo: `<data_dir>/config.json`
- Arquivos auxiliares do usuário ficam em: `<data_dir>/files/`
- Para definir o diretório explicitamente use `TOPOSYNC_DATA_DIR=/caminho/para/dados` (ou `toposync serve --data-dir ...`)
- Dica (dev): `uv run toposync serve --data-dir .toposync-data` (pasta ignorada pelo git)
- Default por SO:
  - Linux: `$XDG_DATA_HOME/toposync` ou `~/.local/share/toposync`
  - macOS: `~/Library/Application Support/Toposync`
  - Windows: `%APPDATA%/Toposync`

Se bater dúvida sobre *qual* diretório está em uso, chame `GET /api/system/paths`.

O frontend lê/salva a composição via `GET/PUT /api/composition`. Versões antigas usavam `localStorage` (`toposync.composition.v1`) e o app tenta migrar automaticamente quando o backend está vazio.

