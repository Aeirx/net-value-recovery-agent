<#
    Windows shim for the Makefile. `make` is not present on this machine, and CI runs on
    ubuntu where it is, so the two must stay in step: every target here mirrors a Makefile
    target of the same name.

    Usage:  .\make.ps1 check
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'install', 'test', 'lint', 'typecheck', 'check', 'smoke',
                 'boundary', 'config', 'reproduce', 'clean')]
    [string]$Target = 'help'
)

$ErrorActionPreference = 'Stop'
$py = 'python'

function Invoke-Step {
    param([string]$Label, [scriptblock]$Body)
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}

switch ($Target) {
    'help' {
        Write-Host 'install    - install the package and dev dependencies'
        Write-Host 'test       - run the test suite'
        Write-Host 'lint       - ruff'
        Write-Host 'typecheck  - mypy strict over world/ and agent/'
        Write-Host 'check      - lint + typecheck + test  (what CI runs)'
        Write-Host 'boundary   - run only the world/agent boundary guard'
        Write-Host 'config     - write data/config_a.json and print its hash'
        Write-Host 'smoke      - 50-transaction end-to-end smoke evaluation'
        Write-Host 'reproduce  - regenerate every number in the README from scratch'
    }
    'install'   { Invoke-Step 'install'   { & $py -m pip install -e ".[dev]" } }
    'test'      { Invoke-Step 'pytest'    { & $py -m pytest } }
    'lint'      { Invoke-Step 'ruff'      { & $py -m ruff check netvalue tests scripts } }
    'typecheck' { Invoke-Step 'mypy'      { & $py -m mypy } }
    'boundary'  { Invoke-Step 'boundary'  { & $py -m pytest tests/test_boundary.py -v } }
    'config'    { Invoke-Step 'config'    { & $py scripts/write_config.py } }
    'smoke'     { Invoke-Step 'smoke'     { & $py scripts/smoke_eval.py --n 50 } }
    'check' {
        Invoke-Step 'ruff'   { & $py -m ruff check netvalue tests scripts }
        Invoke-Step 'mypy'   { & $py -m mypy }
        Invoke-Step 'pytest' { & $py -m pytest }
    }
    'reproduce' {
        Invoke-Step 'config' { & $py scripts/write_config.py }
        Invoke-Step 'pytest' { & $py -m pytest -q }
        Write-Host '--- Phase 3+ stages append here (datasets, baselines, agent, sweeps, report)'
    }
    'clean' {
        Get-ChildItem -Recurse -Directory -Filter __pycache__ |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        foreach ($d in '.pytest_cache', '.mypy_cache', '.ruff_cache') {
            if (Test-Path $d) { Remove-Item -Recurse -Force $d }
        }
        Write-Host 'cleaned'
    }
}
