from pathlib import Path
import os
import sys

root = Path(SPECPATH).parent
if sys.platform == 'win32':
    # Match native DLL search precedence. Build hosts can prepend unrelated ICU
    # libraries (for example Poppler) to PATH, shadowing the Windows ICU ABI Qt uses.
    system32 = str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32')
    os.environ['PATH'] = system32 + os.pathsep + os.environ.get('PATH', '')
a = Analysis([str(root / 'lastsync.py')], pathex=[str(root)], binaries=[], datas=[],
             hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['tkinter', 'pytest'], noarchive=False, optimize=1)
# This QtWidgets application paints with the raster engine. It does not use
# QOpenGLWidget / Qt Quick, so the optional 20 MB Mesa renderer is unnecessary.
omitted = {'opengl32sw.dll', 'icuuc.dll', 'icudt78.dll'}
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in omitted]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='LastSync',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
