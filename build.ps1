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
& $VenvPython -m PyInstaller --noconfirm --clean --onefile --windowed --name AzureHealthBeacon --paths (Join-Path $ProjectRoot 'src') (Join-Path $ProjectRoot 'launcher.py')

Write-Host "Built: $(Join-Path $ProjectRoot 'dist\AzureHealthBeacon.exe')"
