#!/usr/bin/env python3
"""Deterministic DevEco HTTP fixture; never invokes a model or edits task workspaces."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import base64
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import uuid

root = Path(os.environ['FAKE_DEVECO_HOME'])
root.mkdir(exist_ok=True)
mutex = threading.RLock()


def event(kind, **kw):
    with open(root / 'events.jsonl', 'a') as f:
        f.write(json.dumps(dict(kind=kind, time=time.time(), **kw)) + '\n')


def load(sid):
    return json.loads((root / (sid + '.json')).read_text())


def save(s):
    (root / (s['id'] + '.json')).write_text(json.dumps(s))


def update(s):
    if s.get('busy') and time.time() >= s['until']:
        s['busy'] = False
        text = s['text']
        error = {'name': 'APIError', 'data': {'message': 'provider failed'}} if 'FAIL' in text else None
        reply = dict(info=dict(role='assistant', id='msg_' + uuid.uuid4().hex,
                              parentID=s['mid'], sessionID=s['id'],
                              time={'completed': int(time.time() * 1000)},
                              tokens={'total': 11}, finish='length' if 'LENGTH' in text else 'stop'),
                     parts=[{'type': 'text', 'text': 'report: ' + text}])
        if error:
            reply['info']['error'] = error
        s['messages'].append(reply)
        save(s)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle_request(self, method):
        expected = 'Basic ' + base64.b64encode(
            ('deveco:' + os.environ['DEVECO_SERVER_PASSWORD']).encode()).decode()
        if self.headers.get('Authorization') != expected:
            self.send_error(401)
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        directory = urllib.parse.parse_qs(parsed.query).get('directory', [''])[0]
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
        code, result = 200, None
        with mutex:
            try:
                if path == '/doc':
                    result = {'paths': {k: {} for k in ('/session', '/session/status',
                        '/session/{sessionID}/prompt_async', '/session/{sessionID}/abort',
                        '/session/{sessionID}/message')}}
                elif path == '/session' and method == 'POST':
                    sid = 'ses_' + uuid.uuid4().hex
                    result = dict(id=sid, directory=directory, messages=[], busy=False)
                    save(result)
                    event('create', sid=sid, permission=body.get('permission'))
                elif path == '/session/status':
                    result = {}
                    for f in root.glob('ses_*.json'):
                        s = json.loads(f.read_text())
                        update(s)
                        if s['directory'] == directory and s['busy']:
                            result[s['id']] = {'type': 'busy'}
                else:
                    chunks = path.strip('/').split('/')
                    sid = chunks[1]
                    s = load(sid)
                    update(s)
                    if len(chunks) == 2:
                        result = s
                    elif chunks[2] == 'prompt_async':
                        if s['busy']:
                            code, result = 409, {'error': 'overlapping prompt'}
                        else:
                            text = body['parts'][0]['text']
                            s.update(busy=True, until=time.time() + (12 if 'SLOW' in text else .35),
                                     mid=body['messageID'], text=text)
                            s['messages'].append(dict(info=dict(role='user', id=s['mid']), parts=body['parts']))
                            save(s)
                            event('prompt', sid=sid, text=text, body=body)
                            code = 204
                    elif chunks[2] == 'abort':
                        event('abort', sid=sid)
                        if 'ABORT_FAIL' in s.get('text', ''):
                            result = False
                        else:
                            if s['busy']:
                                s['messages'].append(dict(info=dict(role='assistant', parentID=s['mid'],
                                    time={'completed': int(time.time() * 1000)},
                                    error={'name': 'MessageAbortedError'}, tokens={}), parts=[]))
                            s['busy'] = False
                            save(s)
                            result = True
                    elif chunks[2] == 'message':
                        result = s['messages']
                    else:
                        code, result = 404, {'error': path}
            except Exception as e:
                code, result = 500, {'error': repr(e)}
        data = json.dumps(result).encode() if code != 204 else b''
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self): self.handle_request('GET')
    def do_POST(self): self.handle_request('POST')


if __name__ == '__main__':
    args = sys.argv[1:]
    port = int(args[args.index('--port') + 1])
    event('server', pid=os.getpid())
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
