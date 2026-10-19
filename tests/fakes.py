"""An in-process stand-in for a board console, built on the console's own
lease rules (lease.py from fpga-remote, vendored by path in the tests), so
the hub is tested against the real lease semantics without hardware."""

import hashlib
import os
import sys

from fpga_lab.client import ConsoleError

LEASE_PY_DIR = os.environ.get('FPGA_REMOTE_DIR')
if LEASE_PY_DIR:
    sys.path.insert(0, LEASE_PY_DIR)
from lease import LeaseError, LeaseManager, Limits  # noqa: E402


class FakeLive:
    def __init__(self, console, token):
        self.c = console
        self.token = token

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    @property
    def power(self):
        return 'on' if self.c.power_on else 'off'

    def set_power(self, on):
        self.c._gate(self.token)
        self.c.power_on = bool(on)
        self.c.events.append(('power', bool(on)))
        if on and self.c.banner_on_power:
            pass

    def text(self):
        return ''.join(self.c.uart)


class FakeConsole:
    def __init__(self, name='board', clock=None, enforce='on', banner='HELLO\n'):
        kw = {'clock': clock} if clock else {}
        self.lm = LeaseManager(Limits(), **kw)
        self.name = name
        self.enforce = enforce
        self.files_ = {'bitstreams': {}, 'images': {}}
        self.nv_sha = None
        self.power_on = False
        self.uart = []
        self.events = []
        self.banner = banner
        self.banner_on_power = False
        self.unreachable = False
        self.health_ = {'verdict': 'ok', 'age_s': 5}
        self.reservations_seen = None
        self.load_fails = False

    def _check(self):
        if self.unreachable:
            raise ConsoleError(0, 'unreachable', f'{self.name}: ConnectionError')

    def _gate(self, token):
        if self.enforce == 'on' and not self.lm.check(token):
            raise ConsoleError(423, 'held', 'refused')
        if token:
            self.lm.touch(token)

    def _wrap(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except LeaseError as e:
            raise ConsoleError(409, e.code, e.message, e.info)

    # reads
    def lease(self):
        self._check()
        s = self.lm.snapshot()
        s['enforce'] = self.enforce
        return s

    def state(self):
        self._check()
        return {'power': 'on' if self.power_on else 'off', 'flash_state': 'done',
                'load_state': 'done', 'nv_bitstream_sha256': self.nv_sha,
                'nv_bitstream_name': None, 'server_build': 'fake', 'loaded_image': None}

    def health(self):
        self._check()
        return dict(self.health_)

    def files(self, kind):
        self._check()
        return list(self.files_[kind])

    # leases
    def take(self, holder, purpose='', kind='agent', minutes=None, ticket=None):
        self._check()
        L = self._wrap(self.lm.take, holder, purpose, kind=kind, minutes=minutes, ticket=ticket)
        self.events.append(('take', holder))
        return {'token': L['token'], 'until': L['until']}

    def renew(self, token, minutes=None):
        self._check()
        L = self._wrap(self.lm.renew, token, minutes)
        return {'until': L['until']}

    def keep(self, token):
        self.lm.touch(token)
        return {}

    def release(self, token):
        self._check()
        ended = self._wrap(self.lm.release, token)
        self.power_on = False
        self.events.append(('release', ended['holder']))
        return {'released': True}

    def queue_join(self, holder, purpose='', kind='person'):
        return self._wrap(self.lm.queue_join, holder, purpose, kind)

    def set_reservations(self, hub_token, items):
        self._check()
        self.reservations_seen = items
        return {'count': self._wrap(self.lm.set_reservations, items)}

    # hardware
    def upload(self, kind, path, name, token=None):
        with open(path, 'rb') as fh:
            self.files_[kind][name] = hashlib.sha256(fh.read()).hexdigest()
        self.events.append(('upload', name))
        return {'name': name}

    def flash(self, name, token, volatile=False):
        self._gate(token)
        assert not volatile, 'jobs must never flash volatile'
        self.nv_sha = self.files_['bitstreams'][name]
        self.events.append(('flash', name))
        return {'state': 'loading'}

    def load(self, name, sha256, token):
        self._gate(token)
        if self.files_['images'].get(name) != sha256:
            raise ConsoleError(409, 'http_error', 'sha mismatch')
        if not self.power_on:
            raise ConsoleError(409, 'http_error', 'board is off')
        self.events.append(('load', name))
        if not self.load_fails:
            self.uart.append(self.banner)
        return {'state': 'loading'}

    def wait_for(self, key, done=('done',), failed=('error',), timeout=900, poll=2.0, tick=None):
        if tick:
            tick()
        if key == 'load_state' and self.load_fails:
            raise ConsoleError(0, 'load_state_error', 'load_state ended in error')
        return self.state()

    def socket(self, token=None):
        return FakeLive(self, token)
