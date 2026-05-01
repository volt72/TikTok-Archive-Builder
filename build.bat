@echo off
setlocal

cd /d "%~dp0"

set PYTHON_CMD=python
where python >nul 2>nul
if errorlevel 1 set PYTHON_CMD=py

if not exist tiktok_archive_icon_clean.ico (
  echo ERROR: tiktok_archive_icon_clean.ico was not found.
  echo It is needed at build time to set the EXE icon.
  pause
  exit /b 1
)

if not exist gui_integrated_output.py (
  echo ERROR: gui_integrated_output.py was not found.
  pause
  exit /b 1
)

echo Installing build requirements...
%PYTHON_CMD% -m pip install --upgrade pyinstaller playwright greenlet pyee

echo.
echo Building a smaller package that uses installed Chrome or Edge for Playwright.
echo Chromium will not be bundled into dist\TikTokArchiveGUI.

echo.
echo Preserving existing portable app data...
if exist _dist_preserve rmdir /S /Q _dist_preserve
mkdir _dist_preserve
if exist "dist\TikTokArchiveGUI\output" move "dist\TikTokArchiveGUI\output" "_dist_preserve\output" >nul
if exist "dist\TikTokArchiveGUI\tiktok-comment-scrapper-master" move "dist\TikTokArchiveGUI\tiktok-comment-scrapper-master" "_dist_preserve\tiktok-comment-scrapper-master" >nul
if exist "dist\TikTokArchiveGUI\config.json" move "dist\TikTokArchiveGUI\config.json" "_dist_preserve\config.json" >nul
if exist "dist\TikTokArchiveGUI\yt-dlp.exe" move "dist\TikTokArchiveGUI\yt-dlp.exe" "_dist_preserve\yt-dlp.exe" >nul

echo.
echo Cleaning old build folders...
if exist build rmdir /S /Q build
if exist dist rmdir /S /Q dist

echo.
echo Building from spec file...
%PYTHON_CMD% -m PyInstaller -y TikTokArchiveGUI.spec

if not exist "dist\TikTokArchiveGUI\TikTokArchiveGUI.exe" (
  echo ERROR: EXE was not created.
  if exist _dist_preserve (
    echo Preserved data is still available in:
    echo _dist_preserve
  )
  pause
  exit /b 1
)

echo.
echo Removing bundled Playwright browser files to keep the portable folder small...
if exist "dist\TikTokArchiveGUI\_internal\playwright\driver\package\.local-browsers" rmdir /S /Q "dist\TikTokArchiveGUI\_internal\playwright\driver\package\.local-browsers"

echo.
echo Restoring preserved portable app data...
if exist "_dist_preserve\output" move "_dist_preserve\output" "dist\TikTokArchiveGUI\output" >nul
if exist "_dist_preserve\tiktok-comment-scrapper-master" move "_dist_preserve\tiktok-comment-scrapper-master" "dist\TikTokArchiveGUI\tiktok-comment-scrapper-master" >nul
if exist "_dist_preserve\config.json" move "_dist_preserve\config.json" "dist\TikTokArchiveGUI\config.json" >nul
if exist "_dist_preserve\yt-dlp.exe" move "_dist_preserve\yt-dlp.exe" "dist\TikTokArchiveGUI\yt-dlp.exe" >nul
if exist _dist_preserve rmdir /S /Q _dist_preserve
if not exist "dist\TikTokArchiveGUI\output" mkdir "dist\TikTokArchiveGUI\output"

echo.
echo DONE.
echo Portable folder:
echo dist\TikTokArchiveGUI
echo.
echo Run:
echo dist\TikTokArchiveGUI\TikTokArchiveGUI.exe
echo.
echo Notes:
echo - config.json will be created by the GUI if missing.
echo - yt-dlp.exe can be downloaded from the GUI dependency dropdown.
echo - tiktok-comment-scrapper-master can be downloaded from the GUI dependency dropdown.
echo - output folders will be created next to the EXE under dist\TikTokArchiveGUI\output
echo.
pause
