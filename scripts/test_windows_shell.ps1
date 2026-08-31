param(
    [Parameter(Mandatory)] [string]$ShellPath,
    [Parameter(Mandatory)] [string]$CorePath
)

$ErrorActionPreference = 'Stop'
$ShellPath = (Resolve-Path -LiteralPath $ShellPath).Path
$CorePath = (Resolve-Path -LiteralPath $CorePath).Path
Add-Type -AssemblyName UIAutomationClient

function Stop-TestProcesses([Diagnostics.Process]$Shell) {
    if (-not $Shell.HasExited) {
        Stop-Process -Id $Shell.Id -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'AzureHealthBeaconCore.exe' -and $_.ExecutablePath -eq $CorePath
    } | ForEach-Object {
        # A PyInstaller parent and worker can both match; stopping either may
        # make the other exit before this snapshot is consumed.
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Start-TestShell([string]$LocalData) {
    $env:AZURE_HEALTH_BEACON_ALLOW_SECOND_INSTANCE = '1'
    $env:AZURE_HEALTH_BEACON_CORE = $CorePath
    $env:LOCALAPPDATA = $LocalData
    $process = Start-Process -FilePath $ShellPath -PassThru
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        $process.Refresh()
    } until ($process.HasExited -or $process.MainWindowHandle -ne 0 -or (Get-Date) -ge $deadline)
    if ($process.HasExited -or $process.MainWindowHandle -eq 0) {
        throw 'The Windows shell did not open a window within 20 seconds.'
    }
    Start-Sleep -Seconds 2
    return $process
}

function Find-Element(
    [Windows.Automation.AutomationElement]$Root,
    [string]$Name,
    [Windows.Automation.ControlType]$Type
) {
    $condition = [Windows.Automation.AndCondition]::new(
        [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::NameProperty, $Name
        ),
        [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::ControlTypeProperty, $Type
        )
    )
    return $Root.FindFirst([Windows.Automation.TreeScope]::Descendants, $condition)
}

function Require-Element($Element, [string]$Description) {
    if ($null -eq $Element) { throw "Missing UI element: $Description" }
}

function Invoke-Element([Windows.Automation.AutomationElement]$Element) {
    $pattern = [Windows.Automation.InvokePattern]$Element.GetCurrentPattern(
        [Windows.Automation.InvokePattern]::Pattern
    )
    $pattern.Invoke()
    Start-Sleep -Milliseconds 400
}

$oldLocal = $env:LOCALAPPDATA
$oldCore = $env:AZURE_HEALTH_BEACON_CORE
$oldSecond = $env:AZURE_HEALTH_BEACON_ALLOW_SECOND_INSTANCE
$testBase = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$testRoot = Join-Path $testBase "azure-health-beacon-shell-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null

try {
    $firstUse = Join-Path $testRoot 'first-use'
    $shell = Start-TestShell $firstUse
    try {
        $root = [Windows.Automation.AutomationElement]::FromHandle($shell.MainWindowHandle)
        Require-Element (Find-Element $root 'Sign in with Microsoft' ([Windows.Automation.ControlType]::Button)) 'first-use Microsoft sign-in button'
        Require-Element (Find-Element $root 'Test credentials and finish setup' ([Windows.Automation.ControlType]::Button)) 'credential-test button'
    }
    finally { Stop-TestProcesses $shell }

    $configured = Join-Path $testRoot 'configured'
    $data = Join-Path $configured 'AzureHealthBeacon'
    New-Item -ItemType Directory -Path $data -Force | Out-Null
    $fixture = @{
        schema_version = 6
        onboarding_completed = $true
        azure_subscription_id = '00000000-0000-0000-0000-000000000001'
        azure_subscription_name = 'UI test subscription'
        azure_tenant_id = '00000000-0000-0000-0000-000000000002'
        connection_established_utc = (Get-Date).ToUniversalTime().ToString('o')
        connection_purge_pending = $false
        interval_minutes = 5
        timeout_seconds = 5
        retry_count = 0
        update_mode = 'manual'
        last_update_check_utc = ''
        start_with_windows = $false
        start_minimized = $false
        theme_mode = 'dark'
        checks = @()
    }
    [IO.File]::WriteAllText(
        (Join-Path $data 'checks.json'),
        ($fixture | ConvertTo-Json -Depth 5),
        [Text.UTF8Encoding]::new($false)
    )

    $shell = Start-TestShell $configured
    try {
        $root = [Windows.Automation.AutomationElement]::FromHandle($shell.MainWindowHandle)
        $add = Find-Element $root 'Add your first check' ([Windows.Automation.ControlType]::Button)
        Require-Element $add 'add-first-check button'
        Invoke-Element $add

        $sources = @(
            'Provisioning state', 'VM power state', 'Resource property (advanced)',
            'Resource Graph', 'Logs / Application Insights', 'Azure Monitor metric'
        )
        foreach ($source in $sources) {
            Require-Element (Find-Element $root $source ([Windows.Automation.ControlType]::Button)) "signal source $source"
        }
        $continue = Find-Element $root 'Continue  →' ([Windows.Automation.ControlType]::Button)
        Require-Element $continue 'Continue button'
        if ($continue.Current.IsEnabled) {
            throw 'Continue was enabled before the user selected a signal source.'
        }

        Invoke-Element (Find-Element $root 'Resource Graph' ([Windows.Automation.ControlType]::Button))
        $continue = Find-Element $root 'Continue  →' ([Windows.Automation.ControlType]::Button)
        if (-not $continue.Current.IsEnabled) {
            throw 'Continue did not enable after an explicit signal-source choice.'
        }
        Invoke-Element $continue
        Require-Element (Find-Element $root 'KQL query — returned rows are confirmed findings' ([Windows.Automation.ControlType]::Text)) 'Resource Graph KQL editor label'
        Require-Element (Find-Element $root 'Test without saving' ([Windows.Automation.ControlType]::Button)) 'live rule-test button'
        Require-Element (Find-Element $root 'Save and enable' ([Windows.Automation.ControlType]::Button)) 'save-and-enable button'
    }
    finally { Stop-TestProcesses $shell }
}
finally {
    $env:LOCALAPPDATA = $oldLocal
    $env:AZURE_HEALTH_BEACON_CORE = $oldCore
    $env:AZURE_HEALTH_BEACON_ALLOW_SECOND_INSTANCE = $oldSecond
}

Write-Host 'Windows shell first-use and no-preselection flows passed.'
