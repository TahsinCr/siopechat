$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot

function Get-PythonVersion {
    param([string]$Interpreter, [switch]$UseLauncher)
    if ($UseLauncher) {
        $Output = & $Interpreter -3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>$null
    }
    else {
        $Output = & $Interpreter -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>$null
    }
    if ($LASTEXITCODE -ne 0 -or $Output -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
        return $null
    }
    return [Version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
}

function Test-VenvPython {
    param([string]$Interpreter)
    & $Interpreter -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix and sys.version_info >= (3, 11) else 1)' 2>$null
    return ($LASTEXITCODE -eq 0)
}

$SystemPython = $null
$SystemPythonUsesLauncher = $false
foreach ($Name in @('python3', 'python')) {
    $Command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $Command) { continue }
    $Version = Get-PythonVersion $Command.Source
    if ($null -ne $Version -and $Version -ge [Version]'3.11') {
        $SystemPython = $Command.Source
        break
    }
}
if (-not $SystemPython) {
    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($Launcher) {
        $Version = Get-PythonVersion $Launcher.Source -UseLauncher
        if ($null -ne $Version -and $Version -ge [Version]'3.11') {
            $SystemPython = $Launcher.Source
            $SystemPythonUsesLauncher = $true
        }
    }
}
if (-not $SystemPython) {
    throw 'Python 3.11 or newer is required to create the SiopeChat environment.'
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

$VenvDirectory = $null
$VenvPython = $null
foreach ($EnvironmentDirectory in $EnvironmentDirectories) {
    $Candidate = Join-Path $EnvironmentDirectory 'Scripts/python.exe'
    if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
        $Version = Get-PythonVersion $Candidate
        if ($null -ne $Version -and $Version -ge [Version]'3.11' -and (Test-VenvPython $Candidate)) {
            $VenvDirectory = $EnvironmentDirectory
            $VenvPython = $Candidate
            break
        }
        if ($ExplicitEnvironment) {
            throw "SIOPECHAT_VENV must point to a Python 3.11+ virtual environment (found $Version); it was left untouched."
        }
    }
    elseif ($ExplicitEnvironment -and (Test-Path -LiteralPath $EnvironmentDirectory)) {
        throw "The requested environment exists but has no interpreter at $Candidate; it was left untouched."
    }
}

$CreateEnvironment = $false
if (-not $VenvDirectory) {
    if ($ExplicitEnvironment) {
        $VenvDirectory = $EnvironmentDirectories[0]
        if (Test-Path -LiteralPath $VenvDirectory) {
            throw 'The requested environment directory already exists but is not a usable virtual environment; it was left untouched.'
        }
    }
    else {
        $VenvDirectory = Join-Path $Root 'python'
        if (Test-Path -LiteralPath $VenvDirectory) {
            throw "'$VenvDirectory' exists but is not a usable virtual environment; it was left untouched. Set SIOPECHAT_VENV to another path."
        }
    }
    $VenvPython = Join-Path $VenvDirectory 'Scripts/python.exe'
    $CreateEnvironment = $true
}

if ($CreateEnvironment) {
    Write-Host "Creating Python environment at $VenvDirectory"
    if ($SystemPythonUsesLauncher) {
        & $SystemPython -3 -m venv $VenvDirectory
    }
    else {
        & $SystemPython -m venv $VenvDirectory
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}

$Version = Get-PythonVersion $VenvPython
if ($null -eq $Version -or $Version -lt [Version]'3.11' -or -not (Test-VenvPython $VenvPython)) {
    throw "The selected environment must be a Python 3.11+ virtual environment (found $Version)."
}

Write-Host "Installing SiopeChat and its Python dependencies in $VenvDirectory"
& $VenvPython -m pip install --disable-pip-version-check --editable $Root
if ($LASTEXITCODE -ne 0) {
    throw 'Dependency installation failed. The environment was kept so you can retry after fixing the cause.'
}
Write-Host 'Installation complete. Start SiopeChat with .\siopechat.ps1 or .\siopechat.bat.'
