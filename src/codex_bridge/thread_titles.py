from __future__ import annotations

import re
import unicodedata

_MAX_TITLE_LENGTH = 100
_PROMPT_SCAN_LENGTH = 4_096
_PROMPT_SCAN_LINES = 40
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[ _-]?key|access[ _-]?token|token|secret|password|authorization|bearer)"
    r"\s*[:=]\s*\S+"
)
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_./+=-]{40,}(?![A-Za-z0-9])")
_GENERIC_TITLES = {
    "background",
    "chat",
    "conversation",
    "context",
    "description",
    "goal",
    "hello",
    "help",
    "help me",
    "hi",
    "issue",
    "issue details",
    "issue summary",
    "new",
    "new chat",
    "new codex thread",
    "new thread",
    "no specific request",
    "objective",
    "overview",
    "problem",
    "please help",
    "please inspect this",
    "please review this",
    "please see below",
    "request",
    "requirements",
    "user request",
    "inspect this",
    "review this",
    "summary",
    "task",
    "instructions",
    "start",
}
_TEMPLATE_OPENINGS = (
    "act as ",
    "follow these instructions",
    "here is your task",
    "here are your instructions",
    "you are chatgpt",
    "you are codex",
)
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’"}


def normalize_explicit_title(title: str) -> str:
    if not isinstance(title, str):
        raise ValueError("title must be text")
    if any(
        unicodedata.category(character) == "Cc" and character not in "\r\n\t" for character in title
    ):
        raise ValueError("title contains unsupported control characters")
    normalized = " ".join(title.split())
    if not normalized:
        raise ValueError("title must not be blank")
    return _bound_title(normalized)


def derive_thread_title(prompt: str, thread_id: str) -> str:
    headings: list[str] = []
    opening_lines: list[str] = []
    in_fence = False
    lines = prompt[:_PROMPT_SCAN_LENGTH].splitlines()[:_PROMPT_SCAN_LINES]
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        while line.startswith(">"):
            line = line[1:].lstrip()
        marker_length = len(line) - len(line.lstrip("#"))
        is_heading = 1 <= marker_length <= 6 and (
            len(line) == marker_length or line[marker_length].isspace()
        )
        if is_heading:
            line = line[marker_length:].strip()
        elif len(line) > 2 and line[0] in "-*+" and line[1].isspace():
            line = line[2:].strip()
        else:
            marker, separator, rest = line.partition(" ")
            if separator and marker.rstrip(".)").isdigit():
                line = rest.strip()
        line = _strip_outer_quotes(line)
        label, separator, rest = line.partition(":")
        if separator and label.casefold() in {"goal", "request", "task", "title"}:
            line = _strip_outer_quotes(rest.strip())
        if not line:
            continue
        (headings if is_heading else opening_lines).append(line)

    for candidate in (*headings, *opening_lines):
        if _is_template_opening(candidate) or _is_generic_title(candidate):
            continue
        if _contains_secret(candidate):
            continue
        return _bound_title(" ".join(candidate.split()))

    suffix = thread_id[-8:]
    return f"New Codex thread ({suffix})"


def _bound_title(title: str) -> str:
    if len(title) <= _MAX_TITLE_LENGTH:
        return title
    return title[: _MAX_TITLE_LENGTH - 1].rstrip() + "\u2026"


def _strip_outer_quotes(line: str) -> str:
    if len(line) >= 2 and line[0] in _QUOTE_PAIRS and line[-1] == _QUOTE_PAIRS[line[0]]:
        return line[1:-1].strip()
    return line


def _is_generic_title(line: str) -> bool:
    normalized = line.casefold().strip().rstrip(".!?")
    return normalized in _GENERIC_TITLES


def _is_template_opening(line: str) -> bool:
    return line.casefold().startswith(_TEMPLATE_OPENINGS)


def _contains_secret(line: str) -> bool:
    return bool(_SECRET_ASSIGNMENT.search(line) or _LONG_TOKEN.search(line))
