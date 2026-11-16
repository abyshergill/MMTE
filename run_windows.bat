@echo off
rem Build (first run only) and start Modern Multi Terminal (Rust edition).
cd /d "%~dp0"
where cargo >nul 2>nul
if errorlevel 1 (
  if exist "%USERPROFILE%\.cargo\bin\cargo.exe" (
    set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
  ) else (
    echo Rust was not found. Install it from https://rustup.rs and run this file again.
    pause
    exit /b 1
  )
)
cargo run --release -- %*
if errorlevel 1 pause
