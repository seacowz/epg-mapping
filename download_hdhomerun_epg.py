#!/usr/bin/env python3
"""Download XMLTV guide data for an HDHomeRun device.

The script obtains a fresh DeviceAuth token from the tuner, then uses that
token to request XMLTV data from SiliconDust. It uses only Python's standard
library and writes the output atomically.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


USER_AGENT = "HDHomeRun-XMLTV-Downloader/1.0"
CHUNK_SIZE = 1024 * 1024


class DownloadError(RuntimeError):
    pass


def normalize_device(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise argparse.ArgumentTypeError("device address cannot be empty")
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError(f"invalid device address: {value}")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def request(url: str, timeout: float):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
        },
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise DownloadError(
                f"access denied (HTTP {exc.code}). The HDHomeRun XMLTV service "
                "may require an active SiliconDust guide/DVR subscription."
            ) from exc
        raise DownloadError(f"HTTP {exc.code} while requesting {url}") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"unable to reach {url}: {exc.reason}") from exc


def get_json(url: str, timeout: float) -> dict | list:
    with request(url, timeout) as response:
        try:
            return json.load(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DownloadError(f"invalid JSON returned by {url}") from exc


def copy_response_to_file(url: str, destination: Path, timeout: float) -> bool:
    with request(url, timeout) as response, destination.open("wb") as output:
        content_encoding = response.headers.get("Content-Encoding", "").lower()
        shutil.copyfileobj(response, output, length=CHUNK_SIZE)
        output.flush()
        os.fsync(output.fileno())
    return "gzip" in content_encoding


def decompress_file(source: Path, destination: Path) -> None:
    with gzip.open(source, "rb") as compressed, destination.open("wb") as output:
        shutil.copyfileobj(compressed, output, length=CHUNK_SIZE)
        output.flush()
        os.fsync(output.fileno())


def inspect_xmltv(path: Path) -> tuple[int, int]:
    channels = 0
    programmes = 0
    root_checked = False
    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            if event == "start" and not root_checked:
                if element.tag != "tv":
                    raise DownloadError("downloaded XML is not an XMLTV document")
                root_checked = True
            elif event == "end":
                if element.tag == "channel":
                    channels += 1
                elif element.tag == "programme":
                    programmes += 1
                element.clear()
    except ET.ParseError as exc:
        raise DownloadError(f"downloaded guide is invalid XML: {exc}") from exc
    if not root_checked:
        raise DownloadError("downloaded XMLTV document is empty")
    return channels, programmes


def write_json_atomic(data: object, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def download_guide(device: str, output: Path, lineup_output: Path | None, timeout: float) -> None:
    discover_url = device + "/discover.json"
    lineup_url = device + "/lineup.json"

    discover = get_json(discover_url, timeout)
    if not isinstance(discover, dict):
        raise DownloadError("discover.json did not return an object")

    device_auth = str(discover.get("DeviceAuth", "")).strip()
    if not device_auth:
        raise DownloadError("discover.json did not contain a DeviceAuth token")

    lineup = get_json(lineup_url, timeout)
    if not isinstance(lineup, list):
        raise DownloadError("lineup.json did not return a channel list")
    if lineup_output is not None:
        write_json_atomic(lineup, lineup_output)

    query = urllib.parse.urlencode({"DeviceAuth": device_auth})
    guide_url = "https://api.hdhomerun.com/api/xmltv?" + query

    output.parent.mkdir(parents=True, exist_ok=True)
    raw_fd, raw_name = tempfile.mkstemp(
        prefix=output.name + ".download.", suffix=".tmp", dir=output.parent
    )
    os.close(raw_fd)
    xml_fd, xml_name = tempfile.mkstemp(
        prefix=output.name + ".xml.", suffix=".tmp", dir=output.parent
    )
    os.close(xml_fd)
    raw_path = Path(raw_name)
    xml_path = Path(xml_name)

    try:
        header_says_gzip = copy_response_to_file(guide_url, raw_path, timeout)
        with raw_path.open("rb") as source:
            gzip_magic = source.read(2) == b"\x1f\x8b"

        if header_says_gzip or gzip_magic:
            decompress_file(raw_path, xml_path)
        else:
            os.replace(raw_path, xml_path)

        channels, programmes = inspect_xmltv(xml_path)
        if channels == 0:
            raise DownloadError("XMLTV response contained no channels")

        os.replace(xml_path, output)
        device_id = discover.get("DeviceID", "unknown")
        print(f"Device: {device_id}")
        print(f"Lineup channels: {len(lineup)}")
        print(f"XMLTV channels: {channels}")
        print(f"Programs: {programmes}")
        print(f"Saved: {output}")
    finally:
        raw_path.unlink(missing_ok=True)
        xml_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download XMLTV guide data using an HDHomeRun device authorization token."
    )
    parser.add_argument(
        "--device",
        type=normalize_device,
        default="http://192.168.0.200",
        help="HDHomeRun IP address or base URL (default: 192.168.0.200)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("hdhomerun-guide.xml"),
        help="destination XMLTV file (default: hdhomerun-guide.xml)",
    )
    parser.add_argument(
        "--lineup-output",
        type=Path,
        help="optionally save the device lineup JSON to this file",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="HTTP timeout in seconds (default: 120)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        download_guide(
            args.device,
            args.output.expanduser().resolve(),
            args.lineup_output.expanduser().resolve() if args.lineup_output else None,
            args.timeout,
        )
    except (DownloadError, OSError, gzip.BadGzipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
