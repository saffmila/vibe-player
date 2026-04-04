@echo off
setlocal

echo --- TESTING FFmpeg INSTALLATION ONLY ---
echo.

REM --- Aktivace Virtual Environment (venv) ---
echo Activating virtual environment...
if not exist venv\Scripts\activate.bat (
    echo [ERROR] Virtual environment not found! Please run the full install.bat first to create it.
    pause
    exit /b
)
call venv\Scripts\activate.bat
echo Virtual environment activated.
echo.

REM --- Stažení a instalace FFmpeg ---
echo Downloading FFmpeg...
set FFMPEG_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
set FFMPEG_ARCHIVE=ffmpeg-release-essentials.zip

REM Vytvoří složku 'tools', pokud neexistuje
if not exist tools mkdir tools
cd tools

REM Stáhne archiv
curl -L %FFMPEG_URL% -o %FFMPEG_ARCHIVE%

echo Extracting FFmpeg...
REM Rozbalí archiv
tar -a -xf %FFMPEG_ARCHIVE%

echo Cleaning up and renaming...
REM Nejdříve smažeme starou složku ffmpeg, pokud existuje, abychom zabránili vnoření.
if exist ffmpeg rmdir /s /q ffmpeg

REM Přejmenujeme nově staženou verzi na jednotný název "ffmpeg".
for /d %%i in (ffmpeg-*) do (
    move "%%i" ffmpeg
)

REM Smažeme stažený .zip archiv
del %FFMPEG_ARCHIVE%

echo FFmpeg setup is complete.
cd ..
echo.
echo -----------------------------------------
echo TEST COMPLETE!
echo.
echo Check the 'tools' folder for the correct structure.
echo It should be: \tools\ffmpeg\bin\ffmpeg.exe
echo -----------------------------------------
echo.
pause