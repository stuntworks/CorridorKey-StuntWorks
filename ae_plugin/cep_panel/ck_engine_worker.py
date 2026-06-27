#!/usr/bin/env python
# ck_engine_worker.py - long-lived CorridorKey engine worker.
#
# WHY: ae_processor.py is a one-shot CLI - every job spawned its own python that
# reloaded the 2048 model (~5-8s) before doing one frame. This worker imports
# ae_processor ONCE, then runs each job by calling ae_processor.main() in the
# SAME process. ae_processor caches the loaded model (_PROC_CACHE), so the model
# loads on the first job and is reused for every job after - the per-frame reload
# is gone. Same model, same resolution => byte-identical output (zero parity risk).
#
# The broker (ck_broker.py) owns this worker: it spawns it OUTSIDE AE's CEF
# sandbox (so CUDA works), feeds jobs on stdin, and reads results on stdout.
#
# PROTOCOL (newline-delimited):
#   broker -> worker (stdin) : {"id": <n>, "argv": [<engine args>]}\n
#   worker -> broker (stdout): the engine's own stdout (log lines, PROGRESS ...),
#                              then exactly one framing line per job:
#                                __CK_JOB_DONE__ {"id": <n>, "exit": <int>}\n
#   worker -> broker (stderr): worker-level diagnostics only (kept separate so it
#                              never gets mistaken for engine output / the sentinel).
#
# A worker crash (segfault on a bad frame) just ends the stdout stream without a
# sentinel; the broker detects that, logs it, and respawns a fresh worker. One
# bad frame costs one reload, not a hung panel.

import os
import sys
import json
import traceback

DONE = "__CK_JOB_DONE__"

# ae_processor lives next to this file. Importing it binds its logging
# StreamHandler to THIS process's sys.stdout (the pipe to the broker), so all
# engine output flows to the broker unchanged.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import ae_processor  # noqa: E402  (after sys.path setup)


def _emit_done(job_id, code):
    sys.stdout.flush()
    sys.stdout.write("%s %s\n" % (DONE, json.dumps({"id": job_id, "exit": int(code or 0)})))
    sys.stdout.flush()


def main():
    sys.stderr.write("ck_engine_worker ready (pid=%d)\n" % os.getpid())
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except ValueError:
            sys.stderr.write("worker: bad job line\n"); sys.stderr.flush()
            continue

        job_id = job.get("id")
        argv = [str(a) for a in (job.get("argv") or [])]
        saved_argv = sys.argv
        sys.argv = ["ae_processor.py"] + argv
        code = 0
        try:
            ae_processor.main()  # ends in sys.exit(...) for every command
        except SystemExit as e:
            c = e.code
            if c is None or c == "":
                code = 0
            elif isinstance(c, int):
                code = c
            else:
                code = 1
        except Exception as e:
            # Engine raised without exiting; report failure, keep the worker alive.
            sys.stdout.write("worker: job error: %s\n" % e)
            sys.stdout.write(traceback.format_exc())
            code = 1
        finally:
            sys.argv = saved_argv
        _emit_done(job_id, code)


if __name__ == "__main__":
    main()
