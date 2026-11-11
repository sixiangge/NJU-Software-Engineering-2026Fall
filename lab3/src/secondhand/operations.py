"""Online backup and an interruptible maintenance worker for desktop use."""

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from secondhand.domain import require


def backup(database, directory, clock):
    """Use SQLite's online backup API, including committed WAL contents."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(clock(), UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"secondhand-{stamp}.sqlite3"
    require(not destination.exists(), "备份文件名冲突")
    source = database.connect()
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        require(target.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "备份完整性失败")
    finally:
        source.close()
        target.close()
    # Retain at least seven snapshots; deletion is deliberately manual.
    return str(destination)


class MaintenanceWorker:
    """Run expiry, durable delivery and daily backup while the desktop client lives."""

    def __init__(self, application, backup_directory, interval=30):
        require(1 <= interval <= 30, "维护周期须为1～30秒")
        self.application = application
        self.backup_directory = Path(backup_directory)
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None
        self.last_backup = None

    def tick(self):
        """A failed tick is visible and is retried without discarding outbox rows."""
        self.application.maintenance()
        today = datetime.fromtimestamp(self.application.clock(), UTC).date()
        if self.last_backup != today:
            backup(self.application.db, self.backup_directory, self.application.clock)
            self.last_backup = today
        self.error = None

    def run(self):
        """Wait on an event so exit does not have to wait for the next interval."""
        while not self.stop_event.is_set():
            try:
                self.tick()
            except (sqlite3.Error, OSError) as exc:
                self.error = str(exc)
            self.stop_event.wait(self.interval)

    def start(self):
        """One thread per client; database constraints also protect multiple clients."""
        require(self.thread is None, "维护任务已启动")
        self.thread = threading.Thread(target=self.run, name="secondhand-maintenance", daemon=True)
        self.thread.start()

    def stop(self):
        """Finish an in-flight transaction before closing the client."""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)
