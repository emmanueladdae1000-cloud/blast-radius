# Builds the image and scores it on the 30 sample commits, under the scorer's limits.
# Run from this folder in PowerShell:   .\test_local.ps1
$ErrorActionPreference = "Stop"
docker build --platform linux/amd64 -t blast-radius-entry .
$here = (Get-Location).Path
cmd /c "docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp -v `"$here\sample\commits:/data/commits:ro`" -v `"$here\sample\tests.json:/data/tests.json:ro`" blast-radius-entry > output.jsonl"
py score.py sample sample\answers.json output.jsonl
