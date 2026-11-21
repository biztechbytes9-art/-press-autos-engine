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


def headline_mismatch(title: str, excerpt: str, body: str) -> list[str]:
    """Numbers and superlatives in the title/meta description that the article body doesn't contain."""
    body_nums = numbers_in(body)
    problems = []
    for label, text in (("title", title or ""), ("meta description", excerpt or "")):
        missing = [n for n in sorted(numbers_in(text)) if n not in body_nums and not re.fullmatch(r"(19|20)\d\d", n)]
        if missing:
            problems.append(f"The {label} mentions {', '.join(missing)}, which the article body never states; "
                            f"either add it to the body (from the sources) or remove it from the {label}.")
        for word in ("cheapest", "fastest", "first", "biggest", "best", "only", "most powerful", "longest"):
            if re.search(r"\b" + word + r"\b", text, re.I) and not re.search(r"\b" + word + r"\b", body, re.I):
                problems.append(f'The {label} says "{word}", but the article body does not; remove it or support it.')
    return problems


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
