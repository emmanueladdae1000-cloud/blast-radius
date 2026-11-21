$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'benchmark.ps1') -ImageTag rwx-under120-entry -Label sample -Runs 3 -Build