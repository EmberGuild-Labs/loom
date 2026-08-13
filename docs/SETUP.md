# Loom Setup Guide

Setting up the **persistent node** (the always-on machine running the
coordinator)? Follow **[PI_SETUP.md](PI_SETUP.md)** instead — it's the same
material as a step-by-step runbook with verification after every command.

This page is the shorter reference version, plus the per-platform agent setup.

## 1. Architecture recap

- **Coordinator** — one Flask app + one SQLite file, running on your persistent
  node. Hosts the dashboard and the API every agent talks to. It never executes
  jobs itself; it only hands them out.
- **Agent** — a small daemon on *every* node you want in the mesh, including
  the persistent node itself. Sends heartbeats, polls for jobs, runs them,
  reports results.
- **Auth** — each agent carries its own per-device API key, so a leaked key
  compromises exactly one machine and can be rotated on its own. The dashboard
  is gated by a single admin password. No VPN or mesh network required: it's
  plain HTTPS, so a Cloudflare Tunnel is enough. Tailscale/Headscale works too
  if you're already on it, but Loom doesn't assume it.

## 2. Coordinator

```bash
cd loom/coordinator
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml

# generates secret_key + password_hash to paste into config.yaml
cd ../scripts && ../coordinator/venv/bin/python gen_api_key.py --dashboard-secret
```

Run it in the foreground to check the config:

```bash
cd ../coordinator && ./venv/bin/python app.py
```

For anything permanent, run it under gunicorn via the supplied
`loom-coordinator.service` rather than the Flask dev server — see
[PI_SETUP.md](PI_SETUP.md) STEP 6. Keep `--workers 1`: the stale-job reaper and
the login throttle both hold state in process memory.

Expose it however suits you — Cloudflare Tunnel, an nginx reverse proxy, or
just your LAN. The app sets `ProxyFix`, so it works correctly behind a proxy
and even when mounted under a path prefix.

## 3. Register devices

Run on the coordinator. It writes straight to `coordinator/loom.db` and creates
the database if it doesn't exist yet, so you can do this before ever starting
the app.

```bash
cd loom/scripts
PY=../coordinator/venv/bin/python

# the always-on node — exactly one device gets --persistent
$PY gen_api_key.py --add-device --name pi-node --platform linux --persistent

$PY gen_api_key.py --add-device --name macbook --platform macos --tags desktop,dev
$PY gen_api_key.py --add-device --name desktop --platform windows --tags gpu,desktop
```

Other admin commands:

```bash
$PY gen_api_key.py --list-devices [--show-keys]
$PY gen_api_key.py --rotate-key --name macbook      # revokes the old key immediately
$PY gen_api_key.py --remove-device --name old-box
$PY gen_api_key.py --set-persistent --name new-pi   # move the persistent flag
```

## 4. Agent (every node)

```bash
cd loom/agent
python3 -m venv venv                       # Windows: python -m venv venv
./venv/bin/pip install -r requirements.txt # Windows: venv\Scripts\pip
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

| Key | Value |
|---|---|
| `coordinator_url` | `https://mesh.your-domain.com` (or `http://127.0.0.1:5055` on the coordinator itself) |
| `api_key` | the key printed for *this* device in step 3 |
| `device_name` | must exactly match the registered `--name` |
| `max_concurrent_jobs` | how many jobs this node runs at once (default 2) |
| `job_timeout` | seconds before a job is killed (default 3600) |

Test in the foreground:

```bash
./venv/bin/python loom_agent.py
```

The node should appear online within ~10 seconds. `Ctrl+C`, then install it as
a service:

- **Linux / Raspberry Pi** — `service/loom-agent.service` (systemd)
- **macOS** — `service/install_macos.sh` (launchd)
- **Windows** — `service/windows_service.md` (NSSM)

## 5. Job targeting

When an agent polls, the coordinator picks the **oldest** pending job that
matches, in one pass:

1. targeted at this device's id, **or**
2. tagged with a tag this device carries, **or**
3. untargeted — and only if this device is the persistent one

So a `gpu`-tagged job waits for a `gpu` node, and an untargeted job always has
exactly one home. The claim happens inside a `BEGIN IMMEDIATE` transaction, so
two agents polling simultaneously can never both take the same job.

An agent stops polling entirely while it's at `max_concurrent_jobs`, which is
what keeps a busy node from hoarding the queue.

## 6. Auto-triggers

Per-device, in `agent/config.yaml` — no coordinator changes needed.

```yaml
watch:
  - path: "~/render-queue"
    run: "python ~/scripts/handle-render.py {path}"
    mode: local          # run it here, now
    debounce: 2.0        # coalesce the burst of events one file save emits

  - path: "~/inbox"
    run: "python ~/scripts/transcode.py {path}"
    mode: queue          # submit to the coordinator instead
    target_tag: gpu      # ...and let the GPU box take it

cron:
  - at: "02:00"
    run: "bash ~/scripts/nightly-backup.sh"
```

`mode: local` is the default and runs the command on the node that saw the file
change. `mode: queue` submits it as a normal job, which is what you want when
the machine noticing the file isn't the machine that should do the work.

Missing watch paths are logged and skipped, not fatal — a disconnected external
drive won't stop the agent from starting.

## 7. Failure handling

- If an agent dies mid-job, the coordinator marks that job `lost` after 2
  minutes without a check-in, instead of leaving it `running` forever.
- If the coordinator is unreachable, agents back off exponentially up to 2
  minutes and reconnect on their own.
- If a job's completion report fails to send, the agent retries three times
  before giving up.
- **Cancel** on a running job kills the whole process group on the agent, so a
  cancelled `sleep 300` really stops rather than orphaning a child process.

## 8. Public code, private instance

The code is public; your instance data is not. `coordinator/config.yaml`, every
`agent/config.yaml`, and `coordinator/loom.db` are gitignored — they hold your
password hash, your device API keys, and your job history.

If you want those backed up off-device, put them in a separate **private** repo
or an encrypted note. Never in this one.

## 9. Security notes

- Job commands run with whatever permissions the agent process has. Give the
  agent a dedicated user account scoped to what your jobs actually need, and
  don't run it as root or Administrator.
- The dashboard password is hashed with Werkzeug's PBKDF2; the plaintext is
  never stored.
- Login is throttled to 5 attempts per IP per 5 minutes. The lockout is in
  memory, so restarting the service clears it — that's your recovery path if
  you lock yourself out.
- Session cookies are `HttpOnly` + `SameSite=Lax`. Set `secure_cookie: true` in
  `config.yaml` once you're on HTTPS.
- State-changing dashboard requests require a CSRF token.
- Set `debug: false` in production. Flask's debugger is a remote code execution
  console for anyone who can reach the port.
