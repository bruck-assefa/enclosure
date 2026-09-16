> Deployment update: this implementation was deployed on 2026-09-16. See the AWS repository docs/DEPLOYMENT_2026-09-16.md for verified results and rollback locations. The original review plan below is retained as implementation history.

# Sensor collection and health — local implementation, not deployed

## Runtime boundary
The local checkout is source development. `main:app` still requires the Pi and
must not be started on Windows. Hardware-free tests and the AWS simulation preview
are supported separately. No database file is changed by the tests.

## New architecture
- `sensor_worker.py` is the only sensor module importing I2C drivers.
- `sensor_collector.py` owns one spawned worker for bus 1. Reads are sequential.
- Each attempt has a 3-second wall deadline, including process startup.
- On failure the process is discarded to avoid reusing potentially held driver locks.
- Terminate and kill are each given 0.5 seconds. An unkillable process prevents
  replacement: collection reports blocked; the API remains available.
- Failed sensors retry with exponential backoff (10, 20, 40, then 60 seconds).
  Successful sensors are eligible again after 5 seconds. A shared-bus failure can
  still delay other collection attempts, but cannot make HTTP wait for hardware.
- State is in memory; restart starts unavailable. No old reading is presented as new.
- Only one Uvicorn worker is supported. Do not use reload/multiple workers on the Pi.

## Endpoints
`GET /v1/sensors` returns schema version 1. Epoch timestamps are UTC seconds.
Each sensor has source, enabled flag, label, wiring, health, last good measurement,
last attempt/success timestamps, failure count and a stable error category.
Temperature is Celsius, humidity percent RH, pressure hPa.
Healthy readings become stale after 30 seconds. Last good values survive failures.
Error and freshness are separate: a stale reading retains its last error.
Collector progress older than 45 seconds is reported stalled.

`/sensors` and `/sensors/{id}` retain the previous dictionary shape and
online/offline/error names, with health and last-success additions. Missing values
are null instead of misleading zeroes. `/system/scan` now returns cached
online/offline/error health; it no longer performs a hardware scan.

Relay, SQLite, sunrise/sunset, and schedule behavior is otherwise preserved.
The existing startup GPIO initialization and schedule synchronization still occur.
Restarting the Pi service therefore requires an explicitly reviewed deployment window.

## Inventory
Default: sensor_0..7 on 0x70 channels 0..7 enabled. Positions 8..15 on 0x72
are visible but disabled. This matches the inspected deployed code's single mux,
rather than the older local two-mux initialization.
For labels/enable flags, set `ENCLOSURE_SENSOR_CONFIG` to a JSON inventory file.
Its shape is the list returned by `sensor_contract.inventory()`; bus 1,
muxes 0x70/0x72, and BME280 candidate addresses 0x76/0x77 are supported.
No runtime endpoint changes wiring or physical mode. Do not enable the second mux
without confirming the intended hardware inventory.

## Simulation
`sensor_simulator.py` produces the same contract, source=simulation.
It imports no hardware libraries and touches no GPIO/database.
The AWS preview and dashboard expose its scenarios. Simulation never replaces a
failing live sensor automatically. Physical controls are disabled in simulation.

## Tests
Using a Python 3.11+ environment from the repository root:
```
python -m unittest discover -s tests -v
```
Contract and supervisor tests require only Python's standard library.
The hang test uses a spawned fake worker and verifies timeout, recovery to the next
sensor, responsive cached reads and child cleanup. This does not validate actual
Pi driver behavior.

## Cross-repository contract
`sensor_contract.py` and `sensor_simulator.py` are canonical here and vendored
byte-for-byte in AWS `home/admin/smart-enclosure-api/`. Review both copies together.
No Python package publication or runtime Git dependency is required.

## Deployment review (not executed)
Preserve the Pi's uncommitted main.py and database first. The old live code used
thread-based initialization and only mux 0x70; this change replaces that sensor
path without overwriting the live database. The installed service's
PYTHONUNBUFFERED=1 addition must be retained.
Deploy only reviewed Python sources/dependencies, never the repository's enclosure.db.
Validate child-process permissions, deadline behavior, last-success timestamps,
memory/CPU and healthy-sensor progress over SSH after authorized deployment.
A stuck Linux I2C kernel operation or electrical fault may require hardware repair;
this supervisor is not a physical bus-reset mechanism.
