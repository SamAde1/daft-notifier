param(
    [ValidateSet("dev", "prod")]
    [string]$Environment = "dev",

    [ValidateSet("debug", "info", "error")]
    [string]$LogType = "info",

    [Nullable[bool]]$WriteLog,

    [string]$LogDirectory
)

$projectRoot = Split-Path -Path $MyInvocation.MyCommand.Path -Parent
$logDirProvided = -not [string]::IsNullOrWhiteSpace($LogDirectory)
if (-not $logDirProvided) {
    $LogDirectory = Join-Path $projectRoot "logs"
}

$writeLogProvided = $PSBoundParameters.ContainsKey("WriteLog")
if (-not $writeLogProvided) {
    $WriteLog = $true
}

if ($logDirProvided -and -not $writeLogProvided) {
    $WriteLog = $true
}

if ($WriteLog) {
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
}

$writeLogsArg = if ($WriteLog) { "true" } else { "false" }

Push-Location $projectRoot
try {
    python -m daft_monitor `
        --environment $Environment `
        --log-level $LogType `
        --write-logs $writeLogsArg `
        --log-dir $LogDirectory
}
finally {
    Pop-Location
}
