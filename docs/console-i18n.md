# Console translations

The Console supports `system`, `ja-JP`, and `en` in its existing
`QSettings("CodexBridge", "Console")` store under the `language` key. Missing
settings follow the system locale (`ja*` selects Japanese, everything else
English). Invalid/unsupported values select English. The Settings → Language
menu saves the next startup choice. The current UI keeps its startup language.

Application-owned display text uses explicit `tr("English source template",
p0=value)` calls from `console/i18n.py`. Japanese translations live in
`console/locales/ja-JP.json`; English uses the source template. Interpolation
happens after lookup. Keep arguments outside templates and preserve placeholder
names, conversion flags, and formatting specifications in the catalog.

Translate labels at their display boundary. Keep settings keys, API/HTTP/MCP
contracts, history kinds/modes, tray action keys, Usage preset keys, and process
inspection states unchanged. Catalog calls around known display-label maps are
intentional. Never pass arbitrary titles, model/user messages, filenames, paths,
commands, or raw log/error output through translation. Config validation uses
an enumerated presentation adapter so its exception contracts stay unchanged.
API errors likewise remain raw for control decisions (including approval
conflicts); known local messages are translated only when displayed.

Qt-owned labels use PySide6's `qtbase_ja.qm`, with the Console catalog providing
standard button overrides. One translator is installed per QApplication.
Attachment and CSV dialogs use Qt file dialogs so their labels honor the
Console selection independently of the Windows UI language. Original names,
returned paths, filters, and file handling are unchanged. PySide6 supplies the
Qt catalog in source/wheel installations; the Windows spec also includes it
explicitly. The app's JSON catalog ships in both wheel and EXE data.

`tests/test_console_i18n.py` checks locale resolution and persistence,
restart-only selection, Japanese/English Qt surfaces and native button labels,
raw content preservation, narrow Composer controls in both themes, catalog
placeholder parity, literal translation-call coverage, untranslated static UI
sinks, and untranslated dictionary keys. Legacy Console tests use an English
fixture independent of the host locale. To add a locale, extend the resolver,
menu, catalog loader, packaging, and these tests together.

Human Windows screenshots, DPI checks, keyboard/accessibility dogfood, and an
EXE launch remain separate manual checks; automated layout assertions do not
replace those observations.
