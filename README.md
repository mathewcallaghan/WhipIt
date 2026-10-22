<p align="center">
  <img src="whipit/assets/whipit.svg" alt="WhipIt Icon" width="128" height="128">
</p>

# WhipIt

> **AccurateRip + MusicBrainz Archival CD Ripper & Opus Transcoder**

WhipIt is a Linux desktop application built with PyQt6 for ripping Audio CDs into AccurateRip-verified FLAC files alongside synchronized 192kbps Opus mirrors. It uses Whipper and cd-paranoia for extraction, retrieves metadata from MusicBrainz, computes EBU R128 ReplayGain via loudgain, downloads and embeds cover art, and stages outputs in atomic transactions to prevent partial writes.

---

## Table of Contents
- [Core Features](#core-features)
- [System Requirements & Dependencies](#system-requirements--dependencies)
- [Installation](#installation)
- [Using WhipIt (GUI Walkthrough)](#using-whipit-gui-walkthrough)
  - [Drive Selection & Disc Detection](#drive-selection--disc-detection)
  - [Cover Art Management](#cover-art-management)
  - [Pre-Rip Disc Identification & Tracklist Preview](#pre-rip-disc-identification--tracklist-preview)
  - [Multi-Pressing / Edition Selector](#multi-pressing--edition-selector)
  - [Extraction Options](#extraction-options)
  - [Drive Calibration & AccurateRip Offsets](#drive-calibration--accuraterip-offsets)
  - [Status & Progress Tracking](#status--progress-tracking)
  - [Live Whipper Console](#live-whipper-console)
  - [Transaction Staging & Crash Recovery](#transaction-staging--crash-recovery)
- [Archival Directory Structure](#archival-directory-structure)
- [XDG Configuration & Cache Paths](#xdg-configuration--cache-paths)
- [Uninstallation](#uninstallation)
- [Development & Testing](#development--testing)

---

## Core Features

- **Accurate CD Extraction**: Uses `whipper` and `cd-paranoia` for optical extraction with C2 error checking and configured drive read offsets.
- **AccurateRip Verification**: Validates extracted audio tracks against the AccurateRip online database, and runs `flac -t -s` integrity checks after tagging.
- **Dual Output Formats**: Writes bit-perfect FLAC files with embedded art, CUE sheet, and rip log alongside a synchronized 192kbps Opus mirror.
- **Metadata & ReplayGain**: Matches releases via MusicBrainz and applies EBU R128 loudness tags to both FLAC and Opus outputs using `loudgain`.
- **Cover Art Handling**: Retrieves front cover art from the Cover Art Archive and supports substituting custom image files before or during extraction.
- **Desktop Media Dialog Inhibition**: Suppresses desktop automounting and file-manager prompts during rips on GNOME, XFCE (`thunar-volman`), MATE, and Cinnamon.
- **Transaction Staging**: Performs extraction and transcoding in isolated `.whipit-stage.<job-id>` directories before committing to the archive.
- **Notifications & Alerts**: Dispatches desktop notifications (`notify-send`) and completion chimes with rip status.

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

### Cover Art Management
- **Cover Art Archive Lookup**: Fetches front cover artwork from the Cover Art Archive when a MusicBrainz release ID is matched.
- **Custom Image Selection**: Click the cover art preview or the "Change..." button to select a local image (`.jpg`, `.png`).
- **Embedding & Mirroring**: Embeds the active image into FLAC metadata blocks, copies it to the Opus mirror, and writes `cover.jpg`.

### Pre-Rip Disc Identification & Tracklist Preview
- **Identify Disc**: Reads the Table of Contents (TOC) without starting an extraction.
- **Metadata Preview**: Shows artist, album, duration, track count, catalog number, and barcode.
- **Tracklist Table**: Displays track numbers, titles, and durations retrieved from MusicBrainz.
- **Database Links**: Provides links to the MusicBrainz release, Discogs search, or a link to attach the disc ID if uncataloged.

### Multi-Pressing / Edition Selector
- When a disc matches multiple MusicBrainz releases, a **Select Pressing** dropdown lets you pick the specific edition.
- Changing the selection updates track titles, catalog information, and artwork.

### Extraction Options
- **Read Speed**: Select extraction speed (defaults to drive maximum, with options for 4x, 8x, 12x, 16x, or 24x for problem discs).
- **Keep Going (`--keep-going`)**: Continues ripping remaining readable tracks if read errors occur on damaged tracks.
- **Allow Unknown Disc (`-U`)**: Rips uncataloged discs or CD-Rs with generic track names when MusicBrainz data is unavailable.
- **Overwrite Existing**: Replaces an existing rip with a backup preserved during the transaction.

### Drive Calibration & AccurateRip Offsets
- **Offset Configuration**: AccurateRip requires knowing your drive's read sample offset (e.g., `+6`, `+667`, `-12`).
- **Calibration Wizard**: If your drive is uncalibrated, inserting a commercial Audio CD prompts an automated offset search against the AccurateRip database.
- **Manual Override**: Stored offsets can be overridden directly from the UI.

### Status & Progress Tracking
- **Pipeline Stages**: Visual indicators track progress through Disc Ready, Rip & Verify, Opus & ReplayGain, Publishing, and Committed stages.
- **CD Extraction Progress**: Shows current track and progress reported by cd-paranoia.
- **Opus Mirror Progress**: Tracks parallel transcode progress across all tracks.
- **AccurateRip Status**: Displays AccurateRip confidence or verification warnings when complete.

### Live Whipper Console
- An integrated activity console streams raw output from Whipper and cd-paranoia.
- Can be collapsed or expanded at any time.

### Transaction Staging & Crash Recovery
- Extractions and transcodes take place in staging directories (`.whipit-stage.<job-id>`) within the target filesystem.
- If a rip is interrupted or cancelled, WhipIt detects uncommitted jobs on next launch and offers to resume publication or discard the staging data.

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
