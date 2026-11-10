"""Helpers for guard tests that must not match their own documentation.

EIGHT TIMES on this project a guard written as a text search has matched
the prose explaining the very thing it forbids:

    set_                   matched  asset_number
    INSERT                 matched  sys.path.insert
    git diff --quiet       matched  an explanatory comment about it
    kill -9                matched  "Do NOT use kill -9"
    ticket_lab_authority   matched  a docstring forbidding its import
    aft_cashout            matched  _decline_aft_cashout, the fix itself
    printer fault          matched  "no printer fault is logged"
    events per cycle       matched  "this used to read 'events per cycle'"

The pattern is not carelessness. A careful author DOCUMENTS the thing being
forbidden, right next to the code that forbids it, and that documentation
is what the guard finds. The more thorough the comment, the likelier the
false positive.

So: match what RUNS. These helpers strip the parts of a file that are prose
rather than behaviour. For Python, prefer `ast` over any of this -- a guard
on imports or calls cannot be fooled at all.
"""

from __future__ import annotations

import re


def shell_commands_only(source: str) -> str:
    """A shell script with comment lines and echo/printf text removed."""
    out = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "echo" in stripped or "printf" in stripped:
            continue
        out.append(line)
    return "\n".join(out)


def python_code_only(source: str) -> str:
    """Python with docstrings, comments and string literals removed.

    Uses the tokenizer rather than a regex, so a `#` inside a string and a
    quote inside a comment are both handled.
    """
    import io
    import tokenize

    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        # A partial snippet is better than nothing, and a guard that cannot
        # tokenize should say so by failing rather than by passing blindly.
        return source
    return " ".join(out)
