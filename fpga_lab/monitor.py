"""Background duties of the hub: watch the boards, push bookings, record
usage, raise and clear alerts, and dispatch queued jobs.

Each duty is a method that does one pass, so tests call it directly; the hub
runs them on timers.
"""

import json
import threading
import time

import requests

from .client import ConsoleError
from .runner import Runner

UNREACHABLE_AFTER = 5          # consecutive failed polls (1/min) before alerting
STALE_HEALTH_S = 600
PUSH_WINDOW_S = 7 * 86400


def _optional(fn, fallback):
    try:
        return fn()
    except ConsoleError as e:
        if e.status == 404:
            return fallback
        raise


class AlertSink:
    """Log always; post to a chat webhook (Slack-compatible) when one is set."""

    def __init__(self, log_path, webhook_url=None, post=None):
        self.log_path = log_path
        self.webhook_url = webhook_url
        self.post = post or (lambda url, body: requests.post(url, json=body, timeout=10))

    def __call__(self, kind, key, message):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {kind.upper()} {key}: {message}"
        with open(self.log_path, 'a') as fh:
            fh.write(line + '\n')
        if self.webhook_url:
            try:
                prefix = ':rotating_light:' if kind == 'open' else ':white_check_mark:'
                self.post(self.webhook_url, {'text': f'{prefix} {message}'})
            except Exception:
                pass


class Monitor:
    def __init__(self, db, consoles, cfg, hub_token, sink, clock=time.time):
        self.db = db
        self.consoles = consoles
        self.cfg = cfg
        self.hub_token = hub_token
        self.sink = sink
        self.clock = clock
        self.status = {}                     # board -> last snapshot for the page
        self.fails = {b: 0 for b in consoles}
        self.running = {}                    # board -> job id
        self.lock = threading.Lock()
        self.runner = Runner(db, consoles, cfg['boards'], cfg['data_dir'], clock=clock)

    # --------------------------------------------------------------- alerts

    def _alert(self, key, board, message):
        if self.db.open_alert(key, board, message):
            self.sink('open', key, message)

    def _clear(self, key, message):
        if self.db.close_alert(key):
            self.sink('close', key, message)

    # ------------------------------------------------------------------ poll

    def poll(self):
        """One pass over every board: status for the page, usage, alerts."""
        now = self.clock()
        for b, c in self.consoles.items():
            snap = {'board': b, 'checked': now}
            try:
                state = c.state()
                # A console older than the lease code answers 404 here; that
                # is "no leases yet", not "unreachable".
                lease = _optional(c.lease, {'lease': None, 'queue': [], 'offer': None,
                                            'reservations': [], 'enforce': 'absent'})
                health = _optional(c.health, {'verdict': None, 'reason': 'no /api/health'})
            except ConsoleError as e:
                self.fails[b] += 1
                snap.update(reachable=False, error=e.message)
                if self.fails[b] >= UNREACHABLE_AFTER:
                    self._alert(f'unreachable:{b}', b,
                                f'Board {b} console unreachable for {self.fails[b]} min')
                with self.lock:
                    self.status[b] = {**self.status.get(b, {}), **snap}
                continue
            if self.fails[b]:
                self._clear(f'unreachable:{b}', f'Board {b} console reachable again')
            self.fails[b] = 0
            L = lease.get('lease')
            snap.update(reachable=True, lease=L, queue=lease.get('queue', []),
                        offer=lease.get('offer'), enforce=lease.get('enforce'),
                        reservations=lease.get('reservations', []),
                        power=state.get('power'), nv_bitstream=state.get('nv_bitstream_name'),
                        loaded_image=state.get('loaded_image'),
                        health=health.get('verdict'), health_age=health.get('age_s'),
                        build=state.get('server_build'))
            with self.lock:
                self.status[b] = snap
            self.db.record_usage(b, int(now // 60), L['holder'] if L else None,
                                 L['kind'] if L else None)

            verdict, age = health.get('verdict'), health.get('age_s')
            if verdict not in ('ok', None) or (age is not None and age > STALE_HEALTH_S):
                why = f'verdict {verdict}' if verdict != 'ok' else f'health check silent for {age} s'
                self._alert(f'health:{b}', b, f'Board {b} health: {why} {health.get("reason") or ""}'.strip())
            else:
                self._clear(f'health:{b}', f'Board {b} health ok again')

            lim = lease.get('limits') or {}
            stuck_after = lim.get('idle_warn_s', 600) + lim.get('idle_release_s', 300) + 300
            if L and L.get('idle_s', 0) > stuck_after:
                self._alert(f'stuck:{b}', b,
                            f"Board {b}: lease of {L['holder']} idle {L['idle_s']} s and not released "
                            f"-- the console's lease watcher may have stopped")
            else:
                self._clear(f'stuck:{b}', f'Board {b}: lease watcher ok again')

    # ------------------------------------------------------------- bookings

    def push_bookings(self):
        """Send every board its upcoming bookings. The console enforces them."""
        if not self.hub_token:
            return
        now = self.clock()
        for b, c in self.consoles.items():
            if (self.status.get(b) or {}).get('enforce') == 'absent':
                continue                      # console has no lease code yet
            items = [{'id': str(x['id']), 'holder': x['holder'], 'purpose': x['purpose'],
                      'start': x['start'], 'end': x['end']}
                     for x in self.db.bookings(b, since=now) if x['start'] < now + PUSH_WINDOW_S]
            try:
                c.set_reservations(self.hub_token, items)
                self._clear(f'push:{b}', f'Board {b} accepts bookings again')
            except ConsoleError as e:
                self._alert(f'push:{b}', b, f'Board {b} refused the booking list: {e.message}')

    # ------------------------------------------------------------- dispatch

    def dispatch(self, run_async=True):
        """Start every queued job that has a free board."""
        started = []
        for job in self.db.next_queued():
            with self.lock:
                busy = set(self.running)
            b, why = self.runner.pick_board(job, busy=busy)
            if not b:
                if job.get('reason') != why:
                    self.db.set_job(job['id'], reason=f'waiting: {why}')
                continue
            with self.lock:
                if b in self.running:
                    continue
                self.running[b] = job['id']
            started.append((job['id'], b))
            if run_async:
                threading.Thread(target=self._run, args=(job, b), daemon=True).start()
            else:
                self._run(job, b)
        return started

    def _run(self, job, b):
        try:
            self.runner.run(job, b)
        finally:
            with self.lock:
                self.running.pop(b, None)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.status))
