@echo off
rem One-step launcher for Windows. Double-click it, or run from a terminal:
rem
rem   run_windows.bat              set everything up if needed, then start the app
rem   run_windows.bat --gguf       also install the optional local-GGUF model runtime
rem   run_windows.bat --reinstall  delete .venv and build it again from scratch
rem   run_windows.bat --setup-only prepare .venv but do not start the app
rem
rem It finds Python 3.10+ (and offers to install it with winget if missing), creates .venv
rem next to this file, installs requirements.txt (again only when it changes) and runs main.py.

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Modern Multi Terminal Emulator

set "VENV=%CD%\.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"
set "STAMP=%VENV%\.mmte-requirements"
set "WITH_GGUF=0"
set "REINSTALL=0"
set "SETUP_ONLY=0"
set "APP_ARGS="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--gguf" (set "WITH_GGUF=1") else if /i "%~1"=="--reinstall" (set "REINSTALL=1") else if /i "%~1"=="--setup-only" (set "SETUP_ONLY=1") else (set "APP_ARGS=!APP_ARGS! %1")
shift
goto parse
:parsed

rem ---- 1. virtual environment ---------------------------------------------------------------
if "%REINSTALL%"=="1" if exist "%VENV%" (
    echo [MMTE] Removing the old virtual environment ...
    rmdir /s /q "%VENV%"
)

if exist "%VPY%" (
    "%VPY%" -c "import sys" >nul 2>nul || (
        echo [MMTE] The existing .venv is broken; rebuilding it.
        rmdir /s /q "%VENV%"
    )
)

if not exist "%VPY%" (
    call :find_python
    if not defined PY (
        echo [MMTE] Python 3.10 or newer was not found.
        where winget >nul 2>nul
        if not errorlevel 1 (
            choice /c YN /m "[MMTE] Install Python 3.12 now with winget"
            if errorlevel 2 goto no_python
            winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
            rem winget does not refresh PATH for this window; look in the default install folders.
            call :find_python
        )
    )
    if not defined PY goto no_python
    echo [MMTE] Creating virtual environment ...
    !PY! -m venv "%VENV%"
    if errorlevel 1 (
        echo [MMTE] Could not create the virtual environment.
        goto fail
    )
)

rem ---- 2. dependencies (only when requirements change) ----------------------------------------
set "REQS=requirements.txt"
if exist "%STAMP%" (
    findstr /c:"-gguf1" "%STAMP%" >nul 2>nul && set "WITH_GGUF=1"
)
if "%WITH_GGUF%"=="1" set "REQS=requirements.txt requirements-gguf.txt"

set "WANT="
for %%R in (%REQS%) do (
    set "H="
    for /f "skip=1 delims=" %%H in ('certutil -hashfile "%%R" SHA256 2^>nul') do if not defined H set "H=%%H"
    set "WANT=!WANT!!H: =!"
)
set "WANT=!WANT!-gguf%WITH_GGUF%"
set "HAVE="
if exist "%STAMP%" set /p HAVE=<"%STAMP%"

if not "!WANT!"=="!HAVE!" (
    echo [MMTE] Installing dependencies ^(first run or requirements changed^) ...
    "%VPY%" -m pip install --disable-pip-version-check --upgrade pip >nul
    for %%R in (%REQS%) do (
        echo [MMTE]   pip install -r %%R
        "%VPY%" -m pip install --disable-pip-version-check -r "%%R"
        if errorlevel 1 (
            echo [MMTE] Installing %%R failed. Check your internet connection and run this file again.
            goto fail
        )
    )
    >"%STAMP%" echo !WANT!
    echo [MMTE] Dependencies are ready.
)

if "%SETUP_ONLY%"=="1" (
    echo [MMTE] Setup complete. Start the app with run_windows.bat
    goto done
)

rem ---- 3. launch ----------------------------------------------------------------------------
echo [MMTE] Starting Modern Multi Terminal Emulator ...
"%VPY%" "%CD%\main.py" %APP_ARGS%
if errorlevel 1 goto fail
goto done

rem ---- helpers ------------------------------------------------------------------------------
:find_python
set "PY="
set "CHECK=import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
where py >nul 2>nul && py -3 -c "%CHECK%" >nul 2>nul && set "PY=py -3" && goto :eof
where python >nul 2>nul && python -c "%CHECK%" >nul 2>nul && set "PY=python" && goto :eof
for %%V in (313 312 311 310 314) do (
    for %%D in ("%LOCALAPPDATA%\Programs\Python\Python%%V" "%ProgramFiles%\Python%%V") do (
        if exist "%%~D\python.exe" (
            "%%~D\python.exe" -c "%CHECK%" >nul 2>nul && set "PY="%%~D\python.exe"" && goto :eof
        )
    )
)
goto :eof

:no_python
echo [MMTE] Install Python 3.10+ from https://www.python.org/downloads/windows/
echo [MMTE] and tick "Add python.exe to PATH" in the installer, then run this file again.
goto fail

:fail
echo.
pause
endlocal
exit /b 1

:done
endlocal
exit /b 0
