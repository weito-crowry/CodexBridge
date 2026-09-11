# CodexBridge Managed Console Lifecycle Design

## Goal
CodexBridge Consoleの通常操作を「起動する」「トレイに収納する」「完全終了する」のほぼ3操作へ簡略化し、Bridge / Codex App Server / Tunnelの個別起動停止を通常ユーザーから隠す。通常運用ではConsoleがスタックを自動起動・自動復旧し、保守操作だけStatusのAdvancedへ退避する。

## User Experience
- Console起動だけで利用可能状態まで自動遷移する。
- ウィンドウ右上の×は従来どおり終了ではなくTrayへ収納する。
- 完全終了はTrayのExitから行う。
- メイン画面常設は Overall status、Codex Usage、Statusボタンを基本とする。
- Bridge / App Server / Tunnel の個別Start/Stopは通常画面から外し、StatusのAdvancedへ退避する。
- 障害時の通常操作は `Retry now` と `Restart CodexBridge` を中心にする。

## Managed Startup
1. Console起動時に既存Bridgeのhealth/statusを確認する。
2. 既存BridgeがReadyならexternalとしてそのまま利用し、起動し直さない。
3. Bridgeが存在しない、または利用不能で、Codex executableとallowed rootsが有効ならConsole-owned Bridgeを自動起動する。
4. Bridge内部でCodex App Serverを従来どおり自動起動し、initialize/initialized完了とUI API Readyを待つ。
5. Bridge/App Server Ready後、Tunnel preflight（version/doctor）を実行する。
6. preflight成功後、Tunnelを自動起動する。
7. Bridge / App Server / Tunnel が利用可能になったらOverallをReadyとする。
8. Usage取得とCodex update checkは起動フローに追従するが、失敗してもOverall Readyを妨げない。

## Bridge Startup Retry
- Console-owned Bridgeの起動失敗は1回でユーザー操作待ちにしない。
- 初回失敗後は 2秒、5秒、10秒 の待機を挟んで再試行する。
- 3回の再試行後もReadyにならない場合はOverallをErrorにし、Statusに `Retry now` を表示する。
- 無限高頻度リトライは行わない。

## Automatic Bridge Recovery
- Ready後にBridge health/statusが一度失敗しただけでは再起動しない。
- 連続2回失敗で喪失と判定する。
- Console-owned Bridgeが喪失した場合は自動再起動する。
- external Bridgeが喪失した場合も、連続2回失敗後はConsole-owned Bridgeを自動起動して引き継ぐ。
- 自動復旧後はApp Server Ready、Tunnel preflight、Tunnel復旧まで自動で進める。

## Tunnel Startup and Recovery
- Bridge Ready後、Tunnel preflight成功時はユーザー操作なしでTunnelを起動する。
- Consoleが既に起動済みのTunnelを保持している場合は二重起動しない。
- 外部Tunnelの完全なownership/status判定は今回新規に大規模実装しない。
- Tunnel起動が「既に使用中」等で失敗した場合は、外部Tunnel稼働の可能性を含むDegradedとして扱い、詳細をStatusへ表示する。
- Console-owned Tunnelが異常終了した場合の再接続間隔は 1秒、3秒、10秒、30秒。
- 上記で復旧しない場合も停止せず、Degraded表示のまま60秒間隔で復旧を継続する。
- BridgeがReadyでない間はTunnel再起動を試みない。

## Overall Status
Overallは以下の4状態に整理する。
- Starting: 起動・再接続・自動復旧中。
- Ready: Bridge、App Server、Tunnelが利用可能でChatGPT側から利用できる状態。
- Degraded: ローカルBridge/App Serverは使えるがTunnel等の一部機能が利用不能な状態。
- Error: Bridge/App Server自体を所定回数の再試行後も利用可能にできない状態。

Usage取得失敗、Codex update check失敗はOverallをDegraded/Errorにしない。

## Usage and Update
- Codex Usageはメイン画面で常時表示する。
- 既存仕様の起動後遅延取得、限定リトライ、5分pollingを維持する。
- 取得失敗時は最後の正常値を維持する。
- Codex update checkは起動後に自動確認し、新版があればメインへ短い通知を出す。
- update check失敗はOverall Ready判定に影響させない。

## Window Close and Exit
### Window Close
- Tray利用可能時のウィンドウ×は `hide()` してTrayへ収納する。
- Console / Bridge / Tunnelは稼働継続する。

### Tray Exit
- Console-owned Tunnelを停止する。
- Console-owned Bridgeへgraceful shutdownを要求する。
- Bridge shutdownに連動してCodex App Serverを停止する。
- external Bridgeは停止しない。
- external Tunnelは今回のConsoleから停止対象にしない。
- 所定の既存watchdog / graceful shutdown方針を維持する。

## Restart CodexBridge
Statusに通常ユーザー向けの `Restart CodexBridge` を1つ設ける。
- Console-owned Tunnelを停止する。
- Console-owned Bridgeを停止する。
- BridgeをConsole-ownedとして起動する。
- App Server Readyを待つ。
- Tunnel preflight後にTunnelを起動する。
- Readyまで自動遷移する。
- external Bridgeを使用中の場合はexternal processを停止せず、Console-managed stackを新規に確立する方向とする。

## Status UI
メイン画面は概ね以下に簡略化する。

`● Ready    Codex Usage 5h 72% · Week 61%    [Status]`

異常時のみ短い理由を追加する。

Status画面には少なくとも以下を表示する。
- Overall
- Bridge
- App Server
- Tunnel
- Codex version / update status
- Usage
- Retry now
- Restart CodexBridge
- Advanced

Advancedには診断・保守用の個別Bridge/Tunnel操作と詳細情報を置く。通常ユーザーが個別コンポーネントを判断して操作することを前提にしない。

## Ownership Rules
- Consoleが起動したBridge/Tunnelのみownedとして停止対象にする。
- external Bridgeは利用できる限りそのまま使う。
- external Bridge喪失時はConsole-owned Bridgeへ自動引継ぎしてよい。
- ユーザーの外部processを勝手にkillしない。

## Scope / Non-goals
- Windows Service化しない。
- 新しい常駐daemon architectureへ変更しない。
- tunnel-client側の大規模な外部Tunnel discovery/status protocolは今回実装しない。
- Bridge/App Server protocolを不要に変更しない。
- unrelated refactorを行わない。
- Usage/update機能を再設計しない。
- main/shared branchへ直接mergeしない。

## Verification Requirements
実装時は少なくとも以下をテストする。
- 既存Bridge Ready時に再起動しない。
- Bridge不在時に自動起動する。
- Bridge起動失敗時の 2s/5s/10s retry と最終Error。
- Console-owned Bridgeの連続2回health/status失敗で自動復旧する。
- external Bridge喪失後にConsole-owned Bridgeへ引継ぐ。
- Bridge Ready後にTunnel preflightから自動起動する。
- Tunnel異常終了時の 1s/3s/10s/30s retry と、その後60s recovery。
- Usage/update失敗がOverall Readyを壊さない。
- ×でTray収納しruntimeを停止しない。
- Tray Exitでowned Tunnel/Bridgeだけを停止する。
- Restart CodexBridgeの一連の遷移。
- Advancedの個別操作が既存保守用途を維持する。
