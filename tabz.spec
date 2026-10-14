# PyInstaller build: python -m PyInstaller tabz.spec --noconfirm  ->  dist/tabz.exe
import os
from PyInstaller.utils.hooks import collect_submodules

ui = [(os.path.join("tabz", "ui", f), os.path.join("tabz", "ui"))
      for f in os.listdir(os.path.join("tabz", "ui")) if f != "dev.html" and f != "fonts"]
fonts = [(os.path.join("tabz", "ui", "fonts", f), os.path.join("tabz", "ui", "fonts"))
         for f in os.listdir(os.path.join("tabz", "ui", "fonts"))]

a = Analysis(
    ["run.py"],
    datas=ui + fonts,
    hiddenimports=collect_submodules("winrt") + collect_submodules("winocr") + ["clr"],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "skimage", "fitz", "pymupdf", "PyQt5", "PySide6"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="tabz",
    icon=os.path.join("tabz", "ui", "icon.ico"),
    console=False,
    upx=False,
)
