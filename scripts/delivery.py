"""Opt-in webhook delivery of local notices. No sends occur without invoking send.

Transport acceptance is not proof that a person read a notice. Ambiguous attempts
are not automatically retried; receivers should deduplicate the idempotency key.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import urllib.request
from urllib.parse import urlsplit

from core import Block, instant
from journal import Journal


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def destination(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise Block("DELIVERY: configured HTTPS webhook without URL credentials/fragment required")
    return hashlib.sha256(url.encode()).hexdigest()


def post(url, token, payload):
    headers = {"Content-Type": "application/json", "Idempotency-Key": payload["id"]}
    if token:
        if any(c.isspace() for c in token): raise Block("DELIVERY: invalid local token")
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, method="POST", data=json.dumps(payload, allow_nan=False).encode(), headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
        if not 200 <= response.status < 300: raise Block("DELIVERY: unaccepted response")
    return True


def deliver(journal, url, token=None, *, sender=post, clock=instant, retry_unknown=False, limit=20):
    from service import ProcessLock
    path = journal.db.execute("PRAGMA database_list").fetchone()[2]
    if not path: raise Block("DELIVERY: persistent outbox required")
    with ProcessLock(path + ".delivery.lock"):
        return _deliver(journal, url, token, sender=sender, clock=clock, retry_unknown=retry_unknown, limit=limit)


def _deliver(journal, url, token=None, *, sender=post, clock=instant, retry_unknown=False, limit=20):
    dest = destination(url)
    if not 1 <= limit <= 100: raise Block("DELIVERY: bounded batch of 1..100 required")
    journal.db.execute("CREATE TABLE IF NOT EXISTS delivery_attempts (notice TEXT PRIMARY KEY, destination TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, at TEXT NOT NULL)")
    result = {"accepted": 0, "unknown": 0, "skipped_unresolved": 0}
    for notice in journal.pending()[:limit]:
        payload = {"id": notice["id"], "at": notice["at"], "message": notice["message"]}
        if not isinstance(payload["message"], str) or len(payload["message"]) > 8000: raise Block("DELIVERY: invalid notice")
        journal.db.execute("BEGIN IMMEDIATE")
        try:
            # Recheck the outbox inside the transaction; another sender may have won.
            pending = journal.db.execute("SELECT delivered_at FROM notices WHERE id=?", (notice["id"],)).fetchone()
            old = journal.db.execute("SELECT destination,state,attempts FROM delivery_attempts WHERE notice=?", (notice["id"],)).fetchone()
            if pending is None or pending[0] is not None:
                journal.db.rollback(); continue
            if old and (old[0] != dest or not retry_unknown):
                journal.db.rollback(); result["skipped_unresolved"] += 1; continue
            journal.db.execute("INSERT OR REPLACE INTO delivery_attempts VALUES (?,?,?,?,?)", (notice["id"], dest, "SENDING", old[2] + 1 if old else 1, clock().isoformat()))
            journal.db.commit()
        except BaseException:
            journal.db.rollback(); raise
        try:
            if sender(url, token, payload) is not True: raise Block("DELIVERY: no transport acceptance")
            with journal.db:
                journal.db.execute("UPDATE delivery_attempts SET state='ACCEPTED',at=? WHERE notice=?", (clock().isoformat(), notice["id"]))
                journal.db.execute("UPDATE notices SET delivered_at=? WHERE id=?", (clock().isoformat(), notice["id"]))
            result["accepted"] += 1
        except (OSError, ValueError, TypeError):
            with journal.db:
                journal.db.execute("UPDATE delivery_attempts SET state='UNKNOWN',at=? WHERE notice=?", (clock().isoformat(), notice["id"]))
            result["unknown"] += 1
    result["meaning"] = "accepted by HTTP receiver, not verified human receipt"
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["send", "status"]); p.add_argument("--journal", required=True)
    p.add_argument("--retry-unknown", action="store_true"); p.add_argument("--limit", type=int, default=20)
    a = p.parse_args(argv); j = None
    try:
        j = Journal(a.journal)
        if a.command == "status": result = {"pending": len(j.pending()), "sent": False}
        else:
            result = deliver(j, os.environ.get("DAY_TRADER_WEBHOOK_URL", ""), os.environ.get("DAY_TRADER_WEBHOOK_TOKEN"), retry_unknown=a.retry_unknown, limit=a.limit)
        print(json.dumps(result)); return 2 if result.get("unknown") else 0
    except (Block, ValueError, KeyError, TypeError, OSError, sqlite3.Error, KeyboardInterrupt):
        print(json.dumps({"status": "BLOCK_OR_UNKNOWN", "reason": "Delivery failed; destination and private response suppressed"})); return 2
    finally:
        if j: j.close()


if __name__ == "__main__": sys.exit(main())
