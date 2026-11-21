param(
    [string]$ImageTag = 'rwx-under120-baseline',
    [string]$DataPath = '',
    [string]$Label = 'baseline',
    [int]$Runs = 3,
    [switch]$Build
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $DataPath) { $DataPath = Join-Path $PSScriptRoot 'sample' }
$DataPath = (Resolve-Path -LiteralPath $DataPath).Path
$reportDir = Join-Path $PSScriptRoot 'reports'
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
if ($Build) {
    docker build --platform linux/amd64 --tag $ImageTag .
    if ($LASTEXITCODE -ne 0) { throw 'Docker build failed; no older image will be tested.' }
}
$runner = @"
import subprocess,resource,sys,time,json
start=time.monotonic()
p=subprocess.run([sys.executable,'/app/selector.py'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=175)
sys.stdout.buffer.write(p.stdout)
sys.stdout.buffer.flush()
sys.stderr.buffer.write(p.stderr)
sys.stderr.buffer.flush()
c=resource.getrusage(resource.RUSAGE_CHILDREN)
s=resource.getrusage(resource.RUSAGE_SELF)
print('CPU_META='+json.dumps({'cpu_seconds':c.ru_utime+c.ru_stime+s.ru_utime+s.ru_stime,'wall_seconds':time.monotonic()-start,'exit_code':p.returncode}),file=sys.stderr)
sys.exit(p.returncode)
"@
$records = @()
for ($i = 1; $i -le $Runs; $i++) {
    $outputPath = Join-Path $reportDir "$Label-$i.jsonl"
    $stderrPath = Join-Path $reportDir "$Label-$i.stderr.txt"
    $runArgs = @('run','--rm','--interactive','--platform','linux/amd64','--network','none','--cpus','2','--memory','4096m','--memory-swap','4096m','--pids-limit','256','--read-only','--tmpfs','/tmp:rw,size=64m,mode=1777','--cap-drop','ALL','--security-opt','no-new-privileges','--user','65534:65534','--env','HOME=/tmp','--volume',"$DataPath/commits:/data/commits:ro",'--volume',"$DataPath/tests.json:/data/tests.json:ro",'--entrypoint','python',$ImageTag,'-')
    $outputLines = @($runner | & docker @runArgs 2> $stderrPath)
    $containerExit = $LASTEXITCODE
    [IO.File]::WriteAllText($outputPath, (($outputLines -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
    if ($containerExit -ne 0) { throw "Container failed with exit $containerExit. See $stderrPath" }
    $stderrText = [IO.File]::ReadAllText($stderrPath)
    $cpuLine = [regex]::Match($stderrText, 'CPU_META=(\{[^\r\n]+\})')
    if (-not $cpuLine.Success) { throw 'Missing CPU measurement.' }
    $metrics = $cpuLine.Groups[1].Value | ConvertFrom-Json
    $cpuArg = ([double]$metrics.cpu_seconds).ToString('R', [Globalization.CultureInfo]::InvariantCulture)
    $scoreArgs = @('run','--rm','--network','none','--read-only','--env','PYTHONDONTWRITEBYTECODE=1','--volume',"${PSScriptRoot}:/audit:ro",'--volume',"${DataPath}:/eval:ro",'--entrypoint','python',$ImageTag,'/audit/score.py','/eval','/eval/answers.json',"/audit/reports/$Label-$i.jsonl",'--cpu-seconds',$cpuArg)
    $scoreLines = @(& docker @scoreArgs)
    if ($LASTEXITCODE -ne 0) { throw "Official local scorer failed for run $i." }
    $scoreText = $scoreLines -join "`n"
    Write-Output "RUN $i CPU=$($metrics.cpu_seconds) WALL=$($metrics.wall_seconds)"
    Write-Output $scoreText
    $records += [pscustomobject]@{ run=$i; cpu_seconds=$metrics.cpu_seconds; wall_seconds=$metrics.wall_seconds; scorer=$scoreText }
}
$summaryPath = Join-Path $reportDir "$Label-summary.json"
[IO.File]::WriteAllText($summaryPath, ($records | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))