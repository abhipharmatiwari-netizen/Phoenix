@echo off
:: ============================================================
:: Phoenix LIVE — One-Click Build + Deploy
:: Double-click to build the Docker image, deploy the full
:: LIVE stack, wait for health, verify readyz, and capture
:: release evidence — all in one step.
:: ============================================================
setlocal

:: UTF-8 output so log snippets display correctly
chcp 65001 > nul

:: Give the terminal window a clear title
title Phoenix LIVE — Building and Deploying...

:: Maximise the window so the operator sees everything
powershell -NoProfile -Command ^
  "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class Win { [DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int n); [DllImport(\"kernel32.dll\")] public static extern IntPtr GetConsoleWindow(); }'; [Win]::ShowWindow([Win]::GetConsoleWindow(), 3);" ^
  2>nul

set "SCRIPT_DIR=%~dp0"

:: Hand off to the PowerShell script.
:: -NoExit keeps the window open after completion so the operator
:: can read the final GO / NO-GO verdict and any error messages.
powershell.exe -NoExit ^
  -ExecutionPolicy Bypass ^
  -File "%SCRIPT_DIR%start-docker-secretstore.ps1" ^
  -LaunchedFromClick

endlocal
