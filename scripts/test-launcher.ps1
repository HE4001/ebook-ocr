$ErrorActionPreference = "Stop"
$sourceDir = $PSScriptRoot
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("ocr-launcher-test-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $testRoot | Out-Null

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

try {
    # Execute the real script bodies in isolated runspaces with OS/process mocks.
    # No live services are started or stopped, and state files use a temporary root.
    $setup = @'
param($sourceDir, $fixtureRoot, $scenario)
$ErrorActionPreference = "Stop"
$script:Root = $fixtureRoot
$script:Frontend = Join-Path $fixtureRoot "frontend"
$script:LauncherDir = Join-Path $fixtureRoot ".cache\launcher"
$script:StateFile = Join-Path $script:LauncherDir "pids.json"
$script:StartedEntries = @()
$NoBrowser = $true
foreach ($directory in @($script:LauncherDir, (Join-Path $fixtureRoot "backend"), (Join-Path $script:Frontend "node_modules\vite\bin"), (Join-Path $fixtureRoot ".venv\Scripts"))) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}
foreach ($file in @("backend\main.py", "frontend\package.json", "frontend\node_modules\vite\bin\vite.js", ".venv\Scripts\python.exe")) {
    Set-Content -LiteralPath (Join-Path $fixtureRoot $file) -Value "fixture"
}
$script:started = [DateTime]::UtcNow.AddMinutes(-2)
$script:backendCommand = '"C:\Python\python.exe" -m uvicorn backend.main:app --app-dir "' + $fixtureRoot + '" --host 127.0.0.1 --port 8000'
$script:frontendCommand = '"C:\Node\node.exe" "' + (Join-Path $script:Frontend "node_modules\vite\bin\vite.js") + '" "' + $script:Frontend + '" --host 127.0.0.1 --port 5173 --strictPort'
$script:stopped = @()
$fileName = if ($scenario -like "stop-*") { "stop.ps1" } else { "start.ps1" }
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $sourceDir $fileName), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw "Invalid launcher syntax" }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
. (Join-Path $sourceDir "launcher-processes.ps1")
function Get-ListeningPids {
    param([int]$Port)
    if ($Port -eq 8000) { return 101 }
    if ($scenario -ne "partial") { return 202 }
}
function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    if ($scenario -eq "unreadable") { throw "Access denied" }
    $isBackend = $Filter -eq "ProcessId = 101"
    $command = if ($isBackend) { $script:backendCommand } else { $script:frontendCommand }
    if ($scenario -in @("foreign", "stop-foreign")) { $command = $command.Replace($fixtureRoot, ($fixtureRoot + "-other")) }
    if ($scenario -eq "extra-args") { $command += " --reload" }
    [pscustomobject]@{ Name = $(if ($isBackend) { "python.exe" } else { "node.exe" }); CommandLine = $command; CreationDate = $script:started }
}
function Get-Process {
    param($Id, $ErrorAction)
    if ($script:stopped -contains [int]$Id) { throw "Process is gone" }
    $exe = if ([int]$Id -eq 202) { "C:\Node\node.exe" } else { "C:\Python\python.exe" }
    $startTime = if ($scenario -eq "recycled-pid") { $script:started.AddSeconds(10) } else { $script:started }
    [pscustomobject]@{ Id = [int]$Id; Path = $exe; StartTime = $startTime }
}
function Stop-Process {
    param($Id, [switch]$Force, $ErrorAction)
    $script:stopped += [int]$Id
    Add-Content -LiteralPath (Join-Path $fixtureRoot "stopped.txt") -Value $Id
}
function Start-Process { throw "Unexpected process launch" }
function Get-RuntimeRoot { return $null }
function Get-ValidPython { [pscustomobject]@{ Path = "fixture-python"; Version = "3.12" } }
function Test-PythonDependencies { return $true }
function Get-ExecutableCandidates { return @("fixture-runtime") }
function Get-ValidNode { [pscustomobject]@{ Path = "fixture-node"; Version = "24.17.0" } }
function Get-ValidPnpm { [pscustomobject]@{ Path = "fixture-pnpm"; Version = "11.19.0" } }
function Test-Url { return ($scenario -ne "unhealthy") }
if ($scenario -in @("stale", "stop-parent")) {
    $entry = [pscustomobject]@{ role = "backend"; pid = 303; executable = "C:\Python\python.exe"; port = 8000; startedAt = $script:started.ToString("o") }
    if ($scenario -eq "stale") { $entry.startedAt = $script:started.AddDays(-1).ToString("o") }
    @{ root = $fixtureRoot; backend = $entry; frontend = $null } | ConvertTo-Json -Depth 5 | Set-Content $script:StateFile
}
if ($scenario -eq "malformed") { Set-Content $script:StateFile "{broken" }
$main = @($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.TryStatementAst] })[-1]
. ([scriptblock]::Create($main.Extent.Text))
'@
    foreach ($scenario in @("missing", "stale", "malformed", "partial", "unhealthy", "foreign", "unreadable", "extra-args", "recycled-pid", "stop-missing", "stop-parent", "stop-foreign")) {
        $fixture = Join-Path $testRoot $scenario
        $runner = [powershell]::Create()
        try {
            $null = $runner.AddScript($setup).AddArgument($sourceDir).AddArgument($fixture).AddArgument($scenario)
            $null = $runner.Invoke()
            if ($runner.HadErrors) { throw ($runner.Streams.Error | Out-String) }
            $messages = ($runner.Streams.Information | ForEach-Object { $_.ToString() }) -join "`n"
            if ($scenario -in @("missing", "stale", "malformed")) {
                Assert-True ($messages -match "The project is already running") "$scenario did not reuse healthy services"
            } elseif ($scenario -in @("partial", "unhealthy")) {
                Assert-True ($messages -match "Run ocr.bat Restart") "$scenario did not give recovery instructions"
            } elseif ($scenario -notlike "stop-*") {
                Assert-True ($messages -match "Port 8000 is already in use") "$scenario did not reject the conflicting listener"
            } else {
                Assert-True ($messages -notmatch "ERROR:") "$scenario failed to stop cleanly"
            }
        } finally { $runner.Dispose() }
        $statePath = Join-Path $fixture ".cache\launcher\pids.json"
        $stopsPath = Join-Path $fixture "stopped.txt"
        if ($scenario -in @("missing", "stale", "malformed", "partial", "unhealthy")) {
            Assert-True (Test-Path $statePath) "$scenario lost the recovered state"
            $state = Get-Content $statePath -Raw | ConvertFrom-Json
            Assert-True ($state.backend.pid -eq 101) "$scenario did not track the actual Python listener"
            if ($scenario -ne "partial") { Assert-True ($state.frontend.pid -eq 202) "$scenario did not track the frontend" }
            Assert-True (-not (Test-Path $stopsPath)) "$scenario stopped an existing service"
        } elseif ($scenario -in @("stop-missing", "stop-parent")) {
            $stops = @(Get-Content $stopsPath)
            Assert-True ($stops[0] -eq "101" -and $stops[1] -eq "202") "$scenario did not stop the listeners first"
            if ($scenario -eq "stop-parent") { Assert-True ($stops[2] -eq "303") "Recorded launcher parent was not stopped" }
            Assert-True (-not (Test-Path $statePath)) "$scenario left stale state"
        } else {
            Assert-True (-not (Test-Path $statePath)) "$scenario adopted an unverified process"
            Assert-True (-not (Test-Path $stopsPath)) "$scenario stopped an unverified process"
        }
        Write-Host "PASS: $scenario"
    }

    $dispatchSetup = @'
param($sourceDir, $scenario)
$ErrorActionPreference = "Stop"
$NoBrowser = $true
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $sourceDir "ocr.ps1"), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw "Invalid unified launcher syntax" }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
$script:calls = @()
function Invoke-LauncherScript {
    param($Name, [switch]$WithoutBrowser)
    $script:calls += $Name
    if ($Name -eq "start.ps1" -and -not $WithoutBrowser) { throw "NoBrowser flag was lost" }
    if ($Name -eq "stop.ps1" -and $scenario -eq "stop-failed") { return 7 }
    return 0
}
function Wait-OcrPortsFree {
    $script:calls += "wait"
    return ($scenario -ne "port-busy")
}
function Show-OcrStatus { $script:calls += "status" }
$selected = switch ($scenario) {
    "start" { "Start" }
    "stop" { "Stop" }
    "status" { "Status" }
    default { "Restart" }
}
$code = Invoke-OcrAction $selected
[pscustomobject]@{ Code = $code; Calls = ($script:calls -join ",") }
'@
    foreach ($case in @(
        @{ Name = "start"; Code = 0; Calls = "start.ps1" },
        @{ Name = "stop"; Code = 0; Calls = "stop.ps1,wait" },
        @{ Name = "restart"; Code = 0; Calls = "stop.ps1,wait,start.ps1" },
        @{ Name = "status"; Code = 0; Calls = "status" },
        @{ Name = "stop-failed"; Code = 7; Calls = "stop.ps1" },
        @{ Name = "port-busy"; Code = 1; Calls = "stop.ps1,wait" }
    )) {
        $runner = [powershell]::Create()
        try {
            $null = $runner.AddScript($dispatchSetup).AddArgument($sourceDir).AddArgument($case.Name)
            $result = @($runner.Invoke())
            if ($runner.HadErrors) { throw ($runner.Streams.Error | Out-String) }
            Assert-True ($result.Count -eq 1 -and $result[0].Code -eq $case.Code -and $result[0].Calls -eq $case.Calls) "Incorrect dispatch for $($case.Name)"
        } finally { $runner.Dispose() }
        Write-Host "PASS: unified-$($case.Name)"
    }
} finally {
    # Only this test's freshly created temporary directory may be removed.
    $resolved = [IO.Path]::GetFullPath($testRoot)
    $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\ocr-launcher-test-'
    if (-not $resolved.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Invalid test cleanup path" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
