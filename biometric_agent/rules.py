# Copyright (c) 2026, Mothana Alsaady and contributors
# For license information, please see license.txt

"""The two rules `sync.py` (in-process) and `agent.py` (LAN) must not disagree on.

Nothing here imports `frappe` or `zk` -- `agent.py` runs on a box with neither,
and that constraint is exactly why this module exists rather than living next
to one adapter and being imported by the other. Each adapter still owns the
parts that are genuinely its own: reading a `Biometric Device` doctype versus
a JSON state file, deciding what "healthy" means from a doctype's stored
`last_error` versus this run's plain success/failure. What lands here is only
the part both sides must compute identically, so there is exactly one place
to test it and one place it can be wrong.
"""

import datetime
from typing import NamedTuple


def cursor_floor(
	cursor: datetime.datetime | None, start: datetime.datetime | None
) -> datetime.datetime | None:
	"""The earliest punch worth looking at: the cursor, or the import start date.

	On a device that has never synced this is the import start date alone,
	which is what stops a first run importing years of history nobody wants.
	Once syncing, it is the later of the two. `start` can be `None` too -- a
	site that has never set an import start date has no floor at all.
	"""
	if not start:
		return cursor

	return max(cursor, start) if cursor else start


class DeviceReport(NamedTuple):
	"""One device's contribution to a shift's advance, for one run.

	`pull_time` is meaningless when `healthy` is `False` -- nothing reads it in
	that case, but a `NamedTuple` field cannot be conditionally absent.
	"""

	healthy: bool
	pull_time: datetime.datetime


def shift_advance_target(
	reports: list[DeviceReport], current: datetime.datetime | None
) -> datetime.datetime | None:
	"""The new `last_sync_of_checkin` for one shift, or `None` if it must not move.

	Every device feeding the shift must be healthy -- advancing from a single
	device would let a shift fed by two doors be processed while one is still
	offline, silently marking half the staff absent. It moves to the earliest
	of their pull times: the last moment all of them are known to have
	reported. It never moves backwards.
	"""
	if not reports or not all(report.healthy for report in reports):
		return None

	synced_upto = min(report.pull_time for report in reports)

	if current and current >= synced_upto:
		return None

	return synced_upto
