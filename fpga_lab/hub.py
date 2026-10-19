"""The hub: one page for all boards, the booking calendar, the run queue,
usage and alerts. Runs on the coordinator (apollo), never on a board host,
so deploying it never restarts a console and never cuts board power.

The page shows no console URL: those carry each board's secret prefix and
stay in files on this host.
"""

import datetime
import hashlib
import hmac
import os
import re
import threading
import time

from flask import Flask, abort, jsonify, redirect, render_template_string, request

from . import config as config_mod
from .client import Console, ConsoleError, read_url
from .db import DB
from .monitor import AlertSink, Monitor
from . import timeline as timeline_mod

ADMIN_HEADER = 'X-Lab-Admin'
USER_HEADER = 'X-Lab-Token'


def make_consoles(cfg):
    out = {}
    for b, bc in cfg['boards'].items():
        out[b] = Console(read_url(bc['url_file']), name=f'board {b}')
    return out


def create_app(cfg=None, consoles=None, start_threads=True):
    cfg = cfg or config_mod.load()
    db = DB(os.path.join(cfg['data_dir'], 'lab.sqlite'))
    files_dir = os.path.join(cfg['data_dir'], 'files')
    os.makedirs(files_dir, exist_ok=True)
    consoles = consoles if consoles is not None else make_consoles(cfg)
    hub_token = config_mod.read_secret(cfg.get('hub_token_file'))
    admin_token = config_mod.read_secret(cfg.get('admin_token_file'))
    sink = AlertSink(os.path.join(cfg['data_dir'], 'alerts.log'),
                     config_mod.read_secret(cfg.get('webhook_file')))
    mon = Monitor(db, consoles, cfg, hub_token, sink)
    app = Flask(__name__)
    app.config['db'] = db
    app.config['monitor'] = mon

    # Running jobs, cancelling them, reading their logs and qualifying
    # bitstreams need the admin token: a file readable only by the hub's own
    # user. Trusting the source address instead made every account on the hub
    # host an admin -- apollo is a shared server.
    def is_admin():
        given = request.headers.get(ADMIN_HEADER, '')
        return bool(admin_token) and hmac.compare_digest(given, admin_token)

    def user():
        """The caller's lab user name from X-Lab-Token, or None. Tokens are
        stored only as sha256; the admin issues them (`fpga user add`)."""
        tok = request.headers.get(USER_HEADER, '')
        if not tok:
            return None
        return db.user_by_token_sha(hashlib.sha256(tok.encode()).hexdigest())

    def caller():
        """('admin', None) | ('user', name) | (None, None)"""
        if is_admin():
            return 'admin', None
        u = user()
        return ('user', u) if u else (None, None)

    def admin_only():
        if not is_admin():
            abort(403, 'needs the lab admin token (fpga on the hub host, as its owner)')

    def user_or_admin():
        role, name = caller()
        if not role:
            abort(401, 'needs a lab token in X-Lab-Token (ask the lab admin: fpga user add NAME)')
        return role, name

    # ----------------------------------------------------------------- page

    @app.route('/')
    def page():
        now = time.time()
        report = db.usage_report(now - 7 * 86400)
        status = mon.snapshot()
        back = max(1, min(float(request.args.get('back', 6)), 168))
        ahead = max(1, min(float(request.args.get('ahead', 24)), 168))
        tl = timeline_mod.build(db, status, cfg['boards'], now, back, ahead)
        from markupsafe import Markup
        return render_template_string(PAGE, boards=status, cfg=cfg, now=now,
                                      bookings=db.bookings(), jobs=db.jobs(20),
                                      alerts=db.open_alerts(), report=report,
                                      timeline=Markup(timeline_mod.render_svg(tl)),
                                      est=tl['estimates'], back=back, ahead=ahead,
                                      hhmm=_hhmm, day=_day, msg=request.args.get('msg'))

    @app.route('/api/timeline')
    def api_timeline():
        now = time.time()
        tl = timeline_mod.build(db, mon.snapshot(), cfg['boards'], now,
                                float(request.args.get('back', 6)), float(request.args.get('ahead', 24)))
        tl['estimates'] = {str(k): {'board': v[0], 'start': v[1], 'end': v[2]}
                           for k, v in tl['estimates'].items()}
        return jsonify(tl)

    @app.route('/book', methods=['POST'])
    def book():
        f = request.form if request.form else (request.get_json(silent=True) or {})
        try:
            board = str(f['board'])
            if board not in consoles:
                raise ValueError('no such board')
            holder = (f.get('holder') or '').strip()[:64]
            if not holder:
                raise ValueError('enter your name')
            start = _parse_start(f['date'], f['time'])
            minutes = int(f.get('minutes', 60))
            if not 15 <= minutes <= 480:
                raise ValueError('a booking is 15 min to 8 h')
            if start + minutes * 60 < time.time():
                raise ValueError('that time has passed')
            db.add_booking(board, holder, (f.get('purpose') or '').strip()[:120],
                           start, start + minutes * 60, request.remote_addr)
        except (KeyError, ValueError) as e:
            return _reply(f'Not booked: {e}', 400)
        threading.Thread(target=mon.push_bookings, daemon=True).start()
        return _reply('Booked', 200)

    @app.route('/bookings/<int:bid>/cancel', methods=['POST'])
    def cancel_booking(bid):
        f = request.form if request.form else (request.get_json(silent=True) or {})
        rows = [b for b in db.bookings(since=0) if b['id'] == bid]
        if not rows:
            return _reply('No such booking', 404)
        # Trust-based, like the console's typed names: you cancel by naming the
        # holder. The admin may cancel anything.
        if not is_admin() and (f.get('holder') or '').strip() != rows[0]['holder']:
            return _reply('Type the name the booking was made with to cancel it', 403)
        db.cancel_booking(bid)
        threading.Thread(target=mon.push_bookings, daemon=True).start()
        return _reply('Booking cancelled', 200)

    def _reply(text, status):
        if request.form:
            from urllib.parse import quote
            return redirect('./?msg=' + quote(text))
        return jsonify({'message': text}), status

    # ------------------------------------------------------------------ API

    @app.route('/api/status')
    def api_status():
        return jsonify({'boards': mon.snapshot(), 'alerts': db.open_alerts(),
                        'running': mon.running})

    @app.route('/api/bookings')
    def api_bookings():
        return jsonify(db.bookings())

    @app.route('/api/jobs', methods=['GET'])
    def api_jobs():
        return jsonify(db.jobs(int(request.args.get('limit', 50))))

    @app.route('/api/jobs', methods=['POST'])
    def api_submit():
        """Queue a run. The bitstream and image come IN the request and are
        stored by content hash: the hub never opens a path a caller names."""
        role, name = user_or_admin()
        f = request.form
        bit, img = request.files.get('bit'), request.files.get('img')
        owner = name if role == 'user' else (f.get('owner') or '')
        if not owner or not bit or not img:
            return jsonify({'error': 'need the files bit and img (and owner, for the admin)'}), 400
        board = str(f.get('board', 'any'))
        if board != 'any' and board not in consoles:
            return jsonify({'error': 'no such board'}), 400
        stored = {}
        for key, up, ext in (('bit', bit, '.bit'), ('img', img, '.bin')):
            data = up.read()
            sha = hashlib.sha256(data).hexdigest()
            stem = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.splitext(up.filename or key)[0])[:60] or key
            path = os.path.join(files_dir, f'{stem}_{sha[:12]}{ext}')
            if not os.path.exists(path):
                with open(path + '.tmp', 'wb') as fh:
                    fh.write(data)
                os.replace(path + '.tmp', path)
            stored[key] = (path, sha)
        until = f.get('until_regex') or None
        if until:
            try:
                re.compile(until)
            except re.error as e:
                return jsonify({'error': f'bad regex: {e}'}), 400
        jid = db.add_job(owner[:64], f.get('purpose', '')[:120], board,
                         stored['bit'][0], stored['bit'][1], stored['img'][0], stored['img'][1],
                         until, max(10, min(int(f.get('timeout_s', 600)), 7200)))
        return jsonify({'id': jid, 'bit_sha': stored['bit'][1], 'img_sha': stored['img'][1]})

    @app.route('/api/jobs/<int:jid>/cancel', methods=['POST'])
    def api_cancel(jid):
        role, name = user_or_admin()
        j = db.job(jid)
        if not j:
            return jsonify({'error': 'no such job'}), 404
        if role == 'user' and j['owner'] != name:
            return jsonify({'error': 'not your job'}), 403
        if j['state'] in ('queued', 'running'):
            db.set_job(jid, state='cancelled')
        return jsonify(db.job(jid))

    @app.route('/api/jobs/<int:jid>/log')
    def api_log(jid):
        role, name = user_or_admin()      # UART output: your own runs only
        j = db.job(jid)
        if not j:
            return jsonify({'error': 'no such job'}), 404
        if role == 'user' and j['owner'] != name:
            return jsonify({'error': 'not your job'}), 403
        try:
            with open(mon.runner.log_path(jid)) as fh:
                return fh.read(), 200, {'Content-Type': 'text/plain; charset=utf-8'}
        except FileNotFoundError:
            return 'no log yet\n', 404, {'Content-Type': 'text/plain; charset=utf-8'}

    # -------------------------------------------------------- bookings API

    @app.route('/api/bookings', methods=['POST'])
    def api_book():
        """Book a board. JSON: {board, start, minutes, purpose}. start is
        epoch seconds or ISO 8601 (with offset; without one it is hub-local).
        The holder is the caller's lab user; the admin may pass holder."""
        role, name = user_or_admin()
        d = request.get_json(silent=True) or {}
        try:
            board = str(d['board'])
            if board not in consoles:
                raise ValueError('no such board')
            start = _parse_when(d['start'])
            minutes = int(d.get('minutes', 60))
            if not 15 <= minutes <= 480:
                raise ValueError('a booking is 15 min to 8 h')
            if start + minutes * 60 < time.time():
                raise ValueError('that time has passed')
            holder = name if role == 'user' else (d.get('holder') or '').strip()[:64]
            if not holder:
                raise ValueError('holder is required for the admin')
            bid = db.add_booking(board, holder, (d.get('purpose') or '').strip()[:120],
                                 start, start + minutes * 60, request.remote_addr)
        except KeyError as e:
            return jsonify({'error': f'missing {e}'}), 400
        except ValueError as e:
            return jsonify({'error': str(e)}), 409 if 'overlaps' in str(e) else 400
        threading.Thread(target=mon.push_bookings, daemon=True).start()
        b = [x for x in db.bookings(since=0) if x['id'] == bid][0]
        b.pop('ip', None)
        return jsonify(b), 201

    @app.route('/api/bookings/<int:bid>', methods=['DELETE'])
    def api_unbook(bid):
        role, name = user_or_admin()
        rows = [b for b in db.bookings(since=0) if b['id'] == bid]
        if not rows:
            return jsonify({'error': 'no such booking'}), 404
        if role == 'user' and rows[0]['holder'] != name:
            return jsonify({'error': 'not your booking'}), 403
        db.cancel_booking(bid)
        threading.Thread(target=mon.push_bookings, daemon=True).start()
        return jsonify({'cancelled': bid})

    @app.route('/api/free')
    def api_free():
        """When is a board free for N minutes? ?board=2&minutes=60[&after=ISO]"""
        board = request.args.get('board')
        minutes = int(request.args.get('minutes', 60))
        after = _parse_when(request.args['after']) if request.args.get('after') else time.time()
        out = {}
        tl = timeline_mod.build(db, mon.snapshot(), cfg['boards'], time.time(), 0, 24 * 14)
        for row in tl['rows']:
            if board and row['board'] != board:
                continue
            busy = sorted((s['start'], s['end']) for s in row['segs'] if s['end'] > after)
            t = after
            for bs, be in busy:
                if t + minutes * 60 <= bs:
                    break
                t = max(t, be)
            out[row['board']] = {'start': t, 'start_local': _iso(t)}
        return jsonify(out)

    # ----------------------------------------------------------- users API

    @app.route('/api/users', methods=['POST'])
    def api_user_add():
        admin_only()
        name = ((request.get_json(silent=True) or {}).get('name') or '').strip()[:64]
        if not re.fullmatch(r'[A-Za-z0-9._@-]{2,64}', name or ''):
            return jsonify({'error': 'name: 2-64 of letters, digits, . _ @ -'}), 400
        import secrets as _secrets
        tok = 'flab_' + _secrets.token_urlsafe(24)
        db.add_user(name, hashlib.sha256(tok.encode()).hexdigest())
        return jsonify({'name': name, 'token': tok,
                        'note': 'shown once; the hub keeps only its hash'}), 201

    @app.route('/api/users', methods=['GET'])
    def api_users():
        admin_only()
        return jsonify(db.users())

    @app.route('/api/users/<name>', methods=['DELETE'])
    def api_user_remove(name):
        admin_only()
        db.remove_user(name)
        return jsonify({'removed': name})

    @app.route('/api/whoami')
    def api_whoami():
        role, name = caller()
        return jsonify({'role': role, 'name': name})

    @app.route('/api')
    @app.route('/llms.txt')
    def api_guide():
        return AGENT_GUIDE, 200, {'Content-Type': 'text/plain; charset=utf-8'}

    @app.route('/api/qualify', methods=['POST'])
    def api_qualify():
        admin_only()
        d = request.get_json(silent=True) or {}
        if str(d.get('board')) not in consoles or len(d.get('bit_sha', '')) != 64:
            return jsonify({'error': 'need board and a 64-hex bit_sha'}), 400
        db.qualify(str(d['board']), d['bit_sha'], d.get('note', ''))
        return jsonify(db.qualifications())

    @app.route('/api/qualified')
    def api_qualified():
        return jsonify(db.qualifications())

    @app.route('/api/report')
    def api_report():
        days = float(request.args.get('days', 7))
        return jsonify(db.usage_report(time.time() - days * 86400))

    # -------------------------------------------------------------- threads

    if start_threads:
        def loop(fn, every):
            def run():
                while True:
                    try:
                        fn()
                    except Exception as e:
                        print(f'{fn.__name__}: {type(e).__name__}: {e}', flush=True)
                    time.sleep(every)
            threading.Thread(target=run, daemon=True, name=fn.__name__).start()
        loop(mon.poll, 60)
        loop(mon.push_bookings, 300)
        loop(mon.dispatch, 15)
    return app


def _parse_when(v):
    """Epoch seconds, or ISO 8601; a naive ISO time is hub-local."""
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip()
    try:
        return float(v)
    except ValueError:
        pass
    dt = datetime.datetime.fromisoformat(v.replace('Z', '+00:00'))
    return dt.timestamp()


def _iso(t):
    return datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec='minutes')


def _parse_start(date_s, time_s):
    dt = datetime.datetime.strptime(f'{date_s} {time_s}', '%Y-%m-%d %H:%M')
    return dt.timestamp()


def _hhmm(t):
    return time.strftime('%H:%M', time.localtime(t)) if t else ''


def _day(t):
    return time.strftime('%a %d %b', time.localtime(t)) if t else ''


AGENT_GUIDE = """fpga-lab hub -- API for people and agents

Base URL: this host, port 8077 (tailnet only). JSON in and out.
Auth: header  X-Lab-Token: <your token>   (ask the lab admin: fpga user add NAME)
Reads need no token.

READ
  GET  /api/status                 boards: power, lease holder/until, queue, bitstream, health
  GET  /api/timeline?back=6&ahead=24   per board: past use, current lease, bookings, estimated runs
  GET  /api/free?board=2&minutes=60[&after=ISO]   earliest start of a free slot
  GET  /api/bookings               upcoming bookings
  GET  /api/jobs                   run queue (newest first)
  GET  /api/whoami                 who your token is

BOOK A SLOT  (during your booking only you can take the board; nobody's lease may run into it)
  POST   /api/bookings  {"board":"2","start":"2026-10-21T10:00+08:00","minutes":120,"purpose":"..."}
                        -> 201 booking | 409 overlaps another booking
  DELETE /api/bookings/<id>        your own bookings only

RUN SOMETHING  (the hub takes the lease, flashes NV if needed, power-cycles, waits,
                loads, captures UART until the regex or timeout, powers off, releases)
  POST   /api/jobs   multipart: files bit=<.bit> img=<.bin>;
                     fields board=any|1|2, until_regex=..., timeout_s=600, purpose=...
                     -> {"id": N}
  GET    /api/jobs/<id>/log        your own runs: progress and captured UART
  POST   /api/jobs/<id>/cancel     your own runs

USE A BOARD INTERACTIVELY  -- talk to the board's console directly (its URL is
private, ask the admin): POST /api/lease {"holder","purpose","kind":"agent"} ->
token; send it as X-Lease-Token on every action and as {"lease_token"} in a
Socket.IO 'lease_bind' after each connect; POST /api/lease/renew every <=5 min
(409 "yield" = someone is waiting: finish your cycle and DELETE /api/lease).
Always NV-flash, power-cycle and wait ~35 s before JTAG; volatile flash never
brings up the debug TAP.

The `fpga` command wraps all of this:  pip install git+https://github.com/project-starch/fpga-lab
  fpga status | fpga timeline | fpga free 2 --for 2h
  fpga book 2 --at "2026-10-21 10:00" --for 2h --why "..." | fpga bookings | fpga unbook ID
  fpga submit --bit X.bit --img Y.bin --until "PASS" | fpga jobs | fpga log ID | fpga cancel ID
"""


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>FPGA Boards</title>
<style>
:root { --bg:#f5f7f6; --surface:#fff; --fg:#18211d; --muted:#5b6862; --line:#d9e0dc;
        --accent:#0f6b4f; --soft:#e2efe9; --warn:#9a5b00; --warnsoft:#fbf0dc; --stop:#a3261c; }
@media (prefers-color-scheme: dark) { :root { --bg:#111613; --surface:#18201c; --fg:#e3ebe6;
        --muted:#9aa8a1; --line:#2c3833; --accent:#5cc79d; --soft:#1d3329; --warn:#e3a64a;
        --warnsoft:#362a16; --stop:#ef8a7f; color-scheme: dark; } }
body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0;
       padding:24px max(16px, env(safe-area-inset-left)); }
main { max-width:980px; margin:0 auto; display:grid; gap:24px; }
h1 { font-size:22px; margin:0; } h2 { font-size:16px; margin:0 0 8px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:12px; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:12px;
        display:grid; gap:4px; }
.chip { display:inline-block; padding:2px 9px; border-radius:99px; font-size:13px; background:var(--soft); color:var(--accent); }
.chip.held { background:var(--warnsoft); color:var(--warn); }
.chip.down { background:var(--warnsoft); color:var(--stop); }
.muted { color:var(--muted); font-size:13px; }
table { border-collapse:collapse; width:100%; font-size:14px; background:var(--surface); }
th, td { text-align:left; padding:6px 10px; border-top:1px solid var(--line); vertical-align:top; }
th { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; border-top:0; }
.wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
form.book { display:flex; flex-wrap:wrap; gap:8px; align-items:end; background:var(--surface);
            border:1px solid var(--line); border-radius:8px; padding:12px; }
form.book label { display:grid; gap:2px; font-size:13px; color:var(--muted); }
input, select, button { font:inherit; padding:5px 8px; border:1px solid var(--line); border-radius:4px;
                        background:var(--bg); color:var(--fg); }
button { background:var(--accent); color:var(--surface); border-color:var(--accent); cursor:pointer; }
.alert { border-left:3px solid var(--stop); background:var(--warnsoft); padding:8px 12px; border-radius:0 6px 6px 0; }
.msg { border-left:3px solid var(--accent); background:var(--soft); padding:8px 12px; border-radius:0 6px 6px 0; }
.tlwrap { overflow-x:auto; background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:8px; }
svg.tl { display:block; width:100%; min-width:720px; height:auto; font:12px system-ui, sans-serif; }
.tl-grid { stroke:var(--line); stroke-width:1; }
.tl-tick, .tl-now-text { fill:var(--muted); }
.tl-row { fill:var(--fg); font-weight:600; }
.tl-lane { fill:var(--bg); }
.tl-person, .tl-person-now { fill:var(--warn); }
.tl-agent, .tl-agent-now { fill:var(--accent); }
.tl-person { opacity:.55 } .tl-agent { opacity:.55 }
.tl-book-bg { fill:var(--warnsoft); } .tl-book-line { stroke:var(--warn); stroke-width:2; }
.tl-booking { stroke:var(--warn); stroke-width:1; }
.tl-job-est { fill:var(--soft); stroke:var(--accent); stroke-width:1.5; stroke-dasharray:4 3; }
.tl-seg-text { fill:var(--fg); font-size:11px; pointer-events:none; }
.tl-person-now + text, .tl-agent-now + text, .tl-person + text, .tl-agent + text { fill:var(--surface); }
.tl-now { stroke:var(--stop); stroke-width:2; }
.legend { display:flex; flex-wrap:wrap; gap:14px; font-size:13px; color:var(--muted); margin-top:6px; }
.legend i { display:inline-block; width:14px; height:10px; border-radius:2px; margin-right:5px; vertical-align:middle; }
</style></head><body><main>
<header><h1>FPGA Boards</h1>
<div class="muted">Updated {{ hhmm(now) }} · refreshes every minute · times are local to the hub ·
agents: <a href="api">API guide</a></div></header>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
{% for a in alerts %}<div class="alert">{{ a.message }} <span class="muted">since {{ hhmm(a.opened) }}</span></div>{% endfor %}

<section><h2>Boards</h2><div class="grid">
{% for b, s in boards.items() %}<div class="card">
  <b>Board #{{ b }}</b>
  {% if not s.reachable %}<span><span class="chip down">unreachable</span></span>
  {% elif s.lease %}<span><span class="chip held">held by {{ s.lease.holder }} until {{ hhmm(s.lease.until) }}</span></span>
    {% if s.lease.purpose %}<span class="muted">{{ s.lease.purpose }}</span>{% endif %}
  {% elif s.offer %}<span><span class="chip held">offered to {{ s.offer.holder }}</span></span>
  {% else %}<span><span class="chip">free</span></span>{% endif %}
  {% if s.reachable %}
  <span class="muted">power {{ s.power }} · bitstream {{ s.nv_bitstream or 'unknown' }}</span>
  <span class="muted">{{ s.queue|length }} waiting · health {{ s.health or 'n/a' }} · leases {{ {'absent': 'not installed', 'log': 'recorded, not enforced', 'on': 'enforced', 'off': 'off'}.get(s.enforce, s.enforce) }}</span>
  {% endif %}
</div>{% else %}<div class="muted">No status yet; the first poll runs within a minute.</div>{% endfor %}
</div></section>

<section><h2>Timeline</h2>
<div class="tlwrap">{{ timeline }}</div>
<div class="legend">
  <span><i style="background:var(--warn)"></i>person</span>
  <span><i style="background:var(--accent)"></i>agent / run</span>
  <span><i style="background:repeating-linear-gradient(45deg,var(--warnsoft) 0 3px,var(--warn) 3px 5px)"></i>booking</span>
  <span><i style="border:1.5px dashed var(--accent);background:var(--soft)"></i>queued run, estimated</span>
  <span>past {{ back|int }} h · next {{ ahead|int }} h ·
    <a href="?back=24&ahead=72">3 days</a> · <a href="?back=168&ahead=168">2 weeks</a> · <a href="./">default</a></span>
</div>
</section>

<section><h2>Book a board</h2>
<form class="book" method="post" action="book">
  <label>Board <select name="board">{% for b in cfg.boards %}<option value="{{ b }}">#{{ b }}</option>{% endfor %}</select></label>
  <label>Name <input name="holder" required maxlength="64"></label>
  <label>Date <input type="date" name="date" required></label>
  <label>Start <input type="time" name="time" required></label>
  <label>For <select name="minutes"><option value="30">30 min</option><option value="60" selected>1 h</option>
    <option value="120">2 h</option><option value="240">4 h</option></select></label>
  <label>What for <input name="purpose" maxlength="120"></label>
  <button>Book</button>
</form>
<p class="muted">During your booking only you can take the board; a lease that would run into it ends when it starts.</p>
{% if bookings %}<div class="wrap"><table><tr><th>Board</th><th>When</th><th>Who</th><th>What for</th><th></th></tr>
{% for x in bookings %}<tr><td>#{{ x.board }}</td><td>{{ day(x.start) }} {{ hhmm(x.start) }}–{{ hhmm(x.end) }}</td>
<td>{{ x.holder }}</td><td>{{ x.purpose }}</td>
<td><form method="post" action="bookings/{{ x.id }}/cancel"><input name="holder" placeholder="your name" size="10"><button>Cancel</button></form></td></tr>
{% endfor %}</table></div>{% endif %}
</section>

<section><h2>Run queue</h2>
{% if jobs %}<div class="wrap"><table><tr><th>#</th><th>Owner</th><th>Board</th><th>State</th><th>Starts</th><th>Note</th></tr>
{% for j in jobs %}<tr><td>{{ j.id }}</td><td>{{ j.owner }}</td>
<td>{{ j.board or (est[j.id][0] if j.id in est else j.board_req) }}</td><td>{{ j.state }}</td>
<td>{% if j.state == 'queued' and j.id in est %}~{{ day(est[j.id][1]) }} {{ hhmm(est[j.id][1]) }}{% elif j.started %}{{ hhmm(j.started) }}{% endif %}</td>
<td class="muted">{{ j.reason or '' }}</td></tr>{% endfor %}
</table></div>{% else %}<p class="muted">No runs yet. Runs are submitted with <code>fpga submit</code> on the hub host.</p>{% endif %}
</section>

<section><h2>Use in the last 7 days</h2>
{% if report %}<div class="wrap"><table><tr><th>Board</th><th>Who</th><th>Kind</th><th>Hours</th></tr>
{% for r in report %}<tr><td>#{{ r.board }}</td><td>{{ r.holder }}</td><td>{{ r.kind }}</td><td>{{ '%.1f'|format(r.minutes / 60) }}</td></tr>{% endfor %}
</table></div>{% else %}<p class="muted">Nothing recorded yet.</p>{% endif %}
</section>
</main></body></html>
"""


def main():
    cfg = config_mod.load()
    host, port = cfg['hub_listen'].rsplit(':', 1)
    app = create_app(cfg)
    from waitress import serve
    serve(app, host=host, port=int(port), threads=8)


if __name__ == '__main__':
    main()
