# Agent notes for this repo

- Commit message style (based on existing history):
  - Prefer `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`, `i18n: ...`.
  - Use an optional scope when it helps (`feat(pipelines): ...`).
  - Keep the summary short, imperative, and without a trailing period.
- Whenever you add a UI entry/option for the user, it must have clear value and make sense (don’t expose internal identifiers; generate them automatically).
- Don’t delete code you don’t understand just for “cleanup”; if you think it might be parallel work (by the user or another agent), leave it in the working tree and simply don’t include it in the commit (e.g., `git add -p`).
- If `toposync` is running, manual edits to `.toposync-data/config.json` are supported: the runtime reloads the file when it changes (mtime/size), and pipelines may be reconciled on the next orchestrator poll.
- Still prefer editing via the wizard/API (validation/normalization, less chance of conflicts). If editing manually, keep JSON + schema valid (notably `pipeline.graph.schema_version`); invalid files are renamed to `config.corrupt-*` and replaced with defaults. Avoid concurrent edits with the running UI/service (last-write-wins).
- TopoSync deve fornecer estruturas genéricas e empoderar extensões com recursos de base. Elas devem lidar com questões específicas de domínio, e não o TopoSync. Jamais aceite fazer "gambiarras" dentro do código do TopoSync para acomodar casos específicos de uma extensão.
