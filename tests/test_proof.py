"""Answer check tests (alfred/proof.py, M5): the unit splitter, the value extractor and lookup, every citation outcome,
every verdict, the refusals and the time budget. Pure: no model, no index, no files."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alfred import proof  # noqa: E402


def source(text, filename='notes.txt', label='lines 1–5', parts=None, kind='text'):
    return {'text': text, 'filename': filename, 'label': label, 'location': label, 'kind': kind, 'path': 'C:\\data\\' + filename,
            'parts': parts or [(text, {'line': 1})]}


def units(answer):
    return [(u['kind'], u['text']) for u in proof.split_units(answer)[0]]


def values(text):
    return [(v['text'], v['type']) for v in proof.extract_values(text)]


class SplitterTests(unittest.TestCase):
    def test_no_split_on_decimals_thousands_abbreviations_files_or_brackets(self):
        text = ('Dr. Lee paid $3.5 million and 1,250.00 more, e.g. for No. 5 at Acme Inc. in report.pdf (see fig. 2. it is there) [1]. '
                'The next one [2].')
        self.assertEqual([t for _, t in units(text)],
                         ['Dr. Lee paid $3.5 million and 1,250.00 more, e.g. for No. 5 at Acme Inc. in report.pdf (see fig. 2. it is there) [1].',
                          'The next one [2].'])

    def test_marker_after_the_full_stop_belongs_to_the_sentence_before(self):
        found = proof.split_units('It started in 2024. [1] Then it stopped [2].')[0]
        self.assertEqual([(u['text'], u['cites']) for u in found], [('It started in 2024. [1]', [1]), ('Then it stopped [2].', [2])])

    def test_bullets_headings_and_lead_ins(self):
        text = '## Costs\n**Summary**\nHere are the details:\n- Groceries cost 5400 [1].\n2. Power cost 2300 [2]. It rose.\n\nBeyond your files\nPrices rise.'
        self.assertEqual(units(text), [('connective', '## Costs'), ('connective', '**Summary**'), ('connective', 'Here are the details:'),
                                       ('claim', 'Groceries cost 5400 [1].'), ('claim', 'Power cost 2300 [2].'), ('claim', 'It rose.')])
        start = text.index('Groceries')
        self.assertEqual(proof.split_units(text)[0][3]['start'], start)  # the span skips the bullet

    def test_initials_and_abbreviations_before_a_capital(self):
        self.assertEqual([t for _, t in units('Signed by R. Patel at Acme Inc. The box was full.')],
                         ['Signed by R. Patel at Acme Inc.', 'The box was full.'])

    def test_no_info_kind(self):
        kinds = [k for k, _ in units('Your files don’t mention the VAT number [1]. I couldn\'t find a phone number. '
                                     'The policy does not cover floods [2]. The deadline is not specified.')]
        self.assertEqual(kinds, ['no_info', 'no_info', 'claim', 'no_info'])


class ValueTests(unittest.TestCase):
    def test_each_type(self):
        self.assertEqual(values('INV-2024-0117 was paid on 14 March 2025 at 10:00 for $1,250.50 (18%), Q3 2024, B12, $4.2M, '
                                'call 555-0102, mail tom@acme.com, see report_2024.pdf, March 14, 2025-03-14, 14/03/2025, −3'),
                         [('INV-2024-0117', 'identifier'), ('14 March 2025', 'date'), ('10:00', 'number'), ('$1,250.50', 'number'),
                          ('18%', 'number'), ('Q3 2024', 'date'), ('B12', 'identifier'), ('$4.2M', 'number'), ('555-0102', 'identifier'),
                          ('tom@acme.com', 'identifier'), ('report_2024.pdf', 'identifier'), ('March 14', 'date'),
                          ('2025-03-14', 'date'), ('14/03/2025', 'date'), ('3', 'number')])

    def test_markers_and_list_numbering_are_not_values(self):
        found = proof.split_units('1. The fee is 40 [2].')[0][0]
        self.assertEqual(values(found['text']), [('40', 'number')])

    def test_never_inside_a_longer_number(self):
        evidence = proof.Evidence('In 2023 the rate was 3.5 and code INV-3.')
        self.assertIsNone(proof.lookup(proof.extract_values('3')[0], evidence))
        self.assertEqual(proof.lookup(proof.extract_values('3.50')[0], evidence), 'equivalent')
        self.assertEqual(proof.lookup(proof.extract_values('2023')[0], evidence), 'exact')

    def test_values_in_the_question_are_not_checked(self):
        checked = proof.check('Sensor TMP-88231 read 24.2 at 12:00 on 2026-09-02 [1].', {1: source('TMP-88231 | 24.2')},
                              'What did sensor TMP-88231 read at 12:00 on 2026-09-02?')
        self.assertEqual([v['text'] for v in checked.proof['units'][0]['values']], ['24.2'])
        self.assertEqual(checked.proof['units'][0]['status'], 'ok')


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.evidence = proof.Evidence('Go-live moved to 2027-03-22. Total: 1250000 dollars. Ref KX7731. Phone (555) 0102. Up 12%.',
                                       'Q3_board_deck.pptx', 'slide 4')

    def find(self, text):
        return proof.lookup(proof.extract_values(text)[0], self.evidence)

    def test_exact_equivalent_missing(self):
        self.assertEqual(self.find('2027-03-22'), 'exact')
        self.assertEqual(self.find('22 March 2027'), 'equivalent')  # an equivalent date counts as present
        self.assertEqual(self.find('$1,250,000'), 'equivalent')
        self.assertEqual(self.find('$1.25 million'), 'equivalent')
        self.assertEqual(self.find('KX-7731'), 'equivalent')
        self.assertEqual(self.find('555-0102'), 'equivalent')
        self.assertIsNone(self.find('23 March 2027'))
        self.assertIsNone(self.find('12 million'))  # 12% is not 12 million
        self.assertIsNone(self.find('KX-7732'))

    def test_value_in_the_file_name_or_location_label(self):
        self.assertEqual(self.find('Q3_board_deck.pptx'), 'exact')
        self.assertEqual(self.find('slide 4 says 4'), 'exact')


SOURCES = {
    1: source('Role | Phone\nTom Okafor | 555-0102\nAna Ruiz | 555-0177', 'vendor_contacts.xlsx', 'Vendors, rows 1–3'),
    2: source('Brightline Office Systems is the contact for copier service. Brightline Office Systems answers calls in 8 hours.',
              'brightline_terms.txt'),
    3: source('The flight departs Friday from Hyderabad at noon.', 'trip.txt'),
}


class CitationTests(unittest.TestCase):
    def check(self, answer, question='', **kw):
        return proof.check(answer, SOURCES, question, **kw)

    def numbers(self, checked):
        return [c['number'] for c in checked.cited]

    def test_kept(self):
        checked = self.check('The flight departs Friday from Hyderabad [3].')
        self.assertEqual((checked.answer, self.numbers(checked)), ('The flight departs Friday from Hyderabad [1].', [3]))
        self.assertEqual(checked.proof['units'][0]['status'], 'ok')

    def test_never_moved_when_the_values_match(self):
        # The sentence shares more words with source 2, but only the spreadsheet holds the phone number.
        checked = self.check('Tom Okafor is the Brightline Office Systems contact, phone 555-0102 [1].')
        self.assertEqual(self.numbers(checked), [1])
        self.assertEqual(checked.proof['units'][0]['values'][0]['status'], 'exact')

    def test_added_by_value_and_wrong_citation_dropped(self):
        checked = self.check('Tom Okafor can be reached on 555-0102 [2].')
        self.assertEqual((checked.answer, self.numbers(checked)), ('Tom Okafor can be reached on 555-0102 [1].', [1]))
        checked = self.check('Tom Okafor can be reached on 555-0102.')
        self.assertEqual((checked.answer, self.numbers(checked)), ('Tom Okafor can be reached on 555-0102 [1].', [1]))

    def test_moved_by_support(self):
        checked = self.check('The flight departs Friday from Hyderabad [2].')
        self.assertEqual((checked.answer, self.numbers(checked)), ('The flight departs Friday from Hyderabad [1].', [3]))

    def test_dropped_and_flagged_no_source(self):
        checked = self.check('Paris is lovely in spring [3]. The flight departs Friday [3].')
        self.assertEqual(checked.answer, 'Paris is lovely in spring. The flight departs Friday [1].')
        self.assertEqual(checked.flags, [{'start': 0, 'end': 26, 'sentence': 'Paris is lovely in spring.', 'reasons': ['no_source'],
                                          'values': []}])
        self.assertEqual((checked.proof['claims'], checked.proof['verified']), (2, 1))

    def test_value_not_in_sources(self):
        checked = self.check('Tom Okafor answers within 8 hours and costs $90 [2].')
        unit = checked.proof['units'][0]
        self.assertEqual((unit['status'], unit['reasons']), ('flagged', ['value_not_in_sources']))
        self.assertEqual([(v['text'], v['status'], v['source']) for v in unit['values']], [('8', 'exact', 1), ('$90', 'missing', None)])
        self.assertEqual(checked.flags[0]['values'], ['$90'])
        self.assertEqual(self.numbers(checked), [2])  # the citation holding the other value stays

    def test_citation_narrows_to_the_part_with_the_proof(self):
        deck = source('Kickoff\nBudget is $2.4M\nGo-live is 2027-03-22', 'falcon.pptx', 'slides 1–3', kind='slide',
                      parts=[('Kickoff', {'slide': 1}), ('Budget is $2.4M', {'slide': 2}), ('Go-live is 2027-03-22', {'slide': 3})])
        checked = proof.check('Go-live is on 2027-03-22 [1].', {1: deck})
        self.assertEqual(checked.cited, [{'number': 1, 'part': 2, 'highlight': 'Go-live is 2027-03-22'}])


class VerdictTests(unittest.TestCase):
    def test_nothing_verified_is_refused(self):
        checked = proof.check('Paris is lovely in spring [3].', SOURCES, not_found='Nothing found.')
        self.assertEqual((checked.answer, checked.refused, checked.cited, checked.flags), ('Nothing found.', 'unverified', [], []))
        self.assertEqual(checked.unverified_text, 'Paris is lovely in spring.')

    def test_hedge_then_stray_citation_is_a_refusal(self):
        checked = proof.check('Your files don\'t mention the battery level of sensor TMP-88231. [1]', SOURCES)
        self.assertEqual((checked.answer, checked.refused, checked.cited), (proof.NOT_FOUND, 'unverified', []))

    def test_hedge_and_a_supported_claim_is_not_a_refusal(self):
        checked = proof.check('Your files don\'t mention who approved it. [1] The flight departs Friday from Hyderabad [1].', SOURCES)
        self.assertIsNone(checked.refused)
        self.assertEqual(checked.answer, 'Your files don\'t mention who approved it. The flight departs Friday from Hyderabad [1].')
        self.assertEqual([u['kind'] for u in checked.proof['units']], ['no_info', 'claim'])

    def test_search_line_is_stripped_unless_it_is_the_whole_reply(self):
        self.assertEqual(proof.strip_search('Your files don\'t mention it. SEARCH: pet deposit; lease'), 'Your files don\'t mention it.')
        self.assertEqual(proof.strip_search('SEARCH: pet deposit'), 'SEARCH: pet deposit')
        checked = proof.check('The flight departs Friday from Hyderabad [3].\nSEARCH: flight time', SOURCES)
        self.assertEqual(checked.answer, 'The flight departs Friday from Hyderabad [1].')

    def test_result_shape(self):
        checked = proof.check('The flight departs Friday from Hyderabad [3].', SOURCES, 'When is the flight?')
        self.assertEqual(set(checked.proof), {'units', 'claims', 'verified', 'method', 'ms'})
        self.assertEqual(set(checked.proof['units'][0]), {'start', 'end', 'text', 'kind', 'cites', 'status', 'reasons', 'values', 'support'})
        self.assertEqual(checked.proof['units'][0]['cites'], [1])
        self.assertEqual(checked.proof['method'], 'words')
        unit = checked.proof['units'][0]
        self.assertEqual(checked.answer[unit['start']:unit['end']], unit['text'])


class FakeReranker:
    info = {'support_min': 0.5}

    def __init__(self):
        self.calls = []

    def score_pairs(self, pairs):
        self.calls.append(list(pairs))
        return [0.9 if 'Hyderabad' in passage and ('flight' in query or 'plane' in query) else 0.1 for query, passage in pairs]


class RerankerTests(unittest.TestCase):
    def test_one_call_capped_and_used(self):
        reranker = FakeReranker()
        answer = ' '.join(f'The flight number {n} departs on Friday [2].' for n in range(30))
        checked = proof.check(answer, SOURCES, '', reranker)
        self.assertEqual(len(reranker.calls), 1)
        self.assertLessEqual(len(reranker.calls[0]), proof.PAIR_CAP)
        self.assertEqual(checked.proof['method'], 'reranker')

    def test_reranker_moves_a_citation_the_words_cannot(self):
        checked = proof.check('Their plane leaves for Goa at the end of the week [1].', SOURCES, '', FakeReranker())
        self.assertEqual([c['number'] for c in checked.cited], [3])


class WeakValueTests(unittest.TestCase):
    FLAT = {1: source('The flat on Elm Street has three bedrooms and a garden.', 'flat.txt'),
            2: source('Invoice 3 of the roofing job is unpaid.', 'roof.txt')}

    def test_a_number_word_in_the_source_holds_the_digit(self):
        checked = proof.check('The flat has 3 bedrooms [1].', self.FLAT, 'How many bedrooms?')
        self.assertEqual([c['number'] for c in checked.cited], [1])
        self.assertEqual(checked.flags, [])

    def test_a_small_number_alone_does_not_move_a_citation(self):
        sources = {1: source('The flat on Elm Street has several bedrooms and a garden.', 'flat.txt'), 2: self.FLAT[2]}
        checked = proof.check('The flat has 3 bedrooms and a garden [1].', sources, 'How many bedrooms?')
        self.assertEqual([c['number'] for c in checked.cited], [1])  # kept by its words; not moved to the invoice for its '3'
        self.assertEqual(checked.flags[0]['reasons'], ['value_not_in_sources'])
        self.assertEqual(checked.flags[0]['values'], ['3'])

    def test_a_strong_value_still_moves_it(self):
        sources = {1: self.FLAT[1], 2: source('sensor,reading\nTMP-88231,24.2', 'readings.csv')}
        checked = proof.check('The sensor read 24.2 [1].', sources, 'What did the sensor read?')
        self.assertEqual([c['number'] for c in checked.cited], [2])


class SpeedTests(unittest.TestCase):
    def test_ten_sentences_eight_sources_well_under_300_ms(self):
        prose = ' '.join(f'Item {n} of the plan costs ${n * 37},00{n % 10} and ships on 2026-0{1 + n % 9}-1{n % 10}.' for n in range(60))
        sources = {n: source(prose + f' Source {n} ends here.', f'plan_{n}.txt') for n in range(1, 9)}
        answer = ' '.join(f'Item {n} costs ${n * 37},00{n % 10} and ships on 2026-0{1 + n % 9}-1{n % 10} [{1 + n % 8}].' for n in range(10))
        started = time.perf_counter()
        checked = proof.check(answer, sources, 'What do the items cost?')
        elapsed = time.perf_counter() - started
        self.assertEqual(checked.proof['claims'], 10)
        self.assertLess(elapsed, 0.15, elapsed)


if __name__ == '__main__':
    unittest.main()
