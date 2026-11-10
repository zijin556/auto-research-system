param([switch]$Example)
$ErrorActionPreference = 'Stop'
$dataPath = if ($Example) { Join-Path $PSScriptRoot '示例研究' } else { $PSScriptRoot }
$port = if ($Example) { 8788 } else { 8787 }
$client = New-Object System.Net.WebClient
$client.Proxy = $null
$client.Encoding = [System.Text.Encoding]::UTF8
try {
    $health = $client.DownloadString("http://127.0.0.1:$port/api/health") | ConvertFrom-Json
    if ($health.app -ne 'research-file-system' -or $health.root -ne $dataPath) { throw '这个端口不属于本研究目录，未执行停止。' }
    $client.Headers['Content-Type'] = 'application/json'
    $client.Headers['X-Research-Notebook'] = '1'
    $null = $client.UploadString("http://127.0.0.1:$port/api/stop", 'POST', '{}')
    Write-Host '系统已停止，所有已保存资料仍在本地文件夹。'
} catch {
    Write-Host ('系统未运行或未能停止：' + $_.Exception.Message)
    exit 1
} finally { $client.Dispose() }
