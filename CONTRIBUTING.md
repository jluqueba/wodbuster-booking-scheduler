# Contributing to WodBuster Booking Worker

Thank you for your interest in contributing. This is a personal automation project, but issues and pull requests are welcome.

## Getting Started

You need Python 3.12 or newer and Docker Desktop (for the local Postgres container).

1. Clone the repository:

   ```bash
   git clone https://github.com/jluqueba/wodbuster-booking-scheduler.git
   cd wodbuster-booking-scheduler
   ```

2. Create and activate a virtual environment, then install the project with dev extras:

   Windows (PowerShell):

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   ```

   Linux or macOS:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. Start the local database: `docker compose up -d postgres`
4. Run the checks: `.\check.ps1` on Windows, or `make check` on Linux and macOS. This runs `ruff check`, `mypy src`, and `pytest`.

The full setup, configuration, and deployment walkthrough lives in the [Developer Guide](docs/DEVELOPER_GUIDE.md).

## How to Contribute

### Reporting Issues

- Search existing issues before creating a new one.
- Use a clear, descriptive title.
- Include steps to reproduce, expected behavior, and actual behavior.
- Add relevant labels if possible.

Do not include secrets, session cookies, or personal data in an issue. For anything security related, follow the [Security Policy](SECURITY.md) instead of opening a public issue.

### Submitting Changes

1. Create a branch from `main` using a short, descriptive prefix:
   - `feature/<description>` for new functionality
   - `fix/<description>` for bug fixes
   - `docs/<description>` for documentation
   - `chore/<description>` for tooling or maintenance
2. Make your changes with clear, incremental commits.
3. Follow the [Conventional Commits](https://www.conventionalcommits.org/) convention for commit messages (for example, `feat: add vacation window bulk cancel`).
4. Ensure the checks pass locally (`check.ps1` or `make check`) before submitting.
5. Open a Pull Request with a clear description of what changed and why.

### Pull Request Guidelines

- Direct pushes to `main` are blocked; every change goes through a pull request.
- Link the PR to the related issue when one exists.
- Keep PRs focused and reasonably sized.
- The description should answer two questions: what changed, and why.
- The CI check (`ci-gate`) must pass. It runs `ruff`, `mypy`, and `pytest`, and is skipped automatically for documentation-only changes.
- Copilot reviews each pull request automatically. The maintainer reviews and merges.

## Code Standards

- Formatting and linting: `ruff` (see `ruff.toml`).
- Static typing: `mypy` (see `mypy.ini`); the package ships type hints and a `py.typed` marker.
- Tests: `pytest`, organized into unit, component, and contract suites under `tests/`.
- Engineering principles and style are described in [coding-guidelines.md](.github/docs/coding-guidelines.md). In short: prefer simplicity and clarity over cleverness, keep abstractions minimal, and track technical debt as work items rather than TODO comments.

## Code of Conduct

This project follows its [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.

## Questions?

Open a [GitHub issue](https://github.com/jluqueba/wodbuster-booking-scheduler/issues) for questions, bug reports, or feature ideas.
