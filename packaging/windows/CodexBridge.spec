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
# Keep unrelated ICU DLLs found on the build machine's PATH out of the package.
# QtCore imports the Windows ICU API; a bundled third-party ICU can shadow it
# with an incompatible ABI (for example, Poppler's versioned ICU exports).
a.binaries = [
    binary
    for binary in a.binaries
    if not (
        Path(binary[0]).name.casefold() == "icuuc.dll"
        and "pyside6" not in {part.casefold() for part in Path(binary[1]).parts}
    )
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
