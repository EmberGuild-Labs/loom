# Setting up the persistent node (Raspberry Pi)

This is the full walkthrough for standing up Loom's **persistent node** — the
always-on machine that runs the coordinator (dashboard + job queue) and also
runs an agent so it can execute work itself.

Everything below is written for a Raspberry Pi running Raspberry Pi OS
(Bookworm), user `pi`, with **nginx and cloudflared already installed** as part
of an existing homelab. Adjust paths if yours differ.

> **Working style:** run these **one step at a time** and check the verification
> output before moving on. If a step's output looks wrong, stop and fix it —
> every later step assumes the previous one worked.

---

## What you end up with

| Piece | Where | How it runs |
|---|---|---|
| Coordinator (Flask + SQLite) | `/home/pi/loom/coordinator` | `loom-coordinator` systemd service on `127.0.0.1:5055` |
| Agent (this node's worker) | `/home/pi/loom/agent` | `loom-agent` systemd service |
| Dashboard | `https://mesh.<your-domain>` | Cloudflare Tunnel → `localhost:5055` |

Two services, because the two jobs are genuinely separate: the coordinator
hands out work, the agent does work. Keeping them apart means you can restart
one without disturbing the other, and the coordinator can stay sandboxed while
the agent (which must run arbitrary commands) is not.

---

## STEP 0 — Verify prerequisites

```bash
python3 --version && which git nginx cloudflared && python3 -m venv --help > /dev/null && echo "venv ok"
```

**Expect:** Python 3.9 or newer, a path printed for each of `git`, `nginx`,
`cloudflared`, and `venv ok`.

If `python3-venv` or `pip` is missing:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv
```

---

## STEP 1 — Get the files onto the Pi

If you pushed Loom to GitHub (this is the easy path):

```bash
cd ~ && git clone <your-loom-repo-url> loom && cd loom && ls
```

**Expect:** `agent  coordinator  docs  LICENSE  README.md  scripts`

> **Private repo?** The Pi has no GitHub credentials by default, so a plain
> `git clone` of a private repo will fail with an auth prompt. Either use a
> token in the URL (`https://<token>@github.com/...`), or create the files
> with heredocs (`cat > file << 'EOF'`) instead.

---

## STEP 2 — Install the coordinator

```bash
cd ~/loom/coordinator
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

**Verify:**

```bash
./venv/bin/python -c "import flask, yaml, gunicorn; print('coordinator deps ok')"
```

**Expect:** `coordinator deps ok`

> On a 1GB Pi this takes a couple of minutes and is mostly silent. That's
> normal — it's compiling nothing, just downloading wheels.

---

## STEP 3 — Generate your dashboard secret and password

```bash
cd ~/loom/scripts
../coordinator/venv/bin/python gen_api_key.py --dashboard-secret
```

It asks you to choose a dashboard password (minimum 10 characters), then prints
two lines. **Keep this terminal open** — you're pasting those values in the next
step.

The `secret_key` signs your login cookie; the `password_hash` is a one-way hash
of your password, so the real password is never written to disk anywhere.

---

## STEP 4 — Write the coordinator config

```bash
cd ~/loom/coordinator
cp config.example.yaml config.yaml
nano config.yaml
```

Replace the two `REPLACE_ME...` lines with the values printed in STEP 3, and set
`secure_cookie: true` (you'll be serving over HTTPS through the tunnel).

Leave `host: "127.0.0.1"` alone — the coordinator should only be reachable
through the tunnel, never directly from your LAN or the internet.

Save with `Ctrl+O`, `Enter`, then `Ctrl+X`.

**Verify the config parses and the app boots:**

```bash
./venv/bin/python -c "import app; print('config ok')"
```

**Expect:** a `loom coordinator ready (db=...)` log line, then `config ok`.
If you see a `[loom] config.yaml is missing...` or `still has the example
placeholder` message, fix `config.yaml` and re-run.

---

## STEP 5 — Register your devices

Still on the Pi. This writes directly into the coordinator's SQLite database.

Register the Pi itself as the **persistent** node:

```bash
cd ~/loom/scripts
../coordinator/venv/bin/python gen_api_key.py --add-device \
  --name pi-node --platform linux --persistent --tags always-on
```

Then each other machine you want in the mesh:

```bash
../coordinator/venv/bin/python gen_api_key.py --add-device \
  --name macbook --platform macos --tags desktop,dev

../coordinator/venv/bin/python gen_api_key.py --add-device \
  --name windows-desktop --platform windows --tags gpu,desktop
```

Each command prints an `api_key`. **Copy each one somewhere safe** — it goes
into that specific machine's `agent/config.yaml`.

**Verify:**

```bash
../coordinator/venv/bin/python gen_api_key.py --list-devices
```

**Expect:** your devices listed, with `yes` in the PERSIST column for exactly
one of them and `never` under LAST SEEN.

> **Why only one persistent node?** A job submitted with no tag and no target
> device has to land *somewhere*. The persistent node is that somewhere. Two
> persistent nodes would make it a coin flip which one runs it, so the CLI
> refuses to create a second.
>
> Forgot a key later? `--list-devices --show-keys` prints them, and
> `--rotate-key --name <name>` issues a fresh one and invalidates the old.

---

## STEP 6 — Install the coordinator as a service

```bash
sudo cp ~/loom/coordinator/loom-coordinator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now loom-coordinator
sudo systemctl status loom-coordinator --no-pager
```

**Expect:** `active (running)` in green.

If it failed:

```bash
sudo journalctl -u loom-coordinator -n 30 --no-pager
```

The usual cause is a path mismatch — the unit file expects everything under
`/home/pi/loom/coordinator`. Edit the `WorkingDirectory` and `ExecStart` lines
in `/etc/systemd/system/loom-coordinator.service` if you installed elsewhere,
then `sudo systemctl daemon-reload && sudo systemctl restart loom-coordinator`.

**Verify it's actually serving:**

```bash
curl -s http://127.0.0.1:5055/healthz
```

**Expect:** `{"agent_api_version":2,"ok":true}`

---

## STEP 7 — Expose the dashboard through your Cloudflare Tunnel

Look at your existing tunnel config:

```bash
sudo cat /etc/cloudflared/config.yml
```

Add an ingress entry for your dashboard hostname **above** the final
`http_status:404` line — that catch-all must always stay last:

```yaml
ingress:
  - hostname: mesh.your-domain.com
    service: http://localhost:5055
  # ... your existing services ...
  - service: http_status:404
```

Edit it with:

```bash
sudo nano /etc/cloudflared/config.yml
```

Then validate **before** restarting — a malformed config will take down every
other site on this tunnel, not just Loom:

```bash
cloudflared tunnel ingress validate
```

**Expect:** `Validating rules against ...` and `OK`.

Now restart and add the DNS record:

```bash
sudo systemctl restart cloudflared
sudo systemctl status cloudflared --no-pager
```

In the Cloudflare dashboard, add a **CNAME** for `mesh` pointing at
`<your-tunnel-id>.cfargotunnel.com`, proxied (orange cloud) — same pattern as
your other tunnelled services.

**Verify from any machine:**

```bash
curl -s https://mesh.your-domain.com/healthz
```

**Expect:** the same `{"agent_api_version":2,"ok":true}`.

> `/healthz` is the only route that works without logging in, and it
> deliberately reveals nothing — no node names, no counts, no stats.

---

## STEP 8 — Install the agent on the Pi

The Pi runs an agent too, so it can execute jobs and not just hand them out.

```bash
cd ~/loom/agent
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
nano config.yaml
```

Set:

```yaml
coordinator_url: "http://127.0.0.1:5055"   # local — no need to leave the box
api_key: "<the pi-node key from STEP 5>"
device_name: "pi-node"
max_concurrent_jobs: 1                      # a 1GB Pi should not run two at once
```

> The Pi's agent talks to `127.0.0.1` rather than out through the tunnel and
> back. It's faster, and it keeps working even if Cloudflare is having a bad
> day. Every *other* node must use the public HTTPS URL.

**Test it in the foreground first:**

```bash
./venv/bin/python loom_agent.py
```

**Expect:** `loom-agent 1.1.0 starting as 'pi-node' (linux) -> http://127.0.0.1:5055`
and then silence (silence is good — it means heartbeats are succeeding).

Press `Ctrl+C` once you've confirmed, then install it as a service:

```bash
sudo cp ~/loom/agent/service/loom-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now loom-agent
sudo systemctl status loom-agent --no-pager
```

**Expect:** `active (running)`.

---

## STEP 9 — First login and a real test job

Open `https://mesh.your-domain.com` and sign in with the password from STEP 3.

You should see `pi-node` with a green dot, a `persistent` tag, and live
CPU/RAM/disk bars.

Submit a test job — leave the target as **Any node**:

```
echo "hello from $(hostname) at $(date)"
```

Watch it go `pending → assigned → running → done` (the dashboard refreshes every
4 seconds). Click **Log** on the finished row to see the output.

If that works, the mesh is live.

---

## Troubleshooting

**`sudo systemctl status loom-coordinator` shows `active (running)` but the site 502s**
The tunnel is pointing at the wrong port. `curl http://127.0.0.1:5055/healthz`
on the Pi to confirm the coordinator itself is fine, then re-check the
`service:` line in `/etc/cloudflared/config.yml`.

**Node never turns green**
Check the agent's own view first: `sudo journalctl -u loom-agent -n 30
--no-pager`. `heartbeat failed (...)` means it can't reach the coordinator —
verify `coordinator_url` in `agent/config.yaml`. A `403` means the `api_key`
doesn't match any registered device; re-check it against `--list-devices
--show-keys`.

**Locked out of the dashboard after mistyping the password**
Five failures from one IP triggers a 5-minute lockout, and it blocks the
*correct* password too. Either wait it out, or clear it instantly with
`sudo systemctl restart loom-coordinator` (the counter is in memory only).

**Jobs sit at `pending` forever**
An untargeted job only runs on the persistent node. Confirm one exists and is
online: `gen_api_key.py --list-devices`. A job tagged `gpu` when no node
carries that tag will also wait indefinitely — that's intended, not a bug.

**Job went to `lost`**
The agent stopped checking in for 2 minutes while the job was assigned or
running — usually the machine slept, rebooted, or lost network. The job is not
retried automatically; re-submit it.

**`externally-managed-environment` from pip**
You're outside the venv. Use the explicit `./venv/bin/pip` form shown
throughout this guide rather than a bare `pip`.

**Coordinator won't start after an edit to config.yaml**
`sudo journalctl -u loom-coordinator -n 20 --no-pager`. YAML is
whitespace-sensitive — a stray tab or a missing space after a colon will fail
the parse. The config validator prints exactly which key is wrong.

---

## Day-to-day operations

```bash
# Watch what the mesh is doing, live
sudo journalctl -u loom-coordinator -f
sudo journalctl -u loom-agent -f

# Restart after a config change
sudo systemctl restart loom-coordinator
sudo systemctl restart loom-agent

# Update to a newer version of Loom
cd ~/loom && git pull
./coordinator/venv/bin/pip install -r coordinator/requirements.txt
./agent/venv/bin/pip install -r agent/requirements.txt
sudo systemctl restart loom-coordinator loom-agent

# Back up the database (device keys + job history)
cp ~/loom/coordinator/loom.db ~/loom-db-backup-$(date +%F).db
```

`config.yaml` and `loom.db` are gitignored, so `git pull` will never overwrite
your secrets or your device registry.
