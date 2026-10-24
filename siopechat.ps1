$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Script = Join-Path $Root 'siopechat.py'

$NodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $NodeCommand) {
    throw 'SiopeChat requires Node.js 22 or newer. Install Node.js and try again.'
}
$NodeVersion = (& $NodeCommand.Source --version 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $NodeVersion -notmatch '^v?(\d+)\.') {
    throw "Could not read the Node.js version (reported: $NodeVersion)."
}
if ([int]$Matches[1] -lt 22) {
    throw "SiopeChat requires Node.js 22 or newer; found $NodeVersion."
}

function Get-PythonVersion {
    param([string]$Interpreter)
    $Output = & $Interpreter -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>$null
    if ($LASTEXITCODE -ne 0 -or $Output -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
        return $null
    }
    return [Version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
}

function Test-VenvPython {
    param([string]$Interpreter)
    & $Interpreter -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix and sys.version_info >= (3, 10) else 1)' 2>$null
    return ($LASTEXITCODE -eq 0)
}

$EnvironmentDirectories = @()
if ($null -ne [Environment]::GetEnvironmentVariable('SIOPECHAT_VENV')) {
    $ConfiguredEnvironment = [Environment]::GetEnvironmentVariable('SIOPECHAT_VENV')
    if ([string]::IsNullOrWhiteSpace($ConfiguredEnvironment)) {
        throw 'SIOPECHAT_VENV is set but empty. Set it to a virtual-environment directory.'
    }
    if ([IO.Path]::IsPathRooted($ConfiguredEnvironment)) {
        $EnvironmentDirectories += $ConfiguredEnvironment
    }
    else {
        $EnvironmentDirectories += (Join-Path $Root $ConfiguredEnvironment)
    }
    $ExplicitEnvironment = $true
}
else {
    $EnvironmentDirectories = @('python', '.venv', 'venv', 'env') | ForEach-Object { Join-Path $Root $_ }
    $ExplicitEnvironment = $false
}

$VenvPython = $null
foreach ($EnvironmentDirectory in $EnvironmentDirectories) {
    $Candidate = Join-Path $EnvironmentDirectory 'Scripts/python.exe'
    if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
        $Version = Get-PythonVersion $Candidate
        if ($null -ne $Version -and $Version -ge [Version]'3.10' -and (Test-VenvPython $Candidate)) {
            $VenvPython = $Candidate
            break
        }
        if ($ExplicitEnvironment) {
            throw "SIOPECHAT_VENV must point to a Python 3.10+ virtual environment (found $Version)."
        }
    }
    elseif ($ExplicitEnvironment) {
        throw "The SIOPECHAT_VENV directory has no interpreter at $Candidate."
    }
}

if (-not $VenvPython) {
    throw "No usable Python environment found. Run '$Root\install.ps1' first."
}

& $VenvPython $Script @args
exit $LASTEXITCODE
