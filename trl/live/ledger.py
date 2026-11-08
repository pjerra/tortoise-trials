"""ledger.jsonl: every summon guid, hire, forced-role change and GM line, append-as-we-go.

`trials cleanup <run-dir>` replays it. Rows are plain JSON objects: {"kind": ...}
plus kind-specific fields and an ISO-8601 "at". The file is opened in append mode
and flushed per row so a killed runner still leaves the cleanup trail.
"""

import json
import os
import threading
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class Ledger:
    def __init__(self, path):
        self.path = path
        self.rows = []
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        with open(self.path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.rows.append(json.loads(line))
                except ValueError:
                    continue

    def add(self, row):
        row = dict(row)
        row.setdefault("at", now_iso())
        with self._lock:
            self.rows.append(row)
            if self.path:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
                    f.flush()

    def summons(self):
        return [r for r in self.rows if r.get("kind") == "summon"]

    def summon_guids(self):
        return [r.get("guid") for r in self.summons() if r.get("guid")]

    def of_kind(self, kind):
        return [r for r in self.rows if r.get("kind") == kind]

    def slots(self):
        """(server, slot, holder, lease_id, dir) rows for cleanup."""
        return self.of_kind("lease")
