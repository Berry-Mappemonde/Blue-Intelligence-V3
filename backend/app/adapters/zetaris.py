"""Read-only Zetaris REST adapter, with credential-free sample fixtures.

Environment variables:
    ZETARIS_REST_URL: API base URL (default http://localhost:8888/api/v1.0).
    ZETARIS_ORG_ID: organisation ID, required in live mode.
    ZETARIS_API_KEY: bearer token; missing/empty selects sample mode.
    SAMPLE_MODE: 'true' forces sample mode, even when credentials exist.
    TRACE_LOG_PATH: override logs/trace-<run_id>.jsonl at the repository root.
    TRACE_STDOUT: '0' disables stdout traces; defaults to '1'.
    ZETARIS_LIVE: '1' enables the opt-in integration test (not used by the client).

query(sql, fixture='example') selects a JSON fixture in sample mode; the default
fixture is 'example'. Live mode always executes the guarded SQL through Zetaris.
test_connection() uses 'connection' and 'databases' fixtures in sample mode.
Each query call, including rejected/failed calls, emits one JSONL trace.
dry_run() only validates; it neither executes SQL nor emits a query event.

The SQL guard supports doubled quotes and Spark backslash escapes in literals.
It is a conservative lexical guard, not a SQL parser or a database permission
boundary; the live account should also have read-only permissions.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from collections.abc import Callable

import httpx

from app.core import paths
from app.adapters.registry_sources import UNLOCODE_COLUMNS, WPI_COLUMNS

DEFAULT_TRACE_LOG_DIR = paths.LOGS_DIR
SAMPLE_DIR = paths.SAMPLE_DATA_DIR / "zetaris"
_ALLOWED = {"SELECT", "SHOW", "DESCRIBE", "EXPLAIN", "WITH"}
_FORBIDDEN = {
    "CREATE", "DROP", "INSERT", "UPDATE", "DELETE", "ALTER", "REGISTER",
    "UPSERT", "TRUNCATE", "GRANT", "REVOKE",
}
_TOKEN = re.compile(
    r"'(?:\\[\s\S]|''|[^'\\])*'|\"(?:\\[\s\S]|\"\"|[^\"\\])*\"|--[^\n]*(?:\n|$)|"
    r"/\*[\s\S]*?\*/|[A-Za-z_][A-Za-z_0-9]*|\S"
)
_LOGGER = logging.getLogger(__name__)
_PORT_COLUMNS = ("name", "lat", "lon", "source_urls", "validated", "geocode_source", "zone_name", "mrgid", "id")
_PORT_SELECT = (
    "SELECT name, lat, lon, to_json(source_urls) AS source_urls, validated, "
    "geocode_source, zone_name, mrgid, id FROM blue_intel.poe_ports"
)


_REGISTRIES = {
    "unlocode": ("blue_intel.unlocode_ports", UNLOCODE_COLUMNS),
    "wpi": ("blue_intel.wpi_ports", WPI_COLUMNS),
}


def _valid_bbox(bbox) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon) of finite, ordered, in-range floats."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4 or any(isinstance(v, bool) for v in bbox):
        raise ValueError("bbox must be (min_lat, min_lon, max_lat, max_lon)")
    try:
        min_lat, min_lon, max_lat, max_lon = (float(v) for v in bbox)
    except (TypeError, ValueError):
        raise ValueError("bbox values must be numbers") from None
    values = (min_lat, min_lon, max_lat, max_lon)
    if (not all(math.isfinite(v) for v in values) or not -90 <= min_lat <= max_lat <= 90
            or not -180 <= min_lon <= max_lon <= 180):
        raise ValueError("bbox must be finite, ordered and within latitude/longitude ranges")
    return values


class ReadOnlyViolation(ValueError):
    """SQL does not satisfy the adapter's read-only policy."""


class ZetarisUnavailable(RuntimeError):
    """Zetaris or its sample data could not provide a valid response."""


class ZetarisClient:
    """Synchronous, read-only client. Reuse one instance to retain its run ID."""

    def __init__(self, run_id: str | None = None):
        self.run_id = str(uuid4()) if run_id is None else run_id
        if not isinstance(self.run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", self.run_id):
            raise ValueError("run_id must contain only letters, digits, underscores or hyphens")
        self._api_key = os.environ.get("ZETARIS_API_KEY", "").strip()
        self._org_id = os.environ.get("ZETARIS_ORG_ID", "").strip()
        self._base_url = os.environ.get(
            "ZETARIS_REST_URL", "http://localhost:8888/api/v1.0"
        ).rstrip("/")
        sample = os.environ.get("SAMPLE_MODE", "").strip().lower() == "true"
        self.mode = "sample" if sample or not self._api_key else "live"
        _LOGGER.info("ZetarisAdapter mode=%s", self.mode)

    def dry_run(self, sql: str) -> bool:
        """Return True for permitted SQL; otherwise raise ReadOnlyViolation.

        Within single or double quotes, a backslash escapes exactly the next
        character (including another backslash); doubled matching quotes also
        stay inside the literal. Unclosed literals, including dangling escapes,
        are rejected. Only semicolons and keywords outside literals are SQL.
        """
        if not isinstance(sql, str) or not sql.strip():
            raise ReadOnlyViolation("SQL must be a non-empty string")
        tokens = []
        for match in _TOKEN.finditer(sql):
            token = match.group()
            if token.startswith(("--", "/*")):
                # Be conservative: forbidden words outside literals are rejected,
                # including in comments, and executable comments are never allowed.
                words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", token.upper()))
                if token.startswith("/*!") or words & _FORBIDDEN:
                    raise ReadOnlyViolation("Forbidden keyword or executable SQL comment")
                continue
            if token.startswith(("'", '"')):
                if len(token) < 2:
                    raise ReadOnlyViolation("Unterminated quoted literal")
                tokens.append("<quoted>")
                continue
            if token in {"/", "*"} and sql[match.start():].startswith(("/*", "*/")):
                raise ReadOnlyViolation("Unterminated or unexpected SQL comment")
            upper = token.upper()
            if upper in _FORBIDDEN:
                raise ReadOnlyViolation(f"Forbidden SQL keyword: {upper}")
            tokens.append(upper)
        if not tokens or tokens[0] not in _ALLOWED:
            raise ReadOnlyViolation("Only SELECT, SHOW, DESCRIBE, EXPLAIN or WITH are allowed")
        if ";" in tokens[:-1] or tokens.count(";") > 1:
            raise ReadOnlyViolation("Multiple SQL statements are not allowed")
        return True

    def query(self, sql: str, limit: int = 1000, *, fixture: str = "example",
              _sample_filter: Callable[[list], bool] | None = None) -> list[list]:
        """Validate, fetch rows and trace once; never fall back silently from live."""
        started = time.perf_counter()
        status = "error"
        rows = []
        try:
            self.dry_run(sql)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("limit must be a positive integer")
            if self.mode == "sample":
                sample_rows = self._sample_rows(fixture)
                # Internal hook for get_ports: filter before limiting and tracing,
                # without implementing a SQL interpreter for fixture data.
                if _sample_filter is not None:
                    sample_rows = [row for row in sample_rows if _sample_filter(row)]
                rows = sample_rows[:limit]
            else:
                rows = self._live_rows(sql, limit)
            status = "ok"
            return rows
        except ReadOnlyViolation:
            status = "rejected"
            raise
        finally:
            self._trace(sql, len(rows), started, status)

    def get_ports(self, zone_name: str | None = None, mrgid: int | None = None,
                  limit: int = 500) -> list[dict]:
        """Read typed PoE records through Zetaris (or the real-seed sample fixture).

        The SQL REST endpoint has no bind parameters. Text filters are encoded as
        UTF-8 hex literals and decoded by Spark SQL, never interpolated as SQL text.
        Numeric filters accept only integers. Both filters are combined with AND.
        """
        if zone_name is not None and not isinstance(zone_name, str):
            raise ValueError("zone_name must be a string or None")
        if mrgid is not None and (isinstance(mrgid, bool) or not isinstance(mrgid, int)):
            raise ValueError("mrgid must be an integer or None")
        conditions = []
        if zone_name is not None:
            encoded = zone_name.encode("utf-8").hex()
            conditions.append(f"zone_name = decode(unhex('{encoded}'), 'UTF-8')")
        if mrgid is not None:
            conditions.append(f"mrgid = {mrgid:d}")
        sql = _PORT_SELECT
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        def matches(row: list) -> bool:
            port = self._port_dict(row)
            return ((zone_name is None or port["zone_name"] == zone_name)
                    and (mrgid is None or port["mrgid"] == mrgid))

        rows = self.query(sql, limit=limit, fixture="poe_ports", _sample_filter=matches)
        return [self._port_dict(row) for row in rows]

    @staticmethod
    def _port_dict(row: list) -> dict:
        """Normalize the string-valued REST cells, without coercing false to true."""
        if len(row) != len(_PORT_COLUMNS):
            raise ZetarisUnavailable("PoE response has an unexpected number of columns")
        port = dict(zip(_PORT_COLUMNS, row))
        try:
            for field in ("lat", "lon", "validated", "geocode_source", "zone_name", "mrgid"):
                if port[field] == "null":
                    port[field] = None
            if not isinstance(port["name"], str) or not isinstance(port["id"], str):
                raise ValueError("Invalid identity")
            for field, bound in (("lat", 90), ("lon", 180)):
                if port[field] is not None:
                    if isinstance(port[field], bool):
                        raise ValueError("Invalid coordinate")
                    port[field] = float(port[field])
                    if not math.isfinite(port[field]) or abs(port[field]) > bound:
                        raise ValueError("Invalid coordinate")
            urls = port["source_urls"]
            if isinstance(urls, str):
                urls = json.loads(urls)
            if urls is None:
                urls = []
            if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
                raise ValueError("Invalid source URLs")
            port["source_urls"] = list(urls)
            validated = port["validated"]
            if isinstance(validated, str) and validated.lower() in {"true", "false"}:
                validated = validated.lower() == "true"
            if validated is not None and not isinstance(validated, bool):
                raise ValueError("Invalid validation status")
            port["validated"] = validated
            if port["mrgid"] is not None:
                if isinstance(port["mrgid"], bool) or not re.fullmatch(r"-?\d+", str(port["mrgid"])):
                    raise ValueError("Invalid MRGID")
                port["mrgid"] = int(port["mrgid"])
            for field in ("geocode_source", "zone_name"):
                if port[field] is not None and not isinstance(port[field], str):
                    raise ValueError("Invalid text field")
        except (ValueError, TypeError, OverflowError):
            raise ZetarisUnavailable("PoE response contains invalid field values") from None
        return port

    def get_registry_ports(self, country_iso2: str | None = None, bbox: tuple | None = None,
                           source: str = "unlocode", limit: int = 5000) -> list[dict]:
        """Read independent registry ports (UN/LOCODE or WPI) through Zetaris.

        country_iso2: two letters. bbox: (min_lat, min_lon, max_lat, max_lon), finite and
        ordered (no antimeridian wrap). Both are validated before entering SQL; the country
        literal can only be two ASCII letters and the bounds are formatted as decimals.
        """
        if source not in _REGISTRIES:
            raise ValueError("source must be 'unlocode' or 'wpi'")
        view, columns = _REGISTRIES[source]
        conditions = []
        if country_iso2 is not None:
            if not isinstance(country_iso2, str) or not re.fullmatch(r"[A-Za-z]{2}", country_iso2):
                raise ValueError("country_iso2 must be two letters")
            country_iso2 = country_iso2.upper()
            conditions.append(f"country_iso2 = '{country_iso2}'")
        if bbox is not None:
            bbox = _valid_bbox(bbox)
            min_lat, min_lon, max_lat, max_lon = bbox
            conditions.append(f"lat BETWEEN {min_lat:.6f} AND {max_lat:.6f} "
                              f"AND lon BETWEEN {min_lon:.6f} AND {max_lon:.6f}")
        sql = f"SELECT {', '.join(columns)} FROM {view}"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        def matches(row: list) -> bool:
            entry = self._registry_dict(columns, row)
            if country_iso2 is not None and entry["country_iso2"] != country_iso2:
                return False
            if bbox is not None:
                if entry["lat"] is None or entry["lon"] is None:
                    return False
                return bbox[0] <= entry["lat"] <= bbox[2] and bbox[1] <= entry["lon"] <= bbox[3]
            return True

        rows = self.query(sql, limit=limit, fixture=f"{source}_ports", _sample_filter=matches)
        return [dict(self._registry_dict(columns, row), source=source) for row in rows]

    @staticmethod
    def _registry_dict(columns: tuple[str, ...], row: list) -> dict:
        """String REST cells -> typed dict; 'null' / '' become None, lat/lon floats."""
        if len(row) != len(columns):
            raise ZetarisUnavailable("Registry response has an unexpected number of columns")
        entry = {}
        for column, cell in zip(columns, row):
            value = None if cell is None or str(cell).strip() in ("", "null") else cell
            if column in ("lat", "lon") and value is not None:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise ZetarisUnavailable("Registry response contains an invalid coordinate") from None
                if not math.isfinite(value) or abs(value) > (90 if column == "lat" else 180):
                    raise ZetarisUnavailable("Registry response contains an invalid coordinate")
            elif value is not None:
                value = str(value)
            entry[column] = value
        return entry

    def trace_event(self, action: str, status: str = "ok", **fields) -> None:
        """Write a ZetarisAdapter event (e.g. a registry check) to this run's trace."""
        self._write_trace({
            "timestamp": datetime.now(timezone.utc).isoformat(), "agent_name": "ZetarisAdapter",
            "run_id": self.run_id, "action": action, "mode": self.mode, "status": status, **fields,
        })

    def test_connection(self) -> dict:
        """Probe SQL execution and database discovery; return a safe status report."""
        try:
            probe = self.query("SELECT 1", limit=1, fixture="connection")
            # Accept numeric REST cells and their string representations, but
            # do not mistake booleans, malformed shapes or rounded values for 1.
            valid_probe = False
            if len(probe) == 1 and len(probe[0]) == 1 and not isinstance(probe[0][0], bool):
                try:
                    valid_probe = Decimal(str(probe[0][0])) == Decimal(1)
                except InvalidOperation:
                    pass
            if not valid_probe:
                raise ZetarisUnavailable("Zetaris SELECT 1 returned an unexpected result")
            rows = self.query("SHOW LIGHTNING DATABASES", fixture="databases")
            if any(not row or not isinstance(row[0], str) for row in rows):
                raise ZetarisUnavailable("Zetaris returned invalid database names")
            return {"status": "ok", "databases": [row[0] for row in rows], "mode": self.mode}
        except ZetarisUnavailable as exc:
            return {"status": "unavailable", "databases": [], "mode": self.mode, "message": str(exc)}

    @staticmethod
    def _rows(payload: object) -> list[list]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ZetarisUnavailable("Zetaris response must contain a data array")
        rows = payload["data"]
        if any(not isinstance(row, list) for row in rows):
            raise ZetarisUnavailable("Zetaris data must contain rows as arrays")
        return rows

    def _sample_rows(self, fixture: str) -> list[list]:
        if not isinstance(fixture, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", fixture):
            raise ValueError("fixture must be a simple name without a path or extension")
        try:
            with (SAMPLE_DIR / f"{fixture}.json").open(encoding="utf-8") as source:
                payload = json.load(source)
        except (OSError, ValueError):
            raise ZetarisUnavailable("Zetaris sample fixture is missing or invalid") from None
        return self._rows(payload)

    def _live_rows(self, sql: str, limit: int) -> list[list]:
        if not self._org_id:
            raise ZetarisUnavailable("ZETARIS_ORG_ID is required in live mode")
        failure = "connection or timeout error"
        for attempt in range(3):
            try:
                response = httpx.post(
                    f"{self._base_url}/sql-editor/sqls/run",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "X-Org-ID": self._org_id,
                        "X-Request-ID": str(uuid4()),
                        "Content-Type": "application/json",
                    },
                    json={"queryId": str(uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit},
                    timeout=15.0,
                )
            except httpx.RequestError:
                failure = "connection or timeout error"
            else:
                if 500 <= response.status_code < 600:
                    failure = f"HTTP {response.status_code}"
                elif not 200 <= response.status_code < 300:
                    raise ZetarisUnavailable(f"Zetaris rejected the query (HTTP {response.status_code})")
                else:
                    try:
                        payload = response.json()
                    except ValueError:
                        raise ZetarisUnavailable("Zetaris returned invalid JSON") from None
                    return self._rows(payload)
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
        raise ZetarisUnavailable(f"Zetaris unavailable after 3 attempts: {failure}") from None

    def _trace(self, sql: str, row_count: int, started: float, status: str) -> None:
        summary = sql if isinstance(sql, str) else "<non-string SQL>"
        if self._api_key:
            summary = summary.replace(self._api_key, "[REDACTED]")
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_name": "ZetarisAdapter",
            "run_id": self.run_id,
            "action": "zetaris_query",
            "input_summary": {"sql": summary[:200]},
            "row_count": row_count,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "mode": self.mode,
            "status": status,
        }
        self._write_trace(entry)

    def _write_trace(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # Redact the complete serialized entry too (e.g. a caller-supplied run ID).
        if self._api_key:
            line = line.replace(self._api_key, "[REDACTED]")
        path = Path(os.environ.get("TRACE_LOG_PATH") or
                    DEFAULT_TRACE_LOG_DIR / f"trace-{self.run_id}.jsonl")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as target:
                target.write(line)
        except OSError:
            raise ZetarisUnavailable("Could not write the Zetaris query trace") from None
        if os.environ.get("TRACE_STDOUT", "1") != "0":
            print(line, end="", flush=True)
