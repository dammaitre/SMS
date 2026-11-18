# PyInstaller spec: single-file `btsms` executable. Build with `make`.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = SPECPATH + "/.."

a = Analysis(
    [SPECPATH + "/entry.py"],
    pathex=[ROOT + "/src"],
    # textual loads widgets lazily and ships .tcss/.scm data files
    hiddenimports=collect_submodules("textual.widgets") + collect_submodules("dbus_fast"),
    datas=collect_data_files("textual"),
    excludes=["tkinter", "pytest", "unittest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="btsms",
    console=True,
    strip=True,
    upx=False,
)
