@echo off
REM delete-files.bat —— Windows 便捷启动器，实际逻辑在 delete-files.ps1
REM 用法：把「删除名单 CSV」或「报告目录」拖到本文件上，或在命令行传参：
REM   delete-files.bat -Yes -Map "/run/media/user/DISK_A=E:\" D:\reports\r1
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0delete-files.ps1" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
