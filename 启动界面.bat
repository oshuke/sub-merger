@echo off
chcp 936 >nul
cd /d "%~dp0"

set "PYW="
where pythonw >nul 2>nul && set "PYW=pythonw"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYW (
  echo 没找到 pythonw.exe，请安装 Python 3 或把 Python 加入 PATH。
  pause
  exit /b 1
)

start "" %PYW% "%~dp0gui.py"
