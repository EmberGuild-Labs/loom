# Running the Loom agent as a Windows service

Windows has no built-in equivalent of systemd/launchd, so the simplest
reliable option is [NSSM](https://nssm.cc/) (Non-Sucking Service Manager),
which wraps any executable as a proper background service with auto-restart.

## Steps

1. Install Python 3.11+ from python.org (check "Add to PATH" during install).
2. Open PowerShell in the `agent/` folder and set up a virtual environment:
   ```powershell
   python -m venv venv
   venv\Scripts\pip install -r requirements.txt
   ```
3. Copy `config.example.yaml` to `config.yaml` and fill in your
   `coordinator_url`, `api_key`, and `device_name`.
4. Download NSSM from https://nssm.cc/download, extract it, and place
   `nssm.exe` somewhere on your PATH (e.g. `C:\Windows\System32`).
5. Install the service:
   ```powershell
   nssm install LoomAgent "C:\path\to\loom\agent\venv\Scripts\python.exe" "C:\path\to\loom\agent\loom_agent.py"
   nssm set LoomAgent AppDirectory "C:\path\to\loom\agent"
   nssm set LoomAgent Start SERVICE_AUTO_START
   ```
6. Start it:
   ```powershell
   nssm start LoomAgent
   ```
7. Check status / logs:
   ```powershell
   nssm status LoomAgent
   ```
   Configure stdout/stderr log paths in `nssm edit LoomAgent` if you want
   persistent logs (recommended — set them to a file under the loom folder).

## Removing the service later

```powershell
nssm stop LoomAgent
nssm remove LoomAgent confirm
```

## Notes for this Windows node specifically

- Since this can't be tested locally right now, sanity-check after first
  install: `Get-Service LoomAgent` should show `Running`, and the node
  should appear online on the dashboard within ~10 seconds (one heartbeat
  interval).
- `psutil` and the rest of the agent's dependencies are pure cross-platform
  and require no Windows-specific code changes — the same `loom_agent.py`
  runs unmodified on macOS, Linux, and Windows.
- If Windows Defender flags `nssm.exe`, that's a known false positive for
  the tool; it's widely used and open source.
