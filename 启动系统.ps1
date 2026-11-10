param([switch]$Example, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$appPath = $PSScriptRoot
$dataPath = if ($Example) { Join-Path $appPath '示例研究' } else { $appPath }
$port = if ($Example) { 8788 } else { 8787 }
$serviceUrl = "http://127.0.0.1:$port"
$runtimePath = Join-Path $dataPath '.runtime'

function Get-NotebookHealth {
    $request = [System.Net.HttpWebRequest]::Create("$serviceUrl/api/health")
    $request.Proxy = $null
    $request.Timeout = 1500
    $request.ReadWriteTimeout = 1500
    try {
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) }
        finally { $reader.Dispose(); $response.Dispose() }
    } catch { return $null }
}

try {
    $health = Get-NotebookHealth
    if ($health -and ($health.app -ne 'research-file-system' -or $health.root -ne $dataPath)) {
        throw "端口 $port 已被另一个目录使用，请先关闭冲突的程序。"
    }
    if (-not $health) {
        $candidates = @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'),
            'E:\Anaconda3\python.exe'
        )
        $foundPython = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($foundPython -and $foundPython.Source -notlike '*WindowsApps*') { $candidates += $foundPython.Source }
        $pythonPath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $pythonPath) { throw '未找到 Python 3.11 或更新版本。' }
        $entryPath = Join-Path $appPath 'nb.py'
        if ($Example) {
            & $pythonPath -X utf8 $entryPath init-example --root $dataPath --port $port
            if ($LASTEXITCODE -ne 0) { throw '示例初始化未完成，已有文件未清除。' }
        }
        New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
        $arguments = @('-X', 'utf8', '-u', ('"' + $entryPath + '"'), 'serve', '--root', ('"' + $dataPath + '"'), '--port', "$port", '--no-browser')
        Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $appPath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimePath 'server.log') -RedirectStandardError (Join-Path $runtimePath 'server.error.log') | Out-Null
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            Start-Sleep -Milliseconds 300
            $health = Get-NotebookHealth
            if ($health -and $health.app -eq 'research-file-system' -and $health.root -eq $dataPath) { break }
        }
        if (-not $health -or $health.app -ne 'research-file-system' -or $health.root -ne $dataPath) {
            throw "系统未能启动。请查看 $runtimePath\server.error.log。"
        }
    }
    if (-not $NoBrowser) { Start-Process $serviceUrl }
    Write-Host "科研文件管理系统：$serviceUrl"
    Write-Host "资料目录：$dataPath"
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
