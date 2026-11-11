# Copyright (c) 2026, Mothana Alsaady and contributors
# For license information, please see license.txt

"""Sync ZKTeco terminals into a Frappe HR site from a machine on their LAN.

`sync.py` runs this same loop inside Frappe, which is the better shape and the
one to prefer -- devices are documents, the cursor is a field, failures land in
the Error Log of the site people actually look at. It has one precondition:
the Frappe server must reach the terminals on port 4370.

A hosted site cannot. The terminals sit on an office LAN with no route from the
internet, and no amount of configuration on the site changes that. So this
module is the same loop moved to the only machine that *can* reach both: a box
inside that LAN, in a container, talking to the site over the REST API.

What that costs, stated plainly:

- **Config is a file again.** There is no `Biometric Device` record to read,
  because the site may not even have the app installed -- all this needs is
  Frappe HR and an API key. `config.toml` is that file, and it can drift from
  the site the way `local_config.py` does in the upstream tool.
- **The cursor is a file again.** `state.json` in a volume, not a field. It is
  written after every landed punch rather than at the end of a device, so a
  container killed mid-run resumes at the punch that broke instead of skipping
  it. Losing the file is survivable, not catastrophic: the next run re-reads
  from the import start date, this module's own duplicate check drops the
  repeats, and `EmployeeCheckin.validate_duplicate_log` on the site refuses
  anything that gets past it. Duplicate attendance is not reachable.

What it deliberately does *not* cost:

- **Skips are still decided here, before the site is called.** Unknown device
  user, employee who has left, punch already imported -- all three are ordinary,
  every run. The upstream tool matches ERPNext's exception *text* against an
  allowlist, and those messages go through `_()`; on an Arabic site the match
  fails and the sync aborts on somebody who simply left the company. Checking
  the data is both cheaper and language-proof.
- **The device is still behind `zk_client`.** Same module the server-side sync
  uses, imported unchanged -- including the two `pyzk` overrides that stop a
  reachable device being reported as dead and guarantee the keypad is handed
  back. A second copy of that would drift.
- **Shift Types still move only when every device feeding them reported.**

Run it with `python -m biometric_agent.agent --config ...`.
Nothing in this module imports `frappe`.
"""

import argparse
import datetime
import json
import logging
import os
import re
import socket
import sys
import time
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import requests

from biometric_agent import rules, zk_client

log = logging.getLogger("biometric-agent")

# The whitelisted Frappe HR entry point. Going through it rather than inserting
# an Employee Checkin directly is what keeps shift resolution, geolocation and
# the duplicate check on the site's side of the wire, where they belong.
CHECKIN_METHOD = "hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field"
WHOAMI_METHOD = "frappe.auth.get_logged_user"

# Optional, and the site is expected not to have it. A site without this app
# still syncs perfectly well; it just cannot show whether the terminal is up.
REPORT_METHOD = "teebatkarbala.biometric_attendance.agent_api.report_device"

# Mirrors the allowlist inside `add_log_based_on_employee_field`; anything else
# is rejected there anyway.
EMPLOYEE_FIELDNAMES = ("attendance_device_id", "name")

PUNCH_DIRECTIONS = ("AUTO", "IN", "OUT", "None")

# `device_id` is stamped verbatim onto every checkin and is the value people
# filter the checkin report by. Same rule as `BiometricDevice.validate_device_id`.
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# A terminal's own clock stamps every punch, and a drifted one produces checkins
# at the wrong time that nothing downstream can detect. Reported once at startup.
CLOCK_DRIFT_WARNING = datetime.timedelta(minutes=2)

DEFAULT_CONFIG_PATH = "/etc/tk-biometric/config.toml"

# Every setting is also an environment variable, so a single-terminal site can
# be configured entirely from `.env` with no file mounted at all -- which is
# what a container actually wants. The config file stays the answer for more
# than one terminal, and these overlay it rather than replacing it, so the two
# mix: devices in the file, credentials in the environment.
ENV_PREFIX = "TK_BIOMETRIC_"

SITE_ENV = {
	"SITE_URL": "url",
	"API_KEY": "api_key",
	"API_SECRET": "api_secret",
	"SITE_TIMEOUT": "timeout",
}

SYNC_ENV = {
	"AGENT_HOST": "agent_host",
	"REPORT_TO_SITE": "report_to_site",
	"INTERVAL": "interval",
	"IMPORT_START_DATE": "import_start_date",
	"EMPLOYEE_FIELDNAME": "employee_fieldname",
	"PUNCH_VALUES_IN": "punch_values_in",
	"PUNCH_VALUES_OUT": "punch_values_out",
	"UPDATE_SHIFT_LAST_SYNC": "update_shift_last_sync",
	"STATE_FILE": "state_file",
}

# One terminal's worth. `DEVICE_IP` is the switch that turns the whole
# environment path on; see `_read_config_file`.
DEVICE_ENV = {
	"DEVICE_ID": "device_id",
	"DEVICE_IP": "ip",
	"DEVICE_PORT": "port",
	"DEVICE_COMM_KEY": "comm_key",
	"DEVICE_TIMEOUT": "timeout",
	"DEVICE_FORCE_UDP": "force_udp",
	"DEVICE_PUNCH_DIRECTION": "punch_direction",
	"DEVICE_CLEAR_AFTER_SYNC": "clear_after_sync",
	"DEVICE_SHIFTS": "shift_types",
	"DEVICE_LATITUDE": "latitude",
	"DEVICE_LONGITUDE": "longitude",
}

# Stamped on every checkin, so it is worth naming after the door -- but not
# worth refusing to start over.
DEFAULT_DEVICE_ID = "Terminal"


class ConfigError(Exception):
	"""The config file is wrong. Nothing has been attempted yet."""


class SiteError(Exception):
	"""The site refused, or could not be reached."""


# --- config ----------------------------------------------------------------


@dataclass(frozen=True)
class Device:
	"""One terminal.

	Field names match `Biometric Device` exactly, so this object can be handed
	straight to `zk_client` -- which reads `.ip`, `.port`, `.password`,
	`.timeout`, `.force_udp`, `.device_id` and `.punch_direction` off a Frappe
	document and cannot tell the difference. That is the whole adapter.
	"""

	device_id: str
	ip: str
	port: int = 4370
	password: int = 0
	timeout: int = 30
	force_udp: bool = False
	punch_direction: str = "AUTO"
	clear_from_device_on_fetch: bool = False
	latitude: float | None = None
	longitude: float | None = None
	shift_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
	url: str
	api_key: str
	api_secret: str
	http_timeout: int = 30
	interval: int = 900
	import_start_date: datetime.datetime = datetime.datetime.min
	employee_fieldname: str = "attendance_device_id"
	punch_values_in: frozenset[int] = frozenset(zk_client.DEFAULT_PUNCH_VALUES_IN)
	punch_values_out: frozenset[int] = frozenset(zk_client.DEFAULT_PUNCH_VALUES_OUT)
	update_shift_last_sync: bool = False
	state_file: Path = Path("/var/lib/tk-biometric-agent/state.json")
	agent_host: str = ""
	report_to_site: bool = True


@dataclass(frozen=True)
class Config:
	settings: Settings
	devices: tuple[Device, ...] = field(default_factory=tuple)


def load_config(path: str | Path) -> Config:
	"""Read and validate the configuration, from the environment and/or a file.

	Everything that can be wrong is caught here, before a single device is
	touched, because the alternative is a container that starts cleanly and then
	fails the same way every fifteen minutes.
	"""
	raw = _overlay_env(_read_config_file(Path(path)))

	return Config(settings=_load_settings(raw), devices=_load_devices(raw))


def _read_config_file(path: Path) -> dict:
	"""The file, or an empty config when the environment carries the device.

	`TK_BIOMETRIC_DEVICE_IP` is the switch: setting it means "one terminal,
	configured from the environment", and no file needs to exist. Without it a
	missing file is still an error, because the alternative is a mis-mounted
	volume reported as "site.url is required" three checks later.
	"""
	try:
		return tomllib.loads(path.read_text())
	except FileNotFoundError:
		if os.environ.get(ENV_PREFIX + "DEVICE_IP"):
			return {}
		raise ConfigError(
			f"No config file at {path}, and {ENV_PREFIX}DEVICE_IP is not set. Either put the "
			f"device in the environment or copy config.example.toml and mount it."
		)
	except IsADirectoryError:
		# Docker creates a directory at the mount point when the bind source does
		# not exist, which is the most common way this is mounted wrong. Saying so
		# beats a traceback about a path that looks perfectly correct.
		raise ConfigError(
			f"{path} is a directory, not a file. Docker does that when the file it was told "
			f"to mount does not exist -- check the path on the left of the ':' in the volume."
		)
	except OSError as e:
		raise ConfigError(f"Could not read {path}: {e}")
	except tomllib.TOMLDecodeError as e:
		raise ConfigError(f"{path} is not valid TOML: {e}")


def _overlay_env(raw: dict) -> dict:
	"""Let the environment win over the file, section by section."""
	site = dict(raw.get("site") or {}) | _from_env(SITE_ENV)
	sync = dict(raw.get("sync") or {}) | _from_env(SYNC_ENV)

	device = _from_env(DEVICE_ENV)
	devices = list(raw.get("devices") or [])

	if device.get("ip"):
		if devices:
			# Silently preferring one over the other is how a site ends up syncing
			# a terminal nobody thinks is configured.
			log.warning(
				"%sDEVICE_IP is set, so the %d [[devices]] block(s) in the config file are ignored.",
				ENV_PREFIX,
				len(devices),
			)
		devices = [{"device_id": DEFAULT_DEVICE_ID} | device]

	return {"site": site, "sync": sync, "devices": devices}


def _from_env(names: dict) -> dict:
	"""The set, non-empty variables from one of the tables above."""
	return {
		key: os.environ[ENV_PREFIX + name] for name, key in names.items() if os.environ.get(ENV_PREFIX + name)
	}


def _load_settings(raw: dict) -> Settings:
	site = raw.get("site") or {}
	sync = raw.get("sync") or {}

	url = str(site.get("url") or "").rstrip("/")
	if not url:
		raise ConfigError(
			f"The site URL is required. Set {ENV_PREFIX}SITE_URL, or site.url in the config file."
		)

	# The key belongs in the environment, not in a file that gets copied around
	# and pasted into chat when something breaks. Both work; env wins.
	api_key = str(site.get("api_key") or "")
	api_secret = str(site.get("api_secret") or "")
	if not api_key or not api_secret:
		raise ConfigError(
			f"No API credentials. Set {ENV_PREFIX}API_KEY and {ENV_PREFIX}API_SECRET, "
			"or site.api_key and site.api_secret in the config file."
		)

	# Required, and the reason is the whole first run: a terminal that has never
	# been cleared holds years of punches, and every one of them is an HTTP
	# request that lands Attendance in payroll periods already closed.
	start = sync.get("import_start_date")
	if not start:
		raise ConfigError(
			f"An import start date is required, e.g. {ENV_PREFIX}IMPORT_START_DATE=2026-01-01 "
			"or sync.import_start_date in the config file. Without it the first run imports "
			"the terminal's entire history."
		)

	employee_fieldname = str(sync.get("employee_fieldname") or "attendance_device_id")
	if employee_fieldname not in EMPLOYEE_FIELDNAMES:
		raise ConfigError(
			f"The employee fieldname must be one of {', '.join(EMPLOYEE_FIELDNAMES)}, "
			f"got {employee_fieldname!r}"
		)

	return Settings(
		url=url,
		api_key=api_key,
		api_secret=api_secret,
		http_timeout=_as_int(site.get("timeout"), 30, "the site timeout"),
		interval=_as_int(sync.get("interval"), 900, "the sync interval"),
		import_start_date=_parse_start_date(start),
		employee_fieldname=employee_fieldname,
		punch_values_in=_punch_values(sync.get("punch_values_in"), zk_client.DEFAULT_PUNCH_VALUES_IN),
		punch_values_out=_punch_values(sync.get("punch_values_out"), zk_client.DEFAULT_PUNCH_VALUES_OUT),
		update_shift_last_sync=_as_bool(sync.get("update_shift_last_sync"), default=False),
		state_file=Path(sync.get("state_file") or "/var/lib/tk-biometric-agent/state.json"),
		# In a container the hostname is the container id unless `hostname:` is
		# set, which is no use to somebody asking which box this is.
		agent_host=str(sync.get("agent_host") or socket.gethostname()),
		report_to_site=_as_bool(sync.get("report_to_site"), default=True),
	)


def _parse_start_date(value) -> datetime.datetime:
	"""TOML gives a `date` for a bare date and a `datetime` for a full one."""
	if isinstance(value, datetime.datetime):
		return value.replace(microsecond=0)
	if isinstance(value, datetime.date):
		return datetime.datetime.combine(value, datetime.time.min)

	try:
		return datetime.datetime.combine(datetime.date.fromisoformat(str(value)), datetime.time.min)
	except ValueError:
		raise ConfigError(f"The import start date must look like 2026-01-01, got {value!r}")


def _as_bool(value, default: bool = False) -> bool:
	"""TOML gives a real bool; the environment gives a string.

	And `bool("false")` is True, which would turn every "off" in a `.env` file
	into an "on" -- silently, and in the direction that clears device logs.
	"""
	if value is None or value == "":
		return default
	if isinstance(value, bool):
		return value

	return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default: int, what: str) -> int:
	if value is None or value == "":
		return default

	try:
		return int(str(value).strip())
	except ValueError:
		raise ConfigError(f"{what} must be a whole number, got {value!r}")


def _as_float(value, what: str) -> float | None:
	if value is None or value == "":
		return None

	try:
		return float(str(value).strip())
	except ValueError:
		raise ConfigError(f"{what} must be a number, got {value!r}")


def _as_list(value) -> tuple[str, ...]:
	"""A TOML array, or a comma-separated environment string.

	Without the string branch `"Day Shift"` becomes a tuple of nine characters,
	and nine Shift Types that do not exist.
	"""
	if not value:
		return ()
	if isinstance(value, str):
		return tuple(part.strip() for part in value.split(",") if part.strip())

	return tuple(str(part).strip() for part in value)


def _punch_values(raw, default) -> frozenset[int]:
	if raw is None:
		return frozenset(default)

	values = raw if isinstance(raw, list | tuple) else str(raw).split(",")

	try:
		parsed = frozenset(int(str(v).strip()) for v in values if str(v).strip())
	except ValueError:
		raise ConfigError(f"Punch values must be whole numbers, got {raw!r}")

	return parsed or frozenset(default)


def _load_devices(raw: dict) -> tuple[Device, ...]:
	entries = raw.get("devices") or []
	if not entries:
		raise ConfigError(
			f"No devices configured; there is nothing to sync. Set {ENV_PREFIX}DEVICE_IP, "
			"or add a [[devices]] block to the config file."
		)

	devices, seen = [], set()

	for entry in entries:
		device_id = str(entry.get("device_id") or "").strip()
		if not DEVICE_ID_RE.match(device_id):
			raise ConfigError(
				f"device_id {device_id!r} may only contain letters, numbers, hyphens and "
				"underscores -- it is stamped onto every checkin."
			)
		if device_id in seen:
			raise ConfigError(f"device_id {device_id!r} is listed twice.")
		seen.add(device_id)

		if not entry.get("ip"):
			raise ConfigError(f"Device {device_id} has no ip address.")

		direction = str(entry.get("punch_direction") or "AUTO")
		if direction not in PUNCH_DIRECTIONS:
			raise ConfigError(
				f"Device {device_id}: punch_direction must be one of {', '.join(PUNCH_DIRECTIONS)}, "
				f"got {direction!r}"
			)

		devices.append(
			Device(
				device_id=device_id,
				ip=str(entry["ip"]),
				port=_as_int(entry.get("port"), 4370, f"Device {device_id}: port"),
				password=_as_int(entry.get("comm_key"), 0, f"Device {device_id}: comm_key"),
				timeout=_as_int(entry.get("timeout"), 30, f"Device {device_id}: timeout"),
				force_udp=_as_bool(entry.get("force_udp")),
				punch_direction=direction,
				clear_from_device_on_fetch=_as_bool(entry.get("clear_after_sync")),
				latitude=_as_float(entry.get("latitude"), f"Device {device_id}: latitude"),
				longitude=_as_float(entry.get("longitude"), f"Device {device_id}: longitude"),
				shift_types=_as_list(entry.get("shift_types")),
			)
		)

	return tuple(devices)


# --- state -----------------------------------------------------------------


class State:
	"""Per-device high-water mark, on disk.

	The equivalent of `Biometric Device.last_sync_timestamp`, and it earns its
	keep the same way: the ZKTeco protocol has no "punches since X", so every
	read returns the terminal's whole log and this is the only thing standing
	between that and a full re-import every run.

	Written through on every change, by write-temp-then-rename, so a container
	killed mid-device keeps what it landed. The upstream tool recovers its
	position by seeking backwards through its own log file and parsing the last
	line; a 200-byte JSON file rewritten in place is the same information
	without the parsing.

	`readonly` is what `--dry-run` is made of. A dry run creates no checkins, so
	a cursor it advanced would mark punches as done that were never sent -- and
	the real run behind it would skip them, permanently. The whole point of the
	flag is that it changes nothing, and the cursor file is something.
	"""

	def __init__(self, path: Path, readonly: bool = False):
		self.path = path
		self.readonly = readonly
		self._cursors: dict[str, datetime.datetime] = {}

		if not path.exists():
			return

		try:
			stored = json.loads(path.read_text()).get("cursors") or {}
		except (OSError, ValueError) as e:
			# Not fatal, and not silent. A re-read from the import start date
			# is dropped by the duplicate check; pretending the file was fine
			# would be the dangerous outcome.
			log.warning("Ignoring unreadable state file %s: %s", path, e)
			return

		for device_id, value in stored.items():
			try:
				self._cursors[device_id] = datetime.datetime.fromisoformat(value)
			except (TypeError, ValueError):
				log.warning("Ignoring bad cursor for %s in %s: %r", device_id, path, value)

	def get(self, device_id: str) -> datetime.datetime | None:
		return self._cursors.get(device_id)

	def set(self, device_id: str, cursor: datetime.datetime) -> None:
		if self._cursors.get(device_id) == cursor:
			return

		self._cursors[device_id] = cursor
		self.flush()

	def flush(self) -> None:
		if self.readonly:
			return

		self.path.parent.mkdir(parents=True, exist_ok=True)
		payload = {"cursors": {k: v.isoformat() for k, v in self._cursors.items()}}

		# Rename is atomic on the same filesystem: the file is either the old
		# state or the new one, never a truncated half of either.
		tmp = self.path.with_name(self.path.name + ".tmp")
		tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
		os.replace(tmp, self.path)


# --- the site --------------------------------------------------------------


class SiteClient:
	"""The only place this module speaks HTTP.

	A seam, for the same reason `zk_client` is one: it is what lets the tests
	assert on the calls that would have been made without a site to make them
	against.
	"""

	def __init__(self, settings: Settings, dry_run: bool = False):
		self.settings = settings
		self.dry_run = dry_run
		# Flipped off for good the first time the site says it has no such
		# method. See `report_device`.
		self.reporting = settings.report_to_site
		self.session = requests.Session()
		self.session.headers.update(
			{
				"Authorization": f"token {settings.api_key}:{settings.api_secret}",
				"Accept": "application/json",
				"User-Agent": "tk-biometric-agent",
			}
		)

	# -- plumbing --

	def _request(self, method: str, path: str, **kwargs):
		url = f"{self.settings.url}{path}"

		try:
			response = self.session.request(method, url, timeout=self.settings.http_timeout, **kwargs)
		except requests.RequestException as e:
			raise SiteError(f"Could not reach {self.settings.url}: {e}") from e

		if not response.ok:
			raise SiteError(f"{method} {path} -> {response.status_code}: {_error_message(response)}")

		try:
			return response.json()
		except ValueError:
			raise SiteError(f"{method} {path} returned a non-JSON body: {response.text[:200]!r}")

	def _list(self, doctype: str, fields: list[str], filters: list | None = None) -> list[dict]:
		params = {"fields": json.dumps(fields), "limit_page_length": 0}
		if filters:
			params["filters"] = json.dumps(filters)

		path = f"/api/resource/{urllib.parse.quote(doctype)}"
		return self._request("GET", path, params=params).get("data") or []

	# -- calls --

	def whoami(self) -> str:
		"""Prove the credentials work before touching a device."""
		return self._request("GET", f"/api/method/{WHOAMI_METHOD}").get("message") or "?"

	def get_employee_map(self, fieldname: str) -> dict[str, dict]:
		"""Device user id -> the Employee it belongs to.

		Fetched once per run rather than once per punch. A first import of
		several thousand records is what makes the difference obvious, and the
		whole employee list is a few hundred rows.
		"""
		if fieldname == "name":
			rows = self._list("Employee", ["name", "status"])
			return {row["name"]: row for row in rows}

		rows = self._list("Employee", ["name", "status", fieldname], [[fieldname, "is", "set"]])
		return {str(row[fieldname]): row for row in rows}

	def get_existing_checkins(self, device_id: str, since: datetime.datetime | None) -> set[tuple]:
		"""Checkins this device already produced at or after `since`.

		One request instead of one lookup per punch. This is the client-side
		stand-in for the `frappe.db.exists` check in `sync.push_punch`, and it
		matters on every single run: the cursor is inclusive, so the punches
		sharing its exact second always come round again.
		"""
		filters: list = [["device_id", "=", device_id]]
		if since:
			filters.append(["time", ">=", since.strftime(TIMESTAMP_FORMAT)])

		rows = self._list("Employee Checkin", ["employee", "time", "log_type"], filters)

		return {(row["employee"], str(row["time"])[:19], row["log_type"] or None) for row in rows}

	def create_checkin(self, device: Device, punch: dict, timestamp, log_type, employee_fieldname):
		if self.dry_run:
			log.info("      would create checkin: %s at %s (%s)", punch["user_id"], timestamp, log_type)
			return None

		payload = {
			"employee_field_value": punch["user_id"],
			"timestamp": timestamp.strftime(TIMESTAMP_FORMAT),
			"device_id": device.device_id,
			"log_type": log_type,
			"employee_fieldname": employee_fieldname,
			"latitude": device.latitude or None,
			"longitude": device.longitude or None,
		}

		return self._request("POST", f"/api/method/{CHECKIN_METHOD}", json=payload).get("message")

	def get_shift_sync(self, shift: str) -> datetime.datetime | None:
		rows = self._list("Shift Type", ["name", "last_sync_of_checkin"], [["name", "=", shift]])
		if not rows:
			raise SiteError(f"Shift Type {shift!r} does not exist on the site.")

		value = rows[0].get("last_sync_of_checkin")
		return datetime.datetime.fromisoformat(str(value)) if value else None

	def set_shift_sync(self, shift: str, synced_upto: datetime.datetime) -> None:
		if self.dry_run:
			log.info("  would set Shift Type %s last_sync_of_checkin to %s", shift, synced_upto)
			return

		self._request(
			"PUT",
			f"/api/resource/Shift Type/{urllib.parse.quote(shift)}",
			json={"last_sync_of_checkin": synced_upto.strftime(TIMESTAMP_FORMAT)},
		)

	def report_device(self, device: Device, reachable: bool, error: str = "", info: dict | None = None):
		"""Tell the site the terminal is up, or is not. Best effort, always.

		This is telemetry, not the job. The checkins are the job. So every
		failure here is swallowed: a site that has never heard of this app
		answers 404, and the agent must keep importing punches exactly as it
		did before -- not mark a healthy device failed because a status call
		did not land.

		The 404 is expected rather than exceptional, so it is logged once and
		the call is never made again for the life of the process. Repeating it
		every fifteen minutes would turn "your site does not have the module"
		into log noise that hides real errors.
		"""
		if not self.reporting or self.dry_run:
			return

		info = info or {}
		payload = {
			"device_id": device.device_id,
			"reachable": 1 if reachable else 0,
			"agent_host": self.settings.agent_host,
			"agent_interval": self.settings.interval,
			"ip": device.ip,
			"error": error,
			"serial_number": info.get("serial_number") or "",
			"firmware_version": info.get("firmware_version") or "",
		}

		try:
			self._request("POST", f"/api/method/{REPORT_METHOD}", json=payload)
		except SiteError as e:
			if _is_missing_method(e):
				self.reporting = False
				log.info(
					"The site has no device-reporting endpoint, so device status will not be "
					"shown there. Everything else works as normal."
				)
				return

			log.warning("  %s: could not report status: %s", device.device_id, e)


# What Frappe actually says when the endpoint is not there. Verified against a
# running site rather than assumed, because all four cases -- app absent, module
# absent, method absent, and a plain `frappe.throw` from the endpoint itself --
# come back as **417**. Matching on the status code would swallow the last one,
# so a rejected device_id would silently disable reporting forever. It has to be
# the message.
MISSING_METHOD_MARKERS = (
	"Failed to get method for command",  # module or method not there
	"is not installed",  # "App teebatkarbala is not installed"
	"DoesNotExistError",
)


def _is_missing_method(error: SiteError) -> bool:
	"""Is this "the site does not have that endpoint", rather than a real failure?

	A permissions problem, a validation error or a 500 must all stay loud --
	those are things an operator can act on. "You do not have the app" is not.
	"""
	text = str(error)

	return "-> 404" in text or any(marker in text for marker in MISSING_METHOD_MARKERS)


def _error_message(response) -> str:
	"""Dig the human-readable part out of a Frappe error response."""
	try:
		payload = response.json()
	except ValueError:
		return response.text.strip()[:300] or f"HTTP {response.status_code}"

	if raw := payload.get("_server_messages"):
		try:
			messages = [json.loads(m) for m in json.loads(raw)]
			return "; ".join(str(m.get("message", m)) for m in messages)
		except (ValueError, TypeError, AttributeError):
			pass

	return str(payload.get("exception") or payload.get("message") or f"HTTP {response.status_code}")


# --- the sync --------------------------------------------------------------

SKIP_OUTCOMES = (
	"skipped_before_cursor",
	"skipped_unknown_employee",
	"skipped_inactive_employee",
	"skipped_duplicate",
)


def cursor_floor(device: Device, settings: Settings, state: State) -> datetime.datetime:
	"""The earliest punch worth looking at: the cursor, or the import start date.

	Delegates to `rules.cursor_floor`; `settings.import_start_date` is never
	unset here (`load_config` requires it), so the result is never `None`.
	"""
	return cast(
		datetime.datetime, rules.cursor_floor(state.get(device.device_id), settings.import_start_date)
	)


def pull_device(
	client: SiteClient, device: Device, settings: Settings, state: State, employees: dict
) -> dict:
	"""Fetch one terminal and push everything new on it.

	Returns a summary rather than raising: the caller is a loop over every
	device, and one dead terminal is an expected state, not an exception.
	"""
	started_at = datetime.datetime.now().replace(microsecond=0)
	summary: dict = {
		"device": device.device_id,
		"ok": False,
		"fetched": 0,
		"created": 0,
		"pull_time": started_at,
		**dict.fromkeys(SKIP_OUTCOMES, 0),
	}

	try:
		punches = zk_client.get_attendance(device)
	except zk_client.BiometricDeviceError as e:
		log.error("  %s: %s", device.device_id, e)
		client.report_device(device, reachable=False, error=str(e))
		summary["error"] = str(e)
		return summary

	# Reported here rather than at the end of the run, so the site still learns
	# the terminal is healthy on a run where the *site* push is what fails.
	client.report_device(device, reachable=True)

	summary["fetched"] = len(punches)
	floor = cursor_floor(device, settings, state)

	try:
		existing = client.get_existing_checkins(device.device_id, floor)
	except SiteError as e:
		log.error("  %s: %s", device.device_id, e)
		summary["error"] = str(e)
		return summary

	cursor = state.get(device.device_id)
	error = None

	for punch in punches:
		if punch["timestamp"] < floor:
			summary["skipped_before_cursor"] += 1
			continue

		try:
			outcome = push_punch(client, punch, device, settings, employees, existing)
		except Exception as e:
			# This device stops here. The cursor stays on the last good punch,
			# so the next run resumes at the one that broke instead of skipping
			# past it -- which is the whole reason the cursor advances punch by
			# punch rather than once at the end.
			log.exception("  %s: punch at %s failed", device.device_id, punch["timestamp"])
			error = str(e)
			break

		summary[outcome] += 1
		cursor = punch["timestamp"].replace(microsecond=0)

		if outcome == "created":
			# Written through: a checkin exists on the site now, and a cursor
			# that rolls back without it means re-reading records that are
			# already there.
			state.set(device.device_id, cursor)

	if cursor:
		# Skips advance the cursor too, but are not worth a write each.
		state.set(device.device_id, cursor)

	summary["ok"] = error is None
	summary["cursor"] = cursor

	if error:
		summary["error"] = error
	elif device.clear_from_device_on_fetch and punches:
		clear_device(device, client.dry_run)

	return summary


def push_punch(client, punch, device, settings, employees, existing) -> str:
	"""Land a single punch, or say why it was skipped.

	Every skip is decided here, before the site is called. Three of them are
	ordinary on a real site, every run: the device user id belongs to nobody,
	it belongs to somebody who has left, and the punch is already imported.
	"""
	employee = employees.get(punch["user_id"])

	if not employee:
		return "skipped_unknown_employee"

	if employee.get("status") != "Active":
		return "skipped_inactive_employee"

	log_type = zk_client.resolve_log_type(punch, device, settings.punch_values_in, settings.punch_values_out)
	timestamp = punch["timestamp"].replace(microsecond=0)
	key = (employee["name"], timestamp.strftime(TIMESTAMP_FORMAT), log_type)

	if key in existing:
		return "skipped_duplicate"

	client.create_checkin(device, punch, timestamp, log_type, settings.employee_fieldname)

	# A terminal log can hold the same punch twice; so can two runs racing.
	existing.add(key)

	return "created"


def clear_device(device: Device, dry_run: bool) -> None:
	"""Wipe the terminal's log, only ever after every punch has landed.

	The upstream tool clears immediately after the fetch and writes a JSON dump
	to disk first in case the push then fails, with a recovery path that reloads
	it on startup. Clearing last removes the failure all of that exists to
	survive: if anything goes wrong the terminal still holds the data.
	"""
	if dry_run:
		log.info("  %s: would clear the device log", device.device_id)
		return

	try:
		zk_client.clear_attendance(device)
		log.info("  %s: device log cleared", device.device_id)
	except zk_client.BiometricDeviceError as e:
		# Not fatal: the punches are on the site, the terminal just still has
		# them too, and the next run will recognise and skip them.
		log.error("  %s: could not clear the device log: %s", device.device_id, e)


def update_shift_sync_timestamps(client: SiteClient, devices, results: dict) -> None:
	"""Advance `Shift Type.last_sync_of_checkin` for shifts whose devices all reported.

	Frappe HR will not mark Attendance past this timestamp -- it is the site's
	way of saying "every checkin up to here has arrived". Leave it alone and
	checkins pile up while no Attendance is ever created, which is the most
	common way this looks broken. Advance it from a single device and a shift
	fed by two doors gets processed while one door is still offline, silently
	marking half the staff absent.

	So a shift moves only when *every* device feeding it succeeded, and it moves
	to the earliest of their pull times: the last moment all of them are known
	to have reported. It never moves backwards.

	**Off by default, and only correct with exactly one agent.** `devices` is
	this agent's own config, so "every device feeding the shift" can only mean
	every device *this agent knows about*. Run a second agent at another site
	and this one finds its terminal reported, advances the shift, and lets
	Frappe HR mark the day before the other site's punches have arrived --
	precisely the failure the rule exists to prevent.

	A site with this app installed does it server side in
	`sync.update_shift_sync_timestamps`, reading the device records, where every
	agent's terminals are visible. This remains for a site running against stock
	Frappe HR, where nothing else can do it at all.
	"""
	log.warning(
		"Advancing Shift Type timestamps from this agent alone -- safe only if it is the "
		"only one. A site with the app installed should leave this off and let the site do it."
	)

	shift_devices: dict[str, list] = {}

	for device in devices:
		for shift in device.shift_types:
			shift_devices.setdefault(shift, []).append(results[device.device_id])

	for shift, device_results in shift_devices.items():
		reports = [
			rules.DeviceReport(healthy=bool(r.get("ok")), pull_time=r["pull_time"]) for r in device_results
		]

		try:
			current = client.get_shift_sync(shift)
			target = rules.shift_advance_target(reports, current)

			if target is None:
				if not all(report.healthy for report in reports):
					log.warning("  shift %s held back: a device feeding it did not report", shift)
				continue

			client.set_shift_sync(shift, target)
		except SiteError as e:
			log.error("  shift %s: %s", shift, e)
			continue

		log.info("  shift %s synced up to %s", shift, target)


def run_once(config: Config, client: SiteClient, state: State) -> dict:
	"""One pass over every device, then the shift timestamps."""
	settings = config.settings

	employees = client.get_employee_map(settings.employee_fieldname)
	log.info("%d employees carry a device user id", len(employees))

	if not employees:
		# Every punch would be skipped as "unknown employee" and the run would
		# report success having imported nothing. That silence is the most
		# common way this integration appears to be broken.
		log.warning(
			"No employee has %s set, so every punch will be skipped. "
			"Fill it in on Employee -> Attendance -> Attendance Device ID.",
			settings.employee_fieldname,
		)

	results = {}

	for device in config.devices:
		# One device's failure must not stop the others.
		results[device.device_id] = pull_device(client, device, settings, state, employees)
		log.info("  %s", format_summary(results[device.device_id]))

	if settings.update_shift_last_sync:
		update_shift_sync_timestamps(client, config.devices, results)

	return results


def format_summary(summary: dict) -> str:
	"""The same vocabulary `sync.pull_device` returns, on one line.

	Skips are printed as prominently as successes on purpose -- an unmapped
	device user id is invisible otherwise, and it is the single most common
	reason a run that "worked" imported nothing. Every punch fetched is
	accounted for by exactly one number on this line.
	"""
	parts = [
		f"{summary['device']}:",
		f"{summary['fetched']} on device",
		f"{summary['created']} created",
		f"{summary['skipped_duplicate']} already imported",
	]

	for outcome, label in (
		("skipped_before_cursor", "before the cursor"),
		("skipped_unknown_employee", "unknown employee"),
		("skipped_inactive_employee", "inactive employee"),
	):
		if summary[outcome]:
			parts.append(f"{summary[outcome]} skipped, {label}")

	if summary.get("error"):
		parts.append(f"FAILED: {summary['error']}")

	return " ".join(parts)


def report_device_clocks(devices, client: "SiteClient | None" = None) -> None:
	"""Log each terminal's own clock, once, at startup, and register it.

	Punch timestamps come from the device, not the server. A terminal whose
	clock has drifted produces checkins at the wrong time and nothing
	downstream can detect it, so the least this can do is make the drift
	visible to whoever reads the container log.

	It is also the one place that holds the terminal's identity -- serial and
	firmware -- so it is where the site is told. `get_attendance` does not
	return those, and a second connection per run to fetch them would double
	the load on hardware that serves one client at a time and answers slowly
	while somebody is punching on it.
	"""
	for device in devices:
		try:
			info = zk_client.get_device_info(device)
		except zk_client.BiometricDeviceError as e:
			log.error("  %s: %s", device.device_id, e)
			if client:
				client.report_device(device, reachable=False, error=str(e))
			continue

		if client:
			client.report_device(device, reachable=True, info=info)

		log.info(
			"  %s at %s: %s, firmware %s, clock %s",
			device.device_id,
			device.ip,
			info.get("serial_number"),
			info.get("firmware_version"),
			info.get("device_time"),
		)

		try:
			drift = datetime.datetime.fromisoformat(str(info["device_time"])) - datetime.datetime.now()
		except (KeyError, TypeError, ValueError):
			continue

		if abs(drift) > CLOCK_DRIFT_WARNING:
			log.warning(
				"  %s: device clock is %s off this host. Punch times come from the "
				"device -- correct it on the device itself.",
				device.device_id,
				abs(drift),
			)


# --- entry point -----------------------------------------------------------


def parse_args(argv=None):
	parser = argparse.ArgumentParser(
		prog="tk-biometric-agent",
		description="Sync ZKTeco terminals on this LAN into a Frappe HR site.",
	)
	parser.add_argument(
		"--config",
		default=os.environ.get("TK_BIOMETRIC_CONFIG", DEFAULT_CONFIG_PATH),
		help=f"path to config.toml (default: {DEFAULT_CONFIG_PATH})",
	)
	parser.add_argument("--once", action="store_true", help="run one pass and exit, for cron")
	parser.add_argument(
		"--device",
		action="append",
		dest="devices",
		metavar="DEVICE_ID",
		help="sync only this device; repeatable",
	)
	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="read the devices and report, but create nothing on the site",
	)
	parser.add_argument("--log-level", default=os.environ.get("TK_BIOMETRIC_LOG_LEVEL", "INFO"))

	return parser.parse_args(argv)


def select_devices(config: Config, wanted) -> Config:
	if not wanted:
		return config

	known = {d.device_id for d in config.devices}
	if unknown := set(wanted) - known:
		raise ConfigError(f"No such device in the config: {', '.join(sorted(unknown))}")

	return Config(settings=config.settings, devices=tuple(d for d in config.devices if d.device_id in wanted))


def main(argv=None) -> int:
	args = parse_args(argv)

	logging.basicConfig(
		stream=sys.stdout,
		level=args.log_level.upper(),
		format="%(asctime)s %(levelname)-7s %(message)s",
	)

	try:
		config = select_devices(load_config(args.config), args.devices)
	except ConfigError as e:
		log.error("%s", e)
		return 2

	settings = config.settings
	client = SiteClient(settings, dry_run=args.dry_run)

	# Fail fast and loudly. A bad key that only shows up as a 403 on the first
	# punch is a bad key discovered fifteen minutes late.
	try:
		log.info("Authenticated to %s as %s", settings.url, client.whoami())
	except SiteError as e:
		log.error("%s", e)
		return 3

	if args.dry_run:
		log.warning("Dry run: nothing will be created on the site and no device log will be cleared.")

	state = State(settings.state_file, readonly=args.dry_run)

	log.info("Devices:")
	report_device_clocks(config.devices, client)

	while True:
		started = time.monotonic()
		log.info("--- sync run ---")

		try:
			results = run_once(config, client, state)
		except SiteError as e:
			# In loop mode the site being briefly unreachable is weather, not a
			# reason to stop the container; the interval is the retry.
			log.error("%s", e)
			if args.once:
				return 3
			results = {}

		if args.once:
			return 0 if all(r.get("ok") for r in results.values()) else 1

		elapsed = time.monotonic() - started
		log.info("Next run in %ds", max(0, int(settings.interval - elapsed)))

		try:
			time.sleep(max(0.0, settings.interval - elapsed))
		except KeyboardInterrupt:
			log.info("Stopping.")
			return 0


if __name__ == "__main__":
	sys.exit(main())
