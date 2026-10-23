"""Turn files into searchable passages with real locations for citations.

Each extractor returns a list of ``(kind, text, location)`` passages:

* text files: line ranges              {'line': 1, 'end_line': 40}
* DOCX: paragraph ranges               {'paragraph': 1, 'end_paragraph': 12}
* PPTX: one passage per slide (+notes) {'slide': 3}
* XLSX: row ranges per sheet           {'sheet': 'Budget', 'row': 1, 'end_row': 30}
* PDF: per page; OCR for scanned pages {'page': 2, 'ocr': True}
* images: OCR text                     {'ocr': True}
* videos: OCR text of sampled frames   {'seconds': 83.0, 'ocr': True}

Office files are read straight from their XML parts, so no Office install is needed.
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
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      's': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'rel': 'http://schemas.openxmlformats.org/package/2006/relationships'}


class Unsupported(ValueError):
    """The file type can't be searched (the message says why)."""


def extract(path, ocr_enabled=True, video_interval=10, progress=None, cancel=None):
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
        return pdf(path, ocr_enabled)
    return {'.docx': docx, '.pptx': pptx, '.xlsx': xlsx}[suffix](path)


# ------------------------------------------------------------------ helpers
def clean(text):
    text = text.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t ]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def meaningful(text):
    """OCR text worth indexing: at least three real words (drops stray characters read from photos)."""
    return len(re.findall(r'[^\W\d_]{3,}', text)) >= 3


def chunk_units(units, kind, start_key, end_key, extra=None):
    """Group numbered units (lines, paragraphs, rows) into ~CHUNK_CHARS passages."""
    passages, buffer, first, last = [], [], None, None
    for number, text in units:
        if not text.strip():
            continue
        if buffer and sum(len(t) + 1 for t in buffer) + len(text) > CHUNK_CHARS:
            passages.append((kind, '\n'.join(buffer), dict(extra or {}, **{start_key: first, end_key: last})))
            buffer, first = [], None
        while len(text) > CHUNK_CHARS * 2:  # one huge line: split it on its own
            passages.append((kind, text[:CHUNK_CHARS], dict(extra or {}, **{start_key: number, end_key: number})))
            text = text[CHUNK_CHARS:]
        buffer.append(text)
        first = number if first is None else first
        last = number
    if buffer:
        passages.append((kind, '\n'.join(buffer), dict(extra or {}, **{start_key: first, end_key: last})))
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
    return chunk_units(lines, 'text', 'line', 'end_line')


def docx(path):
    with open_office(path) as archive:
        root = read_part(archive, 'word/document.xml')
    paragraphs = []
    body = root.find('w:body', NS)
    number = 0
    for node in (body if body is not None else []):
        tag = node.tag.split('}')[-1]
        if tag == 'p':
            number += 1
            paragraphs.append((number, _docx_text(node)))
        elif tag == 'tbl':
            for row in node.iter(f'{{{NS["w"]}}}tr'):
                number += 1
                cells = [_docx_text(cell) for cell in row.iter(f'{{{NS["w"]}}}tc')]
                paragraphs.append((number, ' | '.join(c for c in cells if c)))
    return chunk_units(paragraphs, 'text', 'paragraph', 'end_paragraph')


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
            texts = [_paragraphs(read_part(archive, name))]
            notes = _slide_notes(archive, names, name)
            if notes:
                texts.append('Speaker notes: ' + notes)
            text = clean('\n'.join(t for t in texts if t))
            if text:
                passages.append(('slide', text, {'slide': number}))
    return passages


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
            passages += chunk_units(rows, 'sheet', 'row', 'end_row', {'sheet': title})
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


def pdf(path, ocr_enabled=True):
    import pymupdf
    passages = []
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError('PDF is password-protected')
        for number, page in enumerate(document, 1):
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


# -------------------------------------------------------------------- media
def image(path):
    text = clean(ocr.read_file(path))
    return [('ocr', text, {'ocr': True})] if meaningful(text) else []


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def video_duration(path):
    out = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(path)], capture_output=True, text=True, errors='replace',
                         creationflags=NO_WINDOW, timeout=60)
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', out.stderr)
    if not match:
        raise ValueError('Not a readable video')
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def video_frame_png(path, seconds, width=660):
    """One frame (PNG bytes) for previews."""
    out = subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-ss', f'{max(0.0, float(seconds)):.2f}', '-i', str(path),
                          '-frames:v', '1', '-vf', f'scale={int(width)}:-2', '-f', 'image2pipe', '-vcodec', 'png', '-'],
                         capture_output=True, creationflags=NO_WINDOW, timeout=60)
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
                       capture_output=True, creationflags=NO_WINDOW, timeout=max(120, int(duration * 2)))
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
