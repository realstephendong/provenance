"""Code -> query.

The critical insight of the whole system: never embed raw code and compare it
to Slack prose. They are different modalities in the same vector space and the
results are thematically adjacent noise. Rewrite the code into prose first,
and run a separate lexical channel for identifiers.
"""

from __future__ import annotations

import asyncio
import re

from .. import config, llm

# Trap #2: tree-sitter grammar setup eats an hour and the regex fallback gets
# 80% of the value. This is the seam -- swap the body, keep the signature.
_CLASS = re.compile(r"\b([A-Z][a-zA-Z0-9]{3,})\b")
_CONST = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
_DEF = re.compile(r"(?:^|\s)(?:def|function|func|fn)\s+([A-Za-z_][A-Za-z0-9_]*)")
_METHOD = re.compile(r"\b([a-z_][a-z0-9_]{3,})\s*\(")
_STRING = re.compile(r"""['"]([^'"\n]{5,60})['"]""")
_IMPORT = re.compile(r"(?:^|\n)\s*(?:import|from|require)\s+([\w./]+)")

_STOPWORDS = {
    "self", "this", "return", "print", "range", "super", "None", "True", "False",
    "else", "elif", "raise", "while", "async", "await", "class", "yield", "assert",
}


def extract_symbols(code: str, language: str | None = None) -> list[str]:
    """Identifiers worth searching for lexically.

    Regex today. A tree-sitter implementation is a drop-in replacement behind
    this signature; nothing else in the pipeline knows the difference.
    """
    found: list[str] = []
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


def enclosing_symbol(code: str) -> str | None:
    m = _DEF.search(code)
    if m:
        return m.group(1)
    m = re.search(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)", code)
    return m.group(1) if m else None


PROSE_PROMPT = """You are given a fragment of code. Write 2-3 sentences describing what it
does, in the words an engineer would use talking about it in Slack.

Name the concrete things: the service or system it talks to, the specific
constants or thresholds and what they would be for, the failure mode it
appears to guard against. If something looks deliberate but unexplained -
a magic number, an unusual retry, a narrow special case - say so plainly,
because that is what we are searching for.

Do not explain the syntax. Do not describe it as "this function".

File: {file_path}
Enclosing symbol: {enclosing}

{code}"""


async def code_to_prose(code: str, file_path: str) -> str:
    prompt = PROSE_PROMPT.format(
        file_path=file_path,
        enclosing=enclosing_symbol(code) or "unknown",
        code=code,
    )
    return await llm.complete(prompt, config.HOT_PATH_MODEL, max_tokens=300)


async def build_queries(code: str, file_path: str, language: str | None) -> dict:
    """Returns the two query texts plus the symbols, for scoring and display."""
    symbols = extract_symbols(code, language)
    description = await code_to_prose(code, file_path)
    return {
        "symbols": symbols,
        "description": description,
        "dense_text": description,
        "sparse_text": " ".join(symbols) + "\n" + description,
    }
