"""Hardware-free deterministic scenarios. Never imports GPIO or writes a database."""
import math
import time
from sensor_contract import new_snapshot, record, aged

SCENARIOS = ("healthy", "sensor_error", "stale", "intermittent", "mux_offline", "pi_offline")


def simulate(scenario="healthy", now=None):
    if scenario not in SCENARIOS:
        raise ValueError("Unknown simulation scenario")
    now = time.time() if now is None else now
    state = new_snapshot("simulation")
    state["collector"] = {"status": "running", "last_progress_at": now}
    for i, s in enumerate(state["sensors"]):
        if not s["enabled"]:
            continue
        record(s, {"temperature_c": round(28 + 5 * math.sin(i + now / 300), 2),
                   "humidity_pct": round(40 + 4 * math.sin(now / 120 + i), 2),
                   "pressure_hpa": 1005.0}, now=now)
        if scenario in ("stale", "pi_offline"):
            s["last_success_at"] = now - 90
        if (scenario == "sensor_error" and i == 1 or
            scenario == "mux_offline" or
            scenario == "intermittent" and i == 2 and int(now / 10) % 2):
            record(s, error="device_unavailable", now=now)
            s["consecutive_failures"] = 3
    if scenario == "stale":
        state["collector"] = {"status": "stalled", "last_progress_at": now - 90}
    return aged(state, now)
