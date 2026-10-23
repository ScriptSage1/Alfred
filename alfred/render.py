"""Answers as Qt rich text: the Markdown models write (paragraphs, bullets, numbered lists, bold,
italics, code, headings, simple tables), with [n] citations drawn as small superscript chips that
link to ``cite:n`` and the "Beyond your files" part marked as general knowledge.

Pure string work (no Qt), so it is tested on its own and safe to call on every streamed token.
"""
import html
import re

from .rag import BEYOND_LINE

CITE = re.compile(r'\[(\d{1,3})\]')


def _inline(text, colors, known):
    out = html.escape(text, quote=False)
    out = re.sub(r'`([^`]+)`', lambda m: f"<code style='background-color:{colors['fill']};'>{m.group(1)}</code>", out)
    out = re.sub(r'\*\*(.+?)\*\*|__(.+?)__', lambda m: f'<b>{m.group(1) or m.group(2)}</b>', out)
    out = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])',
                 lambda m: f'<i>{m.group(1) or m.group(2)}</i>', out)

    def chip(match):
        number = int(match.group(1))
        if known is not None and number not in known:
            return ''
        return (f"<a href='cite:{number}' style='text-decoration:none; color:{colors['accent']};'>"
                f"<sup><span style='background-color:{colors['chip']};'>&nbsp;{number}&nbsp;</span></sup></a>")
    out = CITE.sub(chip, out)
    return re.sub(r'\s+(<a href=.cite:)', r'&nbsp;\1', out)  # a chip never starts a new line on its own


def _blocks(markdown, colors, known):
    lines = markdown.replace('\r\n', '\n').split('\n')
    parts, paragraph, items, kind, table, code = [], [], [], None, [], None

    def close_paragraph():
        if paragraph:
            parts.append(f"<p style='margin:0 0 7px 0;'>{'<br>'.join(_inline(l, colors, known) for l in paragraph)}</p>")
            paragraph.clear()

    def close_list():
        nonlocal kind
        if items:
            tag = 'ol' if kind == 'ol' else 'ul'
            body = ''.join(f"<li style='margin:0 0 3px 0;'>{_inline(t, colors, known)}</li>" for t in items)
            parts.append(f"<{tag} style='margin:0 0 7px -18px;'>{body}</{tag}>")
            items.clear()
        kind = None

    def close_table():
        if table:
            rows = [r for r in table if not re.fullmatch(r'\|?[\s:|-]+\|?', r)]
            cells = [[c.strip() for c in r.strip().strip('|').split('|')] for r in rows]
            body = ''.join('<tr>' + ''.join(f"<td style='padding:2px 10px 2px 0;'>{'<b>' if n == 0 else ''}{_inline(c, colors, known)}"
                                            f"{'</b>' if n == 0 else ''}</td>" for c in row) + '</tr>'
                           for n, row in enumerate(cells))
            parts.append(f"<table style='margin:0 0 7px 0;' cellspacing='0'>{body}</table>")
            table.clear()

    for line in lines:
        if code is not None:
            if line.strip().startswith('```'):
                parts.append(f"<pre style='background-color:{colors['fill']}; margin:0 0 7px 0;'>{html.escape(chr(10).join(code))}</pre>")
                code = None
            else:
                code.append(line)
            continue
        stripped = line.strip()
        if stripped.startswith('```'):
            close_paragraph(), close_list(), close_table()
            code = []
            continue
        if stripped.startswith('|') and stripped.count('|') >= 2:
            close_paragraph(), close_list()
            table.append(stripped)
            continue
        close_table()
        bullet = re.match(r'\s*[-*•]\s+(.*)', line)
        number = re.match(r'\s*\d{1,3}[.)]\s+(.*)', line)
        heading = re.match(r'\s*#{1,6}\s+(.*?)\s*#*$', line)
        if not stripped:
            close_paragraph(), close_list()
        elif heading:
            close_paragraph(), close_list()
            parts.append(f"<p style='margin:6px 0 5px 0;'><b>{_inline(heading.group(1), colors, known)}</b></p>")
        elif bullet or number:
            close_paragraph()
            wanted = 'ul' if bullet else 'ol'
            if kind and kind != wanted:
                close_list()
            kind = wanted
            items.append((bullet or number).group(1))
        elif items and line.startswith(('  ', '\t')):
            items[-1] += ' ' + stripped  # a wrapped list item
        else:
            close_list()
            paragraph.append(stripped)
    if code is not None:
        parts.append(f"<pre style='background-color:{colors['fill']};'>{html.escape(chr(10).join(code))}</pre>")
    close_paragraph(), close_list(), close_table()
    return ''.join(parts)


def answer_html(text, colors, known=None):
    """Rich text for an answer. ``colors``: hex/rgba strings for 'accent', 'chip', 'fill', 'tag',
    'secondary'. ``known``: citation numbers to draw (others are hidden), or None for all."""
    match = BEYOND_LINE.search(text or '')
    files, beyond = (text[:match.start()], text[match.end():]) if match else (text or '', None)
    out = _blocks(files.rstrip(), colors, known)
    if beyond is not None:
        beyond = re.sub(r'^[*_:\s]*\n?', '', beyond)  # the rest of the heading line (closing ** or a colon)
        tag = (f"<p style='margin:10px 0 5px 0;'><span style='background-color:{colors['tag']}; color:{colors['secondary']}; "
               f"font-size:8.5pt;'>&nbsp;GENERAL KNOWLEDGE&nbsp;</span><span style='color:{colors['secondary']}; font-size:9pt;'>"
               f"&nbsp;&nbsp;not from your files</span></p>")
        out += tag + _blocks(CITE.sub('', beyond), colors, set())
    return out
