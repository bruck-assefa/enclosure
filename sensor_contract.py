"""Version 1 sensor contract. Canonical copy: Pi repository; vendored on AWS."""
import copy
import math
import time

VERSION = 1
STALE_AFTER = 30
FIELDS = ("temperature_c", "humidity_pct", "pressure_hpa")


def inventory():
    # Match deployed Pi: only 0x70 enabled. Keep expected positions visible.
    return [{"sensor_id": f"sensor_{i}", "label": f"Sensor {i}",
             "enabled": i < 8, "hardware": {"bus": 1,
             "multiplexer_address": "0x70" if i < 8 else "0x72",
             "channel": i % 8, "sensor_addresses": ["0x76", "0x77"]}}
            for i in range(16)]


def validate_inventory(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise ValueError("Expected 1..16 sensors")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid sensor record")
        if not isinstance(item.get("label"), str) or len(item["label"]) > 100:
            raise ValueError("Invalid sensor label")
        sid = item["sensor_id"]
        if not isinstance(sid, str) or sid in seen:
            raise ValueError("Sensor IDs must be unique strings")
        seen.add(sid)
        hw = item["hardware"]
        if hw["bus"] != 1 or hw["multiplexer_address"] not in ("0x70", "0x72"):
            raise ValueError("Supported wiring: bus 1, multiplexers 0x70/0x72")
        if type(hw["channel"]) is not int or not 0 <= hw["channel"] <= 7:
            raise ValueError("Invalid channel")
        if not hw["sensor_addresses"] or any(a not in ("0x76", "0x77") for a in hw["sensor_addresses"]):
            raise ValueError("Invalid BME280 address")
        if not isinstance(item["enabled"], bool):
            raise ValueError("enabled must be boolean")
    return items


def new_snapshot(source="hardware", items=None):
    items = validate_inventory(items if items is not None else inventory())
    return {"schema_version": VERSION, "source": source, "generated_at": time.time(),
            "collector": {"status": "starting", "last_progress_at": None},
            "sensors": [{**copy.deepcopy(item), "source": source,
                         "status": "unavailable" if item["enabled"] else "disabled",
                         "last_good_reading": None, "last_success_at": None,
                         "last_attempt_at": None, "consecutive_failures": 0,
                         "error_code": None, "age_seconds": None} for item in items]}


def record(sensor, reading=None, error=None, now=None):
    now = time.time() if now is None else now
    sensor["last_attempt_at"] = now
    if reading is not None:
        if any(type(reading.get(k)) not in (float, int) or not math.isfinite(reading[k]) for k in FIELDS):
            raise ValueError("Non-finite or incomplete measurement")
        sensor["last_good_reading"] = {k: reading[k] for k in FIELDS}
        sensor["last_success_at"] = now
        sensor["consecutive_failures"] = 0
        sensor["error_code"] = None
        sensor["status"] = "healthy"
    else:
        sensor["consecutive_failures"] += 1
        sensor["error_code"] = error or "read_error"
        sensor["status"] = "error"


def aged(snapshot, now=None):
    now = time.time() if now is None else now
    result = copy.deepcopy(snapshot)
    result["generated_at"] = now
    progress = result["collector"].get("last_progress_at")
    if result["collector"]["status"] == "running" and progress is not None and now - progress > 45:
        result["collector"]["status"] = "stalled"
    for s in result["sensors"]:
        success = s["last_success_at"]
        s["age_seconds"] = None if success is None else max(0, now - success)
        if not s["enabled"]:
            s["status"] = "disabled"
        elif success is not None and (now - success > STALE_AFTER or success > now + 5):
            s["status"] = "stale"
        elif success is None and not s["error_code"]:
            s["status"] = "unavailable"
    return result


def validate_snapshot(data, expected_source="hardware", now=None):
    # Reject legacy /sensors payloads and invalid readings instead of inventing freshness.
    now = time.time() if now is None else now
    if not isinstance(data, dict):
        raise ValueError("Snapshot must be an object")
    if data.get("schema_version") != VERSION or data.get("source") != expected_source:
        raise ValueError("Incompatible sensor snapshot")
    validate_inventory(data["sensors"])
    collector = data.get("collector")
    if not isinstance(collector, dict) or collector.get("status") not in (
            "starting", "running", "blocked", "stalled", "stopped"):
        raise ValueError("Invalid collector health")
    progress = collector.get("last_progress_at")
    if progress is not None and (type(progress) not in (int, float) or not math.isfinite(progress)
                                 or progress < 0 or progress > now + 5):
        raise ValueError("Invalid collector progress")
    for s in data["sensors"]:
        if s.get("source") != expected_source or s.get("status") not in (
                "healthy", "stale", "unavailable", "error", "disabled"):
            raise ValueError("Invalid source or health")
        for key in ("last_success_at", "last_attempt_at"):
            value = s[key]
            if value is not None and (type(value) not in (int, float) or
                    not math.isfinite(value) or value < 0 or value > now + 5):
                raise ValueError("Invalid source timestamp; check clocks")
        if type(s["consecutive_failures"]) is not int or s["consecutive_failures"] < 0:
            raise ValueError("Invalid failure count")
        reading = s["last_good_reading"]
        if (reading is None) != (s["last_success_at"] is None):
            raise ValueError("Measurement requires a source timestamp")
        if s["status"] == "healthy" and reading is None:
            raise ValueError("Healthy sensor has no measurement")
        if reading is not None and not isinstance(reading, dict):
            raise ValueError("Invalid measurement object")
        if reading is not None and any(type(reading.get(k)) not in (int, float)
                or not math.isfinite(reading[k]) for k in FIELDS):
            raise ValueError("Invalid measurement")
        if s["error_code"] is not None and not isinstance(s["error_code"], str):
            raise ValueError("Invalid error")
    return copy.deepcopy(data)
