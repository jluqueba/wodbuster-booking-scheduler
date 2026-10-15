# Contributing to WodBuster Booking Scheduler

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

1. Make sure an issue describes the change. If none exists, open one first and say in a comment that you are working on it.
2. Create a branch from `main` with one of these prefixes and a short slug: `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, `ci/`.
3. Make your changes with clear, incremental commits that follow [Conventional Commits](https://www.conventionalcommits.org/) (for example, `feat: add vacation window bulk cancel`).
4. Ensure the checks pass locally (`check.ps1` or `make check`) before submitting.
5. Open a Pull Request using the template. Its title is the squash commit message, so it must also follow Conventional Commits.

### Pull Request Guidelines

The full process, including how review findings are triaged, is described in [development-process.md](https://github.com/jluqueba/wodbuster-booking-scheduler/blob/main/.github/docs/development-process.md). In short:

- Direct pushes to `main` are blocked; every change goes through a pull request, and pull requests are squash-merged.
- Each pull request closes exactly one issue with `Closes #N` in its description and carries that issue's area and type labels.
- Keep each pull request to one topic and at most about 400 lines of production code, plus its tests. Split before opening, not during review.
- Do not combine more than one of these in a pull request: session cookie and authentication custody, the booking executor, a schema migration.
- List the invariants the change must hold and the test that proves each one. A change that acts on a WodBuster account, handles credentials or cookies, migrates or deletes data, or parses external responses gets them agreed with the maintainer before coding.
- List every validation command that was run, with its result.
- The CI check (`ci-gate`) must pass. It runs `ruff`, `mypy`, `djlint`, and `pytest` on every pull request, documentation included, because tests also check the published site.
- Copilot reviews every push. A pull request is ready to merge when CI is green, the latest Copilot review examined the head commit, and no correctness or security finding from it remains open. Other findings are minor: they are answered on their thread and resolved without a commit. After three review cycles with blocking findings left, the change is split or narrowed instead.

### Issues, Milestones and Labels

- The milestone goes on the issue, never on the pull request, so each milestone counts every task once.
- Issues carry one or more `area:` labels, a type label (`enhancement`, `bug`, `documentation`) and a size label (`size: S`, `size: M`, `size: L`).
- Issues are public: describe behaviour rather than file paths, and do not paste content from the encrypted design documents.
- Each area in the feature request form matches exactly one label:

  | Form choice | Label |
  | --- | --- |
  | Booking rules and scheduler | `area: booking` |
  | WodBuster client and session cookie | `area: wodbuster-client` |
  | Login and user access | `area: auth` |
  | Admin and user lifecycle | `area: admin` |
  | Gym accounts and discovery | `area: gyms` |
  | Notifications and alerts | `area: notifications` |
  | Attendance statistics | `area: statistics` |
  | Web interface and translations | `area: ui` |
  | Database and migrations | `area: persistence` |
  | Logging and monitoring | `area: observability` |
  | Azure infrastructure and deployment | `area: infra` |
  | CI and merge checks | `area: ci` |
  | Developer tooling | `area: tooling` |
  | Documentation | `area: docs` |

  A new area gets a label and a form choice in the same change.

## Code Standards

- Formatting and linting: `ruff` (see `ruff.toml`).
- Static typing: `mypy` (see `mypy.ini`); the package ships type hints and a `py.typed` marker.
- Tests: `pytest`, organized into unit, component, and contract suites under `tests/`.
- Engineering principles and style are described in [coding-guidelines.md](https://github.com/jluqueba/wodbuster-booking-scheduler/blob/main/.github/docs/coding-guidelines.md). In short: prefer simplicity and clarity over cleverness, keep abstractions minimal, and track technical debt as work items rather than TODO comments.

## Code of Conduct

This project follows its [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.

## Questions?

Open a [GitHub issue](https://github.com/jluqueba/wodbuster-booking-scheduler/issues) for questions, bug reports, or feature ideas.
