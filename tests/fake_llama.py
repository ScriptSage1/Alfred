"""A small stand-in for llama-server.exe, so unit tests need no model and no GPU.

    python fake_llama.py <llama-server arguments>

Speaks what Alfred uses: GET /health, GET /props, POST /v1/embeddings, POST /embeddings (pictures, with the media
marker), and a POST /v1/chat/completions stub (phase B). Like the real server it wants the key from LLAMA_API_KEY,
refuses an input longer than a slot reads with HTTP 500 "input (N tokens) is too large to process" (one character
counts as one token here), and answers a picture request with a wrong marker with HTTP 400.

Test hooks: --list-devices prints FAKE_LLAMA_DEVICES (lines separated by ';'); every start is appended, with its
arguments, to ALFRED_HOME\\fake_starts.jsonl; POST /_test/marker gives the server a new marker.
"""
import hashlib
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = sys.argv[1:]
DIM = 768


def value(flag, default=None):
    return ARGS[ARGS.index(flag) + 1] if flag in ARGS else default


if '--list-devices' in ARGS:
    print('Available devices:')
    for line in filter(None, os.environ.get('FAKE_LLAMA_DEVICES', '').split(';')):
        print('  ' + line)
    sys.exit(0)
if '--version' in ARGS:
    print('version: 0.0.0-fake (build 4242, commit fake)')
    sys.exit(0)

KEY = os.environ.get('LLAMA_API_KEY')
CONTEXT, SLOTS = int(value('-c', 4096)), int(value('-np', 1))
SLOT = CONTEXT // SLOTS
PICTURES = '--mmproj' in ARGS
STATE = {'marker': f'<__media_{secrets.token_hex(4)}__>'}
print(f'fake llama-server pid {os.getpid()}: {json.dumps(ARGS)}', flush=True)
print(f'srv    load_model: initializing, n_slots = {SLOTS}, n_ctx_slot = {SLOT}', flush=True)
home = os.environ.get('ALFRED_HOME')
if home:
    with open(os.path.join(home, 'fake_starts.jsonl'), 'a', encoding='utf-8') as starts:
        starts.write(json.dumps({'pid': os.getpid(), 'args': ARGS}) + '\n')


def vector(seed, scale=3.0):
    """A deterministic vector, not of unit length (Alfred must scale it)."""
    numbers = []
    block = seed.encode('utf-8', 'replace')
    while len(numbers) < DIM:
        block = hashlib.sha256(block).digest()
        numbers += [(b - 127.5) / 127.5 for b in block]
    return [x * scale for x in numbers[:DIM]]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorised(self):
        if KEY and self.headers.get('Authorization') != f'Bearer {KEY}':
            self.send(401, {'error': {'code': 401, 'message': 'Invalid API Key', 'type': 'authentication_error'}})
            return False
        return True

    def body(self):
        length = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(length) or b'null')

    def do_GET(self):
        if self.path == '/health':
            return self.send(200, {'status': 'ok'})
        if not self.authorised():
            return
        if self.path == '/props':
            return self.send(200, {'media_marker': STATE['marker'], 'is_sleeping': False, 'total_slots': SLOTS,
                                   'modalities': {'vision': PICTURES}})
        self.send(404, {'error': {'code': 404, 'message': 'File Not Found'}})

    def do_POST(self):
        payload = self.body()
        if self.path == '/_test/marker':
            STATE['marker'] = f'<__media_{secrets.token_hex(4)}__>'
            return self.send(200, {'marker': STATE['marker']})
        if not self.authorised():
            return
        if self.path == '/v1/embeddings':
            texts = payload['input'] if isinstance(payload['input'], list) else [payload['input']]
            for text in texts:
                if len(text) > SLOT:
                    return self.send(500, {'error': {'code': 500, 'type': 'server_error', 'message':
                                                     f'input ({len(text)} tokens) is too large to process. increase the physical batch size (current batch size: 2048)'}})
            print(f'embed {len(texts)}', flush=True)
            return self.send(200, {'object': 'list', 'data': [{'object': 'embedding', 'index': i, 'embedding': vector(t)}
                                                               for i, t in reversed(list(enumerate(texts)))]})
        if self.path == '/embeddings':
            if not PICTURES:
                return self.send(500, {'error': {'code': 500, 'message': 'image input is not supported by this server'}})
            results = []
            for i, item in enumerate(payload['content']):
                if item.get('prompt_string') != STATE['marker']:
                    return self.send(400, {'error': {'code': 400, 'message': 'number of bitmaps does not match number of markers'}})
                picture = vector('picture:' + item['multimodal_data'][0])
                results.append({'index': i, 'embedding': [vector('token'), picture]})  # one row per token; the last is the picture
            print(f'pictures {len(results)}', flush=True)
            return self.send(200, results)
        if self.path == '/v1/chat/completions':
            return self.send(200, {'object': 'chat.completion', 'choices': [{'index': 0, 'finish_reason': 'stop',
                                                                              'message': {'role': 'assistant', 'content': 'A fake answer.'}}]})
        self.send(404, {'error': {'code': 404, 'message': 'File Not Found'}})


if __name__ == '__main__':
    server = ThreadingHTTPServer((value('--host', '127.0.0.1'), int(value('--port', 8080))), Handler)
    server.daemon_threads = True
    time.sleep(float(os.environ.get('FAKE_LLAMA_LOAD_SECONDS', '0')))
    server.serve_forever()
