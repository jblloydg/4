$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Error "The project environment is missing. Follow the setup steps in README.md first."
}

$workerOptions = @{
    FilePath = $pythonPath
    ArgumentList = "manage.py process_due --loop"
    WorkingDirectory = $projectRoot
    PassThru = $true
    WindowStyle = "Hidden"
}
$worker = Start-Process @workerOptions

try {
    & $pythonPath manage.py runserver
}
finally {
    if ($worker -and -not $worker.HasExited) {
        Stop-Process -Id $worker.Id -Force -ErrorAction SilentlyContinue
    }
}
