@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%run-local.ps1"

where pwsh >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
    exit /b %ERRORLEVEL%
)

where powershell >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
    exit /b %ERRORLEVEL%
)

set "ENVIRONMENT=%~1"
if "%ENVIRONMENT%"=="" set "ENVIRONMENT=dev"

set "LOG_LEVEL=%~2"
if "%LOG_LEVEL%"=="" set "LOG_LEVEL=info"

set "WRITE_LOGS=%~3"
if "%WRITE_LOGS%"=="" set "WRITE_LOGS=true"

set "LOG_DIR=%~4"
if "%LOG_DIR%"=="" set "LOG_DIR=%SCRIPT_DIR%logs"
if not "%~4"=="" if "%~3"=="" set "WRITE_LOGS=true"

if /I "%WRITE_LOGS%"=="true" (
    if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
)

pushd "%SCRIPT_DIR%"
python -m daft_monitor --environment "%ENVIRONMENT%" --log-level "%LOG_LEVEL%" --write-logs "%WRITE_LOGS%" --log-dir "%LOG_DIR%"
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
