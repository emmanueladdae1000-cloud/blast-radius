$ErrorActionPreference='Stop'
$root=$PSScriptRoot
Set-Location -LiteralPath $root
docker build --platform linux/amd64 --tag rwx-under120-candidate .
if ($LASTEXITCODE -ne 0) { throw 'Production dependency image build failed' }
docker build --platform linux/amd64 --tag rwx-under120-profiler --file Dockerfile.profile .
if ($LASTEXITCODE -ne 0) { throw 'Profiler image build failed' }
$profileDir=Join-Path $root ('reports\profiles-'+[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $profileDir | Out-Null
$src='/baseline/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
foreach ($seed in @(217,291)) {
    $log=Join-Path $profileDir "seed-$seed.log"
    Write-Output "Profiling unmodified pinned source, seed $seed; output: $log"
    docker run --rm --platform linux/amd64 --network none --cpus 4 --memory 8g --pids-limit 1024 --read-only --tmpfs /tmp:rw,size=512m,mode=1777 --env PYTHONDONTWRITEBYTECODE=1 --env PYTHON_CPU_COUNT=4 --env SKIP_INTEGRATION=0 --env BR_RAWCMP_ENABLE=1 --env "PYTHONHASHSEED=$seed" --env "SEEDTAG=$seed" --env "PYTHONPATH=/profile_tools:$src" --env "BR_PROFILE_SRC=$src" --env BR_PROFILE_OUT=/out --env BR_TESTS_JSON=/data/tests.json --volume "${root}/tools:/profile_tools:ro" --volume "${root}/sample/tests.json:/data/tests.json:ro" --volume "${profileDir}:/out" --workdir $src --entrypoint python rwx-under120-profiler -m pytest -p rawcmp_plugin -n 4 --dist worksteal --timeout=180 --timeout-method=signal --tb=short -q -o console_output_style=count tests -k 'not test_max_nodes and not test_memory_leak' *> $log
    $exitCode=$LASTEXITCODE
    Get-Content -LiteralPath $log -Tail 5
    if ($exitCode -ne 0) { throw "Baseline pytest run failed with $exitCode; inspect $log before using its observations." }
}
docker run --rm --network none --read-only --env PYTHONDONTWRITEBYTECODE=1 --volume "${root}:/audit" --volume "${profileDir}:/observations:ro" --entrypoint python rwx-under120-profiler /audit/tools/merge_rawcmp.py /observations /audit/operand_trace.json.gz /audit/sample/tests.json
if ($LASTEXITCODE -ne 0) { throw 'Baseline observation merge failed' }
Write-Output 'Rebuilt raw baseline observations. Run test_local.ps1 before submitting.'