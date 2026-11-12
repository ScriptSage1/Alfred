"""Indexing in layers (M2): names first, text, meaning, OCR on worker threads, picture vectors, lazy captions,
background mode, the content cache and the upgrade of an older index.

Run from the repository folder:  .venv\\Scripts\\python -m unittest discover -s tests -v
No Ollama, GPU or real OCR: OCR, the embedder and the vision model are fakes.
"""
import json
import math
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alfred import extract, ocr, power  # noqa: E402
from alfred.index import Index, connect  # noqa: E402
from test_pipeline import Base, FakeEmbedder, FakeVision, make_docx, make_text_image  # noqa: E402


class FakeOcr:
    """Stands in for RapidOCR: text by file name (or for every PDF page), and a count of what it read."""

    def __init__(self, texts=None, page_text='Scanned warranty card for the kitchen fridge'):
        self.texts, self.page_text, self.files, self.pages = dict(texts or {}), page_text, [], 0
        self.lock = threading.Lock()

    def read_file(self, path):
        with self.lock:
            self.files.append(Path(path).name)
        return self.texts.get(Path(path).name, '')

    def read_text(self, image):
        with self.lock:
            self.pages += 1
        return self.page_text

    def patch(self, test):
        for name in ('read_file', 'read_text'):
            patcher = mock.patch.object(ocr, name, getattr(self, name))
            patcher.start()
            test.addCleanup(patcher.stop)
        return self


class PictureEmbedder(FakeEmbedder):
    """FakeEmbedder that also embeds pictures (all to the same 'picture' direction)."""
    confident_picture_similarity = 0.712

    def __init__(self, name='pictures'):
        super().__init__(name)
        self.pictures = 0

    def supports_pictures(self):
        return True

    def embed_pictures(self, images):
        self.pictures += len(images)
        return [[0.3, 0.3, 0.3, 0.9] for _ in images]


class Angle:
    """Text passages point one way, pictures another; the query sits at cosine ``similarity`` to the pictures."""
    batch = 8
    min_similarity = 0.3
    confident_similarity = 0.9
    confident_picture_similarity = 0.712

    def __init__(self, similarity):
        self.similarity, self.pictures = similarity, 0

    def signature(self):
        return 'angle'

    def embed_documents(self, texts, titles=None):
        return [[0.0, 0.0, 1.0] for _ in texts]

    def embed_query(self, text):
        return [self.similarity, math.sqrt(1 - self.similarity ** 2), 0.0]

    def supports_pictures(self):
        return True

    def embed_pictures(self, images):
        self.pictures += len(images)
        return [[1.0, 0.0, 0.0] for _ in images]


def kinds(index, path):
    con = connect(index.db_path)
    try:
        return sorted(kind for (kind,) in con.execute('SELECT kind FROM segments WHERE path=?', (str(path),)))
    finally:
        con.close()


def picture(path, color=(200, 150, 60), size=(640, 480)):
    from PIL import Image
    Image.new('RGB', size, color).save(path)


class LayerTests(Base):
    def setUp(self):
        super().setUp()
        self.ocr = FakeOcr({'receipt.png': 'Invoice total 4250 rupees delivered to Hyderabad'}).patch(self)

    def test_cancelled_after_names_then_the_next_run_reads_the_pending_files(self):
        (self.folder / 'budget-plan.txt').write_text('lighthouse keeper rota for the winter', encoding='utf-8')
        make_docx(self.folder / 'harbour-memo.docx', ['The pilot boat leaves at dawn'])
        picture(self.folder / 'receipt.png')

        def stop_after_names(state):
            if state['phase'] == 'names':
                self.index.cancel.set()
        summary = self.index.update(stop_after_names)
        self.assertEqual(summary['seconds_to_text'], None)
        self.assertEqual([r['filename'] for r in self.index.search('budget plan')], ['budget-plan.txt'])
        self.assertEqual([r['filename'] for r in self.index.search('harbour memo')], ['harbour-memo.docx'])
        self.assertEqual([r['filename'] for r in self.index.search('receipt')], ['receipt.png'])
        self.assertEqual(self.index.search('lighthouse keeper'), [])
        self.assertEqual(self.index.status()['pending'], 2)
        self.assertEqual(self.index.layer_status()['text'], [1, 3])
        self.assertEqual(self.index.pending_changes()['changed'], 2)
        self.assertEqual(self.ocr.files, [])
        phases = []
        summary = self.index.update(lambda state: phases.append(state))
        self.assertEqual(summary['indexed'], 2)
        self.assertIsNotNone(summary['seconds_to_text'])
        self.assertEqual([r['filename'] for r in self.index.search('lighthouse keeper')], ['budget-plan.txt'])
        self.assertEqual([r['filename'] for r in self.index.search('pilot boat dawn')], ['harbour-memo.docx'])
        self.assertEqual([r['filename'] for r in self.index.search('Hyderabad invoice')], ['receipt.png'])
        self.assertEqual(self.index.status()['pending'], 0)
        self.assertTrue(all('layers' in state for state in phases))
        order = [state['phase'] for state in phases]
        self.assertLess(order.index('names'), order.index('indexing'))
        self.assertLess(order.index('indexing'), order.index('reading pictures'))
        self.assertEqual(phases[-1]['layers']['text'], [3, 3])

    def test_picture_is_name_only_first_then_ocr_keeps_its_picture_vector_and_description(self):
        self.settings.update(ocr_enabled=False, vision_mode='background')
        path = self.folder / 'receipt.png'
        picture(path)
        embedder, vision = PictureEmbedder(), FakeVision()
        summary = self.index.update(embedder=embedder, vision=vision)
        self.assertEqual(summary['ocr_device'], 'off')
        self.assertEqual(kinds(self.index, path), ['file', 'picture', 'vision'])
        self.assertEqual(self.ocr.files, [])
        self.settings['ocr_enabled'] = True
        summary = self.index.update(embedder=embedder, vision=vision)
        self.assertEqual(summary['ocr_read'], 1)
        self.assertEqual(kinds(self.index, path), ['ocr', 'picture', 'vision'])
        self.assertEqual((len(vision.calls), embedder.pictures), (1, 1))  # neither described nor embedded again
        self.assertEqual(self.index.search('Hyderabad invoice')[0]['kind'], 'ocr')
        self.settings['ocr_enabled'] = False  # off again: the OCR text leaves, the picture keeps its name
        self.index.update(embedder=embedder, vision=vision)
        self.assertEqual(kinds(self.index, path), ['file', 'picture', 'vision'])

    def test_ocr_result_for_a_file_that_changed_meanwhile_is_dropped(self):
        path = self.folder / 'receipt.png'
        picture(path)
        original = self.ocr.read_file

        def changing(file):
            if len(self.ocr.files) == 0:
                picture(path, (10, 20, 30), (800, 600))  # edited while OCR reads it
            return original(file)
        with mock.patch.object(ocr, 'read_file', changing):
            self.index.update()
            self.assertEqual(kinds(self.index, path), ['file'])
            con = connect(self.index.db_path)
            self.assertIsNone(con.execute("SELECT status FROM layers WHERE layer='ocr'").fetchone())
            con.close()
            self.index.update()
        self.assertEqual(len(self.ocr.files), 2)
        self.assertEqual(kinds(self.index, path), ['ocr'])

    def test_renamed_picture_is_neither_read_nor_embedded_again(self):
        picture(self.folder / 'receipt.png')
        embedder = PictureEmbedder()
        self.index.update(embedder=embedder)
        self.assertEqual((len(self.ocr.files), embedder.pictures), (1, 1))
        (self.folder / 'receipt.png').rename(self.folder / 'copy.png')
        summary = self.index.update(embedder=embedder)
        self.assertEqual((len(self.ocr.files), embedder.pictures), (1, 1))
        self.assertEqual(summary['cache_hits'], 2)
        self.assertEqual(kinds(self.index, self.folder / 'copy.png'), ['ocr', 'picture'])
        self.assertEqual(self.index.search('Hyderabad invoice')[0]['filename'], 'copy.png')

    def test_scanned_pdf_pages_are_read_by_ocr_and_get_picture_vectors(self):
        import pymupdf
        scan = self.dir / 'scan.png'
        make_text_image(scan, ['Scanned warranty card'])
        file = self.folder / 'manual.pdf'
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), 'Chapter one explains the cooling settings in detail.', fontsize=12)
            page = document.new_page()
            page.insert_image(page.rect, filename=str(scan))
            document.save(file)
        embedder = PictureEmbedder()
        summary = self.index.update(embedder=embedder)
        self.assertEqual(self.ocr.pages, 1)
        self.assertEqual(summary['ocr_read'], 1)
        self.assertEqual(self.index.search('warranty fridge')[0]['location'], 'page 2 (text read from image)')
        con = connect(self.index.db_path)
        locations = [json.loads(loc) for (loc,) in con.execute("SELECT location FROM segments WHERE kind='picture'")]
        con.close()
        self.assertEqual(locations, [{'visual': True, 'page': 2}])
        again = self.index.update(embedder=embedder)
        self.assertEqual((again['ocr_read'], again['pictures_embedded'], self.ocr.pages), (0, 0, 1))


class PictureSearchTests(Base):
    def setUp(self):
        super().setUp()
        self.settings['ocr_enabled'] = False
        picture(self.folder / 'IMG_0001.jpg')

    def test_picture_matches_need_the_picture_floor(self):
        strong = Angle(0.75)
        summary = self.index.update(embedder=strong)
        self.assertEqual(summary['pictures_embedded'], 1)
        [hit] = self.index.search('sunset over the harbour', embedder=strong)
        self.assertEqual((hit['kind'], hit['location'], hit['snippet'], hit['visual']), ('picture', 'picture', '', True))
        self.assertNotIn('weak', hit)
        self.assertEqual(self.index.retrieve('sunset over the harbour', strong), [])  # answers need text
        con = connect(self.index.db_path)
        self.assertIsNone(con.execute("SELECT f.rowid FROM fts f JOIN segments s ON s.id = f.rowid WHERE s.kind='picture'").fetchone())
        con.close()
        weak = Angle(0.70)
        self.index._query_cache.clear()
        self.assertEqual(self.index.search('sunset over the harbour', embedder=weak), [])
        [hit] = self.index.search('sunset over the harbour', embedder=weak, include_weak=True)
        self.assertEqual((hit['kind'], hit['weak']), ('picture', True))

    def test_confident_picture_is_not_outranked_for_having_no_words(self):
        (self.folder / 'notes.txt').write_text('sunset over the harbour', encoding='utf-8')  # keyword list only
        strong = Angle(0.75)
        self.index.update(embedder=strong)
        hits = self.index.search('sunset over the harbour', embedder=strong)
        self.assertEqual([hit['kind'] for hit in hits], ['picture', 'text'])  # the picture is only in the meaning list
        self.assertGreater(hits[0]['score'], hits[1]['score'])
        weak = Angle(0.70)  # below the picture floor: no lift, and it is a weaker match
        self.index._query_cache.clear()
        hits = self.index.search('sunset over the harbour', embedder=weak, include_weak=True)
        self.assertEqual([(hit['kind'], hit.get('weak', False)) for hit in hits], [('text', False), ('picture', True)])

    def test_layer_status_counts_pictures(self):
        self.settings['embed_enabled'] = True
        embedder = Angle(0.75)
        self.assertEqual(self.index.layer_status()['pictures'], [0, 0])  # no files yet
        with mock.patch('alfred.ai.Embedder.signature', lambda self: 'angle'):
            self.index.update(embedder=embedder)
            status = self.index.layer_status()
        self.assertEqual(status['names'], [1, 1])
        self.assertEqual(status['pictures'], [1, 1])
        self.assertEqual(status['meaning'], [1, 1])

    def test_location_labels(self):
        self.assertEqual(extract.location_label('picture', {'visual': True, 'seconds': 42}), 'picture at 00:42')
        self.assertEqual(extract.location_label('picture', {'visual': True, 'page': 3}), 'page 3 (picture)')
        self.assertEqual(extract.short_location('picture', {'visual': True}), 'picture')


class SlowVision(FakeVision):
    def describe(self, image):
        time.sleep(0.3)
        return super().describe(image)


class CaptionTests(Base):
    def setUp(self):
        super().setUp()
        self.settings.update(vision_enabled=True, ocr_enabled=False)
        picture(self.folder / 'IMG_1234.jpg')
        self.path = str(self.folder / 'IMG_1234.jpg')

    def test_lazy_mode_describes_on_demand_once(self):
        self.settings['vision_mode'] = 'lazy'
        vision, embedder = SlowVision(), FakeEmbedder()
        summary = self.index.update(embedder=embedder, vision=vision)
        self.assertEqual((summary['described'], vision.calls), (0, []))
        self.assertTrue(self.index.description_pending(self.path))
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(self.index.describe_now(self.path, vision, embedder))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(vision.calls), 1)
        self.assertEqual(len(answers), 2)
        self.assertTrue(answers[0] and answers[0] == answers[1] and 'bench' in answers[0])
        self.assertEqual(self.index.search('kid on a bench in autumn')[0]['location'], 'AI description')
        con = connect(self.index.db_path)
        embedded = con.execute("SELECT count(*) FROM vectors v JOIN segments s ON s.id = v.segment_id WHERE s.kind='vision'").fetchone()[0]
        con.close()
        self.assertEqual(embedded, 1)
        self.assertEqual(self.index.describe_now(self.path, vision), answers[0])  # already described
        self.assertEqual(len(vision.calls), 1)

    def test_background_mode_describes_while_indexing(self):
        self.settings['vision_mode'] = 'background'
        vision = FakeVision()
        self.assertEqual(self.index.update(vision=vision)['described'], 1)


class BackgroundTests(Base):
    def setUp(self):
        super().setUp()
        (self.folder / 'trip.txt').write_text('vacation flight plans', encoding='utf-8')
        self.ocr = FakeOcr({'receipt.png': 'Invoice total 4250 rupees delivered to Hyderabad'}).patch(self)
        picture(self.folder / 'receipt.png')

    def test_background_run_on_battery_brings_only_names_and_text_up_to_date(self):
        embedder = PictureEmbedder()
        with mock.patch.object(power, 'on_battery', return_value=True):
            summary = self.index.update(embedder=embedder, background=True)
            self.assertEqual(summary['paused'], 'battery')
            self.assertEqual((summary['embedded'], summary['ocr_read'], embedder.pictures, self.ocr.files), (0, 0, 0, []))
            self.assertEqual(sum('battery' in w for w in summary['warnings']), 1)
            self.assertIsNone(summary['seconds_to_core'])
            self.assertEqual(self.index.search('vacation flight')[0]['filename'], 'trip.txt')
            summary = self.index.update(embedder=embedder)  # foreground: everything, battery or not
            self.assertIsNone(summary['paused'])
            self.assertEqual((summary['embedded'] > 0, summary['ocr_read'], embedder.pictures), (True, 1, 1))
            self.assertIsNotNone(summary['seconds_to_core'])

    def test_thread_budget(self):
        with mock.patch.object(power, 'usable_cpus', return_value=20):
            self.assertEqual((self.index.thread_budget(), self.index.thread_budget(background=True)), (18, 5))
            self.settings['index_threads'] = 4
            self.assertEqual(self.index.thread_budget(background=True), 4)
        self.assertEqual(ocr.thread_plan(18), (4, 4))
        self.assertEqual(ocr.thread_plan(4), (1, 4))
        self.assertEqual(ocr.thread_plan(2), (1, 2))
        from alfred import ai
        self.assertEqual(ai._options('cpu', 4), {'num_gpu': 0, 'num_thread': 4})
        self.assertEqual(ai._options('auto', 4), {})


class UpgradeTests(Base):
    def test_old_index_is_upgraded_without_ocr_and_gains_picture_vectors(self):
        fake = FakeOcr({'receipt.png': 'Invoice total 4250 rupees delivered to Hyderabad'}).patch(self)
        picture(self.folder / 'receipt.png')
        (self.folder / 'notes.txt').write_text('harbour pilot schedule', encoding='utf-8')
        self.index.update(embedder=FakeEmbedder())
        self.assertEqual(len(fake.files), 1)
        self.index.close()
        con = connect(self.index.db_path)
        with con:  # what an index written before layers looked like
            con.execute('DROP TABLE layers')
            con.execute('DROP TABLE media_cache')
            con.execute("DELETE FROM meta WHERE key IN ('schema_version', 'ocr_enabled')")
            con.execute("INSERT OR REPLACE INTO meta VALUES('media_signature', 'ocr:True|video:10')")
        con.close()
        index = Index(self.settings)
        self.addCleanup(index.close)
        embedder = PictureEmbedder('fake')  # the same text model signature as before
        summary = index.update(embedder=embedder)
        self.assertEqual(len(fake.files), 1)
        self.assertEqual((summary['ocr_read'], summary['embedded']), (0, 0))
        self.assertEqual(summary['pictures_embedded'], 1)
        self.assertEqual(kinds(index, self.folder / 'receipt.png'), ['ocr', 'picture'])


class DeferredPdfTests(Base):
    def test_pdf_reports_pages_for_ocr_and_reads_the_rest(self):
        import pymupdf
        scan = self.dir / 'scan.png'
        make_text_image(scan, ['Scanned warranty card'])
        file = self.folder / 'manual.pdf'
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), 'Chapter one explains the cooling settings in detail.', fontsize=12)
            page = document.new_page()
            page.insert_image(page.rect, filename=str(scan))
            document.new_page().insert_text((72, 72), 'Chapter three covers cleaning the filters every month.', fontsize=12)
            document.save(file)
        deferred = []
        with mock.patch.object(ocr, 'read_text', side_effect=AssertionError('OCR must wait')):
            passages = extract.extract(file, deferred=deferred)
        self.assertEqual(deferred, [2])
        self.assertEqual([loc['page'] for _, _, loc in passages], [1, 3])


if __name__ == '__main__':
    unittest.main()
