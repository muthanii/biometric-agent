# Copyright (c) 2026, Mothana Alsaady and contributors
# For license information, please see license.txt

"""Thin wrapper around `pyzk`, the only place this app talks to a device.

Everything device-shaped is isolated here so `sync.py` deals in plain punch
dicts and never imports `zk`. That is what lets the tests run without hardware.

Nothing here imports `frappe`, deliberately. `agent.py` runs this same code on a
LAN box that has no bench and no site, and the two overrides below are the whole
reason it reuses this module instead of carrying its own copy -- a duplicate
would drift, and both of them are corrections to a failure that looks like a
dead device. The cost is that errors raised here are plain exceptions with
untranslated messages; the desk callers wrap them (`BiometricDevice.test_connection`)
or record them as text (`sync.pull_device`), which is all they did with them anyway.

Two `pyzk` defaults are overridden deliberately:

- `ommit_ping=True`. `ZK.connect()` otherwise shells out to `ping` before it
  does anything and treats a non-zero exit as "device unreachable". Office
  networks routinely filter ICMP to the door devices while leaving port 4370
  open, and a container without a `ping` binary fails the check outright --
  either way a perfectly reachable device is reported as down. The TCP connect
  that follows is the real reachability test, so the pre-check only adds ways
  to be wrong.
- `force_udp=False`. The library falls back to UDP by itself when TCP is
  refused; leaving TCP first keeps large log reads from fragmenting.
"""

import datetime
from collections.abc import Iterator
from contextlib import contextmanager

# `Attendance.punch` as reported by the device keypad. ZKTeco firmware is not
# consistent about these -- some devices only ever report 255 ("undefined"),
# which is why a device can override the mapping with a fixed direction.
DEFAULT_PUNCH_VALUES_IN = (0, 4)
DEFAULT_PUNCH_VALUES_OUT = (1, 5)


class BiometricDeviceError(Exception):
	"""Anything that went wrong between here and the terminal."""


def _load_zk():
	"""Import `pyzk` late, with an error that says what to do about it.

	The dependency is declared in `requirements.txt`. Failing here only takes
	down the sync and gives the operator an actionable error.
	"""
	try:
		# pyzk is a runtime dependency; CI type checks without installing it.
		from zk import ZK  # pyright: ignore[reportMissingImports]

		return ZK
	except ImportError as e:
		raise BiometricDeviceError(
			"The pyzk library is not installed. Install the project requirements with "
			"`pip install -r requirements.txt`."
		) from e


@contextmanager
def connect(device) -> Iterator["object"]:
	"""Open a session to `device`, disabling its keypad for the duration.

	A ZKTeco device serves one client at a time and answers slowly while
	someone is punching on it. `disable_device()` locks the keypad so the log
	cannot grow underneath a read; the `finally` block is what guarantees it is
	handed back, including when the read raises. Skipping that would leave the
	device bricked for staff until someone power-cycles it.
	"""
	ZK = _load_zk()
	conn = None
	zk = ZK(
		device.ip,
		port=device.port or 4370,
		timeout=device.timeout or 30,
		password=device.password or 0,
		force_udp=bool(device.force_udp),
		ommit_ping=True,
	)

	try:
		conn = zk.connect()
		conn.disable_device()
		yield conn
	except Exception as e:
		raise BiometricDeviceError(
			f"Could not read from device {device.device_id} at {device.ip}: {e}"
		) from e
	finally:
		if conn:
			try:
				conn.enable_device()
			finally:
				conn.disconnect()


def get_device_info(device) -> dict:
	"""Identity of the device, for the Test Connection button."""
	with connect(device) as conn:
		return {
			"serial_number": conn.get_serialnumber(),
			"firmware_version": conn.get_firmware_version(),
			"device_name": conn.get_device_name(),
			"device_time": str(conn.get_time()),
		}


def get_attendance(device) -> list[dict]:
	"""Every punch the device is holding, oldest first.

	The protocol has no "since" filter -- `get_attendance()` always returns the
	device's whole log, which on a device that is never cleared is years of
	records. Narrowing to what is new is the caller's job, and is why
	`Biometric Device.last_sync_timestamp` exists.

	Returns plain dicts rather than `pyzk` objects so nothing downstream has to
	import the library.
	"""
	with connect(device) as conn:
		logs = conn.get_attendance() or []

	punches = [
		{
			"user_id": str(log.user_id),
			"timestamp": log.timestamp,
			"punch": log.punch,
			"status": log.status,
			"uid": log.uid,
		}
		for log in logs
		if isinstance(log.timestamp, datetime.datetime)
	]

	# The device returns records in storage order, which is insertion order
	# until the log wraps. Sorting is what makes the cursor safe to advance as
	# we go: on a mid-run failure everything before the cursor is known done.
	punches.sort(key=lambda p: p["timestamp"])

	return punches


def clear_attendance(device) -> None:
	"""Wipe the device's log. Only ever called after a fully successful push."""
	with connect(device) as conn:
		conn.clear_attendance()


def resolve_log_type(punch: dict, device, punch_values_in, punch_values_out) -> str | None:
	"""Map a raw punch code to an Employee Checkin `log_type`.

	Returning None is a legitimate outcome, not a failure: it produces a
	checkin with no direction, which is what Shift Types configured
	"Alternating entries" expect.
	"""
	if device.punch_direction in ("IN", "OUT"):
		return device.punch_direction

	if device.punch_direction != "AUTO":
		return None

	if punch["punch"] in punch_values_in:
		return "IN"
	if punch["punch"] in punch_values_out:
		return "OUT"

	return None
