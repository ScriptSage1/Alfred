"""Search pipeline tests: extraction, OCR, incremental indexing, hybrid ranking, path safety.

Run from the repository folder:  .venv\\Scripts\\python -m unittest discover -s tests -v
No Ollama is needed: semantic search uses a small deterministic fake embedder.
"""
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alfred import config, extract, paths  # noqa: E402
from alfred.index import Index  # noqa: E402

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def make_docx(path, paragraphs, table=()):
    body = ''.join(f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in paragraphs)
    body += ''.join('<w:tbl><w:tr>' + ''.join(f'<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>' for c in row) + '</w:tr></w:tbl>'
                    for row in table)
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('word/document.xml', f'<w:document {W}><w:body>{body}</w:body></w:document>')


def make_pptx(path, slides):
    with zipfile.ZipFile(path, 'w') as z:
        ids = ''.join(f'<p:sldId id="{256 + n}" r:id="rId{n}"/>' for n in range(1, len(slides) + 1))
        z.writestr('ppt/presentation.xml', f'<p:presentation {P} {R}><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>')
        rels = ''.join(f'<Relationship Id="rId{n}" Type="{REL}/slide" Target="slides/slide{n}.xml"/>' for n in range(1, len(slides) + 1))
        z.writestr('ppt/_rels/presentation.xml.rels',
                   f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        for n, text in enumerate(slides, 1):
            z.writestr(f'ppt/slides/slide{n}.xml', f'<p:sld {P} {A}><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t>'
                                                   '</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')


def make_xlsx(path, sheets):
    with zipfile.ZipFile(path, 'w') as z:
        entries = ''.join(f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>' for n, name in enumerate(sheets, 1))
        z.writestr('xl/workbook.xml', f'<workbook {S} {R}><sheets>{entries}</sheets></workbook>')
        rels = ''.join(f'<Relationship Id="rId{n}" Type="{REL}/worksheet" Target="worksheets/sheet{n}.xml"/>' for n in range(1, len(sheets) + 1))
        z.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        strings = [cell for rows in sheets.values() for row in rows for cell in row if isinstance(cell, str)]
        z.writestr('xl/sharedStrings.xml', f'<sst {S}>' + ''.join(f'<si><t>{s}</t></si>' for s in strings) + '</sst>')
        for n, rows in enumerate(sheets.values(), 1):
            xml = ''
            for r, row in enumerate(rows, 1):
                cells = ''.join(f'<c r="{chr(65 + c)}{r}" t="s"><v>{strings.index(v)}</v></c>' if isinstance(v, str)
                                else f'<c r="{chr(65 + c)}{r}"><v>{v}</v></c>' for c, v in enumerate(row))
                xml += f'<row r="{r}">{cells}</row>'
            z.writestr(f'xl/worksheets/sheet{n}.xml', f'<worksheet {S}><sheetData>{xml}</sheetData></worksheet>')


def make_text_image(path, lines):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (1000, 90 + 80 * len(lines)), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', 44) if os.path.exists('C:/Windows/Fonts/segoeui.ttf') else ImageFont.load_default(44)
    for n, line in enumerate(lines):
        draw.text((40, 40 + 80 * n), line, fill='black', font=font)
    image.save(path)


class FakeEmbedder:
    """Bag-of-concepts vectors: words sharing a concept land close together."""
    CONCEPTS = [{'car', 'vehicle', 'automobile', 'engine', 'drive'}, {'money', 'budget', 'cost', 'invoice', 'expense', 'price'},
                {'dog', 'puppy', 'animal', 'pet'}, {'holiday', 'vacation', 'trip', 'travel', 'flight'}]
    batch = 8

    def __init__(self, name='fake'):
        self.name = name

    def signature(self):
        return f'fake:{self.name}'

    def _vector(self, text):
        words = set(t.strip('.,:;!?').lower() for t in text.split())
        return [float(len(words & c)) + 0.01 for c in self.CONCEPTS]

    def embed_documents(self, texts, titles=None):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.home = self.dir / 'home'
        self.folder = self.dir / 'Tracked'
        self.folder.mkdir()
        self.settings = config.load(self.home)
        self.settings['tracked_folders'] = [str(self.folder)]
        self.index = Index(self.settings)

    def tearDown(self):
        self.tmp.cleanup()


class ExtractionTests(Base):
    def test_text_lines(self):
        file = self.folder / 'notes.txt'
        file.write_text('first line\nsecond line about rockets\n', encoding='utf-8')
        [(kind, text, loc)] = extract.extract(file)
        self.assertIn('rockets', text)
        self.assertEqual((loc['line'], loc['end_line']), (1, 2))

    def test_docx_paragraphs_and_tables(self):
        file = self.folder / 'report.docx'
        make_docx(file, ['Project Falcon kickoff', 'Budget approved by finance'], table=[['Owner', 'Riya']])
        passages = extract.extract(file)
        text = '\n'.join(p[1] for p in passages)
        self.assertIn('Project Falcon kickoff', text)
        self.assertIn('Owner | Riya', text)
        self.assertEqual(passages[0][2]['paragraph'], 1)

    def test_pptx_one_passage_per_slide(self):
        file = self.folder / 'deck.pptx'
        make_pptx(file, ['Welcome to the roadmap', 'Launch dates and milestones'])
        passages = extract.extract(file)
        self.assertEqual([p[2]['slide'] for p in passages], [1, 2])
        self.assertIn('milestones', passages[1][1])

    def test_xlsx_rows_per_sheet(self):
        file = self.folder / 'budget.xlsx'
        make_xlsx(file, {'Costs': [['Item', 'Amount'], ['Laptop', 1200]], 'Notes': [['Reviewed by audit']]})
        passages = extract.extract(file)
        self.assertEqual({p[2]['sheet'] for p in passages}, {'Costs', 'Notes'})
        costs = next(p for p in passages if p[2]['sheet'] == 'Costs')
        self.assertIn('Laptop | 1200', costs[1])
        self.assertEqual(extract.location_label(costs[0], costs[2]), 'Costs, rows 1–2')

    def test_legacy_and_unknown_formats_are_reported(self):
        for name in ('old.doc', 'tool.exe'):
            (self.folder / name).write_bytes(b'x')
            with self.assertRaises(extract.Unsupported):
                extract.extract(self.folder / name)

    def test_ocr_reads_text_in_image(self):
        file = self.folder / 'receipt.png'
        make_text_image(file, ['Invoice total 4250 rupees', 'Delivered to Hyderabad'])
        [(kind, text, loc)] = extract.extract(file)
        self.assertEqual(kind, 'ocr')
        self.assertTrue(loc['ocr'])
        self.assertIn('hyderabad', text.lower())
        self.assertIn('4250', text)


    def test_pdf_text_pages_and_scanned_pages(self):
        import pymupdf
        picture = self.folder / 'scan.png'
        make_text_image(picture, ['Scanned warranty card for the fridge'])
        file = self.folder / 'manual.pdf'
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), 'Chapter one explains the cooling settings in detail.', fontsize=12)
            page = document.new_page()
            page.insert_image(page.rect, filename=str(picture))
            document.save(file)
        passages = extract.extract(file)
        self.assertEqual(passages[0][2], {'page': 1})
        self.assertIn('cooling', passages[0][1])
        scanned = next(p for p in passages if p[2]['page'] == 2)
        self.assertTrue(scanned[2]['ocr'])
        self.assertIn('warranty', scanned[1].lower())

    def test_video_text_is_read_with_timestamps(self):
        import subprocess
        import imageio_ffmpeg
        frames = self.dir / 'frames'
        frames.mkdir()
        slides = [(0, 'Welcome to Project Nebula'), (12, 'Budget review approved today'), (24, 'Deadline moved to December')]
        for second in range(36):
            make_text_image(frames / f'f{second:02d}.png', [[t for start, t in slides if start <= second][-1]])
        file = self.folder / 'briefing.mp4'
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-loglevel', 'error', '-framerate', '1', '-i', str(frames / 'f%02d.png'),
                        '-r', '25', '-pix_fmt', 'yuv420p', '-vf', 'scale=1000:-2', str(file)], check=True)
        passages = extract.extract(file, video_interval=10)
        found = {text.lower(): loc['seconds'] for _, text, loc in passages}
        for start, text in slides:
            match = next((seconds for found_text, seconds in found.items() if text.split()[0].lower() in found_text), None)
            self.assertIsNotNone(match, text)
            self.assertTrue(start <= match < start + 12, (text, match))
        self.assertEqual(extract.location_label('video', {'seconds': 83, 'ocr': True}), 'at 01:23 (text read from image)')


class IndexTests(Base):
    def test_incremental_update_and_removal(self):
        a, b = self.folder / 'a.txt', self.folder / 'b.md'
        a.write_text('the zephyr project timeline', encoding='utf-8')
        b.write_text('quarterly numbers', encoding='utf-8')
        first = self.index.update()
        self.assertEqual((first['indexed'], first['unchanged']), (2, 0))
        second = self.index.update()
        self.assertEqual((second['indexed'], second['unchanged']), (0, 2))
        time.sleep(0.02)
        a.write_text('the zephyr project timeline, revised with kestrel', encoding='utf-8')
        os.utime(a, None)
        third = self.index.update()
        self.assertEqual(third['indexed'], 1)
        self.assertTrue(self.index.search('kestrel'))
        b.unlink()
        fourth = self.index.update()
        self.assertEqual(fourth['removed'], 1)
        self.assertFalse(self.index.search('quarterly'))

    def test_untracking_a_folder_removes_its_files(self):
        (self.folder / 'a.txt').write_text('marigold seeds', encoding='utf-8')
        self.index.update()
        self.assertTrue(self.index.search('marigold'))
        self.settings['tracked_folders'] = []
        self.index.remove_root(self.folder)
        self.assertFalse(self.index.search('marigold'))
        self.assertEqual(self.index.status()['files'], 0)

    def test_multiple_folders_and_display(self):
        other = self.dir / 'Second'
        (other / 'sub').mkdir(parents=True)
        (other / 'sub' / 'x.txt').write_text('aurora borealis photos', encoding='utf-8')
        (self.folder / 'y.txt').write_text('aurora sightings log', encoding='utf-8')
        self.settings['tracked_folders'].append(str(other))
        self.index.update()
        results = self.index.search('aurora')
        self.assertEqual(len(results), 2)
        folders = {r['folder'] for r in results}
        self.assertIn('Second › sub', folders)
        self.assertEqual(set(self.index.folder_stats()), {str(self.folder), str(other)})

    def test_hybrid_ranks_semantic_matches(self):
        (self.folder / 'cars.txt').write_text('notes on the automobile engine and how to drive it', encoding='utf-8')
        (self.folder / 'money.txt').write_text('invoice and expense records for the budget', encoding='utf-8')
        (self.folder / 'pets.txt').write_text('my puppy is a happy animal', encoding='utf-8')
        embedder = FakeEmbedder()
        summary = self.index.update(embedder=embedder)
        self.assertEqual(summary['embedded'], 3)
        # No shared words with "vehicle", so only meaning can find it.
        results = self.index.search('vehicle', embedder=embedder)
        self.assertEqual(results[0]['filename'], 'cars.txt')
        self.assertIsNotNone(results[0]['semantic_rank'])
        # Keyword + meaning agree: the file with the exact word wins.
        hybrid = self.index.search('budget cost', embedder=embedder)
        self.assertEqual(hybrid[0]['filename'], 'money.txt')
        self.assertIsNotNone(hybrid[0]['keyword_rank'])
        # Keyword-only still works without embeddings.
        self.assertEqual(self.index.search('puppy')[0]['filename'], 'pets.txt')

    def test_model_change_reembeds(self):
        (self.folder / 'trip.txt').write_text('vacation flight plans', encoding='utf-8')
        self.index.update(embedder=FakeEmbedder('one'))
        summary = self.index.update(embedder=FakeEmbedder('two'))
        self.assertEqual(summary['embedded'], 1)

    def test_skips_secrets_hidden_and_dot_folders(self):
        (self.folder / '.env').write_text('API_KEY=abc', encoding='utf-8')
        (self.folder / 'passwords.txt').write_text('hunter2', encoding='utf-8')
        (self.folder / 'id_rsa').write_text('-----BEGIN', encoding='utf-8')
        (self.folder / '.git').mkdir()
        (self.folder / '.git' / 'config.txt').write_text('remote', encoding='utf-8')
        (self.folder / 'fine.txt').write_text('ordinary words', encoding='utf-8')
        self.index.update()
        con_files = [r['filename'] for r in self.index.search('hunter2 remote API_KEY BEGIN ordinary')]
        self.assertEqual(con_files, ['fine.txt'])

    def test_excerpt_and_resolve_stay_inside_tracked_folders(self):
        (self.folder / 'doc.txt').write_text('lighthouse keeper diary', encoding='utf-8')
        self.index.update()
        result = self.index.search('lighthouse')[0]
        self.assertIn('lighthouse', self.index.read_excerpt(result['path'], segment_id=result['segment_id'])['excerpt'])
        outside = self.dir / 'outside.txt'
        outside.write_text('secret', encoding='utf-8')
        with self.assertRaises(paths.PathError):
            self.index.resolve(str(outside))


class FakeVision:
    """Stands in for the Ollama vision model; records what it was asked to describe."""
    model = 'fake-vision'

    def __init__(self, name='v1'):
        self.name, self.calls = name, []

    def signature(self):
        return f'fake:{self.name}'

    def describe(self, image):
        self.calls.append(image.size)
        return {'caption': 'A child sits on a wooden park bench in autumn.',
                'description': 'A small child in a dark sweater and jeans sits on a bench. Golden trees fill the background.',
                'tags': ['child', 'bench', 'park', 'autumn', 'trees']}


class VisionTests(Base):
    def setUp(self):
        super().setUp()
        self.settings['vision_enabled'] = True
        from PIL import Image
        Image.new('RGB', (640, 480), (200, 150, 60)).save(self.folder / 'IMG_1234.jpg')

    def test_parse_cleans_model_json(self):
        from alfred import ai
        parsed = ai.Vision.parse('{"caption": " A dog  runs. ", "description": "On grass.", "tags": ["Dog", "dog", "#Grass", "", "x"]}')
        self.assertEqual(parsed, {'caption': 'A dog runs.', 'description': 'On grass.', 'tags': ['dog', 'grass', 'x']})
        with self.assertRaises(ai.ProviderError):
            ai.Vision.parse('not json')

    def test_description_is_indexed_once_and_searchable(self):
        vision = FakeVision()
        summary = self.index.update(embedder=FakeEmbedder(), vision=vision)
        self.assertEqual((summary['described'], len(vision.calls)), (1, 1))
        result = self.index.search('kid on a bench in autumn')[0]
        self.assertEqual((result['filename'], result['location']), ('IMG_1234.jpg', 'AI description'))
        excerpt = self.index.read_excerpt(result['path'])
        self.assertEqual(excerpt['vision']['tags'], ['child', 'bench', 'park', 'autumn', 'trees'])
        self.assertEqual(excerpt['vision']['model'], 'fake-vision')
        self.assertFalse(excerpt['description_pending'])
        self.index.update(vision=vision)
        self.assertEqual(len(vision.calls), 1)  # cached: not described again

    def test_changed_picture_or_new_model_is_described_again(self):
        vision = FakeVision()
        self.index.update(vision=vision)
        time.sleep(0.02)
        from PIL import Image
        Image.new('RGB', (640, 480), (20, 50, 160)).save(self.folder / 'IMG_1234.jpg')
        self.index.update(vision=vision)
        self.assertEqual(len(vision.calls), 2)
        other = FakeVision('v2')
        self.index.update(vision=other)
        self.assertEqual(len(other.calls), 1)
        self.assertEqual(len([r for r in self.index.search('bench') if r['kind'] == 'vision']), 1)  # old one replaced

    def test_unavailable_model_falls_back_quietly(self):
        from alfred import ai

        class Offline(FakeVision):
            def describe(self, image):
                raise ai.ProviderError('Cannot reach 127.0.0.1:11434: refused')
        summary = self.index.update(vision=Offline())
        self.assertEqual(summary['described'], 0)
        self.assertTrue(any('paused' in w for w in summary['warnings']))
        path = str(self.folder / 'IMG_1234.jpg')
        self.assertTrue(self.index.read_excerpt(path)['description_pending'])
        self.assertTrue(self.index.search('IMG_1234'))  # still findable by name


class PathSafetyTests(unittest.TestCase):
    def test_refuses_drive_root_profile_and_system_folders(self):
        home = Path.home()
        self.assertIsNotNone(paths.forbidden_reason(Path(home.anchor)))
        self.assertIsNotNone(paths.forbidden_reason(home))
        self.assertIsNotNone(paths.forbidden_reason(home / 'AppData'))
        if os.environ.get('SystemRoot'):
            self.assertIsNotNone(paths.forbidden_reason(Path(os.environ['SystemRoot'])))

    def test_allows_ordinary_folders(self):
        with tempfile.TemporaryDirectory(dir=Path.home() if (Path.home() / 'Documents').is_dir() else None) as folder:
            if not paths.is_within(Path.home() / 'AppData', folder):
                self.assertIsNone(paths.forbidden_reason(folder))
        for name in ('Downloads', 'Documents', 'Pictures', 'Desktop'):
            if (Path.home() / name).is_dir():
                self.assertIsNone(paths.forbidden_reason(Path.home() / name), name)

    def test_secret_names(self):
        for name in ('.env', 'id_ed25519', 'server.pem', 'my-passwords.xlsx', 'aws_credentials.txt', 'vault.kdbx'):
            self.assertTrue(paths.looks_secret(name), name)
        for name in ('report.docx', 'holiday.jpg', 'tokyo-trip.txt'):
            self.assertFalse(paths.looks_secret(name), name)


if __name__ == '__main__':
    unittest.main()
