# Agent notes for this repo

- Always finish a change with a `git commit` (unless the user explicitly says not to).
- Commit message style (based on existing history):
  - Prefer `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`, `i18n: ...`.
  - Use an optional scope when it helps (`feat(pipelines): ...`).
  - Keep the summary short, imperative, and without a trailing period.
- Não apague código que você não entende só por “limpeza”; se achar que pode ser trabalho em paralelo (do usuário ou de outro agente), deixe no working tree e apenas não inclua no commit (ex.: `git add -p`).
