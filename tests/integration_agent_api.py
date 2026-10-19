"""A remote user's `fpga` against a real hub over HTTP: user token, booking,
free slots, timeline, own-only cancel. Run: python tests/integration_agent_api.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fpga_lab import hub as hub_mod  # noqa: E402
from waitress import serve  # noqa: E402

tmp = tempfile.mkdtemp(prefix='lab-agent-')
port = 8098
open(tmp + '/admintok', 'w').write('adm1n')
hubcfg = {'boards': {'2': {'url_file': tmp + '/nourl', 'settle_s': 35}}, 'data_dir': tmp + '/data',
          'hub_listen': f'127.0.0.1:{port}', 'hub_url': f'http://127.0.0.1:{port}',
          'hub_token_file': None, 'admin_token_file': tmp + '/admintok', 'webhook_file': None}
os.makedirs(hubcfg['data_dir'])
app = hub_mod.create_app(hubcfg, consoles={'2': object()}, start_threads=False)
threading.Thread(target=serve, args=(app,), kwargs={'host': '127.0.0.1', 'port': port}, daemon=True).start()
time.sleep(1)
res = []


def run(home, cfg, *args):
    os.makedirs(home + '/.config/fpga-lab', exist_ok=True)
    json.dump(cfg, open(home + '/cfg.json', 'w'))
    r = subprocess.run([sys.executable, '-m', 'fpga_lab.cli', *args], cwd=ROOT,
                       env=dict(os.environ, FPGA_LAB_CONFIG=home + '/cfg.json', HOME=home),
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


def check(name, cond, out=''):
    res.append(bool(cond))
    print(('PASS ' if cond else 'FAIL ') + name + ('' if cond else f' [{out}]'))


admin = dict(hubcfg)
rc, out = run(tmp + '/admin', admin, 'user', 'add', 'diego')
tok = out.splitlines()[-1]
check('admin adds user, token shown once', rc == 0 and tok.startswith('flab_'), out)
home = tmp + '/diego'
diego = {'boards': {}, 'hub_url': f'http://127.0.0.1:{port}', 'data_dir': home + '/data',
         'admin_token_file': None, 'user_token_file': home + '/.config/fpga-lab/token'}
rc, out = run(home, diego, 'bookings')
check('no token: bookings readable', rc == 0, out)
rc, out = run(home, diego, 'book', '2', '--at', '2030-01-01 10:00', '--for', '2h')
check('no token: booking refused with a hint', rc != 0 and 'fpga user add' in out, out)
os.makedirs(home + '/.config/fpga-lab', exist_ok=True)
open(home + '/.config/fpga-lab/token', 'w').write(tok)
rc, out = run(home, diego, 'book', '2', '--at', '2030-01-01 10:00', '--for', '2h', '--why', 'agent test')
check('book with token', rc == 0 and 'for diego' in out, out)
bid = out.split()[1].rstrip(':')
rc, out = run(home, diego, 'book', '2', '--at', '2030-01-01 11:00', '--for', '1h')
check('overlap refused', rc != 0 and 'overlaps' in out, out)
rc, out = run(home, diego, 'free', '2', '--for', '2h')
check('free slot query', rc == 0 and 'board 2: free for 120 min' in out, out)
rc, out = run(home, diego, 'timeline', '--ahead', '200000m')
check('timeline lists the booking', rc == 0 and 'booked' in out and 'diego' in out, out)
rc, out = run(home, diego, 'unbook', bid)
check('unbook own', rc == 0, out)
print('RESULT', sum(res), 'of', len(res))
sys.exit(0 if all(res) else 1)
