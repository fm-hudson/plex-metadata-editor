"""
plex_metadata_lib.py

Shared helpers for pulling Plex item metadata via its API and embedding it
into a media file's own container metadata, plus the post-write validation
checks (duration/audio-stream-count/full decode/tag presence/attachment
presence).

Two container backends:
  - MKV: Matroska tags + cover attachment via mkvpropedit/mkvinfo.
  - MP4: iTunes-style + freeform (----:com.plexmeta:NAME) atoms via
    mutagen (requires the `mutagen` package -- apt: python3-mutagen).

Format-specific functions are named mkv_*/mp4_* (or use the pre-existing
bare names for the original MKV-only functions, kept for backward
compatibility with callers that only ever pass .mkv paths). Callers that
need to handle either format should use the dispatchers: already_synced,
structural_check, write_metadata, tag_presence_check, attachment_check --
each picks the right backend from the file's extension.

Not a standalone script. Imported by plex_mkv_metadata_test.py (single-file,
MKV-only), plex_mkv_metadata_batch_csv.py (MKV-only), and
plex_mkv_metadata_batch.py (folder batch, MKV + MP4).
"""

import csv
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

# Marks a file as already processed by this pipeline. Used for batch
# resume/skip instead of checking for TITLE, since a source rip can
# already carry its own bare TITLE tag from whatever encoded it --
# that's not proof *we* enriched it, and would cause a false skip.
SENTINEL_TAG_NAME = "PLEX_METADATA_SYNCED"

# MP4 freeform atoms are namespaced as ----:MEAN:NAME. Using a reverse-DNS
# mean string (like iTunes' com.apple.iTunes) keeps these from colliding
# with tags any other tool might write.
MP4_MEAN = "com.plexmeta"

CSV_FIELDNAMES = [
    "ratingkey", "file", "backup_path", "tags_written", "poster_embedded",
    "duration_check", "audio_stream_check", "size_check", "structural_check",
    "decode_check", "tag_presence_check", "attachment_check", "overall_result", "notes",
]


def fetch_plex_item(plex_url, token, ratingkey):
    url = f"{plex_url}/library/metadata/{ratingkey}"
    resp = requests.get(url, params={"X-Plex-Token": token}, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    video = root.find("Video")
    if video is None:
        raise RuntimeError(f"No Video element found for ratingKey {ratingkey}")
    return video


def parse_fields(video):
    fields = {
        "title": video.get("title"),
        "summary": video.get("summary"),
        "contentRating": video.get("contentRating"),
        "year_date": video.get("originallyAvailableAt") or video.get("year"),
        "studio": video.get("studio"),
        "thumb": video.get("thumb"),
        "genres": [e.get("tag") for e in video.findall("Genre")],
        "countries": [e.get("tag") for e in video.findall("Country")],
        "directors": [e.get("tag") for e in video.findall("Director")],
        "writers": [e.get("tag") for e in video.findall("Writer")],
        "producers": [e.get("tag") for e in video.findall("Producer")],
        "roles": [(e.get("tag"), e.get("role")) for e in video.findall("Role")],
        "imdb": None,
        "tmdb": None,
        "tvdb": None,
    }
    for guid in video.findall("Guid"):
        gid = guid.get("id", "")
        if gid.startswith("imdb://"):
            fields["imdb"] = gid.split("imdb://", 1)[1]
        elif gid.startswith("tmdb://"):
            fields["tmdb"] = gid.split("tmdb://", 1)[1]
        elif gid.startswith("tvdb://"):
            fields["tvdb"] = gid.split("tvdb://", 1)[1]

    part = video.find("./Media/Part")
    if part is None or not part.get("file"):
        raise RuntimeError("No file path found in Plex response (Media/Part/@file)")
    fields["file"] = part.get("file")
    return fields


def build_tags_xml(fields, path, sentinel_value="1"):
    def simple(name, value):
        return f'      <Simple>\n        <Name>{escape(name)}</Name>\n        <String>{escape(str(value))}</String>\n      </Simple>\n'

    simples = ""
    if fields["title"]:
        simples += simple("TITLE", fields["title"])
    if fields["summary"]:
        simples += simple("SUMMARY", fields["summary"])
    if fields["contentRating"]:
        simples += simple("LAW_RATING", fields["contentRating"])
    if fields["year_date"]:
        simples += simple("DATE_RELEASED", fields["year_date"])
    if fields["studio"]:
        simples += simple("PRODUCTION_STUDIO", fields["studio"])
    for g in fields["genres"]:
        simples += simple("GENRE", g)
    for c in fields["countries"]:
        simples += simple("COUNTRY", c)
    for d in fields["directors"]:
        simples += simple("DIRECTOR", d)
    for w in fields["writers"]:
        simples += simple("WRITTEN_BY", w)
    for p in fields["producers"]:
        simples += simple("PRODUCER", p)
    for actor, character in fields["roles"]:
        if actor:
            simples += simple("ACTOR", actor)
        if character:
            simples += simple("CHARACTER", character)
    if fields["imdb"]:
        simples += simple("IMDB", fields["imdb"])
    if fields["tmdb"]:
        simples += simple("TMDB", fields["tmdb"])
    if fields["tvdb"]:
        simples += simple("TVDB", fields["tvdb"])
    # sentinel_value: "1" (default, older callers) or e.g.
    # "plex_metadata_editor_batch" from plex_mkv_metadata_batch.py.
    simples += simple(SENTINEL_TAG_NAME, sentinel_value)

    xml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE Tags SYSTEM "matroskatags.dtd">\n'
        "<Tags>\n"
        "  <Tag>\n"
        "    <Targets/>\n"
        f"{simples}"
        "  </Tag>\n"
        "</Tags>\n"
    )
    with open(path, "w") as f:
        f.write(xml_content)
    return simples.count("<Simple>")


def download_poster(plex_url, token, thumb_path, dest):
    url = f"{plex_url}{thumb_path}"
    resp = requests.get(url, params={"X-Plex-Token": token}, timeout=30)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        f.write(resp.content)
    return dest


def mediainfo_tags(path):
    """Fast metadata extraction via mediainfo JSON. Returns dict with all
    tags: top-level keys from media.track[0] (Title, Duration, etc.) merged
    with media.track[0].extra (SUMMARY, GENRE, ACTOR, DIRECTOR, PLEX_METADATA_SYNCED,
    Attachments, etc.). Timeout 30s (mediainfo doesn't scale with file size)."""
    try:
        out = subprocess.run(
            ["mediainfo", "--Output=JSON", path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout)
        track = data["media"]["track"][0]
        # Merge top-level track keys with extra subdict
        tags = dict(track)
        if "extra" in track:
            tags.update(track["extra"])
        return tags
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, IndexError) as e:
        return {}


def mkvmerge_attachment_uids(path, name):
    """Get attachment UIDs for a given file name via mkvmerge -J.
    Returns list of uid strings, or [] on error. Timeout 30s."""
    try:
        out = subprocess.run(
            ["mkvmerge", "-J", path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout)
        uids = []
        for attach in data.get("attachments", []):
            if attach.get("file_name") == name:
                uid = attach.get("properties", {}).get("uid")
                if uid is not None:
                    uids.append(str(uid))
        return uids
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
        return []


def ffprobe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=60,
    )
    return float(out.stdout.strip())


def ffprobe_audio_stream_count(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=60,
    )
    lines = [l for l in out.stdout.strip().splitlines() if l.strip()]
    return len(lines)


VAAPI_DEVICE_PATH = "/dev/dri/renderD128"


def full_decode_check(path, use_hwaccel=False):
    # Deliberately no -hwaccel BY DEFAULT: CPU-only decode is stricter and
    # catches corruption that hardware decoders can silently tolerate.
    # This matters most when validating an in-place edit of the one-and-
    # only copy of a file (plex_mkv_metadata_batch.py/test.py) -- there's
    # no untouched original to fall back on if a lenient check misses
    # something.
    #
    # use_hwaccel=True is for callers with a softer risk profile: e.g.
    # mkv_convert.py validates a brand-new file it just created in a
    # staging area -- the real original is completely untouched regardless
    # of this check's outcome, and there's a mandatory manual spot-check
    # before promotion anyway. There, trading some strictness for a much
    # faster GPU decode (especially on large/4K files) is a reasonable
    # call, made explicitly by that caller, not a silent global change.
    #
    # -map 0:V (capital V) selects real video streams and excludes
    # attached-picture streams (e.g. our own embedded cover.jpg, which
    # ffmpeg otherwise tries to probe/decode as a video frame and can
    # warn on due to EXIF quirks in the JPEG -- that's not stream
    # corruption). -c:s copy avoids a spurious "no encoder" error for
    # subtitle streams when muxing to the null format.
    #
    # Pass/fail is decided by returncode alone, not by "is stderr empty":
    # -xerror already makes ffmpeg exit non-zero on any real decode error
    # in the streams being processed. Benign warnings (like the attached
    # picture's probe-stage EXIF complaint above) can still print to
    # stderr on an otherwise-successful (exit 0) run, so gating on stderr
    # text produces false positives.
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if use_hwaccel:
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE_PATH]
    cmd += ["-xerror", "-i", path,
            "-map", "0:V", "-map", "0:a", "-map", "0:s?", "-c:s", "copy",
            "-f", "null", "-"]
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=7200,
    )
    return result.returncode == 0, result.stderr.strip()


def structural_check(path):
    """Dispatcher: fast, no-decode sanity check that the container itself
    still parses correctly. MKV uses mkvmerge --identify (walks the
    EBML/Segment structure -- tracks, tags, attachments, chapters -- without
    touching frame payloads). MP4 has no mkvmerge equivalent, so it uses
    ffprobe to re-parse the moov atom/stream table instead (see
    mp4_structural_check). Either way this catches gross structural damage
    (truncated file, broken container) without paying for a full decode."""
    if get_ext(path) == ".mp4":
        return mp4_structural_check(path)
    result = subprocess.run(
        ["mkvmerge", "--identify", path],
        capture_output=True, text=True, timeout=60,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def mkvinfo_output(path):
    try:
        out = subprocess.run(["mkvinfo", "-v", path], capture_output=True, text=True, timeout=600)
        return out.stdout
    except subprocess.TimeoutExpired:
        return ""


def has_attachment(mkvinfo_text, name):
    return name in mkvinfo_text and "Attachments" in mkvinfo_text


def run_mkvpropedit(path, tags_xml, cover_jpg, dry_run, log=print, title=None):
    delete_args = []
    existing = mkvmerge_attachment_uids(path, "cover.jpg") if not dry_run else []
    for uid in existing:
        delete_args += ["--delete-attachment", f"={uid}"]

    cmd = ["mkvpropedit", path, "--tags", f"global:{tags_xml}"]
    cmd += delete_args
    if cover_jpg:
        cmd += [
            "--attachment-name", "cover.jpg",
            "--attachment-mime-type", "image/jpeg",
            "--add-attachment", cover_jpg,
        ]
    if title:
        # --tags only writes the Tags/Simple-tag TITLE. Matroska also has a
        # separate Segment Info "Title" field (set at original mux time,
        # usually the release-group string) -- that's what file managers
        # like Nautilus/GStreamer read, not the Tags one. Set both so
        # every tool agrees on the title.
        cmd += ["--edit", "info", "--set", f"title={title}"]

    log("mkvpropedit command: " + " ".join(cmd))
    if dry_run:
        log("[DRY RUN] not executing mkvpropedit")
        return True, ""

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        return False, result.stdout + result.stderr
    return True, result.stdout + result.stderr


def already_synced(path):
    """Dispatcher: batch resume/skip check -- has THIS pipeline already
    tagged this file? Checks for our own sentinel tag, not TITLE -- a
    source rip can already carry a bare TITLE tag from whatever encoded it,
    which is not proof we enriched it and would cause a false skip (and a
    permanently-stale file, since real content would never get
    overwritten)."""
    if get_ext(path) == ".mp4":
        return mp4_already_synced(path)
    tags = mediainfo_tags(path)
    return SENTINEL_TAG_NAME in tags


def get_ext(path):
    return os.path.splitext(path)[1].lower()


# ---------------------------------------------------------------------------
# MP4 support (mutagen-based). MKV support lives in the functions above
# (mkvpropedit/mkvinfo/mkvmerge-based).
# ---------------------------------------------------------------------------

def _mp4_freeform_key(name):
    return f"----:{MP4_MEAN}:{name}"


def count_metadata_fields(fields):
    """Count tag values that would be written, without touching any file.
    MP4 has no separate tags-file step like build_tags_xml (mutagen edits
    the atom tree directly on save), so this is how a dry run or post-write
    log line can report a tag count without a real write."""
    count = 0
    for key in ("title", "summary", "contentRating", "year_date", "studio"):
        if fields.get(key):
            count += 1
    for key in ("genres", "countries", "directors", "writers", "producers"):
        count += len(fields.get(key) or [])
    for actor, character in fields.get("roles") or []:
        if actor:
            count += 1
        if character:
            count += 1
    for key in ("imdb", "tmdb", "tvdb"):
        if fields.get(key):
            count += 1
    count += 1  # sentinel
    return count


def build_mp4_tags(video, fields, sentinel_value="1"):
    """Set tags on an already-open mutagen MP4 object from parsed Plex
    fields. Mirrors build_tags_xml's field set, using freeform
    (----:com.plexmeta:NAME) atoms so multi-value fields (GENRE, ACTOR, ...)
    can carry more than one value the same way Matroska Simple tags do.
    TITLE is additionally set on the native \xa9nam atom so file
    managers/media players that only read standard iTunes atoms still show
    it (same reasoning run_mkvpropedit uses for setting Segment Info title
    alongside the Tags TITLE)."""
    # Clear this pipeline's own freeform atoms first so a re-tag REPLACES
    # them. (Previously add() appended to existing values, so re-tagging a
    # file duplicated every tag and left the old sentinel value first.)
    for key in list((video.tags or {}).keys()):
        if key.startswith(f"----:{MP4_MEAN}:"):
            del video.tags[key]

    def add(name, value):
        key = _mp4_freeform_key(name)
        existing = video.get(key, [])
        existing.append(MP4FreeForm(str(value).encode("utf-8")))
        video[key] = existing

    if fields.get("title"):
        video["\xa9nam"] = [fields["title"]]
        add("TITLE", fields["title"])
    if fields.get("summary"):
        add("SUMMARY", fields["summary"])
    if fields.get("contentRating"):
        add("LAW_RATING", fields["contentRating"])
    if fields.get("year_date"):
        add("DATE_RELEASED", fields["year_date"])
    if fields.get("studio"):
        add("PRODUCTION_STUDIO", fields["studio"])
    for g in fields.get("genres") or []:
        add("GENRE", g)
    for c in fields.get("countries") or []:
        add("COUNTRY", c)
    for d in fields.get("directors") or []:
        add("DIRECTOR", d)
    for w in fields.get("writers") or []:
        add("WRITTEN_BY", w)
    for p in fields.get("producers") or []:
        add("PRODUCER", p)
    for actor, character in fields.get("roles") or []:
        if actor:
            add("ACTOR", actor)
        if character:
            add("CHARACTER", character)
    if fields.get("imdb"):
        add("IMDB", fields["imdb"])
    if fields.get("tmdb"):
        add("TMDB", fields["tmdb"])
    if fields.get("tvdb"):
        add("TVDB", fields["tvdb"])
    add(SENTINEL_TAG_NAME, sentinel_value)


def run_mutagen_mp4_write(path, fields, cover_jpg, dry_run, log=print, sentinel_value="1"):
    """MP4 equivalent of run_mkvpropedit: open the file, set tags (+ cover
    art if provided), save in place. mutagen loads only the atom/tag
    structure into memory, not the mdat media payload, so this is safe on
    large (multi-GB) files. Returns (ok, error_message, tags_written)."""
    if dry_run:
        log(f"[DRY RUN] would write MP4 tags to {path}")
        return True, "", 0

    try:
        video = MP4(path)
        build_mp4_tags(video, fields, sentinel_value=sentinel_value)
        if cover_jpg and os.path.exists(cover_jpg):
            with open(cover_jpg, "rb") as f:
                cover_data = f.read()
            video["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
        video.save()
        return True, "", count_metadata_fields(fields)
    except Exception as exc:
        return False, str(exc), 0


def mp4_already_synced(path):
    try:
        video = MP4(path)
    except Exception:
        return False
    return _mp4_freeform_key(SENTINEL_TAG_NAME) in video


def mp4_structural_check(path):
    """Fast, no-decode sanity check for MP4: ffprobe re-parses the moov
    atom/stream table. No mkvmerge equivalent exists for MP4 containers."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return False, (result.stdout + result.stderr).strip()
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return False, f"ffprobe output not valid JSON: {exc}"
    if not data.get("streams"):
        return False, "ffprobe reported no streams"
    return True, ""


def mp4_tag_presence_check(path):
    expected = ["TITLE", "SUMMARY", "DIRECTOR", "ACTOR", SENTINEL_TAG_NAME]
    try:
        video = MP4(path)
    except Exception as exc:
        return False, [f"reopen_failed: {exc}"]
    missing = [name for name in expected if _mp4_freeform_key(name) not in video]
    return (not missing), missing


def mp4_attachment_check(path):
    try:
        video = MP4(path)
    except Exception:
        return False
    return bool(video.get("covr"))


# ---------------------------------------------------------------------------
# Format dispatchers -- used by plex_mkv_metadata_batch.py to handle MKV and
# MP4 files through the same call sites. structural_check and
# already_synced (above) also dispatch internally.
# ---------------------------------------------------------------------------

def write_metadata(path, fields, tags_xml_path, cover_jpg, dry_run, log=print, title=None, sentinel_value="1"):
    """Unified write dispatcher. MKV uses run_mkvpropedit (needs a
    pre-built tags XML file from build_tags_xml). MP4 uses
    run_mutagen_mp4_write (edits the atom tree directly, no XML file, so
    tags_xml_path is ignored). Returns (ok, error_message, tags_written) --
    tags_written is None for MKV since build_tags_xml already reported it
    to the caller before this was called. sentinel_value only matters for
    MP4 here -- for MKV it's baked into the XML by build_tags_xml."""
    if get_ext(path) == ".mp4":
        return run_mutagen_mp4_write(path, fields, cover_jpg, dry_run, log=log, sentinel_value=sentinel_value)
    ok, output = run_mkvpropedit(path, tags_xml_path, cover_jpg, dry_run, log=log, title=title)
    return ok, output, None


def tag_presence_check(path):
    """Unified post-write tag-presence dispatcher. Returns (ok, missing)."""
    if get_ext(path) == ".mp4":
        return mp4_tag_presence_check(path)
    info_text = mkvinfo_output(path)
    expected = ["TITLE", "SUMMARY", "DIRECTOR", "ACTOR", SENTINEL_TAG_NAME]
    missing = [t for t in expected if t not in info_text]
    return (not missing), missing


def attachment_check(path, cover_jpg_path):
    """Unified post-write cover-attachment dispatcher. No poster to check
    (cover_jpg_path falsy) always passes, matching the MKV batch script's
    original `if cover_jpg_path else True` behavior."""
    if not cover_jpg_path:
        return True
    if get_ext(path) == ".mp4":
        return mp4_attachment_check(path)
    info_text = mkvinfo_output(path)
    return has_attachment(info_text, "cover.jpg")


class IncrementalCsvWriter:
    """Opens the CSV once, appends+flushes+fsyncs after every row so the
    file is readable mid-run (tail/cat/spreadsheet) rather than only
    complete once the whole batch finishes."""

    def __init__(self, csv_path, fieldnames=CSV_FIELDNAMES):
        self.csv_path = csv_path
        self.fieldnames = fieldnames
        write_header = not os.path.exists(csv_path)
        self._fh = open(csv_path, "a", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=fieldnames)
        if write_header:
            self._writer.writeheader()
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def write_row(self, row):
        self._writer.writerow(row)
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self):
        self._fh.close()
