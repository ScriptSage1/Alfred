"""Turn files into searchable passages with real locations for citations.

Each extractor returns a list of ``(kind, text, location)`` passages:

* text files: line ranges              {'line': 1, 'end_line': 40}
* DOCX: paragraph ranges               {'paragraph': 1, 'end_paragraph': 12}
* PPTX: one passage per slide (+notes) {'slide': 3, 'title': 'Budget'}
* XLSX: row ranges per sheet           {'sheet': 'Budget', 'row': 1, 'end_row': 30, 'columns': 'Item | Cost'}
* PDF: per page; OCR for scanned pages {'page': 2, 'ocr': True}
  tables on a page are read row by row  {'page': 40, 'section': 'THIRD YEAR SEMESTER-II', 'columns': 'Course Code | …'}
* images: OCR text                     {'ocr': True}
* videos: OCR text of sampled frames   {'seconds': 83.0, 'ocr': True}

Passages of documents with headings (DOCX heading styles or bold one-line paragraphs, Markdown
``#`` lines) also carry the heading they sit under, ``{'section': '1.3 Scope of testing'}``, and a
new heading starts a new passage. Office files are read straight from their XML parts, so no
Office install is needed.
"""
import html
import io
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from . import ocr
from .console import hidden

TEXT_SUFFIXES = {'.txt', '.md', '.markdown', '.csv', '.tsv', '.log', '.rst', '.json', '.yaml', '.yml', '.html', '.htm', '.xml', '.ini', '.cfg', '.toml'}
OFFICE_SUFFIXES = {'.docx', '.pptx', '.xlsx'}
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff'}
VIDEO_SUFFIXES = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.wmv', '.mpeg', '.mpg'}
SUPPORTED = TEXT_SUFFIXES | OFFICE_SUFFIXES | {'.pdf'} | IMAGE_SUFFIXES | VIDEO_SUFFIXES
LEGACY_OFFICE = {'.doc': 'DOCX', '.ppt': 'PPTX', '.xls': 'XLSX'}

CHUNK_CHARS = 1400
MAX_TEXT_BYTES = 8 * 1024 ** 2
MAX_DOCUMENT_BYTES = 100 * 1024 ** 2
MAX_VIDEO_BYTES = 4 * 1024 ** 3
MAX_VIDEO_FRAMES = 120
MAX_ZIP_PART = 64 * 1024 ** 2   # guards against zip bombs posing as Office files
NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      's': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'rel': 'http://schemas.openxmlformats.org/package/2006/relationships'}


class Unsupported(ValueError):
    """The file type can't be searched (the message says why)."""


def extract(path, ocr_enabled=True, video_interval=10, progress=None, cancel=None, pdf_tables=True):
    path = Path(path)
    suffix = path.suffix.lower()
    size = path.stat().st_size
    if suffix in LEGACY_OFFICE:
        raise Unsupported(f'Old {suffix} format — save it as {LEGACY_OFFICE[suffix]} to make it searchable')
    if suffix not in SUPPORTED:
        raise Unsupported(f'{suffix or "Files without an extension"} can’t be searched')
    if suffix in IMAGE_SUFFIXES or suffix in VIDEO_SUFFIXES:
        if not ocr_enabled:
            raise Unsupported('Turn on “Read text in images and videos” to search this file')
        if suffix in VIDEO_SUFFIXES:
            if size > MAX_VIDEO_BYTES:
                raise Unsupported('Video larger than 4 GB')
            return video(path, video_interval, progress, cancel)
        return image(path)
    if size > (MAX_TEXT_BYTES if suffix in TEXT_SUFFIXES else MAX_DOCUMENT_BYTES):
        raise Unsupported('File too large to index')
    if suffix in TEXT_SUFFIXES:
        return text_file(path)
    if suffix == '.pdf':
        return pdf(path, ocr_enabled, pdf_tables)
    return {'.docx': docx, '.pptx': pptx, '.xlsx': xlsx}[suffix](path)


# ------------------------------------------------------------------ helpers
def clean(text):
    text = text.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t ]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def meaningful(text):
    """OCR text worth indexing: at least three real words (drops stray characters read from photos)."""
    return len(re.findall(r'[^\W\d_]{3,}', text)) >= 3


def chunk_units(units, kind, start_key, end_key, extra=None, headings=None):
    """Group numbered units (lines, paragraphs, rows) into ~CHUNK_CHARS passages.

    ``headings`` maps the numbers of units that are headings to (text, level 1 or 2): a heading starts
    a new passage (unless the current one is still short), and each passage records the heading it
    sits under ('Week 9 › Aim' for a sub-heading)."""
    headings = headings or {}
    passages, buffer, first, last, section, opened, top = [], [], None, None, None, None, None

    def flush(text, start, end):
        loc = dict(extra or {}, **{start_key: start, end_key: end})
        if opened:
            loc['section'] = opened
        passages.append((kind, text, loc))

    for number, text in units:
        if not text.strip():
            continue
        if number in headings:
            if buffer and sum(len(t) + 1 for t in buffer) > CHUNK_CHARS * 0.35:
                flush('\n'.join(buffer), first, last)
                buffer, first = [], None
            heading, level = headings[number]
            heading = ' '.join(heading.split())
            if level <= 1:
                top = section = heading[:120]
            else:
                parent = top if not top or len(top) <= 60 else top[:60].rsplit(' ', 1)[0] + '…'
                section = (f'{parent} › {heading}' if parent else heading)[:120]
        if buffer and sum(len(t) + 1 for t in buffer) + len(text) > CHUNK_CHARS:
            flush('\n'.join(buffer), first, last)
            buffer, first = [], None
        if not buffer:
            opened = section
        while len(text) > CHUNK_CHARS * 2:  # one huge line: split it on its own
            flush(text[:CHUNK_CHARS], number, number)
            text = text[CHUNK_CHARS:]
        buffer.append(text)
        first = number if first is None else first
        last = number
    if buffer:
        flush('\n'.join(buffer), first, last)
    return passages


def decode(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16', 'replace')
    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', 'replace')


def strip_markup(text):
    text = re.sub(r'(?is)<(script|style)\b.*?</\1>', ' ', text)
    text = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h\d|tr)>', '\n', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    return html.unescape(text)


def read_part(archive, name):
    info = archive.getinfo(name)
    if info.file_size > MAX_ZIP_PART:
        raise ValueError(f'{name} is unreasonably large')
    return ElementTree.fromstring(archive.read(info))


def open_office(path):
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError('Not a valid Office file (it may be password-protected)') from None


def natural(name):
    return [int(part) if part.isdigit() else part for part in re.split(r'(\d+)', name)]


# ---------------------------------------------------------------- documents
def text_file(path):
    text = decode(path.read_bytes())
    if path.suffix.lower() in ('.html', '.htm', '.xml'):
        text = strip_markup(text)
    lines = [(number, line.rstrip()) for number, line in enumerate(text.replace('\r\n', '\n').split('\n'), 1)]
    headings = {}
    if path.suffix.lower() in ('.md', '.markdown', '.txt'):
        for number, line in lines:
            match = re.match(r'(#{1,6})\s+(.+?)\s*#*$', line)
            if match:
                headings[number] = (match.group(2), len(match.group(1)))
    return chunk_units(lines, 'text', 'line', 'end_line', headings=headings)


def docx(path):
    with open_office(path) as archive:
        root = read_part(archive, 'word/document.xml')
    paragraphs, headings = [], {}
    body = root.find('w:body', NS)
    number = 0
    for node in (body if body is not None else []):
        tag = node.tag.split('}')[-1]
        if tag == 'p':
            number += 1
            text = _docx_text(node)
            paragraphs.append((number, text))
            level = _docx_heading(node, text)
            if level:
                headings[number] = (text, level)
        elif tag == 'tbl':
            for row in node.iter(f'{{{NS["w"]}}}tr'):
                number += 1
                cells = [_docx_text(cell) for cell in row.iter(f'{{{NS["w"]}}}tc')]
                paragraphs.append((number, ' | '.join(c for c in cells if c)))
    return chunk_units(paragraphs, 'text', 'paragraph', 'end_paragraph', headings=headings)


def _docx_heading(node, text):
    """Heading level (1 = top, 2 = below it) of a heading paragraph, else 0. Headings use a heading
    style (Heading 1–9, Title) or are short one-line paragraphs that are bold throughout."""
    if not text or len(text) > 90 or '\n' in text:
        return 0
    style = node.find('w:pPr/w:pStyle', NS)
    name = (style.get(f'{{{NS["w"]}}}val') if style is not None else '') or ''
    styled = re.match(r'(?i)(heading\s*(\d)|title|subtitle)', name)
    if styled:
        return 1 if name.lower().startswith('title') or styled.group(2) == '1' else 2
    if node.find('w:pPr/w:outlineLvl', NS) is not None:
        return 2
    if text.rstrip()[-1:] in '.,;:!?':
        return 0
    runs = [run for run in node.iter(f'{{{NS["w"]}}}r') if ''.join(t.text or '' for t in run.iter(f'{{{NS["w"]}}}t')).strip()]
    if not runs or not all(_bold(run) for run in runs):
        return 0
    return 1 if re.match(r'(\d+|[IVX]+)\.\s+\S', text) or re.match(r'(?i)(week|chapter|part|unit|module)\s+\d', text) else 2


def _bold(run):
    flag = run.find('w:rPr/w:b', NS)
    return flag is not None and flag.get(f'{{{NS["w"]}}}val', 'true') not in ('0', 'false')


def _docx_text(node):
    parts = []
    for item in node.iter():
        tag = item.tag.split('}')[-1]
        if tag == 't' and item.text:
            parts.append(item.text)
        elif tag == 'tab':
            parts.append('\t')
        elif tag in ('br', 'cr'):
            parts.append('\n')
    return clean(''.join(parts))


def pptx(path):
    passages = []
    with open_office(path) as archive:
        names = set(archive.namelist())
        slides = sorted((n for n in names if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)), key=natural)
        order = _presentation_order(archive, names) or slides
        for number, name in enumerate(order, 1):
            if name not in names:
                continue
            slide = read_part(archive, name)
            texts = [_paragraphs(slide)]
            notes = _slide_notes(archive, names, name)
            if notes:
                texts.append('Speaker notes: ' + notes)
            text = clean('\n'.join(t for t in texts if t))
            if text:
                loc = {'slide': number}
                first_line = texts[0].split('\n')[0]
                title = _slide_title(slide) or (first_line if len(first_line) <= 80 else '')
                if title.strip():
                    loc['title'] = ' '.join(title.split())[:120]
                passages.append(('slide', text, loc))
    return passages


def _slide_title(slide):
    """Text of the slide's title placeholder, if it has one."""
    p = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
    for shape in slide.iter(f'{p}sp'):
        placeholder = shape.find(f'{p}nvSpPr/{p}nvPr/{p}ph')
        if placeholder is not None and placeholder.get('type') in ('title', 'ctrTitle'):
            body = shape.find(f'{p}txBody')
            return _paragraphs(body).replace('\n', ' ') if body is not None else ''
    return ''


def _paragraphs(root):
    lines = []
    for paragraph in root.iter(f'{{{NS["a"]}}}p'):
        line = ''.join(t.text or '' for t in paragraph.iter(f'{{{NS["a"]}}}t'))
        if line.strip():
            lines.append(line.strip())
    return '\n'.join(lines)


def _rels(archive, names, part):
    folder, file = part.rsplit('/', 1)
    rels = f'{folder}/_rels/{file}.rels'
    if rels not in names:
        return {}
    result = {}
    for rel in read_part(archive, rels).iter(f'{{{NS["rel"]}}}Relationship'):
        target = rel.get('Target', '')
        resolved = os.path.normpath(os.path.join(folder, target)).replace('\\', '/') if not target.startswith('/') else target.lstrip('/')
        result[rel.get('Id')] = (rel.get('Type', ''), resolved)
    return result


def _presentation_order(archive, names):
    if 'ppt/presentation.xml' not in names:
        return []
    rels = _rels(archive, names, 'ppt/presentation.xml')
    root = read_part(archive, 'ppt/presentation.xml')
    order = []
    for slide in root.iter('{http://schemas.openxmlformats.org/presentationml/2006/main}sldId'):
        target = rels.get(slide.get(f'{{{NS["r"]}}}id'))
        if target:
            order.append(target[1])
    return order


def _slide_notes(archive, names, slide):
    for kind, target in _rels(archive, names, slide).values():
        if kind.endswith('/notesSlide') and target in names:
            text = _paragraphs(read_part(archive, target))
            return re.sub(r'^\d+$', '', text, flags=re.M).strip()  # drop the slide-number placeholder
    return ''


def xlsx(path):
    passages = []
    with open_office(path) as archive:
        names = set(archive.namelist())
        shared = []
        if 'xl/sharedStrings.xml' in names:
            for item in read_part(archive, 'xl/sharedStrings.xml').iter(f'{{{NS["s"]}}}si'):
                shared.append(''.join(t.text or '' for t in item.iter(f'{{{NS["s"]}}}t')))
        rels = _rels(archive, names, 'xl/workbook.xml')
        workbook = read_part(archive, 'xl/workbook.xml')
        for sheet in workbook.iter(f'{{{NS["s"]}}}sheet'):
            title = sheet.get('name', 'Sheet')
            target = rels.get(sheet.get(f'{{{NS["r"]}}}id'), ('', ''))[1]
            if target not in names:
                continue
            rows = []
            for row in read_part(archive, target).iter(f'{{{NS["s"]}}}row'):
                values = []
                for cell in row.iter(f'{{{NS["s"]}}}c'):
                    value = _cell(cell, shared)
                    if value:
                        values.append(value)
                if values:
                    rows.append((int(row.get('r') or len(rows) + 1), ' | '.join(values)))
            extra = {'sheet': title}
            if len(rows) > 1:
                extra['columns'] = rows[0][1][:200]  # the header row: every passage of a long sheet says what its columns are
            passages += chunk_units(rows, 'sheet', 'row', 'end_row', extra)
    return passages


def _cell(cell, shared):
    kind = cell.get('t')
    if kind == 'inlineStr':
        return clean(''.join(t.text or '' for t in cell.iter(f'{{{NS["s"]}}}t')))
    value = cell.find('s:v', NS)
    if value is None or value.text is None:
        return ''
    if kind == 's':
        try:
            return clean(shared[int(value.text)])
        except (ValueError, IndexError):
            return ''
    if kind == 'b':
        return 'TRUE' if value.text == '1' else 'FALSE'
    return value.text.strip()


TABLE_WORKERS = max(1, min(8, (os.cpu_count() or 2) // 2))


def pdf(path, ocr_enabled=True, tables=True):
    """Passages of a PDF, page by page. With ``tables``, ruled tables are read row by row (slower: pages
    with a grid of lines are searched for tables, several at once in worker processes)."""
    import pymupdf
    passages = []
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError('PDF is password-protected')
        read = _table_pages(path, table_pages(document)) if tables else {}
        for number, page in enumerate(document, 1):
            if read.get(number):
                passages += read[number]
                continue
            text = clean(page.get_text('text'))
            if len(text) >= 20:
                passages += [('page', piece, {'page': number}) for piece in _split(text)]
            elif ocr_enabled:
                pixmap = page.get_pixmap(dpi=200)
                from PIL import Image
                image = Image.open(io.BytesIO(pixmap.tobytes('png')))
                text = clean(ocr.read_text(image))
                if meaningful(text):
                    passages += [('page', piece, {'page': number, 'ocr': True}) for piece in _split(text)]
    return passages


def _split(text):
    if len(text) <= CHUNK_CHARS * 1.5:
        return [text]
    return [text for _, text, _ in chunk_units(list(enumerate(text.split('\n'), 1)), 'page', 'a', 'b')]


# A PDF table read as plain text is one cell per line, so a heading like "No of Credits" ends up far from
# its numbers. Tables are read cell by cell instead and written one row per line, each value named by its
# column: "Course Code: 20CS32001 · Course Title: Internet of Things · … · No of Credits: 3".
def _has_grid(page):
    """Whether a page has enough horizontal and vertical lines to hold a ruled table. A border or a few rules
    don't count: on real documents, pages with tables have dozens of lines and pages without have a handful."""
    across = down = 0
    for drawing in page.get_drawings():
        for item in drawing['items']:
            if item[0] == 'l':
                start, end = item[1], item[2]
                across += abs(start.y - end.y) < 1
                down += abs(start.x - end.x) < 1
            elif item[0] == 're':
                box = item[1]
                across += 1 if box.height < 2 else 0 if box.width < 2 else 2
                down += 1 if box.width < 2 else 0 if box.height < 2 else 2
        if across >= 5 and down >= 5:
            return True
    return False


def table_pages(document):
    """Page numbers (from 1) that may hold a ruled table: a quick look at the drawings, no table search."""
    try:
        return [number for number, page in enumerate(document, 1) if _has_grid(page)]
    except Exception:
        return []


def _pdf_tables(page):
    """The real tables on a page."""
    try:
        found = page.find_tables().tables
    except Exception:  # a malformed page: read it as plain text
        return []
    return [t for t in found if t.row_count >= 3 and t.col_count >= 2]


def read_table_pages(path, numbers):
    """{page number: passages} for the pages among ``numbers`` that have tables (a worker process runs this)."""
    import pymupdf
    read = {}
    with pymupdf.open(path) as document:
        for number in numbers:
            page = document[number - 1]
            tables = _pdf_tables(page)
            if tables:
                read[number] = _pdf_table_page(page, number, tables)
    return read


def _table_pages(path, numbers):
    """read_table_pages for many pages, split across worker processes when it's worth starting them."""
    if len(numbers) < 8 or TABLE_WORKERS < 2:
        return read_table_pages(path, numbers)
    from concurrent.futures import ProcessPoolExecutor
    shares = [numbers[i::TABLE_WORKERS] for i in range(TABLE_WORKERS)]
    try:
        with ProcessPoolExecutor(TABLE_WORKERS) as pool:
            read = {}
            for part in pool.map(read_table_pages, [str(path)] * len(shares), shares):
                read.update(part)
            return read
    except Exception as error:  # no worker processes here (a locked-down PC): read them one by one
        import logging
        logging.getLogger('alfred').info('Reading PDF tables in one process (%s)', error)
        return read_table_pages(path, numbers)


def _cells(page, table):
    """Rows of cell texts (None for a cell merged into its neighbour), words spaced as on the page."""
    import pymupdf
    words = page.get_text('words')
    rows = []
    for row in table.rows:
        cells = []
        for box in row.cells:
            if box is None:
                cells.append(None)
                continue
            rect = pymupdf.Rect(box)
            inside = sorted((w for w in words if rect.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))),
                            key=lambda w: (w[5], w[6], w[7]))
            cells.append(' '.join(w[4] for w in inside).strip())
        rows.append(cells)
    width = max(len(r) for r in rows)
    return [r + [None] * (width - len(r)) for r in rows]


def table_lines(rows):
    """(columns, lines) for a table given as rows of cells: header rows become column names (a two-level
    header like "Periods per Week" over L, T, P names the columns L, T, P and lists the group), and
    every other row becomes one line of "column: value" pairs."""
    if not rows:
        return '', []
    width = len(rows[0])

    def label_like(row):
        cells = [' '.join(c.split()) for c in row if c and c.strip()]
        return len(cells) >= 2 and all(len(c) <= 60 and not (len(c) > 25 and c.endswith('.')) for c in cells)
    if not label_like(rows[0]):  # no header (a table continued from the previous page): plain rows
        return '', [' · '.join(' '.join(c.split()) for c in row if c and c.strip()) for row in rows if any(c and c.strip() for c in row)]
    heads = 1  # the first row is the header; rows under it without numbers are more header
    while heads < min(3, len(rows) - 1) and sum(1 for c in rows[heads] if c) >= 2 and not any(re.search(r'\d', c or '') for c in rows[heads]):
        heads += 1
    labels, groups = [], []
    for col in range(width):
        stack = [rows[h][col] for h in range(heads)]
        leaf = next((c for c in reversed(stack) if c), '')
        labels.append(' '.join(leaf.split()))
        parent = ''
        if heads > 1 and stack[-1]:  # the name came from a lower header row: find the group spanning it
            parent = next((rows[0][c] for c in range(col, -1, -1) if rows[0][c]), '') if not stack[0] else stack[0]
        parent = ' '.join(parent.split())
        if groups and groups[-1][0] == parent and parent:
            groups[-1][1].append(labels[-1])
        else:
            groups.append((parent, [labels[-1]]))
    columns = ' | '.join(f'{parent}: {", ".join(names)}' if parent and parent not in names else ' | '.join(n for n in names if n)
                         for parent, names in groups if any(names))
    lines = []
    for row in rows[heads:]:
        pairs = []
        for label, cell in zip(labels, row):
            cell = ' '.join((cell or '').split())
            if cell:
                pairs.append(f'{label}: {cell}' if label and label != cell else cell)
        if pairs:
            lines.append(' · '.join(pairs))
    return columns, lines


def _pdf_table_page(page, number, tables):
    """Passages for a page with tables: the text around them as usual, and each table as rows under its
    caption (the line just above it), split into passages that each repeat the caption and columns."""
    import pymupdf
    boxes = [pymupdf.Rect(t.bbox) for t in tables]
    items = []
    for x0, y0, x1, y1, text, *_ in page.get_text('blocks'):
        block = pymupdf.Rect(x0, y0, x1, y1)
        if not any(abs(block & box) >= 0.5 * max(abs(block), 1e-6) for box in boxes):
            items.append((y0, 'text', text))
    items += [(box.y0, 'table', table) for box, table in zip(boxes, tables)]
    items.sort(key=lambda item: item[0])
    passages, prose, caption = [], [], ''

    def flush_prose(force=False):
        text = clean('\n'.join(prose))
        if len(text) >= 200 or (force and len(text) >= 20):
            passages.extend(('page', piece, {'page': number}) for piece in _split(text))
            prose.clear()

    for _, what, value in items:
        if what == 'text':
            prose.append(value)
            last = [line.strip() for line in value.strip().split('\n') if re.search(r'[^\W\d_]{3,}', line)]
            caption = ' '.join(last[-1].split())[:120] if last else caption
            continue
        columns, lines = table_lines(_cells(page, value))
        if not lines:
            continue
        flush_prose()
        lead = ' '.join(' '.join(prose).split())  # a short page heading travels with the table's first passage
        if caption and lead.endswith(caption):
            lead = lead[:-len(caption)].strip()
        prose.clear()
        loc = {'page': number, 'columns': columns[:300]} if columns else {'page': number}
        if caption:
            loc['section'] = caption
        head = '\n'.join(part for part in (caption, f'Columns: {columns}' if columns else '') if part)
        piece = [lead] if lead else []
        for line in lines:
            if piece and sum(len(p) + 1 for p in piece) + len(line) > CHUNK_CHARS:
                passages.append(('page', '\n'.join([head] + piece if head else piece), dict(loc)))
                piece = []
            piece.append(line)
        if piece:
            passages.append(('page', '\n'.join([head] + piece if head else piece), dict(loc)))
        caption = ''
    tail = ' '.join(' '.join(prose).split())
    if passages and len(tail) < 200:  # a footer or a line after the last table: keep it with that table
        if tail:
            kind, text, loc = passages[-1]
            passages[-1] = (kind, f'{text}\n{tail}', loc)
    else:
        flush_prose(force=True)
    return passages


# -------------------------------------------------------------------- media
def image(path):
    text = clean(ocr.read_file(path))
    return [('ocr', text, {'ocr': True})] if meaningful(text) else []


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def video_duration(path):
    out = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(path)], capture_output=True, text=True, errors='replace',
                         timeout=60, **hidden())
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', out.stderr)
    if not match:
        raise ValueError('Not a readable video')
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def video_frame_png(path, seconds, width=660):
    """One frame (PNG bytes) for previews."""
    out = subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-ss', f'{max(0.0, float(seconds)):.2f}', '-i', str(path),
                          '-frames:v', '1', '-vf', f'scale={int(width)}:-2', '-f', 'image2pipe', '-vcodec', 'png', '-'],
                         capture_output=True, timeout=60, **hidden())
    if out.returncode or not out.stdout:
        raise ValueError('Could not read a frame from this video')
    return out.stdout


def video(path, interval=10, progress=None, cancel=None):
    """OCR one frame every ``interval`` seconds (stretched so long videos stay under
    MAX_VIDEO_FRAMES); near-identical frames and repeated text are read once."""
    from PIL import Image, ImageChops, ImageStat
    duration = video_duration(path)
    step = max(float(interval or 10), duration / MAX_VIDEO_FRAMES, 1.0)
    passages, previous_thumb, previous_text = [], None, ''
    with tempfile.TemporaryDirectory(prefix='alfred-frames-') as folder:
        subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-i', str(path), '-vf', f'fps=1/{step:.3f},scale=1280:-2',
                        '-frames:v', str(MAX_VIDEO_FRAMES), str(Path(folder) / 'f%04d.png')],
                       capture_output=True, timeout=max(120, int(duration * 2)), **hidden())
        frames = sorted(Path(folder).glob('f*.png'))
        for number, frame in enumerate(frames):
            if cancel is not None and cancel.is_set():
                break
            if progress:
                progress(number, len(frames))
            seconds = round(min(number * step + step / 2, duration), 1)  # the fps filter samples mid-interval
            with Image.open(frame) as picture:
                thumb = picture.convert('L').resize((160, 90))
                if previous_thumb is not None and ImageStat.Stat(ImageChops.difference(thumb, previous_thumb)).mean[0] < 0.6:
                    continue  # same picture as the last frame (a still slide, a paused screen)
                previous_thumb = thumb
                text = clean(ocr.read_text(picture.copy()))
            if meaningful(text) and _similar(text, previous_text) < 0.85:
                passages.append(('video', text, {'seconds': seconds, 'ocr': True}))
                previous_text = text
    return passages


def _similar(a, b):
    words_a, words_b = set(a.lower().split()), set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


# ---------------------------------------------------------------- citations
def clock(seconds):
    seconds = int(seconds or 0)
    return f'{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}' if seconds >= 3600 else f'{seconds // 60:02d}:{seconds % 60:02d}'


def location_label(kind, loc):
    if kind == 'vision' or loc.get('ai'):
        return 'AI description' + (f' at {clock(loc["seconds"])}' if 'seconds' in loc else '')
    ocr_note = ' (text read from image)' if loc.get('ocr') else ''
    if 'seconds' in loc:
        return f'at {clock(loc["seconds"])}{ocr_note}'
    if 'slide' in loc:
        return f'slide {loc["slide"]}'
    if 'sheet' in loc:
        rows = f'row {loc["row"]}' if loc['row'] == loc['end_row'] else f'rows {loc["row"]}–{loc["end_row"]}'
        return f'{loc["sheet"]}, {rows}'
    if 'page' in loc:
        return f'page {loc["page"]}{ocr_note}'
    if 'paragraph' in loc:
        return f'paragraph {loc["paragraph"]}' if loc['paragraph'] == loc['end_paragraph'] else f'paragraphs {loc["paragraph"]}–{loc["end_paragraph"]}'
    if 'line' in loc:
        return f'line {loc["line"]}' if loc['line'] == loc['end_line'] else f'lines {loc["line"]}–{loc["end_line"]}'
    if loc.get('name_only'):
        return 'file name'
    return 'image text' if loc.get('ocr') else 'text'


def short_location(kind, loc):
    """Compact place for a citation row: 'p. 3', 'slide 2', 'October, rows 2–3', 'lines 10–24', 'at 00:25'."""
    if kind == 'vision' or loc.get('ai'):
        return 'picture description' + (f' at {clock(loc["seconds"])}' if 'seconds' in loc else '')
    if 'seconds' in loc:
        return f'at {clock(loc["seconds"])}'
    if 'slide' in loc:
        return f'slide {loc["slide"]}'
    if 'sheet' in loc:
        rows = f'row {loc["row"]}' if loc['row'] == loc['end_row'] else f'rows {loc["row"]}–{loc["end_row"]}'
        return f'{loc["sheet"]}, {rows}'
    if 'page' in loc:
        return f'p. {loc["page"]}'
    if 'paragraph' in loc:
        return loc.get('section') or (f'¶ {loc["paragraph"]}' if loc['paragraph'] == loc['end_paragraph']
                                      else f'¶ {loc["paragraph"]}–{loc["end_paragraph"]}')
    if 'line' in loc:
        return f'line {loc["line"]}' if loc['line'] == loc['end_line'] else f'lines {loc["line"]}–{loc["end_line"]}'
    return 'text in picture' if loc.get('ocr') else 'text'


def chunk_header(path, folder, kind, loc):
    """A short, deterministic line saying where a passage comes from, put in front of it for search
    (keyword and meaning) so a chunk that never names its document or section can still be found:
    'travel-plan.docx · AlfredSamples · Day 2 · ¶ 5–9'."""
    parts = [Path(path).name]
    if folder:
        parts.append(folder)
    for key in ('title', 'section'):
        if loc.get(key):
            parts.append(str(loc[key]))
    if loc.get('columns'):
        parts.append(f'columns: {loc["columns"]}')
    place = short_location(kind, loc)
    if place not in parts:
        parts.append(place)
    if loc.get('ocr') and kind != 'ocr':
        parts.append('text read from the picture')
    return ' · '.join(parts)
