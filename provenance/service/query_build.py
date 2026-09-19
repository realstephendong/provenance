"""Code -> two query representations.

2.2 is the load-bearing idea here: code is never embedded directly against Slack
prose. An LLM rewrites the selection into the register an engineer would use talking
about it, and *that* is what gets embedded. Identifiers are extracted separately and
carry the lexical channel, where exact token overlap is what matters.
"""

from __future__ import annotations

import re

from .. import config, llm

_DEF = re.compile(r"(?:^|\s)(?:def|function|func|fn)\s+([A-Za-z_][A-Za-z0-9_]*)")
_CONST = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
_CLASS = re.compile(r"\b([A-Z][a-zA-Z0-9]{3,})\b")
_METHOD = re.compile(r"\b([a-z_][a-z0-9_]{3,})\s*\(")
_STRING = re.compile(r"""['"]([^'"\n]{5,60})['"]""")
_IMPORT = re.compile(r"(?:^|\n)\s*(?:import|from|require)\s+([\w./]+)")
_STOPWORDS = {
    "self", "this", "return", "print", "range", "super", "None", "True", "False",
    "else", "elif", "raise", "while", "async", "await", "class", "yield", "assert",
}

_ENCLOSING = re.compile(r"(?:^|\n)\s*(?:def|class|function|func|fn)\s+([A-Za-z_][A-Za-z0-9_]*)")


def extract_symbols(code: str, language: str | None = None) -> list[str]:
    """Regex today, explicitly a swappable seam for tree-sitter later -- nothing else
    in the pipeline needs to know the difference."""
    found = []
    for pattern in (_DEF, _CONST, _CLASS, _METHOD, _IMPORT):
        found.extend(pattern.findall(code))
    found.extend(s.strip() for s in _STRING.findall(code))
    seen, out = set(), []
    for f in found:
        f = f.strip()
        if not f or f in _STOPWORDS or f.lower() in seen:
            continue
        seen.add(f.lower())
        out.append(f)
    return out[:40]


def _enclosing_symbol(code: str) -> str | None:
    match = _ENCLOSING.search(code)
    return match.group(1) if match else None


PROSE_PROMPT = """You are given a fragment of code. Write 2-3 sentences describing
what it does, in the words an engineer would use talking about it in Slack.

Name the concrete things: the service or system it talks to, the specific constants
or thresholds and what they would be for, the failure mode it appears to guard
against. If something looks deliberate but unexplained -- a magic number, an unusual
retry, a narrow special case -- say so plainly, because that is what we are searching
for.

Do not explain the syntax. Do not describe it as "this function".

File: {file_path}
Enclosing symbol: {enclosing}

{code}"""


async def build_queries(code: str, file_path: str, language: str | None) -> dict:
    code = code[:config.MAX_CODE_CHARS]     # cap tokens sent to the LLM; long selections get truncated, not rejected
    symbols = extract_symbols(code, language)
    try:
        description = await llm.complete(PROSE_PROMPT.format(
            file_path=file_path, enclosing=_enclosing_symbol(code) or "unknown", code=code,
        ), config.HOT_PATH_MODEL, max_tokens=300)
    except Exception:
        # Graceful degrade (18 row 7): a failed code-to-prose call must not 500 the
        # whole request. Fall back to a naive description built from what regex
        # already extracted -- worse recall, but the request still completes.
        description = f"Code in {file_path} involving: {', '.join(symbols[:15]) or 'no extracted symbols'}."
    return {
        "symbols": symbols, "description": description, "dense_text": description,
        "sparse_text": " ".join(symbols) + "\n" + description,
    }
