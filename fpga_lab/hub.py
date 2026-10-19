"""The hub: one page for all boards, the booking calendar, the run queue,
usage and alerts. Runs on the coordinator (apollo), never on a board host,
so deploying it never restarts a console and never cuts board power.

The page shows no console URL: those carry each board's secret prefix and
stay in files on this host.
"""

import datetime
import hmac
import os
import threading
import time

from flask import Flask, abort, jsonify, redirect, render_template_string, request

from . import config as config_mod
from .client import Console, ConsoleError, read_url
from .db import DB
from .monitor import AlertSink, Monitor

LOCAL = ('127.0.0.1', '::1')


def make_consoles(cfg):
    out = {}
    for b, bc in cfg['boards'].items():
        out[b] = Console(read_url(bc['url_file']), name=f'board {b}')
    return out


def create_app(cfg=None, consoles=None, start_threads=True):
    cfg = cfg or config_mod.load()
    db = DB(os.path.join(cfg['data_dir'], 'lab.sqlite'))
    consoles = consoles if consoles is not None else make_consoles(cfg)
    hub_token = config_mod.read_secret(cfg.get('hub_token_file'))
    sink = AlertSink(os.path.join(cfg['data_dir'], 'alerts.log'),
                     config_mod.read_secret(cfg.get('webhook_file')))
    mon = Monitor(db, consoles, cfg, hub_token, sink)
    app = Flask(__name__)
    app.config['db'] = db
    app.config['monitor'] = mon

    # The hub host itself: loopback, plus the address the hub listens on
    # (when it is bound to the Tailscale address, the local CLI arrives from it).
    own = set(LOCAL) | {cfg['hub_listen'].rsplit(':', 1)[0]}

    def local_only():
        if request.remote_addr not in own:
            abort(403, 'only from the hub host')

    # ----------------------------------------------------------------- page

    @app.route('/')
    def page():
        now = time.time()
        report = db.usage_report(now - 7 * 86400)
        return render_template_string(PAGE, boards=mon.snapshot(), cfg=cfg, now=now,
                                      bookings=db.bookings(), jobs=db.jobs(20),
                                      alerts=db.open_alerts(), report=report,
                                      hhmm=_hhmm, day=_day, msg=request.args.get('msg'))

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
        # holder. The hub host itself may cancel anything.
        if request.remote_addr not in own and (f.get('holder') or '').strip() != rows[0]['holder']:
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
        local_only()
        d = request.get_json(silent=True) or {}
        need = ('owner', 'bit_path', 'bit_sha', 'img_path', 'img_sha')
        if any(not d.get(k) for k in need):
            return jsonify({'error': f'need {need}'}), 400
        board = str(d.get('board', 'any'))
        if board != 'any' and board not in consoles:
            return jsonify({'error': 'no such board'}), 400
        for p in (d['bit_path'], d['img_path']):
            if not os.path.isfile(p):
                return jsonify({'error': f'not a file on the hub host: {p}'}), 400
        jid = db.add_job(d['owner'], d.get('purpose', ''), board, d['bit_path'],
                         d['bit_sha'], d['img_path'], d['img_sha'],
                         d.get('until_regex'), int(d.get('timeout_s', 600)))
        return jsonify({'id': jid})

    @app.route('/api/jobs/<int:jid>/cancel', methods=['POST'])
    def api_cancel(jid):
        local_only()
        j = db.job(jid)
        if not j:
            return jsonify({'error': 'no such job'}), 404
        if j['state'] in ('queued', 'running'):
            db.set_job(jid, state='cancelled')
        return jsonify(db.job(jid))

    @app.route('/api/jobs/<int:jid>/log')
    def api_log(jid):
        try:
            with open(mon.runner.log_path(jid)) as fh:
                return fh.read(), 200, {'Content-Type': 'text/plain; charset=utf-8'}
        except FileNotFoundError:
            return 'no log yet\n', 404, {'Content-Type': 'text/plain; charset=utf-8'}

    @app.route('/api/qualify', methods=['POST'])
    def api_qualify():
        local_only()
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


def _parse_start(date_s, time_s):
    dt = datetime.datetime.strptime(f'{date_s} {time_s}', '%Y-%m-%d %H:%M')
    return dt.timestamp()


def _hhmm(t):
    return time.strftime('%H:%M', time.localtime(t)) if t else ''


def _day(t):
    return time.strftime('%a %d %b', time.localtime(t)) if t else ''


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
</style></head><body><main>
<header><h1>FPGA Boards</h1>
<div class="muted">Updated {{ hhmm(now) }} · refreshes every minute · times are local to the hub</div></header>
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
{% if jobs %}<div class="wrap"><table><tr><th>#</th><th>Owner</th><th>Board</th><th>State</th><th>Note</th></tr>
{% for j in jobs %}<tr><td><a href="api/jobs/{{ j.id }}/log">{{ j.id }}</a></td><td>{{ j.owner }}</td>
<td>{{ j.board or j.board_req }}</td><td>{{ j.state }}</td><td class="muted">{{ j.reason or '' }}</td></tr>{% endfor %}
</table></div>{% else %}<p class="muted">No runs yet. Submit with <code>fpga submit</code> on the hub host.</p>{% endif %}
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
    from werkzeug.serving import run_simple
    run_simple(host, int(port), app, threaded=True)


if __name__ == '__main__':
    main()
