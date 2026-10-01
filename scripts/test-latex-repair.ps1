$ErrorActionPreference = 'Stop'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('ocr-latex-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$setup = @'
param($sourceDir, $fixture, $scenario)
$ErrorActionPreference = 'Stop'
. (Join-Path $sourceDir 'latex-dependencies.ps1')
$bin = Join-Path $fixture 'windows'
New-Item -ItemType Directory -Path $bin -Force | Out-Null
Set-Content -LiteralPath (Join-Path $bin 'tlmgr.bat') -Value 'fixture'
$script:calls = @()
$script:installed = $false
function Invoke-LatexTool {
    param($Tool, $Arguments, $DownloadDirectory, $WorkingDirectory, $TimeoutSeconds, [switch]$Quiet)
    $script:calls += ($Arguments -join ' ')
    if ($Arguments[0] -eq 'install') {
        if ($scenario -eq 'update' -and $script:calls.Count -eq 1) {
            return [pscustomobject]@{ ExitCode = 1; Output = 'tlmgr itself needs to be updated' }
        }
        if ($scenario -eq 'install-failed') { return [pscustomobject]@{ ExitCode = 1; Output = 'network failed' } }
        $script:installed = $true
    }
    if ($Arguments[0] -eq 'search') {
        return [pscustomobject]@{ ExitCode = 0; Output = "transitive-package:`n    texmf-dist/tex/latex/transitive.sty" }
    }
    if ($Arguments[0] -eq '-no-shell-escape') {
        if ($scenario -eq 'compile-failed') { return [pscustomobject]@{ ExitCode = 1; Output = '! Undefined control sequence.' } }
        if (-not $script:installed -or $scenario -eq 'still-missing') {
            return [pscustomobject]@{ ExitCode = 1; Output = "! LaTeX Error: File ``transitive.sty' not found." }
        }
        Set-Content -LiteralPath (Join-Path $WorkingDirectory 'probe.pdf') -Value '%PDF-1.5 fixture'
    }
    return [pscustomobject]@{ ExitCode = 0; Output = '' }
}
switch ($scenario) {
    'update' {
        Install-LatexPackages $bin @('ctex') $fixture
        if (($script:calls -join ',') -ne 'install --reinstall ctex,update --self,install --reinstall ctex') { throw 'Incorrect update/retry sequence' }
    }
    'install-failed' {
        try { Install-LatexPackages $bin @('ctex') $fixture; throw 'Expected failure was not raised' }
        catch { if ($_.Exception.Message -notlike 'TeX Live package installation failed*') { throw } }
    }
    'binaries' {
        function Get-LatexDependencyPath { return 'present' }
        $missing = @(Get-MissingLatexDependencies (Join-Path $bin 'kpsewhich.exe'))
        if ($missing.Count -ne 3 -or $missing.File -notcontains 'xdvipdfmx.exe' -or $missing[0].Packages -notcontains 'xetex.windows') { throw 'Missing binaries were hidden by an existing format' }
    }
    'compiler' {
        $previous = $env:EBOOK_OCR_XELATEX
        try {
            $env:EBOOK_OCR_XELATEX = Join-Path $bin 'xelatex.exe'
            function Resolve-LatexApplication { param($Name); if ($script:installed) { return $Name }; return $null }
            $found = Repair-LatexCompiler $fixture
            if ($found -ne $env:EBOOK_OCR_XELATEX -or $script:calls[-1] -ne 'postaction install script xetex') { throw 'Compiler repair did not restore the configured installation' }
        } finally { $env:EBOOK_OCR_XELATEX = $previous }
    }
    default {
        try {
            Test-LatexCompilation (Join-Path $bin 'xelatex.exe') $fixture
            if ($scenario -ne 'transitive') { throw 'Expected compile failure was not raised' }
            if ($script:calls.Count -ne 4 -or $script:calls[2] -ne 'install --reinstall transitive-package') { throw 'Transitive dependency was not installed and rechecked' }
        } catch {
            if ($scenario -eq 'transitive') { throw }
            if ($scenario -eq 'still-missing' -and $_.Exception.Message -notlike '*still missing after repair*') { throw }
            if ($scenario -eq 'compile-failed' -and $_.Exception.Message -notlike 'LaTeX compilation verification failed*') { throw }
        }
    }
}
'@
try {
    foreach ($scenario in @('update', 'install-failed', 'binaries', 'compiler', 'transitive', 'still-missing', 'compile-failed')) {
        $runner = [powershell]::Create()
        try {
            $null = $runner.AddScript($setup).AddArgument($PSScriptRoot).AddArgument((Join-Path $testRoot $scenario)).AddArgument($scenario)
            $null = $runner.Invoke()
            if ($runner.Streams.Error.Count -gt 0) { throw ($runner.Streams.Error | Out-String) }
            Write-Host "PASS: latex-repair-$scenario"
        } finally { $runner.Dispose() }
    }
} finally {
    $resolved = [IO.Path]::GetFullPath($testRoot)
    $prefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\ocr-latex-test-'
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid test cleanup path' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
