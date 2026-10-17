$ErrorActionPreference = 'Stop'
$Script = Join-Path $PSScriptRoot 'siopechat.py'
$LocalPython = Join-Path $PSScriptRoot 'python/Scripts/python.exe'
if (Test-Path $LocalPython) { & $LocalPython $Script @args }
elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3 $Script @args }
elseif (Get-Command python -ErrorAction SilentlyContinue) { & python $Script @args }
else { throw 'Python 3.10 or newer is required.' }
exit $LASTEXITCODE
