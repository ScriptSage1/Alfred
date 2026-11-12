"""Proof-first answers (M5): checks a generated answer against the sources the model was given.

``check(answer, sources, question, reranker=None)`` is the one entry point. Pure: no I/O and no model except the
optional reranker passed in (``reranker.score(query, passages)``, or ``score_pairs(pairs)`` to score every pair in one
call). ``sources`` is ``{number: source}`` as ``rag.ask`` holds them; each source has ``text``, ``filename``, ``label``
(and ``location``), ``parts`` = [(text, loc)], ``kind`` and ``path``. Markers in ``answer`` are source numbers.

1. Units.  The body (everything before "Beyond your files") is split into sentences and bullet items, each with its
   character span and its [n] markers. No split on decimals, thousands separators, abbreviations (e.g., No., Dr.),
   file names or inside brackets; a marker after the full stop belongs to the sentence before it. Kinds: ``claim``,
   ``no_info`` (says the files don't cover something: never keeps a citation, never checked) and ``connective``
   (a heading, or a lead-in ending in ':' with no checkable value and fewer than 4 content words).
2. Values.  Numbers (``1,250.00``, ``12%``, ``$4.2M``), dates (``14 March 2025``, ``2025-03-14``, ``Q3 2024``), clock
   times, and identifiers (codes such as ``INV-2024-0117``, ``B12``, emails, phone numbers, file names). Not values: the
   [n] markers, list numbering, and anything that also appears in the question (the user supplied it).
3. Lookup.  A value is present in a source when it occurs in the source's text, file name or location label:
   ``exact`` (same characters, ignoring case and spacing) or ``equivalent`` (the same number after normalising, the same
   date in another written form, an identifier in one of the written forms ``index.query_profile`` searches for).
   An equivalent date counts as present and the answer's wording is kept (M5_DESIGN section 9).
4. Support.  For a (claim, source) pair: the window is the sentence of the source that holds the claim's values and
   shares most content words with it, plus one sentence either side (short lines read on to about 350 characters),
   inside one of the source's parts. Scored by the share of the claim's content words in the window (supported at
   >= 0.5 and at least 2 words) and, with the reranker, by its score (all pairs in one call, at most 24 per answer)
   against ``support_min`` of the model. A pair is supported when either says so: the reranker adds paraphrases the
   words miss, but reads a short follow-up ("It was for team breakfast") as irrelevant without the sentence before.
5. Citations.  Values decide first: a cited source holding any of the claim's values is never moved or dropped; a value
   found only in another offered source adds that source; a cited source holding none of the values is dropped when
   others hold them. A claim without values (or whose values are in no source) keeps the cited sources that support it,
   moves an unsupported citation to the best supporting source, or drops it; an uncited claim gets the best supporting
   source. A claim left with no citation is flagged ``no_source``; a value in no offered source flags
   ``value_not_in_sources`` (this also catches figures the model calculated itself).
6. Verdict.  A claim is ``ok`` or ``flagged``. The answer is refused (``refused='unverified'``, the not-found message,
   the model's text kept as ``unverified_text``) when it only says the files don't cover the question, or when it has
   claims, none ok and no citation left.

The reranker measures relevance, not agreement: a passage on the same subject with a different figure scores high (the
value check covers that), and a flipped meaning with no value in it ("approved" vs "rejected") passes both.
"""
import logging
import re
import time
from collections import namedtuple
from decimal import Decimal, InvalidOperation

from . import index as _index

log = logging.getLogger('alfred')
BEYOND = 'Beyond your files'
BEYOND_LINE = re.compile(rf'(?im)^[#>*_\s]*{BEYOND}\b')
NOT_FOUND = 'I couldn’t find anything about this in your files.'
SUPPORT_MIN = 0.30          # reranker score a window needs (per model in rerank.MODELS['support_min'])
WORD_SHARE, WORD_MIN = 0.5, 2  # without the reranker: share of the claim's content words in the window, and at least 2
PAIR_CAP = 24               # reranker pairs per answer; the rest are scored by words
WINDOW_CHARS = 700
ALTERNATIVES = 2            # other sources scored for a claim whose citation may have to move

Checked = namedtuple('Checked', 'answer proof cited refused unverified_text flags')

# ------------------------------------------------------------------ units
MARKER = re.compile(r'\[(\d{1,3})\]')
TRAILING_MARKERS = re.compile(r'(?:\s*\[\d{1,3}\])+')
BULLET = re.compile(r'[ \t]*(?:[-*•–+]|\d{1,2}[.)])[ \t]+')
HEADING = re.compile(r'^\s*(?:#{1,6}\s|(?:\*\*|__)[^*_\n]+(?:\*\*|__):?\s*$)')
SEARCH_LEAK = re.compile(r'(?:(?<=[.!?])|(?<=^)|(?<=\n))[ \t]*[*_`#>]*SEARCH\s*:[^\n]*')
# Never a sentence end: titles and Latin abbreviations. "Maybe" ones end a sentence before a capital letter.
ABBREV_ALWAYS = {'dr', 'mr', 'mrs', 'ms', 'prof', 'st', 'vs', 'no', 'nos', 'fig', 'figs', 'approx', 'est', 'e.g', 'i.e', 'cf', 'ca',
                 'ref', 'vol', 'p', 'pp', 'para', 'sec', 'ch', 'art', 'nr', 'tel', 'ext', 'dept', 'sr', 'jr', 'gen', 'col', 'lt', 'mt'}
ABBREV_MAYBE = {'inc', 'ltd', 'co', 'corp', 'llc', 'plc', 'etc', 'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct',
                'nov', 'dec', 'a.m', 'p.m', 'min', 'max', 'avg', 'hrs', 'mins', 'yrs', 'al'}
# Says the files don't cover something. Kept narrow on purpose: "The policy does not cover floods [1]" is a claim.
NO_INFO = re.compile(
    r"(?i)\b(?:i|we)\s+(?:couldn['’]t|could\s+not|can['’]t|cannot|did\s+not|didn['’]t|was\s+unable\s+to|am\s+unable\s+to)\s+"
    r"(?:\w+\s+){0,2}?(?:find|locate|see)\b"
    r"|\b(?:files?|documents?|sources?|passages?|records?|notes?|text|excerpts?|information|data|materials?|context)\s+"
    r"(?:\w+\s+){0,3}?(?:don['’]t|do\s+not|doesn['’]t|does\s+not|didn['’]t|did\s+not)\s+(?:\w+\s+){0,2}?"
    r"(?:mention|say|state|contain|include|cover|provide|specify|have)"
    r"|\b(?:doesn['’]t|does\s+not|don['’]t|do\s+not|didn['’]t|did\s+not)\s+(?:\w+\s+)?(?:mention|specify|state|say)"
    r"|\bno\s+(?:information|mention|record|details?)\b"
    r"|\bnot\s+(?:mentioned|stated|specified|found)\b")
COMMON = set('the and for that this with are was were your you files file from have has had not but what which will would should '
             'can could its also there their they them than then into about been being does did more most some such only very '
             'here following below above these those each other any all per via both either one'.split()) | _index.STOPWORDS
MONTH_SHORT = {m: m[:3] for m in _index.MONTHS}
NUMBER_WORDS = {w: str(n) for n, w in enumerate('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen '
                                                 'fifteen sixteen seventeen eighteen nineteen twenty'.split()) if n >= 2}
NUMBER_WORDS.update({'thirty': '30', 'forty': '40', 'fifty': '50', 'sixty': '60', 'seventy': '70', 'eighty': '80', 'ninety': '90'})
NUMBER_DIGITS = set(NUMBER_WORDS.values())


def strip_search(answer):
    """The answer without a leaked 'SEARCH: …' line (one that is not the whole reply)."""
    cleaned = SEARCH_LEAK.sub('', answer or '')
    return cleaned.strip() if cleaned.strip() else (answer or '')


def markers(text):
    """Citation markers in ``text`` as (start, end, number); a bracket glued to a word (array[0]) is not one."""
    return [(m.start(), m.end(), int(m.group(1))) for m in MARKER.finditer(text)
            if not (m.start() > 0 and text[m.start() - 1].isalnum())]


def plain(text):
    """``text`` without citation markers (and the spaces they leave)."""
    out, pos = [], 0
    for start, end, _ in markers(text):
        out.append(text[pos:start])
        pos = end
    out.append(text[pos:])
    return re.sub(r'\s+([.,;:!?])', r'\1', re.sub(r'[ \t]{2,}', ' ', ''.join(out))).strip()


def _masked(text):
    """``text`` with markers replaced by spaces (offsets kept)."""
    for start, end, _ in markers(text):
        text = text[:start] + ' ' * (end - start) + text[end:]
    return text


def _sentence_spans(line):
    """(start, end) of each sentence in one line of an answer (see the module docstring)."""
    spans, start, i, n, depth = [], 0, 0, len(line), 0
    while i < n:
        c = line[i]
        if c == '[':
            m = MARKER.match(line, i)
            if m:
                i = m.end()
                continue
            depth += 1
        elif c == '(':
            depth += 1
        elif c in ')]':
            depth = max(0, depth - 1)
        elif c in '.!?…' and depth == 0:
            j = i
            while j + 1 < n and line[j + 1] in '.!?…':
                j += 1
            k = j + 1
            while k < n and line[k] in '"”’\')*_':
                k += 1
            m = TRAILING_MARKERS.match(line, k)
            end = m.end() if m else k
            if end >= n or line[end].isspace():
                if c == '.' and j == i and not _ends_sentence(line[start:i], line[end:]):
                    i = end
                    continue
                spans.append((start, end))
                start = end
                while start < n and line[start].isspace():
                    start += 1
                i = start
                continue
            i = j + 1
            continue
        i += 1
    if line[start:].strip():
        spans.append((start, len(line.rstrip())))
    return spans


def _ends_sentence(before, after):
    word = re.search(r'([A-Za-z][A-Za-z.]*)$', before)
    token = word.group(1).lower().rstrip('.') if word else ''
    if re.search(r'(?:^|[\s(])[A-Z]$', before):
        return False  # an initial: R. Patel
    if token in ABBREV_ALWAYS:
        return False
    if token in ABBREV_MAYBE:
        nxt = after.lstrip()[:1]
        return bool(nxt) and nxt.isupper()
    return True


def content_words(text):
    """The words of ``text`` that carry meaning (for support by words): no common words, months shortened, number words
    as digits ('fourteen' → '14', so a form that says '14 cartons' supports it)."""
    words = [MONTH_SHORT.get(w, NUMBER_WORDS.get(w, w)) for w in _index._words(plain(text))]
    return [w for w in dict.fromkeys(words) if (len(w) >= 3 and not w.isdigit() and w not in COMMON) or w in NUMBER_DIGITS]


def split_units(answer):
    """(units, body_end): units of the answer body, each {'start', 'end', 'text', 'kind', 'cites'}."""
    cut = BEYOND_LINE.search(answer)
    body_end = cut.start() if cut else len(answer)
    units = []
    for line in re.finditer(r'[^\n]+', answer[:body_end]):
        base, raw = line.start(), line.group(0)
        if not raw.strip():
            continue
        if HEADING.match(raw):
            lead = len(raw) - len(raw.lstrip())
            units.append(_unit(answer, base + lead, base + len(raw.rstrip()), 'connective'))
            continue
        bullet = BULLET.match(raw)
        offset = bullet.end() if bullet else len(raw) - len(raw.lstrip())
        for start, end in _sentence_spans(raw[offset:]):
            units.append(_unit(answer, base + offset + start, base + offset + end, None))
    return units, body_end


def _unit(answer, start, end, kind):
    text = answer[start:end]
    bare = plain(text)
    if kind is None:
        if not re.search(r'[^\W_]', bare):
            kind = 'connective'
        elif NO_INFO.search(bare):
            kind = 'no_info'
        elif bare.endswith(':') and not extract_values(bare) and len(content_words(bare)) < 4:
            kind = 'connective'
        else:
            kind = 'claim'
    return {'start': start, 'end': end, 'text': text, 'kind': kind, 'cites': list(dict.fromkeys(n for _, _, n in markers(text)))}


# ------------------------------------------------------------------ values
_MONTHS = (r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept(?:ember)?|Sep|'
           r'Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)')
MONTH_RE = f'(?:{_MONTHS}|{_MONTHS.upper()})'  # capitalised or all capitals: 'may' and 'march' are usually words
MONTH_NUMBER = {m: n for n, m in enumerate(_index.MONTHS, 1)}
MONTH_NUMBER.update({m[:3]: n for m, n in list(MONTH_NUMBER.items())})
MONTH_NUMBER['sept'] = 9
EXTENSIONS = 'pdf|docx?|xlsx?|xlsm|csv|tsv|pptx?|txt|md|markdown|rtf|odt|ods|odp|png|jpe?g|gif|bmp|tiff?|webp|heic|mp4|mov|mkv|avi|webm|mp3|wav|html?|json|xml|eml|msg|zip'
EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
FILENAME = re.compile(rf'(?<![\w.-])[\w][\w.-]*\.(?:{EXTENSIONS})\b', re.I)
QUARTER = re.compile(r'\b(Q[1-4])\s*(?:of\s+|[-/]\s*)?(\d{4})\b|\b(\d{4})\s*[-/]?\s*(Q[1-4])\b')
ID_JOINED = re.compile(r'(?<![\w-])[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+(?![\w-])')
ID_SHORT = re.compile(r'(?<![\w-])[A-Za-z]{1,5}\d+[A-Za-z\d]*(?![\w-])')
DATES = [
    ('iso', re.compile(r'\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b')),
    ('dmy', re.compile(rf'\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_RE})\b\.?,?\s+(\d{{4}})\b')),
    ('mdy', re.compile(rf'\b({MONTH_RE})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b')),
    ('my', re.compile(rf'\b({MONTH_RE})\b\.?,?\s+(\d{{4}})\b')),
    ('md', re.compile(rf'\b({MONTH_RE})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b')),
    ('dm', re.compile(rf'\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_RE})\b')),
    ('slash', re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b')),
    ('dotted', re.compile(r'\b(\d{1,2})[.-](\d{1,2})[.-](\d{4})\b')),
]
PHONE = re.compile(r'(?<![\w-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{3,4}[.-]\d{3,4}(?:[.-]\d{3,4})?(?![\w-])')
TIME = re.compile(r'(?<![\d:])(\d{1,2}):([0-5]\d)(?::[0-5]\d)?(?![\d:])(?:\s?([AaPp])\.?[Mm]\b\.?)?')
NUMBER = re.compile(r'(?<![\w.])(?<!\d,)([$€£¥₹]\s?)?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?!\d)(?!,\d{3})(?:st|nd|rd|th)?'
                    r'(?:(\s?%|\s?per\s?cent\b)|([kKmMbB](?:n|illion)?\b|\s(?:thousand|million|billion|bn|mn)\b))?')
SCALE = {'k': Decimal(1000), 'thousand': Decimal(1000), 'm': Decimal(10) ** 6, 'mn': Decimal(10) ** 6, 'million': Decimal(10) ** 6,
         'b': Decimal(10) ** 9, 'bn': Decimal(10) ** 9, 'billion': Decimal(10) ** 9}


def _is_code(fragment):
    """INV-2026-0117-like: a letter and a digit, with an upper-case part or a part mixing letters and digits (not 20-minute)."""
    if not (re.search(r'[A-Za-z]', fragment) and re.search(r'\d', fragment)):
        return False
    pieces = re.split(r'[-_]', fragment)
    return any(p.isalpha() and p.isupper() for p in pieces) or any(re.search(r'[A-Za-z]', p) and re.search(r'\d', p) for p in pieces)


def _decimal(digits):
    try:
        return Decimal(digits.replace(',', '')).normalize()
    except InvalidOperation:
        return None


def _date_parts(name, g):
    def month(word):
        word = word.lower().rstrip('.')
        return MONTH_NUMBER.get(word) or MONTH_NUMBER.get(word[:3])
    if name == 'iso':
        return [(int(g[0]), int(g[1]), int(g[2]))]
    if name == 'dmy':
        return [(int(g[2]), month(g[1]), int(g[0]))]
    if name == 'mdy':
        return [(int(g[2]), month(g[0]), int(g[1]))]
    if name == 'my':
        return [(int(g[1]), month(g[0]), None)]
    if name == 'md':
        return [(None, month(g[0]), int(g[1]))]
    if name == 'dm':
        return [(None, month(g[1]), int(g[0]))]
    a, b, year = int(g[0]), int(g[1]), int(g[2])
    year += 2000 if year < 100 else 0
    return [(year, b, a), (year, a, b)]  # day first or month first: both are written


def extract_values(text):
    """The checkable values of ``text`` in order: [{'text', 'type', 'start', 'end', ...}] (see the module docstring)."""
    text = _masked(text)
    found = []

    def take(match, kind, **extra):
        nonlocal text
        raw = match.group(0)
        start = match.start() + len(raw) - len(raw.lstrip())
        end = match.end() - (len(raw) - len(raw.rstrip()))
        found.append(dict(text=raw.strip(), type=kind, start=start, end=end, **extra))
        text = text[:match.start()] + ' ' * (match.end() - match.start()) + text[match.end():]

    for m in list(EMAIL.finditer(text)):
        take(m, 'identifier', form='email')
    for m in list(FILENAME.finditer(text)):
        take(m, 'identifier', form='file')
    for m in list(QUARTER.finditer(text)):
        quarter, year = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        take(m, 'date', parts=[('Q', int(year), int(quarter[1]))])
    for pattern in (ID_JOINED, ID_SHORT):
        for m in list(pattern.finditer(text)):
            fragment = m.group(0)
            if pattern is ID_JOINED and not _is_code(fragment):
                continue
            if re.fullmatch(r'\d+(?:st|nd|rd|th)', fragment, re.I) or fragment.lower() in ('mp3', 'mp4', 'h264'):
                continue
            take(m, 'identifier', form='code')
    for name, pattern in DATES:
        for m in list(pattern.finditer(text)):
            if name in ('md', 'dm', 'my') and not re.match(r'[A-Z0-9]', m.group(0)):
                continue  # 'may 2025' is rarely a date
            parts = [p for p in _date_parts(name, m.groups()) if p[1] and 1 <= p[1] <= 12 and (p[2] is None or 1 <= p[2] <= 31)]
            if parts:
                take(m, 'date', parts=parts)
    for m in list(PHONE.finditer(text)):
        digits = re.findall(r'\d+', m.group(0))
        joined = ''.join(digits)
        if len(joined) < 7 or (len(digits) == 2 and all(re.fullmatch(r'(19|20)\d\d', d) for d in digits)):
            continue  # a range of years, 2024-2025
        take(m, 'identifier', form='phone')
    for m in list(TIME.finditer(text)):
        hour = int(m.group(1)) % 12 + (12 if (m.group(3) or '').lower() == 'p' else 0) if m.group(3) else int(m.group(1))
        take(m, 'number', form='time', key=(hour, int(m.group(2))))
    for m in list(NUMBER.finditer(text)):
        digits, pct, scale = m.group(2), m.group(3), m.group(4)
        value = _decimal(digits)
        if value is None:
            continue
        suffix = '%' if pct else (scale.strip().lower().rstrip('.') if scale else None)
        suffix = {'million': 'm', 'mn': 'm', 'billion': 'b', 'bn': 'b', 'thousand': 'k', 'millions': 'm'}.get(suffix, suffix)
        alts = {value}
        if re.fullmatch(r'\d{1,3}\.\d{3}', digits):
            alts.add(Decimal(digits.replace('.', '')))  # 940.000 may be a thousands separator
        scaled = value * SCALE[suffix] if suffix in SCALE else None
        take(m, 'number', form='number', value=value, alts=alts, suffix=suffix if suffix in ('%', 'k', 'm', 'b') else None,
             scaled=scaled.normalize() if scaled is not None else None)
    found.sort(key=lambda v: v['start'])
    return found


# ------------------------------------------------------------------ lookup
class Evidence:
    """What one source offers the check: its text, file name and location label, indexed for value lookup and windows."""

    def __init__(self, text, name='', label='', parts=None):
        full = '\n'.join(x for x in (text or '', name or '', label or '') if x)
        self.flat = ' '.join(full.casefold().split())
        self.words = _index._words(full)
        self.numbers, self.dates, self.times = [], [], set()
        for value in extract_values(full):
            if value['type'] == 'date':
                self.dates.append((_literal(value['text']), value['parts']))
                for part in value['parts']:
                    if part[0] != 'Q' and part[0]:
                        self.numbers.append({'text': str(part[0]), 'value': Decimal(part[0]), 'alts': {Decimal(part[0])},
                                             'suffix': None, 'scaled': None})  # the year of a date counts as that number
            elif value.get('form') == 'time':
                self.times.add(value['key'])
            elif value['type'] == 'number':
                self.numbers.append(value)
        for word in set(self.words) & set(NUMBER_WORDS):  # a source that says 'three rooms' holds the number 3
            number = Decimal(NUMBER_WORDS[word])
            self.numbers.append({'text': word, 'value': number, 'alts': {number}, 'suffix': None, 'scaled': None})
        self.name_words = set(_index._words(f'{name} {label}'))
        self.sentences = []  # per part: [{'text', 'flat', 'words'}]
        for part_text, _ in (parts or [(text or '', None)]):
            sentences, pieces = [], []
            for line in (part_text or '').split('\n'):
                for piece in re.split(r'(?<=[.!?])\s+(?=[A-Z0-9"“(\[])', line):
                    if pieces and pieces[-1][1] and not _ends_sentence(pieces[-1][0][:-1], piece):
                        pieces[-1][0] += ' ' + piece  # 'No. DN-30981' is one sentence
                    else:
                        pieces.append([piece, True])
                if pieces:
                    pieces[-1][1] = False  # a line break always ends a sentence
            for piece, _ in pieces:
                piece = piece.strip()
                if piece:
                    folded = piece.casefold()
                    sentences.append({'text': piece, 'flat': re.sub(r'(?<=\d),(?=\d{3})', '', ' '.join(folded.split())),
                                      'words': {MONTH_SHORT.get(w, w) for w in _index._words(piece)}})
            self.sentences.append(sentences)


def evidence(source):
    return Evidence(source.get('text', ''), source.get('filename', ''),
                    ' '.join(x for x in (source.get('label'), source.get('location')) if x), source.get('parts'))


def _literal(text):
    text = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', text.casefold())
    return re.sub(r'\s+', ' ', re.sub(r'\bof\b|[,.]', ' ', text)).strip()


def _flat(text):
    return ' '.join(text.casefold().split())


def _exact(raw, flat):
    """An identifier, e-mail or file name written the same way (ignoring case and spacing), as a whole token."""
    needle = _flat(raw)
    return bool(needle) and re.search(r'(?<![\w.-])' + re.escape(needle) + r'(?![\w-])(?!\.\w)', flat) is not None


def _parts_match(a, b):
    """Parsed dates agree: same month, no conflicting year or day, at least two parts shared."""
    if a[0] == 'Q' or b[0] == 'Q':
        return a == b
    if a[1] is None or a[1] != b[1]:
        return False
    for x, y in ((a[0], b[0]), (a[2], b[2])):
        if x is not None and y is not None and x != y:
            return False
    return sum(1 for x, y in zip(a, b) if x is not None and y is not None) >= 2


def _number_match(a, b):
    if a['suffix'] and b['suffix'] and a['suffix'] != b['suffix']:
        same = False
    else:
        same = bool(a['alts'] & b['alts'])
    return same or (a['scaled'] is not None and a['scaled'] in (b['alts'] | ({b['scaled']} if b['scaled'] is not None else set())))


def _identifier_forms(raw):
    """The word sequences search accepts for an identifier (index.query_profile), so search and check agree."""
    forms = []
    for chunk_forms in _index.query_profile(raw).identifiers:
        forms.extend(chunk_forms)
    digits = re.findall(r'\d+', raw)
    if re.fullmatch(r'[\d\s().+-]+', raw) and len(digits) > 1:
        forms += [tuple(digits), (''.join(digits),)]  # a phone number written with other separators
    return list(dict.fromkeys(forms))


def lookup(value, ev):
    """'exact', 'equivalent' or None: is ``value`` present in the evidence of one source?"""
    kind, form = value['type'], value.get('form')
    if kind == 'number' and form == 'time':
        return 'exact' if value['key'] in ev.times else None
    if kind == 'number':  # compared with the source's own numbers, so 3 is never found inside 2023, 3.5 or a date
        raw = _flat(value['text'])
        matches = [n for n in ev.numbers if _number_match(value, n)]
        return ('exact' if any(_flat(n['text']) == raw for n in matches) else 'equivalent') if matches else None
    if _exact(value['text'], ev.flat):
        return 'exact'
    if kind == 'date':
        literal = _literal(value['text'])
        if any(literal == lit for lit, _ in ev.dates):
            return 'exact'
        if any(_parts_match(a, b) for a in value['parts'] for _, parts in ev.dates for b in parts):
            return 'equivalent'
        if value['parts'][0][0] == 'Q':  # Q3 2024 written as 'Q3 … 2024'
            _, year, quarter = value['parts'][0]
            return 'equivalent' if _index._contains(ev.words, (f'q{quarter}',)) and str(year) in ev.words else None
        return None
    if form in ('email', 'file'):
        return None
    if any(_index._contains(ev.words, f) for f in _identifier_forms(value['text'])):
        return 'equivalent'
    return None


def _needles(value):
    """Short strings that show a value is in a sentence (to pick the window; the verdict uses lookup)."""
    raw = ' '.join(value['text'].casefold().split())
    out = {raw, re.sub(r'(?<=\d),(?=\d{3})', '', raw).lstrip('$€£¥₹ ')}
    if value['type'] == 'number' and value.get('form') == 'number':
        out |= {format(a, 'f') for a in value['alts']}
    return {n for n in out if n}


# ------------------------------------------------------------------ support
def _variants(word):
    return set(_index._variants(word)) | {word + 's', word + 'es'}


def _found(claim_words, words):
    return sum(1 for w in claim_words if _variants(w) & words)


def _window(claim, ev):
    """{'part', 'sentence', 'text', 'found'}: the best window of a source for a claim."""
    best, best_key = None, None
    for p, sentences in enumerate(ev.sentences):
        for i, sentence in enumerate(sentences):
            hits = sum(1 for needles in claim['needles'] if any(n in sentence['flat'] for n in needles))
            key = (hits, _found(claim['words'], sentence['words']), -len(sentence['text']))
            if best_key is None or key > best_key:
                best, best_key = (p, i), key
    if best is None:
        return {'part': None, 'sentence': '', 'text': '', 'found': _found(claim['words'], ev.name_words)}
    p, i = best
    sentences = ev.sentences[p]
    # One sentence either side; short lines (forms, table rows, slides) read on while the window stays short.
    low, high = max(0, i - 1), min(len(sentences), i + 2)
    size = sum(len(s['text']) + 1 for s in sentences[low:high])
    while size < WINDOW_CHARS // 2 and (low > 0 or high < len(sentences)):
        if low > 0:
            low -= 1
            size += len(sentences[low]['text']) + 1
        if high < len(sentences) and size < WINDOW_CHARS // 2:
            high += 1
            size += len(sentences[high - 1]['text']) + 1
    around = sentences[low:high]
    words = set(ev.name_words).union(*(s['words'] for s in around))
    text = ' '.join(s['text'] for s in around)
    if len(text) > WINDOW_CHARS:  # keep the best sentence whole and trim its neighbours
        core = sentences[i]['text'][:WINDOW_CHARS]
        room = max(0, WINDOW_CHARS - len(core)) // 2
        before = ' '.join(s['text'] for s in sentences[low:i])[-room:] if room else ''
        after = ' '.join(s['text'] for s in sentences[i + 1:high])[:room] if room else ''
        text = ' '.join(x for x in (before, core, after) if x)
    return {'part': p, 'sentence': sentences[i]['text'], 'text': text, 'found': _found(claim['words'], words)}


def _word_support(claim, window):
    total = len(claim['words'])
    share = window['found'] / total if total else 0.0
    return round(share, 3), window['found'] >= WORD_MIN and share >= WORD_SHARE


def support_min(reranker):
    info = getattr(reranker, 'info', None)
    return float((info or {}).get('support_min', SUPPORT_MIN)) if isinstance(info, dict) else SUPPORT_MIN


def _rerank(reranker, pairs):
    if hasattr(reranker, 'score_pairs'):
        return list(reranker.score_pairs(pairs))
    scores, by_query = [None] * len(pairs), {}
    for n, (query, passage) in enumerate(pairs):
        by_query.setdefault(query, []).append((n, passage))
    for query, items in by_query.items():
        for (n, _), score in zip(items, reranker.score(query, [p for _, p in items])):
            scores[n] = score
    return scores


# ------------------------------------------------------------------ the check
def check(answer, sources, question='', reranker=None, not_found=NOT_FOUND):
    """Check ``answer`` (markers = source numbers) against ``sources`` ({number: source}). Returns Checked:
    answer   the checked answer, citations renumbered by first appearance (or ``not_found`` when refused)
    proof    {'units', 'claims', 'verified', 'method', 'ms'} (M5_DESIGN 3.7; spans and cites refer to ``answer``,
             or to ``unverified_text`` when refused)
    cited    [{'number': source number, 'part': index into its parts or None, 'highlight': sentence}] in display order
    refused  None or 'unverified'
    unverified_text  the model's text without citations, when refused
    flags    [{'start', 'end', 'sentence', 'reasons', 'values'}] for the flagged claims of ``answer``"""
    started = time.perf_counter()
    answer = strip_search((answer or '').strip())
    units, body_end = split_units(answer)
    rest = answer[body_end:]
    numbers = [n for n in sources]
    evidences = {n: evidence(s) for n, s in sources.items()}
    asked = Evidence(question or '')

    claims = []
    for unit in units:
        # Only claims keep citations: a no_info sentence or a heading never carries one.
        unit.update(status='ok', reasons=[], values=[], support=None, final=[])
        if unit['kind'] != 'claim':
            continue
        unit['final'] = [n for n in unit['cites'] if n in sources]
        values = [v for v in extract_values(unit['text']) if not lookup(v, asked)]
        bare = plain(unit['text'])
        claim = {'unit': unit, 'values': values, 'words': content_words(bare), 'text': bare,
                 'needles': [_needles(v) for v in values], 'windows': {}, 'scores': {}}
        claim['found'] = {n: [lookup(v, evidences[n]) for v in values] for n in numbers}
        claim['windows'] = {n: _window(claim, evidences[n]) for n in numbers}
        claims.append(claim)

    # Support scores: words for every pair; the reranker for the pairs that decide something (in one call, capped).
    smin = support_min(reranker)
    method = 'words'
    for claim in claims:
        for n in numbers:
            claim['scores'][n] = _word_support(claim, claim['windows'][n]) + ('words',)
    if reranker is not None and claims:
        wanted, later = [], []
        for claim in claims:  # in answer order: first the pairs whose verdict depends on the reranker
            cited = claim['unit']['final']
            undecided = not cited or not all(claim['scores'][n][1] for n in cited)
            cover = _value_cover(claim)
            if cover is None and undecided:
                wanted += [(claim, n) for n in cited]
                others = sorted((n for n in numbers if n not in cited), key=lambda n: -claim['scores'][n][0])
                wanted += [(claim, n) for n in others[:ALTERNATIVES + (1 if not cited else 0)]]
            else:
                later += [(claim, n) for n in (cover or cited)]  # decided already (values, or words): scored for the record
        wanted += later
        seen, pairs = set(), []
        for claim, n in wanted:
            if (id(claim), n) not in seen and claim['windows'][n]['text'] and len(pairs) < PAIR_CAP:
                seen.add((id(claim), n))
                pairs.append((claim, n))
        try:
            scores = _rerank(reranker, [(c['text'], c['windows'][n]['text']) for c, n in pairs])
        except Exception as error:  # noqa: BLE001 - a reranker failure falls back to words
            log.warning('Answer check: reranker failed (%s); using words', error)
            scores = []
        for (claim, n), score in zip(pairs, scores):
            if score is not None:
                # The reranker can only add support the words already show: a short follow-up ("It was for team
                # breakfast") reads as irrelevant to it without the sentence before, yet every word is in the row.
                claim['scores'][n] = (round(float(score), 3), float(score) >= smin or claim['scores'][n][1], 'reranker')
                method = 'reranker'

    for claim in claims:
        _decide(claim, numbers)

    # Verdict and refusal.
    claim_units = [u for u in units if u['kind'] == 'claim']
    verified = sum(1 for u in claim_units if u['status'] == 'ok')
    surviving = sum(len(u['final']) for u in units)
    refused = None
    if not claim_units and any(u['kind'] == 'no_info' for u in units):
        refused = 'unverified'  # only says the files don't cover it
    elif claim_units and not verified and not surviving:
        refused = 'unverified'

    text, order = _rebuild(answer, units, rest, keep=refused is None)
    cited = []
    for number in order:
        best = max(((c['scores'][number][0], c['windows'][number]) for c in claims if number in c['unit']['final']),
                   key=lambda item: item[0], default=(None, None))[1]
        cited.append({'number': number, 'part': best['part'] if best else None, 'highlight': best['sentence'] if best else ''})
    display = {n: d for d, n in enumerate(order, 1)}
    proof_units = []
    for unit in units:
        proof_units.append({'start': unit['new_start'], 'end': unit['new_end'], 'text': unit['new_text'], 'kind': unit['kind'],
                            'cites': [display[n] for n in unit['final'] if n in display], 'status': unit['status'],
                            'reasons': unit['reasons'],
                            'values': [dict(v, source=display.get(v['source'])) for v in unit['values']],
                            'support': unit['support']})
    proof = {'units': proof_units, 'claims': len(claim_units), 'verified': verified, 'method': method,
             'ms': round((time.perf_counter() - started) * 1000, 1)}
    if refused:
        return Checked(not_found, proof, [], refused, text, [])
    flags = [{'start': u['start'], 'end': u['end'], 'sentence': u['text'], 'reasons': u['reasons'],
              'values': [v['text'] for v in u['values'] if v['status'] == 'missing']}
             for u in proof_units if u['status'] == 'flagged']
    return Checked(text, proof, cited, None, None, flags)


def _value_cover(claim):
    """For a claim with values found somewhere: the sources to cite (cited ones holding a value first, then the fewest
    others that hold the rest). None when the claim has no value found in any source."""
    values, found, scores = claim['values'], claim['found'], claim['scores']
    if not values or not any(any(found[n]) for n in found):
        return None
    cited = [n for n in claim['unit']['final'] if n in found]
    keep = [n for n in cited if any(found[n])]
    covered = {i for n in keep for i, status in enumerate(found[n]) if status}
    others = [n for n in found if n not in keep]

    def holds(n, i):  # a source the claim does not cite is added for a weak value only if its words also support the claim
        return bool(found[n][i]) and (not _weak(values[i]) or scores[n][1])
    while True:
        missing = {i for i, _ in enumerate(values) if i not in covered and any(holds(n, i) for n in others)}
        if not missing:
            break
        best = max(others, key=lambda n: (len({i for i in missing if holds(n, i)}), -others.index(n)))
        keep.append(best)
        others.remove(best)
        covered |= {i for i, status in enumerate(found[best]) if status}
    return keep or None


def _weak(value):
    """A small whole number or a bare year: in too many passages to place a citation on its own ('3', '2027')."""
    if value['type'] != 'number' or value.get('form') != 'number' or value.get('suffix') or not value['text'][:1].isdigit():
        return False
    number = value['value']
    return number == number.to_integral_value() and (number < 100 or 1900 <= number <= 2100)


def _decide(claim, numbers):
    unit, scores = claim['unit'], claim['scores']
    cited = list(unit['final'])
    cover = _value_cover(claim)
    if cover is not None:
        final = cover  # values decide: kept, added, and cited sources without any value dropped
    elif len(claim['words']) < WORD_MIN and not claim['values']:
        final = cited  # too short to judge ("Friday [1].")
    else:
        final = []
        spare = sorted((n for n in numbers if n not in cited and scores[n][1]), key=lambda n: -scores[n][0])
        for n in cited:
            if scores[n][1]:
                final.append(n)
            elif spare:
                final.append(spare.pop(0))  # moved to the best supporting source
        if not cited and spare:
            final.append(spare[0])
        final = list(dict.fromkeys(final))
    unit['final'] = final
    unit['moves'] = _moves(cited, final)
    for i, value in enumerate(claim['values']):
        holders = [n for n in final if claim['found'][n][i]]  # a value only in a source the claim does not cite is missing
        status = claim['found'][holders[0]][i] if holders else 'missing'
        unit['values'].append({'text': value['text'], 'type': value['type'], 'status': status, 'source': holders[0] if holders else None})
    if any(v['status'] == 'missing' for v in unit['values']):
        unit['reasons'].append('value_not_in_sources')
    if not final:
        unit['reasons'].append('no_source')
    unit['status'] = 'flagged' if unit['reasons'] else 'ok'
    unit['support'] = max((scores[n][0] for n in final), default=None)


def _moves(cited, final):
    """{old: new or None}: how the unit's markers change; sources in ``final`` that no marker carries are added."""
    moves, added = {}, [n for n in final if n not in cited]
    for n in cited:
        if n in final:
            continue
        moves[n] = added.pop(0) if added else None
    return moves


def _rewrite(text, unit, display=None, keep=True):
    """The unit's text with its markers changed (moved, dropped, added) and, with ``display``, renumbered."""
    final = unit['final']
    moves = unit.get('moves', {})
    out, pos, carried = [], 0, set()
    for start, end, n in markers(text):
        out.append(text[pos:start])
        pos = end
        new = moves.get(n, n) if n in final or n in moves else None
        if new is not None and keep:
            carried.add(new)
            out.append(f'[{display[new] if display else new}]')
    out.append(text[pos:])
    new_text = ''.join(out)
    extra = [n for n in final if n not in carried] if keep else []
    if extra:
        tags = ''.join(f'[{display[n] if display else n}]' for n in extra)
        trailing = re.search(r'(?:\s*\[\d{1,3}\])+\s*$', new_text)
        stop = re.search(r'\s*[.!?…]+["”’)*_]*\s*$', new_text)
        if trailing:
            new_text = new_text.rstrip() + tags
        elif stop:
            new_text = new_text[:stop.start()] + ' ' + tags + new_text[stop.start():].lstrip()
        else:
            new_text = new_text.rstrip() + ' ' + tags
    if new_text != text:
        new_text = re.sub(r'(\[\d+\])(\s*\1)+', r'\1', new_text)
        new_text = re.sub(r'[ \t]+(?=[.!?,;:])', '', re.sub(r'[ \t]{2,}', ' ', new_text)).strip()
    return new_text


def _rebuild(answer, units, rest, keep=True):
    """(text, order): the answer with every unit rewritten and citations numbered by first appearance."""
    order = []
    if keep:
        for unit in units:
            probe = _rewrite(unit['text'], unit)
            for _, _, n in markers(probe):
                if n not in order:
                    order.append(n)
    display = {n: d for d, n in enumerate(order, 1)}
    out, pos, length = [], 0, 0
    for unit in units:
        gap = answer[pos:unit['start']]
        out.append(gap)
        length += len(gap)
        new = _rewrite(unit['text'], unit, display, keep)
        unit['new_start'], unit['new_end'], unit['new_text'] = length, length + len(new), new
        out.append(new)
        length += len(new)
        pos = unit['end']
    out.append(answer[pos:len(answer) - len(rest)])
    body = ''.join(out).rstrip()
    if rest and not BEYOND_LINE.sub('', rest).strip(' *_:\n'):
        rest = ''  # an empty "Beyond your files" heading
    text = (body + ('\n\n' + rest.lstrip() if rest else '')).strip()
    lead = len(''.join(out)) - len(''.join(out).lstrip())
    if lead:
        for unit in units:
            unit['new_start'] -= lead
            unit['new_end'] -= lead
    return text, order
