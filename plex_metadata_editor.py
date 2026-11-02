#!/usr/bin/python3
"""
plex_metadata_editor.py

PURPOSE:
  Local web UI for reviewing and editing metadata one video file at a time.
  For each .mkv/.mp4/.m4v file in a chosen folder it:
    1. Looks the file up in Plex first (by file path). If found, loads Plex's
       full metadata + poster.
    2. Otherwise falls back to the tags embedded in the file itself
       (Matroska global tags / MP4 atoms) and its embedded cover art, or a
       frame grabbed from the video if there is no cover.
    3. Shows everything in an editable form (all fields editable) with the
       poster, and lets you replace the poster (upload an image, pick one of
       Plex's alternate posters, or use a video frame).
    4. On Save: writes the tags + poster INTO THE FILE ONLY (mkvpropedit for
       MKV, mutagen for MP4). Plex is a READ-ONLY source -- nothing is ever
       sent to Plex (the Plex client in this script refuses anything but GET).

  CHANGE MARKERS: every field is compared with what's in the file right
  now. Green NEW = the file doesn't have it yet (e.g. filled in from Plex),
  orange CHANGED = the file has a different value (hover to see it), red
  REMOVED = in the file now but cleared in the form. A summary line counts
  them. The header also shows who tagged the file before (batch script or
  this editor) from its PLEX_METADATA_SYNCED tag.

  "ALREADY DONE" TAG: every save writes PLEX_METADATA_SYNCED=
  plex_metadata_editor_ui (the batch script writes
  plex_metadata_editor_batch). The batch enrichment job
  (plex_mkv_metadata_batch.py) skips files carrying either value unless run
  with --force; files with the legacy value "1" get re-tagged by the batch.

  QUIT: the Quit button stops the server (waits for a save in progress to
  finish first). Closing the browser window alone leaves it running.

  SAVE SAFETY (every save):
    a. Safety copy of the original is made first (next to the file as a
       hidden .<name>.editor_safety_<timestamp>, or in "Keep backups in" if
       that setting is filled in).
    b. Tags/poster are written to the file.
    c. Quick checks, edited file vs original (a few seconds, no full
       decode): duration, stream layout (codecs, resolution, channels,
       sample rate), container structure, MD5 of the raw audio/video
       packets at the start and middle of the file (proves the media
       itself is byte-identical), file size change is small, tags re-read
       exactly as saved, cover present.
    d. All checks pass -> safety copy deleted (kept if "Keep backups in" is
       set). Any check fails -> original is restored from the safety copy
       and the UI shows what failed.

  CONVERT PANEL ("Convert" button -- COMING SOON):
    Lists other video formats in the folder (avi, wmv, mov, flv, mpg, vob,
    asf, divx, rm, ts, webm, ...). Select files and add them to a saved
    conversion queue. "Start queue" is disabled: the conversion job
    (mkv_convert.py-style convert + heavy verification, mkv_promote.py-style
    promotion, remove original, then metadata edit here) is not built yet.
    Nothing is converted, moved or deleted by this panel.

  Folder, Plex URL, Plex token, backup folder and include-subfolders are
  all SETTINGS IN THE UI (gear button), not command-line arguments. They
  start with defaults and are remembered in a settings file between runs.

REQUIRES:
  /usr/bin/python3 (has `requests` + `mutagen`; bare `python3` may resolve to
  linuxbrew without them). mkvtoolnix (mkvpropedit, mkvextract, mkvmerge),
  ffmpeg, ffprobe. plex_metadata_lib.py in the same folder as this script.

USAGE:
  /usr/bin/python3 plex_metadata_editor.py [--port PORT] [--bind ADDRESS]
  then open http://127.0.0.1:PORT in a browser.

ARGUMENTS:
  --port PORT      Port for the local web server. Default: 8765.
  --bind ADDRESS   Address to listen on. Default: 127.0.0.1 (this machine
                   only). The UI has NO LOGIN and can modify files -- only
                   use 0.0.0.0 on a network you fully trust.

UI SETTINGS (defaults, all changeable in the Settings panel):
  Directory            (none -- you pick it on first run)
  Plex URL             http://127.0.0.1:32400
  Plex token           same default token as plex_mkv_metadata_batch.py
  Keep backups in      (blank = temporary safety copy next to the file,
                        deleted once the save is verified)
  Include subfolders   on

EXAMPLES:
  /usr/bin/python3 ~/myScripts/plex_metadata_editor.py
  /usr/bin/python3 ~/myScripts/plex_metadata_editor.py --port 9000

OUTPUT:
  Log:        ~/logs/plex_metadata_editor_YYYY_MM_DD.log
  Save log:   ~/scripts_meta_data/plex_metadata_editor_progress.csv
              (one row per Save: file, safety copy, write/verification/restore results)
  Settings:   ~/scripts_meta_data/plex_metadata_editor_settings.json
              (chmod 600 -- contains the Plex token; also stores the last
              file viewed per folder so you resume where you left off)
  Convert queue: ~/scripts_meta_data/plex_metadata_editor_convert_queue.json
              (files queued for future MKV conversion; one list for all folders)
  Image cache: ~/scripts_meta_data/plex_metadata_editor_cache/
              (posters/frames/temp tag XML; wiped at every start)

SIDE EFFECTS:
  - Save edits the ORIGINAL video file in place (tags + cover attachment).
    Only the container metadata changes, not the audio/video streams.
  - Every save makes a full safety copy first, so it needs free space equal
    to the file size (+1GB margin) on the file's disk (or the backup disk),
    and a large file takes a while to copy. The copy is deleted after a
    verified save unless "Keep backups in" is set
    (<name>.bak.YYYYMMDD_HHMMSS there).
  - If a save fails verification, the file is put back exactly as it was.
  - Unknown global tags already in an MKV (e.g. ENCODER) are preserved and
    shown under "Other file tags". Non-global (per-track) tags are never
    touched. MP4 atoms this editor doesn't manage are left alone.
  - Nothing is written to Plex. Plex will notice the file changed and may
    rescan it.
"""

import argparse
import base64
import csv
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

APP_VERSION = "v1.0.0"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_metadata_lib as lib  # noqa: E402
import media_tag_io as mt  # noqa: E402  -- shared with plex_mkv_metadata_batch.py
# Field model, MKV/MP4 read/write, verification and the safe-write pipeline
# live in media_tag_io.py so the batch job uses exactly the same code.
from media_tag_io import (  # noqa: E402
    SENTINEL_SOURCES, SENTINEL_VALUE_UI,
    clean_fields, empty_fields, extract_mkv_cover, extract_mp4_cover, has_content,
    parse_plex, read_mkv, read_mp4, validate_fields,
)

try:
    from plex_mkv_metadata_batch import DEFAULT_PLEX_TOKEN
except Exception:
    DEFAULT_PLEX_TOKEN = ""

LOG_DIR = os.path.expanduser("~/logs")
DATA_DIR = os.path.expanduser("~/scripts_meta_data")
SETTINGS_PATH = os.path.join(DATA_DIR, "plex_metadata_editor_settings.json")
PROGRESS_CSV = os.path.join(DATA_DIR, "plex_metadata_editor_progress.csv")
CACHE_DIR = os.path.join(DATA_DIR, "plex_metadata_editor_cache")
CONVERT_QUEUE_PATH = os.path.join(DATA_DIR, "plex_metadata_editor_convert_queue.json")
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v")
# Other video formats listed in the Convert panel (conversion itself: coming soon).
CONVERT_EXTENSIONS = (".avi", ".wmv", ".mov", ".flv", ".mpg", ".mpeg", ".mpe", ".vob", ".asf",
                      ".divx", ".rm", ".rmvb", ".ts", ".m2ts", ".webm", ".3gp", ".ogm")

DEFAULT_SETTINGS = {
    "directory": "",
    "plex_url": "http://127.0.0.1:32400",
    "plex_token": DEFAULT_PLEX_TOKEN,
    "backup_dir": "",
    "recursive": True,
    "positions": {},
}

CSV_FIELDS = ["timestamp", "file", "rating_key", "safety_copy", "poster_action",
              "file_write", "verification", "restored", "notes"]

# Save steps shown in the UI progress panel, in order ("Restore original" is added only on failure).
SAVE_STEPS = ["Prepare poster", "Safety copy", "Check original", "Write tags", "Verify", "Clean up", "Reload file"]


_log_lock = threading.Lock()


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    path = os.path.join(LOG_DIR, f"plex_metadata_editor_{datetime.date.today():%Y_%m_%d}.log")
    with _log_lock:
        with open(path, "a") as f:
            f.write(line + "\n")
    print(line, flush=True)


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Plex
# ---------------------------------------------------------------------------

class Plex:
    """Read-only Plex client: only GET requests exist here, so nothing in
    this script can change Plex."""

    def __init__(self, url, token):
        self.url = (url or "").rstrip("/")
        self.token = token or ""

    def get(self, path, params=None):
        params = dict(params or {})
        params["X-Plex-Token"] = self.token
        r = requests.get(self.url + path, params=params, timeout=60)
        r.raise_for_status()
        return r

    def xml(self, path, params=None):
        return ET.fromstring(self.get(path, params).content)

    def sections(self):
        root = self.xml("/library/sections")
        return [{"key": d.get("key"), "title": d.get("title"), "type": d.get("type")}
                for d in root.findall("Directory")]

    def build_index(self):
        """realpath(file) -> ratingKey for every movie/episode in every
        movie/show section."""
        index = {}
        for s in self.sections():
            item_type = {"movie": 1, "show": 4}.get(s["type"])
            if not item_type:
                continue
            root = self.xml(f"/library/sections/{s['key']}/all", {"type": item_type})
            for video in root.findall("Video"):
                for part in video.findall("./Media/Part"):
                    if part.get("file"):
                        index[os.path.realpath(part.get("file"))] = video.get("ratingKey")
        return index

    def item(self, rating_key):
        root = self.xml(f"/library/metadata/{rating_key}")
        video = root.find("Video")
        if video is None:
            raise RuntimeError(f"No Video element for ratingKey {rating_key}")
        section = video.get("librarySectionID") or root.get("librarySectionID")
        return video, section

    def image(self, url, width=600, height=900):
        """Any Plex image path or agent URL -> JPEG bytes via Plex's photo
        transcoder (handles relative /library paths and remote agent URLs)."""
        try:
            r = self.get("/photo/:/transcode",
                         {"url": url, "width": width, "height": height, "minSize": 1, "upscale": 1})
            return r.content, r.headers.get("Content-Type", "image/jpeg")
        except Exception:
            if url.startswith("/"):
                r = self.get(url)
                return r.content, r.headers.get("Content-Type", "image/jpeg")
            raise


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

def to_jpeg(src, dest, max_width=1000):
    r = run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src,
             "-vf", f"scale='min({max_width},iw)':-2", "-frames:v", "1", "-q:v", "2", dest], timeout=60)
    if r.returncode != 0 or not os.path.exists(dest):
        raise RuntimeError(f"image conversion failed: {r.stderr.strip()[:200]}")
    return dest


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = self.load_settings()
        self.files = []        # absolute paths (.mkv/.mp4/.m4v -- editable)
        self.other_files = []  # absolute paths of other formats (Convert panel)
        self.index = {}        # realpath -> ratingKey
        self.records = {}      # file index -> loaded record (server-side snapshot)
        self.status = {"plex_ok": None, "plex_message": "", "plex_indexed": 0}
        # Background save job + its live progress. Guarded by plock (not
        # self.lock, which the save itself holds) so the UI can poll freely.
        self.plock = threading.Lock()
        self.job = None
        self.progress = None

    # -- save progress (read by /api/save_status while a save runs) --
    def prog_begin(self, name):
        now = time.time()
        with self.plock:
            self.progress = {"name": name, "started": now, "step_started": now, "current": None,
                             "steps": [{"name": s, "state": "pending"} for s in SAVE_STEPS],
                             "pct": None, "bytes_done": 0, "bytes_total": 0, "rate": 0, "eta": None}

    def prog_step(self, name):
        with self.plock:
            p = self.progress
            for s in p["steps"]:
                if s["state"] == "active":
                    s["state"] = "done"
            match = [s for s in p["steps"] if s["name"] == name]
            if match:
                match[0]["state"] = "active"
            else:  # e.g. "Restore original": insert before the clean-up steps
                pos = next((k for k, s in enumerate(p["steps"]) if s["state"] == "pending"), len(p["steps"]))
                p["steps"].insert(pos, {"name": name, "state": "active"})
            p.update(current=name, step_started=time.time(), pct=None, bytes_done=0, bytes_total=0, rate=0, eta=None)

    def prog_fail(self):
        with self.plock:
            for s in self.progress["steps"]:
                if s["state"] == "active":
                    s["state"] = "failed"

    def prog_skip_pending(self, names):
        with self.plock:
            for s in self.progress["steps"]:
                if s["state"] == "pending" and s["name"] in names:
                    s["state"] = "skipped"

    def prog_pct(self, pct):
        with self.plock:
            self.progress["pct"] = max(0.0, min(100.0, float(pct)))

    def prog_bytes(self, done, total):
        with self.plock:
            p = self.progress
            elapsed = max(time.time() - p["step_started"], 0.001)
            rate = done / elapsed
            p.update(bytes_done=done, bytes_total=total, pct=100.0 * done / total if total else 100.0,
                     rate=rate, eta=(total - done) / rate if rate > 0 else None)

    def prog_finish(self):
        with self.plock:
            for s in self.progress["steps"]:
                if s["state"] == "active":
                    s["state"] = "done"
            self.progress["current"] = None

    def start_save(self, i, raw_fields, poster):
        """Validate, then run save() in a background thread. The UI polls
        save_status(job) for progress and the final result."""
        errors = validate_fields(clean_fields(raw_fields))
        if errors:
            return {"ok": False, "errors": errors}
        with self.plock:
            if self.job and self.job["state"] == "running":
                return {"ok": False, "errors": ["a save is already running -- wait for it to finish"]}
            job = {"id": uuid.uuid4().hex, "state": "running", "result": None}
            self.job = job

        def work():
            try:
                res = self.save(i, raw_fields, poster)
            except Exception as exc:
                log(f"ERROR save job: {exc}\n{traceback.format_exc()}")
                res = {"ok": False, "restored": False, "steps": [{"step": "save", "ok": False, "detail": str(exc)[:300]}]}
            with self.plock:
                job["result"] = res
                job["state"] = "done"
        threading.Thread(target=work, daemon=True).start()
        return {"job": job["id"]}

    def save_status(self, job_id):
        with self.plock:
            job = self.job
            if not job or job["id"] != job_id:
                return {"state": "unknown"}
            prog = json.loads(json.dumps(self.progress)) if self.progress else None
            if prog:
                now = time.time()
                prog["elapsed"] = now - prog.pop("started")
                prog["step_elapsed"] = now - prog.pop("step_started")
            return {"state": job["state"], "progress": prog,
                    "result": job["result"] if job["state"] == "done" else None}

    # -- settings --
    def load_settings(self):
        s = dict(DEFAULT_SETTINGS)
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH) as fh:
                    s.update(json.load(fh))
            except Exception as exc:
                log(f"WARNING: settings file unreadable, using defaults: {exc}")
        s.pop("push_to_plex", None)  # removed setting from older versions
        return s

    def save_settings(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self.settings, fh, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        os.chmod(SETTINGS_PATH, 0o600)

    def plex(self):
        return Plex(self.settings["plex_url"], self.settings["plex_token"])

    # -- file list --
    def apply(self):
        """Rescan the folder + rebuild the Plex path index."""
        with self.lock:
            self.records = {}
            self.files = []
            self.other_files = []
            d = self.settings["directory"]
            errors = []
            if d:
                if not os.path.isdir(d):
                    errors.append(f"Directory not found: {d}")
                else:
                    for root, dirs, names in os.walk(d):
                        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
                        for n in names:
                            if n.startswith("."):
                                continue
                            if n.lower().endswith(VIDEO_EXTENSIONS):
                                self.files.append(os.path.join(root, n))
                            elif n.lower().endswith(CONVERT_EXTENSIONS):
                                self.other_files.append(os.path.join(root, n))
                        if not self.settings["recursive"]:
                            break
                    self.files.sort(key=lambda p: os.path.relpath(p, d).lower())
                    self.other_files.sort(key=lambda p: os.path.relpath(p, d).lower())
            try:
                self.index = self.plex().build_index()
                self.status = {"plex_ok": True, "plex_indexed": len(self.index),
                               "plex_message": f"{len(self.index)} Plex items indexed"}
            except Exception as exc:
                self.index = {}
                self.status = {"plex_ok": False, "plex_indexed": 0, "plex_message": f"Plex unavailable: {exc}"}
                errors.append(self.status["plex_message"])
            log(f"Applied settings: dir={d!r} files={len(self.files)} other_formats={len(self.other_files)} "
                f"{self.status['plex_message']}")
            return errors

    def file_list(self):
        d = self.settings["directory"]
        return [{"i": i, "rel": os.path.relpath(p, d),
                 "in_plex": os.path.realpath(p) in self.index} for i, p in enumerate(self.files)]

    # -- conversion queue (tracking only; the conversion job is coming soon) --
    def load_queue(self):
        if not os.path.exists(CONVERT_QUEUE_PATH):
            return []
        try:
            with open(CONVERT_QUEUE_PATH) as fh:
                return json.load(fh)
        except Exception as exc:
            log(f"WARNING: convert queue file unreadable, treating as empty: {exc}")
            return []

    def save_queue(self, queue):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONVERT_QUEUE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(queue, fh, indent=2)
        os.replace(tmp, CONVERT_QUEUE_PATH)

    def convert_state(self):
        with self.lock:
            queue = self.load_queue()
            queued = {q["path"] for q in queue}
            d = self.settings["directory"]
            for q in queue:
                q["exists"] = os.path.exists(q["path"])
            candidates = []
            for p in self.other_files:
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                candidates.append({"path": p, "rel": os.path.relpath(p, d), "size": size,
                                   "ext": os.path.splitext(p)[1].lower().lstrip("."),
                                   "in_plex": os.path.realpath(p) in self.index,
                                   "queued": p in queued})
            return {"candidates": candidates, "queue": queue}

    def queue_add(self, paths):
        with self.lock:
            allowed = set(self.other_files)  # only files listed in the panel
            queue = self.load_queue()
            queued = {q["path"] for q in queue}
            added = 0
            for p in paths:
                if p in allowed and p not in queued:
                    queue.append({"path": p, "size": os.path.getsize(p), "status": "queued",
                                  "added": datetime.datetime.now().isoformat(timespec="seconds")})
                    queued.add(p)
                    added += 1
            self.save_queue(queue)
            log(f"Convert queue: added {added} file(s), {len(queue)} total")

    def queue_remove(self, paths):
        with self.lock:
            drop = set(paths)
            queue = [q for q in self.load_queue() if q["path"] not in drop]
            self.save_queue(queue)
            log(f"Convert queue: removed {len(drop)} file(s), {len(queue)} total")

    def position(self):
        rel = self.settings["positions"].get(self.settings["directory"])
        d = self.settings["directory"]
        for i, p in enumerate(self.files):
            if os.path.relpath(p, d) == rel:
                return i
        return 0

    # -- per-file load --
    def load(self, i):
        with self.lock:
            path = self.files[i]
            st = os.stat(path)
            prefix = hashlib.sha1(f"{path}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
            rec = {"i": i, "path": path, "name": os.path.basename(path), "size": st.st_size,
                   "ext": os.path.splitext(path)[1].lower(), "prefix": prefix, "warnings": [],
                   "plex": None, "plex_fields": None, "file_fields": None}

            try:
                if mt.is_mp4(path):   # by content: some MP4s are named .mkv
                    file_fields, mp4 = read_mp4(path)
                    cover = os.path.join(CACHE_DIR, f"{prefix}_embedded.jpg")
                    rec["embedded_cover"] = cover if extract_mp4_cover(mp4, cover) else None
                else:
                    file_fields, info = read_mkv(path, os.path.join(CACHE_DIR, prefix))
                    cover = os.path.join(CACHE_DIR, f"{prefix}_embedded.jpg")
                    rec["embedded_cover"] = cover if extract_mkv_cover(path, info, cover) else None
                sentinel = file_fields.pop("_sentinel", None)
                rec["tagged_by"] = ({"value": sentinel, "source": SENTINEL_SOURCES.get(sentinel, "unknown tool")}
                                    if sentinel is not None else None)
                rec["file_fields"] = file_fields
            except Exception as exc:
                rec["file_fields"] = empty_fields()
                rec["tagged_by"] = None
                rec["embedded_cover"] = None
                rec["warnings"].append(f"Could not read file tags: {exc}")

            rk = self.index.get(os.path.realpath(path))
            if rk:
                try:
                    video, section = self.plex().item(rk)
                    rec["plex"] = {"rating_key": rk, "section": section, "type": video.get("type"),
                                   "thumb": video.get("thumb")}
                    rec["plex_fields"] = parse_plex(video)
                except Exception as exc:
                    rec["warnings"].append(f"Plex lookup failed: {exc}")

            if rec["plex_fields"]:
                fields = dict(rec["plex_fields"])
                fields["extra_tags"] = rec["file_fields"]["extra_tags"]
                rec["source"] = "plex"
            elif has_content(rec["file_fields"]):
                fields = rec["file_fields"]
                rec["source"] = "file"
            else:
                fields = empty_fields()
                fields["extra_tags"] = rec["file_fields"]["extra_tags"]
                fields["title"] = os.path.splitext(rec["name"])[0]
                rec["source"] = "none"
            rec["fields"] = fields

            if rec["plex"] and rec["plex"]["thumb"]:
                rec["poster_kind"] = "plex"
            elif rec["embedded_cover"]:
                rec["poster_kind"] = "embedded"
            else:
                rec["poster_kind"] = "frame"

            self.records[i] = rec
            self.settings["positions"][self.settings["directory"]] = os.path.relpath(path, self.settings["directory"])
            self.save_settings()
            return self.public(rec)

    def public(self, rec):
        v = rec["prefix"]
        return {
            "i": rec["i"], "total": len(self.files), "path": rec["path"], "name": rec["name"],
            "size": rec["size"], "ext": rec["ext"], "source": rec["source"], "fields": rec["fields"],
            "file_fields": rec["file_fields"], "plex_fields": rec["plex_fields"],
            "plex": rec["plex"], "warnings": rec["warnings"], "tagged_by": rec["tagged_by"],
            "has_embedded_cover": bool(rec["embedded_cover"]),
            "poster_url": f"/api/image?i={rec['i']}&kind={rec['poster_kind']}&v={v}",
            "poster_kind": rec["poster_kind"],
            "frame_url": f"/api/image?i={rec['i']}&kind=frame&v={v}",
        }

    def frame(self, rec):
        dest = os.path.join(CACHE_DIR, f"{rec['prefix']}_frame.jpg")
        if not os.path.exists(dest):
            try:
                t = lib.ffprobe_duration(rec["path"]) * 0.1
            except Exception:
                t = 30
            r = run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.1f}", "-i", rec["path"],
                     "-map", "0:V:0", "-frames:v", "1", "-vf", "scale='min(1000,iw)':-2", "-q:v", "2", dest],
                    timeout=120)
            if r.returncode != 0 or not os.path.exists(dest):
                raise RuntimeError(f"frame grab failed: {r.stderr.strip()[:200]}")
        return dest

    def image(self, i, kind):
        rec = self.records.get(i) or (self.load(i) and self.records[i])
        if kind == "plex" and rec["plex"] and rec["plex"]["thumb"]:
            return self.plex().image(rec["plex"]["thumb"])
        if kind == "embedded" and rec["embedded_cover"]:
            with open(rec["embedded_cover"], "rb") as fh:
                data = fh.read()
            return data, "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        with open(self.frame(rec), "rb") as fh:
            return fh.read(), "image/jpeg"

    def plex_posters(self, i):
        rec = self.records[i]
        if not rec["plex"]:
            return []
        root = self.plex().xml(f"/library/metadata/{rec['plex']['rating_key']}/posters")
        return [{"key": p.get("key"), "thumb": p.get("thumb") or p.get("key"),
                 "selected": p.get("selected") == "1", "provider": p.get("provider") or "upload"}
                for p in root.findall("Photo")]

    # -- save --
    def resolve_poster(self, rec, poster):
        """Returns (jpeg_path_to_embed_or_None, action_label)."""
        action = (poster or {}).get("action", "keep")
        dest = os.path.join(CACHE_DIR, f"{rec['prefix']}_new_cover.jpg")
        if action == "upload":
            data_url = poster.get("data", "")
            raw = base64.b64decode(data_url.split(",", 1)[-1])
            src = os.path.join(CACHE_DIR, f"{rec['prefix']}_upload.bin")
            with open(src, "wb") as fh:
                fh.write(raw)
            to_jpeg(src, dest)
            os.remove(src)
            return dest, "upload"
        if action == "plex":
            data, _ = self.plex().image(poster["key"], 1000, 1500)
            with open(dest, "wb") as fh:
                fh.write(data)
            return dest, "plex_alternate"
        if action == "frame":
            to_jpeg(self.frame(rec), dest)
            return dest, "video_frame"
        # keep: if the file has no cover yet but Plex has one, embed Plex's.
        if not rec["embedded_cover"] and rec["plex"] and rec["plex"]["thumb"]:
            data, _ = self.plex().image(rec["plex"]["thumb"], 1000, 1500)
            with open(dest, "wb") as fh:
                fh.write(data)
            return dest, "keep (embedded Plex poster)"
        return None, "keep"

    def save(self, i, raw_fields, poster):
        """Resolve the poster, then media_tag_io.safe_write(): safety copy ->
        write -> verify against original -> delete copy, or restore the
        original if anything fails. File only; never Plex."""
        with self.lock:
            rec = self.records.get(i)
            if rec is None:
                raise RuntimeError("file not loaded -- reload the page")
            path = rec["path"]
            f = clean_fields(raw_fields)
            errors = validate_fields(f)
            if errors:
                return {"ok": False, "errors": errors}
            row = {k: "" for k in CSV_FIELDS}
            row.update(timestamp=datetime.datetime.now().isoformat(timespec="seconds"), file=path,
                       rating_key=(rec["plex"] or {}).get("rating_key", ""))
            result = {"ok": False, "steps": [], "restored": False}
            log(f"SAVE start: {path}")
            self.prog_begin(rec["name"])
            try:
                if os.stat(path).st_size != rec["size"]:
                    raise RuntimeError("file changed on disk since it was loaded -- reload it first")
                self.prog_step("Prepare poster")
                cover, row["poster_action"] = self.resolve_poster(rec, poster)
                res = mt.safe_write(path, f, cover, SENTINEL_VALUE_UI, os.path.join(CACHE_DIR, rec["prefix"]),
                                    backup_dir=self.settings["backup_dir"].strip(), safety_tag="editor_safety",
                                    progress=_ProgressHooks(self))
                result.update(ok=res["ok"], restored=res["restored"], steps=res["steps"])
                row.update(safety_copy=res["safety_copy"], file_write=res["file_write"],
                           verification=res["verification"], restored=res["restored_text"], notes=res["error"])
            except Exception as exc:  # poster / pre-check problems -- the file was not touched
                self.prog_fail()
                self.prog_skip_pending(set(SAVE_STEPS) - {"Reload file"})
                result["steps"].append({"step": "save", "ok": False, "detail": str(exc)[:300]})
                row["notes"] = str(exc)[:300]
                log(f"FAIL:save:{path}:{exc}\n{traceback.format_exc()}")
            finally:
                write_header = not os.path.exists(PROGRESS_CSV)
                with open(PROGRESS_CSV, "a", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
                    if write_header:
                        w.writeheader()
                    w.writerow(row)
            log(f"SAVE {'PASS' if result['ok'] else 'FAIL'}: {path}")
            self.prog_step("Reload file")
            result["record"] = self.load(i)
            self.prog_finish()
            return result


class _ProgressHooks:
    """Adapts App's progress methods to media_tag_io.safe_write's hooks."""

    def __init__(self, app):
        self.step, self.bytes, self.pct = app.prog_step, app.prog_bytes, app.prog_pct
        self.fail, self.skip_pending = app.prog_fail, app.prog_skip_pending


def browse(path):
    path = os.path.abspath(path or "/mnt")
    if not os.path.isdir(path):
        path = "/"
    try:
        dirs = sorted((d for d in os.listdir(path)
                       if not d.startswith(".") and os.path.isdir(os.path.join(path, d))), key=str.lower)
    except PermissionError:
        dirs = []
    return {"path": path, "parent": os.path.dirname(path) if path != "/" else None, "dirs": dirs}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

APP = None
SERVER = None
PORT = None


def quit_server():
    """Stop the server from the UI's Quit button. Waits for any save/load in
    progress (they hold APP.lock), removes the launcher's PID file if it
    points at us, then shuts the HTTP server down from a separate thread."""
    with APP.lock:
        pid_file = os.path.join(DATA_DIR, f"plex_metadata_editor_{PORT}.pid")
        try:
            with open(pid_file) as fh:
                if fh.read().strip() == str(os.getpid()):
                    os.remove(pid_file)
        except OSError:
            pass
        log("Quit requested from the UI -- shutting down")
        threading.Thread(target=SERVER.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "PlexMetadataEditor/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the log file to meaningful events only

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                self.send_bytes(PAGE.encode(), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                with APP.lock:
                    self.send_json({"settings": {k: v for k, v in APP.settings.items() if k != "positions"},
                                    "files": APP.file_list(), "position": APP.position(),
                                    "status": APP.status})
            elif u.path == "/api/file":
                self.send_json(APP.load(int(q["i"])))
            elif u.path == "/api/image":
                data, ctype = APP.image(int(q["i"]), q.get("kind", "plex"))
                self.send_bytes(data, ctype)
            elif u.path == "/api/plexphoto":
                data, ctype = APP.plex().image(q["u"], 300, 450)
                self.send_bytes(data, ctype)
            elif u.path == "/api/plex_posters":
                self.send_json(APP.plex_posters(int(q["i"])))
            elif u.path == "/api/save_status":
                self.send_json(APP.save_status(q.get("job", "")))
            elif u.path == "/api/convert":
                self.send_json(APP.convert_state())
            elif u.path == "/api/browse":
                self.send_json(browse(q.get("path")))
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            log(f"ERROR GET {self.path}: {exc}\n{traceback.format_exc()}")
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self):
        # Custom header blocks cross-site form posts from other web pages
        # (browsers can't add it cross-origin without a CORS preflight we never answer).
        if self.headers.get("X-Editor") != "1":
            self.send_json({"error": "forbidden"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            u = urlparse(self.path)
            if u.path == "/api/settings":
                with APP.lock:
                    for k in ("directory", "plex_url", "plex_token", "backup_dir"):
                        if k in payload:
                            APP.settings[k] = str(payload[k]).strip()
                    for k in ("recursive",):
                        if k in payload:
                            APP.settings[k] = bool(payload[k])
                    APP.save_settings()
                    errors = APP.apply()
                    self.send_json({"errors": errors, "files": APP.file_list(),
                                    "position": APP.position(), "status": APP.status})
            elif u.path == "/api/test_plex":
                try:
                    secs = Plex(payload.get("plex_url"), payload.get("plex_token")).sections()
                    self.send_json({"ok": True, "sections": secs})
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)})
            elif u.path == "/api/convert/add":
                APP.queue_add(payload.get("paths") or [])
                self.send_json(APP.convert_state())
            elif u.path == "/api/convert/remove":
                APP.queue_remove(payload.get("paths") or [])
                self.send_json(APP.convert_state())
            elif u.path == "/api/convert/start":
                self.send_json({"error": "Conversion is coming soon -- the queue is saved and will be used then."}, 501)
            elif u.path == "/api/quit":
                quit_server()
                self.send_json({"ok": True})
            elif u.path == "/api/save":
                self.send_json(APP.start_save(int(payload["i"]), payload.get("fields"), payload.get("poster")))
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            log(f"ERROR POST {self.path}: {exc}\n{traceback.format_exc()}")
            self.send_json({"error": str(exc)}, 500)


PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Plex Metadata Editor</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px system-ui,sans-serif;background:#1e1f22;color:#ddd}
header{display:flex;gap:8px;align-items:center;padding:8px 12px;background:#2b2d31;border-bottom:1px solid #111;position:sticky;top:0;z-index:5}
header .grow{flex:1;min-width:0}
button{background:#3a3d44;color:#eee;border:1px solid #555;border-radius:4px;padding:5px 10px;cursor:pointer}
button:hover{background:#4a4e57}
button.primary{background:#e5a00d;color:#111;border-color:#e5a00d;font-weight:600}
button:disabled{opacity:.5;cursor:default}
select,input,textarea{background:#15161a;color:#ddd;border:1px solid #444;border-radius:4px;padding:5px;font:inherit}
textarea{width:100%;resize:vertical}
main{display:flex;gap:16px;padding:12px}
#left{width:320px;flex-shrink:0}
#left img{width:100%;border-radius:6px;background:#111;min-height:200px;display:block}
#left .btns{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
#right{flex:1;min-width:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px 14px}
label{display:block;font-size:12px;color:#aaa;margin-bottom:2px}
label input,label textarea{width:100%;display:block;margin-top:2px}
.span2{grid-column:span 2}.spanall{grid-column:1/-1}
fieldset{border:1px solid #3a3d44;border-radius:6px;margin:10px 0;padding:8px 10px}
legend{color:#e5a00d;padding:0 4px}
table{width:100%;border-collapse:collapse}td{padding:2px}th{padding:2px 8px}#convertModal td{padding:2px 8px}td input{width:100%}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.b-plex{background:#e5a00d;color:#111}.b-file{background:#4a90d9;color:#fff}.b-none{background:#777;color:#fff}
.muted{color:#888;font-size:12px}.warn{color:#f0a040}.ok{color:#5c5}.bad{color:#f55}
#status{font-size:13px;margin-bottom:10px;padding:8px 10px;background:#25272c;border:1px solid #3a3d44;border-radius:6px}
#status:empty{display:none}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:10}
.modal.show{display:flex}
.box{background:#2b2d31;border-radius:8px;padding:16px;width:min(720px,95vw);max-height:90vh;overflow:auto}
.box label{margin-bottom:8px}
.row{display:flex;gap:6px;align-items:center}.row input[type=text],.row input[type=password]{flex:1}
#dirlist div{padding:3px 6px;cursor:pointer;border-radius:3px}#dirlist div:hover{background:#3a3d44}
#posterGrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
#posterGrid img{width:100%;cursor:pointer;border:3px solid transparent;border-radius:4px}
#posterGrid img.sel{border-color:#e5a00d}
.dirty{color:#e5a00d}
.soon{background:#5b3fa0;color:#fff;border-radius:10px;padding:1px 8px;font-size:11px;font-weight:700;margin-left:6px}
.mk{display:inline-block;font-size:10px;font-weight:700;border-radius:8px;padding:0 6px;margin-left:6px;cursor:help;vertical-align:1px}
.mk-new{background:#2e7d32;color:#fff}.mk-chg{background:#b26a00;color:#fff}.mk-rem{background:#8e2b2b;color:#fff}
.is-new{border-color:#3fa046 !important;box-shadow:0 0 0 1px #3fa046}.is-chg{border-color:#d08a1a !important;box-shadow:0 0 0 1px #d08a1a}
#diffSummary{margin:0 0 8px;font-size:12px}
.pstep{margin:3px 0;color:#888}.pstep .pi{display:inline-block;width:18px}
.ps-done{color:#5c5}.ps-active{color:#eee;font-weight:600}.ps-failed{color:#f55}.ps-skipped{color:#666}
.pbar{height:10px;background:#15161a;border:1px solid #444;border-radius:5px;overflow:hidden;margin:4px 0 2px 18px;max-width:560px}
.pbar>div{height:100%;background:#e5a00d;transition:width .4s}
.pbar.ind>div{width:30%;animation:pind 1.2s ease-in-out infinite alternate}
@keyframes pind{from{margin-left:0}to{margin-left:70%}}
.ps-active .muted{margin-left:18px;font-weight:400}
button.quit{background:#6b2020;border-color:#8e2b2b}button.quit:hover{background:#8e2b2b}
</style></head><body>
<header>
  <button onclick="openSettings()" title="Settings">&#9881; Settings</button>
  <span style="color:#e5a00d;background:#15161a;border:1px solid #3a3d44;border-radius:4px;font-size:11px;font-weight:bold;padding:2px 6px;" title="Application Version">v1.0.0</span>
  <button onclick="openConvert()" title="Queue other formats for MKV conversion">Convert <span class="soon">SOON</span></button>
  <button onclick="nav(-1)">&#9664; Prev</button>
  <select id="filesel" onchange="go(+this.value)" style="max-width:45vw"></select>
  <button onclick="nav(1)">Next &#9654;</button>
  <span id="count" class="muted"></span>
  <span class="grow"></span>
  <span id="dirty" class="dirty"></span>
  <button onclick="revert()">Revert</button>
  <button onclick="save(false)" id="saveBtn">Save</button>
  <button class="primary" onclick="save(true)" id="saveNextBtn">Save + Next</button>
  <button class="quit" onclick="quitApp()" title="Stop the editor server">&#x23FB; Quit</button>
</header>
<main id="main" style="display:none">
  <div id="left">
    <img id="poster" alt="poster">
    <div class="muted" id="posterInfo"></div>
    <div class="btns">
      <button onclick="document.getElementById('upload').click()">Upload image</button>
      <input type="file" id="upload" accept="image/*" style="display:none" onchange="uploadPoster(this)">
      <button id="plexPosterBtn" onclick="openPlexPosters()">Choose from Plex</button>
      <button onclick="useFrame()">Use video frame</button>
      <button onclick="resetPoster()">Undo poster</button>
    </div>
    <div class="muted" style="margin-top:10px;word-break:break-all" id="pathInfo"></div>
  </div>
  <div id="right">
    <div id="status"></div>
    <div class="row" style="margin-bottom:8px">
      <span>Source:</span><span id="source" class="badge"></span>
      <span id="taggedBy" style="margin-left:10px;font-size:12px"></span>
      <span class="grow"></span>
      <button id="usePlex" onclick="loadFrom('plex_fields')">Load Plex values</button>
      <button id="useFile" onclick="loadFrom('file_fields')">Load file values</button>
    </div>
    <div id="warnings" class="warn"></div>
    <div id="diffSummary"></div>
    <form id="form" onsubmit="return false" oninput="markDirty()">
      <div class="grid">
        <label class="span2">Title<input name="title"></label>
        <label>Sort title<input name="sort_title"></label>
        <label>Original title<input name="original_title"></label>
        <label class="spanall">Tagline<input name="tagline"></label>
        <label class="spanall">Summary<textarea name="summary" rows="5"></textarea></label>
        <label>Content rating<input name="content_rating" placeholder="e.g. PG-13, TV-14"></label>
        <label>Release date<input name="release_date" placeholder="YYYY-MM-DD"></label>
        <label>Year<input name="year"></label>
        <label>Studio<input name="studio"></label>
        <label>IMDb ID<input name="imdb"></label>
        <label>TMDb ID<input name="tmdb"></label>
        <label>TVDb ID<input name="tvdb"></label>
      </div>
      <fieldset><legend>TV episode</legend>
        <div class="grid">
          <label>Show<input name="show_title"></label>
          <label>Season<input name="season"></label>
          <label>Episode<input name="episode"></label>
        </div>
      </fieldset>
      <fieldset><legend>Lists <span class="muted">(one per line)</span></legend>
        <div class="grid">
          <label>Genres<textarea name="genres" rows="4"></textarea></label>
          <label>Collections<textarea name="collections" rows="4"></textarea></label>
          <label>Countries<textarea name="countries" rows="4"></textarea></label>
          <label>Directors<textarea name="directors" rows="4"></textarea></label>
          <label>Writers<textarea name="writers" rows="4"></textarea></label>
          <label>Producers<textarea name="producers" rows="4"></textarea></label>
        </div>
      </fieldset>
      <fieldset><legend>Cast</legend>
        <table><thead><tr><th align=left>Actor</th><th align=left>Character</th><th></th></tr></thead><tbody id="roles"></tbody></table>
        <button type="button" onclick="addRow('roles',{actor:'',character:''})">+ Add actor</button>
      </fieldset>
      <fieldset id="extraBox"><legend>Other file tags <span class="muted">(MKV global tags not covered above)</span></legend>
        <table><thead><tr><th align=left>Name</th><th align=left>Value</th><th></th></tr></thead><tbody id="extra_tags"></tbody></table>
        <button type="button" onclick="addRow('extra_tags',{name:'',value:''})">+ Add tag</button>
      </fieldset>
    </form>
  </div>
</main>
<div id="empty" style="padding:40px;text-align:center" class="muted">Open Settings and choose a directory to begin.</div>

<div class="modal" id="settingsModal"><div class="box">
  <h3 style="margin-top:0">Settings <span class="muted" style="font-weight:normal;">(v1.0.0)</span></h3>
  <div class="muted" style="margin-bottom:8px">Plex is read-only: metadata and posters are read from Plex, edits are saved to the media file only.</div>
  <label>Directory<div class="row"><input type="text" id="s_directory"><button onclick="browseFor('s_directory')">Browse</button></div></label>
  <label>Plex URL<input type="text" id="s_plex_url"></label>
  <label>Plex token<div class="row"><input type="password" id="s_plex_token"><button onclick="toggleToken()">Show</button></div></label>
  <label>Keep backups in <span class="muted">(blank = temporary safety copy next to the file, deleted after a verified save)</span><div class="row"><input type="text" id="s_backup_dir"><button onclick="browseFor('s_backup_dir')">Browse</button></div></label>
  <label><input type="checkbox" id="s_recursive" style="width:auto;display:inline"> Include subfolders</label>
  <div id="browser" style="display:none;border:1px solid #444;border-radius:4px;padding:6px;margin:8px 0">
    <div class="row"><b id="browsePath" style="flex:1;word-break:break-all"></b><button onclick="pickDir()">Use this folder</button></div>
    <div id="dirlist" style="max-height:260px;overflow:auto;margin-top:6px"></div>
  </div>
  <div id="settingsMsg" class="muted" style="margin:8px 0"></div>
  <div class="row"><button onclick="testPlex()">Test Plex connection</button><span class="grow" style="flex:1"></span>
    <button onclick="closeModal('settingsModal')">Close</button><button class="primary" onclick="applySettings()">Apply</button></div>
</div></div>

<div class="modal" id="convertModal"><div class="box" style="width:min(1000px,96vw)">
  <div class="row"><h3 style="margin:0">Convert to MKV</h3><span class="soon">COMING SOON</span><span style="flex:1"></span><button onclick="closeModal('convertModal')">Close</button></div>
  <p class="muted">Files in the current folder that aren't .mkv/.mp4/.m4v. Queue them now; the conversion job itself isn't built yet. The queue is saved between runs and covers all folders.</p>
  <fieldset><legend>Other-format files in this folder <span class="muted" id="candCount"></span></legend>
    <div class="row" style="margin-bottom:6px">
      <button onclick="candSelect('all')">Select all</button><button onclick="candSelect('avi')">Select all AVI</button><button onclick="candSelect('none')">Select none</button>
      <span style="flex:1"></span><button class="primary" onclick="queueAdd()">Add selected to queue</button>
    </div>
    <div style="max-height:30vh;overflow:auto"><table><thead><tr><th></th><th align=left>File</th><th align=left>Type</th><th align=right>Size</th><th>Plex</th><th></th></tr></thead><tbody id="candRows"></tbody></table></div>
  </fieldset>
  <fieldset><legend>Queue <span class="muted" id="queueCount"></span></legend>
    <div style="max-height:25vh;overflow:auto"><table><thead><tr><th align=left>File</th><th align=right>Size</th><th align=left>Status</th><th align=left>Added</th><th></th></tr></thead><tbody id="queueRows"></tbody></table></div>
    <div class="row" style="margin-top:8px"><button onclick="queueClear()">Clear queue</button><span style="flex:1"></span>
      <button disabled title="Coming soon">&#9654; Start queue</button><span class="soon">COMING SOON</span></div>
  </fieldset>
  <fieldset><legend>Planned conversion steps (per queued file)</legend>
    <ol class="muted" style="margin:0;padding-left:20px;line-height:1.5">
      <li><b>Disk check</b> &mdash; confirm enough free space before starting each file.</li>
      <li><b>Convert into a staging folder</b> (original untouched) with the mkv_convert.py pipeline: HEVC via the Intel GPU (VAAPI), full hardware path for h264/hevc/mpeg2, software decode + hardware encode for old codecs (XviD/DivX etc); audio and subtitles copied unchanged.</li>
      <li><b>Heavy verification</b> (same as mkv_convert.py): duration matches, audio stream count matches, full decode of the entire new file.</li>
      <li><b>Promote</b> (same as mkv_promote.py): only PASS results; place the new .mkv in the original's folder under the same name with a .mkv extension; confirm the copy's size; clear the staging copy.</li>
      <li><b>Remove the original</b> file (planned setting: delete, or archive like mkv_promote.py does &mdash; to decide when built).</li>
      <li><b>Metadata</b> &mdash; the new .mkv shows up in this editor to load Plex metadata, edit, and save to the file as usual.</li>
    </ol>
  </fieldset>
</div></div>

<div class="modal" id="posterModal"><div class="box">
  <h3 style="margin-top:0">Plex posters <span class="muted">(click to use)</span></h3>
  <div id="posterGrid"></div>
  <div class="row" style="margin-top:10px"><span style="flex:1"></span><button onclick="closeModal('posterModal')">Close</button></div>
</div></div>

<script>
let S = {files: [], rec: null, poster: {action: 'keep'}, dirty: false, browseTarget: null};
const $ = id => document.getElementById(id);
const SCALARS = ['title','sort_title','original_title','tagline','summary','content_rating','release_date','year','studio','imdb','tmdb','tvdb','show_title','season','episode'];
const LISTS = ['genres','collections','countries','directors','writers','producers'];
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

async function api(path, body){
  const opt = body===undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Editor':'1'}, body:JSON.stringify(body)};
  const r = await fetch(path, opt); const j = await r.json();
  if(!r.ok) throw new Error(j.error||r.statusText); return j;
}
function setStatus(html){ $('status').innerHTML = html; if(html) window.scrollTo({top:0, behavior:'smooth'}); }
function markDirty(){ S.dirty = true; $('dirty').textContent = 'unsaved changes'; markChanges(); }

// ---- NEW / CHANGED / REMOVED markers vs what's in the file right now ----
function mark(host, before, cls, text, title){
  let m = host.querySelector(':scope > .mk-'+cls);
  if(!text){ if(m) m.remove(); return; }
  if(!m){ m = document.createElement('span'); m.className = 'mk mk-'+cls; host.insertBefore(m, before); }
  m.textContent = text; m.title = title||'';
}
function listOf(v){ const out=[]; (Array.isArray(v)?v:String(v||'').split('\n')).forEach(x=>{x=String(x).trim(); if(x && !out.includes(x)) out.push(x);}); return out; }
function markChanges(){
  if(!S.rec) return;
  const base = S.rec.file_fields || {}, form = $('form'); let nNew=0, nChg=0, nRem=0;
  SCALARS.forEach(k=>{
    const el = form.elements[k], lab = el.closest('label');
    const cur = el.value.trim(), old = String(base[k]||'').trim();
    const isNew = cur && !old, isChg = cur && old && cur!==old, isRem = !cur && old;
    mark(lab, el, 'new', isNew?'NEW':'', 'The file does not have this yet — it will be added on Save');
    mark(lab, el, 'chg', isChg?'CHANGED':'', 'In the file now: '+old);
    mark(lab, el, 'rem', isRem?'REMOVED':'', 'In the file now (will be removed on Save): '+old);
    el.classList.toggle('is-new', !!isNew); el.classList.toggle('is-chg', !!(isChg||isRem));
    nNew+=isNew?1:0; nChg+=isChg?1:0; nRem+=isRem?1:0;
  });
  LISTS.forEach(k=>{
    const el = form.elements[k], lab = el.closest('label');
    const cur = listOf(el.value), old = listOf(base[k]);
    const add = cur.filter(x=>!old.includes(x)), rem = old.filter(x=>!cur.includes(x));
    mark(lab, el, 'new', add.length?`+${add.length} NEW`:'', 'Not in the file yet: '+add.join(', '));
    mark(lab, el, 'rem', rem.length?`−${rem.length} REMOVED`:'', 'In the file now, will be removed: '+rem.join(', '));
    el.classList.toggle('is-new', add.length>0 && !old.length); el.classList.toggle('is-chg', (add.length>0 && old.length>0) || rem.length>0);
    nNew+=add.length; nRem+=rem.length;
  });
  const key = r=>(r.actor||'').trim()+'\u0001'+(r.character||'').trim();
  const oldRoles = (base.roles||[]).map(key), rows = [...$('roles').querySelectorAll('tr')];
  const curRoles = rows.map(tr=>{ const o={}; tr.querySelectorAll('input').forEach(i=>o[i.dataset.k]=i.value); return o; });
  let rAdd=0;
  rows.forEach((tr,j)=>{ const k=key(curRoles[j]), isNew = k!=='\u0001' && !oldRoles.includes(k); tr.querySelectorAll('input').forEach(i=>i.classList.toggle('is-new', isNew)); rAdd+=isNew?1:0; });
  const curKeys = curRoles.map(key), rRem = oldRoles.filter(k=>!curKeys.includes(k)).length;
  const legend = $('roles').closest('fieldset').querySelector('legend');
  mark(legend, null, 'new', rAdd?`+${rAdd} NEW`:'', 'Cast entries not in the file yet');
  mark(legend, null, 'rem', rRem?`−${rRem} REMOVED`:'', 'Cast entries in the file now that will be removed');
  nNew+=rAdd; nRem+=rRem;
  const parts = [];
  if(nNew) parts.push(`<span class="mk mk-new">${nNew} NEW</span>`);
  if(nChg) parts.push(`<span class="mk mk-chg">${nChg} CHANGED</span>`);
  if(nRem) parts.push(`<span class="mk mk-rem">${nRem} REMOVED</span>`);
  $('diffSummary').innerHTML = parts.length
    ? `Compared with what's in the file now:${parts.join('')} <span class=muted>— green = file doesn't have it yet, orange = file has a different value (hover a tag to see it). Applied on Save.</span>`
    : '<span class=muted>Matches what is in the file now — nothing new to save.</span>';
}

async function quitApp(){
  if(!confirm(S.dirty ? 'You have UNSAVED changes that will be lost.\n\nQuit the editor anyway?' : 'Quit the editor? This stops the server.')) return;
  setStatus('Stopping… (waits for any save in progress to finish)');
  try { await api('/api/quit', {}); } catch(e) {}
  S.dirty = false;
  document.body.innerHTML = '<div style="padding:80px;text-align:center;font-size:18px">Editor stopped — you can close this window.<div class="muted" style="margin-top:12px">Start it again with ~/myScripts/start_plex_metadata_editor.sh</div></div>';
}
function clearDirty(){ S.dirty = false; $('dirty').textContent = ''; }
function confirmLeave(){ return !S.dirty || confirm('Discard unsaved changes?'); }

async function init(){
  const st = await api('/api/state');
  fillSettings(st.settings); setFiles(st.files);
  if(!st.settings.directory){ openSettings(); return; }
  if(st.status.plex_ok===false) setStatus('<span class=warn>'+esc(st.status.plex_message)+'</span>');
  if(S.files.length) go(st.position, true);
  if(location.hash==='#convert') openConvert();
}
function setFiles(files){
  S.files = files;
  $('filesel').innerHTML = files.map(f=>`<option value="${f.i}">${f.in_plex?'● ':'○ '}${esc(f.rel)}</option>`).join('');
  $('main').style.display = files.length ? 'flex' : 'none';
  $('empty').style.display = files.length ? 'none' : 'block';
  $('empty').textContent = files.length ? '' : 'No .mkv/.mp4/.m4v files found. Open Settings to choose a directory, or Convert to see other formats here.';
}
function nav(d){ if(S.rec) go(Math.min(S.files.length-1, Math.max(0, S.rec.i+d))); }
async function go(i, force){
  if(!force && !confirmLeave()) { if(S.rec) $('filesel').value = S.rec.i; return; }
  setStatus('Loading…');
  try { show(await api('/api/file?i='+i)); setStatus(''); }
  catch(e){ setStatus('<span class=bad>Load failed: '+esc(e.message)+'</span>'); }
}
function show(rec){
  S.rec = rec; S.poster = {action:'keep'};
  $('filesel').value = rec.i;
  $('count').textContent = `${rec.i+1} of ${rec.total}`;
  $('source').textContent = rec.source; $('source').className = 'badge b-'+rec.source;
  $('pathInfo').textContent = rec.path + ' (' + (rec.size/1073741824).toFixed(2) + ' GB)' + (rec.plex ? ` — Plex ${rec.plex.type} #${rec.plex.rating_key}` : ' — not in Plex');
  $('warnings').innerHTML = rec.warnings.map(esc).join('<br>');
  $('usePlex').style.display = rec.plex_fields ? '' : 'none';
  $('plexPosterBtn').style.display = rec.plex ? '' : 'none';
  $('extraBox').style.display = rec.ext==='.mkv' ? '' : 'none';
  const tb = rec.tagged_by;
  $('taggedBy').innerHTML = tb ? `Previously tagged by <b>${esc(tb.source)}</b> <span class=muted>(PLEX_METADATA_SYNCED=${esc(tb.value)})</span>` : '<span class=muted>Never tagged by the batch script or this editor</span>';
  fillForm(rec.fields); showPoster(); clearDirty(); markChanges();
}
function fillForm(f){
  const form = $('form');
  SCALARS.forEach(k => form.elements[k].value = f[k]||'');
  LISTS.forEach(k => form.elements[k].value = (f[k]||[]).join('\n'));
  $('roles').innerHTML=''; (f.roles||[]).forEach(r=>addRow('roles', r, true));
  $('extra_tags').innerHTML=''; (f.extra_tags||[]).forEach(t=>addRow('extra_tags', t, true));
}
function addRow(tbody, obj, quiet){
  const keys = Object.keys(obj); const tr = document.createElement('tr');
  tr.innerHTML = keys.map(k=>`<td><input data-k="${k}" value="${esc(obj[k])}"></td>`).join('') + '<td width=30><button type=button title=Remove>✕</button></td>';
  tr.querySelector('button').onclick = ()=>{ tr.remove(); markDirty(); };
  $(tbody).appendChild(tr); if(!quiet) markDirty();
}
function readRows(tbody){
  return [...$(tbody).querySelectorAll('tr')].map(tr=>{ const o={}; tr.querySelectorAll('input').forEach(i=>o[i.dataset.k]=i.value); return o; });
}
function readForm(){
  const form = $('form'), f = {};
  SCALARS.forEach(k => f[k] = form.elements[k].value);
  LISTS.forEach(k => f[k] = form.elements[k].value.split('\n'));
  f.roles = readRows('roles'); f.extra_tags = readRows('extra_tags'); return f;
}
function loadFrom(which){
  const src = S.rec[which]; if(!src) return;
  const f = Object.assign({}, src); f.extra_tags = readRows('extra_tags');
  fillForm(f); markDirty();
}
function revert(){ if(S.rec && confirmLeave()) show(S.rec); }

// ---- poster ----
function showPoster(){
  const labels = {plex:'Plex poster', embedded:'Embedded in file', frame:'Video frame (no poster found)'};
  if(S.poster.action==='keep'){ $('poster').src = S.rec.poster_url; $('posterInfo').textContent = labels[S.rec.poster_kind] + (S.rec.has_embedded_cover ? '' : ' — not yet embedded in file'); }
  else if(S.poster.action==='upload'){ $('poster').src = S.poster.data; $('posterInfo').textContent = 'New: uploaded image (saved on Save)'; }
  else if(S.poster.action==='plex'){ $('poster').src = '/api/plexphoto?u='+encodeURIComponent(S.poster.thumb); $('posterInfo').textContent = 'New: Plex alternate poster (saved on Save)'; }
  else if(S.poster.action==='frame'){ $('poster').src = S.rec.frame_url; $('posterInfo').textContent = 'New: video frame (saved on Save)'; }
}
function uploadPoster(input){
  const file = input.files[0]; if(!file) return;
  const rd = new FileReader();
  rd.onload = ()=>{ S.poster = {action:'upload', data: rd.result}; showPoster(); markDirty(); input.value=''; };
  rd.readAsDataURL(file);
}
function useFrame(){ S.poster = {action:'frame'}; showPoster(); markDirty(); }
function resetPoster(){ S.poster = {action:'keep'}; showPoster(); }
async function openPlexPosters(){
  $('posterGrid').innerHTML = 'Loading…'; $('posterModal').classList.add('show');
  try{
    const list = await api('/api/plex_posters?i='+S.rec.i);
    $('posterGrid').innerHTML = list.length ? '' : 'No posters available in Plex.';
    list.forEach(p=>{
      const img = document.createElement('img'); img.src = '/api/plexphoto?u='+encodeURIComponent(p.thumb);
      img.title = p.provider + (p.selected ? ' (current)' : ''); if(p.selected) img.className='sel';
      img.onclick = ()=>{ S.poster = {action:'plex', key:p.key, thumb:p.thumb}; showPoster(); markDirty(); closeModal('posterModal'); };
      $('posterGrid').appendChild(img);
    });
  }catch(e){ $('posterGrid').textContent = 'Failed: '+e.message; }
}

// ---- save ----
// ---- save (runs as a background job on the server; progress polled every 0.5s) ----
const sleep = ms => new Promise(r => setTimeout(r, ms));
function fmtTime(s){ s = Math.max(0, Math.round(s)); return s >= 3600 ? `${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m` : `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`; }
function setBusy(on){ document.querySelectorAll('header button:not(.quit), header select').forEach(b => b.disabled = on); }
function renderProgress(p){
  if(!p) return;
  const icon = {done:'✔', active:'▶', failed:'✘', skipped:'–', pending:'·'};
  const rows = p.steps.map(s => {
    let extra = '';
    if(s.state === 'active'){
      if(p.pct !== null){
        const det = p.bytes_total ? ` · ${fmtSize(p.bytes_done)} of ${fmtSize(p.bytes_total)} · ${fmtSize(p.rate)}/s` + (p.eta !== null ? ` · about ${fmtTime(p.eta)} left` : '') : '';
        extra = `<div class="pbar"><div style="width:${p.pct.toFixed(1)}%"></div></div><div class="muted">${p.pct.toFixed(0)}%${det}</div>`;
      } else {
        extra = `<div class="pbar ind"><div></div></div><div class="muted">working… ${fmtTime(p.step_elapsed)}</div>`;
      }
    }
    return `<div class="pstep ps-${s.state}"><span class="pi">${icon[s.state]||''}</span>${esc(s.name)}${extra}</div>`;
  }).join('');
  $('status').innerHTML = `<b>Saving ${esc(p.name)}</b> <span class="muted">· ${fmtTime(p.elapsed)} elapsed — please wait, don't close this window</span>${rows}`;
}
async function save(next){
  if(!S.rec) return;
  setBusy(true);
  setStatus('<b>Starting save…</b>');
  try{
    const start = await api('/api/save', {i:S.rec.i, fields:readForm(), poster:S.poster});
    if(start.errors){ setStatus('<span class=bad>'+start.errors.map(esc).join('<br>')+'</span>'); return; }
    let r = null;
    while(!r){
      await sleep(500);
      const st = await api('/api/save_status?job='+start.job);
      if(st.state === 'unknown') throw new Error('lost track of the save job (was the server restarted?)');
      if(st.state === 'done') r = st.result; else renderProgress(st.progress);
    }
    const lines = r.steps.map(s=>`<div class="${s.ok?'ok':'bad'}">${s.ok?'✔':'✘'} ${esc(s.step)}${s.detail?': <span class=muted>'+esc(s.detail)+'</span>':''}</div>`).join('');
    clearDirty(); if(r.record) show(r.record);
    setStatus(`<b class="${r.ok?'ok':'bad'}">${r.ok?'Saved to file and verified':(r.restored?'Save failed — original file restored':'Save failed')}</b>`+lines);
    if(next && r.ok && r.record.i < S.files.length-1) { const html = $('status').innerHTML; await go(r.record.i+1, true); setStatus('<div class=muted>Previous file:</div>'+html); }
  }catch(e){ setStatus('<span class=bad>Save failed: '+esc(e.message)+'</span>'); }
  finally{ setBusy(false); }
}
document.addEventListener('keydown', e=>{ if((e.ctrlKey||e.metaKey) && e.key==='s'){ e.preventDefault(); save(false); } });
window.addEventListener('beforeunload', e=>{ if(S.dirty){ e.preventDefault(); e.returnValue=''; } });

// ---- convert queue (tracking only; conversion coming soon) ----
function fmtSize(b){ return b>=1073741824 ? (b/1073741824).toFixed(2)+' GB' : b>=1048576 ? (b/1048576).toFixed(0)+' MB' : Math.ceil(b/1024)+' KB'; }
async function openConvert(){ $('convertModal').classList.add('show'); renderConvert(await api('/api/convert')); }
function renderConvert(c){
  S.convert = c;
  $('candCount').textContent = `(${c.candidates.length})`;
  $('candRows').innerHTML = c.candidates.length ? c.candidates.map((f,k)=>`<tr><td width=24><input type=checkbox data-k="${k}" ${f.queued?'disabled':''}></td><td style="word-break:break-all">${esc(f.rel)}</td><td>${esc(f.ext)}</td><td align=right>${fmtSize(f.size)}</td><td align=center>${f.in_plex?'●':'○'}</td><td class=muted>${f.queued?'queued':''}</td></tr>`).join('')
    : '<tr><td colspan=6 class=muted>No other-format video files in this folder.</td></tr>';
  const total = c.queue.reduce((a,q)=>a+(q.size||0),0);
  $('queueCount').textContent = `(${c.queue.length} files, ${fmtSize(total)})`;
  $('queueRows').innerHTML = c.queue.length ? c.queue.map((q,k)=>`<tr><td style="word-break:break-all">${esc(q.path)}</td><td align=right>${fmtSize(q.size||0)}</td><td class="${q.exists?'':'bad'}">${q.exists?esc(q.status):'missing'}</td><td class=muted>${esc(q.added)}</td><td width=30><button onclick="queueRemove(${k})" title=Remove>✕</button></td></tr>`).join('')
    : '<tr><td colspan=5 class=muted>Queue is empty.</td></tr>';
}
function candSelect(mode){
  document.querySelectorAll('#candRows input[type=checkbox]:not(:disabled)').forEach(cb=>{
    const f = S.convert.candidates[+cb.dataset.k];
    cb.checked = mode==='all' || (mode==='avi' && f.ext==='avi');
  });
}
async function queueAdd(){
  const paths = [...document.querySelectorAll('#candRows input:checked')].map(cb=>S.convert.candidates[+cb.dataset.k].path);
  if(!paths.length) return;
  renderConvert(await api('/api/convert/add', {paths}));
}
async function queueRemove(k){ renderConvert(await api('/api/convert/remove', {paths:[S.convert.queue[k].path]})); }
async function queueClear(){
  if(!S.convert.queue.length || !confirm('Remove all files from the queue? (Files on disk are not touched.)')) return;
  renderConvert(await api('/api/convert/remove', {paths:S.convert.queue.map(q=>q.path)}));
}

// ---- settings ----
function fillSettings(s){
  ['directory','plex_url','plex_token','backup_dir'].forEach(k=>$('s_'+k).value = s[k]||'');
  $('s_recursive').checked = !!s.recursive;
}
function openSettings(){ $('settingsMsg').textContent=''; $('browser').style.display='none'; $('settingsModal').classList.add('show'); }
function closeModal(id){ $(id).classList.remove('show'); }
function toggleToken(){ const t=$('s_plex_token'); t.type = t.type==='password'?'text':'password'; }
function readSettings(){
  return {directory:$('s_directory').value, plex_url:$('s_plex_url').value, plex_token:$('s_plex_token').value,
          backup_dir:$('s_backup_dir').value, recursive:$('s_recursive').checked};
}
async function testPlex(){
  $('settingsMsg').textContent = 'Testing…';
  const r = await api('/api/test_plex', readSettings());
  $('settingsMsg').innerHTML = r.ok ? `<span class=ok>Connected. Sections: ${r.sections.map(s=>esc(s.title)+' ('+esc(s.type)+')').join(', ')}</span>` : `<span class=bad>Failed: ${esc(r.error)}</span>`;
}
async function applySettings(){
  if(!confirmLeave()) return;
  $('settingsMsg').textContent = 'Scanning folder and indexing Plex…';
  try{
    const r = await api('/api/settings', readSettings());
    setFiles(r.files); clearDirty(); S.rec = null;
    $('settingsMsg').innerHTML = (r.errors.length ? '<span class=warn>'+r.errors.map(esc).join('<br>')+'</span><br>' : '') + `${r.files.length} files. ${esc(r.status.plex_message)}`;
    if(r.files.length){ closeModal('settingsModal'); go(r.position, true); if(r.errors.length) setStatus('<span class=warn>'+r.errors.map(esc).join('<br>')+'</span>'); }
  }catch(e){ $('settingsMsg').innerHTML = '<span class=bad>'+esc(e.message)+'</span>'; }
}
async function browseFor(target){ S.browseTarget = target; $('browser').style.display='block'; await browseTo($(target).value || '/mnt'); }
async function browseTo(path){
  const r = await api('/api/browse?path='+encodeURIComponent(path));
  $('browsePath').textContent = r.path; $('browsePath').dataset.path = r.path;
  const items = (r.parent!==null ? [{n:'↑ ..', p:r.parent}] : []).concat(r.dirs.map(d=>({n:'📁 '+d, p:(r.path==='/'?'':r.path)+'/'+d})));
  $('dirlist').innerHTML = '';
  items.forEach(it=>{ const d=document.createElement('div'); d.textContent=it.n; d.onclick=()=>browseTo(it.p); $('dirlist').appendChild(d); });
}
function pickDir(){ $(S.browseTarget).value = $('browsePath').dataset.path; $('browser').style.display='none'; }
init().catch(e=>setStatus('<span class=bad>'+esc(e.message)+'</span>'));
</script>
</body></html>
"""


def main():
    parser = argparse.ArgumentParser(description="Local web UI for editing video metadata (file tags + Plex).")
    parser.add_argument("--port", type=int, default=8765, help="web server port (default 8765)")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="listen address (default 127.0.0.1; UI has no login -- see usage header)")
    args = parser.parse_args()

    try:
        import setproctitle
        setproctitle.setproctitle(f"plex metadata web editor (port {args.port})")
        if hasattr(setproctitle, "setthreadtitle"):
            setproctitle.setthreadtitle("plex-web-edit")
    except ImportError:
        pass

    os.makedirs(DATA_DIR, exist_ok=True)
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    global APP, SERVER, PORT
    mt.set_logger(log)
    APP = App()
    APP.apply()
    PORT = args.port
    SERVER = ThreadingHTTPServer((args.bind, args.port), Handler)
    log(f"Plex Metadata Editor listening on http://{args.bind}:{args.port}")
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
    SERVER.server_close()
    log("Server stopped")


if __name__ == "__main__":
    main()
