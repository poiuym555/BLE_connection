"""Record the live ESP32-to-jETPAK Bluetooth signal without camera acquisition."""

import argparse
import asyncio
import csv
import logging
import math
from datetime import datetime, timezone
from pathlib import Path


async def record_rssi(device, path, address, duration, interval, stop, on_sample=None):
    """Flush each fresh reading to CSV until the deadline, Stop, or a device error."""
    if not all(math.isfinite(value) and value > 0 for value in (duration, interval)):
        raise ValueError("Duration and sample interval must be finite and positive")
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + duration
    due = started
    count = 0
    with Path(path).open("x", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("timestamp_utc", "elapsed_s", "ble_address", "rssi_dbm", "error"))
        output.flush()
        while not stop.is_set() and loop.time() < deadline:
            try:
                rssi = await asyncio.wait_for(device.read_rssi(), deadline - loop.time())
            except Exception as error:
                if isinstance(error, asyncio.TimeoutError) and loop.time() >= deadline:
                    break
                writer.writerow((datetime.now(timezone.utc).isoformat(),
                                 f"{loop.time() - started:.6f}", address, "", str(error)))
                output.flush()
                raise
            elapsed = loop.time() - started
            if stop.is_set() or elapsed >= duration:
                break
            writer.writerow((datetime.now(timezone.utc).isoformat(), f"{elapsed:.6f}", address, rssi, ""))
            output.flush()
            count += 1
            if on_sample is not None:
                on_sample(elapsed, rssi, count)
            due = max(due + interval, loop.time())
            try:
                await asyncio.wait_for(stop.wait(), max(0, min(due, deadline) - loop.time()))
            except asyncio.TimeoutError:
                pass
    if not count and not stop.is_set():
        raise RuntimeError("No live RSSI sample arrived within the recording duration")
    return count


def make_frame(requested_port=None):
    """Build a standalone wx frame using the recording GUI's exact device operations."""
    import wx
    from serial.tools import list_ports
    from wx.lib.plot import PlotCanvas, PlotGraphics, PolyLine, PolyMarker
    from wxasync import AsyncBind, StartCoroutine
    from polysync import PolySyncDevice
    from polysync.core import discovery
    from polysync.core.transport import SerialCommandTransport

    class RssiFrame(wx.Frame):
        def __init__(self):
            super().__init__(None, title="PolySync Bluetooth RSSI recorder", size=(850, 650))
            self.device = None
            self.candidates = ()
            self.connected = False
            self.address = ""
            self.busy = False
            self.action = None
            self.recording = None
            self.closing = False
            self.stop = asyncio.Event()
            self.points = []
            panel = wx.Panel(self)
            body = wx.BoxSizer(wx.VERTICAL)
            self.port = wx.ComboBox(panel, value=requested_port or "", size=(160, -1))
            self.refresh_ports = wx.Button(panel, label="Refresh ports")
            self.open = wx.Button(panel, label="Open PolySync")
            self.scan = wx.Button(panel, label="Find devices")
            self.jetpak = wx.Choice(panel)
            self.connect = wx.Button(panel, label="Connect")
            self.duration = wx.SpinCtrlDouble(panel, min=1, max=86400, initial=60, inc=10, size=(100, -1))
            self.interval = wx.SpinCtrlDouble(panel, min=0.1, max=60, initial=1, inc=0.1, size=(100, -1))
            self.interval.SetDigits(1)
            self.folder = wx.DirPickerCtrl(panel, path=str(Path.cwd()), message="Choose CSV output folder")
            self.start_button = wx.Button(panel, label="Start recording")
            self.stop_button = wx.Button(panel, label="Stop")
            self.signal = wx.StaticText(panel, label="Live RSSI: —")
            self.status = wx.TextCtrl(panel, value="Open PolySync, then find and connect a jETPAK.",
                                      style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 60))
            self.plot = PlotCanvas(panel)
            for items in (
                ("PolySync port:", self.port, self.refresh_ports, self.open),
                (self.scan, self.jetpak, self.connect),
                ("Duration (s):", self.duration, "Sample interval (s):", self.interval),
                ("Save CSV to:", self.folder),
                (self.start_button, self.stop_button, self.signal),
            ):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for item in items:
                    control = wx.StaticText(panel, label=item) if isinstance(item, str) else item
                    proportion = 1 if control in (self.jetpak, self.folder) else 0
                    row.Add(control, proportion, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
                body.Add(row, 0, wx.EXPAND | wx.ALL, 8)
            body.Add(self.plot, 1, wx.EXPAND | wx.ALL, 8)
            body.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            panel.SetSizer(body)
            self.SetMinSize((750, 520))
            self.refresh_ports.Bind(wx.EVT_BUTTON, self.on_refresh_ports)
            AsyncBind(wx.EVT_BUTTON, self.on_open, self.open)
            AsyncBind(wx.EVT_BUTTON, self.on_scan, self.scan)
            AsyncBind(wx.EVT_BUTTON, self.on_connect, self.connect)
            AsyncBind(wx.EVT_BUTTON, self.on_start, self.start_button)
            self.stop_button.Bind(wx.EVT_BUTTON, lambda event: self.stop.set())
            AsyncBind(wx.EVT_CLOSE, self.on_close, self)
            self.on_refresh_ports(None)
            self.update_controls()

        def on_refresh_ports(self, event):
            value = self.port.GetValue()
            ports = discovery.supported_serial_ports(list_ports.comports())
            self.port.SetItems([port.device for port in ports])
            self.port.SetValue(value or (ports[0].device if ports else ""))

        def update_controls(self):
            recording = self.recording is not None and not self.recording.done()
            idle = not self.closing and not self.busy and not recording
            for control in (self.port, self.refresh_ports):
                control.Enable(idle and self.device is None)
            self.open.Enable(idle)
            self.open.SetLabel("Close PolySync" if self.device is not None else "Open PolySync")
            self.scan.Enable(idle and self.device is not None)
            self.jetpak.Enable(idle and not self.connected and bool(self.candidates))
            self.connect.Enable(idle and (self.connected or bool(self.candidates)))
            self.connect.SetLabel("Disconnect" if self.connected else "Connect")
            for control in (self.duration, self.interval, self.folder):
                control.Enable(idle)
            self.start_button.Enable(idle and self.connected)
            self.stop_button.Enable(recording and not self.busy and not self.closing)

        async def operate(self, operation):
            if self.closing or self.busy or (self.recording is not None and not self.recording.done()):
                return
            self.busy = True
            self.action = asyncio.current_task()
            self.update_controls()
            try:
                await operation()
            except Exception as error:
                logging.exception("Bluetooth operation failed")
                self.status.SetValue(str(error))
            finally:
                self.busy = False
                self.action = None
                self.update_controls()

        async def on_open(self, event):
            async def open_controller():
                if self.device is not None:
                    self.device.transport.close()
                    self.device = None
                    self.connected = False
                    self.address = ""
                    self.candidates = ()
                    self.jetpak.Clear()
                    self.signal.SetLabel("Live RSSI: —")
                    self.status.SetValue("PolySync closed.")
                    return
                self.status.SetValue("Identifying PolySync...")
                match = await discovery.probe_polysync_port(self.port.GetValue(), SerialCommandTransport)
                if match is None:
                    raise ConnectionError("PolySync not found. Check the port and close other PolySync applications.")
                self.device = PolySyncDevice(match.transport, firmware_version=match.version)
                try:
                    status = await self.device.status()
                    if status.output_enabled or status.sync_enabled:
                        raise RuntimeError("A recording is active. Finish it before opening the RSSI recorder.")
                except BaseException:
                    self.device.transport.close()
                    self.device = None
                    raise
                self.connected = status.ble_connected
                self.status.SetValue(f"PolySync ready on {match.name}. Find devices to identify the jETPAK.")
            await self.operate(open_controller)

        async def on_scan(self, event):
            async def scan_devices():
                self.status.SetValue("Scanning for jETPAK devices...")
                self.candidates = tuple(candidate for candidate in await self.device.scan() if candidate.is_jetpak)
                self.jetpak.SetItems([f"{item.name} ({item.address}) — scan {item.rssi} dBm"
                                      for item in self.candidates])
                if self.candidates:
                    self.jetpak.SetSelection(0)
                status = await self.device.status()
                self.connected = status.ble_connected
                if self.connected and self.candidates:
                    # Firmware puts the already connected peripheral first in the scan response.
                    self.address = self.candidates[0].address
                self.status.SetValue(f"Found {len(self.candidates)} jETPAK device(s).")
            await self.operate(scan_devices)

        async def on_connect(self, event):
            async def change_connection():
                if self.connected:
                    status = await self.device.disconnect_jetpak()
                    self.connected = status.ble_connected
                    if not self.connected:
                        self.address = ""
                        self.signal.SetLabel("Live RSSI: —")
                    self.status.SetValue("Disconnected. Find devices before connecting again.")
                    self.candidates = ()
                    self.jetpak.Clear()
                    return
                selection = self.jetpak.GetSelection()
                if selection == wx.NOT_FOUND:
                    raise ValueError("Select a scanned jETPAK device first")
                address = self.candidates[selection].address
                self.status.SetValue("Connecting...")
                status = await self.device.connect_jetpak(address)
                self.connected = status.ble_connected
                if not self.connected:
                    raise ConnectionError("jETPAK did not connect")
                self.address = address
                self.status.SetValue(f"Connected to {address}. Ready to record live RSSI.")
            await self.operate(change_connection)

        async def on_start(self, event):
            async def start_recording():
                if not self.address:
                    raise ValueError("Find devices first to identify the connected jETPAK")
                folder = Path(self.folder.GetPath())
                if not folder.is_dir():
                    raise ValueError("Choose an existing output folder")
                # Probe the live command before creating a file; discovery RSSI is never a substitute.
                rssi = await self.device.read_rssi()
                self.signal.SetLabel(f"Live RSSI: {rssi} dBm")
                path = folder / f"bluetooth_rssi_{datetime.now():%Y%m%d-%H%M%S-%f}.csv"
                self.points = []
                self.stop = asyncio.Event()
                self.recording = StartCoroutine(self.capture(path), self)
            await self.operate(start_recording)

        def show_sample(self, elapsed, rssi, count):
            self.signal.SetLabel(f"Live RSSI: {rssi} dBm")
            self.points.append((elapsed, rssi))
            # Bound redraw cost while retaining every reading in the CSV.
            points = self.points[::max(1, len(self.points) // 2000)]
            lines = [PolyLine(points, colour="#2468b4", width=2),
                     PolyMarker([self.points[-1]], colour="#2468b4", marker="circle", size=1)]
            self.plot.Draw(PlotGraphics(lines, "Live Bluetooth signal", "Elapsed (s)", "RSSI (dBm)"),
                           xAxis=(0, max(1, elapsed)))
            self.status.SetValue(f"Recording: {elapsed:.1f} / {self.duration.GetValue():g} s; {count} samples\n{self.path}")

        async def capture(self, path):
            self.path = path
            self.status.SetValue(f"Recording live RSSI...\n{path}")
            self.update_controls()
            try:
                count = await record_rssi(self.device, path, self.address, self.duration.GetValue(),
                                          self.interval.GetValue(), self.stop, self.show_sample)
                result = "Stopped" if self.stop.is_set() else "Completed"
                self.status.SetValue(f"{result}: {count} samples saved.\n{path}")
            except Exception as error:
                logging.exception("RSSI recording failed")
                self.status.SetValue(f"Recording failed: {error}\nReadings already saved: {path}")
                try:
                    self.connected = (await self.device.status()).ble_connected
                except Exception:
                    self.connected = False
            finally:
                self.recording = None
                self.update_controls()

        async def on_close(self, event):
            if event.CanVeto():
                event.Veto()
            if self.closing:
                return
            self.closing = True
            self.busy = True
            self.stop.set()
            self.update_controls()
            if self.action is not None:
                self.action.cancel()
                await asyncio.gather(self.action, return_exceptions=True)
            if self.recording is not None:
                await self.recording
            if self.device is not None:
                self.device.transport.close()
            wx.GetApp().ExitMainLoop()
            self.Destroy()

    return RssiFrame()


async def run(port=None):
    from wxasync import WxAsyncApp

    app = WxAsyncApp()
    frame = make_frame(port)
    app.SetTopWindow(frame)
    frame.Show()
    await app.MainLoop()
    # Destroy queues native deletion; release wxasync bindings before interpreter shutdown.
    app.Yield()


def main():
    """Start the standalone Bluetooth RSSI application."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="PolySync serial port, for example COM5")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(arguments.port))


if __name__ == "__main__":
    main()
