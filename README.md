# fpga-lab

Leases, bookings, a run queue, usage and alerts for the lab's FPGA boards.
It runs on a coordinator host (apollo), never on a board host, and talks to
each board's console (fpga-remote) only through the console's lease API. The
consoles stay the authority: if the hub is down, the boards still work and
leases are still enforced.

## For agents

Everything is an HTTP API; `GET /api` (or `/llms.txt`) on the hub returns a
plain-text guide an agent can read directly. Each person or agent gets a
token from the lab admin (`fpga user add NAME`, shown once, stored hashed);
bookings and runs are then attributed to that name, and only their owner (or
the admin) can cancel them or read a run's log.

## Commands

```
fpga status                                   every board: holder, time left, queue, bookings
fpga timeline | fpga free 2 --for 2h          who has / will have each board; earliest free slot
fpga book 2 --at "2026-10-21 10:00" --for 2h --why "..." | fpga bookings | fpga unbook 7
fpga take 2 --for 60m --why "boot test"       lease board 2 (60 min default, 2 h cap)
fpga extend 2 | fpga keep 2 | fpga release 2
fpga queue 2                                  wait for a held board; you get 5 min to claim it
fpga submit --bit X.bit --img Y.bin [--board any|1|2] [--until REGEX] [--timeout 10m]
fpga jobs | fpga cancel 17 | fpga log 17
fpga qualify 2 X.bit                          X passed the cross-board canary on board 2
fpga report --days 7
```

A submitted run:
1. takes an agent lease on the first free qualified board;
2. uploads the files if missing;
3. flashes the bitstream non-volatile if it is not resident;
4. power-cycles the board and waits the board's settle time (35 s on board 2);
5. loads the image with its sha256;
6. captures the UART until `--until` matches or the timeout passes;
7. powers off and releases.

When a person queues for the board, the run finishes on the time it has and
does not renew.

## Hub

A page for all boards (`http://<apollo tailscale address>:8077/`, tailnet only), a booking form, the run queue,
7-day usage and open alerts.
- **Bookings** are pushed to each console every 5 min and on change. During
  a booking only its holder may take the board, and nobody's lease may run
  into it.
- **Alerts:** console unreachable for 5 min, health check not ok or silent, a
  lease idle past its release. They are written to
  `~/.local/share/fpga-lab/alerts.log`, and also to a Slack-compatible webhook
  when `webhook_file` is set in the config.
- **Access:** the page and the booking form are open to whoever can reach the
  port (grant it in Tailscale to the people who should book). Submitting,
  cancelling and reading runs, and qualifying bitstreams, need the admin token
  (`~/.config/fpga-lab/admin-token`, mode 600), which `fpga` sends for the hub's
  owner. No address is trusted: on a shared server every account could
  otherwise act as admin. Submitted files travel in the request and are
  stored by content hash; the hub never opens a path a caller names.

## Use from another machine (a colleague's laptop or agent)

```
pip install git+https://github.com/project-starch/fpga-lab
mkdir -p ~/.config/fpga-lab
echo '{"hub_url": "http://100.112.174.77:8077", "boards": {}}' > ~/.config/fpga-lab/config.json
(umask 077; echo '<token from the admin>' > ~/.config/fpga-lab/token)
fpga timeline
```

## Install on apollo

```
./deploy/install.sh            # venv, `fpga` in ~/.local/bin, hub token, unit
./deploy/install.sh --start    # also start the hub
```
- **Console URLs.** They come from `~/.claude-kisp/secrets/fpga-console-url`
  (board 1) and `...-board2` (board 2), which are never printed. Override them
  in `~/.config/fpga-lab/config.json`.
- **Bookings need the hub token** (`~/.config/fpga-lab/hub-token`) set as
  `LEASE_HUB_TOKEN` in each console's env file, followed by a console restart.
  A restart cuts board power, so pick an idle moment.

## Tests

```
FPGA_REMOTE_DIR=../fpga-remote python -m pytest tests/
python tests/integration_console.py ../fpga-remote <python>   # against the real console code, no hardware
python tests/integration_submit.py                             # fpga submit through a real hub
python tests/integration_agent_api.py                          # a remote user's token, bookings, timeline
```
