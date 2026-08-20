$ErrorActionPreference = "SilentlyContinue"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# Selenium Manager must choose a driver compatible with the installed Chrome.
# Remove PATH entries that would make Selenium reuse a stale chromedriver.
$env:Path = (@($env:Path -split ";") | Where-Object {
    if ([string]::IsNullOrWhiteSpace($_)) {
        return $false
    }
    try {
        $pathEntry = [System.IO.Path]::GetFullPath($_.Trim())
        -not (Test-Path -LiteralPath (Join-Path $pathEntry "chromedriver.exe") -PathType Leaf)
    }
    catch {
        return $true
    }
}) -join ";"
$pidFile = Join-Path $projectDir "majui.pid"
$profileDir = [System.IO.Path]::GetFullPath("C:\MajesticSeleniumProfile")
$expectedProfileDir = [System.IO.Path]::GetFullPath("C:\MajesticSeleniumProfile")
$appPath = [System.IO.Path]::GetFullPath((Join-Path $projectDir "app.py"))
if ($profileDir -ne $expectedProfileDir) {
    throw "Unexpected Chrome profile path"
}

$targetPids = New-Object System.Collections.Generic.HashSet[int]
if (Test-Path -LiteralPath $pidFile) {
    $savedPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedPid) -and $savedPid -gt 4) {
        $savedProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid"
        if ($savedProcess -and $savedProcess.Name -match '^(python|pythonw|py|pyw)(\.exe)?$' -and $savedProcess.CommandLine -like "*$appPath*") {
            [void]$targetPids.Add($savedPid)
        }
    }
}

Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 5000 -State Listen | ForEach-Object {
    $owner = Get-Process -Id $_.OwningProcess
    if ($owner.ProcessName -in @("python", "pythonw", "py", "pyw")) {
        [void]$targetPids.Add([int]$_.OwningProcess)
    }
}

$snapshot = Get-CimInstance Win32_Process
$changed = $true
while ($changed) {
    $changed = $false
    foreach ($process in $snapshot) {
        if ($targetPids.Contains([int]$process.ParentProcessId) -and -not $targetPids.Contains([int]$process.ProcessId)) {
            [void]$targetPids.Add([int]$process.ProcessId)
            $changed = $true
        }
    }
}

foreach ($process in $snapshot) {
    if ($process.Name -eq "chrome.exe" -and $process.CommandLine -like "*$profileDir*") {
        [void]$targetPids.Add([int]$process.ProcessId)
    }
}

foreach ($processId in $targetPids) {
    if ($processId -gt 4) {
        Stop-Process -Id $processId -Force
    }
}

$deadline = (Get-Date).AddSeconds(6)
while ((Get-Date) -lt $deadline -and ($targetPids | Where-Object { Get-Process -Id $_ })) {
    Start-Sleep -Milliseconds 150
}

foreach ($lockName in @("SingletonLock", "SingletonCookie", "SingletonSocket")) {
    $lockPath = Join-Path $profileDir $lockName
    if ([System.IO.Path]::GetFullPath($lockPath).StartsWith($profileDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $lockPath -Force
    }
}
Remove-Item -LiteralPath $pidFile -Force

Start-Process -FilePath "pyw.exe" -ArgumentList @("-3", $appPath) -WorkingDirectory $projectDir -WindowStyle Hidden
