param(
    [switch]$Installer
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
$Python = Get-Command python.exe -ErrorAction SilentlyContinue
$Venv = Join-Path $ProjectRoot '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if ($PythonLauncher) {
        & $PythonLauncher.Source -3.12 -m venv $Venv
    }
    elseif ($Python) {
        & $Python.Source -m venv $Venv
    }
    else {
        throw 'Python 3.12 was not found. Install it from python.org and try again.'
    }
}

& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot 'requirements-dev.txt')
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
& $VenvPython -m ruff check $ProjectRoot
& $VenvPython -m unittest discover -s (Join-Path $ProjectRoot 'tests') -v
$VersionInfo = Join-Path $ProjectRoot 'build\version_info.txt'
$Version = & $VenvPython (Join-Path $ProjectRoot 'scripts\write_version_info.py') $VersionInfo
$BrandIcon = Join-Path $ProjectRoot 'assets\AzureHealthBeacon.ico'
& $VenvPython -m PyInstaller --noconfirm --clean --onefile --windowed --name AzureHealthBeacon --icon $BrandIcon --add-data "$BrandIcon;assets" --version-file $VersionInfo --paths (Join-Path $ProjectRoot 'src') (Join-Path $ProjectRoot 'launcher.py')

if ($Installer) {
    $Compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if (-not $Compiler) {
        $Candidates = @(
            (Join-Path $env:ProgramFiles 'Inno Setup 7\ISCC.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
        )
        $CompilerPath = $Candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    }
    else {
        $CompilerPath = $Compiler.Source
    }
    if (-not $CompilerPath) {
        throw 'Inno Setup was not found. Install Inno Setup 7 or run the GitHub release workflow.'
    }
    & $CompilerPath "/DAppVersion=$Version" (Join-Path $ProjectRoot 'installer\AzureHealthBeacon.iss')
}

Write-Host "Built: $(Join-Path $ProjectRoot 'dist\AzureHealthBeacon.exe')"
