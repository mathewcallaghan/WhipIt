<p align="center">
  <img src="whipit/assets/whipit.svg" alt="WhipIt Icon" width="128" height="128">
</p>

# WhipIt

> **AccurateRip + MusicBrainz Archival CD Ripper & Opus Transcoder**

WhipIt is a native Linux desktop application built with PyQt6 that turns Audio CDs into bit-perfect, AccurateRip-verified FLAC masters alongside synchronized 192k Opus mirrors for mobile devices. It automates tagging via MusicBrainz, calculates EBU R128 ReplayGain, extracts/embeds artwork, and guarantees archival data integrity through atomic transaction staging.

---

## Table of Contents
- [Core Features](#core-features)
- [System Requirements & Dependencies](#system-requirements--dependencies)
- [Installation](#installation)
- [Using WhipIt (GUI Walkthrough)](#using-whipit-gui-walkthrough)
  - [Drive Selection & Disc Detection](#drive-selection--disc-detection)
  - [Live Cover Art & On-The-Fly Replacement](#live-cover-art--on-the-fly-replacement)
  - [Pre-Rip Disc Identification & Tracklist Preview](#pre-rip-disc-identification--tracklist-preview)
  - [Multi-Pressing / Edition Selector](#multi-pressing--edition-selector)
  - [Extraction Speed & Scratched Disc Recovery](#extraction-speed--scratched-disc-recovery)
  - [Drive Calibration & AccurateRip Offsets](#drive-calibration--accuraterip-offsets)
  - [5-Stage Visual Pipeline & Dual Progress Bars](#5-stage-visual-pipeline--dual-progress-bars)
  - [Live Whipper Console](#live-whipper-console)
  - [Atomic Transactions & 1-Click Crash Recovery](#atomic-transactions--1-click-crash-recovery)
- [Archival Directory Structure](#archival-directory-structure)
- [XDG Configuration & Cache Paths](#xdg-configuration--cache-paths)
- [Uninstallation](#uninstallation)
- [Development & Testing](#development--testing)

---

## Core Features

- **Bit-Perfect CD Extraction**: Powered by `whipper` and `cd-paranoia` for secure optical extraction with automated C2 error checking.
- **AccurateRip Checksums**: Validates every track against the community AccurateRip sector database to verify physical rip accuracy.
- **Archival FLAC Masters & 192k Opus Mirror**: Produces a bit-perfect FLAC audio library (verified via AccurateRip and internal FLAC audio stream MD5) with embedded cover art, ReplayGain tags, `.cue` sheet, and log, plus a synchronized 192k Opus VBR mirror.
- **Post-Tagging Stream Re-Verification**: Re-verifies every candidate FLAC file with `flac -t -s` after art embedding and ReplayGain tagging to guarantee stream decodability and audio MD5 match prior to publication.
- **Instant Cover Art & On-The-Fly Replacement**: Pulls original high-resolution master cover art from the Cover Art Archive (coverartarchive.org). Allows clicking "Change..." to substitute a custom scan at any point while tracks are ripping.
- **EBU R128 ReplayGain**: Applies standard album and track loudness normalization tags to both FLAC and Opus files using `loudgain`.
- **Cover Art Integration**: Discovers or downloads `cover.jpg`, embeds it directly into FLAC metadata picture blocks, and copies it to the Opus directory.
- **OS Media Dialog Inhabitation**: Automatically suppresses desktop automount popups and auto-open file browser prompts during rips across GNOME, XFCE (`thunar-volman`), MATE, and Cinnamon.
- **Atomic Publication**: Rips and encodes inside isolated staging directories (`.whipit-stage.<uuid>`). Only fully validated, complete albums are committed to your library.
- **Desktop Notifications & Sound Alerts**: Dispatches system notifications (`notify-send`) and completion chimes displaying verification status and cover art.

---

## System Requirements & Dependencies

WhipIt requires **Linux** and **Python 3.9+**, along with standard CLI audio tools.

### Install Dependencies via Package Manager

**Ubuntu / Debian / Linux Mint:**
```bash
sudo apt update && sudo apt install -y whipper flac opus-tools loudgain libnotify-bin eject desktop-file-utils diffutils python3-venv
```

**Fedora:**
```bash
sudo dnf install -y whipper flac opus-tools loudgain libnotify eject desktop-file-utils diffutils python3-virtualenv
```

**Arch Linux / Manjaro:**
```bash
sudo pacman -S --needed whipper flac opus-tools loudgain libnotify eject desktop-file-utils diffutils
```

---

## Installation

Clone this repository and run the automated installer:

```bash
chmod +x install.sh
./install.sh
```

### Developer Mode (Editable Install)
To develop locally so code changes reflect immediately without reinstalling:
```bash
./install.sh -e
```

The installer will:
1. Create an isolated environment in `~/.local/share/whipit/venv`.
2. Install WhipIt and its dependencies (`pydantic`, `PyQt6`).
3. Place a single command link at `~/.local/bin/whipit`.
4. Install the custom SVG icon and desktop launcher menu entry (`whipit.desktop`).
5. Configure repository tracking so the in-app "Check for Updates" menu can pull Git updates automatically.

---

## Using WhipIt (GUI Walkthrough)

Start WhipIt from your application launcher (under **Audio & Video**) or run:

```bash
whipit
```

### Drive Selection & Disc Detection
- **Optical Drive Dropdown**: Detects all connected SATA and external USB CD/DVD/BD drives.
- **Tray State Indicator**: Actively displays `Audio CD Loaded`, `Tray Empty`, or `Checking...`.
- **Eject Drive**: Safely unlocks `CDROM_LOCKDOOR` and opens the tray directly from the UI.

### Live Cover Art & On-The-Fly Replacement
- **Instant Display**: Within 2 seconds of identification or rip start, WhipIt fetches the front cover from the Cover Art Archive and displays it in the GUI.
- **Interactive Review**: Review the artwork while tracks 1, 2, 3... are extracting.
- **Change Art... On The Fly**: Click the art thumbnail or "Change..." during the rip to substitute a higher-resolution scan or custom image from your disk.
- **Seamless Embedding**: Upon rip completion, the active image is automatically embedded into every FLAC file, copied to the Opus mirror, and saved as `cover.jpg`.

### Pre-Rip Disc Identification & Tracklist Preview
- Click **Identify Disc** to read the Table of Contents (TOC) without ripping.
- Displays album artist, album title, duration, track count, catalog numbers, and barcode.
- Populates an expanded preview table showing track numbers, titles, and lengths with minimal scrolling.
- Provides direct clickable links to the **MusicBrainz** entry, **Discogs** release, or a 1-click **Attach Disc ID** URL if the CD is not yet cataloged.

### Multi-Pressing / Edition Selector
- If a disc matches multiple editions in MusicBrainz (e.g., US Original, European Remaster, Japanese Edition), a **Select Pressing** dropdown appears.
- Switching pressings updates the track titles, catalog metadata, and cover art in real time prior to extraction.

### Extraction Speed & Scratched Disc Recovery
- **Read Speed Selector**: Defaults to full speed, with quick options for `4x (Safe Mode)`, `8x`, `12x`, `16x`, or `24x`.
- **Rescue readable tracks (`--keep-going`)**: When enabled, read errors on scratched sectors will not abort the entire job; WhipIt extracts all remaining readable tracks.
- **Allow unknown disc (`-U`)**: Rips home recordings or uncatalogued CDs with generic track titles.
- **Overwrite existing**: Backs up and safely replaces any previous rip sharing the same album name.

### Drive Calibration & AccurateRip Offsets
- AccurateRip requires knowing your drive's read sample offset (e.g. `+6`, `+667`, `-12`).
- **Auto-Calibration Wizard**: If your drive is uncalibrated, inserting a commercial Audio CD prompts you to auto-detect its offset against AccurateRip.
- **Manual Override**: Offsets are stored in `~/.config/whipit/offsets`, or can be typed into the **Offset Override** field on the fly.

### 5-Stage Visual Pipeline & Dual Progress Bars
- The top badge row illuminates each stage as it progresses:
  1. `Disc Ready` -> 2. `Rip & Verify` -> 3. `Opus & ReplayGain` -> 4. `Publishing` -> 5. `Committed`
- **CD Extraction Progress Bar**: Live per-track extraction percentage from `cd-paranoia`.
- **Opus 192k Mirror Progress Bar**: Live multi-threaded encoding status across all CPU cores.
- **AccurateRip Badge**: Shows `✓ AccurateRip Verified` or warning flags upon completion.

### Live Whipper Console
- An integrated, collapsible activity console streams raw output from Whipper and cd-paranoia.
- Toggle it open or closed at any time with the header switch.

### Atomic Transactions & 1-Click Crash Recovery
- WhipIt stages candidate files in hidden directories on the destination filesystem.
- If power is lost or an extraction is cancelled, WhipIt detects interrupted jobs upon next launch and presents a prompt to roll back and clean up staging caches.

---

## Archival Directory Structure

By default, WhipIt saves output to `~/WhipIt` (customizable in the GUI or via `WHIPIT_OUTPUT_DIR`).

### Single-Disc Albums
```
~/WhipIt/Artist - Album Title/
├── Artist - Album Title (FLAC)/
│   ├── 01. First Track.flac
│   ├── 02. Second Track.flac
│   ├── cover.jpg
│   ├── Artist - Album Title.cue
│   └── Artist - Album Title.log
└── Artist - Album Title (Opus)/
    ├── 01. First Track.opus
    ├── 02. Second Track.opus
    └── cover.jpg
```

### Multi-Disc Sets
```
~/WhipIt/Artist - Album Title/
├── Artist - Album Title (FLAC)/
│   ├── Disc 1/
│   │   ├── 01. Track One.flac
│   │   ├── cover.jpg
│   │   └── Artist - Album Title (Disc 1).cue
│   └── Disc 2/
│       ├── 01. Track One.flac
│       └── cover.jpg
└── Artist - Album Title (Opus)/
    ├── Disc 1/
    └── Disc 2/
```

---

## XDG Configuration & Cache Paths

WhipIt adheres to standard FreeDesktop XDG specifications:

| Path | Description |
|---|---|
| `~/.config/whipit/offsets` | Drive hardware IDs and saved AccurateRip read offsets |
| `~/.local/state/whipit/jobs/` | JSON transaction manifests for active/recoverable rips |
| `~/.local/state/whipit/` | Full session logs and error logs |
| `~/.cache/whipit/` | Extraction workspace for temporary paranoia WAVs |
| `~/.cache/whipit/covers/` | Downloaded Cover Art Archive cache |
| `~/.local/share/applications/whipit.desktop` | Desktop launcher menu entry |
| `~/.local/share/icons/hicolor/scalable/apps/whipit.svg` | Scalable desktop application icon |

---

## Uninstallation

To remove WhipIt, its desktop launcher, and command links:

```bash
./uninstall.sh
```

To perform a complete purge including all configuration, drive offsets, and logs:

```bash
./uninstall.sh --purge
```

---

## Development & Testing

Run tests in an isolated development environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e '.[test]'
pytest -v
```

Alternatively, run tests directly using WhipIt's installed application venv:
```bash
~/.local/share/whipit/venv/bin/pip install pytest
~/.local/share/whipit/venv/bin/pytest -v
```
