"""Async supervisor; HTTP handlers only read the snapshot."""
import timing_config as timing
import math
import asyncio
import json
import logging
import multiprocessing
import os
import time
from sensor_contract import new_snapshot, record, aged, validate_inventory
from sensor_worker import worker

log = logging.getLogger(__name__)


class SensorCollector:
    def __init__(self, items=None, timeout=timing.SENSOR_READ_TIMEOUT, interval=timing.SENSOR_READ_INTERVAL, target=worker):
        self.state = new_snapshot(items=items)
        self.timeout, self.interval, self.target = timeout, interval, target
        self.process = self.pipe = self.task = None
        self.stopping = False

    def snapshot(self):
        return aged(self.state)

    async def start(self):
        self.stopping = False
        self.task = asyncio.create_task(self.run())

    def spawn(self):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        process = ctx.Process(target=self.target, args=(child,), daemon=True)
        try:
            process.start()
        except Exception:
            parent.close()
            process.close()
            raise
        finally:
            child.close()
        self.process, self.pipe = process, parent

    async def reap(self):
        if self.process is None:
            return True
        if self.process.is_alive():
            self.process.terminate()
        deadline = time.monotonic() + timing.SENSOR_WORKER_STOP_TIMEOUT
        while self.process.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(timing.SENSOR_WORKER_POLL)
        if self.process.is_alive():
            self.process.kill()
            deadline = time.monotonic() + timing.SENSOR_WORKER_STOP_TIMEOUT
            while self.process.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(timing.SENSOR_WORKER_POLL)
        if self.process.is_alive():
            # Never start another bus owner while a kernel-blocked process lives.
            self.state["collector"]["status"] = "blocked"
            return False
        self.process.join(timeout=0)
        self.process.close()
        self.pipe.close()
        self.process = self.pipe = None
        return True

    async def read(self, sensor):
        if self.process is None:
            self.spawn()
        self.pipe.send({"sensor_id": sensor["sensor_id"], "hardware": sensor["hardware"]})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.pipe.poll():
                return self.pipe.recv()
            if not self.process.is_alive():
                return {"error": "worker_exited"}
            await asyncio.sleep(timing.SENSOR_WORKER_POLL)
        return {"error": "read_timeout"}

    async def run(self):
        due = {}
        try:
            while not self.stopping:
                if self.state["collector"]["status"] == "blocked":
                    if not await self.reap():
                        await asyncio.sleep(timing.SENSOR_BLOCKED_RETRY)
                        continue
                self.state["collector"]["status"] = "running"
                for s in self.state["sensors"]:
                    if self.stopping:
                        break
                    if not s["enabled"] or time.monotonic() < due.get(s["sensor_id"], 0):
                        continue
                    s["last_attempt_at"] = time.time()
                    try:
                        result = await self.read(s)
                        record(s, result.get("reading"), result.get("error"))
                    except Exception:
                        log.exception("Sensor worker failed for %s", s["sensor_id"])
                        record(s, error="worker_error")
                    self.state["collector"]["last_progress_at"] = time.time()
                    if s["error_code"]:
                        log.warning("%s: %s", s["sensor_id"], s["error_code"])
                        if not await self.reap():
                            break
                    max_doublings = max(0, math.ceil(math.log2(timing.SENSOR_RETRY_MAX / self.interval)))
                    delay = min(timing.SENSOR_RETRY_MAX,
                                self.interval * 2 ** min(s["consecutive_failures"], max_doublings))
                    due[s["sensor_id"]] = time.monotonic() + delay
                await asyncio.sleep(timing.SENSOR_LOOP_PAUSE)
        except asyncio.CancelledError:
            raise
        finally:
            await self.reap()

    async def stop(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


def configured_collector():
    path = os.environ.get("ENCLOSURE_SENSOR_CONFIG")
    items = None
    if path:
        with open(path, encoding="utf-8") as file:
            items = validate_inventory(json.load(file))
    return SensorCollector(items=items)
