# Music setup

The active code is `acquisition/`.

For music requests, use the tools in [acquisition/AGENTS.md](acquisition/AGENTS.md).
Downloads and LLM advice stop at review. Final publication approval stays with
the user in the web app. Do not bypass agent restrictions with database edits
or user passwords. Use isolated fixtures to test publication.

Server-specific notes, if present, are in the gitignored `local/` directory; read
`local/AGENTS.server.md` before touching a running deployment. Never commit
anything from `local/`. Validate both Compose projects with `config --quiet`;
never print resolved configuration, which contains secrets. Container paths like
`/library` are not host paths. Consult the bind mounts first.

Source and template files are versioned. Configuration, credentials, databases,
media, runtime files and backups are excluded. Private credentials use mode `0600`.
Use an explicit file list when staging; check for secrets before pushing.

Run `uv sync --project acquisition --frozen --group dev`, then
`PYTHONPATH=acquisition acquisition/.venv/bin/pytest -q acquisition/tests`.
Check JS with `node --check acquisition/app/static/app.js`.
Deploy only reviewed changes, preserving private state and manual review.
