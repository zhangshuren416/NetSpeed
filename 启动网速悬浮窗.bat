@echo off
rem ============================================
rem  启动网速悬浮窗（无控制台窗口）
rem  优先使用 PATH 中的 pythonw.exe，其次 pyw.exe
rem ============================================
setlocal
set "SCRIPT=%~dp0netspeed.pyw"

where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%SCRIPT%"
    goto :eof
)

where pyw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pyw.exe -3 "%SCRIPT%"
    goto :eof
)

echo 未找到 pythonw.exe / pyw.exe。
echo 请安装 Python 3（勾选 Add to PATH），或直接使用 dist\NetSpeed.exe。
pause