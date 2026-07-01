#!/usr/bin/env pwsh
# Windows / PowerShell equivalent of `make check`.
# Runs ruff, mypy, and pytest (excluding live_contract). Exits non-zero on
# first failure.

$ErrorActionPreference = "Stop"

Write-Host "==> ruff check" -ForegroundColor Cyan
ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> mypy src" -ForegroundColor Cyan
mypy src
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> pytest" -ForegroundColor Cyan
pytest -m "not live_contract"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> all checks passed" -ForegroundColor Green
