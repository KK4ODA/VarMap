# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for VarMap.

    pyinstaller packaging/varmap.spec                 -> one-folder build in dist/VarMap  (Windows installer / portable zip)
    VARMAP_ONEFILE=1 pyinstaller packaging/varmap.spec -> single executable in dist/       (macOS, Linux)

Data files are added as (source, destination) tuples so the same spec works on
every platform; the app locates them relative to its own package path.
"""
import os
import sys

root = os.path.abspath(os.path.join(SPECPATH, ".."))
onefile = os.environ.get("VARMAP_ONEFILE", "0") == "1"

datas = [
    (os.path.join(root, "varmap", "web", "static"), os.path.join("varmap", "web", "static")),
    (os.path.join(root, "varmap", "web", "templates"), os.path.join("varmap", "web", "templates")),
    (os.path.join(root, "varmap", "storage", "schema.sql"), os.path.join("varmap", "storage")),
]
hidden = ["waitress", "serial", "serial.tools.list_ports"]
if sys.platform == "win32":
    hidden += ["win32gui", "win32con", "win32process", "win32api"]

a = Analysis(
    [os.path.join(root, "varmap_launcher.py")],
    pathex=[root],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if onefile:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="VarMap", debug=False, strip=False, upx=False,
              console=True, disable_windowed_traceback=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VarMap", debug=False, strip=False, upx=False,
              console=True, disable_windowed_traceback=False)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VarMap")
