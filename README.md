# fpga-lab

Leases, bookings, a run queue, usage and alerts for the lab's FPGA boards.
It runs on a coordinator host (apollo), never on a board host, and talks to
each board's console (fpga-remote) only through the console's lease API. The
consoles stay the authority: if the hub is down, the boards still work and
leases are still enforced.

## Commands

```
fpga status                                   every board: holder, time left, queue, bookings
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
- **Access:** job submission, cancel and qualify are accepted only from the hub
  host. The page and bookings are open to whoever can reach the port; grant
  that port in Tailscale only to the people who should book.

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
```
