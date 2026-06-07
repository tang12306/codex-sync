# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python 3.10+ package for Codex Sync, a Windows-side sync desktop console and agent core. Core modules live in `codex_sync/`; notable areas include CLI wiring in `cli.py`, configuration in `config.py`, backup/sync logic in `full_backup.py`, `server.py`, and `sync_server.py`, and Windows integration in `windows_task.py` and `wsl.py`. The local web desktop UI is under `codex_sync/web/` with `js/`, `styles/`, and `index.html`. Tests live in `tests/` and are organized by feature, for example `test_config.py`, `test_full_backup.py`, and `test_integration_http.py`. Generated files such as `dist/`, `build/`, `__pycache__/`, and `.pytest_cache/` should stay untracked.

## Build, Test, and Development Commands

Use PowerShell from the repository root.

```powershell
python -m pip install -e .
python -m pytest
python -m codex_sync desktop
python -m codex_sync desktop-legacy
python sync_server.py
```

`pip install -e .` installs the `codex-sync` console entry point in editable mode. `python -m pytest` runs the test suite; tests also use `unittest` style classes. `python -m codex_sync desktop` opens the web desktop UI, while `desktop-legacy` runs the Tkinter console. `python sync_server.py` starts the local test server on `http://127.0.0.1:8888`.

## Coding Style & Naming Conventions

Follow the existing Python style: 4-space indentation, type hints where practical, `snake_case` functions and variables, `PascalCase` classes, and small command handlers named `cmd_<action>`. Keep CLI output JSON serializable and prefer `ensure_ascii=False` when printing user-facing JSON. Web UI files use plain JavaScript modules and CSS split by responsibility (`tokens.css`, `layout.css`, `components.css`).

## Testing Guidelines

Add or update tests in `tests/test_<feature>.py`. Prefer isolated temporary directories and environment overrides such as `CODEX_SYNC_HOME` for filesystem tests. Cover configuration redaction, backup safety, sync edge cases, and Windows-task behavior when those areas change. Run `python -m pytest` before submitting changes.

## Commit & Pull Request Guidelines

No Git history is available in this checkout, so use clear imperative commit messages, for example `Add backup import validation`. Pull requests should include a short problem statement, the change summary, test results, and screenshots for visible web desktop changes.

## Security & Configuration Tips

Do not commit real tokens or local deployment config. Keep `deploy.json` private and use `deploy.example.json` as the shareable template. Full conversation uploads are plaintext unless explicitly allowed with `--allow-plaintext-upload`; avoid enabling that in tests or examples unless the behavior is the subject under test.
