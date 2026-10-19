"""Hub, run queue, bookings, usage and alerts against fake consoles that use
the console's real lease rules. Each behaviour has a firing and a quiet case."""

import hashlib
import os
import time
from urllib.parse import unquote

import pytest

from fakes import FakeConsole  # noqa: E402  (tests/ is on sys.path via conftest)
from fpga_lab import hub as hub_mod
from fpga_lab.db import DB
from fpga_lab.monitor import AlertSink, Monitor
from fpga_lab.runner import Runner, board_is_free, file_name


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.01)


@pytest.fixture
def lab(tmp_path):
    clock = Clock()
    consoles = {'1': FakeConsole('board 1', clock=clock),
                '2': FakeConsole('board 2', clock=clock)}
    cfg = {'boards': {'1': {'settle_s': 30}, '2': {'settle_s': 35}},
           'data_dir': str(tmp_path), 'hub_token_file': None, 'webhook_file': None}
    db = DB(str(tmp_path / 'lab.sqlite'))
    posted = []
    sink = AlertSink(str(tmp_path / 'alerts.log'), 'http://hook', post=lambda u, b: posted.append(b))
    mon = Monitor(db, consoles, cfg, 'hubsecret', sink, clock=clock)
    mon.runner.sleep = clock.sleep
    bit = tmp_path / 'design.bit'
    bit.write_bytes(b'bitstream-bytes')
    img = tmp_path / 'smoke.bin'
    img.write_bytes(b'image-bytes')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    return dict(clock=clock, consoles=consoles, cfg=cfg, db=db, mon=mon, posted=posted,
                bit=str(bit), img=str(img), bit_sha=sha(bit), img_sha=sha(img), tmp=tmp_path)


def submit(lab, board='any', until=None, timeout=60):
    return lab['db'].add_job('Sam', 'smoke', board, lab['bit'], lab['bit_sha'],
                             lab['img'], lab['img_sha'], until, timeout)


# ------------------------------------------------------------ dispatch

def test_any_board_needs_qualification(lab):
    jid = submit(lab)
    assert lab['mon'].dispatch(run_async=False) == []
    assert 'not qualified' in lab['db'].job(jid)['reason']
    lab['db'].qualify('2', lab['bit_sha'])
    assert lab['mon'].dispatch(run_async=False) == [(jid, '2')]


def test_full_run_sequence_and_release(lab):
    jid = submit(lab, board='1', until=r'HELLO')
    lab['mon'].dispatch(run_async=False)
    j = lab['db'].job(jid)
    assert j['state'] == 'done' and j['matched'] == 1, j
    ev = [e[0] for e in lab['consoles']['1'].events]
    assert ev[:3] == ['take', 'upload', 'upload']
    assert ev.index('flash') < ev.index('load')
    # power cycled (off, then on) after the flash and before the load
    evs = lab['consoles']['1'].events
    i_off, i_on, i_load = evs.index(('power', False)), evs.index(('power', True)), ev.index('load')
    assert ev.index('flash') < i_off < i_on < i_load
    assert lab['consoles']['1'].events[-2:] == [('power', False), ('release', f'job{jid}:Sam')]
    assert lab['consoles']['1'].lm.holder() is None
    log = open(lab['mon'].runner.log_path(jid)).read()
    assert 'HELLO' in log and 'settling 30 s' in log


def test_resident_bitstream_is_not_reflashed(lab):
    c = lab['consoles']['1']
    c.files_['bitstreams'][file_name(lab['bit'], lab['bit_sha'], '.bit')] = lab['bit_sha']
    c.nv_sha = lab['bit_sha']
    submit(lab, board='1')
    lab['mon'].dispatch(run_async=False)
    assert not any(e[0] == 'flash' for e in c.events)


def test_until_regex_not_seen_fails_and_still_releases(lab):
    jid = submit(lab, board='1', until=r'NEVER', timeout=20)
    lab['mon'].dispatch(run_async=False)
    j = lab['db'].job(jid)
    assert j['state'] == 'failed' and 'no match' in j['reason']
    assert lab['consoles']['1'].lm.holder() is None
    assert lab['consoles']['1'].power_on is False


def test_load_error_releases_board(lab):
    lab['consoles']['1'].load_fails = True
    jid = submit(lab, board='1')
    lab['mon'].dispatch(run_async=False)
    assert lab['db'].job(jid)['state'] == 'failed'
    assert lab['consoles']['1'].lm.holder() is None


def test_held_board_is_skipped_quiet_when_free(lab):
    c = lab['consoles']['1']
    tok = c.take('Kim', kind='person')['token']
    jid = submit(lab, board='1')
    assert lab['mon'].dispatch(run_async=False) == []
    assert 'held by Kim' in lab['db'].job(jid)['reason']
    c.release(tok)
    assert lab['mon'].dispatch(run_async=False) == [(jid, '1')]


def test_job_yields_when_someone_queues(lab):
    c = lab['consoles']['1']
    jid = submit(lab, board='1', until=r'NEVER', timeout=400)
    orig_load = c.load

    def load_then_queue(*a, **k):
        r = orig_load(*a, **k)
        c.queue_join('Kim')
        return r
    c.load = load_then_queue
    lab['mon'].dispatch(run_async=False)
    log = open(lab['mon'].runner.log_path(jid)).read()
    assert 'finishing this job without renewing' in log
    # The job ran its course on the time it had; it did not fail on the yield.
    assert lab['db'].job(jid)['reason'].startswith('no match')
    assert c.lm.snapshot()['offer']['holder'] == 'Kim'


def test_cancel_stops_a_running_job(lab):
    c = lab['consoles']['1']
    jid = submit(lab, board='1', until=r'NEVER', timeout=400)
    orig_load = c.load

    def load_then_cancel(*a, **k):
        r = orig_load(*a, **k)
        lab['db'].set_job(jid, state='cancelled')
        return r
    c.load = load_then_cancel
    lab['mon'].dispatch(run_async=False)
    assert lab['db'].job(jid)['state'] == 'cancelled'
    assert c.lm.holder() is None and c.power_on is False


def test_board_free_respects_bookings():
    now = 1000.0
    assert board_is_free({'lease': None, 'offer': None, 'reservations': []}, now)
    assert not board_is_free({'lease': None, 'offer': None,
                              'reservations': [{'start': now + 30, 'end': now + 600}]}, now)
    assert board_is_free({'lease': None, 'offer': None,
                          'reservations': [{'start': now + 3600, 'end': now + 7200}]}, now)


# ------------------------------------------------------------- bookings

def test_bookings_pushed_and_enforced_by_console(lab):
    db, mon, c = lab['db'], lab['mon'], lab['consoles']['2']
    now = lab['clock']()
    db.add_booking('2', 'Kim', 'tests', now - 60, now + 3600)
    with pytest.raises(ValueError):
        db.add_booking('2', 'Lee', '', now + 1800, now + 5400)        # overlap refused
    mon.push_bookings()
    assert [r['holder'] for r in c.reservations_seen] == ['Kim']
    with pytest.raises(Exception) as e:
        c.take('Sam', kind='person')
    assert getattr(e.value, 'code', '') == 'reserved'
    assert c.take('Kim', kind='person')['token']


# ------------------------------------------------------ poll, usage, alerts

def test_unreachable_alert_after_five_polls_and_clears(lab):
    mon, c = lab['mon'], lab['consoles']['1']
    c.unreachable = True
    for _ in range(4):
        mon.poll()
    assert lab['db'].open_alerts() == []                      # quiet before the threshold
    mon.poll()
    assert [a['key'] for a in lab['db'].open_alerts()] == ['unreachable:1']
    mon.poll()
    assert len(lab['posted']) == 1                           # not re-sent while open
    c.unreachable = False
    mon.poll()
    assert lab['db'].open_alerts() == []
    assert len(lab['posted']) == 2                           # the all-clear


def test_health_alert(lab):
    mon, c = lab['mon'], lab['consoles']['2']
    mon.poll()
    assert lab['db'].open_alerts() == []
    c.health_ = {'verdict': 'degraded', 'age_s': 5, 'reason': 'app not serving'}
    mon.poll()
    assert any('health' in a['key'] for a in lab['db'].open_alerts())
    c.health_ = {'verdict': 'ok', 'age_s': 5}
    mon.poll()
    assert lab['db'].open_alerts() == []


def test_usage_report_counts_minutes_per_holder(lab):
    mon, c, clock = lab['mon'], lab['consoles']['1'], lab['clock']
    c.take('Kim', kind='person')
    for _ in range(30):
        mon.poll()
        clock.t += 60
    rep = lab['db'].usage_report(clock() - 86400)
    row = [r for r in rep if r['board'] == '1'][0]
    assert row['holder'] == 'Kim' and row['minutes'] == 30


# --------------------------------------------------------------- web app

def test_page_and_booking_form(lab, tmp_path):
    app = hub_mod.create_app(lab['cfg'], consoles=lab['consoles'], start_threads=False)
    app.config['monitor'].poll()
    http = app.test_client()
    r = http.get('/')
    assert r.status_code == 200 and b'FPGA Boards' in r.data
    tomorrow = time.strftime('%Y-%m-%d', time.localtime(time.time() + 86400))
    r = http.post('/book', data={'board': '2', 'holder': 'Kim', 'date': tomorrow,
                                 'time': '10:00', 'minutes': '60', 'purpose': 'x'})
    assert r.status_code == 302 and 'Booked' in unquote(r.headers['Location'])
    r = http.post('/book', data={'board': '2', 'holder': 'Lee', 'date': tomorrow,
                                 'time': '10:30', 'minutes': '60'})
    assert 'overlaps' in unquote(r.headers['Location'])
    bid = http.get('/api/bookings').get_json()[0]['id']
    r = http.post(f'/bookings/{bid}/cancel', data={'holder': 'Lee'},
                  environ_overrides={'REMOTE_ADDR': '100.1.2.3'})
    assert 'Type the name' in unquote(r.headers['Location'])
    r = http.post(f'/bookings/{bid}/cancel', data={'holder': 'Kim'},
                  environ_overrides={'REMOTE_ADDR': '100.1.2.3'})
    assert 'cancelled' in unquote(r.headers['Location'])


def test_job_submit_only_from_hub_host(lab):
    app = hub_mod.create_app(lab['cfg'], consoles=lab['consoles'], start_threads=False)
    http = app.test_client()
    body = {'owner': 'Sam', 'bit_path': lab['bit'], 'bit_sha': lab['bit_sha'],
            'img_path': lab['img'], 'img_sha': lab['img_sha']}
    r = http.post('/api/jobs', json=body, environ_overrides={'REMOTE_ADDR': '100.1.2.3'})
    assert r.status_code == 403
    r = http.post('/api/jobs', json=body)
    assert r.status_code == 200 and r.get_json()['id'] == 1


def test_page_never_shows_console_url(lab):
    secret = 'http://board.example:5000/S3CR3TPREFIX/'
    from fpga_lab.client import Console
    lab['consoles']['1'] = FakeConsole('board 1')
    app = hub_mod.create_app(lab['cfg'], consoles={'1': Console(secret, name='board 1')},
                             start_threads=False)
    mon = app.config['monitor']
    mon.poll()                    # unreachable; error text must not carry the URL
    http = app.test_client()
    for path in ('/', '/api/status'):
        assert b'S3CR3TPREFIX' not in http.get(path).data
    assert 'S3CR3TPREFIX' not in repr(Console(secret))


def test_old_console_without_leases_is_not_unreachable(lab):
    from fpga_lab.client import ConsoleError
    c = lab['consoles']['1']

    def nf():
        raise ConsoleError(404, 'http_error', 'HTTP 404')
    c.lease = nf
    c.health = nf
    for _ in range(6):
        lab['mon'].poll()
    assert lab['db'].open_alerts() == []
    assert lab['mon'].snapshot()['1']['enforce'] == 'absent'
    jid = submit(lab, board='1')
    assert lab['mon'].dispatch(run_async=False) == []
    assert 'no leases yet' in lab['db'].job(jid)['reason']
    lab['mon'].push_bookings()              # skipped for that board, no alert
    assert lab['db'].open_alerts() == []
