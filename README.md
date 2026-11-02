# Plex Metadata Editor & Batch Reviewer Suite (`v1.0.0`)

A suite of tools for pulling rich metadata and poster artwork from a **Plex Media Server** (strictly **read-only**) and embedding them directly into local **`.mkv`**, **`.mp4`**, and **`.m4v`** media container files with **full safety-copy backup, byte-level stream verification, and automatic rollback**.

Once embedded, your titles, summaries, genres, cast/crew, release dates, content ratings, studio tags, external IDs (`IMDb`, `TMDb`, `TVDb`), and cover art travel permanently inside the video files themselves — surviving library rebuilds, server migrations, or playback in offline players.

---

## ✨ Features

- **Plex is 100% Read-Only:** Only `GET` requests are ever sent to Plex. All edits are written exclusively into the local media file containers (`mkvpropedit` for Matroska `.mkv`, `mutagen` for `.mp4`/`.m4v`).
- **4 Ways to Work:**
  1. **Native Desktop Batch Reviewer (`plex_reviewer_qt.py`)** — Standalone **PyQt5** desktop app with no local web server or browser required.
  2. **Web Batch Reviewer (`plex_metadata_reviewer.py`)** — Local browser-based batch queue reviewer with automatic server shutdown when the window closes.
  3. **Web Single-File Editor (`plex_metadata_editor.py`)** — Full interactive tag & poster editor (edit any field, choose alternate Plex posters, upload custom cover art, or grab a video frame).
  4. **CLI Batch Enricher (`plex_mkv_metadata_batch.py`)** — Automated command-line batch processor with dry-run support, disk-space guards, and incremental CSV reporting.
- **Visual Diff Highlighting:** Instantly see which tags are **`NEW`** (vivid green) vs. **`CHG`** (vivid orange, with hover tooltips showing the file's previous value).
- **3-Second Auto-Mode:** Hands-free batch review mode with a live countdown bar that automatically saves and advances through your queue, pausing immediately if you interact.
- **6-Step Verified Safe-Write Pipeline (`media_tag_io.py`):**
  1. **Safety Copy:** Creates a full backup copy before touching the file (with real-time `%`, `GB`, and `MB/s` progress).
  2. **Check Original:** Captures baseline stream layout, duration, and 20-second audio/video packet MD5 hashes at the start and middle of the file.
  3. **Write Tags:** Embeds tags + `cover.jpg` attachment while preserving unrelated MKV global tags (such as `ENCODER`).
  4. **Verify:** Confirms duration, stream layout, raw A/V packet MD5 hashes, container structure, exact tag read-back, and cover presence.
  5. **Restore Original (on failure):** Automatically restores and re-verifies the original file if any check fails.
  6. **Clean Up:** Removes the temporary safety copy once verified (or keeps it in your configured backup directory).
- **Smart Resume Sentinel (`PLEX_METADATA_SYNCED`):** Tracks whether a file was tagged by the batch reviewer (`plex_metadata_editor_batch`) or hand-edited in the single-file UI (`plex_metadata_editor_ui`), so batch runs never overwrite hand-curated files.

---

## 🖥️ Tools Overview

| Tool | Script | Launcher | Process Name (`ps` / `top`) | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Desktop Batch Reviewer** | `plex_reviewer_qt.py` | `./start_plex_reviewer_qt.sh` | `plex metadata ui editor` / `plex-meta-ui` | Native PyQt5 desktop queue reviewer with `Prev`, `Skip`, `OK & Next`, `Auto-Mode`, and live 6-step save progress. |
| **Web Batch Reviewer** | `plex_metadata_reviewer.py` | `./start_plex_metadata_reviewer.sh` | `plex metadata web reviewer (port 8766)` / `plex-web-rev` | Browser app window on `127.0.0.1:8766` with the same batch review workflow and 15s heartbeat auto-shutdown. |
| **Web Single-File Editor** | `plex_metadata_editor.py` | `./start_plex_metadata_editor.sh` | `plex metadata web editor (port 8765)` / `plex-web-edit` | Full single-file editor on `127.0.0.1:8765` with editable fields, alternate Plex poster picker, and video frame grabber. |
| **CLI Batch Enricher** | `plex_mkv_metadata_batch.py` | *(CLI)* | `python3` | Headless recursive batch script with `--no-dry-run`, `--limit`, `--force`, and CSV audit logs. |

---

## 📦 Prerequisites

### System Packages (Linux / Debian / Ubuntu)
Requires `mkvtoolnix` (`mkvpropedit`, `mkvextract`, `mkvmerge`), `ffmpeg` / `ffprobe`, `mediainfo`, and Python 3:
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-pyqt5 python3-mutagen python3-requests python3-setproctitle mkvtoolnix ffmpeg mediainfo
```

### Python Packages (via `pip` if not using `apt`)
```bash
pip install -r requirements.txt
```

---

## 🚀 Quick Start

### 1. Native PyQt5 Desktop Batch Reviewer (Recommended for Batch Review)
```bash
./start_plex_reviewer_qt.sh
```
1. In the **Settings & Scan** dialog, click **Browse...** to select your media folder, enter your **Plex URL** (`http://127.0.0.1:32400`) and **Plex Token**, and click **Start Scan**.
2. Review the highlighted **`NEW`** (green) and **`CHG`** (orange) fields for each matched file.
3. Click **OK & Next** (or enable **Auto-Mode**) to write and verify the metadata.

### 2. Web Batch Reviewer
```bash
./start_plex_metadata_reviewer.sh
# To stop manually:
./start_plex_metadata_reviewer.sh --stop
```

### 3. Web Single-File Editor (Full Field & Poster Editing)
```bash
./start_plex_metadata_editor.sh
# To stop manually:
./start_plex_metadata_editor.sh --stop
```

### 4. Command-Line Batch Script
```bash
# Dry run first (lists matched files without modifying anything):
/usr/bin/python3 plex_mkv_metadata_batch.py --folder "/path/to/media" --plex-token "YOUR_PLEX_TOKEN"

# Test on the first 3 files:
/usr/bin/python3 plex_mkv_metadata_batch.py --folder "/path/to/media" --plex-token "YOUR_PLEX_TOKEN" --no-dry-run --limit 3

# Full batch run:
/usr/bin/python3 plex_mkv_metadata_batch.py --folder "/path/to/media" --plex-token "YOUR_PLEX_TOKEN" --no-dry-run
```

---

## ⚙️ Configuration & Logs

- **Settings File:** `~/scripts_meta_data/plex_metadata_editor_settings.json` (shared across all UIs; stores your folder, Plex URL, Plex token, and optional backup directory).
- **Logs:** Written to `~/logs/` (`start_plex_reviewer_qt_YYYY_MM_DD.log`, `start_plex_metadata_reviewer_YYYY_MM_DD.log`, `plex_metadata_editor_YYYY_MM_DD.log`).
- **Audit CSVs:** Written to `~/scripts_meta_data/`.

---

## 📄 License

Licensed under the **GNU General Public License v3.0 (GPLv3)**. See [LICENSE](LICENSE) for full details.
