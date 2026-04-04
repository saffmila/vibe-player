@echo off
setlocal

echo Checking for Python 3.11 using 'py' launcher...

REM --- OPRAVA ZDE ---
REM Místo 'python --version' si přímo vyžádáme 3.11
py -3.11 --version > tmp_version.txt 2>nul

REM Zkontrolujeme, jestli příkaz 'py -3.11' vůbec fungoval
if errorlevel 1 (
    echo [ERROR] Python 3.11 not found via 'py -3.11'.
    echo Please make sure Python 3.11 is installed correctly from python.org.
    if exist tmp_version.txt del tmp_version.txt
    pause
    exit /b
)

REM Zkontrolujeme, jestli je to opravdu verze 3.11
findstr /R "Python 3\.11\." tmp_version.txt >nul
if errorlevel 1 (
    echo [ERROR] 'py -3.11' command did not return Python 3.11.x.
    echo Output was:
    type tmp_version.txt
    del tmp_version.txt
    pause
    exit /b
)
del tmp_version.txt
echo Found Python 3.11.

echo Creating virtual environment...
REM --- OPRAVA ZDE ---
REM Vytvoříme venv pomocí specifické verze 3.11
py -3.11 -m venv venv
if not exist venv\Scripts\activate.bat (
    echo [ERROR] Virtual environment not created!
    pause
    exit /b
)

echo.
echo Checking for VLC Media Player...
REM Verify if VLC is installed in the default 64-bit directory
if not exist "C:\Program Files\VideoLAN\VLC\libvlc.dll" (
        echo [WARNING] VLC Media Player ^(64-bit^) not found!
        echo Please install it from https://www.videolan.org/vlc/ 
        echo WITHOUT VLC, THE PLAYER WILL NOT WORK.
    ) else (
        echo [OK] VLC Media Player found.
    )


echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Upgrading build tools and installing requirements...
REM OPRAVA 1: Aktualizujeme vsechny nastroje
REM Teď už jsme ve venv, takže 'python' příkaz bude správně 3.11
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
pip install pyinstaller

echo.
echo Downloading FFmpeg...
REM OPRAVA 2: Změnili jsme URL na .zip verzi
set FFMPEG_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
set FFMPEG_ARCHIVE=ffmpeg-release-essentials.zip

if not exist tools mkdir tools
cd tools
curl -L %FFMPEG_URL% -o %FFMPEG_ARCHIVE%

echo Extracting FFmpeg...
REM Pouzijeme vestaveny 'tar' s parametrem pro automatickou detekci komprese
tar -xf %FFMPEG_ARCHIVE%

REM --- OPRAVA ZDE ---
REM Nejdříve smažeme starou složku ffmpeg, pokud existuje, abychom zabránili vnoření.
if exist ffmpeg rmdir /s /q ffmpeg

REM Přejmenujeme nově staženou verzi na jednotný název.
for /d %%i in (ffmpeg-*) do (
    move "%%i" ffmpeg
)
del %FFMPEG_ARCHIVE%


echo.
echo FFmpeg extracted.
cd ..
set PATH=%CD%\tools\ffmpeg\bin;%PATH%
echo Added to PATH for current session.

echo.
echo -----------------------------------------
echo INSTALLATION COMPLETE!
echo You can now run the program using:
echo.
echo run_debug.bat (to see the console with logs)
echo OR
echo run.bat (for normal use without a console)
echo -----------------------------------------
echo.
pause

exit /b 0