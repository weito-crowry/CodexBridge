# Issue #123: Python 3.14 Windows パッケージ検証

## 結果と範囲

2026-11-07、`codex/python314-migration` の `16a53d2` を、既存環境の
Python 3.14.10 / PySide6 6.11.2 / PyInstaller 6.22.3 で再パッケージした。
ビルド、実 EXE の非対話起動・終了、同梱 Qt/アセット/主要モジュールの補助検証は **PASS**。
今回の実 EXE 起動では、過去の `blocked by policy` は発生しなかった。

通常の Console ウィンドウ表示、Bridge 起動、MCP 接続、実スレッド一覧取得、
通常 GUI 操作での終了は **NOT RUN（手動確認待ち）**。
CLI の終了や補助 QWidget の終了を、これらの確認済みとは扱わない。
Issue の全受入条件を完了したという結論ではない。

製品コード、spec、依存関係、設定は変更していない。
`tzdata` 警告に対して無条件の依存追加は行っていない。
最新 Issue 本文を照合し、コメントは 0 件だった。

## ビルドと成果物

`scripts/build_windows.ps1` は既存 `build/windows-package` と `dist` を清掃するため、
同じ spec を PyInstaller に直接渡し、新しい出力先を指定した。
既存 venv を直接使用し、依存更新や環境の同期は行っていない。

```powershell
$outputRoot = 'D:/CodexBridge/.runtime/issue-sequence-20261107/package-123'
.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean `
  --distpath "$outputRoot/dist" --workpath "$outputRoot/build" `
  packaging/windows/CodexBridge.spec
```

終了コード 0、約 228 秒。既存出力を再利用せずに Analysis から実行した。
同じスクリプトの .NET `ZipFile.CreateFromDirectory` 設定
（Optimal、親ディレクトリを含む）で新しい ZIP も作成した。
ZIP は 387 entries を含み、`zipfile.testzip()` と EXE entry の存在確認は PASS。

成果物の基点は `D:/CodexBridge/.runtime/issue-sequence-20261107/package-123/`。

| 成果物 | サイズ | SHA-256 |
| --- | ---: | --- |
| `dist/CodexBridge/CodexBridge.exe` | 11,285,513 bytes | `43c73586b08950f768b054b84840a8fb2c66c42e530c5d9f8d0058040b3a839b` |
| `CodexBridge-windows.zip` | 70,519,513 bytes | `af4ab4ca72a1c95821a8bef11c32b3f747480cdf3ac7b909b346744dfe4cc37a` |

元の `dist/CodexBridge/CodexBridge.exe`（11,274,453 bytes）と ZIP
（70,511,050 bytes）は保持し、更新時刻は 2026-11-04 のまま。
今回のビルド成果物・調査スクリプト・ログは commit に含めない。

## 実 EXE の非対話スモーク

ローカル補助スクリプト `package-123/smoke_exe.py` は実 EXE を
`subprocess.run`、`CREATE_NO_WINDOW`、20 秒 timeout で実行する。
子プロセスだけの環境で `CODEX_BRIDGE_CONFIG`、APPDATA、LOCALAPPDATA、USERPROFILE と
作業ディレクトリを専用 `smoke-home` に向け、継承した CODEX/Qt/Python 環境指定を除いた。
親プロセスやユーザーの環境設定は変更しない。

| 引数 | 結果 |
| --- | --- |
| `--help` | PASS: exit 0、usage/options の出力、stderr 空 |
| `--ui-port invalid` | PASS: 期待どおり exit 2、整数範囲の設定エラーを出力 |

前者は設定読み込み・Qt 作成前、後者は設定検証で終了する既存 entry 経路である。
生成 EXE の bootloader と同梱 Python でコードが実行され、プロセスが終了することを確認した。
`smoke-home` のファイルは、用意した `config.toml` 一つだけだった。
Bridge、Codex、Tunnel、外部スレッド監視、ユーザーの稼働サービスは起動・操作していない。

## 同梱物と補助 Qt 実行

`bundle-inventory.json` に必要ファイルのサイズと hash を保存した。
`python314.dll`、`base_library.zip`、Qt6Core/Gui/Widgets/Network DLL、
QtCore/QtWidgets 拡張、`qwindows.dll`、`qoffscreen.dll` が存在する。
ICO と Setup HTML は checkout のアセットとバイト一致。
PySide6 外から混入した `icu*.dll` はない。
EXE 内の PYZ は 1,439 モジュールを収録している。

補助スクリプト `audit_bundle.py` / `probe_bundle.py` は EXE から
PYZ、PyInstaller の importer/bootstrap、pywintypes runtime hook を読み出し、
同じ Python 3.14.10 の `-I -S` 子プロセスで使用した。
モジュール検索パスはパッケージの base library、内部ディレクトリ、win32 関連だけとし、
checkout や venv の site-packages へのフォールバックを排除した。
DLL 検索・Qt plugin もパッケージへ向け、設定と QSettings は専用領域へ隔離した。

- Qt 6.11.2 の Core/Gui/Widgets/Network 拡張を同梱 DLL で読み込み。
- `_load_gui()` から MainWindow クラスを import。ただし MainWindow は構築しない。
- `QApplication` と小さな `QWidget` を offscreen で構築し、タイマーで close。
  イベントループは exit 0、window 非表示まで確認。
- 同梱アイコンを読み込み、32px pixmap の生成を確認。
- 隔離 TOML の `ui_port=54321` と config path の読み込みを確認。
- MainWindow、usage、server、mcp、uvicorn、psutil を import し、
  対象モジュールの origin がすべてパッケージ内部であることを確認。

これは **同梱コンポーネントの補助検証** であり、通常 EXE の GUI 起動ではない。
MainWindow によるサービス管理、MCP 通信、実データ読み込みは実行していない。

初回の補助検証は probe が省略した pywintypes runtime hook により exit 1、
二回目は省略した pywin32 bootstrap により exit 1 だった。
どちらの機構も製品 EXE には収録済みであり、補助検証側を実際の bootstrap に合わせた
三回目は exit 0。製品の修正やパッケージの再ビルドは行っていない。
失敗時の stderr もローカルに保持した。

## tzdata 警告の判定

新ビルドでも `WARNING: Hidden import "tzdata" not found!` が再現した。
導入済み `_pyinstaller_hooks_contrib/stdhooks/hook-zoneinfo.py` が、Windows の場合に
`hiddenimports = ['tzdata']` を指定するためである。
PYZ には zoneinfo が含まれる一方、tzdata は含まれない。

同梱コードの usage 日時変換を、tzdata の import spec が存在しない補助プロセスで実行した。
2026-11-07 00:00 UTC の epoch 秒、epoch ミリ秒、UTC ISO8601 文字列はすべて
`2026-11-07 09:00:00` となり、Windows ローカル日時への変換は **PASS**。
製品 `.py` の検索では `ZoneInfo` / `zoneinfo` / `tzdata` の直接利用はなかった。

一方、診断用の `ZoneInfo('Asia/Tokyo')` は **ZoneInfoNotFoundError** になった。
したがって、警告を「任意のタイムゾーン機能で実害がない」とは結論しない。
現行の確認済み entry/import と datetime の固定 UTC・Windows ローカル変換には
実害を観測していないが、IANA 名による明示的なタイムゾーン参照は使えない。
通常 GUI と全通信経路の実行時ログについては手動確認が残る。

## 自動確認と残件

次のコマンドはすべて exit 0。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_console_entry.py `
  tests/test_console_runtime_launcher.py tests/test_console_config.py `
  tests/test_console_usage.py -q `
  --junitxml=.runtime/issue-sequence-20261107/package-123/focused.xml
# 65 passed in 3.93s

.venv/Scripts/python.exe -m compileall -q packaging/windows/codexbridge_entry.py `
  src/codex_bridge/console_entry.py src/codex_bridge/console/config.py `
  src/codex_bridge/console/usage.py
.venv/Scripts/python.exe -m ruff check packaging/windows/codexbridge_entry.py `
  src/codex_bridge/console_entry.py src/codex_bridge/console/config.py `
  src/codex_bridge/console/usage.py
.venv/Scripts/python.exe -m ruff format --check packaging/windows/codexbridge_entry.py `
  src/codex_bridge/console_entry.py src/codex_bridge/console/config.py `
  src/codex_bridge/console/usage.py
.venv/Scripts/python.exe -m mypy src/codex_bridge/console_entry.py `
  src/codex_bridge/console/config.py src/codex_bridge/console/usage.py
.venv/Scripts/python.exe .runtime/issue-sequence-20261107/package-123/smoke_exe.py
.venv/Scripts/python.exe .runtime/issue-sequence-20261107/package-123/audit_bundle.py
```

Full pytest は今回の focused 方針により **NOT RUN**。
MainWindow の既存モジュールサイズ超過は別 Issue の対象で、本検証では変更していない。

ローカル証拠は `build.log`、`focused.log/xml`、`exe-smoke.json`、
`bundle-inventory.json`、`bundle-probe.json`、`bundle-probe.stdout/stderr`、
`bundle-probe-initial.stderr`、`bundle-probe-second.stderr`。
手動確認では、この新規成果物の通常ウィンドウ表示と設定読み込み、Bridge/MCP/スレッド一覧、
Qt/ICU/アセット関連ログ、通常終了を確認する。
