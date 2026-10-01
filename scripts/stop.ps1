$ErrorActionPreference = "Stop"
$script:Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script:LauncherDir = Join-Path $script:Root ".cache\launcher"
$script:StateFile = Join-Path $script:LauncherDir "pids.json"
. (Join-Path $PSScriptRoot "launcher-processes.ps1")

function Test-TrackedProcess {
    param([object]$Entry)
    if ($null -eq $Entry -or $null -eq $Entry.pid -or [string]::IsNullOrWhiteSpace([string]$Entry.startedAt)) {
        return $false
    }
    if (($Entry.role -ne "backend" -or [int]$Entry.port -ne 8000) -and
        ($Entry.role -ne "frontend" -or [int]$Entry.port -ne 5173)) {
        return $false
    }
    try {
        $process = Get-Process -Id ([int]$Entry.pid) -ErrorAction Stop
        $expectedStart = [DateTime]::Parse(
            [string]$Entry.startedAt,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
        if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $expectedStart).TotalSeconds) -ge 2) {
            return $false
        }
        return [String]::Equals(
            [string]$process.Path,
            [string]$Entry.executable,
            [StringComparison]::OrdinalIgnoreCase
        )
    } catch {
        return $false
    }
}

try {
    $state = $null
    if (Test-Path -LiteralPath $script:StateFile -PathType Leaf) {
        try { $state = Get-Content -LiteralPath $script:StateFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $state = $null }
    }
    if ($null -ne $state -and -not [string]::Equals([string]$state.root, $script:Root, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "Ignoring launcher state from a different project directory. Recorded PIDs will not be used; checking this project's listeners."
        $state = $null
    }

    # Stop verified listeners before their recorded launcher parents.
    $entries = @((Get-ProjectListenerEntry "backend" $script:Root), (Get-ProjectListenerEntry "frontend" $script:Root), $state.backend, $state.frontend)
    foreach ($entry in $entries) {
        if ($null -eq $entry -or $null -eq $entry.pid) {
            continue
        }
        if (Test-TrackedProcess $entry) {
            Stop-Process -Id ([int]$entry.pid) -Force -ErrorAction Stop
            Write-Host ("Stopped {0} process {1}." -f $entry.role, $entry.pid)
        } else {
            Write-Host ("Did not stop PID {0}: it is gone or no longer matches this project." -f $entry.pid)
        }
    }
    if (Test-Path -LiteralPath $script:StateFile) { Remove-Item -LiteralPath $script:StateFile -Force }
    Write-Host "Project launcher state cleared."
    exit 0
} catch {
    Write-Host ("ERROR: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
