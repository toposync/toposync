# Agent notes for this repo

- Commit message style (based on existing history):
  - Prefer `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`, `i18n: ...`.
  - Use an optional scope when it helps (`feat(pipelines): ...`).
  - Keep the summary short, imperative, and without a trailing period.
- Não apague código que você não entende só por “limpeza”; se achar que pode ser trabalho em paralelo (do usuário ou de outro agente), deixe no working tree e apenas não inclua no commit (ex.: `git add -p`).
- Se o `toposync` estiver em execução, não edite `/.toposync-data/config.json` diretamente: alterações manuais podem ser sobrescritas pelo estado gravado/syncado pela UI e pelo serviço.
- Prefira editar via wizard/API e reiniciar/reativar a composição depois para que a persistência fique estável.
