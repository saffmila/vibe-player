@echo off
REM Execute the installation script with execution policy bypass to allow PowerShell commands
REM This ensures Tee-Object works even if the system has restricted script execution

powershell -ExecutionPolicy Bypass -Command ".\install.bat | Tee-Object -FilePath 'install_log.txt'"

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Installation finished with issues. Check install_log.txt for details.
)

pause