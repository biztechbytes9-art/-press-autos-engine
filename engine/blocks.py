"""Convert the writer's simple Markdown into WordPress block markup."""
import html
import re


def _inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", t)

    def link(m):
        url = m.group(2)
        if not re.match(r"^(https?://|/)", url):
            return m.group(1)
        return '<a href="%s">%s</a>' % (html.escape(url, quote=True), m.group(1))

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, t)


def to_blocks(md: str) -> str:
    out, para, items, rows = [], [], [], []
    ordered = [False]

    def flush_para():
        if para:
            out.append("<!-- wp:paragraph -->\n<p>" + _inline(" ".join(para)) + "</p>\n<!-- /wp:paragraph -->")
            para.clear()

    def flush_list():
        if items:
            tag = "ol" if ordered[0] else "ul"
            attr = ' {"ordered":true}' if ordered[0] else ""
            lis = "".join("<!-- wp:list-item -->\n<li>" + _inline(i) + "</li>\n<!-- /wp:list-item -->" for i in items)
            out.append(f'<!-- wp:list{attr} -->\n<{tag} class="wp-block-list">{lis}</{tag}>\n<!-- /wp:list -->')
            items.clear()

    def flush_table():
        if rows:
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            cells = [r for r in cells if not all(re.fullmatch(r":?-{3,}:?", c) for c in r)]
            if cells:
                head, body = cells[0], cells[1:]
                th = "".join("<th>" + _inline(c) + "</th>" for c in head)
                tb = "".join("<tr>" + "".join("<td>" + _inline(c) + "</td>" for c in r) + "</tr>" for r in body)
                out.append('<!-- wp:table -->\n<figure class="wp-block-table"><table><thead><tr>' + th +
                           "</tr></thead><tbody>" + tb + "</tbody></table></figure>\n<!-- /wp:table -->")
            rows.clear()

    def flush_all():
        flush_para(); flush_list(); flush_table()

    for raw in md.strip().splitlines():
        line = raw.rstrip()
        if line.startswith("# "):  # the title is set separately; never repeat it as h1
            flush_all()
            continue
        if line.startswith("## ") or line.startswith("### "):
            flush_all()
            level = 2 if line.startswith("## ") else 3
            attr = "" if level == 2 else ' {"level":3}'
            text = line[level + 1:].strip()
            out.append(f'<!-- wp:heading{attr} -->\n<h{level} class="wp-block-heading">{_inline(text)}</h{level}>\n<!-- /wp:heading -->')
        elif line.startswith("|"):
            flush_para(); flush_list()
            rows.append(line)
        elif re.match(r"^[-*] ", line):
            flush_para(); flush_table()
            if items and ordered[0]:
                flush_list()
            ordered[0] = False
            items.append(line[2:].strip())
        elif re.match(r"^\d+\. ", line):
            flush_para(); flush_table()
            if items and not ordered[0]:
                flush_list()
            ordered[0] = True
            items.append(re.sub(r"^\d+\. ", "", line).strip())
        elif not line.strip():
            flush_all()
        else:
            flush_list(); flush_table()
            para.append(line.strip())
    flush_all()
    return "\n\n".join(out)


def key_facts(facts: list) -> str:
    """'Key facts' summary box at the top of an article (styled by the press.autos Design plugin)."""
    items = [str(f).strip() for f in facts if str(f).strip()][:5]
    if len(items) < 2:
        return ""
    lis = "".join("<!-- wp:list-item -->\n<li>" + _inline(i) + "</li>\n<!-- /wp:list-item -->" for i in items)
    return ('<!-- wp:group {"className":"pa-keyfacts","layout":{"type":"constrained"}} -->\n'
            '<div class="wp-block-group pa-keyfacts">'
            '<!-- wp:paragraph {"className":"pa-keyfacts__title"} -->\n<p class="pa-keyfacts__title"><strong>Key facts</strong></p>\n<!-- /wp:paragraph -->\n'
            f'<!-- wp:list -->\n<ul class="wp-block-list">{lis}</ul>\n<!-- /wp:list -->'
            '</div>\n<!-- /wp:group -->\n\n')


def also_read(posts: list) -> str:
    """'Also read' internal links to related published articles."""
    items = "".join('<!-- wp:list-item -->\n<li><a href="%s">%s</a></li>\n<!-- /wp:list-item -->'
                    % (html.escape(p["link"], quote=True), html.escape(p["title"], quote=False)) for p in posts[:3])
    return ('<!-- wp:paragraph {"className":"pa-alsoread__title"} -->\n<p class="pa-alsoread__title"><strong>Also read</strong></p>\n'
            '<!-- /wp:paragraph -->\n\n'
            f'<!-- wp:list {{"className":"pa-alsoread"}} -->\n<ul class="wp-block-list pa-alsoread">{items}</ul>\n<!-- /wp:list -->')
