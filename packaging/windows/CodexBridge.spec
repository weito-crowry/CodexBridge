from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
SOURCE = ROOT / "src"
ENTRY = ROOT / "packaging" / "windows" / "codexbridge_entry.py"
ICON = SOURCE / "codex_bridge" / "assets" / "codexbridge_icon_256.ico"

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=[(str(ICON), "codex_bridge/assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
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
