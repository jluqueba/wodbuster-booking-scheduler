#!/usr/bin/env pwsh
# Windows / PowerShell equivalent of `make check`.
# Runs ruff, mypy, djlint, and pytest (excluding live_contract). Exits
# non-zero on first failure.

$ErrorActionPreference = "Stop"

Write-Host "==> ruff check" -ForegroundColor Cyan
ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> mypy src" -ForegroundColor Cyan
mypy src
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> djlint templates" -ForegroundColor Cyan
djlint src/wodbuster_worker/templates --lint
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> pytest" -ForegroundColor Cyan
pytest -m "not live_contract"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> all checks passed" -ForegroundColor Green
