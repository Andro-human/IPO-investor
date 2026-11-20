"""Local state: small JSON files + append-only JSONL logs under the state dir."""
import json
import pathlib
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST)


class State:
    def __init__(self, state_dir: pathlib.Path):
        self.dir = pathlib.Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> pathlib.Path:
        return self.dir / name

    def read(self, name: str, default):
        p = self.path(name)
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return default

    def write(self, name: str, data) -> None:
        tmp = self.path(name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str))
        tmp.replace(self.path(name))

    def append(self, name: str, record: dict) -> None:
        record = {"ts": now_ist().isoformat(timespec="seconds"), **record}
        with open(self.path(name), "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def read_jsonl(self, name: str) -> list:
        p = self.path(name)
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    # --- applications we placed (or would have placed in dry-run) ---
    def applications(self) -> dict:
        return self.read("applications.json", {})

    def save_application(self, rec: dict) -> None:
        apps = self.applications()
        apps[rec["key"]] = rec
        self.write("applications.json", apps)
