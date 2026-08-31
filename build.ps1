param(
    [switch]$Installer
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
$Python = Get-Command python.exe -ErrorAction SilentlyContinue
$Venv = Join-Path $ProjectRoot '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$DotNet = Get-Command dotnet.exe -ErrorAction SilentlyContinue

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
if (-not $DotNet -or [version](& $DotNet.Source --version) -lt [version]'10.0.100') {
    throw '.NET SDK 10 or newer was not found. Install it from dotnet.microsoft.com and try again.'
}
$VersionInfo = Join-Path $ProjectRoot 'build\version_info.txt'
$Version = & $VenvPython (Join-Path $ProjectRoot 'scripts\write_version_info.py') $VersionInfo
$BrandIcon = Join-Path $ProjectRoot 'assets\AzureHealthBeacon.ico'
& $VenvPython -m PyInstaller --noconfirm --clean --onefile --console --name AzureHealthBeaconCore --icon $BrandIcon --version-file $VersionInfo --paths (Join-Path $ProjectRoot 'src') (Join-Path $ProjectRoot 'core_launcher.py')
$ShellProject = Join-Path $ProjectRoot 'src\windows\AzureHealthBeacon.Shell\AzureHealthBeacon.Shell.csproj'
$ShellOutput = Join-Path $ProjectRoot 'build\windows-shell'
& $DotNet.Source publish $ShellProject -c Release -r win-x64 --self-contained true -o $ShellOutput "/p:Version=$Version" "/p:AssemblyVersion=$Version.0" "/p:FileVersion=$Version.0"
Copy-Item -LiteralPath (Join-Path $ShellOutput 'AzureHealthBeacon.exe') -Destination (Join-Path $ProjectRoot 'dist\AzureHealthBeacon.exe') -Force
& (Join-Path $ProjectRoot 'scripts\test_windows_shell.ps1') -ShellPath (Join-Path $ProjectRoot 'dist\AzureHealthBeacon.exe') -CorePath (Join-Path $ProjectRoot 'dist\AzureHealthBeaconCore.exe')

if ($Installer) {
    $Compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if (-not $Compiler) {
        $Candidates = @(
            (Join-Path $ProjectRoot '.tools\inno\ISCC.exe'),
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

Write-Host "Built shell: $(Join-Path $ProjectRoot 'dist\AzureHealthBeacon.exe')"
Write-Host "Built engine: $(Join-Path $ProjectRoot 'dist\AzureHealthBeaconCore.exe')"
