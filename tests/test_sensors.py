import asyncio
import time
import unittest
from sensor_contract import aged, inventory, new_snapshot, record, validate_snapshot
from sensor_collector import SensorCollector
from sensor_simulator import simulate

READING = {"temperature_c": 25.0, "humidity_pct": 40.0, "pressure_hpa": 1000.0}


def fake_worker(pipe):
    while True:
        config = pipe.recv()
        if config["sensor_id"] == "sensor_0":
            time.sleep(30)  # A real stuck child, not a mocked timeout.
        pipe.send({"reading": READING})


class ContractTests(unittest.TestCase):
    def test_failure_keeps_last_good_and_ages(self):
        state = new_snapshot()
        sensor = state["sensors"][0]
        record(sensor, READING, now=100)
        record(sensor, error="read_timeout", now=110)
        self.assertEqual(sensor["last_good_reading"], READING)
        self.assertEqual(sensor["last_success_at"], 100)
        self.assertEqual(aged(state, 120)["sensors"][0]["status"], "error")
        self.assertEqual(aged(state, 140)["sensors"][0]["status"], "stale")

    def test_disabled_visible_and_no_fabricated_zero(self):
        state = new_snapshot()
        self.assertEqual(len(state["sensors"]), 16)
        self.assertIsNone(state["sensors"][0]["last_good_reading"])
        self.assertEqual(state["sensors"][8]["status"], "disabled")

    def test_recovery_clears_failures(self):
        s = new_snapshot()["sensors"][0]
        record(s, error="read_timeout", now=100)
        record(s, READING, now=101)
        self.assertEqual(s["consecutive_failures"], 0)
        self.assertIsNone(s["error_code"])

    def test_legacy_simulation_and_bad_timestamps_rejected(self):
        for data in ({}, [], simulate()):
            with self.assertRaises(ValueError):
                validate_snapshot(data)
        state = new_snapshot()
        record(state["sensors"][0], READING, now=time.time() + 500)
        with self.assertRaises(ValueError):
            validate_snapshot(state)

    def test_nonfinite_rejected(self):
        with self.assertRaises(ValueError):
            record(new_snapshot()["sensors"][0], dict(READING, temperature_c=float("nan")))

    def test_simulation_scenarios_share_contract(self):
        for scenario in ("healthy", "sensor_error", "stale", "intermittent", "mux_offline", "pi_offline"):
            data = simulate(scenario, now=1000)
            validate_snapshot(data, expected_source="simulation", now=1000)
        self.assertEqual(simulate("stale", now=1000)["sensors"][0]["status"], "stale")
        self.assertEqual(simulate("sensor_error", now=1000)["sensors"][1]["error_code"], "device_unavailable")


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stuck_sensor_does_not_block_api_or_next_sensor(self):
        collector = SensorCollector(items=inventory()[:2], target=fake_worker, timeout=1.5, interval=60)
        await collector.start()
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                start = time.monotonic()
                snapshot = collector.snapshot()
                self.assertLess(time.monotonic() - start, .1)
                if snapshot["sensors"][1]["status"] == "healthy":
                    break
                await asyncio.sleep(.02)
            else:
                self.fail("Second sensor was blocked by the first sensor")
            self.assertEqual(snapshot["sensors"][0]["error_code"], "read_timeout")
            self.assertEqual(snapshot["sensors"][0]["consecutive_failures"], 1)
            self.assertEqual(snapshot["sensors"][1]["last_good_reading"], READING)
        finally:
            await collector.stop()
        self.assertIsNone(collector.process)

    async def test_deadline_reaps_worker(self):
        collector = SensorCollector(items=inventory()[:1], target=fake_worker, timeout=.5)
        result = await collector.read(collector.state["sensors"][0])
        self.assertEqual(result["error"], "read_timeout")
        self.assertTrue(await collector.reap())
        self.assertIsNone(collector.process)


if __name__ == "__main__":
    unittest.main()
