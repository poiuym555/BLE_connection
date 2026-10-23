import asyncio
import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bluetooth_rssi import record_rssi


class RssiRecordingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "rssi.csv"
        self.address = "AA:BB:CC:DD:EE:FF"
        self.stop = asyncio.Event()
        self.device = AsyncMock()
        self.outputs = []
        original_open = Path.open

        def open_path(path, mode="r", *args, **kwargs):
            output = original_open(path, mode, *args, **kwargs)
            if path == self.path and mode == "x":
                self.outputs.append(output)
            return output

        patched = patch.object(Path, "open", open_path)
        patched.start()
        self.addCleanup(patched.stop)

    def rows(self):
        with self.path.open(newline="", encoding="utf-8") as source:
            return list(csv.DictReader(source))

    async def record(self, **kwargs):
        return await record_rssi(self.device, self.path, self.address,
                                 kwargs.pop("duration", 1), kwargs.pop("interval", 0.001),
                                 self.stop, **kwargs)

    async def test_fresh_readings_include_utc_elapsed_address_and_flush_each_row(self):
        self.device.read_rssi.side_effect = [-38, -57, -72]
        samples = []

        def on_sample(elapsed, rssi, count):
            samples.append((elapsed, rssi, count))
            self.assertEqual(len(self.rows()), count)
            if count == 3:
                self.stop.set()

        self.assertEqual(await self.record(on_sample=on_sample), 3)
        rows = self.rows()
        self.assertEqual(list(rows[0]),
                         ["timestamp_utc", "elapsed_s", "ble_address", "rssi_dbm", "error"])
        self.assertEqual([int(row["rssi_dbm"]) for row in rows], [-38, -57, -72])
        self.assertEqual([sample[2] for sample in samples], [1, 2, 3])
        self.assertEqual(self.device.read_rssi.await_count, 3)
        elapsed = [float(row["elapsed_s"]) for row in rows]
        self.assertEqual(elapsed, sorted(elapsed))
        self.assertTrue(all(0 <= value < 1 for value in elapsed))
        for row in rows:
            self.assertEqual(row["ble_address"], self.address)
            self.assertEqual(row["error"], "")
            self.assertEqual(datetime.fromisoformat(row["timestamp_utc"]).utcoffset(), timedelta(0))
        self.assertTrue(self.outputs[0].closed)

    async def test_stop_before_recording_prevents_reads(self):
        self.stop.set()
        self.assertEqual(await self.record(), 0)
        self.device.read_rssi.assert_not_awaited()
        self.assertEqual(self.rows(), [])
        self.assertTrue(self.outputs[0].closed)

    async def test_stop_during_read_discards_result_and_prevents_further_reads(self):
        async def read():
            self.stop.set()
            return -42

        self.device.read_rssi.side_effect = read
        self.assertEqual(await self.record(), 0)
        self.device.read_rssi.assert_awaited_once()
        self.assertEqual(self.rows(), [])
        self.assertTrue(self.outputs[0].closed)

    async def test_value_returning_after_deadline_is_excluded(self):
        async def read():
            if self.device.read_rssi.await_count == 1:
                return -40
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                loop = asyncio.get_running_loop()
                after_deadline = loop.time() + 0.03
                while loop.time() < after_deadline:
                    await asyncio.sleep(0.005)
                return -80

        self.device.read_rssi.side_effect = read
        self.assertEqual(await self.record(duration=0.03), 1)
        self.assertEqual([row["rssi_dbm"] for row in self.rows()], ["-40"])
        self.assertEqual(self.device.read_rssi.await_count, 2)
        self.assertTrue(self.outputs[0].closed)

    async def test_no_sample_within_duration_reports_error(self):
        async def read():
            await asyncio.Future()

        self.device.read_rssi.side_effect = read
        with self.assertRaisesRegex(RuntimeError, "No live RSSI sample"):
            await self.record(duration=0.02)
        self.assertEqual(self.rows(), [])
        self.assertTrue(self.outputs[0].closed)

    async def test_device_failure_preserves_flushed_readings_and_error_row(self):
        async def read():
            if self.device.read_rssi.await_count == 1:
                return -45
            self.assertEqual(self.rows()[0]["rssi_dbm"], "-45")
            raise ConnectionError("Bluetooth disconnected")

        self.device.read_rssi.side_effect = read
        with self.assertRaisesRegex(ConnectionError, "Bluetooth disconnected"):
            await self.record()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rssi_dbm"], "-45")
        self.assertEqual(rows[1]["rssi_dbm"], "")
        self.assertEqual(rows[1]["ble_address"], self.address)
        self.assertEqual(rows[1]["error"], "Bluetooth disconnected")
        self.assertTrue(self.outputs[0].closed)

    async def test_invalid_timing_does_not_open_output_or_read_device(self):
        for field in ("duration", "interval"):
            for value in (0, -1, float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    await self.record(**{field: value})
        self.assertFalse(self.path.exists())
        self.device.read_rssi.assert_not_awaited()

    async def test_device_timeout_before_deadline_is_saved_and_raised(self):
        self.device.read_rssi.side_effect = asyncio.TimeoutError("Serial reply timed out")
        with self.assertRaisesRegex(asyncio.TimeoutError, "Serial reply timed out"):
            await self.record()
        self.assertEqual(self.rows()[0]["error"], "Serial reply timed out")
        self.assertEqual(self.rows()[0]["rssi_dbm"], "")
        self.assertTrue(self.outputs[0].closed)

    async def test_existing_output_is_never_overwritten(self):
        self.path.write_text("existing recording", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            await self.record()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "existing recording")
        self.device.read_rssi.assert_not_awaited()

    async def test_cancellation_closes_file_and_preserves_prior_readings(self):
        reading = asyncio.Event()

        async def read():
            if self.device.read_rssi.await_count == 1:
                return -49
            reading.set()
            await asyncio.Future()

        self.device.read_rssi.side_effect = read
        task = asyncio.create_task(self.record(duration=10))
        try:
            await asyncio.wait_for(reading.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual([row["rssi_dbm"] for row in self.rows()], ["-49"])
        self.assertTrue(self.outputs[0].closed)


if __name__ == "__main__":
    unittest.main()
