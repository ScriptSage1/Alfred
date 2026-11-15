@echo off
rem Removes Alfred from this PC: stops it, removes its start-at-sign-in and Start menu shortcuts and its Windows
rem registration, and (if you agree) its data, saved API keys and this folder. It never touches the files Alfred searched.
setlocal
title Uninstall Alfred
set "HERE=%~dp0"
set "DATA=%LOCALAPPDATA%\Alfred"

echo.
echo  Uninstall Alfred
echo  ----------------
echo  This stops Alfred and removes its shortcuts from this PC.
echo  The files and folders you searched with Alfred are never touched.
echo.
choice /c YN /n /m " Continue? [Y/N] "
if errorlevel 2 goto :cancelled

echo.
echo  Stopping Alfred...
taskkill /im Alfred.exe /f >nul 2>&1
rem Only the search engine copies Alfred started from its own data folder, never another program's.
powershell -NoProfile -Command "Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -like ($env:LOCALAPPDATA + '\Alfred\*') } | Stop-Process -Force" >nul 2>&1
timeout /t 2 /nobreak >nul

echo  Removing shortcuts...
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Alfred.lnk" >nul 2>&1
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Alfred.lnk" >nul 2>&1
reg delete "HKCU\Software\Classes\AppUserModelId\Alfred.Desktop" /f >nul 2>&1

echo.
echo  Alfred keeps your settings, the search index, downloaded models and saved chats in:
echo    %DATA%
choice /c YN /n /m " Delete them too? [Y/N] "
if errorlevel 2 goto :keys
if exist "%DATA%\settings.json" (
  rmdir /s /q "%DATA%"
  echo  Deleted.
) else (
  if exist "%DATA%" (rmdir /s /q "%DATA%" & echo  Deleted.) else (echo  Nothing was there.)
)

:keys
echo.
choice /c YN /n /m " Delete API keys Alfred saved in Windows Credential Manager? [Y/N] "
if errorlevel 2 goto :folder
powershell -NoProfile -Command "cmdkey /list | ForEach-Object { if ($_ -match 'target=(.+)$') { $t = $Matches[1].Trim(); if ($t -eq 'Alfred' -or $t -like '*@Alfred') { cmdkey /delete:$t | Out-Null } } }" >nul 2>&1
echo  Done.

:folder
echo.
echo  If you added Alfred to an AI tool, remove it there too, for example:
echo    claude mcp remove alfred
echo.
if not exist "%HERE%Alfred.exe" goto :finished
echo  The program itself is this folder:
echo    %HERE%
choice /c YN /n /m " Delete this folder now? [Y/N] "
if errorlevel 2 goto :finished
echo  Alfred is removed. This window closes in a moment.
cd /d "%TEMP%"
start "" /b cmd /c "ping -n 3 127.0.0.1 >nul & rmdir /s /q "%HERE%.""
exit /b 0

:finished
echo.
echo  Finished. To remove the program itself, delete the folder that holds Alfred.exe.
echo.
pause
exit /b 0

:cancelled
echo.
echo  Nothing was changed.
echo.
pause
exit /b 0
