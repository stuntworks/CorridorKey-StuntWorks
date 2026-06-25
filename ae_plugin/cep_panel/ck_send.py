#!/usr/bin/env python
# ck_send.py - thin client the CEP panel spawns INSTEAD of running the engine.
#
# The panel still does `python ck_send.py <engine args>` exactly like it used to
# do `python ck_launch.py <engine args>`. The difference: ck_send.py does NOT
# touch CUDA. It only opens a loopback socket to ck_broker.py (which runs
# outside AE's sandbox) and forwards the job. The broker runs the torch/CUDA
# engine in a clean process and streams stdout back; ck_send.py reprints those
# lines and exits with the engine's real exit code. So the panel's existing
# stdout-streaming + exit-code handling keeps working with zero logic change.
#
# Because ck_send.py never initializes CUDA, the CEF sandbox mitigations that
# crash the engine (0xC0000005) do not matter here.
#
# FALLBACK CHAIN (never worse than before):
#   1. connect to broker -> forward job.
#   2. refused? try to start the broker via its Scheduled Task, retry once.
#   3. still down? run the engine directly (today's behavior; will crash in the
#      sandbox, but that is no worse than not having a broker at all).

import os
import sys
import json
import time
import socket
import subprocess

# Engine emits non-ASCII (e.g. "3 -> 4 channels" with a real arrow). Default
# Windows console encoding (cp1252) would crash on re-print. Force UTF-8 so the
# panel (Node reads our stdout as UTF-8) gets clean bytes.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROCESSOR = os.path.join(HERE, "ae_processor.py")
CONFIG_PATH = os.path.join(HERE, "ck_broker_config.json")
TASK_NAME = r"CorridorKey\CUDABroker"


def _load_cfg():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _connect(cfg, timeout=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((cfg["host"], int(cfg["port"])))
    s.settimeout(None)
    return s


def _start_broker_task():
    """Ask Task Scheduler to start the broker. schtasks is a child of the panel
    (sandboxed) but it only SIGNALS the scheduler; the broker itself is launched
    by svchost in a clean session, which is the whole point."""
    try:
        subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _run_direct():
    """Last-resort fallback: run the engine in-process (today's behavior)."""
    cmd = [sys.executable, PROCESSOR] + sys.argv[1:]
    try:
        return subprocess.run(cmd, cwd=HERE).returncode
    except Exception as e:
        sys.stderr.write("ck_send: direct fallback failed: %s\n" % e)
        return 1


def _stream(sock):
    """Forward broker messages to our stdout/stderr; return engine exit code."""
    f = sock.makefile("r", encoding="utf-8")
    out = sys.stdout
    for line in f:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        t = msg.get("type")
        if t == "line":
            out.write(msg.get("data", "") + "\n")
            out.flush()
        elif t == "done":
            return int(msg.get("exit", 0))
        elif t == "error":
            sys.stderr.write("ck_send: broker error: %s\n" % msg.get("msg"))
            return 1
    # Socket closed with no 'done' -> treat as failure.
    return 1


def main():
    try:
        cfg = _load_cfg()
    except Exception as e:
        sys.stderr.write("ck_send: no broker config (%s); running direct\n" % e)
        return _run_direct()

    req = json.dumps({
        "auth": cfg.get("secret"),
        "op": "run",
        "root": os.environ.get("CORRIDORKEY_ROOT", ""),
        "cwd": HERE,
        "args": sys.argv[1:],
    }) + "\n"

    # Attempt 1: connect.
    sock = None
    try:
        sock = _connect(cfg)
    except Exception:
        # Attempt 2: start the broker task, wait briefly, retry.
        _start_broker_task()
        for _ in range(10):
            time.sleep(0.5)
            try:
                sock = _connect(cfg)
                break
            except Exception:
                continue

    if sock is None:
        sys.stderr.write("ck_send: broker unreachable; running direct (may crash in sandbox)\n")
        return _run_direct()

    try:
        sock.sendall(req.encode("utf-8"))
        return _stream(sock)
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
