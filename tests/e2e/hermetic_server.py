"""Start the real dashboard server for browser tests, with IB pointed at a closed port.

The server comes up with ``connected: false`` and no background loops, so a test can drive the
page by injecting messages into ``handleMessage()`` without any IB traffic.
"""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def hermetic_server(deadline_s: float = 60.0):
    port = _free_port()
    env = dict(os.environ, SERVER_PORT=str(port), SERVER_HOST="127.0.0.1")
    # Strip Discord secrets (an ambient token adds a ~15s login to boot) and point the child at
    # an empty .env, since config.load_dotenv() would otherwise read the repo .env back in.
    for k in list(env):
        if k.startswith("DISCORD_"):
            env.pop(k, None)
    fd, dotenv = tempfile.mkstemp(suffix=".env")
    os.close(fd)
    env["DOTENV_PATH"] = dotenv
    env["IB_HOST"] = "127.0.0.1"
    env["IB_PORT"] = str(_free_port())          # nothing listens there: connect fails fast
    # cwd=ROOT so ``-m`` imports this checkout's package (a worktree), not the editable install.
    proc = subprocess.Popen([sys.executable, "-m", "spx_trade_desk.server"], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        end = time.time() + deadline_s
        while time.time() < end:
            if proc.poll() is not None:
                raise RuntimeError(f"hermetic server exited early (code {proc.returncode})")
            try:
                urllib.request.urlopen(f"{url}/api/state", timeout=1)
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("hermetic server did not start in time")
        yield url
    finally:
        try:
            proc.send_signal(signal.SIGINT)
        except (ValueError, OSError):
            proc.terminate()                     # Windows: no SIGINT through Popen
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if os.path.exists(dotenv):
            os.remove(dotenv)
