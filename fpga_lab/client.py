"""Talk to one board console (fpga-remote) over its HTTP and Socket.IO API.

The console URL contains the board's secret prefix. It is read from a file at
the moment it is needed and never printed, logged or stored anywhere else:
errors carry the path, not the URL.
"""

import os
import threading
import time
from urllib.parse import urlsplit

import requests


class ConsoleError(Exception):
    def __init__(self, status, code, message, body=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.body = body or {}


class Console:
    def __init__(self, base_url, timeout=20, session=None, name='board'):
        self.base = base_url.rstrip('/')
        parts = urlsplit(self.base)
        self.origin = f'{parts.scheme}://{parts.netloc}'
        self.prefix = parts.path.rstrip('/')
        self.timeout = timeout
        self.http = session or requests.Session()
        self.name = name

    def __repr__(self):
        # Never the URL: it carries the board's secret prefix.
        return f'<Console {self.name}>'

    # ------------------------------------------------------------- HTTP core

    def _req(self, method, path, token=None, ok=(200, 201), **kw):
        headers = kw.pop('headers', {}) or {}
        if token:
            headers['X-Lease-Token'] = token
        try:
            r = self.http.request(method, self.base + path, headers=headers,
                                  timeout=kw.pop('timeout', self.timeout), **kw)
        except requests.RequestException as e:
            raise ConsoleError(0, 'unreachable', f'{self.name}: {type(e).__name__}') from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code not in ok:
            raise ConsoleError(r.status_code, body.get('code', 'http_error'),
                               body.get('error', f'HTTP {r.status_code}'), body)
        return body

    # ----------------------------------------------------------------- reads

    def state(self):
        return self._req('GET', '/api/state')

    def health(self):
        return self._req('GET', '/api/health')

    def lease(self):
        return self._req('GET', '/api/lease')

    def files(self, kind):
        return [f['name'] for f in self._req('GET', f'/api/{kind}').get('files', [])]

    # ---------------------------------------------------------------- leases

    def take(self, holder, purpose='', kind='agent', minutes=None, ticket=None):
        body = {'holder': holder, 'purpose': purpose, 'kind': kind}
        if minutes is not None:
            body['minutes'] = minutes
        if ticket:
            body['ticket'] = ticket
        return self._req('POST', '/api/lease', json=body)

    def renew(self, token, minutes=None):
        body = {} if minutes is None else {'minutes': minutes}
        return self._req('POST', '/api/lease/renew', token=token, json=body)

    def keep(self, token):
        return self._req('POST', '/api/lease/keep', token=token, json={})

    def release(self, token):
        return self._req('DELETE', '/api/lease', token=token, json={})

    def queue_join(self, holder, purpose='', kind='person'):
        return self._req('POST', '/api/lease/queue',
                         json={'holder': holder, 'purpose': purpose, 'kind': kind})

    def queue_leave(self, ticket):
        return self._req('DELETE', '/api/lease/queue', json={'ticket': ticket})

    def set_reservations(self, hub_token, items):
        return self._req('PUT', '/api/lease/reservations',
                         headers={'X-Hub-Token': hub_token},
                         json={'reservations': items})

    # ------------------------------------------------------------- hardware

    def upload(self, kind, path, name, token=None):
        with open(path, 'rb') as fh:
            return self._req('POST', f'/api/{kind}/upload', token=token,
                             files={'file': (name, fh)}, data={'name': name},
                             timeout=300)

    def flash(self, name, token, volatile=False):
        return self._req('POST', '/api/flash-bitstream', token=token,
                         json={'filename': name, 'volatile': volatile})

    def load(self, name, sha256, token):
        return self._req('POST', '/api/load-image', token=token,
                         json={'filename': name, 'sha256': sha256})

    def wait_for(self, key, done=('done',), failed=('error',), timeout=900,
                 poll=2.0, tick=None):
        """Poll /api/state until state[key] is in done or failed. tick() is
        called every poll (used to renew the lease while waiting)."""
        t0 = time.time()
        while True:
            st = self.state()
            v = st.get(key)
            if v in done:
                return st
            if v in failed:
                raise ConsoleError(0, f'{key}_{v}', f'{key} ended in {v}', st)
            if time.time() - t0 > timeout:
                raise ConsoleError(0, 'timeout', f'{key} still {v} after {timeout}s', st)
            if tick:
                tick()
            time.sleep(poll)

    def socket(self, token=None):
        return Live(self, token)


class Live:
    """A Socket.IO connection to the console: power control and UART capture.

    Long-polling only: it needs no websocket library quirks, and an Origin
    header the console accepts is set explicitly (exactly one).
    """

    def __init__(self, console, token=None):
        import socketio   # imported here so HTTP-only users need not install it
        self.c = console
        self.token = token
        self.sio = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
        self.lock = threading.Lock()
        self.uart = []
        self.power = None
        self.refused = []
        self.bound = threading.Event()
        self.powered = threading.Event()

        @self.sio.on('connect')
        def _on_connect():
            if self.token:
                self.sio.emit('lease_bind', {'lease_token': self.token})

        @self.sio.on('lease_bound')
        def _on_bound(d):
            if d.get('holder'):
                self.bound.set()

        @self.sio.on('uart_data')
        def _on_uart(d):
            with self.lock:
                self.uart.append(d.get('text', ''))

        @self.sio.on('power_state')
        def _on_power(d):
            self.power = d.get('state')
            self.powered.set()

        @self.sio.on('lease_refused')
        def _on_refused(d):
            self.refused.append(d)

    def __enter__(self):
        self.sio.connect(self.c.origin, socketio_path=self.c.prefix + '/socket.io',
                         headers={'Origin': self.c.origin}, transports=['polling'],
                         wait=True, wait_timeout=20)
        if self.token and not self.bound.wait(10):
            raise ConsoleError(0, 'not_bound', 'the console did not accept the lease token')
        self.powered.wait(5)
        return self

    def __exit__(self, *exc):
        try:
            self.sio.disconnect()
        except Exception:
            pass

    def text(self):
        with self.lock:
            return ''.join(self.uart)

    def set_power(self, on, timeout=10):
        want = 'on' if on else 'off'
        if self.power == want:
            return
        self.powered.clear()
        self.sio.emit('power_toggle')
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.refused:
                d = self.refused.pop()
                raise ConsoleError(423, d.get('code', 'refused'), d.get('error', 'refused'), d)
            if self.power == want:
                return
            self.powered.wait(0.5)
        raise ConsoleError(0, 'timeout', f'power did not turn {want}')


def read_url(path):
    """Read a console URL from a secret file. The error names the file only."""
    p = os.path.expanduser(path)
    try:
        with open(p) as fh:
            url = fh.read().strip()
    except OSError as e:
        raise ConsoleError(0, 'no_url', f'cannot read console URL file {path}: {e.strerror}')
    if not url.startswith(('http://', 'https://')):
        raise ConsoleError(0, 'no_url', f'console URL file {path} does not hold a URL')
    return url
