# Dot-source this file, then call Initialize-LatexDependencies before starting the backend.

function Resolve-LatexApplication {
    param([string]$Name)

    if ([string]::IsNullOrEmpty($Name) -or $Name.IndexOfAny([char[]]'*?') -ge 0) {
        return $null
    }
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        return [IO.Path]::GetFullPath($command.Path)
    }
    if ((Test-Path -LiteralPath $Name -PathType Leaf) -and [IO.Path]::GetExtension($Name) -in @('.exe', '.com', '.bat', '.cmd')) {
        return (Resolve-Path -LiteralPath $Name -ErrorAction Stop).ProviderPath
    }
    return $null
}

function Find-LatexExecutable {
    $configured = $env:EBOOK_OCR_XELATEX
    if ([string]::IsNullOrEmpty($configured)) {
        $configured = [Environment]::GetEnvironmentVariable('EBOOK_OCR_XELATEX', 'User')
    }
    if (-not [string]::IsNullOrEmpty($configured)) {
        $executable = Resolve-LatexApplication $configured
        if ($null -ne $executable) { return $executable }
        Write-Warning "EBOOK_OCR_XELATEX is invalid: '$configured'. Continuing with automatic XeLaTeX discovery."
    }

    $executable = Resolve-LatexApplication 'xelatex'
    if ($null -ne $executable) { return $executable }
    foreach ($location in @(
        @{ Root = 'APPDATA'; Bin = 'windows' },
        @{ Root = 'PROGRAMDATA'; Bin = 'windows' },
        @{ Root = 'APPDATA'; Bin = 'win32' },
        @{ Root = 'PROGRAMDATA'; Bin = 'win32' }
    )) {
        $root = [Environment]::GetEnvironmentVariable($location.Root, 'Process')
        if ([string]::IsNullOrEmpty($root)) { continue }
        $candidate = Join-Path $root ("TinyTeX\bin\{0}\xelatex.exe" -f $location.Bin)
        $executable = Resolve-LatexApplication $candidate
        if ($null -ne $executable) { return $executable }
    }
    return $null
}

function Get-LatexDependencyPath {
    param([string]$Kpsewhich, [string]$File)

    $arguments = @($File)
    if ($File -eq 'xelatex.fmt') {
        $arguments = @('--engine=xetex', '--format=fmt', $File)
    }
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $Kpsewhich @arguments 2>$null)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($code -eq 0 -and -not [string]::IsNullOrWhiteSpace(($output -join "`n"))) {
        return ([string]$output[0]).Trim()
    }
    return $null
}

function Get-MissingLatexDependencies {
    param([string]$Kpsewhich)

    # Check binaries even when a stale format file still exists.
    $binDirectory = Split-Path -Parent $Kpsewhich
    foreach ($binary in @(
        @{ File = 'xelatex.exe'; Package = 'xetex' },
        @{ File = 'xetex.exe'; Package = 'xetex' },
        @{ File = 'xdvipdfmx.exe'; Package = 'dvipdfmx' }
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $binDirectory $binary.File) -PathType Leaf)) {
            $packages = @($binary.Package)
            if ((Split-Path -Leaf $binDirectory) -eq 'windows') { $packages += ($binary.Package + '.windows') }
            [pscustomobject]@{ File = $binary.File; Packages = $packages }
        }
    }

    # Package names are TeX Live names; tools and amsfonts each provide several files.
    $dependencies = @(
        @{ Packages = @('ctex'); Files = @('ctexbook.cls') },
        @{ Packages = @('geometry'); Files = @('geometry.sty') },
        @{ Packages = @('amsmath'); Files = @('amsmath.sty') },
        @{ Packages = @('amsfonts'); Files = @('amssymb.sty') },
        @{ Packages = @('tools'); Files = @('longtable.sty', 'array.sty') },
        @{ Packages = @('fancyhdr'); Files = @('fancyhdr.sty') },
        @{ Packages = @('ulem'); Files = @('ulem.sty') },
        @{ Packages = @('hyperref'); Files = @('hyperref.sty') },
        @{ Packages = @('fontspec'); Files = @('fontspec.sty') },
        @{ Packages = @('xecjk'); Files = @('xeCJK.sty') },
        @{ Packages = @('fandol'); Files = @(
            'FandolSong-Regular.otf', 'FandolSong-Bold.otf',
            'FandolHei-Regular.otf', 'FandolHei-Bold.otf',
            'FandolKai-Regular.otf', 'FandolFang-Regular.otf'
        ) },
        @{ Packages = @(); Files = @('xelatex.fmt') }
    )
    foreach ($dependency in $dependencies) {
        foreach ($file in $dependency.Files) {
            if ($null -eq (Get-LatexDependencyPath $Kpsewhich $file)) {
                [pscustomobject]@{ File = $file; Packages = $dependency.Packages }
                if ($file -eq 'xelatex.fmt') {
                    $binDirectory = Split-Path -Parent $Kpsewhich
                    if ((Split-Path -Leaf $binDirectory) -eq 'windows') {
                        foreach ($binary in @(
                            @{ File = 'xetex.exe'; Packages = @('xetex', 'xetex.windows') },
                            @{ File = 'latex.exe'; Packages = @('latex-bin', 'latex-bin.windows') }
                        )) {
                            if (-not (Test-Path -LiteralPath (Join-Path $binDirectory $binary.File) -PathType Leaf)) {
                                [pscustomobject]@{ File = $binary.File; Packages = $binary.Packages }
                            }
                        }
                    }
                }
            }
        }
    }
}

function Invoke-LatexTool {
    param([string]$Tool, [string[]]$Arguments, [string]$DownloadDirectory,
        [string]$WorkingDirectory = (Split-Path -Parent $Tool), [int]$TimeoutSeconds = 600, [switch]$Quiet)

    New-Item -ItemType Directory -Path $DownloadDirectory -Force -ErrorAction Stop | Out-Null
    $logName = 'latex-' + [IO.Path]::GetFileNameWithoutExtension($Tool) + '-' + [Guid]::NewGuid().ToString('N')
    $stdoutLog = Join-Path $DownloadDirectory ($logName + '.out.log')
    $stderrLog = Join-Path $DownloadDirectory ($logName + '.err.log')
    $program = $Tool
    $argumentText = $Arguments -join ' '
    if ([IO.Path]::GetExtension($Tool) -in @('.bat', '.cmd')) {
        $program = Join-Path ([Environment]::SystemDirectory) 'cmd.exe'
        $argumentText = '/d /s /c ""' + $Tool + '" ' + $argumentText + '"'
    }
    $previousLocale = $env:LC_ALL
    $previousLanguage = $env:LANG
    try {
        $env:LC_ALL = 'C'
        $env:LANG = 'C'
        # Separate logs keep Perl warnings from becoming PowerShell 5.1 errors.
        $process = Start-Process -FilePath $program -ArgumentList $argumentText -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru -ErrorAction Stop
        # Retain the handle so Windows PowerShell 5.1 can read ExitCode after exit.
        $null = $process.Handle
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            # Stop only the process tree started by this invocation.
            & (Join-Path ([Environment]::SystemDirectory) 'taskkill.exe') /PID $process.Id /T /F | Out-Null
            throw "LaTeX tool timed out after $TimeoutSeconds seconds. See $stdoutLog and $stderrLog."
        }
        $process.WaitForExit()
    } finally {
        $env:LC_ALL = $previousLocale
        $env:LANG = $previousLanguage
    }
    $stdout = [IO.File]::ReadAllText($stdoutLog)
    $stderr = [IO.File]::ReadAllText($stderrLog)
    if ($stdout -and -not $Quiet) { Write-Host $stdout }
    if ($stderr -and -not $Quiet) { Write-Host $stderr }
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = ($stdout + "`n" + $stderr) }
}

function Install-LatexPackages {
    param([string]$BinDirectory, [string[]]$Packages, [string]$DownloadDirectory)

    $tlmgr = Join-Path $BinDirectory 'tlmgr.bat'
    if (-not (Test-Path -LiteralPath $tlmgr -PathType Leaf)) {
        throw "Automatic dependency repair requires tlmgr.bat in $BinDirectory. Missing packages: $($Packages -join ', ')."
    }
    $arguments = @('install', '--reinstall') + @($Packages | Select-Object -Unique)
    $result = Invoke-LatexTool $tlmgr $arguments $DownloadDirectory
    if ($result.ExitCode -ne 0 -and $result.Output -match '(?is)(tlmgr itself needs to be updated|please update tlmgr itself|critical updates)') {
        $update = Invoke-LatexTool $tlmgr @('update', '--self') $DownloadDirectory
        if ($update.ExitCode -ne 0) { throw 'TeX Live package-manager update failed.' }
        $result = Invoke-LatexTool $tlmgr $arguments $DownloadDirectory
    }
    if ($result.ExitCode -ne 0) { throw "TeX Live package installation failed (exit $($result.ExitCode)). See logs in $DownloadDirectory." }
}

function Repair-LatexCompiler {
    param([string]$DownloadDirectory)

    $configured = $env:EBOOK_OCR_XELATEX
    if (-not $configured) { $configured = [Environment]::GetEnvironmentVariable('EBOOK_OCR_XELATEX', 'User') }
    $directories = @()
    if ($configured -and [IO.Path]::IsPathRooted($configured) -and $configured -notmatch '[*?]') {
        $directories += Split-Path -Parent $configured
    }
    foreach ($rootName in @('APPDATA', 'PROGRAMDATA')) {
        $root = [Environment]::GetEnvironmentVariable($rootName, 'Process')
        if ($root) {
            foreach ($bin in @('windows', 'win32')) { $directories += Join-Path $root "TinyTeX\bin\$bin" }
        }
    }
    foreach ($directory in @($directories | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath (Join-Path $directory 'tlmgr.bat') -PathType Leaf)) { continue }
        Write-Host "Repairing the existing TeX Live compiler in $directory..."
        $previousPath = $env:PATH
        try {
            $env:PATH = $directory + ';' + $previousPath
            $packages = @('xetex', 'latex-bin', 'dvipdfmx', 'kpathsea')
            if ((Split-Path -Leaf $directory) -eq 'windows') {
                $packages += @('xetex.windows', 'latex-bin.windows', 'dvipdfmx.windows', 'kpathsea.windows')
            }
            Install-LatexPackages $directory $packages $DownloadDirectory
            $result = Invoke-LatexTool (Join-Path $directory 'tlmgr.bat') @('postaction', 'install', 'script', 'xetex') $DownloadDirectory
            if ($result.ExitCode -ne 0) { throw 'XeTeX post-install configuration failed.' }
            $executable = Resolve-LatexApplication (Join-Path $directory 'xelatex.exe')
            if ($executable) { return $executable }
            throw "XeLaTeX is still missing after repair in $directory."
        } catch {
            Write-Warning "Existing TeX Live repair failed: $($_.Exception.Message)"
        } finally { $env:PATH = $previousPath }
    }
    return $null
}

function Test-LatexCompilation {
    param([string]$Executable, [string]$DownloadDirectory)

    $binDirectory = Split-Path -Parent $Executable
    $probeDirectory = Join-Path $DownloadDirectory ('latex-check-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $probeDirectory -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'latex-dependency-probe.tex') -Destination (Join-Path $probeDirectory 'probe.tex')
    $repaired = @()
    for ($attempt = 0; $attempt -le 20; $attempt++) {
        $result = Invoke-LatexTool $Executable @('-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error', 'probe.tex') $DownloadDirectory -WorkingDirectory $probeDirectory -TimeoutSeconds 120 -Quiet
        $pdf = Join-Path $probeDirectory 'probe.pdf'
        if ($result.ExitCode -eq 0 -and (Test-Path -LiteralPath $pdf -PathType Leaf) -and (Get-Item -LiteralPath $pdf).Length -gt 0) { return }
        # Only repair missing files reported by our own fixed probe, never user content.
        $match = [regex]::Match($result.Output, 'File [`'']([A-Za-z0-9_.-]+)[`''] not found')
        if (-not $match.Success -or $attempt -eq 20) { throw "LaTeX compilation verification failed. See $probeDirectory and logs in $DownloadDirectory." }
        $file = $match.Groups[1].Value
        if ($repaired -contains $file) { throw "LaTeX file $file is still missing after repair. See $probeDirectory." }
        $tlmgr = Join-Path $binDirectory 'tlmgr.bat'
        if (-not (Test-Path -LiteralPath $tlmgr -PathType Leaf)) { throw "Missing transitive dependency $file; no tlmgr.bat is available to repair it." }
        $pattern = '/' + [regex]::Escape($file) + '$'
        $search = Invoke-LatexTool $tlmgr @('search', '--global', '--file', $pattern) $DownloadDirectory
        $packages = @([regex]::Matches($search.Output, '(?m)^([a-zA-Z0-9][a-zA-Z0-9_.-]*):\s*$') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique)
        if ($search.ExitCode -ne 0 -or $packages.Count -eq 0) { throw "Cannot resolve a TeX Live package for $file. See logs in $DownloadDirectory." }
        Install-LatexPackages $binDirectory $packages $DownloadDirectory
        $repaired += $file
    }
}

function Install-LatexTinyTeX {
    param([string]$DownloadDirectory)

    $installRoot = $null
    foreach ($rootName in @('APPDATA', 'PROGRAMDATA')) {
        $root = [Environment]::GetEnvironmentVariable($rootName, 'Process')
        if ([string]::IsNullOrEmpty($root) -or -not [IO.Path]::IsPathRooted($root)) { continue }
        $candidate = [IO.Path]::GetFullPath($root)
        if ($candidate -match '[^\x00-\x7F]|\s') { continue }
        $installRoot = $candidate.TrimEnd([char[]]'\/')
        break
    }
    if ($null -eq $installRoot) {
        throw 'TinyTeX needs an APPDATA or PROGRAMDATA path containing only ASCII characters and no spaces. Install XeLaTeX yourself and set EBOOK_OCR_XELATEX.'
    }
    $destination = [IO.Path]::GetFullPath((Join-Path $installRoot 'TinyTeX'))

    New-Item -ItemType Directory -Path $DownloadDirectory -Force -ErrorAction Stop | Out-Null
    $cacheRoot = (Resolve-Path -LiteralPath $DownloadDirectory -ErrorAction Stop).ProviderPath.TrimEnd([char[]]'\/')
    Write-Host 'XeLaTeX was not found. Preparing the official TinyTeX-1 Windows release...'
    $previousProtocol = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = $previousProtocol -bor [Net.SecurityProtocolType]::Tls12
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/rstudio/tinytex-releases/releases/latest' -Headers @{
            'User-Agent' = 'ebook-ocr-launcher'
            'Accept' = 'application/vnd.github+json'
        } -ErrorAction Stop
        $assetName = 'TinyTeX-1-windows-{0}.exe' -f $release.tag_name
        $asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
        if ($null -eq $asset -or [string]$asset.digest -notmatch '^sha256:[0-9a-fA-F]{64}$') {
            throw "The official release does not provide $assetName with a SHA-256 digest."
        }
        $installer = Join-Path $cacheRoot $assetName
        $expectedHash = ([string]$asset.digest).Substring(7)
        $cachedHash = if (Test-Path -LiteralPath $installer -PathType Leaf) {
            (Get-FileHash -LiteralPath $installer -Algorithm SHA256 -ErrorAction Stop).Hash
        } else { '' }
        if ($cachedHash -ne $expectedHash) {
            Write-Host ("Downloading {0}..." -f $assetName)
            Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $installer -ErrorAction Stop
        }
        if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256 -ErrorAction Stop).Hash -ne $expectedHash) {
            throw "TinyTeX download failed SHA-256 verification: $installer. The installer was not executed."
        }
    } finally {
        [Net.ServicePointManager]::SecurityProtocol = $previousProtocol
    }

    $extractName = 'tinytex-extract-' + [Guid]::NewGuid().ToString('N')
    $extractDirectory = Join-Path $cacheRoot $extractName
    New-Item -ItemType Directory -Path $extractDirectory -ErrorAction Stop | Out-Null
    $stdoutLog = Join-Path $cacheRoot ($extractName + '.out.log')
    $stderrLog = Join-Path $cacheRoot ($extractName + '.err.log')
    Write-Host ("Extracting TinyTeX; logs: {0}, {1}" -f $stdoutLog, $stderrLog)
    $process = Start-Process -FilePath $installer -ArgumentList '-y' -WorkingDirectory $extractDirectory -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -Wait -PassThru -ErrorAction Stop
    if ($process.ExitCode -ne 0) {
        throw "TinyTeX extraction failed (exit $($process.ExitCode)). See $stdoutLog and $stderrLog."
    }

    $source = (Resolve-Path -LiteralPath (Join-Path $extractDirectory 'TinyTeX') -ErrorAction Stop).ProviderPath
    $expectedDestination = [IO.Path]::GetFullPath((Join-Path $installRoot 'TinyTeX'))
    if (-not $source.StartsWith($cacheRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals($destination, $expectedDestination, [StringComparison]::OrdinalIgnoreCase)) {
        throw "TinyTeX installation refused: source must be inside $cacheRoot and target must be $expectedDestination."
    }
    if (Test-Path -LiteralPath $destination) {
        # A concurrent launcher may have completed an installation during download.
        foreach ($binName in @('windows', 'win32')) {
            $existing = Resolve-LatexApplication (Join-Path $destination "bin\$binName\xelatex.exe")
            if ($existing) { return $existing }
        }
        $backup = [IO.Path]::GetFullPath($destination + '.backup-' + [Guid]::NewGuid().ToString('N'))
        if (-not [string]::Equals((Split-Path -Parent $backup), $installRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'TinyTeX backup target must remain inside the selected installation root.'
        }
        Write-Host "Preserving the incomplete TinyTeX installation in $backup"
        Move-Item -LiteralPath $destination -Destination $backup -ErrorAction Stop
    }
    # The cache and APPDATA may be on different volumes; retain the extraction cache.
    Copy-Item -LiteralPath $source -Destination $destination -Recurse -ErrorAction Stop
    foreach ($binName in @('windows', 'win32')) {
        $executable = Resolve-LatexApplication (Join-Path $destination ("bin\{0}\xelatex.exe" -f $binName))
        if ($null -ne $executable) { return $executable }
    }
    throw "The extracted TinyTeX installation has no XeLaTeX executable: $destination"
}

function Initialize-LatexDependencies {
    param([Parameter(Mandatory = $true)][string]$DownloadDirectory)

    if ($DownloadDirectory -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)') {
        throw 'DownloadDirectory must be an absolute Windows cache directory.'
    }
    $DownloadDirectory = [IO.Path]::GetFullPath($DownloadDirectory)
    $executable = Find-LatexExecutable
    if ($null -eq $executable) { $executable = Repair-LatexCompiler $DownloadDirectory }
    $installed = $null -eq $executable
    if ($installed) { $executable = Install-LatexTinyTeX $DownloadDirectory }
    $binDirectory = Split-Path -Parent $executable
    $previousPath = $env:PATH
    $env:PATH = $binDirectory + ';' + $previousPath
    try {
        Write-Host ("Checking XeLaTeX dependencies: {0}" -f $executable)
        $kpsewhich = Resolve-LatexApplication (Join-Path $binDirectory 'kpsewhich.exe')
        if ($null -eq $kpsewhich) {
            $packages = @('kpathsea')
            if ((Split-Path -Leaf $binDirectory) -eq 'windows') { $packages += 'kpathsea.windows' }
            Install-LatexPackages $binDirectory $packages $DownloadDirectory
            $kpsewhich = Resolve-LatexApplication (Join-Path $binDirectory 'kpsewhich.exe')
            if ($null -eq $kpsewhich) { throw "kpsewhich.exe is still missing beside $executable after repair." }
        }
        $tlmgr = Join-Path $binDirectory 'tlmgr.bat'
        if ($installed) {
            if (-not (Test-Path -LiteralPath $tlmgr -PathType Leaf)) {
                throw "The new TinyTeX installation is missing its TeX Live package manager: $tlmgr"
            }
            $result = Invoke-LatexTool $tlmgr @('postaction', 'install', 'script', 'xetex') $DownloadDirectory
            if ($result.ExitCode -ne 0) { throw 'TinyTeX XeTeX post-install configuration failed.' }
        }
        $missing = @(Get-MissingLatexDependencies $kpsewhich)
        if ($missing.Count -gt 0) {
            $missingFiles = ($missing | ForEach-Object { $_.File }) -join ', '
            if (-not (Test-Path -LiteralPath $tlmgr -PathType Leaf)) {
                throw "Missing LaTeX files: $missingFiles. No TeX Live tlmgr.bat exists beside the selected compiler, so automatic repair is unavailable. Install these dependencies in that distribution before starting the backend."
            }
            $packages = @($missing | ForEach-Object { $_.Packages } | Select-Object -Unique)
            if ($packages.Count -gt 0) {
                Write-Host ("Installing packages for missing LaTeX files ({0}): {1}" -f $missingFiles, ($packages -join ', '))
                Install-LatexPackages $binDirectory $packages $DownloadDirectory
            }
            if ($null -eq (Get-LatexDependencyPath $kpsewhich 'xelatex.fmt')) {
                $fmtutil = Resolve-LatexApplication (Join-Path $binDirectory 'fmtutil-sys.exe')
                if ($null -eq $fmtutil) { $fmtutil = Resolve-LatexApplication (Join-Path $binDirectory 'fmtutil-sys.bat') }
                if ($null -eq $fmtutil) {
                    $formatPackages = @('texlive-scripts')
                    if ((Split-Path -Leaf $binDirectory) -eq 'windows') { $formatPackages += 'texlive-scripts.windows' }
                    Install-LatexPackages $binDirectory $formatPackages $DownloadDirectory
                    $fmtutil = Resolve-LatexApplication (Join-Path $binDirectory 'fmtutil-sys.exe')
                    if ($null -eq $fmtutil) { $fmtutil = Resolve-LatexApplication (Join-Path $binDirectory 'fmtutil-sys.bat') }
                    if ($null -eq $fmtutil) { throw "fmtutil-sys is still unavailable beside $executable after repair." }
                }
                Write-Host 'Generating the missing XeLaTeX format...'
                $result = Invoke-LatexTool $fmtutil @('--byfmt', 'xelatex') $DownloadDirectory
                if ($result.ExitCode -ne 0) { throw 'XeLaTeX format generation failed; LaTeX dependencies remain incomplete.' }
            }
            $missing = @(Get-MissingLatexDependencies $kpsewhich)
            if ($missing.Count -gt 0) {
                throw ("LaTeX dependencies are still missing after installation: {0}" -f (($missing | ForEach-Object { $_.File }) -join ', '))
            }
        }
        Write-Host 'Verifying Chinese text, fonts, math, tables, and PDF output...'
        Test-LatexCompilation $executable $DownloadDirectory
        $env:EBOOK_OCR_XELATEX = $executable
        Write-Host 'XeLaTeX dependencies are ready.'
        return $executable
    } catch {
        $env:PATH = $previousPath
        throw
    }
}
