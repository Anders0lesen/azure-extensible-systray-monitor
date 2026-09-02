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
$oldPyiArchive = $env:_PYI_ARCHIVE_FILE
$oldPyiHome = $env:_PYI_APPLICATION_HOME_DIR
$oldPyiLevel = $env:_PYI_PARENT_PROCESS_LEVEL
$oldPyiReset = $env:PYINSTALLER_RESET_ENVIRONMENT
$testBase = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$testRoot = Join-Path $testBase "azure-health-beacon-shell-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null

# Carry the stale runtime state inherited from the retired Python front end
# through every launch. The WPF shell must reset it for its private engine.
$env:_PYI_ARCHIVE_FILE = Join-Path $testRoot 'retired-AzureHealthBeacon.exe'
$env:_PYI_APPLICATION_HOME_DIR = Join-Path $testRoot '_MEI-retired-runtime'
$env:_PYI_PARENT_PROCESS_LEVEL = '2'
Remove-Item Env:PYINSTALLER_RESET_ENVIRONMENT -ErrorAction SilentlyContinue

try {
    $firstUse = Join-Path $testRoot 'first-use'
    $shell = Start-TestShell $firstUse
    try {
        $root = [Windows.Automation.AutomationElement]::FromHandle($shell.MainWindowHandle)
        Require-Element (Find-Element $root 'Sign in with Microsoft' ([Windows.Automation.ControlType]::Button)) 'first-use Microsoft sign-in button'
        Require-Element (Find-Element $root 'Test credentials and finish setup' ([Windows.Automation.ControlType]::Button)) 'credential-test button'

        foreach ($navigationName in @('Overview', 'Settings', 'About')) {
            $navigation = Find-Element $root $navigationName ([Windows.Automation.ControlType]::Button)
            Require-Element $navigation "first-use $navigationName navigation"
            if (-not $navigation.Current.IsEnabled) {
                throw "$navigationName was blocked before Azure credentials were accepted."
            }
        }
        Invoke-Element (Find-Element $root 'Settings' ([Windows.Automation.ControlType]::Button))
        Require-Element (Find-Element $root 'Check for updates' ([Windows.Automation.ControlType]::Button)) 'first-use Settings update button'
        Invoke-Element (Find-Element $root 'About' ([Windows.Automation.ControlType]::Button))
        Require-Element (Find-Element $root 'Check for updates' ([Windows.Automation.ControlType]::Button)) 'first-use About update button'
        Require-Element (Find-Element $root 'Open diagnostic log' ([Windows.Automation.ControlType]::Button)) 'first-use sanitized diagnostic-log button'
        Invoke-Element (Find-Element $root 'Overview' ([Windows.Automation.ControlType]::Button))
        Require-Element (Find-Element $root 'Sign in with Microsoft' ([Windows.Automation.ControlType]::Button)) 'return from recovery navigation to setup'
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
    # This UI-only fixture never makes an Azure request. Presence of both
    # opaque files represents a configured encrypted identity without placing
    # any real token material in the test workspace.
    $identity = Join-Path $data 'identity'
    New-Item -ItemType Directory -Path $identity -Force | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $identity 'token-cache.bin'), [byte[]](1))
    [IO.File]::WriteAllBytes((Join-Path $identity 'account-state.bin'), [byte[]](1))

    $shell = Start-TestShell $configured
    try {
        $root = [Windows.Automation.AutomationElement]::FromHandle($shell.MainWindowHandle)

        Invoke-Element (Find-Element $root 'Settings' ([Windows.Automation.ControlType]::Button))
        $saveSettings = Find-Element $root 'Save settings' ([Windows.Automation.ControlType]::Button)
        $deleteConnection = Find-Element $root 'Delete Azure connection…' ([Windows.Automation.ControlType]::Button)
        $checkUpdates = Find-Element $root 'Check for updates' ([Windows.Automation.ControlType]::Button)
        Require-Element $saveSettings 'compact save-settings button'
        Require-Element $deleteConnection 'compact delete-connection button'
        Require-Element $checkUpdates 'compact check-updates button'
        foreach ($button in @($saveSettings, $deleteConnection, $checkUpdates)) {
            if ($button.Current.BoundingRectangle.Width -gt 260) {
                throw "Settings action expanded beyond its compact width: $($button.Current.Name)"
            }
        }
        $editCondition = [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::ControlTypeProperty,
            [Windows.Automation.ControlType]::Edit
        )
        $settingsEdits = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $editCondition)
        if ($settingsEdits.Count -ne 3) {
            throw "Expected three compact monitoring fields; found $($settingsEdits.Count)."
        }
        foreach ($edit in $settingsEdits) {
            if ($edit.Current.BoundingRectangle.Width -gt 100) {
                throw 'A monitoring value field expanded beyond its compact width.'
            }
        }
        if ($deleteConnection.Current.BoundingRectangle.Left -le $settingsEdits[0].Current.BoundingRectangle.Left) {
            throw 'Settings did not use the available horizontal space as two columns.'
        }

        Invoke-Element (Find-Element $root 'Checks' ([Windows.Automation.ControlType]::Button))
        $add = Find-Element $root '＋  Add a check' ([Windows.Automation.ControlType]::Button)
        Require-Element $add 'add-check button'
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
    $env:_PYI_ARCHIVE_FILE = $oldPyiArchive
    $env:_PYI_APPLICATION_HOME_DIR = $oldPyiHome
    $env:_PYI_PARENT_PROCESS_LEVEL = $oldPyiLevel
    $env:PYINSTALLER_RESET_ENVIRONMENT = $oldPyiReset
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host 'Windows shell first-use and no-preselection flows passed.'
