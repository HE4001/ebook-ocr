$ErrorActionPreference = 'Stop'

# Isolated function mocks: no downloads, installations, or live service changes.
$setup = @'
param($sourceDir, $scenario)
$ErrorActionPreference = 'Stop'
. (Join-Path $sourceDir 'latex-dependencies.ps1')
$previousCompiler = $env:EBOOK_OCR_XELATEX
$previousPath = $env:PATH
try {
    $env:EBOOK_OCR_XELATEX = 'C:\missing-tex\xelatex.exe'
    $script:lookups = @()
    function Resolve-LatexApplication {
        param([string]$Name)
        $script:lookups += $Name
        if ($scenario -eq 'configured' -and $Name -eq $env:EBOOK_OCR_XELATEX) { return $Name }
        if ($scenario -in @('path', 'initialize') -and $Name -eq 'xelatex') { return 'C:\found-tex\xelatex.exe' }
        if ($scenario -eq 'tinytex' -and $Name -eq (Join-Path $env:APPDATA 'TinyTeX\bin\windows\xelatex.exe')) { return $Name }
        if ($scenario -eq 'initialize' -and $Name -eq 'C:\found-tex\kpsewhich.exe') { return $Name }
        return $null
    }
    function Install-LatexTinyTeX { throw 'Unexpected installation' }
    function Get-MissingLatexDependencies { }
    function Test-LatexCompilation { }
    $warnings = @()
    $result = if ($scenario -eq 'initialize') {
        Initialize-LatexDependencies -DownloadDirectory 'C:\unused-test-cache' 3>&1 |
            ForEach-Object { if ($_ -is [System.Management.Automation.WarningRecord]) { $warnings += $_ } else { $_ } }
    } else {
        Find-LatexExecutable 3>&1 |
            ForEach-Object { if ($_ -is [System.Management.Automation.WarningRecord]) { $warnings += $_ } else { $_ } }
    }
    $expected = switch ($scenario) {
        'configured' { 'C:\missing-tex\xelatex.exe' }
        'path' { 'C:\found-tex\xelatex.exe' }
        'initialize' { 'C:\found-tex\xelatex.exe' }
        'tinytex' { Join-Path $env:APPDATA 'TinyTeX\bin\windows\xelatex.exe' }
        'missing' { $null }
    }
    if ($result -ne $expected) { throw "Unexpected discovery result for $scenario : $result" }
    if ($scenario -eq 'configured') {
        if ($warnings.Count -ne 0 -or $script:lookups.Count -ne 1) { throw 'Valid explicit configuration lost priority' }
    } elseif ($warnings.Count -ne 1) { throw 'Invalid configuration must produce one warning' }
    if ($scenario -eq 'initialize' -and $env:EBOOK_OCR_XELATEX -ne $expected) { throw 'Backend would inherit the invalid configuration' }
} finally {
    $env:EBOOK_OCR_XELATEX = $previousCompiler
    $env:PATH = $previousPath
}
'@

foreach ($scenario in @('configured', 'path', 'tinytex', 'missing', 'initialize')) {
    $runner = [powershell]::Create()
    try {
        $null = $runner.AddScript($setup).AddArgument($PSScriptRoot).AddArgument($scenario)
        $null = $runner.Invoke()
        if ($runner.HadErrors) { throw ($runner.Streams.Error | Out-String) }
        Write-Host "PASS: latex-$scenario"
    } finally { $runner.Dispose() }
}
