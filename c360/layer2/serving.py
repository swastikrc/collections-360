"""Open the current Layer 2 serving database, read-only and locked down.

The agent's tools only ever reach data through this module. The connection is opened
read-only, then external access (files, HTTP, extensions, ATTACH, COPY) is disabled and the
configuration is locked, so even SQL that slipped past the validator cannot leave the database.
"""
import datetime as dt
import threading
from pathlib import Path

from c360.layer2 import config


class ServingError(RuntimeError):
    pass


class QueryTimeout(ServingError):
    pass


def current_path(l2_dir=None) -> Path:
    l2_dir = Path(l2_dir) if l2_dir else config.settings().l2_dir
    pointer = l2_dir / "CURRENT"
    if not pointer.exists():
        raise ServingError(f"No serving database: {pointer} not found. Run `python run_layer1.py --only publish_l2`.")
    path = l2_dir / pointer.read_text().strip()
    if not path.exists():
        raise ServingError(f"CURRENT points at {path.name}, which does not exist. Re-run publish_l2.")
    return path


class Serving:
    """A locked-down, read-only connection to one serving database file."""

    def __init__(self, path):
        import duckdb
        self.path = Path(path)
        self._lock = threading.Lock()
        self._con = duckdb.connect(str(self.path), read_only=True)
        self.build = self._read_build()
        self.text_mode = self.build.get("text_search_mode") or "like"
        if self.text_mode == "fts":
            try:        # must happen before external access is switched off
                self._con.execute("LOAD fts")
            except Exception:       # noqa: BLE001 - extension not installed on this machine
                self.text_mode = "like"
        self._con.execute("SET enable_external_access = false")
        self._con.execute("SET lock_configuration = true")
        self.row_counts = dict(self._con.execute("SELECT table_name, row_count FROM meta.row_counts").fetchall())
        self.columns: dict[str, dict[str, dict]] = {}
        for table, column, dtype, desc in self._con.execute(
                "SELECT table_name, column_name, data_type, description FROM meta.columns").fetchall():
            self.columns.setdefault(table, {})[column] = {"type": dtype, "description": desc or ""}

    def _read_build(self) -> dict:
        cur = self._con.execute("SELECT * FROM meta.build")
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else {}

    @property
    def tables(self) -> set[str]:
        return set(self.columns)

    @property
    def snapshot_date(self) -> dt.date:
        d = self.build.get("snapshot_date")
        if isinstance(d, dt.datetime):
            return d.date()
        if isinstance(d, dt.date):
            return d
        return dt.date.fromisoformat(str(d or config.settings().snapshot_date)[:10])

    @property
    def build_ts(self) -> str:
        return str(self.build.get("build_ts") or "")

    def execute(self, sql, params=None, timeout=None):
        """Run one statement. Returns (column names, rows). Interrupted after `timeout` seconds."""
        timeout = timeout if timeout is not None else config.settings().sql_timeout
        box: dict = {}

        def run():
            try:
                cur = self._con.execute(sql, params) if params else self._con.execute(sql)
                box["columns"] = [d[0] for d in cur.description] if cur.description else []
                box["rows"] = cur.fetchall() if cur.description else []
            except Exception as e:      # noqa: BLE001 - re-raised on the calling thread
                box["error"] = e

        with self._lock:
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(timeout)
            if worker.is_alive():
                self._con.interrupt()
                worker.join(10)
                raise QueryTimeout(f"query stopped after {timeout} seconds; make it narrower (add filters or a shorter period)")
        if "error" in box:
            raise box["error"]
        return box["columns"], box["rows"]

    def close(self):
        try:
            self._con.close()
        except Exception:       # noqa: BLE001
            pass


_OPEN: dict[str, Serving] = {}


def open_current(l2_dir=None) -> Serving:
    """The current serving database (one shared connection per file per process)."""
    path = current_path(l2_dir)
    key = str(path)
    if key not in _OPEN:
        _OPEN[key] = Serving(path)
    return _OPEN[key]


def close_all():
    for s in _OPEN.values():
        s.close()
    _OPEN.clear()
