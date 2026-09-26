function Get-ListeningPids {
    param([int]$Port)

    $owners = @()
    try {
        $owners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess)
    } catch {
        foreach ($line in @(netstat -ano -p tcp 2>$null)) {
            if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
                $owners += [int]$Matches[1]
            }
        }
    }
    return @($owners | ForEach-Object { [int]$_ } | Select-Object -Unique)
}

function Get-ProjectListenerEntry {
    param([string]$Role, [string]$Root)

    $port = if ($Role -eq "backend") { 8000 } else { 5173 }
    $owners = @(Get-ListeningPids $port)
    if ($owners.Count -ne 1) { return $null }
    try {
        $details = Get-CimInstance Win32_Process -Filter "ProcessId = $($owners[0])" -ErrorAction Stop
        # Match the entire launcher command, including exact checkout paths.
        # A Python name, an HTTP response, or a path substring is not ownership proof.
        $exePattern = '(?:"[^"\r\n]+"|[^\s"]+)'
        $rootPattern = '(?:"' + [regex]::Escape($Root) + '"|' + [regex]::Escape($Root) + ')'
        if ($Role -eq "backend") {
            if ($details.Name -ne "python.exe") { return $null }
            $pattern = '^\s*' + $exePattern + '\s+-m\s+uvicorn\s+backend\.main:app\s+--app-dir\s+' + $rootPattern + '\s+--host\s+127\.0\.0\.1\s+--port\s+8000\s*$'
        } else {
            if ($details.Name -ne "node.exe") { return $null }
            $frontend = Join-Path $Root "frontend"
            $vite = Join-Path $frontend "node_modules\vite\bin\vite.js"
            $vitePattern = '(?:"' + [regex]::Escape($vite) + '"|' + [regex]::Escape($vite) + ')'
            $frontendPattern = '(?:"' + [regex]::Escape($frontend) + '"|' + [regex]::Escape($frontend) + ')'
            $pattern = '^\s*' + $exePattern + '\s+' + $vitePattern + '\s+' + $frontendPattern + '\s+--host\s+127\.0\.0\.1\s+--port\s+5173\s+--strictPort\s*$'
        }
        if ([string]::IsNullOrWhiteSpace($details.CommandLine) -or $details.CommandLine -notmatch $pattern) { return $null }
        $process = Get-Process -Id $owners[0] -ErrorAction Stop
        if (-not $process.Path -or -not $details.CreationDate -or
            [Math]::Abs(($process.StartTime.ToUniversalTime() - $details.CreationDate.ToUniversalTime()).TotalSeconds) -ge 2) {
            return $null
        }
        return [pscustomobject]@{
            role = $Role
            pid = $process.Id
            executable = $process.Path
            port = $port
            startedAt = $process.StartTime.ToUniversalTime().ToString("o")
        }
    } catch {
        # If process identity cannot be read, leave it alone.
        return $null
    }
}
