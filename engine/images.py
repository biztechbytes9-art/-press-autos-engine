"""Images for drafts, using only pictures we are allowed to publish.

1. Official press photos: when a story's source (or its primary source) is a carmaker/regulator press site
   (source_authority 1.0 in config.yaml), use that page's own photos, credited "Image: <site>".
2. Otherwise a branded news card is generated (headline, category, key figures): our own image, no copyright risk.
Photos from news sites are never used.
"""
import io
import logging
import textwrap
from urllib.parse import urljoin

import requests

from . import extract, sources

log = logging.getLogger("engine.images")

CAT_COLOURS = {
    "ev": "#059669", "motorcycles": "#ea580c", "used-cars": "#7c3aed", "suvs": "#0284c7",
    "commercial": "#475569", "tech": "#4f46e5", "motorsport": "#dc2626", "reviews": "#b45309",
    "compare": "#0d9488", "trends": "#1d4ed8",
}
CAT_LABELS = {"ev": "EV", "used-cars": "Used cars", "suvs": "SUVs"}
FONT_FILES = {
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
           "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"],
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"],
}
MIN_WIDTH = 800


# ------------------------------------------------------------------ official photos
def is_official(cfg: dict, url: str) -> bool:
    d = sources.domain_of(url or "")
    for dom, v in (cfg.get("source_authority") or {}).items():
        if (d == dom or d.endswith("." + dom)) and float(v) >= 1.0:
            return True
    return False


PRESS_HOSTS = ("press", "pressroom", "newsroom", "media", "news", "presse")
PRESS_PATHS = ("/press", "/media", "/newsroom", "/news/", "/news-", "/article/press-release", "/press-release")


def is_press_page(url: str) -> bool:
    """Maker websites are often "personal use only"; their press/media pages are meant for journalists."""
    from urllib.parse import urlparse
    u = urlparse(url or "")
    host = u.netloc.lower().removeprefix("www.")
    if host.split(".")[0] in PRESS_HOSTS:
        return True
    path = u.path.lower()
    return any(p in path for p in PRESS_PATHS) or host.endswith(".gov") or ".gov." in host or host.endswith("europa.eu")


def official_candidates(cfg: dict, srcs: list[dict], facts: dict, draft: dict) -> list[tuple[str, str]]:
    """(image URL, credit) pairs from official press/media pages only, best first."""
    pages = [s for s in srcs if is_official(cfg, s.get("url", "")) and is_press_page(s.get("url", ""))]
    have = {s.get("url") for s in srcs}
    for extra in (facts.get("primary_source"), draft.get("image_hint")):
        if not extra or extra in have:
            continue
        if is_official(cfg, extra) and is_press_page(extra) and not any(x in extra.lower() for x in (".jpg", ".jpeg", ".png", ".webp")):
            page = extract.fetch_article(extra, cfg["site"]["user_agent"])
            if page:
                pages.append(page)
                have.add(extra)
    out, seen = [], set()
    hint = draft.get("image_hint") or ""
    if hint and is_official(cfg, hint) and any(x in hint.lower() for x in (".jpg", ".jpeg", ".png", ".webp")):
        out.append((hint, "Image: " + credit_name(hint, "")))
        seen.add(hint)
    for p in pages:
        credit = "Image: " + credit_name(p.get("url", ""), p.get("site_name", ""))
        for u in p.get("images") or ([p["image"]] if p.get("image") else []):
            u = urljoin(p.get("url", ""), u)
            if u.startswith("http") and u not in seen:
                out.append((u, credit))
                seen.add(u)
    return out[:4]


def credit_name(url: str, site_name: str) -> str:
    if site_name and len(site_name) <= 40:
        return site_name
    d = sources.domain_of(url)
    parts = [p for p in d.split(".") if p not in ("www", "pressroom", "newsroom", "media", "news", "press", "group", "global")]
    return (parts[0] if parts else d).replace("-", " ").title()


def download_image(url: str, user_agent: str, timeout: int = 30) -> tuple[bytes, str, tuple[int, int]] | None:
    """Image bytes + MIME type + size, or None if missing, too small or too big."""
    try:
        r = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
        r.raise_for_status()
        mime = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            return None
        data = r.content
        if not 20_000 <= len(data) <= 12_000_000:
            return None
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            size = im.size
        if size[0] < MIN_WIDTH:
            return None
        return data, mime, size
    except Exception as exc:  # noqa: BLE001
        log.info("image skipped %s: %s", url, exc)
        return None


# ------------------------------------------------------------------ news card
def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    for path in FONT_FILES[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        test = (line + " " + w).strip()
        if draw.textlength(test, font=font) <= width:
            line = test
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def render_card(title: str, category: str, stats: list[str], edition_label: str = "") -> bytes:
    """1200x675 JPEG news card in the press.autos style."""
    from PIL import Image, ImageDraw
    W, H, M = 1200, 675, 72
    accent = _hex(CAT_COLOURS.get(category, "#2563eb"))
    img = Image.new("RGB", (W, H), (11, 18, 32))
    d = ImageDraw.Draw(img)
    # soft diagonal glow in the category colour
    for i in range(0, 420, 6):
        a = max(0, 60 - i // 7)
        col = tuple(int(bg + (ac - bg) * a / 255) for bg, ac in zip((11, 18, 32), accent))
        d.ellipse((W - 520 + i // 2, -300 + i // 2, W + 300 - i // 2, 420 - i // 2), outline=col, width=6)
    d.rectangle((0, 0, 14, H), fill=accent)

    # header: wordmark + edition
    f_brand = _font(34)
    d.text((M, 56), "press", font=f_brand, fill=(255, 255, 255))
    x = M + d.textlength("press", font=f_brand)
    d.text((x, 56), ".autos", font=f_brand, fill=accent)
    if edition_label:
        f_ed = _font(22, bold=False)
        d.text((x + d.textlength(".autos", font=f_brand) + 18, 66), edition_label.upper(), font=f_ed, fill=(148, 163, 184))

    # category pill
    label = CAT_LABELS.get(category, category.replace("-", " ").title() or "News").upper()
    f_pill = _font(22)
    pw = d.textlength(label, font=f_pill) + 36
    d.rounded_rectangle((M, 132, M + pw, 132 + 42), radius=21, fill=accent)
    d.text((M + 18, 141), label, font=f_pill, fill=(255, 255, 255))

    # headline, shrinking until it fits in 4 lines
    max_w = W - 2 * M
    for size in (66, 60, 54, 48, 44, 40):
        f_head = _font(size)
        lines = _wrap(d, title, f_head, max_w)
        if len(lines) <= 4:
            break
    lines = lines[:4]
    if len(lines) == 4 and d.textlength(lines[3], font=f_head) > max_w - 40:
        lines[3] = textwrap.shorten(lines[3], width=max(10, len(lines[3]) - 3), placeholder="…")
    y = 206
    for ln in lines:
        d.text((M, y), ln, font=f_head, fill=(255, 255, 255))
        y += int(size * 1.18)

    # key figures as chips along the bottom
    f_stat = _font(26)
    x, y = M, H - 112
    for s in [s for s in stats if s][:3]:
        s = textwrap.shorten(str(s), width=26, placeholder="…")
        w = d.textlength(s, font=f_stat) + 40
        if x + w > W - M:
            break
        d.rounded_rectangle((x, y, x + w, y + 54), radius=12, fill=(30, 41, 59), outline=accent, width=2)
        d.text((x + 20, y + 12), s, font=f_stat, fill=(226, 232, 240))
        x += w + 16

    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True, progressive=True)
    return out.getvalue()


# ------------------------------------------------------------------ main entry
def choose_images(cfg: dict, srcs: list[dict], facts: dict, draft: dict, edition: dict, edition_key: str) -> dict:
    """Returns {"featured": (bytes, mime, filename, alt, caption, kind), "inline": same or None}."""
    icfg = cfg.get("images") or {}
    ua = cfg["site"]["user_agent"]
    slug = (draft.get("slug") or draft.get("title", "press-autos"))
    slug = "".join(ch if ch.isalnum() else "-" for ch in slug.lower()).strip("-")[:60] or "press-autos"
    alt = draft.get("title", "")
    found = []
    if icfg.get("official_photos", True):
        for url, credit in official_candidates(cfg, srcs, facts, draft):
            got = download_image(url, ua)
            if got:
                data, mime, _ = got
                ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
                found.append((data, mime, f"{slug}-{len(found) + 1}.{ext}", alt, credit, "official"))
            if len(found) >= 2:
                break
    result = {"featured": None, "inline": None}
    if found:
        result["featured"] = found[0]
        result["inline"] = found[1] if len(found) > 1 and icfg.get("inline_photo", True) else None
    elif icfg.get("news_card", True):
        label = "" if edition_key == "global" else edition.get("name", "").replace("press.autos", "").strip()
        card = render_card(draft.get("title", ""), facts.get("category") or "", draft.get("card_stats") or [], label)
        result["featured"] = (card, "image/jpeg", f"{slug}-card.jpg", alt, "", "card")
    return result


def image_block(media: dict, caption: str) -> str:
    mid, src = media.get("id"), media.get("source_url", "")
    alt = (media.get("alt_text") or "").replace('"', "&quot;")
    cap = f'<figcaption class="wp-element-caption">{caption}</figcaption>' if caption else ""
    return (f'<!-- wp:image {{"id":{mid},"sizeSlug":"large"}} -->\n'
            f'<figure class="wp-block-image size-large"><img src="{src}" alt="{alt}" class="wp-image-{mid}"/>{cap}</figure>\n'
            f'<!-- /wp:image -->')


def insert_after_paragraph(html: str, block: str, n: int = 2) -> str:
    marker = "<!-- /wp:paragraph -->"
    pos = -1
    for _ in range(n):
        pos = html.find(marker, pos + 1)
        if pos < 0:
            return html + "\n\n" + block
    end = pos + len(marker)
    return html[:end] + "\n\n" + block + html[end:]
