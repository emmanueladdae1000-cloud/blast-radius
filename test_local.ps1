$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'benchmark.ps1') -ImageTag rwx-under110-entry -Label sample -Runs 3 -Build