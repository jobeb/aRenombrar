# -*- mode: python ; coding: utf-8 -*-
import os
import re
import sys
from PyInstaller.utils.hooks import collect_all

# Leído del propio código fuente (no reescrito a mano aquí) para que el
# Info.plist del bundle de macOS nunca se quede desincronizado de la versión
# real de la app -- antes se quedó clavado en "1.0.0" mientras
# core/version.py ya iba por 1.1.0.
with open(os.path.join(SPECPATH, 'core', 'version.py'), encoding='utf-8') as _vf:
    _app_version = re.search(r'__version__\s*=\s*"([^"]+)"', _vf.read()).group(1)

# Windows quiere .ico; macOS quiere .icns (y Tk en tiempo de ejecución usa
# el PNG cuadrado en macOS/Linux vía iconphoto — ver gui/app.py:_apply_icon).
# Se incluyen los tres como datos para que estén disponibles pese a con qué
# plataforma se construya, y el .spec elige el que usa PyInstaller para el
# icono del propio ejecutable/bundle según la plataforma de build.
datas = [
    ('iconoPrincipal.ico', '.'),
    ('iconoPrincipal.icns', '.'),
    ('IconoSinFondo.png', '.'),
    ('plex_logo.png', '.'),
    ('jellyfin_logo.png', '.'),
]
binaries = []
hiddenimports = ['PIL._tkinter_finder']

tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

try:
    tmp_ret = collect_all('tkinterdnd2')
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
except Exception:
    pass

try:
    tmp_ret = collect_all('pystray')
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
except Exception:
    pass


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

_is_macos = sys.platform == 'darwin'
_icon = 'iconoPrincipal.icns' if _is_macos else 'iconoPrincipal.ico'

# upx=False a propósito: en un build onedir el espacio ahorrado no compensa
# la descompresión en cada carga de DLL, y los binarios empaquetados con UPX
# disparan escaneo heurístico más agresivo de Windows Defender en cada
# ejecutable "nuevo" (cada rebuild produce hashes distintos) -- eso es varios
# segundos de arranque que no aparecen en la instrumentación "Arranque:" de
# main.py/gui/app.py porque ocurren antes de que Python ejecute su primera
# línea.

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='aRenombrar',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=_is_macos,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[_icon],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='aRenombrar',
)

# Sin este bloque, en macOS PyInstaller deja una carpeta con un ejecutable
# Unix normal (sin Info.plist, sin icono de Dock, sin comportamiento de app
# bundle estándar) en vez de un .app de verdad.
if _is_macos:
    app = BUNDLE(
        coll,
        name='aRenombrar.app',
        icon='iconoPrincipal.icns',
        bundle_identifier='com.arenombrar.app',
        info_plist={
            'NSHighResolutionCapable': True,
            'CFBundleShortVersionString': _app_version,
            'LSUIElement': False,
        },
    )
