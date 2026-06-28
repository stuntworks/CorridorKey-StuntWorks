#!/usr/bin/env python
# ck_broker.py - CorridorKey CUDA broker.
#
# WHY THIS EXISTS:
#   The CK engine (ae_processor.py: torch + CUDA) crashes with access violation
#   0xC0000005 at torch.cuda init ONLY when After Effects' CEP panel spawns it,
#   because CEF/Chromium child processes inherit STICKY process mitigations
#   (ACG / Win32k disable) + a restricted token that CUDA's cuInit cannot use.
#   A child CANNOT turn those off (proven: ck_launch.py breakaway still crashed).
#   The only escape is a parent that was NEVER sandboxed.
#
#   This broker is started OUTSIDE AE by a logon Scheduled Task (parent =
#   Task Scheduler / svchost), so it runs in the user's interactive session
#   with a full clean token. The CEP panel sends it jobs over a loopback
#   socket; the broker runs the engine with a normal subprocess (clean) and
#   streams stdout lines + the exit code back. PROVEN end-to-end on 2026-06-16:
#   a Task-Scheduler-launched 'single' CUDA run exits 0 where the same command
#   inside AE crashes 0xC0000005.
#
# PROTOCOL (newline-delimited JSON over 127.0.0.1):
#   request : {"auth": "<secret>", "op": "run"|"ping"|"shutdown",
#              "args": [<engine args>], "cwd": "<dir>", "root": "<CK root>"}\n
#   reply   : one JSON object per line, then the socket closes:
#              {"type":"line","data":"<one stdout line>"}\n   (zero or more)
#              {"type":"done","exit":<int>}\n                  (always last on run)
#              {"type":"pong","version":"<v>","busy":<bool>}\n (ping)
#              {"type":"error","msg":"<why>"}\n                (auth / bad request)
#
# SECURITY:
#   - pre-shared secret (config file, written by installer / first run)
#   - engine arg[0] must be an allow-listed subcommand
#   - only ever runs <venv python> ae_processor.py; never arbitrary commands
#   - environment is the broker's own clean env (NOT the panel's); only the
#     CK root is overlaid from the request.

import os
import sys
import json
import queue
import socket
import struct  # noqa: F401  (reserved; protocol is line-based)
import threading
import subprocess
import time

VERSION = "1.0.0"

# Per-job timeout (seconds) for warm-worker GPU jobs (CNN-only path).
# Batch commands legitimately run many frames — 600 s (10 min) is intentionally
# generous so a real long batch is never killed.  extract never hits this path.
_GPU_JOB_TIMEOUT = 600

# SAM2 subprocess timeouts (fresh process per call).
# sam-apply is single-frame inference; batch/batch-scrub may span many frames.
_SAM_APPLY_TIMEOUT = 120    # seconds
_SAM_BATCH_TIMEOUT = 600    # seconds

# SAM2 commands that MUST run as a fresh subprocess (never via warm worker).
# SAM2 leaves non-daemon threads (AsyncVideoFrameLoader) and Hydra atexit state
# alive in a long-lived process, so a warm worker can never exit cleanly after
# a SAM2 job.  A one-shot subprocess sidesteps this entirely.
_SAM_CMDS = {"sam-apply", "batch", "batch-scrub"}

# CNN-only commands that use the warm worker (never load SAM2).
_CNN_CMDS = {"cache", "single", "postproc"}

HERE = os.path.dirname(os.path.abspath(__file__))
PROCESSOR = os.path.join(HERE, "ae_processor.py")
CONFIG_PATH = os.path.join(HERE, "ck_broker_config.json")
LOG_PATH = os.path.join(os.environ.get("TEMP", HERE), "corridorkey_broker.log")

# Engine subcommands the broker is allowed to launch (matches ae_processor.py).
ALLOWED_CMDS = {
    "extract", "single", "batch", "batch-scrub",
    "cache", "postproc", "sam-apply",
}

# One CUDA job at a time (single GPU). Serializes 'run' ops.
_run_lock = threading.Lock()


def _log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg.rstrip("\n") + "\n")
    except Exception:
        pass  # logging must never break the broker


def _load_config():
    """Read host/port/secret. Create with a fresh secret if missing."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        # urandom is fine here; this is a localhost-only shared secret.
        secret = os.urandom(24).hex()
        cfg = {"host": "127.0.0.1", "port": 37429, "secret": secret}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        _log("config created at %s" % CONFIG_PATH)
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 37429)
    return cfg


def _venv_python():
    """Resolve the CK venv python (same interpreter the engine needs)."""
    # broker lives in <root>/ae_plugin/cep_panel/ ; venv at <root>/.venv
    root = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
    cand = os.path.join(root, ".venv", "Scripts", "python.exe")
    if os.path.exists(cand):
        return cand
    # Fall back to whatever interpreter is running the broker.
    return sys.executable


def _send(conn, obj):
    conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _clean_env(root):
    """Broker's own env is already clean (launched outside AE). Overlay the
    CK root if the request supplied one; never trust panel-supplied env."""
    env = dict(os.environ)
    if root:
        env["CORRIDORKEY_ROOT"] = root
    return env


# ── Warm worker (persistent engine; loads the model once) ──────
CK_ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
WORKER_SCRIPT = os.path.join(HERE, "ck_engine_worker.py")
DONE_SENTINEL = "__CK_JOB_DONE__"

_worker = None          # subprocess.Popen of ck_engine_worker.py
_job_seq = 0            # monotonic job id


def _worker_alive():
    return _worker is not None and _worker.poll() is None


def _drain_worker_stderr(proc):
    """Worker stderr is diagnostics only - keep it out of the job stream and in
    the broker log, and stop the pipe from filling up and blocking the worker."""
    try:
        for line in proc.stderr:
            _log("worker-err: " + line.rstrip("\n"))
    except Exception:
        pass


def _spawn_worker():
    """Start the persistent engine worker. Returns the Popen or None."""
    global _worker
    env = _clean_env(CK_ROOT)
    # Eager mode for the interactive warm worker: skip torch.compile so the
    # first frame is not stalled by a multi-minute max-autotune compile (and
    # avoids recompile-on-respawn + cudagraph wedges). Same math => identical
    # output. DaVinci runs the engine directly (no broker) and still compiles.
    env["CORRIDORKEY_SKIP_COMPILE"] = "1"
    try:
        _worker = subprocess.Popen(
            [_venv_python(), WORKER_SCRIPT],
            cwd=HERE, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            bufsize=1, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        _log("worker spawn failed: %s" % e)
        _worker = None
        return None
    threading.Thread(target=_drain_worker_stderr, args=(_worker,), daemon=True).start()
    _log("worker spawned pid=%s" % _worker.pid)
    return _worker


def _run_via_worker(conn, args):
    """Send the job to the warm worker, stream its stdout to the panel.
    Returns: 'ok' (completed), 'no_worker' (never started -> caller may fall
    back), or 'died' (streamed then crashed/timed-out -> error already sent,
    no fallback).

    TIMEOUT SAFETY: a reader thread drains worker stdout into a queue so the
    main thread can apply a bounded wait (_GPU_JOB_TIMEOUT).  If no DONE
    sentinel arrives within the deadline the wedged worker is killed, _worker
    is cleared (next job respawns a fresh one), an error is sent to the panel,
    and the lock is released — the broker is never permanently jammed.
    """
    global _job_seq, _worker
    if not _worker_alive():
        if _spawn_worker() is None:
            return "no_worker"
    _job_seq += 1
    jid = _job_seq
    try:
        _worker.stdin.write(json.dumps({"id": jid, "argv": list(args)}) + "\n")
        _worker.stdin.flush()
    except Exception as e:
        _log("worker write failed (%s); will fall back" % e)
        try:
            _worker.kill()
        except Exception:
            pass
        _worker = None
        return "no_worker"

    # Reader thread: puts each stdout line (or None on EOF) into the queue.
    line_q = queue.Queue()
    captured_worker = _worker  # snapshot — _worker could be reassigned elsewhere

    def _reader():
        try:
            for ln in captured_worker.stdout:
                line_q.put(ln)
        except Exception:
            pass
        line_q.put(None)  # EOF sentinel

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    deadline = time.monotonic() + _GPU_JOB_TIMEOUT
    produced = False
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = line_q.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue  # check deadline again
            if line is None:
                # EOF — worker exited without sentinel
                break
            if line.startswith(DONE_SENTINEL):
                try:
                    info = json.loads(line[len(DONE_SENTINEL):].strip())
                except ValueError:
                    info = {"exit": 1}
                _send(conn, {"type": "done", "exit": int(info.get("exit", 0))})
                _log("done(worker): %s exit=%s" % (args[0], info.get("exit")))
                return "ok"
            produced = True
            try:
                _send(conn, {"type": "line", "data": line.rstrip("\n")})
            except (BrokenPipeError, ConnectionError):
                _log("client disconnected mid-run (worker keeps model warm)")
                # Do NOT kill the worker; let it finish for model-warm benefit.
                # Drain the queue in the background so the reader thread exits.
                threading.Thread(target=lambda: [line_q.get() for _ in iter(lambda: line_q.get(block=False) if not line_q.empty() else None, None)], daemon=True).start()
                return "ok"
    except Exception as e:
        _log("worker read loop error: %s" % e)

    # ── If we reach here the job did NOT complete cleanly ──
    if timed_out:
        _log("TIMEOUT: job '%s' exceeded %ds; killing wedged worker" % (args[0], _GPU_JOB_TIMEOUT))
        try:
            captured_worker.kill()
        except Exception:
            pass
        _worker = None
        _send(conn, {"type": "error",
                     "msg": "engine timed out after %ds on '%s'; please retry" % (_GPU_JOB_TIMEOUT, args[0])})
        return "died"

    # EOF without sentinel — worker crashed on this job.
    _log("worker died mid-job (exit=%s)" % (captured_worker.poll() if captured_worker else "?"))
    _worker = None
    _send(conn, {"type": "error", "msg": "engine worker crashed on this frame; retry"})
    return "died" if produced else "no_worker"


def _run_extract_direct(conn, args, cwd, env):
    """Run 'extract' as a standalone subprocess — NO _run_lock, NO warm worker.
    extract is a pure CPU/cv2 frame-grab (<1 s).  Bypassing the GPU lock means
    the source frame is available instantly even while a GPU key job is running.
    Own timeout: 60 s (bad file / hung decoder cannot wedge the broker).
    Streams lines + exactly one terminating done to the panel, matching the
    standard protocol."""
    cmd = [_venv_python(), PROCESSOR] + [str(a) for a in args]
    _log("extract(direct): %s" % " ".join(args))
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            bufsize=1, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        _send(conn, {"type": "error", "msg": "extract spawn failed: %s" % e})
        _log("extract spawn failed: %s" % e)
        return

    deadline = time.monotonic() + 60.0

    # Reader thread + queue so we can enforce the 60 s deadline.
    line_q = queue.Queue()

    def _reader():
        try:
            for ln in proc.stdout:
                line_q.put(ln)
        except Exception:
            pass
        line_q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = line_q.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                _send(conn, {"type": "line", "data": line.rstrip("\n")})
            except (BrokenPipeError, ConnectionError):
                proc.wait()
                _log("extract: client disconnected")
                return
    except Exception as e:
        _log("extract read loop error: %s" % e)

    if timed_out:
        _log("extract TIMEOUT (60s); killing")
        try:
            proc.kill()
        except Exception:
            pass
        _send(conn, {"type": "error", "msg": "extract timed out (60 s); bad file?"})
        _send(conn, {"type": "done", "exit": 1})
        return

    proc.wait()
    _send(conn, {"type": "done", "exit": int(proc.returncode)})
    _log("extract done exit=%s" % proc.returncode)


def _run_via_sam_subprocess(conn, args, cwd, env, timeout):
    """Run a SAM2 job as a fresh one-shot subprocess under _run_lock.

    SAM2 (facebookresearch/sam2) leaves non-daemon threads (AsyncVideoFrameLoader
    from init_state) and a Hydra config singleton/atexit state alive in a
    long-lived process, so a persistent warm worker can never exit cleanly after
    a SAM2 job.  Running each SAM2 call in its own subprocess gives it a clean
    process lifecycle and avoids poisoning the broker or the warm worker.

    Watchdog: identical reader-thread + queue + bounded deadline pattern used by
    _run_via_worker.  On timeout the subprocess is killed, an error message then
    a terminating done are sent so the panel never hangs, and _run_lock is
    released normally.

    Env additions vs the base clean env:
      CORRIDORKEY_SKIP_COMPILE=1  -- no torch.compile stall on every fresh spawn
      PYTORCH_CUDA_ALLOC_CONF     -- expandable segments prevents allocator OOM
                                     fragmentation across many short SAM runs on
                                     Windows (each process starts a new CUDA ctx)
    """
    sam_env = dict(env)
    sam_env["CORRIDORKEY_SKIP_COMPILE"] = "1"
    sam_env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

    cmd = [_venv_python(), PROCESSOR] + [str(a) for a in args]
    _log("sam-subprocess(%ds): %s" % (timeout, " ".join(str(a) for a in args)))
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=sam_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            bufsize=1, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        _send(conn, {"type": "error", "msg": "sam spawn failed: %s" % e})
        _send(conn, {"type": "done", "exit": 1})
        _log("sam spawn failed: %s" % e)
        return

    deadline = time.monotonic() + timeout
    line_q = queue.Queue()

    def _reader():
        try:
            for ln in proc.stdout:
                line_q.put(ln)
        except Exception:
            pass
        line_q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = line_q.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue
            if line is None:
                break
            # ponytail: progress = alive. Reset deadline on every line so timeout
            # is a stall watchdog, not a wall-clock cap. Clip length stops mattering;
            # a genuinely frozen engine (no output for `timeout`s) still gets killed.
            deadline = time.monotonic() + timeout
            try:
                _send(conn, {"type": "line", "data": line.rstrip("\n")})
            except (BrokenPipeError, ConnectionError):
                proc.wait()
                _log("sam-subprocess: client disconnected mid-run exit=%s" % proc.returncode)
                return
    except Exception as e:
        _log("sam-subprocess read loop error: %s" % e)

    if timed_out:
        _log("TIMEOUT: sam job '%s' exceeded %ds; killing subprocess" % (args[0], timeout))
        try:
            proc.kill()
        except Exception:
            pass
        _send(conn, {"type": "error",
                     "msg": "SAM engine timed out after %ds on '%s'; please retry" % (timeout, args[0])})
        _send(conn, {"type": "done", "exit": 1})
        return

    proc.wait()
    _send(conn, {"type": "done", "exit": int(proc.returncode)})
    _log("done(sam-subprocess): %s exit=%s" % (args[0], proc.returncode))


def _run_via_subprocess(conn, args, cwd, env, timeout=None):
    """Fallback: one fresh engine process per job (reloads the model).
    Used only if the warm worker cannot start for a CNN-only job.

    When timeout is None the function uses an unbounded read loop (original
    behaviour for the CNN fallback path).  When a timeout is passed it uses the
    reader-thread + queue + deadline pattern so the fallback can never wedge the
    broker either.
    """
    cmd = [_venv_python(), PROCESSOR] + [str(a) for a in args]
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            bufsize=1, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        _send(conn, {"type": "error", "msg": "spawn failed: %s" % e})
        _send(conn, {"type": "done", "exit": 1})
        _log("spawn failed: %s" % e)
        return

    if timeout is None:
        # Original unbounded path (CNN fallback — warm worker already confirmed dead).
        try:
            for line in proc.stdout:
                _send(conn, {"type": "line", "data": line.rstrip("\n")})
        except (BrokenPipeError, ConnectionError):
            proc.wait()
            _log("client disconnected mid-run; engine finished exit=%s" % proc.returncode)
            return
        proc.wait()
        _send(conn, {"type": "done", "exit": int(proc.returncode)})
        _log("done(subprocess): %s exit=%s" % (args[0], proc.returncode))
        return

    # Bounded path: reader-thread + queue + deadline (used when caller passes timeout).
    deadline = time.monotonic() + timeout
    line_q = queue.Queue()

    def _reader():
        try:
            for ln in proc.stdout:
                line_q.put(ln)
        except Exception:
            pass
        line_q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = line_q.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                _send(conn, {"type": "line", "data": line.rstrip("\n")})
            except (BrokenPipeError, ConnectionError):
                proc.wait()
                _log("subprocess: client disconnected mid-run exit=%s" % proc.returncode)
                return
    except Exception as e:
        _log("subprocess read loop error: %s" % e)

    if timed_out:
        _log("TIMEOUT: subprocess job '%s' exceeded %ds; killing" % (args[0], timeout))
        try:
            proc.kill()
        except Exception:
            pass
        _send(conn, {"type": "error",
                     "msg": "engine timed out after %ds on '%s'; please retry" % (timeout, args[0])})
        _send(conn, {"type": "done", "exit": 1})
        return

    proc.wait()
    _send(conn, {"type": "done", "exit": int(proc.returncode)})
    _log("done(subprocess-bounded): %s exit=%s" % (args[0], proc.returncode))


def _handle_run(conn, req):
    """Route the incoming run request to the correct execution path.

    Command -> path routing table:

      extract            -> _run_extract_direct   (no lock, CPU-only, instant)
      sam-apply          -> _run_via_sam_subprocess (lock, fresh process, 120 s)
      batch              -> _run_via_sam_subprocess (lock, fresh process, 600 s)
      batch-scrub        -> _run_via_sam_subprocess (lock, fresh process, 600 s)
      cache              -> _run_via_worker        (lock, warm worker)
      single             -> _run_via_worker        (lock, warm worker)
      postproc           -> _run_via_worker        (lock, warm worker)

    SAM2 commands always go to a fresh subprocess because SAM2 leaves non-daemon
    threads and Hydra atexit state alive, which prevents a persistent worker from
    ever finishing cleanly.  CNN-only commands keep using the warm worker so the
    model stays hot and subsequent frames are fast.

    CNN fallback: if the warm worker cannot start, the CNN job falls back to a
    plain per-job subprocess (existing behaviour preserved).
    """
    args = req.get("args") or []
    if not args or args[0] not in ALLOWED_CMDS:
        _send(conn, {"type": "error",
                     "msg": "command not allowed: %r" % (args[0] if args else None)})
        return
    cwd = req.get("cwd") or HERE
    env = _clean_env(req.get("root"))

    # PATH 1: extract — CPU-only frame-grab, no GPU lock, no warm worker.
    # Bypassing _run_lock means the source frame is available even while a GPU
    # job holds the lock.
    if args[0] == "extract":
        _run_extract_direct(conn, args, cwd, env)
        return

    # PATH 2: EVERY GPU job runs as a fresh one-shot subprocess, under _run_lock
    # (single GPU). The long-lived warm worker hangs intermittently AFTER finishing
    # a job (work done, output written, then no DONE sentinel) — observed on
    # sam-apply, postproc AND cache. Root: SAM2 leaks non-daemon threads + Hydra
    # atexit state and the merge module pulls SAM2 in, so a persistent process can
    # never exit a job cleanly. A fresh process per job dies clean = zero hangs.
    # Eager model load is only ~3s (CORRIDORKEY_SKIP_COMPILE=1), so per-call reload
    # is cheap; reliability >> the warm-worker optimization.
    _SUB_TIMEOUTS = {
        "sam-apply": _SAM_APPLY_TIMEOUT, "postproc": _SAM_APPLY_TIMEOUT,
        "cache": _SAM_APPLY_TIMEOUT, "single": _SAM_BATCH_TIMEOUT,
        "batch": _SAM_BATCH_TIMEOUT, "batch-scrub": _SAM_BATCH_TIMEOUT,
    }
    sub_timeout = _SUB_TIMEOUTS.get(args[0], _SAM_APPLY_TIMEOUT)
    with _run_lock:
        _log("run(subprocess/%ds): %s" % (sub_timeout, " ".join(str(a) for a in args)))
        _run_via_sam_subprocess(conn, args, cwd, env, sub_timeout)


def _handle(conn, cfg):
    try:
        conn_file = conn.makefile("r", encoding="utf-8")
        first = conn_file.readline()
        if not first:
            return
        req = json.loads(first)
        if req.get("auth") != cfg["secret"]:
            _send(conn, {"type": "error", "msg": "bad auth"})
            _log("rejected: bad auth")
            return
        op = req.get("op", "run")
        if op == "ping":
            busy = _run_lock.locked()
            _send(conn, {"type": "pong", "version": VERSION, "busy": busy})
        elif op == "shutdown":
            _send(conn, {"type": "done", "exit": 0})
            _log("shutdown requested")
            raise SystemExit(0)
        elif op == "run":
            _handle_run(conn, req)
        else:
            _send(conn, {"type": "error", "msg": "unknown op: %r" % op})
    except SystemExit:
        raise
    except Exception as e:
        try:
            _send(conn, {"type": "error", "msg": "broker error: %s" % e})
        except Exception:
            pass
        _log("handler error: %s" % e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    cfg = _load_config()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((cfg["host"], int(cfg["port"])))
    except OSError as e:
        _log("bind failed on %s:%s -> %s (another broker already running?)"
             % (cfg["host"], cfg["port"], e))
        # Another instance likely owns the port; exit quietly.
        return 0
    s.listen(8)
    _log("broker %s listening on %s:%s" % (VERSION, cfg["host"], cfg["port"]))
    try:
        while True:
            conn, _addr = s.accept()
            # Each connection handled on its own thread; the run lock keeps
            # CUDA jobs serial while pings stay responsive.
            threading.Thread(target=_handle, args=(conn, cfg), daemon=True).start()
    except SystemExit:
        _log("broker exiting")
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            s.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main() or 0)
