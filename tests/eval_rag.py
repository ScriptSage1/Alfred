"""Evaluate Ask Alfred on a folder of sample files (by default C:\\Users\\<you>\\AlfredSamples).

    .venv\\Scripts\\python tests\\eval_rag.py --home <scratch folder> [--answers ollama:gemma3:4b,claude_code:sonnet]
                                            [--rerankers none,bge-reranker-v2-m3] [--models <folder with reranker models>]

1. Retrieval: for each question, does the expected file (and the passage with the expected fact)
   reach the top 1 / 3 / the sources given to the model? With and without chunk headers (a copy of
   the index whose search text and vectors are the bare passages) and with and without a reranker.
2. Answers: correctness (the expected fact in the answer, citing the expected file), citation
   validity (raw [n] markers the model wrote that point at real sources), out-of-scope behaviour,
   and timing (first token, total) for each chat model.

It never touches your own index or settings: everything lives under --home.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alfred import ai, config, providers, rag, rerank  # noqa: E402
from alfred.index import Index, connect  # noqa: E402

# (question, history, expected file name fragment or None for out of scope, facts the answer should contain (any of))
QUESTIONS = [
    ('How much was approved in the Nebula budget review?', [], 'nebula-briefing', ['18,000', '18000']),
    ('When is the Project Nebula deadline?', [], 'nebula-briefing', ['December 5', 'Dec 5', '5 December']),
    ('Where can I collect the lost umbrella?', [], 'notice-board', ['desk 4']),
    ('What did the mechanic do to my car?', [], 'car-service', ['spark plug']),
    ('How much did I spend on groceries?', [], 'household-expenses', ['5400', '5,400']),
    ('What goes in the raised beds?', [], 'garden-club', ['omato']),
    ('Where does the Goa flight leave from?', [], 'travel-plan', ['Hyderabad']),
    ('Who is travelling on the Goa trip?', [], 'travel-plan', ['Moon']),
    ('What is the average online cart abandonment rate?', [], 'SwiftCart', ['70']),
    ('What architecture does SwiftCart use?', [], 'SwiftCart', ['three-tier', 'three tier', '3-tier']),
    ('When and where is the GSoC orientation event?', [], '15.12.55', ['Block 5', 'Seminar Hall']),
    ('How many packets were lost when pinging 192.168.2.1?', [], '11.20.54', ['1', '25%']),
    ('Which modules are covered in the ATM testing scope?', [], 'SE WEEK', ['Card reader', 'PIN']),
    ('What are the entry and exit criteria for testing the ATM?', [], 'SE WEEK', ['entry', 'Entry']),
    ('What are the non-functional requirements of the ATM system?', [], 'SE WEEK', ['secur', 'perform', 'availab']),
    ('Is there a photo of pasta?', [], '15.53.57', ['pasta']),
    ('What car is parked in the garage photo?', [], '01.03.51', ['BMW']),
    # one passage among many in a long document
    ('What happens after three wrong PIN attempts?', [], 'SE WEEK', ['retain the card']),
    ('Which testing tool is mentioned for the ATM test environment?', [], 'SE WEEK', ['Selenium']),
    ('What should a withdrawal receipt contain?', [], 'SE WEEK', ['Date and time', 'date and time']),
    ('What are the limitations and assumptions of the ATM system?', [], 'SE WEEK', ['one customer at a time']),
    ('What should the ATM do if it cannot dispense cash?', [], 'SE WEEK', ['must not debit', 'Unable to dispense']),
    ('Who is the example customer in the database design?', [], 'SE WEEK', ['Ravi']),
    ('What is the daily withdrawal limit test case about?', [], 'SE WEEK', ['daily limit', 'daily withdrawal limit']),
    # different words from the files
    ('How much did power cost me?', [], 'household-expenses', ['2300', '2,300']),
    ('What vegetables is the gardening group planting?', [], 'garden-club', ['omato', 'chilli']),
    ('Has anyone found my umbrella?', [], 'notice-board', ['reception', 'desk 4']),
    ('What maintenance was done on the automobile?', [], 'car-service', ['spark plug', 'oil']),
    # follow-ups (the question only makes sense with the chat so far)
    ('and electricity?', [('How much did I spend on groceries?', 'Groceries cost 5400 [1].')], 'household-expenses', ['2300', '2,300']),
    ('when is its deadline?', [('What was approved in the Project Nebula budget review?', 'The budget review approved 18,000 rupees [1].')],
     'nebula-briefing', ['December 5', 'Dec 5']),
    ('what are its results?', [('What is SwiftCart?', 'SwiftCart is an e-commerce platform upgrade with guest checkout [1].')],
     'SwiftCart', ['%', 'faster', 'abandon']),
    # not in the files
    ('What is the capital of France?', [], None, []),
    ('What is my passport number?', [], None, []),
]
# Chats about one document (run when a file matching the first item is in the samples): the fact sits in a
# table on page 40 of a 233-page PDF whose other pages talk about credits and semesters in prose.
DOCUMENT_QUESTIONS = [
    ('CSE_AR22', "what are the subjects in third year's second semester", [], ['Internet of Things']),
    ('CSE_AR22', 'is there a table in document that lists their respective credits?',
     [("what are the subjects in third year's second semester", 'Third Year, Semester II has Internet of Things and '
       'Statistics for Machine Learning [1].')], ['No of Credits: 3', 'Credits: 3']),
]
REWRITTEN = {'and electricity?': 'How much did I spend on electricity?',
             'when is its deadline?': 'When is the Project Nebula deadline?',
             'what are its results?': 'What are the results of SwiftCart?',
             'is there a table in document that lists their respective credits?':
                 'Is there a table listing the credits of the third year second semester subjects?'}


def history_of(pairs):
    out = []
    for question, answer in pairs:
        out += [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer}]
    return out


def prepare(home, samples):
    settings = config.load(home)
    settings.update(tracked_folders=[str(samples)], embed_enabled=True, vision_enabled=True)
    settings.save()
    index = Index(settings)
    embedder = ai.Embedder.from_settings(settings)
    summary = index.update(embedder=embedder, vision=ai.Vision.from_settings(settings))
    print(f"Index: {index.status()['files']} files, {index.status()['segments']} passages ({summary['seconds']} s)")
    return settings, index, embedder


def bare_copy(settings, embedder, home):
    """A copy of the index whose search text and vectors are the bare passages (no headers)."""
    target = Path(home) / 'bare'
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    shutil.copy(settings.db_path, target / 'index.sqlite')
    for suffix in ('-wal', '-shm'):
        if Path(str(settings.db_path) + suffix).exists():
            shutil.copy(str(settings.db_path) + suffix, str(target / 'index.sqlite') + suffix)
    bare = config.load(target)
    bare.update({k: v for k, v in settings.items() if k in config.DEFAULTS})
    bare.save()
    index = Index(bare)
    index.header = lambda path, kind, loc: ''  # what retrieval adds to candidates
    con = connect(bare.db_path)
    rows = list(con.execute('SELECT id, path, text FROM segments'))
    with con:
        con.execute('UPDATE fts SET text = (SELECT text FROM segments WHERE segments.id = fts.rowid)')
        con.execute('DELETE FROM vectors')
    vectors = []
    for start in range(0, len(rows), embedder.batch):
        batch = rows[start:start + embedder.batch]
        vectors += embedder.embed_documents([t for _, _, t in batch], [Path(p).stem for _, p, _ in batch])
    import numpy as np
    with con:
        for (segment_id, _, _), vector in zip(rows, vectors):
            array = np.asarray(vector, dtype=np.float32)
            array /= max(float(np.linalg.norm(array)), 1e-12)
            con.execute('INSERT INTO vectors VALUES(?,?,?)', (segment_id, array.size, array.tobytes()))
        con.execute("INSERT OR REPLACE INTO meta VALUES('vectors_version', ?)", (str(time.time()),))
    con.close()
    return index


def document(index, fragment):
    """The indexed file whose name contains ``fragment`` (None when the samples don't have it)."""
    con = connect(index.db_path)
    try:
        return next((p for (p,) in con.execute('SELECT path FROM files') if fragment in Path(p).name), None)
    finally:
        con.close()


def cases(index):
    """(question, history pairs, expected file fragment, facts, scope path or None) for everything that can run."""
    out = [(q, pairs, expected, facts, None) for q, pairs, expected, facts in QUESTIONS]
    for fragment, question, pairs, facts in DOCUMENT_QUESTIONS:
        scope = document(index, fragment)
        if scope:
            out.append((question, pairs, fragment, facts, scope))
    return out


def retrieval(index, embedder, reranker, label, budget=6000 * rag.CHARS_PER_TOKEN):
    rows = []
    for question, pairs, expected, facts, scope in cases(index):
        if expected is None:
            continue
        query = REWRITTEN[question] if pairs else question  # judged on the standalone question a rewrite should produce
        started = time.time()
        ranked, _, _ = rag.gather(index, [query], embedder, reranker, scope)
        sources = rag.build_sources(index, rag.choose(index, ranked, scope), int(budget))
        seconds = time.time() - started
        files = [Path(c['path']).name for c in ranked]
        given = [Path(s['path']).name for s in sources]
        has = lambda text: any(f.lower() in text.lower() for f in facts)
        first = next((n for n, name in enumerate(files, 1) if expected in name), None)
        passage = next((n for n, c in enumerate(ranked, 1) if expected in Path(c['path']).name and has(c['text'])), None)
        fact = any(has(s['text']) for s in sources if expected in Path(s['path']).name)
        rows.append({'question': question, 'rank': first, 'passage': passage, 'given': expected in ' '.join(given), 'fact': fact,
                     'precision': sum(expected in g for g in given) / len(given) if given else 0.0, 'seconds': seconds,
                     'sources': len(sources), 'chars': sum(len(s['text']) for s in sources)})
    n = len(rows)
    pct = lambda value: round(100 * value / n, 1)
    stats = {'config': label, 'file@1': pct(sum(r['rank'] == 1 for r in rows)),
             'passage@1': pct(sum(r['passage'] == 1 for r in rows)), 'passage@3': pct(sum(bool(r['passage']) and r['passage'] <= 3 for r in rows)),
             'passage MRR': round(sum(1 / r['passage'] for r in rows if r['passage']) / n, 3),
             'fact in context': pct(sum(r['fact'] for r in rows)), 'context precision': pct(sum(r['precision'] for r in rows)),
             'sources': round(statistics.mean(r['sources'] for r in rows), 1), 'context chars': int(statistics.mean(r['chars'] for r in rows)),
             'ms': int(1000 * statistics.median(r['seconds'] for r in rows))}
    misses = [(r['question'], r['passage']) for r in rows if r['passage'] != 1]
    return stats, misses


class Recording:
    """Wraps a provider to keep the raw text the model wrote (before citations are cleaned)."""

    def __init__(self, provider):
        self.provider, self.raw = provider, []

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def stream(self, messages, on_token=lambda t: None, cancel=None, on_status=lambda text: None):
        parts = []
        text = self.provider.stream(messages, on_token=lambda t: (parts.append(t), on_token(t)), cancel=cancel, on_status=on_status)
        self.raw.append(''.join(parts) or text)
        return text


def answers(index, settings, embedder, reranker, spec):
    kind, _, model = spec.partition(':')
    provider = providers.for_settings(dict(settings), kind, model)
    rows = []
    every = cases(index)
    for question, pairs, expected, facts, scope in every:
        recording = Recording(provider)
        started = time.time()
        try:
            result = rag.ask(index, settings, question, history_of(pairs), provider=recording, embedder=embedder, reranker=reranker,
                             only_path=scope)
        except Exception as error:
            rows.append({'question': question, 'error': str(error)[:200]})
            print(f'  ! {question}: {error}')
            continue
        raw = recording.raw[-1] if recording.raw else ''
        offered = len(result.get('offered') or [])
        markers = [int(n) for group in re.findall(r'\[(\d+(?:\s*,\s*\d+)*)\]', raw.split('Beyond your files')[0])
                   for n in re.split(r'\s*,\s*', group)]
        answer = result['answer']
        files_part = answer.split('Beyond your files')[0]
        cited_files = [c['filename'] for c in result['citations']]
        if expected is None:
            correct = not result['citations'] and bool(re.search(r"(?i)(don.t|do not|doesn.t|does not|couldn.t|no )\b.{0,60}(file|mention|find|information|contain)", files_part))
        else:
            correct = any(f.lower() in files_part.lower() for f in facts) and any(expected in name for name in cited_files)
        rows.append({'question': question, 'correct': correct, 'markers': len(markers), 'valid_markers': sum(1 <= m <= offered for m in markers),
                     'cited': cited_files, 'first_token': result['timings'].get('first_token'), 'total': time.time() - started,
                     'rewrite': result['timings'].get('rewrite'), 'query': result.get('query'), 'beyond': result.get('beyond'),
                     'searched_again': result.get('searched'),
                     'answer': answer})
        print(f"  {'✓' if correct else '✗'} {question} → {answer[:110]!r} {cited_files} {round(time.time() - started, 1)} s")
    ok = [r for r in rows if 'error' not in r]
    markers = sum(r['markers'] for r in ok)
    in_scope = [r for r, q in zip(rows, every) if q[2] is not None and 'error' not in r]
    out_scope = [r for r, q in zip(rows, every) if q[2] is None and 'error' not in r]
    stats = {'model': spec, 'answered': len(ok), 'errors': len(rows) - len(ok),
             'correct (in files)': f"{sum(r['correct'] for r in in_scope)}/{len(in_scope)}",
             'out of scope handled': f"{sum(r['correct'] for r in out_scope)}/{len(out_scope)}",
             'raw citation validity': round(sum(r['valid_markers'] for r in ok) / markers, 3) if markers else None,
             'answers citing': f"{sum(bool(r['cited']) for r in in_scope)}/{len(in_scope)}",
             'first token s (median)': round(statistics.median(r['first_token'] for r in ok if r['first_token']), 2) if ok else None,
             'total s (median)': round(statistics.median(r['total'] for r in ok), 2) if ok else None,
             'total s (max)': round(max(r['total'] for r in ok), 2) if ok else None}
    return stats, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--samples', default=str(Path.home() / 'AlfredSamples'))
    parser.add_argument('--rerankers', default='none,' + rerank.DEFAULT)
    parser.add_argument('--answers', default='')
    parser.add_argument('--answer-reranker', default=rerank.DEFAULT)
    parser.add_argument('--models', default='', help='folder holding downloaded reranker models (default: under --home)')
    parser.add_argument('--out', default='')
    args = parser.parse_args()
    os.environ['ALFRED_HOME'] = args.home
    if args.models:
        rerank.folder = lambda name: Path(args.models) / name
    settings, index, embedder = prepare(args.home, args.samples)
    bare = bare_copy(settings, embedder, args.home)
    report = {'retrieval': [], 'misses': {}, 'answers': [], 'answer_rows': {}}
    loaded = {}
    for name in [r for r in args.rerankers.split(',') if r]:
        reranker = None
        if name != 'none':
            if not rerank.installed(name):
                print(f'(reranker {name} not downloaded; skipped)')
                continue
            reranker = loaded[name] = rerank.Reranker(name)
            print(f'Reranker {name} on {reranker.device}')
        for headers, ix in (('headers', index), ('no headers', bare)):
            stats, misses = retrieval(ix, embedder, reranker, f'{headers} · {name}')
            report['retrieval'].append(stats)
            report['misses'][stats['config']] = misses
            print(json.dumps(stats))
    answer_reranker = loaded.get(args.answer_reranker)
    for spec in [s for s in args.answers.split(',') if s]:
        print(f'Answers from {spec} (reranker: {answer_reranker.name if answer_reranker else "none"})')
        stats, rows = answers(index, settings, embedder, answer_reranker, spec)
        report['answers'].append(stats)
        report['answer_rows'][spec] = rows
        print(json.dumps(stats))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
