"""Automatic checks run on every draft before it goes to the review queue."""
import re


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def copy_overlap(draft: str, source: str, n: int = 8) -> float:
    """Share of the draft's n-word phrases that also appear in the source (0..1)."""
    dw, sw = _words(draft), _words(source)
    if len(dw) < n or len(sw) < n:
        return 0.0
    src = {tuple(sw[i:i + n]) for i in range(len(sw) - n + 1)}
    grams = [tuple(dw[i:i + n]) for i in range(len(dw) - n + 1)]
    return sum(g in src for g in grams) / len(grams)


NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:\s?(%|kwh|kw|km|mph|hp|bhp|nm|units|million|billion|lakh|crore))?", re.I)


def numbers_in(text: str) -> set[str]:
    out = set()
    for m in NUM.finditer(text):
        n = m.group(1).replace(",", "")
        if len(n) == 1:  # single digits are usually ordinals/list numbers
            continue
        out.add(n)
    return out


def unsupported_numbers(draft: str, sources_text: str) -> list[str]:
    """Numbers in the draft that never appear in any source text (likely invented or miscalculated)."""
    allowed = numbers_in(sources_text)
    # years and common rounding are fine
    missing = []
    for n in sorted(numbers_in(draft)):
        if n in allowed:
            continue
        if re.fullmatch(r"(19|20)\d\d", n):
            continue
        missing.append(n)
    return missing
