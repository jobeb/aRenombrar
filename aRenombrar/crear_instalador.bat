@echo off
cd /d "%~dp0"
echo ============================================
echo   aRenombrar - Crear instalador Setup.exe
echo ============================================
echo.

:: Detectar Python
set PYTHON=
python --version >nul 2>&1 && set PYTHON=python
if "%PYTHON%"=="" (py --version >nul 2>&1 && set PYTHON=py)
if "%PYTHON%"=="" set PYTHON=C:\Users\Jose\AppData\Local\Programs\Python\Python313\python.exe

echo Usando Python: %PYTHON%
echo.

:: [1] Instalar dependencias + PyInstaller
echo [1/4] Instalando dependencias...
"%PYTHON%" -m pip install customtkinter Pillow requests pyinstaller pystray --quiet
echo       OK
echo.

:: [2] Compilar con PyInstaller usando aRenombrar.spec
echo [2/4] Compilando a .exe con PyInstaller...
if exist dist\aRenombrar rmdir /s /q dist\aRenombrar
if exist build rmdir /s /q build

"%PYTHON%" -m PyInstaller --noconfirm aRenombrar.spec

if errorlevel 1 (
    echo ERROR al compilar con PyInstaller.
    pause
    exit /b 1
)
echo       OK
echo.

:: [3] Buscar Inno Setup (busqueda amplia)
echo [3/4] Buscando Inno Setup...
set ISCC=

:: Buscar con PowerShell en Program Files (cubre cualquier version)
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-ChildItem -Path 'C:\Program Files','C:\Program Files (x86)' -Filter ISCC.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName"') do set ISCC=%%i

if "%ISCC%"=="" (
    echo.
    echo ERROR: Inno Setup no encontrado.
    echo Asegurate de haberlo instalado desde: https://jrsoftware.org/isdl.php
    echo Luego vuelve a ejecutar este script.
    pause
    exit /b 1
)
echo       Encontrado: %ISCC%
echo.

:: [4] Compilar el instalador
echo [4/4] Generando aRenombrar_Setup.exe...
if exist installer_output rmdir /s /q installer_output
"%ISCC%" setup.iss

if errorlevel 1 (
    echo ERROR al generar el instalador.
    pause
    exit /b 1
)
echo       OK
echo.

echo ============================================
echo   Instalador listo en:
echo   installer_output\aRenombrar_Setup.exe
echo ============================================
echo.
echo Puedes compartir ese archivo con quien quieras.
echo Al ejecutarlo, instala la app con acceso directo