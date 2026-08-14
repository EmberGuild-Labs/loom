"""
Loom Agent
----------
Runs on every node in the mesh (macOS, Linux/Raspberry Pi, Windows).

Responsibilities:
  - Sends a heartbeat with CPU/RAM/disk stats every HEARTBEAT_INTERVAL seconds
  - Polls the coordinator for assigned jobs and executes them as subprocesses
  - Optionally watches local directories and reacts to file changes
  - Optionally runs cron-style scheduled jobs defined in config.yaml

Cross-platform notes:
  - psutil reports CPU/RAM identically on all three OSes; the disk root differs
    (see DISK_ROOT below)
  - subprocess with shell=True works everywhere, but the *commands* are yours
    to make platform-appropriate
  - File watching uses `watchdog`, which supports all three

Run with: python loom_agent.py  (reads config.yaml next to this file)
"""

import os
import sys
import time
import signal
import socket
import logging
import platform
import subprocess
import threading
from urllib.parse import urlparse

import psutil
import requests
import yaml

try:
    import schedule
except ImportError:
    schedule = None

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    Observer = None
    FileSystemEventHandler = object


AGENT_VERSION = "1.1.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("LOOM_AGENT_CONFIG", os.path.join(BASE_DIR, "config.yaml"))

HEARTBEAT_INTERVAL = 10
JOB_POLL_INTERVAL = 5

# Backoff bounds for when the coordinator is unreachable. Without this the
# agent retried every 5s forever and filled the system log with identical
# stack traces while the Pi was rebooting.
BACKOFF_MAX = 120

# psutil.disk_usage("/") raises on Windows, where there is no "/" -- report the
# drive the agent itself is installed on instead.
DISK_ROOT = os.path.abspath(os.sep) if os.name != "nt" else os.path.splitdrive(BASE_DIR)[0] + os.sep

log = logging.getLogger("loom.agent")

_shutdown = threading.Event()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        raise SystemExit(
            f"[loom-agent] Missing {path}\n"
            f"             cp config.example.yaml config.yaml, then fill in\n"
            f"             coordinator_url, api_key and device_name."
        )
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    missing = [k for k in ("coordinator_url", "api_key", "device_name") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"[loom-agent] config.yaml is missing: {', '.join(missing)}")
    if str(cfg["api_key"]).startswith("REPLACE_ME"):
        raise SystemExit(
            "[loom-agent] config.yaml still has the placeholder api_key.\n"
            "             Register this device on the coordinator:\n"
            "               python scripts/gen_api_key.py --add-device --name <name> --platform <os>"
        )

    parsed = urlparse(cfg["coordinator_url"])
    if parsed.scheme not in ("http", "https"):
        raise SystemExit("[loom-agent] coordinator_url must start with http:// or https://")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        # The API key travels in a header on every request; over plain HTTP to a
        # remote host it is readable by anything on the path.
        log.warning(
            "coordinator_url uses plain HTTP to a remote host -- your API key is "
            "sent unencrypted. Use HTTPS (e.g. via your Cloudflare Tunnel)."
        )
    return cfg


CONFIG = load_config()
COORDINATOR_URL = CONFIG["coordinator_url"].rstrip("/")
API_KEY = CONFIG["api_key"]
DEVICE_NAME = CONFIG["device_name"]
JOB_TIMEOUT = int(CONFIG.get("job_timeout", 3600))

# Job matching on the coordinator is first-match, not load-aware, so the agent
# enforces its own ceiling: it simply stops polling while it is already at
# capacity, and the work stays queued for whichever node frees up first.
MAX_CONCURRENT_JOBS = int(CONFIG.get("max_concurrent_jobs", 2))

HEADERS = {
    "X-API-Key": API_KEY,
    "User-Agent": f"loom-agent/{AGENT_VERSION}",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# job_id -> Popen, so a dashboard cancel can actually kill the process
_running = {}
_running_lock = threading.Lock()


def detect_platform():
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


# ---------------------------------------------------------------------------
# Stats + heartbeat
# ---------------------------------------------------------------------------

def collect_stats():
    vm = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage(DISK_ROOT)
        disk_percent = disk.percent
    except OSError as e:
        log.debug("disk usage unavailable for %s: %s", DISK_ROOT, e)
        disk_percent = None

    with _running_lock:
        running_ids = sorted(_running.keys())

    return {
        "hostname": socket.gethostname(),
        "platform": detect_platform(),
        "agent_version": AGENT_VERSION,
        # interval=None returns the load since the previous call rather than
        # blocking for a second. main() primes it once at startup so the first
        # real heartbeat is already meaningful.
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": vm.percent,
        # Deliberately (total - available), not psutil's vm.used.
        #
        # psutil derives .percent from (total - available)/total, but computes
        # .used differently per platform -- on macOS it is active + wired,
        # which excludes inactive, compressed and cached pages. Reporting both
        # verbatim made the dashboard show "70% RAM" next to "6.3/16.0 GB",
        # which is 39%. Both were right by their own definition and the pair
        # was nonsense. Deriving used from available keeps the bar and the
        # numbers consistent on every platform.
        "ram_used_gb": round((vm.total - vm.available) / (1024 ** 3), 2),
        "ram_total_gb": round(vm.total / (1024 ** 3), 2),
        "disk_percent": disk_percent,
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "running_jobs": running_ids,
    }


def heartbeat_loop():
    backoff = HEARTBEAT_INTERVAL
    warned = False
    while not _shutdown.is_set():
        try:
            res = SESSION.post(
                f"{COORDINATOR_URL}/api/heartbeat", json=collect_stats(), timeout=15
            )
            res.raise_for_status()
            if warned:
                log.info("coordinator reachable again")
                warned = False
            backoff = HEARTBEAT_INTERVAL

            for job_id in (res.json().get("cancel") or []):
                cancel_job(job_id)
        except Exception as e:
            if not warned:
                log.warning("heartbeat failed (%s); backing off", e)
                warned = True
            backoff = min(backoff * 2, BACKOFF_MAX)
        _shutdown.wait(backoff)


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

def cancel_job(job_id):
    with _running_lock:
        proc = _running.get(job_id)
    if not proc:
        return
    log.info("cancelling job %s on coordinator request", job_id)
    try:
        # Kill the whole process group -- shell=True means the direct child is
        # a shell, and terminating only that would orphan the real work.
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError) as e:
        log.debug("could not signal job %s: %s", job_id, e)


def run_job(job):
    job_id = job["id"]
    command = job["command"]
    log.info("running job %s: %s", job_id, command)

    try:
        SESSION.post(f"{COORDINATOR_URL}/api/jobs/{job_id}/running", timeout=15)
    except Exception as e:
        log.debug("could not mark job %s running: %s", job_id, e)

    popen_kwargs = {
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "errors": "replace",
        "cwd": CONFIG.get("job_working_dir") or BASE_DIR,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    exit_code, output = None, ""
    try:
        proc = subprocess.Popen(command, **popen_kwargs)
        with _running_lock:
            _running[job_id] = proc
        try:
            output = proc.communicate(timeout=JOB_TIMEOUT)[0] or ""
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            log.warning("job %s exceeded %ss, killing", job_id, JOB_TIMEOUT)
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
            output = (proc.communicate()[0] or "") + f"\n[loom] killed after {JOB_TIMEOUT}s timeout"
            exit_code = -1
        payload = {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "result": f"exit code {exit_code}",
            "log": output[-16000:],
        }
    except Exception as e:
        log.error("job %s could not be started: %s", job_id, e)
        payload = {"success": False, "exit_code": None, "result": str(e)[:500], "log": str(e)}
    finally:
        with _running_lock:
            _running.pop(job_id, None)

    # Report completion with a few retries -- losing this leaves the job to be
    # reaped as 'lost' on the coordinator even though it actually succeeded.
    for attempt in range(3):
        try:
            SESSION.post(
                f"{COORDINATOR_URL}/api/jobs/{job_id}/complete", json=payload, timeout=20
            )
            return
        except Exception as e:
            log.warning("reporting job %s failed (attempt %d): %s", job_id, attempt + 1, e)
            _shutdown.wait(2 ** attempt)
    log.error("gave up reporting completion of job %s", job_id)


def poll_loop():
    backoff = JOB_POLL_INTERVAL
    while not _shutdown.is_set():
        with _running_lock:
            at_capacity = len(_running) >= MAX_CONCURRENT_JOBS
        if at_capacity:
            _shutdown.wait(JOB_POLL_INTERVAL)
            continue
        try:
            res = SESSION.get(f"{COORDINATOR_URL}/api/jobs/poll", timeout=15)
            res.raise_for_status()
            backoff = JOB_POLL_INTERVAL
            job = res.json().get("job")
            if job:
                threading.Thread(
                    target=run_job, args=(job,), daemon=True, name=f"loom-job-{job['id']}"
                ).start()
                continue  # check again immediately in case more work is queued
        except Exception as e:
            log.debug("job poll failed: %s", e)
            backoff = min(backoff * 2, BACKOFF_MAX)
        _shutdown.wait(backoff)


def queue_job(command, target_tag=None):
    """Queue work through the coordinator so any eligible node can pick it up."""
    try:
        SESSION.post(
            f"{COORDINATOR_URL}/api/jobs/submit",
            json={"command": command, "target_tag": target_tag},
            timeout=15,
        ).raise_for_status()
        log.info("queued triggered job: %s", command)
    except Exception as e:
        log.error("could not queue triggered job: %s", e)


# ---------------------------------------------------------------------------
# File-watch triggers
# ---------------------------------------------------------------------------

class TriggerHandler(FileSystemEventHandler):
    """Runs a command when files under a watched path change.

    `mode: local` (default) runs it here immediately -- the trigger already
    happened on this machine. `mode: queue` submits it to the coordinator so it
    can land on whichever node matches the tag.
    """

    def __init__(self, spec):
        self.command_template = spec["run"]
        self.mode = spec.get("mode", "local")
        self.target_tag = spec.get("target_tag")
        self.debounce = float(spec.get("debounce", 1.0))
        self._last_fired = {}
        self._lock = threading.Lock()

    def on_any_event(self, event):
        if event.is_directory:
            return
        path = event.src_path

        # A single file save emits several events (created, modified, closed).
        # Without debouncing, one save fires the command three or four times.
        with self._lock:
            now = time.monotonic()
            if now - self._last_fired.get(path, 0) < self.debounce:
                return
            self._last_fired[path] = now

        command = self.command_template.replace("{path}", path)
        log.info("file trigger fired for %s", path)
        if self.mode == "queue":
            queue_job(command, self.target_tag)
        else:
            threading.Thread(
                target=_run_detached, args=(command,), daemon=True
            ).start()


def _run_detached(command):
    """Fire-and-forget local execution, off the watchdog thread so a slow
    command does not stall every other file event."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            errors="replace", timeout=JOB_TIMEOUT,
        )
        if result.returncode != 0:
            log.warning(
                "local trigger exited %s: %s",
                result.returncode, (result.stderr or "")[-500:],
            )
    except Exception as e:
        log.error("local trigger failed: %s", e)


def start_file_watchers():
    watch_config = CONFIG.get("watch") or []
    if not watch_config:
        return None
    if Observer is None:
        log.warning("watchdog is not installed -- file triggers disabled")
        return None

    observer = Observer()
    started = 0
    for w in watch_config:
        path = os.path.expanduser(w["path"])
        if not os.path.isdir(path):
            # Previously this raised and took the whole agent down at startup.
            log.error("watch path does not exist, skipping: %s", path)
            continue
        observer.schedule(TriggerHandler(w), path, recursive=w.get("recursive", False))
        log.info("watching %s -> %s (%s)", path, w["run"], w.get("mode", "local"))
        started += 1

    if not started:
        return None
    observer.start()
    return observer


# ---------------------------------------------------------------------------
# Cron-style scheduled jobs
# ---------------------------------------------------------------------------

def start_scheduler():
    cron_jobs = CONFIG.get("cron") or []
    if not cron_jobs:
        return
    if schedule is None:
        log.warning("`schedule` is not installed -- cron jobs disabled")
        return

    for job in cron_jobs:
        command = job["run"]
        # Run each scheduled command on its own thread. schedule runs jobs
        # inline, so a long-running one would previously block and delay every
        # other scheduled job behind it.
        schedule.every().day.at(job["at"]).do(
            lambda cmd=command: threading.Thread(
                target=_run_detached, args=(cmd,), daemon=True
            ).start()
        )
        log.info("scheduled daily at %s -> %s", job["at"], command)

    def loop():
        while not _shutdown.is_set():
            try:
                schedule.run_pending()
            except Exception as e:
                log.error("scheduler tick failed: %s", e)
            _shutdown.wait(20)

    threading.Thread(target=loop, daemon=True, name="loom-cron").start()


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def _handle_signal(signum, frame):
    log.info("received signal %s, shutting down", signum)
    _shutdown.set()


def main():
    logging.basicConfig(
        level=os.environ.get("LOOM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info(
        "loom-agent %s starting as '%s' (%s) -> %s",
        AGENT_VERSION, DEVICE_NAME, detect_platform(), COORDINATOR_URL,
    )

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle_signal)

    # Prime psutil's CPU counter so the first heartbeat reports a real delta
    # rather than the meaningless 0.0 of a first non-blocking call.
    psutil.cpu_percent(interval=None)

    observer = start_file_watchers()
    start_scheduler()
    threading.Thread(target=poll_loop, daemon=True, name="loom-poll").start()

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop, daemon=True, name="loom-heartbeat"
    )
    heartbeat_thread.start()

    try:
        while not _shutdown.is_set():
            _shutdown.wait(1)
    except KeyboardInterrupt:
        _shutdown.set()

    log.info("stopping")
    if observer:
        observer.stop()
        observer.join(timeout=5)

    # Give jobs still in flight a moment to report their result before exit.
    with _running_lock:
        outstanding = list(_running.keys())
    if outstanding:
        log.info("waiting for %d in-flight job(s): %s", len(outstanding), outstanding)
        deadline = time.time() + 10
        while time.time() < deadline:
            with _running_lock:
                if not _running:
                    break
            time.sleep(0.5)


if __name__ == "__main__":
    main()
