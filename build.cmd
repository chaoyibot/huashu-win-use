@echo off
REM huashu-win-use 环境安装（对应原版的 build.sh）
REM Python 路径按本机情况调整，若已装到别处改下面两行
set PY=C:\Program Files\Python311\python.exe
set VENV=D:\小宝输出\huashu-win-use\.venv
if not exist "%VENV%Scripts\python.exe" %PY% -m venv "%VENV%"
call "%VENV%\Scripts\activate.bat"
pip install --quiet pywin32 Pillow pywinauto comtypes
