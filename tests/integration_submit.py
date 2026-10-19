"""`fpga submit/jobs/log/cancel` against a real hub served by waitress (no
consoles, no dispatch). Run: python tests/integration_submit.py"""
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

tmp = tempfile.mkdtemp(prefix='lab-sub-')
port = 8099
open(tmp + '/admintok', 'w').write('adm1n')
cfg = {'boards': {'2': {'url_file': tmp + '/nourl', 'settle_s': 35}}, 'data_dir': tmp + '/data',
       'hub_listen': f'127.0.0.1:{port}', 'hub_url': f'http://127.0.0.1:{port}',
       'hub_token_file': None, 'admin_token_file': tmp + '/admintok', 'webhook_file': None}
os.makedirs(cfg['data_dir'])
json.dump(cfg, open(tmp + '/cfg.json', 'w'))
app = hub_mod.create_app(cfg, consoles={'2': object()}, start_threads=False)  # never polled
from waitress import serve  # noqa: E402
threading.Thread(target=serve, args=(app,), kwargs={'host': '127.0.0.1', 'port': port}, daemon=True).start()
time.sleep(1)
open(tmp + '/x.bit', 'wb').write(b'B' * 1000)
open(tmp + '/x.bin', 'wb').write(b'I' * 2000)
res = []


def fpga(*args, admin=True):
    c = dict(cfg, admin_token_file=tmp + '/admintok' if admin else None)
    json.dump(c, open(tmp + '/cfg.json', 'w'))
    r = subprocess.run([sys.executable, '-m', 'fpga_lab.cli', *args], cwd=ROOT,
                       env=dict(os.environ, FPGA_LAB_CONFIG=tmp + '/cfg.json', HOME=tmp),
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


def check(name, cond, out=''):
    res.append(bool(cond))
    print(('PASS ' if cond else 'FAIL ') + name + ('' if cond else f' [{out}]'))


rc, out = fpga('submit', '--bit', tmp + '/x.bit', '--img', tmp + '/x.bin', '--board', '2', '--until', 'HELLO')
check('submit with admin token', rc == 0 and 'job 1 queued' in out, out)
stored = os.listdir(cfg['data_dir'] + '/files')
check('files stored by content', len(stored) == 2 and all('_' in f for f in stored), stored)
rc, out = fpga('submit', '--bit', tmp + '/x.bit', '--img', tmp + '/x.bin', admin=False)
check('submit without token refused', rc != 0 and 'lab token' in out, out)
rc, out = fpga('jobs')
check('jobs lists it', '1' in out and 'queued' in out, out)
rc, out = fpga('log', '1', admin=False)
check('log without token refused', rc != 0, out)
rc, out = fpga('cancel', '1')
check('cancel', rc == 0 and 'cancelled' in out, out)
print('RESULT', sum(res), 'of', len(res))
sys.exit(0 if all(res) else 1)
