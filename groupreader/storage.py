from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3

from .model import Message, validate_message
from .parser import TIME, parse_time


class SessionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(position INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS frames(id INTEGER PRIMARY KEY, captured_at TEXT NOT NULL,
                                               direction TEXT NOT NULL, payload TEXT NOT NULL);
        """)

    def save(self, group: str, reference: str, messages: list[Message],
             frame: list[Message] | None = None, direction: str = "newer"):
        with self.connection:
            self.connection.executemany("INSERT OR REPLACE INTO metadata VALUES(?,?)",
                                        [("group", group), ("reference", reference), ("version", "2")])
            self.connection.execute("DELETE FROM messages")
            self.connection.executemany("INSERT INTO messages VALUES(?,?)", [
                (i, json.dumps(message.to_dict(), ensure_ascii=False)) for i, message in enumerate(messages)])
            if frame is not None:
                self.connection.execute("INSERT INTO frames(captured_at,direction,payload) VALUES(?,?,?)", (
                    datetime.now().isoformat(), direction,
                    json.dumps([m.to_dict() for m in frame], ensure_ascii=False)))

    def load(self):
        metadata = dict(self.connection.execute("SELECT key,value FROM metadata"))
        if metadata.get("version") not in {"1", "2"}:
            raise ValueError("会话文件格式不受支持")
        messages = [Message(**json.loads(row[0])) for row in self.connection.execute(
            "SELECT payload FROM messages ORDER BY position")]
        for message in messages:
            validate_message(message)
        if metadata["version"] == "1":
            messages = self._repair_legacy_dates(date.fromisoformat(metadata["reference"]), messages)
        return metadata, messages

    def _repair_legacy_dates(self, reference: date, messages: list[Message]) -> list[Message]:
        first, last = self.connection.execute("SELECT MIN(captured_at), MAX(captured_at) FROM frames").fetchone()
        if first is None:
            return messages
        first_day = datetime.fromisoformat(first).date()
        last_day = datetime.fromisoformat(last).date()
        if reference.year != first_day.year or reference >= first_day:
            return messages

        repaired = []
        for message in messages:
            if first_day == last_day or TIME.fullmatch(message.raw_time):
                legacy = parse_time(message.raw_time, reference)
                actual = parse_time(message.raw_time, reference, first_day)
                if legacy and actual and legacy[0] == message.timestamp and actual[0] != message.timestamp:
                    message = replace(message, timestamp=actual[0])
            repaired.append(message)
        return repaired

    def close(self):
        self.connection.close()
