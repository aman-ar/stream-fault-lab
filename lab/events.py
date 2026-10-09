import asyncio
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid


def redact_message(message):
    # MediaMTX / FFmpeg may print input URLs. Never persist URL credentials or queries.
    message = re.sub(r'(https?|rtsp)://[^\s\]\)"\']+', r'\1://[redacted-url]', str(message))
    return message[:2000]


class Events:
    def __init__(self, directory: Path):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.records = deque(maxlen=2000)
        self.subscribers = set()
        self.sequence = 0
        self.part = 0
        self.file = None
        self._rotate()

    def _rotate(self):
        if self.file:
            self.file.close()
        self.file = (self.directory / f"{self.run_id}-{self.part:03}.jsonl").open("a")
        self.part += 1
        # Bound total trace storage to 20 files (~100MB); newest files survive.
        files = sorted(self.directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        for old in files[:-20]:
            old.unlink()

    def emit(self, component, event, message, level="INFO", **fields):
        self.sequence += 1
        record = dict(timestamp=datetime.now(timezone.utc).isoformat(), run_id=self.run_id,
                      sequence=self.sequence, component=component, level=level,
                      event=event, message=redact_message(message), **fields)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        if self.file.tell() + len(line.encode()) > 5_000_000:
            self._rotate()
        self.file.write(line)
        self.file.flush()
        self.records.append(record)
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(record)
        return record

    def subscribe(self):
        queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(queue)
        return queue

    def close(self):
        if self.file:
            self.file.close()
