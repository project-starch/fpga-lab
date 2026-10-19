"""fpga -- leases and runs on the lab's FPGA boards.

  fpga status                         every board: holder, time left, queue
  fpga take BOARD [--for 60m] [--why TEXT] [--name NAME]
  fpga extend BOARD | fpga keep BOARD | fpga release BOARD
  fpga queue BOARD [--why TEXT]       wait for a held board
  fpga timeline [--ahead 24h]         who has had / has / will have each board
  fpga free BOARD [--for 60m]         earliest free slot
  fpga book BOARD --at "2026-10-21 10:00" [--for 2h] [--why TEXT]
  fpga bookings | fpga unbook ID
  fpga submit --bit X.bit --img Y.bin [--board any|1|2] [--until REGEX] [--timeout 10m]
  fpga jobs | fpga cancel ID | fpga log ID
  fpga qualify BOARD BIT.bit          (admin) bitstream passed the cross-board canary
  fpga user add NAME | fpga user list | fpga user remove NAME      (admin)
  fpga report [--days 7]

take/extend/keep/release/queue talk to a board's console directly (needs its
URL file); the lease token is kept in ~/.local/state/fpga-lab/leases.json
(mode 600). Everything else talks to the hub: your token goes in
~/.config/fpga-lab/token (mode 600), or the admin token on the hub host.
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import time

import requests

from . import config as config_mod
from .client import Console, ConsoleError, read_url

STATE = os.path.expanduser('~/.local/state/fpga-lab/leases.json')


def _hhmm(t):
    return time.strftime('%H:%M', time.localtime(t)) if t else '?'


def _minutes(s):
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(m|min|h)?\s*', str(s))
    if not m:
        raise argparse.ArgumentTypeError('use e.g. 30m, 90m, 2h')
    v = float(m.group(1))
    return v * 60 if m.group(2) == 'h' else v


def _seconds(s):
    return int(_minutes(s) * 60)


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _load_tokens():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def _save_tokens(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(d, fh)
    os.replace(tmp, STATE)


def _console(cfg, board):
    if board not in cfg['boards']:
        raise SystemExit(f'no board {board!r}; boards: {", ".join(cfg["boards"])}')
    return Console(read_url(cfg['boards'][board]['url_file']), name=f'board {board}')


def _token(board):
    t = _load_tokens().get(board)
    if not t:
        raise SystemExit(f'you hold no lease on board {board} (fpga take {board})')
    return t


def _hub(cfg, method, path, **kw):
    headers = kw.pop('headers', {})
    tok = config_mod.read_secret(cfg.get('admin_token_file'))
    if tok:
        headers['X-Lab-Admin'] = tok
    utok = config_mod.read_secret(cfg.get('user_token_file'))
    if utok:
        headers['X-Lab-Token'] = utok
    tok = tok or utok
    try:
        r = requests.request(method, cfg['hub_url'].rstrip('/') + path, timeout=300,
                             headers=headers, **kw)
    except requests.RequestException as e:
        raise SystemExit(f'hub not reachable at {cfg["hub_url"]}: {type(e).__name__}')
    if r.headers.get('Content-Type', '').startswith('application/json'):
        body = r.json()
    else:
        body = r.text
    if r.status_code >= 400:
        msg = body.get('error') if isinstance(body, dict) else body
        if r.status_code == 401:
            msg = 'needs a lab token in ~/.config/fpga-lab/token (ask the lab admin: fpga user add NAME)'
        raise SystemExit(f'hub: {msg}')
    return body


# ------------------------------------------------------------------ commands

def cmd_status(cfg, a):
    for b in cfg['boards']:
        try:
            st = _console(cfg, b).lease()
        except ConsoleError as e:
            if e.status == 404:
                print(f'board {b}: console has no leases yet (old console code)')
            else:
                print(f'board {b}: unreachable ({e.message})')
            continue
        except SystemExit as e:
            print(f'board {b}: {e}')
            continue
        L = st.get('lease')
        if L:
            line = f"held by {L['holder']} until {_hhmm(L['until'])}"
            if L.get('purpose'):
                line += f" ({L['purpose']})"
            if L.get('idle_warning'):
                line += ' -- idle, releasing soon'
        elif st.get('offer'):
            line = f"offered to {st['offer']['holder']} until {_hhmm(st['offer']['until'])}"
        else:
            line = 'free'
        extra = []
        if st.get('queue'):
            extra.append(f"{len(st['queue'])} waiting")
        nxt = [r for r in st.get('reservations') or [] if r['end'] > time.time()]
        if nxt:
            r = nxt[0]
            extra.append(f"booked {r['holder']} {_hhmm(r['start'])}-{_hhmm(r['end'])}")
        extra.append(f"leases {st.get('enforce')}")
        print(f'board {b}: {line}  [{"; ".join(extra)}]')
    return 0


def cmd_take(cfg, a):
    c = _console(cfg, a.board)
    name = a.name or getpass.getuser()
    tokens = _load_tokens()
    try:
        r = c.take(name, a.why or '', kind='person', minutes=a.for_,
                   ticket=tokens.get(f'ticket:{a.board}'))
    except ConsoleError as e:
        print(f'not taken: {e.message}', file=sys.stderr)
        return 1
    tokens[a.board] = r['token']
    tokens.pop(f'ticket:{a.board}', None)
    _save_tokens(tokens)
    print(f'board {a.board} is yours until {_hhmm(r["until"])}')
    return 0


def cmd_extend(cfg, a):
    try:
        r = _console(cfg, a.board).renew(_token(a.board))
    except ConsoleError as e:
        print(f'not extended: {e.message}', file=sys.stderr)
        return 1
    print(f'extended until {_hhmm(r["until"])}')
    return 0


def cmd_keep(cfg, a):
    try:
        _console(cfg, a.board).keep(_token(a.board))
    except ConsoleError as e:
        print(f'not kept: {e.message}', file=sys.stderr)
        return 1
    print('kept')
    return 0


def cmd_release(cfg, a):
    tokens = _load_tokens()
    try:
        _console(cfg, a.board).release(_token(a.board))
    except ConsoleError as e:
        print(f'not released: {e.message}', file=sys.stderr)
        if e.code in ('not_holder', 'no_lease'):
            tokens.pop(a.board, None)
            _save_tokens(tokens)
        return 1
    tokens.pop(a.board, None)
    _save_tokens(tokens)
    print(f'board {a.board} released')
    return 0


def cmd_queue(cfg, a):
    try:
        r = _console(cfg, a.board).queue_join(a.name or getpass.getuser(), a.why or '')
    except ConsoleError as e:
        print(f'not queued: {e.message}', file=sys.stderr)
        return 1
    tokens = _load_tokens()
    tokens[f'ticket:{a.board}'] = r['ticket']
    _save_tokens(tokens)
    print(f'number {r["position"]} in the queue for board {a.board}; '
          f'when offered, run: fpga take {a.board}')
    return 0


def cmd_submit(cfg, a):
    for p in (a.bit, a.img):
        if not os.path.isfile(p):
            raise SystemExit(f'not a file: {p}')
    if a.until:
        re.compile(a.until)
    form = {'owner': a.name or getpass.getuser(), 'purpose': a.why or '',
            'board': a.board, 'until_regex': a.until or '', 'timeout_s': str(a.timeout)}
    with open(a.bit, 'rb') as fb, open(a.img, 'rb') as fi:
        r = _hub(cfg, 'POST', '/api/jobs', data=form,
                 files={'bit': (os.path.basename(a.bit), fb), 'img': (os.path.basename(a.img), fi)})
    print(f'job {r["id"]} queued')
    return 0


def cmd_jobs(cfg, a):
    for j in _hub(cfg, 'GET', '/api/jobs'):
        when = _hhmm(j['finished'] or j['started'] or j['created'])
        print(f"{j['id']:>5}  {j['state']:<9} board {j['board'] or j['board_req']:<3} "
              f"{j['owner']:<12} {when}  {j['reason'] or ''}")
    return 0


def cmd_cancel(cfg, a):
    j = _hub(cfg, 'POST', f'/api/jobs/{a.id}/cancel')
    print(f"job {a.id}: {j['state']}")
    return 0


def cmd_log(cfg, a):
    sys.stdout.write(_hub(cfg, 'GET', f'/api/jobs/{a.id}/log'))
    return 0


def cmd_qualify(cfg, a):
    _hub(cfg, 'POST', '/api/qualify', json={'board': a.board, 'bit_sha': _sha(a.bit),
                                           'note': os.path.basename(a.bit)})
    print(f'board {a.board}: {os.path.basename(a.bit)} qualified')
    return 0


def _when(s):
    import datetime
    s = s.strip().replace(' ', 'T', 1)
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone()          # your local time
    return dt.isoformat()


def cmd_book(cfg, a):
    b = _hub(cfg, 'POST', '/api/bookings', json={'board': a.board, 'start': _when(a.at),
                                                 'minutes': int(a.for_), 'purpose': a.why or ''})
    print(f"booking {b['id']}: board {b['board']} {_hhmm(b['start'])}-{_hhmm(b['end'])} "
          f"{time.strftime('%a %d %b', time.localtime(b['start']))} for {b['holder']}")
    return 0


def cmd_bookings(cfg, a):
    rows = _hub(cfg, 'GET', '/api/bookings')
    if not rows:
        print('no bookings')
    for b in rows:
        print(f"{b['id']:>5}  board {b['board']}  {time.strftime('%a %d %b %H:%M', time.localtime(b['start']))}"
              f"-{_hhmm(b['end'])}  {b['holder']:<14} {b.get('purpose') or ''}")
    return 0


def cmd_unbook(cfg, a):
    _hub(cfg, 'DELETE', f'/api/bookings/{a.id}')
    print(f'booking {a.id} cancelled')
    return 0


def cmd_free(cfg, a):
    r = _hub(cfg, 'GET', f'/api/free?board={a.board}&minutes={int(a.for_)}')
    for b, v in r.items():
        print(f"board {b}: free for {int(a.for_)} min from "
              f"{time.strftime('%a %d %b %H:%M', time.localtime(v['start']))}")
    return 0


def cmd_timeline(cfg, a):
    tl = _hub(cfg, 'GET', f'/api/timeline?back=0&ahead={a.ahead / 60:.2f}')
    now = tl['now']
    for row in tl['rows']:
        print(f"board {row['board']}:")
        segs = sorted((s for s in row['segs'] if s['end'] > now), key=lambda s: s['start'])
        if not segs:
            print('  free')
        for s in segs:
            kind = {'booking': 'booked', 'job-est': 'run (est.)'}.get(s['type'], 'lease')
            print(f"  {time.strftime('%a %H:%M', time.localtime(max(s['start'], now)))}"
                  f"-{_hhmm(s['end'])}  {kind:<10} {s['label']}")
    return 0


def cmd_user(cfg, a):
    if a.action == 'add':
        r = _hub(cfg, 'POST', '/api/users', json={'name': a.name})
        print(f"user {r['name']} added. Token (shown once; give it to them privately):\n{r['token']}")
    elif a.action == 'list':
        for u in _hub(cfg, 'GET', '/api/users'):
            print(u['name'])
    elif a.action == 'remove':
        _hub(cfg, 'DELETE', f'/api/users/{a.name}')
        print(f'user {a.name} removed')
    return 0


def cmd_report(cfg, a):
    rows = _hub(cfg, 'GET', f'/api/report?days={a.days}')
    if not rows:
        print('nothing recorded')
    for r in rows:
        print(f"board {r['board']}  {r['holder']:<20} {r['kind']:<7} {r['minutes'] / 60:6.1f} h")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='fpga', description=__doc__.split('\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split('\n', 2)[2])
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    t = sub.add_parser('take')
    t.add_argument('board')
    t.add_argument('--for', dest='for_', type=_minutes, default=None, metavar='60m')
    t.add_argument('--why', default='')
    t.add_argument('--name')
    for n in ('extend', 'keep', 'release'):
        sub.add_parser(n).add_argument('board')
    q = sub.add_parser('queue')
    q.add_argument('board')
    q.add_argument('--why', default='')
    q.add_argument('--name')
    s = sub.add_parser('submit')
    s.add_argument('--bit', required=True)
    s.add_argument('--img', required=True)
    s.add_argument('--board', default='any')
    s.add_argument('--until')
    s.add_argument('--timeout', type=_seconds, default=600, metavar='10m')
    s.add_argument('--why', default='')
    s.add_argument('--name')
    sub.add_parser('jobs')
    for n in ('cancel', 'log'):
        sub.add_parser(n).add_argument('id', type=int)
    qq = sub.add_parser('qualify')
    qq.add_argument('board')
    qq.add_argument('bit')
    tl = sub.add_parser('timeline')
    tl.add_argument('--ahead', type=_minutes, default=24 * 60, metavar='24h')
    fr = sub.add_parser('free')
    fr.add_argument('board')
    fr.add_argument('--for', dest='for_', type=_minutes, default=60, metavar='60m')
    bk = sub.add_parser('book')
    bk.add_argument('board')
    bk.add_argument('--at', required=True, help='"2026-10-21 10:00" (your local time) or ISO 8601')
    bk.add_argument('--for', dest='for_', type=_minutes, default=60, metavar='60m')
    bk.add_argument('--why', default='')
    sub.add_parser('bookings')
    sub.add_parser('unbook').add_argument('id', type=int)
    us = sub.add_parser('user')
    us.add_argument('action', choices=('add', 'list', 'remove'))
    us.add_argument('name', nargs='?')
    r = sub.add_parser('report')
    r.add_argument('--days', type=float, default=7)
    a = p.parse_args(argv)
    cfg = config_mod.load()
    return globals()['cmd_' + a.cmd](cfg, a)


if __name__ == '__main__':
    sys.exit(main())
