# Agent notes for this repo

- Author name, when needed: `Mateus Calza`.
- Commit messages: use short conventional summaries such as `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`, or `i18n: ...`; add a scope only when useful and do not add a trailing period.
- Preserve user work. Do not delete or revert code you do not understand; if a change may be parallel work, leave it unstaged.
- Local app command for testing/visual validation: `TOPOSYNC_AUTH_MODE=bypass npm run dev`.
- Default development data directory: `.toposync-data`. Manual `config.json` edits are supported, but prefer UI/API validation and keep pipeline graph schema valid.
- For frontend routes, links, API calls, event streams, WebSockets, and extension asset/file URLs, preserve Home Assistant ingress paths with Toposync base path helpers.
- Keep the core generic. Domain-specific behavior belongs in extensions; avoid core hacks for extension-specific cases.
- Prefer targeted verification: run the smallest relevant pytest, typecheck, build, docs build, or distribution smoke test for the files changed.
