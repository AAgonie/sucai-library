$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$mode = [string]$env:PHOTO_WEB_RESTART_MODE
$supervised = [string]$env:PHOTO_WEB_SUPERVISED

if ([string]::IsNullOrWhiteSpace($env:PHOTO_WEB_SUPERVISOR_ID)) {
    $env:PHOTO_WEB_SUPERVISOR_ID = [Guid]::NewGuid().ToString("N")
}
$supervisorId = [string]$env:PHOTO_WEB_SUPERVISOR_ID

if ($mode -notin @("local", "nas")) {
    Write-Host "PHOTO_WEB_RESTART_MODE must be local or nas."
    exit 2
}

if ($supervised -ne "1") {
    Write-Host "PHOTO_WEB_SUPERVISED must be 1."
    exit 2
}

$bindAddress = if ($mode -eq "local") { "127.0.0.1" } else { "0.0.0.0" }
$logDirectory = Join-Path $projectRoot "logs"
$logFile = Join-Path $logDirectory "supervisor.log"
$retryDelays = @(5, 10, 20, 40, 60)
$consecutiveFailures = 0
$healthFailureCount = 0
$watchdogTriggered = 0
$childProcessId = 0
$healthTimeoutSeconds = 20

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Set-Location -LiteralPath $projectRoot

function Write-SupervisorConsole {
    param([string]$Message)
    # Console output must never terminate the watchdog. Windows PowerShell can
    # throw IndexOutOfRangeException while third-party progress bars redraw.
    try {
        Write-Host $Message -ErrorAction Stop
    }
    catch {
        # The durable supervisor log is written separately below.
    }
}

function Write-SupervisorLog {
    param(
        [int]$ExitCode,
        [int]$RuntimeSeconds,
        [string]$Retry,
        [int]$RetryAttempt,
        [int]$RetryDelaySeconds,
        [int]$FailuresReset
    )

    $timestamp = [DateTimeOffset]::Now.ToString("yyyy-MM-ddTHH:mm:ss.fffzzz")
    $line = "timestamp=$timestamp mode=$mode supervisor_id=$supervisorId child_pid=$childProcessId exit_code=$ExitCode retry=$Retry retry_attempt=$RetryAttempt retry_delay_seconds=$RetryDelaySeconds runtime_seconds=$RuntimeSeconds consecutive_failures=$consecutiveFailures failures_reset=$FailuresReset watchdog_triggered=$watchdogTriggered health_failures=$healthFailureCount"
    Add-Content -LiteralPath $logFile -Value $line -Encoding ASCII
    Write-SupervisorConsole "[supervisor] $line"
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Write-SupervisorLog -ExitCode 9009 -RuntimeSeconds 0 -Retry "not_started" -RetryAttempt 0 -RetryDelaySeconds 0 -FailuresReset 0
    Write-SupervisorConsole "Python was not found. Install Python 3.12.10 and add it to PATH."
    exit 1
}
$pythonPath = $pythonCommand.Source

while ($true) {
    $startedAt = [DateTimeOffset]::Now
    $healthFailureCount = 0
    $watchdogTriggered = 0
    $childProcessId = 0
    Write-SupervisorConsole "[supervisor] Starting mode=$mode supervisor_id=$supervisorId host=$bindAddress port=8000 workers=1"

    $childProcess = $null
    $exitCode = 1
    try {
        $childProcess = Start-Process -FilePath $pythonPath -ArgumentList @("-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", $bindAddress, "--port", "8000", "--workers", "1") -PassThru -NoNewWindow
        $childProcessId = $childProcess.Id
        $nextHealthCheckAt = $startedAt.AddSeconds(30)

        while (-not $childProcess.HasExited) {
            Start-Sleep -Milliseconds 500
            $childProcess.Refresh()
            if ($childProcess.HasExited) {
                break
            }
            if ([DateTimeOffset]::Now -lt $nextHealthCheckAt) {
                continue
            }

            $nextHealthCheckAt = [DateTimeOffset]::Now.AddSeconds(10)
            try {
                $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec $healthTimeoutSeconds
                if ([int]$health.StatusCode -ne 200) {
                    throw "Health endpoint returned HTTP $($health.StatusCode)."
                }
                $healthFailureCount = 0
            }
            catch {
                $healthFailureCount += 1
                Write-SupervisorConsole "[supervisor] Health check failed $healthFailureCount/3: $($_.Exception.Message)"
                if ($healthFailureCount -ge 3) {
                    $watchdogTriggered = 1
                    Write-SupervisorConsole "[supervisor] Health endpoint failed three consecutive checks; stopping child process $childProcessId."
                    Stop-Process -Id $childProcessId -Force -ErrorAction SilentlyContinue
                    break
                }
            }
        }

        $childProcess.WaitForExit()
        $exitCode = if ($watchdogTriggered) { 76 } else { $childProcess.ExitCode }
    }
    catch {
        Write-SupervisorConsole "[supervisor] Failed to start or monitor child process: $($_.Exception.Message)"
        $exitCode = 1
    }
    finally {
        if ($null -ne $childProcess) {
            try {
                if (-not $childProcess.HasExited) {
                    Stop-Process -Id $childProcess.Id -Force -ErrorAction SilentlyContinue
                    $childProcess.WaitForExit()
                }
            }
            catch {
                Write-SupervisorConsole "[supervisor] Child cleanup warning: $($_.Exception.Message)"
            }
            $childProcess.Dispose()
        }
    }

    $runtimeSeconds = [int][Math]::Floor(([DateTimeOffset]::Now - $startedAt).TotalSeconds)
    $failuresReset = 0
    if ($runtimeSeconds -ge 600) {
        $consecutiveFailures = 0
        $failuresReset = 1
    }

    if ($exitCode -eq 0) {
        Write-SupervisorLog -ExitCode $exitCode -RuntimeSeconds $runtimeSeconds -Retry "none" -RetryAttempt 0 -RetryDelaySeconds 0 -FailuresReset $failuresReset
        exit 0
    }

    if ($exitCode -eq 75) {
        Write-SupervisorLog -ExitCode $exitCode -RuntimeSeconds $runtimeSeconds -Retry "web_requested" -RetryAttempt 0 -RetryDelaySeconds 2 -FailuresReset $failuresReset
        Start-Sleep -Seconds 2
        continue
    }

    $consecutiveFailures += 1
    if ($consecutiveFailures -le $retryDelays.Count) {
        $retryDelay = $retryDelays[$consecutiveFailures - 1]
        Write-SupervisorLog -ExitCode $exitCode -RuntimeSeconds $runtimeSeconds -Retry "scheduled" -RetryAttempt $consecutiveFailures -RetryDelaySeconds $retryDelay -FailuresReset $failuresReset
        Start-Sleep -Seconds $retryDelay
        continue
    }

    Write-SupervisorLog -ExitCode $exitCode -RuntimeSeconds $runtimeSeconds -Retry "limit_reached" -RetryAttempt $retryDelays.Count -RetryDelaySeconds 0 -FailuresReset $failuresReset
    exit $exitCode
}
