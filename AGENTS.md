# Agent notes for this repo

- Commit message style (based on existing history):
  - Prefer `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`, `i18n: ...`.
  - Use an optional scope when it helps (`feat(pipelines): ...`).
  - Keep the summary short, imperative, and without a trailing period.
- Whenever you add a UI entry/option for the user, it must have clear value and make sense (don’t expose internal identifiers; generate them automatically).
- Don’t delete code you don’t understand just for “cleanup”; if you think it might be parallel work (by the user or another agent), leave it in the working tree and simply don’t include it in the commit (e.g., `git add -p`).
- If `toposync` is running, don’t edit `/.toposync-data/config.json` directly: manual changes can be overwritten by state saved/synced by the UI and the service.
- Prefer editing via the wizard/API and then restarting/reactivating the composition to keep persistence stable.
- TopoSync deve fornecer estruturas genéricas e empoderar extensões com recursos de base. Elas devem lidar com questões específicas de domínio, e não o TopoSync. Jamais aceite fazer "gambiarras" dentro do código do TopoSync para acomodar casos específicos de uma extensão.
