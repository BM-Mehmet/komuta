# Repository Guidelines

## Project Structure & Module Organization

`app.py` contains the minimal terminal-style PyQt6 desktop application and its D-Bus toggle endpoint. `launch.sh` starts it, and `install.sh` adds KDE Plasma shortcut and session startup entries. `README.md` documents user setup. Runtime clipboard data is stored outside the repository under `~/.local/state/komuta/`.

## Build, Test, and Development Commands

This is a Python 3.10+ and PyQt6 application; no compilation step is needed. Run it with `python3 app.py` during development or `./launch.sh` for normal startup. On Arch Linux, install PyQt6 with `sudo pacman -S python-pyqt6`. `./install.sh` configures per-user KDE shortcuts and autostart; it does not need root privileges.

## Coding Style & Naming Conventions

Use four spaces for Python indentation, `snake_case` for functions and variables, and `PascalCase` for classes. Keep UI strings in Turkish, use Qt signals for UI updates from background work, and keep user data outside the source tree. Clipboard size and item limits are defined near the top of `app.py`; update the README when changing them. No formatter or linter is configured yet.

## Testing Guidelines

There is no test framework or test suite configured at present. Keep changes small and manually check UI and clipboard behavior on a PyQt6 desktop session. Add a test framework and document its command if the data or search logic grows beyond the current single-module implementation.

## Commit & Pull Request Guidelines

No Git history is available to establish a commit convention. Write concise, imperative commit subjects (for example, `Add clipboard pinning`) and keep each commit focused. Pull requests should explain the change, list manual checks, link related issues when applicable, and include screenshots for user-facing visual changes.

## Security & Configuration

Do not commit credentials, tokens, or machine-specific configuration. Provide safe example configuration with secrets omitted, and document required environment variables as the project adds them.
