# Loom

Loom weaves your devices into one mesh. A coordinator on your always-on node
tracks live CPU/RAM/disk for every machine you own, and lets any device queue
work for any other — triggered from a dashboard, by file changes, or on a
schedule.

Built for a home network with one persistent node (a Raspberry Pi running 24/7)
coordinating machines that come and go — a laptop that sleeps, a desktop that's
only on in the evenings.

```
                    ┌──────────────────────────────┐
                    │   Coordinator (Flask)         │
                    │   on the persistent node      │
                    │                               │
                    │   · device registry           │
                    │   · job queue + assignment    │
                    │   · password-gated dashboard  │
                    └───────────────┬───────────────┘
                                    │  HTTPS + per-device API key
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
        ┌─────▼──────┐        ┌─────▼──────┐        ┌─────▼──────┐
        │   Agent     │        │   Agent     │        │   Agent     │
        │  (macOS)    │        │  (Linux)    │        │ (Windows)   │
        │             │        │             │        │             │
        │  heartbeat  │        │  heartbeat  │        │  heartbeat  │
        │  job poll   │        │  job poll   │        │  job poll   │
        │  file watch │        │  file watch │        │  file watch │
        │  cron jobs  │        │  cron jobs  │        │  cron jobs  │
        └─────────────┘        └─────────────┘        └─────────────┘
```

## What it does

- **Live dashboard** — every node's online status, CPU%, RAM used/total, disk
  usage and uptime, refreshed every 4 seconds.
- **Job queue** — submit a shell command and aim it at a specific device, a
  capability tag (`gpu`, `desktop`, …), or nothing at all, in which case it
  falls back to your persistent node.
- **Job logs and cancellation** — read any job's captured output from the
  dashboard, and cancel one mid-run. Cancelling kills the whole process group,
  so the work actually stops.
- **Auto-triggers** — each agent can watch directories for file changes and
  either run something locally or queue it for a better-suited node, plus
  cron-style scheduled commands. No coordinator config needed.
- **Survives failure** — agents back off and reconnect when the coordinator
  goes away; jobs whose agent dies mid-run are marked `lost` rather than
  hanging in `running` forever.
- **Cross-platform** — one agent file runs unmodified on macOS, Linux
  (including Raspberry Pi), and Windows.
- **No VPN required** — auth is per-device API keys plus a dashboard password,
  not network position. A plain HTTPS tunnel is enough; a mesh VPN is optional.

## Quick start

Full walkthrough with copy-pasteable commands and a verification step after
every command: **[docs/PI_SETUP.md](docs/PI_SETUP.md)**.
Condensed reference: **[docs/SETUP.md](docs/SETUP.md)**.

```bash
# 1. Coordinator, on your always-on machine
cd coordinator
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
cd ../scripts && ../coordinator/venv/bin/python gen_api_key.py --dashboard-secret
#    ^ paste the two printed values into coordinator/config.yaml

# 2. Register each machine (exactly one gets --persistent)
../coordinator/venv/bin/python gen_api_key.py --add-device \
    --name pi-node --platform linux --persistent
../coordinator/venv/bin/python gen_api_key.py --add-device \
    --name macbook --platform macos --tags desktop,dev

# 3. Start the coordinator
cd ../coordinator && ./venv/bin/python app.py     # dev; use gunicorn for real

# 4. Agent, on every machine
cd ../agent
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml   # fill in url, api_key, device_name
./venv/bin/python loom_agent.py
```

Then run both as services so they survive reboots — unit files for systemd,
launchd, and NSSM are included.

## Repo layout

```
loom/
├── coordinator/                    Flask app, dashboard, job queue
│   ├── app.py                      routes, auth, job assignment, reaper
│   ├── db.py                       connection + schema management
│   ├── schema.sql                  tables and indexes
│   ├── loom-coordinator.service    systemd unit (gunicorn)
│   ├── templates/  static/
│   └── config.example.yaml
├── agent/                          the daemon that runs on every node
│   ├── loom_agent.py
│   ├── config.example.yaml
│   └── service/                    24/7 setup per platform
│       ├── loom-agent.service          Linux / Raspberry Pi (systemd)
│       ├── install_macos.sh             macOS (launchd, generated)
│       └── windows_service.md          Windows (NSSM)
├── scripts/
│   └── gen_api_key.py              admin CLI: devices, keys, dashboard secret
└── docs/
    ├── PI_SETUP.md                 step-by-step persistent-node runbook
    └── SETUP.md                    condensed reference
```

## Job targeting

When an agent polls, the coordinator takes the **oldest** pending job matching
any of:

1. targeted at that device's id,
2. tagged with a tag that device carries, or
3. untargeted — and only for the device marked `persistent`.

Rule 3 is why exactly one node should be persistent: it guarantees ownerless
work has precisely one home. The admin CLI refuses to create a second.

The claim runs inside a `BEGIN IMMEDIATE` transaction, so two agents polling at
the same instant can't both take the same job. An agent stops polling while it's
at `max_concurrent_jobs`, which keeps one node from hoarding the queue.

## Auto-triggers

Configured per-device in `agent/config.yaml`:

```yaml
watch:
  - path: "~/render-queue"
    run: "python ~/scripts/handle-render.py {path}"
    mode: local          # run here immediately (default)
    debounce: 2.0        # coalesce the event burst one file save produces

  - path: "~/inbox"
    run: "python ~/scripts/transcode.py {path}"
    mode: queue          # hand it to the coordinator instead
    target_tag: gpu      # ...so the GPU box picks it up

cron:
  - at: "02:00"
    run: "bash ~/scripts/nightly-backup.sh"
```

File watching uses `watchdog`; scheduling uses `schedule` and runs in the
agent's local time. Both are optional — leave the sections out and the agent
just does heartbeats and job polling.

## Security notes

- `coordinator/config.yaml`, every `agent/config.yaml`, and `coordinator/loom.db`
  are gitignored. They hold your password hash, per-device API keys, and job
  history — don't commit them.
- Each device has its own API key. Rotate one without touching the others:
  `gen_api_key.py --rotate-key --name <device>`. The old key stops working
  immediately.
- The dashboard password is hashed with Werkzeug's PBKDF2. Login is throttled to
  5 attempts per IP per 5 minutes; the counter is in memory, so restarting the
  service is your way back in if you lock yourself out.
- Session cookies are `HttpOnly` + `SameSite=Lax`; set `secure_cookie: true`
  once you're on HTTPS. State-changing requests require a CSRF token.
- **Jobs are arbitrary shell commands.** They run with whatever permissions the
  agent process has. Run the agent as a dedicated, scoped user — not root, not
  Administrator.
- Bind the coordinator to `127.0.0.1` and reach it through a tunnel or reverse
  proxy rather than exposing port 5055 directly.
- Never set `debug: true` on a reachable coordinator — Flask's debugger is a
  remote code execution console.

## Current limitations

- **No automatic retry.** A job marked `lost` (agent died mid-run) is not
  re-queued; you re-submit it yourself.
- **Assignment isn't load-aware.** Nodes self-limit via `max_concurrent_jobs`,
  but the coordinator doesn't compare CPU/RAM across idle nodes when choosing
  where work lands.
- **Single admin account.** One shared dashboard password, no per-user logins
  or audit trail of who submitted what.
- **Single coordinator.** If the persistent node is down, the mesh has no queue.
  Agents keep running and reconnect on their own, but nothing is assigned in the
  meantime.
- **Windows is untested on real hardware.** The code is platform-agnostic Python
  and the Windows-specific paths (drive-letter disk root, `terminate()` instead
  of process groups) are handled explicitly — but it hasn't been run on an
  actual Windows box. Report anything you hit.

## License

MIT — see [LICENSE](LICENSE).
