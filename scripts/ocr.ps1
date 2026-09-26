param(
    [Parameter(Position = 0)]
    [ValidateSet("Menu", "Start", "Stop", "Restart", "Status")]
    [string]$Action = "Menu",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$script:Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "launcher-processes.ps1")

function Show-OcrStatus {
    Write-Host ""
    foreach ($service in @(
        @{ Role = "backend"; Name = "后端"; Port = 8000; Url = "http://127.0.0.1:8000/api/health" },
        @{ Role = "frontend"; Name = "前端"; Port = 5173; Url = "http://127.0.0.1:5173/" }
    )) {
        $owners = @(Get-ListeningPids $service.Port)
        if ($owners.Count -eq 0) {
            Write-Host ("  {0}：未运行（端口 {1}）" -f $service.Name, $service.Port) -ForegroundColor DarkGray
            continue
        }
        $entry = Get-ProjectListenerEntry $service.Role $script:Root
        if ($null -eq $entry) {
            Write-Host ("  {0}：端口 {1} 被占用，无法确认为本项目（PID {2}）" -f $service.Name, $service.Port, ($owners -join ", ")) -ForegroundColor Yellow
            continue
        }
        $healthy = $false
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $service.Url -TimeoutSec 2
            $healthy = ($response.StatusCode -eq 200)
        } catch { }
        if ($healthy) {
            Write-Host ("  {0}：运行正常（PID {1}）" -f $service.Name, $entry.pid) -ForegroundColor Green
        } else {
            Write-Host ("  {0}：进程存在，但暂未就绪（PID {1}）" -f $service.Name, $entry.pid) -ForegroundColor Yellow
        }
    }
    Write-Host ""
}

function Invoke-LauncherScript {
    param([string]$Name, [switch]$WithoutBrowser)

    # Keep worker exit codes isolated from the interactive menu process.
    $worker = New-Object System.Diagnostics.Process
    $worker.StartInfo.FileName = Join-Path $PSHOME "powershell.exe"
    $worker.StartInfo.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot $Name) + '"'
    if ($WithoutBrowser) { $worker.StartInfo.Arguments += " -NoBrowser" }
    # Inherit the console directly: piping output can keep the menu blocked
    # after the worker exits when newly launched services inherit pipe handles.
    $worker.StartInfo.UseShellExecute = $false
    try {
        $null = $worker.Start()
        $worker.WaitForExit()
        return $worker.ExitCode
    } finally { $worker.Dispose() }
}

function Wait-OcrPortsFree {
    $deadline = (Get-Date).AddSeconds(5)
    do {
        $owners = @((Get-ListeningPids 8000)) + @((Get-ListeningPids 5173))
        if ($owners.Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    Write-Host ("端口仍被占用（PID {0}），请检查对应程序后重试。" -f (($owners | Select-Object -Unique) -join ", ")) -ForegroundColor Yellow
    return $false
}

function Invoke-OcrAction {
    param([string]$SelectedAction)

    if ($SelectedAction -eq "Status") {
        Show-OcrStatus
        return 0
    }
    if ($SelectedAction -in @("Stop", "Restart")) {
        $code = Invoke-LauncherScript "stop.ps1"
        if ($code -ne 0) { return $code }
        if (-not (Wait-OcrPortsFree)) { return 1 }
        Write-Host "服务已停止。" -ForegroundColor Green
    }
    if ($SelectedAction -in @("Start", "Restart")) {
        return (Invoke-LauncherScript "start.ps1" -WithoutBrowser:$NoBrowser)
    }
    return 0
}

try {
    if ($Action -ne "Menu") {
        exit (Invoke-OcrAction $Action)
    }
    while ($true) {
        Write-Host ""
        Write-Host "  纸页重排 · 服务管理" -ForegroundColor Cyan
        Show-OcrStatus
        Write-Host "  [1] 启动 / 打开页面（回车默认）"
        Write-Host "  [2] 停止服务"
        Write-Host "  [3] 重启服务"
        Write-Host "  [4] 刷新状态"
        Write-Host "  [0] 退出管理窗口"
        Write-Host ""
        Write-Host "  关闭此窗口不会停止服务；停止或重启会中断正在识别的任务。" -ForegroundColor DarkGray
        $choice = Read-Host "请选择"
        if ($null -eq $choice) { exit 0 }
        $choice = $choice.Trim()
        if ($choice -eq "0") { exit 0 }
        $selected = switch ($choice) {
            "" { "Start" }
            "1" { "Start" }
            "2" { "Stop" }
            "3" { "Restart" }
            "4" { "Status" }
            default { $null }
        }
        if ($null -eq $selected) {
            Write-Host "请输入 0 到 4。" -ForegroundColor Yellow
            continue
        }
        # The next menu refresh already displays status.
        if ($selected -eq "Status") { continue }
        $code = Invoke-OcrAction $selected
        if ($code -ne 0) {
            Write-Host "操作未完成，请查看上方提示；可选择重试或退出。" -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host ("ERROR: {0}" -f $_.Exception.Message) -ForegroundColor Red
    if ($Action -eq "Menu") { $null = Read-Host "按回车关闭窗口" }
    exit 1
}
