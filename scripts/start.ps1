param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSHOME "Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1") -Global -Force -ErrorAction Stop
$script:Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script:Frontend = Join-Path $script:Root "frontend"
$script:LauncherDir = Join-Path $script:Root ".cache\launcher"
$script:StateFile = Join-Path $script:LauncherDir "pids.json"
$script:BackendOut = Join-Path $script:LauncherDir "backend.out.log"
$script:BackendErr = Join-Path $script:LauncherDir "backend.err.log"
$script:FrontendOut = Join-Path $script:LauncherDir "frontend.out.log"
$script:FrontendErr = Join-Path $script:LauncherDir "frontend.err.log"
$script:StartedEntries = @()
. (Join-Path $PSScriptRoot "launcher-processes.ps1")
. (Join-Path $PSScriptRoot "latex-dependencies.ps1")

function Get-RuntimeRoot {
    $profileRoot = $env:USERPROFILE
    if ([string]::IsNullOrWhiteSpace($profileRoot)) {
        $profileRoot = [Environment]::GetFolderPath("UserProfile")
    }
    if ([string]::IsNullOrWhiteSpace($profileRoot)) {
        return $null
    }
    return (Join-Path $profileRoot ".cache\codex-runtimes\codex-primary-runtime\dependencies")
}

function Get-ExecutableCandidates {
    param([string[]]$Names, [string[]]$Fallbacks)

    $values = @()
    foreach ($name in $Names) {
        foreach ($command in @(Get-Command -Name $name -All -ErrorAction SilentlyContinue)) {
            $path = $command.Source
            if ([string]::IsNullOrWhiteSpace($path)) {
                $path = $command.Path
            }
            if (-not [string]::IsNullOrWhiteSpace($path)) {
                $values += $path
            }
        }
    }
    $values += $Fallbacks
    return @($values | Where-Object { $_ } | Select-Object -Unique)
}

function Get-ValidPython {
    param([string[]]$Candidates)

    foreach ($candidate in $Candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        try {
            $versionLines = @(& $candidate -c "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))" 2>$null)
            $candidateExitCode = $LASTEXITCODE
            $versionText = $versionLines | Select-Object -First 1
            if ($candidateExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($versionText)) {
                continue
            }
            $parts = $versionText.Trim().Split(".")
            $major = [int]$parts[0]
            $minor = [int]$parts[1]
            if (($major -gt 3) -or ($major -eq 3 -and $minor -ge 11)) {
                return [pscustomobject]@{ Path = $candidate; Version = "$major.$minor" }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Get-ValidNode {
    param([string[]]$Candidates)

    foreach ($candidate in $Candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        try {
            $versionLines = @(& $candidate --version 2>$null)
            $candidateExitCode = $LASTEXITCODE
            $versionText = $versionLines | Select-Object -First 1
            if ($candidateExitCode -ne 0 -or $versionText -notmatch "v?(\d+)\.(\d+)\.(\d+)") {
                continue
            }
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            $supported = (($major -eq 20) -and ($minor -ge 19)) -or (($major -ge 22) -and (($major -gt 22) -or ($minor -ge 12)))
            if ($supported) {
                return [pscustomobject]@{ Path = $candidate; Version = "$major.$minor.$($Matches[3])" }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Get-ValidPnpm {
    param([string[]]$Candidates)

    foreach ($candidate in $Candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        try {
            $versionLines = @(& $candidate --version 2>$null)
            $candidateExitCode = $LASTEXITCODE
            $versionText = $versionLines | Select-Object -First 1
            if ($candidateExitCode -eq 0 -and $versionText -match "\d+\.\d+\.\d+") {
                return [pscustomobject]@{ Path = $candidate; Version = $Matches[0] }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Test-PythonDependencies {
    param([string]$Python)

    try {
        $null = & $Python -c "import fastapi,uvicorn,httpx,pydantic,multipart,fitz,PIL,mistune" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Install-BackendDependencies {
    param([string]$Python)

    $requirements = Join-Path $script:Root "backend\requirements.txt"
    Write-Host "Synchronizing backend dependencies in .venv from requirements.txt..."
    & $Python -m pip --version | Out-Host
    if ($LASTEXITCODE -ne 0) {
        & $Python -m ensurepip --upgrade | Out-Host
        $pipCode = $LASTEXITCODE
        if ($pipCode -ne 0) {
            throw "pip is unavailable in .venv. Install Python 3.11+ with ensurepip enabled."
        }
    }
    & $Python -m pip install -r $requirements | Out-Host
    $installCode = $LASTEXITCODE
    if ($installCode -ne 0) {
        throw "Backend dependency installation failed. Check network access and backend\requirements.txt."
    }
    if (-not (Test-PythonDependencies $Python)) {
        throw "Backend dependencies are still incomplete after installation."
    }
}

function Test-TrackedProcess {
    param([object]$Entry, [string]$Root)

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

function Read-LauncherState {
    if (-not (Test-Path -LiteralPath $script:StateFile -PathType Leaf)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $script:StateFile -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Save-LauncherState {
    param([object]$Backend, [object]$Frontend)

    $state = [ordered]@{
        version = 1
        root = $script:Root
        startedAt = (Get-Date).ToUniversalTime().ToString("o")
        backend = $Backend
        frontend = $Frontend
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $script:StateFile -Encoding UTF8
}

function Stop-VerifiedEntries {
    param([object[]]$Entries)

    $stopEntries = @()
    foreach ($entry in @($Entries)) {
        if ($null -ne $entry) {
            $stopEntries += Get-ProjectListenerEntry $entry.role $script:Root
            $stopEntries += $entry
        }
    }
    foreach ($entry in $stopEntries) {
        if ($null -eq $entry -or $null -eq $entry.pid) {
            continue
        }
        if (Test-TrackedProcess $entry $script:Root) {
            try {
                Stop-Process -Id ([int]$entry.pid) -Force -ErrorAction Stop
                Write-Host ("Stopped {0} process {1}." -f $entry.role, $entry.pid)
            } catch {
                Write-Warning ("Could not stop {0} process {1}: {2}" -f $entry.role, $entry.pid, $_.Exception.Message)
            }
        } else {
            Write-Warning ("Did not stop PID {0}: it no longer matches this project." -f $entry.pid)
        }
    }
}

function Test-Url {
    param([string]$Url)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400)
    } catch {
        return $false
    }
}

function Show-LogTail {
    param([string]$Path)

    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Write-Host ("--- {0} ---" -f $Path)
        Get-Content -LiteralPath $Path -Tail 20 | ForEach-Object { Write-Host $_ }
    }
}

try {
    New-Item -ItemType Directory -Force -Path $script:LauncherDir | Out-Null
    if (-not (Test-Path -LiteralPath (Join-Path $script:Root "backend\main.py") -PathType Leaf)) {
        throw "backend\main.py was not found. Run this script from the project checkout."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $script:Frontend "package.json") -PathType Leaf)) {
        throw "frontend\package.json was not found. Run this script from the project checkout."
    }

    $state = Read-LauncherState
    if ($null -ne $state -and -not [string]::Equals([string]$state.root, $script:Root, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "Ignoring launcher state from a different project directory. Recorded PIDs will not be used; checking this project's listeners."
        $state = $null
    }
    $backendListener = Get-ProjectListenerEntry "backend" $script:Root
    $frontendListener = Get-ProjectListenerEntry "frontend" $script:Root
    if ($null -ne $backendListener -or $null -ne $frontendListener) {
        # Recover missing/stale records and track the actual listener, including
        # the child interpreter created by Windows venv's python.exe launcher.
        if ($null -ne $state -and [string]::Equals([string]$state.root, $script:Root, [StringComparison]::OrdinalIgnoreCase)) {
            if ($null -eq $backendListener -and (Test-TrackedProcess $state.backend $script:Root)) { $backendListener = $state.backend }
            if ($null -eq $frontendListener -and (Test-TrackedProcess $state.frontend $script:Root)) { $frontendListener = $state.frontend }
        }
        Save-LauncherState $backendListener $frontendListener
        $state = Read-LauncherState
    }
    $trackedBackend = $false
    $trackedFrontend = $false
    if ($null -ne $state -and [string]::Equals([string]$state.root, $script:Root, [StringComparison]::OrdinalIgnoreCase)) {
        $trackedBackend = Test-TrackedProcess $state.backend $script:Root
        $trackedFrontend = Test-TrackedProcess $state.frontend $script:Root
        if ($trackedBackend -or $trackedFrontend) {
            if ($trackedBackend -and $trackedFrontend -and (Test-Url "http://127.0.0.1:8000/api/health") -and (Test-Url "http://127.0.0.1:5173/")) {
                Write-Host "The project is already running (8000 and 5173)."
                if (-not $NoBrowser) {
                    Start-Process "http://127.0.0.1:5173/" | Out-Null
                }
                exit 0
            }
            throw "This project already has a tracked launcher process. Run ocr.bat Restart to restart it."
        }
    }

    foreach ($port in @(8000, 5173)) {
        $owners = @(Get-ListeningPids $port)
        if ($owners.Count -gt 0) {
            $ownerText = ($owners -join ", ")
            throw ("Port {0} is already in use by PID {1}. Stop that application yourself before starting this project." -f $port, $ownerText)
        }
    }
    if ($null -ne $state -and [string]::Equals([string]$state.root, $script:Root, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $script:StateFile)) {
        Remove-Item -LiteralPath $script:StateFile -Force
    }

    $runtimeRoot = Get-RuntimeRoot
    $fallbackPython = if ($runtimeRoot) { Join-Path $runtimeRoot "python\python.exe" } else { $null }
    $fallbackNode = if ($runtimeRoot) { Join-Path $runtimeRoot "node\bin\node.exe" } else { $null }
    $fallbackPnpm = if ($runtimeRoot) { Join-Path $runtimeRoot "bin\fallback\pnpm.cmd" } else { $null }

    $venvDir = Join-Path $script:Root ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    $runPython = $null
    $usingVenv = $false
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $localPython = Get-ValidPython @($venvPython)
        if ($null -eq $localPython) {
            throw ".venv exists but its Python is not a working Python 3.11+ interpreter."
        }
        $runPython = $localPython.Path
        $usingVenv = $true
    } else {
        $pythonCandidates = Get-ExecutableCandidates @("python.exe", "python3.exe") @($fallbackPython)
        $basePython = Get-ValidPython $pythonCandidates
        if ($null -eq $basePython) {
            throw "No working Python 3.11+ interpreter was found. Install Python or repair PATH."
        }
        Write-Host ("Using Python {0} ({1})." -f $basePython.Version, $basePython.Path)
        if (Test-Path -LiteralPath $venvDir) {
            Write-Host "A partial .venv was found; using the verified interpreter while it remains usable."
            if (Test-PythonDependencies $basePython.Path) {
                $runPython = $basePython.Path
            } else {
                throw ".venv is incomplete and the fallback Python has no backend dependencies. Remove the partial .venv and rerun."
            }
        } else {
            Write-Host "Creating the project-local .venv..."
            & $basePython.Path -m venv $venvDir | Out-Host
            $venvCode = $LASTEXITCODE
            if ($venvCode -eq 0 -and (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
                $createdPython = Get-ValidPython @($venvPython)
                if ($null -ne $createdPython) {
                    $runPython = $createdPython.Path
                    $usingVenv = $true
                }
            }
            if ($null -eq $runPython -and (Test-PythonDependencies $basePython.Path)) {
                Write-Host "The bundled Python has the required packages; using it without modifying global Python."
                $runPython = $basePython.Path
            }
            if ($null -eq $runPython) {
                throw "Could not create a usable .venv. Install Python 3.11+ with venv/ensurepip enabled."
            }
        }
    }

    $requirements = Join-Path $script:Root "backend\requirements.txt"
    $backendDependencyMarker = Join-Path $script:LauncherDir "backend-dependencies.sha256"
    $requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
    $installedRequirementsHash = if (Test-Path -LiteralPath $backendDependencyMarker -PathType Leaf) {
        (Get-Content -LiteralPath $backendDependencyMarker -Raw).Trim()
    } else { $null }
    $backendDependenciesReady = Test-PythonDependencies $runPython
    if ($usingVenv) {
        if (-not $backendDependenciesReady -or $installedRequirementsHash -ne $requirementsHash) {
            Install-BackendDependencies $runPython
            Set-Content -LiteralPath $backendDependencyMarker -Value $requirementsHash -Encoding ASCII
        }
    } else {
        if (-not $backendDependenciesReady) {
            throw "Backend dependencies are missing from the available runtime. A local .venv is required; install Python 3.11+ with venv support."
        }
    }
    Write-Host ("Backend Python ready: {0}" -f $runPython)

    $nodeCandidates = Get-ExecutableCandidates @("node.exe") @($fallbackNode)
    $node = Get-ValidNode $nodeCandidates
    if ($null -eq $node) {
        throw "No supported Node.js was found. Vite requires Node 20.19+ or 22.12+."
    }
    Write-Host ("Using Node.js {0} ({1})." -f $node.Version, $node.Path)

    $pnpmCandidates = Get-ExecutableCandidates @("pnpm.cmd", "pnpm.exe") @($fallbackPnpm)
    $pnpm = Get-ValidPnpm $pnpmCandidates
    if ($null -eq $pnpm) {
        throw "pnpm was not found. Install pnpm or make the bundled runtime available."
    }
    Write-Host ("Using pnpm {0}." -f $pnpm.Version)

    $viteCli = Join-Path $script:Frontend "node_modules\vite\bin\vite.js"
    $frontendDependencyMarker = Join-Path $script:LauncherDir "frontend-dependencies.sha256"
    $packageHash = (Get-FileHash -LiteralPath (Join-Path $script:Frontend "package.json") -Algorithm SHA256).Hash
    $lockHash = (Get-FileHash -LiteralPath (Join-Path $script:Frontend "pnpm-lock.yaml") -Algorithm SHA256).Hash
    $frontendDependencyHash = $packageHash + $lockHash
    $installedFrontendHash = if (Test-Path -LiteralPath $frontendDependencyMarker -PathType Leaf) {
        (Get-Content -LiteralPath $frontendDependencyMarker -Raw).Trim()
    } else { $null }
    if ($installedFrontendHash -ne $frontendDependencyHash -or -not (Test-Path -LiteralPath $viteCli -PathType Leaf)) {
        Write-Host "Synchronizing frontend dependencies from pnpm-lock.yaml..."
        $previousCI = $env:CI
        Push-Location $script:Frontend
        try {
            $env:CI = "true"
            & $pnpm.Path install --frozen-lockfile | Out-Host
            $frontendCode = $LASTEXITCODE
        } finally {
            $env:CI = $previousCI
            Pop-Location
        }
        if ($frontendCode -ne 0) {
            throw "Frontend dependency installation failed. Check network access and frontend\pnpm-lock.yaml."
        }
        if (-not (Test-Path -LiteralPath $viteCli -PathType Leaf)) {
            throw "Vite is still missing after frontend dependency installation."
        }
        Set-Content -LiteralPath $frontendDependencyMarker -Value $frontendDependencyHash -Encoding ASCII
    }

    $xelatex = Initialize-LatexDependencies -DownloadDirectory (Join-Path $script:Root ".cache\dependency-downloads")
    Write-Host ("LaTeX ready: {0}" -f $xelatex)

    $backendArgs = @(
        "-m", "uvicorn", "backend.main:app", "--app-dir", ('"' + $script:Root + '"'),
        "--host", "127.0.0.1", "--port", "8000"
    )
    Write-Host "Starting backend on http://127.0.0.1:8000 ..."
    $backendProcess = Start-Process -FilePath $runPython -ArgumentList $backendArgs -WorkingDirectory $script:Root -RedirectStandardOutput $script:BackendOut -RedirectStandardError $script:BackendErr -WindowStyle Hidden -PassThru
    $backendEntry = [ordered]@{ role = "backend"; pid = $backendProcess.Id; executable = $runPython; port = 8000; startedAt = $backendProcess.StartTime.ToUniversalTime().ToString("o") }
    $script:StartedEntries += [pscustomobject]$backendEntry
    Save-LauncherState $script:StartedEntries[0] $null

    $frontendArgs = @(
        ('"' + $viteCli + '"'), ('"' + $script:Frontend + '"'),
        "--host", "127.0.0.1", "--port", "5173", "--strictPort"
    )
    Write-Host "Starting frontend on http://127.0.0.1:5173 ..."
    $frontendProcess = Start-Process -FilePath $node.Path -ArgumentList $frontendArgs -WorkingDirectory $script:Frontend -RedirectStandardOutput $script:FrontendOut -RedirectStandardError $script:FrontendErr -WindowStyle Hidden -PassThru
    $frontendEntry = [ordered]@{ role = "frontend"; pid = $frontendProcess.Id; executable = $node.Path; port = 5173; startedAt = $frontendProcess.StartTime.ToUniversalTime().ToString("o") }
    $script:StartedEntries += [pscustomobject]$frontendEntry
    Save-LauncherState $script:StartedEntries[0] $script:StartedEntries[1]

    $deadline = (Get-Date).AddSeconds(30)
    $backendReady = $false
    $frontendReady = $false
    while ((Get-Date) -lt $deadline) {
        $backendReady = Test-Url "http://127.0.0.1:8000/api/health"
        $frontendReady = Test-Url "http://127.0.0.1:5173/"
        if ($backendReady -and $frontendReady) {
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $backendReady) {
        Show-LogTail $script:BackendErr
        throw "Backend did not become healthy within 30 seconds."
    }
    if (-not $frontendReady) {
        Show-LogTail $script:FrontendErr
        throw "Frontend did not become healthy within 30 seconds."
    }

    Write-Host "Project is ready: http://127.0.0.1:5173/"
    if (-not $NoBrowser) {
        Start-Process "http://127.0.0.1:5173/" | Out-Null
    }
    exit 0
} catch {
    Write-Host ("ERROR: {0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host $_.InvocationInfo.PositionMessage
    Write-Host $_.ScriptStackTrace
    if ($script:StartedEntries.Count -gt 0) {
        Stop-VerifiedEntries $script:StartedEntries
    }
    $stateAfterFailure = Read-LauncherState
    if ($script:StartedEntries.Count -gt 0 -and $null -ne $stateAfterFailure -and [string]::Equals([string]$stateAfterFailure.root, $script:Root, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $script:StateFile -Force -ErrorAction SilentlyContinue
    }
    exit 1
}
