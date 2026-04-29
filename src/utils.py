"""Shared utility helpers (logging, date parsing, text normalisation)."""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from typing import Any

from loguru import logger


def setup_logging(verbose: bool = False) -> None:
    """Configure loguru with a single stderr sink in GitHub-Actions-friendly format."""

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{name}</cyan> - {message}",
        colorize=True,
    )


def parse_date(value: Any) -> date | None:
    """Tolerant date parser — Semantic Scholar returns mixed shapes."""

    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def title_tokens(title: str) -> set[str]:
    return set(_TOKEN_RE.findall(title.lower()))


def title_jaccard(a: str, b: str) -> float:
    """Jaccard similarity over lowercase alphanumeric tokens."""

    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Text sanitisation for display ──────────────────────────────────────────
# Common sources of ugly raw text in abstracts (as seen in arXiv / SS data):
#   * LaTeX math:   $\pi_{0.6}$, $x^2$, $$\mathcal{L}$$
#   * Inline cite:  \cite{foo}, \ref{bar}
#   * Markdown em:  *word*, **word**, _word_
#   * HTML entities already decoded will pass through; we avoid double-escaping
_LATEX_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_LATEX_INLINE = re.compile(r"\$([^$]{1,120}?)\$")
_LATEX_CMD_ARGS = re.compile(r"\\[a-zA-Z]+\{[^}]*\}")
_LATEX_CMD_BARE = re.compile(r"\\[a-zA-Z]+")
_MD_BOLD = re.compile(r"\*\*([^*\n]{1,80})\*\*")
_MD_ITALIC_STAR = re.compile(r"(?<![*\w])\*([^*\s][^*\n]{0,80}?[^*\s]|[^*\s])\*(?!\*)")
_MD_ITALIC_UNDER = re.compile(r"(?<![_\w])_([^_\s][^_\n]{0,80}?[^_\s]|[^_\s])_(?!_)")
_MULTI_WS = re.compile(r"[ \t]{2,}")


def sanitise_abstract(text: str) -> str:
    """Strip LaTeX / markdown noise so emails and WeChat cards read cleanly.

    The goal is display-fidelity, not semantic fidelity: we replace math with
    its inner symbols (roughly), and remove ``\\cite`` / ``\\ref`` commands
    that never render usefully in plain HTML.
    """

    if not text:
        return text
    t = text
    # LaTeX math — keep the inner content but drop the delimiters. Replace
    # classic macros with their common glyphs so output doesn't look like code.
    t = _LATEX_DISPLAY.sub(lambda m: _latex_inner_to_plain(m.group(1)), t)
    t = _LATEX_INLINE.sub(lambda m: _latex_inner_to_plain(m.group(1)), t)
    # Bare LaTeX commands without args (e.g. ``\approx``): drop.
    t = _LATEX_CMD_ARGS.sub("", t)
    t = _LATEX_CMD_BARE.sub("", t)
    # Markdown italics / bold — keep the inner word.
    t = _MD_BOLD.sub(r"\1", t)
    t = _MD_ITALIC_STAR.sub(r"\1", t)
    t = _MD_ITALIC_UNDER.sub(r"\1", t)
    # Tidy whitespace without touching newlines.
    t = _MULTI_WS.sub(" ", t)
    return t.strip()


_LATEX_GLYPHS = {
    r"\pi": "π", r"\alpha": "α", r"\beta": "β", r"\gamma": "γ",
    r"\delta": "δ", r"\epsilon": "ε", r"\theta": "θ", r"\lambda": "λ",
    r"\mu": "μ", r"\sigma": "σ", r"\tau": "τ", r"\phi": "φ",
    r"\omega": "ω", r"\Sigma": "Σ", r"\Omega": "Ω",
    r"\times": "×", r"\approx": "≈", r"\leq": "≤", r"\geq": "≥",
    r"\rightarrow": "→", r"\to": "→",
}


def _latex_inner_to_plain(inner: str) -> str:
    """Best-effort: replace common LaTeX macros with Unicode glyphs, then strip
    residual ``{`` ``}`` / ``\\`` and trailing subscripts like ``_{0.6}``."""

    s = inner
    for tex, glyph in _LATEX_GLYPHS.items():
        s = s.replace(tex, glyph)
    # ``_{...}`` → subscript text
    s = re.sub(r"_\{([^}]*)\}", r"_\1", s)
    s = re.sub(r"\^\{([^}]*)\}", r"^\1", s)
    # strip remaining braces & backslashes; collapse whitespace
    s = s.replace("{", "").replace("}", "").replace("\\", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s
