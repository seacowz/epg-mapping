# Bay Area OTA EPG Mapping for Jellyfin

This repository builds an XMLTV guide for over-the-air television in
the San Francisco Bay Area and San Jose. It is designed for a Jellyfin server
using an HDHomeRun tuner.

The HDHomeRun supplies the live channels and television streams directly to
Jellyfin. Guide listings come separately from the free United States XMLTV
feed published by [IPTV-EPG.org](https://iptv-epg.org/guides). The filter in
this repository downloads that large US feed, keeps the selected channel IDs,
applies any required schedule offsets, then creates a compact XMLTV file for
Jellyfin.

```text
HDHomeRun ── live channels/video ────────────────> Jellyfin

IPTV-EPG U.S. XMLTV feed
        │
        v
  filter_epg.py + epg-mappings.csv
        │
        v
  epg-candidates.xml ── guide data ─────────────> Jellyfin
```

## Repository files

### `epg-mappings.csv`

This is the source of truth for channel-to-guide mappings. Each row associates
an HDHomeRun virtual channel number with an XMLTV channel ID.

| Column | Purpose |
| --- | --- |
| `GuideNumber` | HDHomeRun virtual channel number, such as `5.2` |
| `GuideName` | Channel name reported by the tuner |
| `EpgId` | XMLTV channel ID in the IPTV-EPG source feed |
| `EpgName` | Display name for that XMLTV channel |
| `Approved` | `YES` marks the known-good mapping for a tuner channel |
| `TimeShiftHours` | Hours added to the source schedule; `0` means no shift |
| `Notes` | Verification details and other mapping context |

For a channel with one approved row, the filter includes only that row's
`EpgId`. If a channel has no approved row, every candidate for that channel is
retained in the generated XMLTV file so it can be evaluated in Jellyfin.

There must not be two different approved `EpgId` values for the same
`GuideNumber`. The filter treats that as an error rather than choosing one
silently.

`TimeShiftHours` changes the timestamps written to the filtered file. For
example, `3` moves every program on that XMLTV channel three hours later. Use a
shift only after comparing the source listing with the channel being received.

### `filter_epg.py`

This is the production guide generator. It:

1. Reads `epg-mappings.csv`.
2. Downloads the compressed U.S. XMLTV source feed.
3. Streams through the source without loading the full file into memory.
4. Retains approved mappings and all candidates for unresolved channels.
5. Applies per-channel schedule shifts from `TimeShiftHours`.
6. Atomically replaces the generated `epg-candidates.xml` file.

The default source is:

```text
https://iptv-epg.org/files/epg-us.xml.gz
```

The available country feeds and their current update status are listed on the
[IPTV-EPG guides page](https://iptv-epg.org/guides). This repository does not
redistribute the original guide feed; the script downloads it directly from
IPTV-EPG.org when it runs.

### `download_hdhomerun_epg.py`

This optional diagnostic utility reads `discover.json` and `lineup.json` from
an HDHomeRun, obtains its `DeviceAuth` value, and attempts to download XMLTV
data from SiliconDust's cloud API. It can also save the tuner's lineup JSON.

It is **not used by the normal Jellyfin guide workflow or the cron job below**.
SiliconDust may return HTTP 401 or 403 when its guide/DVR service is not active.
The production guide in this repository comes from IPTV-EPG.org instead.

Example diagnostic command:

```bash
python3 download_hdhomerun_epg.py \
  --device 192.168.0.200 \
  --output hdhomerun-guide.xml \
  --lineup-output hdhomerun-lineup.json
```

## Requirements

- Python 3.10 or newer
- Network access to `https://iptv-epg.org`
- Enough temporary storage for the compressed source stream and generated XML
- A Jellyfin server that can read the generated file

Both Python programs use only the Python standard library.

## Installation on TrueNAS

The examples below use the directory already used by this setup:

```text
/mnt/data/apps/jellyfin/config/epg
```

Clone the repository into that directory:

```bash
cd /mnt/data/apps/jellyfin/config
git clone https://github.com/seacowz/epg-mapping.git epg
cd epg
```

If the directory already exists and is already a clone, update it manually
with:

```bash
cd /mnt/data/apps/jellyfin/config/epg
git pull --ff-only
```

Repository updates are intentionally separate from guide refreshes. The cron
job downloads fresh guide data but does not automatically change the scripts
or approved mappings.

## Generate the guide manually

Run the filter once before configuring Jellyfin:

```bash
/usr/bin/python3 /mnt/data/apps/jellyfin/config/epg/filter_epg.py \
  --mappings /mnt/data/apps/jellyfin/config/epg/epg-mappings.csv \
  --output /mnt/data/apps/jellyfin/config/epg/epg-candidates.xml
```

Successful output reports the number of selected XMLTV IDs, approved and
unresolved tuner channels, schedule shifts, and the final channel/program
counts. With the current Bay Area mappings, the generated XMLTV file is
typically around 12 MB, compared with roughly 900 MB for the uncompressed
original U.S. XMLTV feed. Exact sizes vary as mappings and upstream guide data
change.

Confirm that the generated file exists and is readable:

```bash
ls -lh /mnt/data/apps/jellyfin/config/epg/epg-candidates.xml
grep -c '<channel id=' /mnt/data/apps/jellyfin/config/epg/epg-candidates.xml
grep -c '<programme ' /mnt/data/apps/jellyfin/config/epg/epg-candidates.xml
```

## Schedule automatic updates with cron

On TrueNAS SCALE, the preferred persistent setup is **System Settings** >
**Advanced Settings** > **Cron Jobs** > **Add**. Use:

- **Description:** `Update Jellyfin EPG`
- **Run As User:** `root`
- **Schedule:** daily at `03:15`
- **Command:**

```bash
/usr/bin/flock -n /mnt/data/apps/jellyfin/config/epg/filter.lock /usr/bin/nice -n 10 /usr/bin/python3 /mnt/data/apps/jellyfin/config/epg/filter_epg.py --mappings /mnt/data/apps/jellyfin/config/epg/epg-mappings.csv --output /mnt/data/apps/jellyfin/config/epg/epg-candidates.xml >> /mnt/data/apps/jellyfin/config/epg/filter.log 2>&1
```

See the official [TrueNAS cron-job documentation](https://www.truenas.com/docs/scale/systemsettings/advanced/managecronjobs/)
for the available scheduler fields.

Alternatively, edit root's crontab from the shell:

```bash
crontab -e
```

Add this single line to rebuild the guide every day at 3:15 AM:

```cron
15 3 * * * /usr/bin/flock -n /mnt/data/apps/jellyfin/config/epg/filter.lock /usr/bin/nice -n 10 /usr/bin/python3 /mnt/data/apps/jellyfin/config/epg/filter_epg.py --mappings /mnt/data/apps/jellyfin/config/epg/epg-mappings.csv --output /mnt/data/apps/jellyfin/config/epg/epg-candidates.xml >> /mnt/data/apps/jellyfin/config/epg/filter.log 2>&1
```

The command uses:

- `flock` to prevent overlapping runs.
- `nice` to lower the filter's CPU scheduling priority.
- `filter.log` to retain normal output and errors.

Cron uses the TrueNAS host's local time. Check the configured time zone if the
job runs at an unexpected hour.

To review the latest run:

```bash
tail -100 /mnt/data/apps/jellyfin/config/epg/filter.log
```

## Configure Jellyfin

Jellyfin supports HDHomeRun tuner devices and XMLTV guide providers. Its
official [Live TV setup guide](https://jellyfin.org/docs/general/server/live-tv/setup-guide/)
also notes that guide channels must be mapped to their corresponding tuner
channels.

### 1. Add the HDHomeRun tuner

1. Open the Jellyfin administration dashboard.
2. Go to **Live TV**.
3. Under **Tuner Devices**, add or detect the HDHomeRun.
4. If adding it manually, select **HDHomeRun** and use a URL such as
   `http://192.168.0.200`.
5. Save the tuner.

This tuner entry supplies the actual live television streams. Do not replace
it with the XMLTV file.

### 2. Add the generated XMLTV guide

1. Still under **Live TV**, select **Add Provider** under
   **TV Guide Data Providers**.
2. Select **XMLTV**.
3. Enter the path to `epg-candidates.xml` as seen from inside the Jellyfin
   application/container.
4. Save the provider and refresh guide data.

For the TrueNAS layout in this README, the host file is:

```text
/mnt/data/apps/jellyfin/config/epg/epg-candidates.xml
```

If `/mnt/data/apps/jellyfin/config` is mounted inside the Jellyfin container as
`/config`, the path entered in Jellyfin is normally:

```text
/config/epg/epg-candidates.xml
```

The correct value is the **container-visible path**, which may differ if the
TrueNAS application uses a different mount point.

### 3. Map guide channels

1. Open the menu for the XMLTV provider.
2. Select **Map Channels**.
3. For each HDHomeRun channel, select the matching XMLTV entry.
4. Exit the mapping screen to save the selections.
5. Select **Refresh Guide Data**.
6. Open Jellyfin's **Live TV Guide** and verify current and upcoming programs.

Jellyfin stores these channel selections in its own configuration/database;
they are not written back into `epg-mappings.csv`. Keeping the XMLTV `EpgId`
stable helps Jellyfin retain the mappings across daily guide refreshes.

## Changing or correcting a mapping

1. Edit the appropriate row in `epg-mappings.csv`.
2. Make sure only one `EpgId` is approved for that `GuideNumber`.
3. Set `Approved` to `YES` after the schedule has been verified.
4. Set `TimeShiftHours` only when the correct feed needs a consistent offset.
5. Run `filter_epg.py` manually.
6. Refresh guide data in Jellyfin.
7. Revisit **Map Channels** only if the selected `EpgId` changed or the channel
   is not currently mapped.

## Contributing mappings

Contributions to `epg-mappings.csv` are welcome through GitHub pull requests.
Contributions should remain focused on Bay Area and San Jose OTA reception,
such as filling a local channel gap, correcting an outdated affiliation, or
replacing an approximate guide with a better-matching feed.

To contribute:

1. Fork this repository and create a branch for the mapping change.
2. Edit `epg-mappings.csv` while preserving its header and column order.
3. Verify the proposed guide against live programming for more than one time
   slot whenever possible.
4. Mark `Approved` as `YES` only when the mapping has been confirmed.
5. Ensure there is no other approved `EpgId` for the same `GuideNumber`.
6. Use `TimeShiftHours` only for a consistent, verified schedule offset.
7. Explain the station, market, verification, and any time shift in `Notes`.
8. Open a pull request describing what changed and how it was verified.

If a candidate has not yet been verified, it can be added without `YES` in the
`Approved` column. The filter will retain all unapproved candidates for that
channel, allowing them to be compared before one is promoted to known-good.

## Generated files

These runtime files are not source files and should not normally be committed:

| File | Purpose |
| --- | --- |
| `epg-candidates.xml` | Filtered XMLTV file consumed by Jellyfin |
| `epg-candidates.xml.tmp` | Temporary file used during an active filter run |
| `filter.log` | Cron output and errors |
| `filter.lock` | Lock file used by `flock` |

## Troubleshooting

- **Jellyfin cannot see the XMLTV file:** verify the path from inside the
  Jellyfin container and confirm the file is readable by the Jellyfin process.
- **A guide channel is missing from Map Channels:** confirm its `EpgId` is in
  `epg-candidates.xml`, then refresh guide data in Jellyfin.
- **A schedule is consistently early or late:** verify the correct source feed
  first, then adjust `TimeShiftHours` and regenerate the XMLTV file.
- **The filter reports multiple approved IDs:** remove `YES` from all but the
  verified row for that `GuideNumber`.
- **The cron job appears not to run:** check `filter.log`, the host time zone,
  file permissions, and `command -v flock python3 nice`.
- **The HDHomeRun diagnostic utility returns 401/403:** that SiliconDust cloud
  XMLTV endpoint may require its guide/DVR service. This does not affect the
  IPTV-EPG-based production filter.

## Data source and attribution

The original U.S. XMLTV guide is provided by
[IPTV-EPG.org](https://iptv-epg.org/guides) and downloaded from:

```text
https://iptv-epg.org/files/epg-us.xml.gz
```

Channel availability, identifiers, schedules, and update frequency are
controlled by the upstream provider and may change. The mapping CSV in this
repository is a locally verified selection for Bay Area OTA reception and may
require future maintenance as stations change affiliations or subchannels.

IPTV-EPG provides its guide feeds as a free service and accepts donations to
help support their continued operation. If this project is useful to you,
consider using the donation option available through
[IPTV-EPG.org](https://iptv-epg.org/guides).
