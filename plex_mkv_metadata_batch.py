#!/usr/bin/python3
"""
plex_mkv_metadata_batch.py

REQUIRES: /usr/bin/python3 (has `requests` + `mutagen` installed via apt).
  Do NOT run as `python3 plex_mkv_metadata_batch.py` if your shell's
  `python3` resolves to something else (e.g. linuxbrew) -- invoke it
  explicitly as `/usr/bin/python3 plex_mkv_metadata_batch.py ...` or via
  `./plex_mkv_metadata_batch.py` (uses this file's shebang).

PURPOSE
  Batch version of plex_mkv_metadata_test.py: finds every .mkv or .mp4 file
  under a given folder (recursive) that Plex also has a library entry for,
  and for each one pulls that item's Plex metadata (title, summary, genre,
  cast, crew, external IDs, poster) and embeds it into the file's own
  container metadata -- Matroska tags + cover.jpg attachment via
  mkvpropedit for .mkv, iTunes/freeform atoms + cover art via mutagen for
  .mp4.

  Writing and verification use media_tag_io.py -- the SAME code as the UI
  editor (plex_metadata_editor.py): safety copy -> write -> quick checks
  against the original -> delete the safety copy, or automatically RESTORE
  the original if any check fails. Unrelated tags already in an MKV (e.g.
  ENCODER) are kept. Plex library sections are looked up automatically
  (no hard-coded section numbers).

  Discovery is automatic: queries the Plex Movies section (id 2) once, and
  matches every item whose Media/Part/@file starts with --folder and ends
  in .mkv or .mp4. No manual file list to maintain.

  Every file this batch tags gets PLEX_METADATA_SYNCED=
  plex_metadata_editor_batch. Files are SKIPPED (safe to re-run/resume a
  partial batch) unless --force is passed, when the tag is:
    plex_metadata_editor_batch -- tagged by this batch script
    plex_metadata_editor_ui    -- tagged by hand in plex_metadata_editor.py
                                  (the UI tool)
  Any OTHER value -- notably the old "1" written by earlier versions of
  this script -- is RE-TAGGED on purpose (brings older files up to the
  current pipeline). The CSV notes/log say what was found.

CHECKS (edited file vs its safety copy; seconds, no full decode)
  duration_check    : duration unchanged (ffprobe)
  stream_check      : same video/audio/subtitle streams (codec, resolution,
                      channels, sample rate)
  packet_md5_check  : MD5 of 20s of raw audio/video packets at the start
                      and middle of the file -- proves the media data
                      itself is byte-identical
  size_check        : size changed by no more than tags + cover can explain
  structural_check  : container re-parses cleanly (mkvmerge / ffprobe)
  tag_check         : every tag reads back exactly as written, incl.
                      PLEX_METADATA_SYNCED=plex_metadata_editor_batch
  cover_check       : cover present (when a Plex poster was embedded)
  decode_check      : OPT-IN (--decode-check) full CPU decode of the whole
                      file -- slow; tests SOURCE file health, not anything
                      the tag write could cause. See decode_audit.py for a
                      separate corruption sweep.
  (Replaces the old mkvinfo -v tag/attachment checks, which read the whole
  file twice -- ~53s each on a 9.5GB MKV.)

USAGE
  python3 plex_mkv_metadata_batch.py [options]

ARGUMENTS
  --folder       Root folder to scan (recursive). Only files under this path
                 are matched against Plex's library.
  --plex-url     Base URL of the Plex server. Default: http://127.0.0.1:32400
  --plex-token   Plex X-Plex-Token (or set PLEX_TOKEN environment variable).
  --backup-dir   Optional. If set, the safety copy of each original is made
                 here and KEPT (<name>.bak.<timestamp>). If blank (default),
                 a temporary hidden safety copy is made next to the file
                 and deleted once the file passes all checks.
  --no-dry-run   Actually perform safety copies + writes. Without this
                 flag the script only discovers matching files, prints the
                 planned action for each, and makes no changes.
  --limit        Process only the first N matched files (0 = no limit).
                 Useful for staged rollout, e.g. --limit 3 to prove the
                 batch loop before committing to the full folder.
  --force        Re-tag files even if they already carry the sentinel tag
                 (normally these are skipped, which makes reruns safe).
                 WARNING: this also overwrites files hand-edited in the UI
                 tool (plex_metadata_editor.py) with plain Plex metadata.
  --min-free-gb  Extra disk-space floor (in GB) required, on top of the
                 file's own size, where the safety copy goes (--backup-dir,
                 or the file's own disk if blank). Default: 20.
  --decode-check Also run the slow full-decode validation step (see CHECKS
                 above). Off by default -- use decode_audit.py for a
                 separate, dedicated corruption sweep instead.

EXAMPLES
  # Dry run (default) - lists every matched file and what would happen
  python3 plex_mkv_metadata_batch.py

  # Prove the pipeline on 3 files before running the whole folder
  python3 plex_mkv_metadata_batch.py --no-dry-run --limit 3

  # Full real run over the whole folder
  python3 plex_mkv_metadata_batch.py --no-dry-run

  # Re-tag everything even if already tagged
  python3 plex_mkv_metadata_batch.py --no-dry-run --force

OUTPUT LOCATION
  Progress/text log : ~/logs/plex_mkv_metadata_batch_progress.log
  Result CSV        : ~/scripts_meta_data/plex_mkv_metadata_batch_<YYYY_MM_DD>.csv
                       (one row per file, written+flushed immediately after
                       that file finishes — readable mid-run, not just at
                       the end). If that day's CSV already exists with an
                       older column layout, _v2 (_v3, ...) is used instead.
  Safety copies      : <name>.batch_safety_<timestamp> next to each
                       file (deleted after a verified write), or
                       <--backup-dir>/<name>.bak.<timestamp> (kept)
  Temp tags/poster    : ~/scripts_meta_data/ (removed per file)

SIDE EFFECTS (only when --no-dry-run is passed)
  - Makes a full-size safety copy of each file before editing (next to it,
    or in --backup-dir), verified by size.
  - Edits each ORIGINAL file in place -- mkvpropedit for .mkv, mutagen for
    .mp4 -- writing the same field set either way: TITLE, SUMMARY, GENRE,
    COUNTRY, LAW_RATING, DATE_RELEASED, PRODUCTION_STUDIO, DIRECTOR,
    WRITTEN_BY, PRODUCER, ACTOR/CHARACTER, IMDB, TMDB, TVDB, SORT_WITH,
    ORIGINAL_TITLE, TAGLINE, COLLECTION, SHOW/SEASON/EPISODE (TV), plus
    cover art. Other existing MKV global tags (e.g. ENCODER) are kept. (MP4 stores everything except TITLE/cover as freeform
    ----:com.plexmeta:NAME atoms, since standard iTunes atoms don't cover
    most of these fields.)
  - If disk space where the safety copy goes drops below the required
    floor before a file, the ENTIRE BATCH STOPS (does not skip and continue) —
    running out of space mid-copy is worse than stopping early.
  - If a single file's write or validation fails, that file is logged as
    FAIL and the batch CONTINUES to the next file — one bad file does not
    block the rest. If the write had already happened, the ORIGINAL IS
    RESTORED automatically from the safety copy and re-verified (if the
    restore itself fails, the safety copy is kept and its path logged).
"""

import argparse
import csv
import datetime
import os
import shutil
import sys
import xml.etree.ElementTree as ET

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_metadata_lib as lib  # noqa: E402
import media_tag_io as mt  # noqa: E402  -- shared with plex_metadata_editor.py
# Sentinel values live in media_tag_io (shared with the UI editor); re-exported
# here so older code importing them from this module keeps working.
from media_tag_io import (  # noqa: E402,F401
    SENTINEL_SOURCES, SENTINEL_VALUE_BATCH, SENTINEL_VALUE_LEGACY, SENTINEL_VALUE_UI, SKIP_VALUES,
)

DEFAULT_PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
LOG_DIR = os.path.expanduser("~/logs")
DATA_DIR = os.path.expanduser("~/scripts_meta_data")
LOG_FILE = os.path.join(LOG_DIR, "plex_mkv_metadata_batch_progress.log")
SUPPORTED_EXTENSIONS = (".mkv", ".mp4")
# Plex item type to list for each kind of library section (looked up live).
SECTION_ITEM_TYPES = {"movie": 1, "show": 4}

CSV_FIELDNAMES = [
    "ratingkey", "file", "previous_sentinel", "safety_copy", "backup_kept", "tags_written",
    "poster_embedded", "duration_check", "stream_check", "packet_md5_check", "size_check",
    "structural_check", "decode_check", "tag_check", "cover_check", "restored",
    "overall_result", "notes",
]


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def discover_matching_items(plex_url, token, folder):
    """Look up every movie and TV library section in Plex (no hard-coded
    section numbers), list each once, and return [(ratingkey, file_path),
    ...] for every item whose file lives under `folder` and ends in .mkv or
    .mp4."""
    folder_real = os.path.realpath(folder)
    if not folder_real.endswith(os.sep):
        folder_real += os.sep

    resp = requests.get(f"{plex_url}/library/sections", params={"X-Plex-Token": token}, timeout=60)
    resp.raise_for_status()
    matches = []
    for section in ET.fromstring(resp.content).findall("Directory"):
        item_type = SECTION_ITEM_TYPES.get(section.get("type"))
        if not item_type:
            continue
        url = f"{plex_url}/library/sections/{section.get('key')}/all"
        resp = requests.get(url, params={"type": item_type, "X-Plex-Token": token}, timeout=60)
        resp.raise_for_status()
        for video in ET.fromstring(resp.content).findall("Video"):
            ratingkey = video.get("ratingKey")
            for part in video.findall("./Media/Part"):
                file_path = part.get("file")
                if not file_path:
                    continue
                if os.path.realpath(file_path).startswith(folder_real) and file_path.lower().endswith(SUPPORTED_EXTENSIONS):
                    matches.append((ratingkey, file_path))
    return matches


def sentinel_value(path):
    """Value of the PLEX_METADATA_SYNCED tag, or None. See
    media_tag_io.read_sentinel (ffprobe for MKV -- mediainfo's quick scan
    misses tags stored at the end of the file)."""
    return mt.read_sentinel(path)


def free_space_gb(path):
    usage = shutil.disk_usage(path)
    return usage.free / (1024 ** 3)


def process_one_file(ratingkey, file_path, args, csv_writer):
    row = {k: "" for k in CSV_FIELDNAMES}
    row.update(ratingkey=ratingkey, file=file_path, tags_written=0, poster_embedded=False,
               overall_result="FAIL")

    if not os.path.exists(file_path):
        row["notes"] = "source file not found on disk"
        log(f"FAIL:{file_path}:source_file_missing")
        csv_writer.write_row(row)
        return "FAIL"

    synced = sentinel_value(file_path)
    row["previous_sentinel"] = synced or ""
    retag_note = ""
    if synced is not None and synced not in SKIP_VALUES:
        log(f"RETAG:{file_path}:found {lib.SENTINEL_TAG_NAME}={synced} "
            f"({SENTINEL_SOURCES.get(synced, 'unknown tool')}), not a skip value -- re-tagging")
        retag_note = f"re-tag: previous {lib.SENTINEL_TAG_NAME}={synced}"
        row["notes"] = retag_note
    if not args.force and synced in SKIP_VALUES:
        source = SENTINEL_SOURCES[synced]
        row["overall_result"] = "SKIP"
        row["notes"] = (f"already tagged by {source} ({lib.SENTINEL_TAG_NAME}={synced}), "
                        "use --force to re-tag")
        log(f"SKIP:{file_path}:already_tagged_by_{source.replace(' ', '_')}")
        csv_writer.write_row(row)
        return "SKIP"

    orig_size = os.path.getsize(file_path)

    if args.dry_run:
        log(f"[DRY RUN] Would process ratingKey={ratingkey} file={file_path} ({orig_size} bytes)")
        row["overall_result"] = "DRY_RUN"
        row["notes"] = "dry run only" + (f"; {retag_note}" if retag_note else "")
        csv_writer.write_row(row)
        return "DRY_RUN"

    # --- Disk guard (batch-stopping): room for the safety copy + floor ---
    copy_dir = args.backup_dir or os.path.dirname(file_path)
    if args.backup_dir:
        os.makedirs(args.backup_dir, exist_ok=True)
    required_gb = orig_size / (1024 ** 3) + args.min_free_gb
    avail_gb = free_space_gb(copy_dir)
    if avail_gb < required_gb:
        log(f"FAIL:{file_path}:insufficient_disk_space in {copy_dir} (need ~{required_gb:.1f}GB, have {avail_gb:.1f}GB)")
        row["notes"] = f"insufficient disk space in {copy_dir}: need ~{required_gb:.1f}GB, have {avail_gb:.1f}GB"
        csv_writer.write_row(row)
        raise SystemExit("STOP_BATCH_DISK_SPACE")

    log(f"Processing ratingKey={ratingkey} file={file_path} ({orig_size} bytes)")
    tmp_prefix = os.path.join(DATA_DIR, f"plex_mkv_metadata_batch_{ratingkey}")
    cover_jpg_path = f"{tmp_prefix}_cover.jpg"

    try:
        video = lib.fetch_plex_item(args.plex_url, args.plex_token, ratingkey)
        fields = mt.parse_plex(video)
        # Everything comes from Plex, except unrelated MKV global tags already
        # in the file (e.g. ENCODER), which are carried over untouched.
        try:
            fields["extra_tags"] = mt.read_file_fields(file_path, tmp_prefix).get("extra_tags", [])
        except Exception as exc:
            log(f"WARNING: {file_path}:could not read existing tags, extra tags not kept: {exc}")
        fields = mt.clean_fields(fields)
        errors = mt.validate_fields(fields)
        if errors:
            row["notes"] = f"Plex metadata failed validation (file not touched): {'; '.join(errors)}"
            log(f"FAIL:{file_path}:plex_metadata_invalid:{errors}")
            csv_writer.write_row(row)
            return "FAIL"

        cover = None
        if video.get("thumb"):
            try:
                lib.download_poster(args.plex_url, args.plex_token, video.get("thumb"), cover_jpg_path)
                cover = cover_jpg_path
            except Exception as e:
                log(f"WARNING: {file_path}:poster_download_failed:{str(e)[:100]}")
        row["poster_embedded"] = cover is not None

        extra_check = None
        if args.decode_check:
            def extra_check(path):
                ok, err = lib.full_decode_check(path)
                return None if ok else f"full decode failed: {err[:200]}"

        res = mt.safe_write(file_path, fields, cover, SENTINEL_VALUE_BATCH, tmp_prefix,
                            backup_dir=args.backup_dir, safety_tag="batch_safety",
                            extra_check=extra_check, min_free_bytes=int(args.min_free_gb * 1024 ** 3))
        c = res["checks"]
        row.update(safety_copy=res["safety_copy"], tags_written=res["tags_written"],
                   duration_check=c["duration"], stream_check=c["streams"], packet_md5_check=c["packets"],
                   size_check=c["size"], structural_check=c["structural"],
                   decode_check=c["extra"] if args.decode_check else "SKIPPED",
                   tag_check=c["tags"], cover_check=c["cover"], restored=res["restored_text"],
                   overall_result="PASS" if res["ok"] else "FAIL",
                   notes="; ".join(x for x in (retag_note, res["error"]) if x))
        if args.backup_dir and res["safety_copy"] and os.path.exists(res["safety_copy"]):
            row["backup_kept"] = res["safety_copy"]

        if res["ok"]:
            log(f"DONE:{file_path}")
        elif res["restored"]:
            log(f"FAIL:{file_path}:{res['error']} -- original restored")
        else:
            log(f"FAIL:{file_path}:{res['error']}" + (f" -- safety copy kept at {res['safety_copy']}"
                                                     if res["safety_copy"] and os.path.exists(res["safety_copy"]) else ""))
        csv_writer.write_row(row)
        return row["overall_result"]

    except Exception as exc:
        row["notes"] = "; ".join(x for x in (retag_note, f"exception: {exc}") if x)
        log(f"FAIL:{file_path}:exception:{exc}")
        csv_writer.write_row(row)
        return "FAIL"
    finally:
        if os.path.exists(cover_jpg_path):
            os.remove(cover_jpg_path)


def pick_csv_path(date_str):
    """Today's CSV, or _v2/_v3... if today's file already exists with a
    different (older) column layout -- appending would misalign columns."""
    n = 1
    while True:
        suffix = "" if n == 1 else f"_v{n}"
        path = os.path.join(DATA_DIR, f"plex_mkv_metadata_batch_{date_str}{suffix}.csv")
        if not os.path.exists(path):
            return path
        with open(path, newline="") as fh:
            if next(csv.reader(fh), []) == CSV_FIELDNAMES:
                return path
        n += 1


def main():
    parser = argparse.ArgumentParser(description="Batch-embed Plex metadata into every matched MKV/MP4 file's own tags.")
    parser.add_argument("--folder", default=os.environ.get("PLEX_MEDIA_FOLDER", ""),
                        help="Root media folder to scan (recursive).")
    parser.add_argument("--plex-url", default="http://127.0.0.1:32400")
    parser.add_argument("--plex-token", default=DEFAULT_PLEX_TOKEN)
    parser.add_argument("--backup-dir", default="",
                        help="Keep each file's safety copy here. Blank (default): temporary copy next "
                             "to the file, deleted after the file passes all checks.")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--min-free-gb", type=float, default=20)
    parser.add_argument("--decode-check", dest="decode_check", action="store_true",
                         help="Also run the full CPU decode validation (the slow, high-CPU step). "
                              "Off by default: decode_check tests SOURCE FILE health (frame-level "
                              "corruption), not anything mkvpropedit/mutagen could introduce -- they "
                              "only ever touch header/tag atoms, never frame data. Use "
                              "decode_audit.py for a dedicated, separate corruption sweep instead of "
                              "paying this cost on every tagging run.")
    parser.set_defaults(dry_run=True, decode_check=False)
    args = parser.parse_args()
    args.backup_dir = args.backup_dir.strip()
    mt.set_logger(log)

    log(f"=== Starting batch run folder={args.folder} dry_run={args.dry_run} limit={args.limit} force={args.force} "
        f"decode_check={args.decode_check} backup_dir={args.backup_dir or '(temporary safety copies)'} ===")

    matches = discover_matching_items(args.plex_url, args.plex_token, args.folder)
    log(f"Discovered {len(matches)} matching .mkv/.mp4 file(s) under {args.folder}")
    for ratingkey, file_path in matches:
        log(f"  ratingKey={ratingkey}  {file_path}")

    if args.limit > 0:
        matches = matches[: args.limit]
        log(f"Limiting to first {len(matches)} file(s) due to --limit {args.limit}")

    date_str = datetime.datetime.now().strftime("%Y_%m_%d")
    os.makedirs(DATA_DIR, exist_ok=True)
    csv_path = pick_csv_path(date_str)
    csv_writer = lib.IncrementalCsvWriter(csv_path, fieldnames=CSV_FIELDNAMES)

    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "DRY_RUN": 0}
    try:
        for ratingkey, file_path in matches:
            result = process_one_file(ratingkey, file_path, args, csv_writer)
            counts[result] = counts.get(result, 0) + 1
    except SystemExit as exc:
        if str(exc) == "STOP_BATCH_DISK_SPACE":
            log("=== BATCH STOPPED EARLY: insufficient disk space for the safety copy ===")
            csv_writer.close()
            sys.exit(1)
        raise
    finally:
        csv_writer.close()

    log(f"=== Batch complete. PASS={counts['PASS']} FAIL={counts['FAIL']} SKIP={counts['SKIP']} DRY_RUN={counts['DRY_RUN']} ===")
    log(f"CSV: {csv_path}")


if __name__ == "__main__":
    main()
