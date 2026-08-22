#!/usr/bin/env python3
"""Download a large XMLTV feed and retain IDs selected by one mapping CSV."""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_SOURCE = "https://iptv-epg.org/files/epg-us.xml.gz"
APPROVED_VALUES = {"yes", "y", "true", "1", "x"}
MAPPING_COLUMNS = (
    "GuideNumber",
    "GuideName",
    "EpgId",
    "EpgName",
    "Approved",
    "TimeShiftHours",
    "Notes",
)
REQUIRED_COLUMNS = set(MAPPING_COLUMNS)


def read_mappings(csv_path: Path) -> tuple[set[str], int, int, dict[str, float]]:
    """Select approved IDs and retain all alternatives for unresolved channels."""
    by_number: dict[str, list[dict[str, str]]] = {}
    ungrouped: set[str] = set()

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                f"Mapping CSV is missing required columns: {', '.join(sorted(missing))}"
            )
        for row in reader:
            epg_id = row.get("EpgId", "").strip()
            guide_number = row.get("GuideNumber", "").strip()
            if not epg_id:
                continue
            if guide_number:
                by_number.setdefault(guide_number, []).append(row)
            else:
                ungrouped.add(epg_id)

    selected = set(ungrouped)
    shifts: dict[str, float] = {}
    approved_channels = 0
    unresolved_channels = 0

    for guide_number, rows in by_number.items():
        candidate_ids = {row.get("EpgId", "").strip() for row in rows}
        approved_rows = [
            row
            for row in rows
            if row.get("Approved", "").strip().lower() in APPROVED_VALUES
        ]
        approved_ids = {row.get("EpgId", "").strip() for row in approved_rows}

        if len(approved_ids) > 1:
            raise RuntimeError(
                f"Multiple approved EPG IDs for guide number {guide_number}: "
                f"{', '.join(sorted(approved_ids))}"
            )

        if approved_ids:
            epg_id = next(iter(approved_ids))
            selected.add(epg_id)
            approved_channels += 1

            raw_shifts = {
                row.get("TimeShiftHours", "").strip() or "0"
                for row in approved_rows
                if row.get("EpgId", "").strip() == epg_id
            }
            if len(raw_shifts) > 1:
                raise RuntimeError(
                    f"Conflicting TimeShiftHours for guide number {guide_number}: "
                    f"{', '.join(sorted(raw_shifts))}"
                )
            raw_shift = next(iter(raw_shifts), "0")
            try:
                shift = float(raw_shift)
            except ValueError as exc:
                raise RuntimeError(
                    f"Invalid TimeShiftHours for guide number {guide_number}: "
                    f"{raw_shift!r}"
                ) from exc

            previous_shift = shifts.get(epg_id)
            if previous_shift is not None and previous_shift != shift:
                raise RuntimeError(
                    f"Conflicting time shifts for EPG ID {epg_id}: "
                    f"{previous_shift}, {shift}"
                )
            if shift:
                shifts[epg_id] = shift
        else:
            selected.update(candidate_ids)
            unresolved_channels += 1

    if not selected:
        raise RuntimeError("No EPG IDs were selected from the mapping CSV")
    return selected, approved_channels, unresolved_channels, shifts


XMLTV_TIME = re.compile(r"^(\d{14}|\d{12}|\d{10}|\d{8})(.*)$")


def shift_xmltv_time(value: str, hours: float) -> str:
    """Shift an XMLTV timestamp while preserving its timezone suffix."""
    match = XMLTV_TIME.match(value)
    if not match:
        raise RuntimeError(f"Unsupported XMLTV timestamp: {value!r}")
    digits, suffix = match.groups()
    formats = {
        8: "%Y%m%d",
        10: "%Y%m%d%H",
        12: "%Y%m%d%H%M",
        14: "%Y%m%d%H%M%S",
    }
    shifted = datetime.strptime(digits, formats[len(digits)]) + timedelta(hours=hours)
    return shifted.strftime(formats[len(digits)]) + suffix


def escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def filter_feed(
    source_url: str,
    ids: set[str],
    output_path: Path,
    shifts: dict[str, float] | None = None,
) -> tuple[int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    channel_count = 0
    programme_count = 0
    programmes_scanned = 0
    shifts = shifts or {}

    request = urllib.request.Request(source_url, headers={"User-Agent": "epg-filter/2.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            with gzip.GzipFile(fileobj=response) as source:
                context = ET.iterparse(source, events=("start", "end"))
                _event, root = next(context)
                if root.tag != "tv":
                    raise RuntimeError("Downloaded file is not XMLTV: missing <tv> root")

                attributes = "".join(
                    f' {key}="{escape_attr(value)}"' for key, value in root.attrib.items()
                )
                with temporary.open("wb") as output:
                    output.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
                    output.write(f"<tv{attributes}>\n".encode("utf-8"))

                    for event, element in context:
                        if event != "end" or element.tag not in {"channel", "programme"}:
                            continue

                        epg_id = (
                            element.get("id")
                            if element.tag == "channel"
                            else element.get("channel")
                        )
                        if epg_id in ids:
                            if element.tag == "programme" and epg_id in shifts:
                                hours = shifts[epg_id]
                                for attribute in ("start", "stop"):
                                    value = element.get(attribute)
                                    if value:
                                        element.set(attribute, shift_xmltv_time(value, hours))
                            output.write(ET.tostring(element, encoding="utf-8"))
                            output.write(b"\n")
                            if element.tag == "channel":
                                channel_count += 1
                            else:
                                programme_count += 1

                        element.clear()
                        root.clear()
                        if element.tag == "programme":
                            programmes_scanned += 1
                            if programmes_scanned % 100000 == 0:
                                print(
                                    f"Scanned {programmes_scanned} programmes; "
                                    f"retained {programme_count}",
                                    flush=True,
                                )

                    output.write(b"</tv>\n")

        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return channel_count, programme_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Filter a large gzipped XMLTV feed using one seven-column mapping CSV."
    )
    parser.add_argument("--mappings", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    args = parser.parse_args()

    ids, approved, unresolved, shifts = read_mappings(args.mappings)
    print(f"Selected {len(ids)} unique EPG IDs", flush=True)
    print(
        f"Applied {approved} approved channel mappings; "
        f"kept all candidates for {unresolved} unresolved channels",
        flush=True,
    )
    if shifts:
        details = ", ".join(
            f"{epg_id}={hours:+g}h" for epg_id, hours in sorted(shifts.items())
        )
        print(f"Applying schedule shifts: {details}", flush=True)

    channels, programmes = filter_feed(args.source, ids, args.output, shifts)
    print(f"Wrote {channels} channels and {programmes} programmes to {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
