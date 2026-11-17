param(
    [int]$Port = 8790
)

Get-CimInstance Win32_Process | ForEach-Object {
    $line = $_.CommandLine
    if (-not $line) {
        return
    }
    if ($line.Contains("studio.py") -or $line.Contains("StudioServer")) {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}
