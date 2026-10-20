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
           'data_dir': str(tmp_path), 'hub_token_file': None, 'webhook_file': None,
           'hub_listen': '127.0.0.1:8077'}
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
                  environ_overrides={'REMOTE_ADDR': '127.0.0.1'})   # no address is trusted
    assert 'Type the name' in unquote(r.headers['Location'])
    r = http.post(f'/bookings/{bid}/cancel', data={'holder': 'Kim'},
                  environ_overrides={'REMOTE_ADDR': '100.1.2.3'})
    assert 'cancelled' in unquote(r.headers['Location'])


def _admin_app(lab, token='adm1n'):
    p = lab['tmp'] / 'admin-token'
    p.write_text(token)
    cfg = dict(lab['cfg'], admin_token_file=str(p))
    return hub_mod.create_app(cfg, consoles=lab['consoles'], start_threads=False)


def _files(lab):
    return {'bit': (open(lab['bit'], 'rb'), 'design.bit'), 'img': (open(lab['img'], 'rb'), 'smoke.bin')}


def test_job_submit_needs_admin_token_not_an_address(lab):
    app = _admin_app(lab)
    http = app.test_client()
    form = {'owner': 'Sam', 'board': '1', **_files(lab)}
    # From the hub host itself, without the token: refused (apollo is shared).
    for addr in ('127.0.0.1', '100.112.174.77'):
        r = http.post('/api/jobs', data={'owner': 'Sam', 'board': '1', **_files(lab)},
                      environ_overrides={'REMOTE_ADDR': addr})
        assert r.status_code == 401, addr
    r = http.post('/api/jobs', data={'owner': 'Sam', **_files(lab)}, headers={'X-Lab-Admin': 'wrong'})
    assert r.status_code == 401
    r = http.post('/api/jobs', data=form, headers={'X-Lab-Admin': 'adm1n'},
                  content_type='multipart/form-data')
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d['bit_sha'] == lab['bit_sha'] and d['img_sha'] == lab['img_sha']
    job = lab['db'].job(d['id'])
    assert job['bit_path'].startswith(str(lab['tmp'] / 'files'))     # stored copy, not a caller path
    assert http.get(f"/api/jobs/{d['id']}/log").status_code == 401
    assert http.get(f"/api/jobs/{d['id']}/log", headers={'X-Lab-Admin': 'adm1n'}).status_code in (200, 404)
    assert http.post(f"/api/jobs/{d['id']}/cancel").status_code == 401


def test_no_admin_token_file_means_no_admin(lab):
    app = hub_mod.create_app(dict(lab['cfg'], admin_token_file=None), consoles=lab['consoles'],
                             start_threads=False)
    r = app.test_client().post('/api/jobs', data={'owner': 'Sam', **_files(lab)},
                               headers={'X-Lab-Admin': ''})
    assert r.status_code == 401


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
    lab['mon'].push_bookings()              # no bookings: no alert
    assert lab['db'].open_alerts() == []
    c.set_reservations = lambda *a: (_ for _ in ()).throw(ConsoleError(404, 'http_error', 'HTTP 404'))
    lab['mon'].push_bookings()
    assert lab['db'].open_alerts() == []
    now = lab['clock']()
    lab['db'].add_booking('1', 'Kim', '', now + 600, now + 1200)
    lab['mon'].push_bookings()              # a booking it cannot enforce: alert
    assert [a['key'] for a in lab['db'].open_alerts()] == ['push:1']




# --------------------------------------------------------------- timeline

def test_timeline_past_now_bookings_and_estimates(lab):
    from fpga_lab import timeline as tlm
    db, mon, clock = lab['db'], lab['mon'], lab['clock']
    c1 = lab['consoles']['1']
    tok = c1.take('Kim', kind='person', minutes=60)['token']
    for _ in range(20):                      # 20 minutes of Kim on board 1
        mon.poll()
        clock.t += 60
    mon.poll()
    now = clock()
    db.add_booking('2', 'Lee', 'demo', now + 1800, now + 5400)
    lab['db'].qualify('2', lab['bit_sha'])
    j1 = submit(lab, board='1', timeout=600)   # waits for Kim's lease
    j2 = submit(lab, board='any', timeout=600) # only qualified on 2, must avoid Lee's booking
    tl = tlm.build(db, mon.snapshot(), lab['cfg']['boards'], now, 6, 24)
    r1 = [r for r in tl['rows'] if r['board'] == '1'][0]['segs']
    past = [s for s in r1 if s['type'] == 'used']
    assert past and past[0]['label'] == 'Kim' and 19 * 60 <= past[0]['end'] - past[0]['start'] <= 21 * 60
    assert any(s['type'] == 'now' and s['end'] > now for s in r1)
    b1, s1, e1 = tl['estimates'][j1]
    assert b1 == '1' and s1 >= c1.lm.holder()['until']           # after Kim's lease
    b2, s2, e2 = tl['estimates'][j2]
    assert b2 == '2' and (e2 <= now + 1800 or s2 >= now + 5400)  # not inside the booking
    svg = tlm.render_svg(tl)
    assert svg.startswith('<svg') and 'Kim' in svg and 'Lee' in svg and 'now' in svg
    c1.release(tok)


def test_page_shows_timeline_and_escapes_names(lab):
    app = hub_mod.create_app(lab['cfg'], consoles=lab['consoles'], start_threads=False)
    now = lab['clock']()
    lab['db'].add_booking('2', '<script>x</script>', '', now + 600, now + 1200)
    page = app.test_client().get('/').data.decode()
    assert '<svg class="tl"' in page and 'Timeline' in page
    assert '<script>x</script>' not in page and '&lt;script&gt;' in page
    assert app.test_client().get('/api/timeline').status_code == 200



# ------------------------------------------------------ users and agent API

def test_user_tokens_bookings_and_own_jobs(lab):
    app = _admin_app(lab)
    http = app.test_client()
    A = {'X-Lab-Admin': 'adm1n'}
    assert http.post('/api/users', json={'name': 'diego'}).status_code == 403
    tok = http.post('/api/users', json={'name': 'diego'}, headers=A).get_json()['token']
    tok2 = http.post('/api/users', json={'name': 'kim'}, headers=A).get_json()['token']
    D, K = {'X-Lab-Token': tok}, {'X-Lab-Token': tok2}
    assert http.get('/api/whoami', headers=D).get_json() == {'role': 'user', 'name': 'diego'}
    assert http.get('/api/whoami', headers={'X-Lab-Token': 'flab_wrong'}).get_json()['role'] is None
    import time as _t
    start = _t.time() + 3600
    # booking: holder is the token's user, a typed holder is ignored
    r = http.post('/api/bookings', headers=D,
                  json={'board': '2', 'start': start, 'minutes': 60, 'holder': 'someone-else'})
    assert r.status_code == 201 and r.get_json()['holder'] == 'diego'
    bid = r.get_json()['id']
    r = http.post('/api/bookings', headers=K, json={'board': '2', 'start': start + 1800, 'minutes': 60})
    assert r.status_code == 409
    iso = _t.strftime('%Y-%m-%dT%H:%M', _t.localtime(start + 7200))
    assert http.post('/api/bookings', headers=K, json={'board': '2', 'start': iso, 'minutes': 30}).status_code == 201
    assert http.post('/api/bookings', json={'board': '2', 'start': start, 'minutes': 30}).status_code == 401
    assert http.delete(f'/api/bookings/{bid}', headers=K).status_code == 403
    free = http.get('/api/free?board=2&minutes=60').get_json()['2']['start']
    assert free < start or free >= start + 3600
    assert http.delete(f'/api/bookings/{bid}', headers=D).status_code == 200
    # jobs: owner is the user; others cannot cancel or read
    r = http.post('/api/jobs', headers=D, data={'owner': 'faked', 'board': '1', **_files(lab)},
                  content_type='multipart/form-data')
    assert r.status_code == 200
    jid = r.get_json()['id']
    assert lab['db'].job(jid)['owner'] == 'diego'
    assert http.get(f'/api/jobs/{jid}/log', headers=K).status_code == 403
    assert http.post(f'/api/jobs/{jid}/cancel', headers=K).status_code == 403
    assert http.post(f'/api/jobs/{jid}/cancel', headers=D).get_json()['state'] == 'cancelled'
    # removed user loses access
    http.delete('/api/users/diego', headers=A)
    assert http.get('/api/whoami', headers=D).get_json()['role'] is None
    # tokens stored hashed
    assert tok not in str(lab['db'].q('SELECT * FROM users'))
    assert 'X-Lab-Token' in http.get('/llms.txt').data.decode()



def test_owner_resolution_and_colours():
    from fpga_lab.owners import owner_of, colour, UNKNOWN
    cfg = {'holder_owner': {'board-lane': 'Alexey'}, 'people': {'kim@x.org': 'Kim'}}
    assert owner_of({'holder': 'board-lane'}, cfg) == 'Alexey'
    assert owner_of({'holder': 'job12:diego'}, cfg) == 'diego'
    assert owner_of({'holder': 'anything', 'who': 'kim@x.org'}, cfg) == 'Kim'
    assert owner_of({'holder': 'anything', 'who': 'lee@y.org'}, cfg) == 'lee'
    assert owner_of({'holder': 'Sam'}, cfg) == 'Sam'
    assert colour('Alexey') == colour('Alexey') and colour('Alexey') != UNKNOWN
    assert colour('(unknown)') == UNKNOWN
    assert colour('Alexey', {'colours': {'Alexey': '#123456'}}) == '#123456'


def test_first_dozen_people_get_distinct_stable_colours(tmp_path):
    from fpga_lab.db import DB
    from fpga_lab.owners import colour
    db = DB(str(tmp_path / 'c.sqlite'))
    names = [f'user{i}' for i in range(12)]
    cols = [colour(n, None, db) for n in names]
    assert len(set(cols)) == 12
    assert [colour(n, None, db) for n in names] == cols          # stable


def test_board_without_leases_but_powered_is_shown_in_use(lab):
    from fpga_lab.client import ConsoleError
    from fpga_lab import timeline as tlm
    c = lab['consoles']['1']

    def nf():
        raise ConsoleError(404, 'http_error', 'HTTP 404')
    c.lease = nf
    c.power_on = True
    for _ in range(10):
        lab['mon'].poll()
        lab['clock'].t += 60
    c.power_on = False                       # quiet control: off is not "in use"
    for _ in range(5):
        lab['mon'].poll()
        lab['clock'].t += 60
    tl = tlm.build(lab['db'], lab['mon'].snapshot(), lab['cfg']['boards'], lab['clock'](), 6, 24)
    segs = [s for s in tl['rows'][0]['segs'] if s['type'] == 'used']
    assert len(segs) == 1 and segs[0]['owner'] == '(unknown)'
    assert 9 * 60 <= segs[0]['end'] - segs[0]['start'] <= 11 * 60
    assert 'holder unknown' in tlm.render_svg(tl)
    c.power_on = True
    lab['mon'].poll()
    app = hub_mod.create_app(lab['cfg'], consoles=lab['consoles'], start_threads=False)
    app.config['monitor'].poll()
    assert 'in use · holder unknown' in app.test_client().get('/').data.decode()


def test_timeline_colours_by_owner_and_legend(lab):
    cfg = dict(lab['cfg'], holder_owner={'board-lane': 'Alexey'})
    app = hub_mod.create_app(cfg, consoles=lab['consoles'], start_threads=False)
    mon = app.config['monitor']
    lab['consoles']['1'].take('board-lane', kind='agent')
    lab['consoles']['2'].take('job3:diego', kind='agent')
    mon.poll()
    page = app.test_client().get('/').data.decode()
    from fpga_lab.owners import colour
    db = app.config['db']
    assert colour('Alexey', cfg, db) in page and colour('diego', cfg, db) in page
    assert colour('Alexey', cfg, db) != colour('diego', cfg, db)
    assert '>Alexey</span>' in page and '>diego</span>' in page      # legend entries
