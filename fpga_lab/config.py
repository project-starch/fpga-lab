"""Where the boards are and where fpga-lab keeps its state.

~/.config/fpga-lab/config.json (optional; defaults below match apollo):

    {
      "boards": {
        "1": {"url_file": "~/.claude-kisp/secrets/fpga-console-url", "settle_s": 30},
        "2": {"url_file": "~/.claude-kisp/secrets/fpga-console-url-board2", "settle_s": 35}
      },
      "hub_listen": "100.112.174.77:8077",
      "hub_url": "http://100.112.174.77:8077",
      "hub_token_file": "~/.config/fpga-lab/hub-token",     (shared with the consoles)
      "admin_token_file": "~/.config/fpga-lab/admin-token", (submit/cancel/log/qualify)
      "data_dir": "~/.local/share/fpga-lab",
      "webhook_file": null
    }

hub_listen: bind to the host's Tailscale address, not 0.0.0.0, so the page
and booking form are reachable only over the tailnet (and Tailscale grants
decide who). deploy/install.sh writes this file with that address.

settle_s is the wait from power-on to the first examine: board 2 needs 35 s,
measured; a volatile flash never brings up the debug TAP, so jobs always flash
non-volatile and power-cycle.
"""

import json
import os

DEFAULTS = {
    'boards': {
        '1': {'url_file': '~/.claude-kisp/secrets/fpga-console-url', 'settle_s': 30},
        '2': {'url_file': '~/.claude-kisp/secrets/fpga-console-url-board2', 'settle_s': 35},
    },
    'hub_listen': '0.0.0.0:8077',
    'hub_url': 'http://127.0.0.1:8077',
    'hub_token_file': '~/.config/fpga-lab/hub-token',
    'admin_token_file': '~/.config/fpga-lab/admin-token',
    'data_dir': '~/.local/share/fpga-lab',
    'webhook_file': None,
}


def config_path():
    return os.path.expanduser(os.environ.get('FPGA_LAB_CONFIG', '~/.config/fpga-lab/config.json'))


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(config_path()) as fh:
            user = json.load(fh)
    except FileNotFoundError:
        user = {}
    for k, v in user.items():
        cfg[k] = v
    cfg['data_dir'] = os.path.expanduser(cfg['data_dir'])
    os.makedirs(cfg['data_dir'], exist_ok=True)
    return cfg


def read_secret(path):
    """A small secret file (hub token, webhook URL). None if absent."""
    if not path:
        return None
    try:
        with open(os.path.expanduser(path)) as fh:
            return fh.read().strip() or None
    except OSError:
        return None
