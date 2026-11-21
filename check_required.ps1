param([string]$ImageTag='rwx-under120-raw')
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$data=Join-Path $root 'reports\regression'
$tests=(Get-Content -Raw -LiteralPath (Join-Path $data 'tests.json') | ConvertFrom-Json).tests
$needed=Get-Content -Raw -LiteralPath (Join-Path $data 'minimum_required_tests.json') | ConvertFrom-Json
$argsList=@('run','--rm','--platform','linux/amd64','--network','none','--cpus','2','--memory','4096m','--memory-swap','4096m','--pids-limit','256','--read-only','--tmpfs','/tmp:rw,size=64m,mode=1777','--cap-drop','ALL','--security-opt','no-new-privileges','--user','65534:65534','--env','HOME=/tmp','--volume',"$data/commits:/data/commits:ro",'--volume',"$data/tests.json:/data/tests.json:ro",$ImageTag)
$lines=@(& docker @argsList 2> (Join-Path $data 'candidate.stderr.txt'))
if ($LASTEXITCODE -ne 0) { throw 'Regression selector run failed' }
$predictions=@{}
foreach ($line in $lines) { $row=$line | ConvertFrom-Json; $predictions[$row.commit]=@($row.workers | ForEach-Object { foreach ($n in $_) { $n } }) }
foreach ($entry in $needed.PSObject.Properties) {
    if (-not $predictions.ContainsKey($entry.Name)) { throw "Missing case $($entry.Name)" }
    $selected=$predictions[$entry.Name]
    foreach ($id in $entry.Value) {
        $test=@($tests | Where-Object { $_.id -eq $id })
        if ($test.Count -ne 1 -or $test[0].number -notin $selected) { throw "Missed focused failure: $($entry.Name) -> $id" }
    }
    Write-Output "PASS $($entry.Name): every independently confirmed failing test is retained."
}
[IO.File]::WriteAllText((Join-Path $data 'candidate.jsonl'),(($lines -join "`n")+"`n"),[Text.UTF8Encoding]::new($false))