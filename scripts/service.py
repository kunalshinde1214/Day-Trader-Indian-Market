"""Bounded foreground monitoring supervisor. Never restarts an execution request."""
import argparse
from contextlib import AbstractContextManager
import json
import os
import sqlite3
from pathlib import Path
import sys
import time

from core import Block, instant
from journal import Journal
from trader import write, read
from watch import cycle


class ProcessLock(AbstractContextManager):
    """OS-owned lock: released on process exit, including crashes; no stale PID guess."""
    def __init__(self, path): self.path = Path(path); self.stream = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0: self.stream.write(b"0"); self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close(); self.stream = None
            raise Block("MONITOR_BUSY: another process holds this work directory") from None
        return self
    def __exit__(self, *args):
        if self.stream is not None:
            self.stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally: self.stream.close(); self.stream = None


def run(workdir, journal, *, cycles=12, interval=5, max_failures=5, client=None, worker=cycle, clock=instant, sleep=time.sleep):
    if type(cycles) is not int or not 1 <= cycles <= 8640 or not 5 <= interval <= 60 or not 1 <= max_failures <= 20:
        raise Block("SERVICE: invalid bounded run settings")
    w = Path(workdir); failures = 0; completed = 0; reason = "cycle_limit"
    with ProcessLock(w / "state/monitor.lock"):
        if (w / "state/service-stop.json").exists(): raise Block("SERVICE_STOP: use clear-stop before a new run")
        health = {"state": "starting", "entry_allowed": False}
        try:
            for _ in range(cycles):
                if (w / "state/service-stop.json").exists(): reason = "requested_stop"; break
                try: health = worker(w, journal, clock(), client=client)
                except (Block, ValueError, KeyError, TypeError, OSError):
                    health = {"state": "disconnected", "entry_allowed": False, "as_of": clock().isoformat()}
                    write(w / "state/monitor.json", health)
                completed += 1
                failures = failures + 1 if health["state"] != "running" else 0
                write(w / "state/service.json", {"as_of": clock().isoformat(), "state": "running", "completed_cycles": completed, "consecutive_failures": failures})
                if failures >= max_failures:
                    reason = "failure_limit"
                    write(w / "state/halt.json", {"halted": True, "as_of": clock().isoformat()})
                    journal.enqueue({"at": clock().isoformat(), "message": "MONITOR_STOPPED: repeated feed failures; entries paused", "delivery": "local_outbox_only"})
                    break
                if completed < cycles: sleep(min(60, interval * 2 ** min(failures, 4)))
        except BaseException:
            reason = "interrupted_or_failed"
            raise
        finally:
            stopped = {**health, "as_of": clock().isoformat(), "state": "stopped", "entry_allowed": False}
            write(w / "state/monitor.json", stopped)
            write(w / "state/service.json", {"as_of": clock().isoformat(), "state": "stopped", "completed_cycles": completed, "reason": reason})
    return {"completed_cycles": completed, "reason": reason, "entry_allowed": False, "orders_submitted": 0}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["run", "stop", "clear-stop", "status"]); p.add_argument("--workdir", required=True)
    p.add_argument("--cycles", type=int, default=12); p.add_argument("--interval", type=float, default=5)
    p.add_argument("--max-failures", type=int, default=5); p.add_argument("--broker-poll", action="store_true")
    a = p.parse_args(argv); w = Path(a.workdir); j = None
    try:
        if a.command == "stop":
            write(w / "state/service-stop.json", {"at": instant().isoformat()})
            write(w / "state/halt.json", {"halted": True, "as_of": instant().isoformat()})
            result = {"stop_requested": True, "entry_halt": True}
        elif a.command == "clear-stop":
            with ProcessLock(w / "state/monitor.lock"):
                (w / "state/service-stop.json").unlink(missing_ok=True)
            result = {"stop_latch_cleared": True, "entry_halt_changed": False}
        elif a.command == "status":
            result = read(w / "state/service.json")
            # A dead process may have left 'running' on disk. Expose age, not liveness.
            result["heartbeat_age_seconds"] = (instant() - instant(result["as_of"])).total_seconds()
            result["process_liveness_verified"] = False
        else:
            client = None
            if a.broker_poll:
                from broker import adapter
                client = adapter(read(w / "config.json")["broker"])
            j = Journal(w / "state/journal.sqlite")
            # A stop latch must be deliberately removed by the operator before a new run.
            result = run(w, j, cycles=a.cycles, interval=a.interval, max_failures=a.max_failures, client=client)
        print(json.dumps(result)); return 2 if result.get("reason") == "failure_limit" else 0
    except (Block, ValueError, KeyError, TypeError, OSError, sqlite3.Error, KeyboardInterrupt):
        print(json.dumps({"status": "BLOCK_OR_STOPPED", "reason": "Monitor unavailable; inspect local service and halt state", "orders_submitted": 0})); return 2
    finally:
        if j: j.close()


if __name__ == "__main__": sys.exit(main())
