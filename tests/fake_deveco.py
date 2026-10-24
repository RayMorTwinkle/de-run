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
subscribers = []


def publish(directory, payload):
    for d, inbox in subscribers:
        if d == directory:
            inbox.put(payload)


def event(kind, **kw):
    with open(root / 'events.jsonl', 'a') as f:
        f.write(json.dumps(dict(kind=kind, time=time.time(), **kw)) + '\n')


def load(sid):
    return json.loads((root / (sid + '.json')).read_text())


def save(s):
    (root / (s['id'] + '.json')).write_text(json.dumps(s))


def fixture(directory, name, default):
    path = Path(directory) / (name + '.fixture.json')
    return json.loads(path.read_text()) if path.is_file() else default


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
        if path == '/event':
            self.stream_events(directory)
            return
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
        code, result = 200, None
        with mutex:
            try:
                if path == '/doc':
                    result = {'paths': {k: {} for k in ('/session', '/session/status',
                        '/session/{sessionID}/prompt_async', '/session/{sessionID}/abort',
                        '/session/{sessionID}/message', '/event')}}
                elif path == '/provider':
                    time.sleep(fixture(directory, 'provider-delay', 0))
                    result = fixture(directory, 'provider', dict(connected=['deveco'], default={'deveco': 'GLM-5.3'},
                                  all=[dict(id='deveco', models={'GLM-5.3': {'name': 'GLM 5.3'},
                                                               'GLM-5.1': {'name': 'GLM 5.1'}}),
                                       dict(id='disconnected', models={'hidden': {}})]))
                elif path == '/config':
                    result = fixture(directory, 'config', {})
                elif path == '/agent':
                    result = fixture(directory, 'agent', [{'name': 'build'}])
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
                            if body.get('model'):
                                s['model'] = {'providerID': body['model']['providerID'], 'id': body['model']['modelID']}
                            s['messages'].append(dict(info=dict(role='user', id=s['mid'], model=body.get('model')),
                                                      parts=body['parts']))
                            save(s)
                            event('prompt', sid=sid, text=text, body=body)
                            if 'EARLY_PROVIDER' in text:
                                # Real DevEco fails before creating even the user message.
                                s['messages'].pop()
                                s['busy'] = False
                                save(s)
                                publish(directory, dict(type='session.error', properties=dict(
                                    sessionID=sid, error={'name': 'UnknownError', 'data': {
                                        'message': 'ProviderNoProvidersError: no default provider'}})))
                            elif 'STREAM_LOST' in text:
                                publish(directory, None)
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

    def stream_events(self, directory):
        import queue
        inbox = queue.Queue()
        subscriber = (directory, inbox)
        with mutex:
            subscribers.append(subscriber)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        try:
            payload = dict(type='server.connected', properties={})
            while payload is not None:
                # Exercise multi-line SSE data and CRLF framing, not just JSON lines.
                data = json.dumps(payload, indent=2)
                self.wfile.write(('\r\n'.join('data: ' + line for line in data.splitlines()) +
                                  '\r\n\r\n').encode())
                self.wfile.flush()
                try:
                    payload = inbox.get(timeout=.2)
                except queue.Empty:
                    payload = dict(type='server.heartbeat', properties={})
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with mutex:
                subscribers.remove(subscriber)
            self.close_connection = True

    def do_GET(self): self.handle_request('GET')
    def do_POST(self): self.handle_request('POST')


if __name__ == '__main__':
    args = sys.argv[1:]
    port = int(args[args.index('--port') + 1])
    event('server', pid=os.getpid())
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
