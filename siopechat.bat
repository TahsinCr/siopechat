@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%python\Scripts\python.exe" goto use_local
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
echo SiopeChat requires Python 3.10 or newer. 1>&2
exit /b 1

:use_py
py -3 "%ROOT%siopechat.py" %*
exit /b %ERRORLEVEL%

:use_local
"%ROOT%python\Scripts\python.exe" "%ROOT%siopechat.py" %*
exit /b %ERRORLEVEL%

:use_python
python "%ROOT%siopechat.py" %*
exit /b %ERRORLEVEL%
