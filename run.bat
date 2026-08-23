@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" goto usage
if /i "%~1"=="verify" goto verify
if /i "%~1"=="selftest" goto selftest
if /i "%~1"=="live" goto live
if /i "%~1"=="soak" goto soak
if /i "%~1"=="analyze" goto analyze
if /i "%~1"=="dashboard" goto dashboard
goto custom

:usage
echo ======================================================================
echo   Multi-Agent Autonomous Chaos Engineering Framework
echo ======================================================================
echo Usage:
echo   run.bat dashboard               - Launch interactive Web Dashboard UI
echo   run.bat verify                  - Pre-flight checks (K8s, Prometheus, Chaos Mesh)
echo   run.bat selftest                - Run 20 scenarios simulated
echo   run.bat live                    - Run live scenarios against cluster
echo   run.bat soak                    - Run Sentinel soak test
echo   run.bat analyze                 - Generate thesis Tables IV/V and statistics
echo.
echo Running self-test by default...
.venv\Scripts\python.exe -u main.py selftest
exit /b %errorlevel%

:verify
.venv\Scripts\python.exe -u main.py verify
exit /b %errorlevel%

:selftest
.venv\Scripts\python.exe -u main.py selftest
exit /b %errorlevel%

:live
.venv\Scripts\python.exe -u main.py run --mode live --repetitions 1
exit /b %errorlevel%

:soak
.venv\Scripts\python.exe -u main.py soak-test --duration 24h
exit /b %errorlevel%

:analyze
.venv\Scripts\python.exe -u main.py analyze
exit /b %errorlevel%

:dashboard
.venv\Scripts\python.exe -u main.py dashboard
exit /b %errorlevel%

:custom
.venv\Scripts\python.exe -u main.py %*
exit /b %errorlevel%
