"""fpga_lab.client against the real console code (fpga-remote's
tests/dev_server.py: no hardware, subprocess refused). Not collected by
pytest; run:  python tests/integration_console.py <fpga-remote-checkout> <python>
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fpga_lab.client import Console, ConsoleError  # noqa: E402

repo, py = sys.argv[1], sys.argv[2]
port = 5098
tmp = tempfile.mkdtemp(prefix='lab-int-')
env = dict(os.environ, XDG_STATE_HOME=tmp, FPGA_AUDIT_LOG=tmp + '/audit.jsonl',
           LEASE_ENFORCE='on', LEASE_HUB_TOKEN='hubsecret',
           FPGA_HEALTH_STATUS=tmp + '/none.json')
srv = subprocess.Popen([py, os.path.join(repo, 'tests', 'dev_server.py'), str(port)],
                       env=env, stdout=open(tmp + '/srv.log', 'w'), stderr=subprocess.STDOUT)
res = []


def check(name, cond):
    res.append(bool(cond))
    print(('PASS ' if cond else 'FAIL ') + name, flush=True)


def code(fn):
    try:
        fn()
    except ConsoleError as e:
        return e.code
    return None


try:
    c = Console(f'http://127.0.0.1:{port}/', name='dev')
    for _ in range(60):
        try:
            c.lease()
            break
        except ConsoleError:
            time.sleep(0.5)
    check('health unknown without status file', c.health()['verdict'] == 'unknown')
    L = c.take('lane', 'int test', kind='agent')
    tok = L['token']
    check('take gives token', len(tok) > 20)
    check('second take refused: held', code(lambda: c.take('x', kind='agent')) == 'held')
    with c.socket() as nobody:
        try:
            nobody.set_power(True, timeout=3)
            refused = False
        except ConsoleError as e:
            refused = e.code in ('held', 'no_lease')
        check('socket without token cannot power on', refused)
    with c.socket(tok) as live:
        live.set_power(True)
        check('holder powers on over socket', c.state()['power'] == 'on')
        live.set_power(False)
        check('holder powers off', c.state()['power'] == 'off')
    check('renew ok', c.renew(tok)['until'] > time.time())
    q = c.queue_join('Kim')
    check('renew yields while queued', code(lambda: c.renew(tok)) == 'yield')
    c.queue_leave(q['ticket'])
    check('renew ok after queue left', c.renew(tok)['until'] > time.time())
    now = time.time()
    check('bookings need hub token',
          code(lambda: c.set_reservations('wrong', [])) == 'forbidden')
    c.set_reservations('hubsecret', [{'id': 'b', 'holder': 'Kim', 'start': now + 120, 'end': now + 900}])
    check('agent renew capped by booking', c.renew(tok)['until'] <= now + 121)
    c.release(tok)
    check('released', c.lease()['lease'] is None)
    check('reservation visible', c.lease()['reservations'][0]['holder'] == 'Kim')
finally:
    srv.terminate()
    srv.wait(10)
print('RESULT', sum(res), 'of', len(res))
sys.exit(0 if all(res) else 1)
