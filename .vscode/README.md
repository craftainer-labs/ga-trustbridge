# .vscode/

Workspace settings, tasks, launch configs, and extension recommendations
— committed so anyone who opens this repo in VS Code gets a working,
consistent setup without configuring anything by hand.

- `extensions.json` — recommends the Dev Containers extension.
- `settings.json` — editor/workspace settings that apply both inside and
  outside the devcontainer.
- `tasks.json` — lint/type-check/test tasks; run inside the devcontainer.
- `launch.json` — the debugpy config for running the app.

## Do

- Add a new lint/type-check/test entry point here as a task, mirroring
  the underlying `uv run ...` command exactly — don't reimplement it.

## Don't

- Add a task here that needs the host's Docker daemon (e.g. anything
  that `docker compose exec`s into a sibling container) — a task run
  from this window executes inside the devcontainer, which can't reach
  those containers. See
  `.devcontainer/stack/selenium/README.md`.
- Put container-only settings (interpreter path, in-container formatter)
  here — those belong in `devcontainer.json`'s
  `customizations.vscode.settings` instead, so they don't leak into a
  host-side window that never attaches to the container.
