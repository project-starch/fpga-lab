#!/bin/bash
# Install fpga-lab on the coordinator host (apollo): a venv with the `fpga`
# command and the hub, a hub token shared with the consoles, and a user unit.
#   ./deploy/install.sh            install / update; does not start the hub
#   ./deploy/install.sh --start    also enable and start the hub
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
DATA=$HOME/.local/share/fpga-lab
VENV=$DATA/venv
mkdir -p "$DATA" "$HOME/.config/fpga-lab" "$HOME/.local/bin" "$HOME/.config/systemd/user"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade "$HERE"
ln -sf "$VENV/bin/fpga" "$HOME/.local/bin/fpga"
CFG=$HOME/.config/fpga-lab/config.json
if [[ ! -f "$CFG" ]]; then
    ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
    [[ -n "$ip" ]] || { echo "no Tailscale address; is tailscale up?" >&2; exit 1; }
    cat >"$CFG" <<JSON
{"hub_listen": "$ip:8077", "hub_url": "http://$ip:8077"}
JSON
    echo "config written: $CFG (hub on $ip:8077, tailnet only)"
fi
TOK=$HOME/.config/fpga-lab/hub-token
if [[ ! -s "$TOK" ]]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_hex(24))' >"$TOK")
    echo "hub token created: $TOK (mode 600). Each console needs the same value as"
    echo "LEASE_HUB_TOKEN in its ~/.config/fpga-remote.env before bookings take effect."
fi
ADM=$HOME/.config/fpga-lab/admin-token
if [[ ! -s "$ADM" ]]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_hex(24))' >"$ADM")
    echo "admin token created: $ADM (mode 600; fpga submit/cancel/log/qualify use it)"
fi
chmod 700 "$HOME/.config/fpga-lab" "$DATA"
install -m 644 "$HERE/deploy/fpga-lab-hub.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
for b in 1 2; do
    f=$("$VENV/bin/python" - "$b" <<'PY'
import sys
from fpga_lab import config
print(config.load()['boards'].get(sys.argv[1], {}).get('url_file', ''))
PY
)
    [[ -r "${f/#\~/$HOME}" ]] && echo "board $b: console URL file present" || echo "board $b: MISSING console URL file ($f)"
done
if [[ "${1:-}" == --start ]]; then
    systemctl --user enable --now fpga-lab-hub
    systemctl --user --no-pager status fpga-lab-hub | head -5
fi
echo "done. Try: fpga status"
