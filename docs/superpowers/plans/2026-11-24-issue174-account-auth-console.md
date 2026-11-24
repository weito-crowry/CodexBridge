# Issue #174: Codex account authentication in Qt Console

## Intent and boundaries

Show App Server connectivity separately from Codex account authentication, classify only evidence-backed auth failures, and let the local Qt Console start or cancel the existing App Server ChatGPT login flow. Keep account details to known account type, auth mode, and plan values. Never expose email, user ID, login ID, URL, user code, tokens, or raw account/login errors through general status, SSE, MCP, logs, diagnostics, or HTTP errors.

Use the installed `codex-cli 0.171.0` schema from side-effect-free `app-server generate-ts`; no real App Server, browser, account, or turn is used. Reuse `account/read`, `account/login/start`, `account/login/cancel`, `account/login/completed`, and `account/updated`. Device code is supported by this installed schema. Browser URLs are limited to the official `https://auth.openai.com` issuer and the expected login/device paths.

The approved architecture is the existing App Server RPC -> Bridge -> narrow UI API -> asynchronous Qt controller path. OAuth implementation, auth-file access, arbitrary RPC forwarding, logout, account switching, and failed-turn retry are out of scope.

## Files and responsibilities

- `src/codex_bridge/account_auth.py`: safe snapshot projection, error classification, generation-aware single-flight reads, login correlation and lifecycle.
- `src/codex_bridge/bridge.py`, `server.py`, `ui_api.py`: RPC integration, notification refreshes, safe read DTO, authenticated local start/cancel routes, and no-store login responses.
- `src/codex_bridge/console/api_client.py`, new `console/account_auth_controller.py`, `main_window.py`, `main_window_layout.py`, `settings_actions.py`, `api_error_presentation.py`, and `console/locales/ja-JP.json`: nonblocking status, login/cancel/device-code UX, URL validation, and Japanese/English messages.
- Focused tests in `tests/test_account_auth.py`, `tests/test_bridge.py`, `tests/test_ui_api.py`, and Console auth/controller/API/layout tests. No unrelated workflow or CPP/LAN changes.

## Ordered tasks

1. Add failing unit tests for account-read projection, malformed/unsupported values, auth-error classification, URL acceptance/rejection, and login notification races; implement the small domain service and rerun those tests.
2. Add failing Bridge/UI API tests for exact RPC methods/params, read refresh triggers, safe DTO/error responses, single login, cancel correlation, Origin/Host/control-token enforcement, and no-store responses; implement the endpoints without a general RPC proxy.
3. Add failing offscreen Qt/API tests for connection transitions, generation/single-flight behavior, safe browser launch, device-code display, cancel/failure/timeout recovery, and i18n; add the status and Settings actions without changing turn terminal behavior.
4. Run focused and requested repository verification, inspect the scoped diff, stage only Issue #174 files, commit with `#174`, and normally push to `origin/codex/issue174-account-auth-console`. Do not close the Issue or merge.

## Review focus

- A stale read or notification from a prior connection/login must not overwrite current state.
- `account/read` must use `{}`; no forced refresh, and only its minimal projected DTO may leave the Bridge.
- `unauthorized` structured turn error triggers a read; bare HTTP 401/403, 429/quota, network, approval, disconnect, and generic RPC errors do not become “login required”.
- Auth start/cancel must fail closed without the Console control token and local Origin/Host checks. Only the two known login types may be sent.
- URL userinfo, non-HTTPS, wrong host/path, or non-default port must not launch. Secrets never enter logs/events/general DTOs.
- Successful auth refreshes account status and usage but never restarts a failed turn or Bridge.

## Execution record

- Base: `origin/dev` at `43ba7164c3be568f98ed98bbfda87346d47f6662`.
- Branch: `codex/issue174-account-auth-console`.
- Original CPP branch and its two unpushed commits remain untouched; pre-existing untracked paths remain untouched.
- Focused auth, UI API, Bridge, Console client, and menu regression suite: 156 passed after the final race fix.
- Ruff check/format, `mypy src`, `compileall src tests`, and `git diff --check`: passed.
- Module-size guard: failed on 22 existing files outside this change; the new auth modules were not reported.
- Full `uv run --no-sync pytest -q`: interrupted after about 87 minutes at approximately 25% progress in a sustained CPU-heavy case. It did not produce a complete suite result.
- Real Windows visible Console/browser/login/turn acceptance: NOT_RUN by instruction.
