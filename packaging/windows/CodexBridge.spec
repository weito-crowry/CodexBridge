from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
SOURCE = ROOT / "src"
ENTRY = ROOT / "packaging" / "windows" / "codexbridge_entry.py"
ICON = SOURCE / "codex_bridge" / "assets" / "codexbridge_icon_256.ico"
SETUP_APP = SOURCE / "codex_bridge" / "assets" / "codexbridge_setup_app.html"

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=[(str(ICON), "codex_bridge/assets"), (str(SETUP_APP), "codex_bridge/assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
def is_icu_runtime(name: str) -> bool:
    normalized = Path(name).name.casefold()
    return normalized.endswith(".dll") and normalized.startswith(("icuuc", "icudt", "icuin"))


def is_pyside6_source(path: str) -> bool:
    return "pyside6" in {part.casefold() for part in Path(path).parts}


# Keep ICU runtime DLLs from the build machine out of the package. QtCore
# imports the Windows ICU API, so a third-party ICU can shadow Qt's compatible
# runtime (for example, Poppler's versioned ICU exports). Preserve any ICU
# runtime that PyInstaller sourced from PySide6 itself.
a.binaries = [
    binary
    for binary in a.binaries
    if not (is_icu_runtime(binary[0]) and not is_pyside6_source(binary[1]))
]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CodexBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)
collect = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CodexBridge",
)
