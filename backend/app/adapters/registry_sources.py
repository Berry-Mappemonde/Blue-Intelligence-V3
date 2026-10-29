"""Independent port registries for Zetaris: UN/LOCODE (UNECE) and the World Port Index (NGA).

Both are third-party references, not Blue Intelligence output, so a port confirmed
against them is not circular evidence.

UN/LOCODE — UNECE, https://unece.org/trade/cefact/unlocode-code-list-country-and-territory
  Download: https://unlocode.unece.org/downloads/unlocode-latest.zip (UNECE's pre-release
  build, includes codes approved since the last biannual issue; the numbered releases at
  service.unece.org refuse scripted downloads). Files: release/csv/UNLOCODE CodeListPart{1,2,3}.csv,
  UTF-8, no header, columns: Change, Country, Location, Name, NameWoDiacritics, Subdivision,
  Function, Status, Date, IATA, Coordinates, Remarks. (The datahub.io/core/un-locode mirror
  orders Status before Function.) UN/LOCODE is published by UNECE for free use; datahub's own
  scripts are ODC-PDDL-1.0 and it notes the code list's licence is not formally stated.
  We keep entries whose Function has "1" in position 1 (port).

World Port Index — NGA Pub 150, https://msi.nga.mil/Publications/WPI
  Download: https://msi.nga.mil/api/publications/download?type=view&key=16920959/SFH00000/UpdatedPub150.csv
  US Government work (NGA), public domain. A JSON API with ISO2 codes also exists
  (https://msi.nga.mil/api/publications/world-port-index?output=json) but lists fewer ports.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter

UNLOCODE_URL = "https://unlocode.unece.org/downloads/unlocode-latest.zip"
WPI_URL = "https://msi.nga.mil/api/publications/download?type=view&key=16920959/SFH00000/UpdatedPub150.csv"

UNLOCODE_COLUMNS = ("country_iso2", "location", "code", "name", "name_ascii", "subdivision",
                    "function_code", "status", "lat", "lon")
WPI_COLUMNS = ("wpi_number", "name", "alternate_name", "country_name", "country_iso2", "unlocode",
               "lat", "lon", "first_port_of_entry", "quarantine_pratique", "quarantine_sanitation",
               "quarantine_other", "harbor_use")

_COORD = re.compile(r"^\s*(\d{2})(\d{2})([NS])\s+(\d{3})(\d{2})([EW])\s*$")
# Country names in Pub 150 that neither UN/LOCODE prefixes nor pycountry resolve.
_WPI_COUNTRY_OVERRIDES = {
    "Burma": "MM", "Turkey": "TR", "Korea, South": "KR", "Korea, North": "KP", "Russia": "RU",
    "Iran": "IR", "Syria": "SY", "Laos": "LA", "Vietnam": "VN", "Taiwan": "TW", "Macau": "MO",
    "Federated States of Micronesia": "FM", "Virgin Islands (British)": "VG", "Brunei": "BN",
    "Cabo Verde": "CV", "Cape Verde": "CV", "Cote d'Ivoire": "CI", "Curacao": "CW",
    "Falkland Islands (Islas Malvinas)": "FK", "Saint Helena": "SH", "Tanzania": "TZ",
    "Venezuela": "VE", "Bolivia": "BO", "Moldova": "MD", "Congo (Kinshasa)": "CD",
    "Congo (Brazzaville)": "CG", "Johnson Atoll": "UM", "Wake Island": "UM", "Midway Islands": "UM",
}


def parse_unlocode_coordinates(value: str | None) -> tuple[float, float] | None:
    """'3223N 06441W' (DDMMN DDDMMW) -> (32.3833, -64.6833); None if absent or malformed."""
    match = _COORD.match(value or "")
    if not match:
        return None
    lat_d, lat_m, ns, lon_d, lon_m, ew = match.groups()
    lat_m, lon_m = int(lat_m), int(lon_m)
    if lat_m >= 60 or lon_m >= 60:
        return None
    lat = int(lat_d) + lat_m / 60
    lon = int(lon_d) + lon_m / 60
    if lat > 90 or lon > 180:
        return None
    return round(-lat if ns == "S" else lat, 6), round(-lon if ew == "W" else lon, 6)


def unlocode_ports(lines) -> list[dict]:
    """Port entries (Function position 1 == '1') from UNECE's CSV parts, as UNLOCODE_COLUMNS dicts."""
    out = []
    for row in csv.reader(lines):
        if len(row) < 11 or not row[2].strip() or not row[6].startswith("1"):
            continue  # country header rows have no location code
        coords = parse_unlocode_coordinates(row[10])
        out.append({
            "country_iso2": row[1].strip().upper(), "location": row[2].strip().upper(),
            "code": (row[1] + row[2]).strip().upper(), "name": row[3].strip(),
            "name_ascii": row[4].strip(), "subdivision": row[5].strip() or None,
            "function_code": row[6].strip(), "status": row[7].strip() or None,
            "lat": coords[0] if coords else None, "lon": coords[1] if coords else None,
        })
    return out


def _clean(value) -> str | None:
    text = str(value or "").strip()
    return text if text and text.lower() not in ("nan", "none", "unknown") else None


def _float(value) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return round(number, 6)


def wpi_ports(text: str) -> list[dict]:
    """Pub 150 CSV text -> WPI_COLUMNS dicts, with ISO2 from UN/LOCODE or the country name."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    prefix_votes: dict[str, Counter] = {}
    for row in rows:
        code = re.sub(r"\s+", "", row.get("UN/LOCODE") or "").upper()
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}", code):
            prefix_votes.setdefault(row["Country Code"].strip(), Counter())[code[:2]] += 1

    def iso2(name: str, code: str) -> str | None:
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}", code):
            return code[:2]
        if name in prefix_votes:
            return prefix_votes[name].most_common(1)[0][0]
        if name in _WPI_COUNTRY_OVERRIDES:
            return _WPI_COUNTRY_OVERRIDES[name]
        try:
            import pycountry
            return pycountry.countries.lookup(name).alpha_2
        except (ImportError, LookupError):
            return None

    out = []
    for row in rows:
        name = _clean(row.get("Main Port Name"))
        lat, lon = _float(row.get("Latitude")), _float(row.get("Longitude"))
        if not name:
            continue
        code = re.sub(r"\s+", "", row.get("UN/LOCODE") or "").upper()
        country = (row.get("Country Code") or "").strip()
        number = _float(row.get("World Port Index Number"))
        out.append({
            "wpi_number": str(int(number)) if number is not None else None,
            "name": name, "alternate_name": _clean(row.get("Alternate Port Name")),
            "country_name": country or None, "country_iso2": iso2(country, code),
            "unlocode": code if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}", code) else None,
            "lat": lat, "lon": lon,
            "first_port_of_entry": _clean(row.get("First Port of Entry")),
            "quarantine_pratique": _clean(row.get("Quarantine - Pratique")),
            "quarantine_sanitation": _clean(row.get("Quarantine - Sanitation")),
            "quarantine_other": _clean(row.get("Quarantine - Other")),
            "harbor_use": _clean(row.get("Harbor Use")),
        })
    return out


def write_csv(rows: list[dict], columns: tuple[str, ...], path) -> int:
    """Header + rows, empty cells for None; returns the row count."""
    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in columns})
    return len(rows)


# Committed SAMPLE_MODE subset: the countries of the 20 sample zones and input_examples/.
# Large countries are cut to the sample zone (Alaska for the US, the Azores for Portugal).
FIXTURE_COUNTRIES = ("AL", "DZ", "AO", "AI", "AG", "AR", "SH", "PT", "AU", "BH", "BS", "BD", "BB",
                     "BE", "BZ", "BM", "BJ", "BA", "VG", "US")
_FIXTURE_REGIONS = {  # country -> (UN/LOCODE subdivision, (min_lat, min_lon, max_lat, max_lon))
    "US": ("AK", (51.0, -180.0, 72.0, -129.0)),
    "PT": ("20", (36.5, -32.0, 40.0, -24.5)),
}


def in_fixture_scope(row: dict, source: str) -> bool:
    country = row.get("country_iso2")
    if country not in FIXTURE_COUNTRIES:
        return False
    if country not in _FIXTURE_REGIONS:
        return True
    subdivision, (min_lat, min_lon, max_lat, max_lon) = _FIXTURE_REGIONS[country]
    if source == "unlocode" and row.get("subdivision") == subdivision:
        return True
    lat, lon = row.get("lat"), row.get("lon")
    return lat is not None and lon is not None and min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def write_fixture(rows: list[dict], columns: tuple[str, ...], source: str, path, description: str) -> int:
    """Zetaris REST response shape (string cells, "null" for missing), filtered to the sample scope."""
    import json
    data = [["null" if row.get(c) is None else str(row[c]) for c in columns]
            for row in rows if in_fixture_scope(row, source)]
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"description": description, "columns": list(columns), "data": data},
                  stream, ensure_ascii=False, indent=0)
        stream.write("\n")
    return len(data)
