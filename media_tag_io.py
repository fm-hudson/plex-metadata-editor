"""
media_tag_io.py

Shared media-file tagging + safe-write pipeline used by BOTH
plex_metadata_editor.py (the web UI) and plex_mkv_metadata_batch.py (the
batch enrichment job). Not a standalone script.

Contains:
  - the field model (title, sort_title, ..., genres, roles, extra_tags)
    and conversion to/from container tags (Matroska Simple tags / MP4
    ----:com.plexmeta:NAME freeform atoms + standard iTunes atoms)
  - MKV read/write (mkvextract / mkvpropedit), MP4 read/write (mutagen)
  - parse_plex(): Plex item XML -> field model
  - PLEX_METADATA_SYNCED sentinel values and read_sentinel()
  - safe_write(): safety copy -> write -> quick verification against the
    original -> delete the copy, or restore the original automatically if
    any check fails. Quick checks (seconds, no full decode): duration,
    stream layout, MD5 of raw audio/video packets at start+middle, size
    change, container structure, tags read back exactly, cover present,
    plus an optional caller-supplied extra check (e.g. the batch job's
    opt-in full decode).

REQUIRES: /usr/bin/python3 with mutagen + requests; mkvtoolnix, ffmpeg,
ffprobe; plex_metadata_lib.py in the same folder.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import traceback
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

import plex_metadata_lib as lib

# ---------------------------------------------------------------------------
# PLEX_METADATA_SYNCED ("already done") sentinel values
# ---------------------------------------------------------------------------
SENTINEL_VALUE_BATCH = "plex_metadata_editor_batch"   # written by plex_mkv_metadata_batch.py
SENTINEL_VALUE_UI = "plex_metadata_editor_ui"         # written by plex_metadata_editor.py
SENTINEL_VALUE_LEGACY = "1"                           # older batch runs -- gets re-tagged
SKIP_VALUES = {SENTINEL_VALUE_BATCH, SENTINEL_VALUE_UI}
SENTINEL_SOURCES = {SENTINEL_VALUE_BATCH: "batch script", SENTINEL_VALUE_UI: "UI editor",
                    SENTINEL_VALUE_LEGACY: "old batch script (will be re-tagged)"}

MP4_EXTENSIONS = (".mp4", ".m4v")
MP4_PREFIX = f"----:{lib.MP4_MEAN}:"

# Verification: seconds of raw packets hashed at each sample point, and the
# largest size change an edit can plausibly cause (tags + a cover image).
HASH_SECONDS = 20
MAX_SIZE_CHANGE = 25 * 1024 ** 2
# Safety copy / restore run in chunks of this size so callers can show progress.
COPY_CHUNK = 64 * 1024 ** 2

SCALAR_FIELDS = ["title", "sort_title", "original_title", "tagline", "summary",
                 "content_rating", "release_date", "year", "studio",
                 "imdb", "tmdb", "tvdb", "show_title", "season", "episode"]
LIST_FIELDS = ["genres", "collections", "countries", "directors", "writers", "producers"]

# Container tag names (Matroska Simple tag names; MP4 uses the same names as
# ----:com.plexmeta:NAME freeform atoms). release_date/year both map to
# DATE_RELEASED and are handled separately.
TAG_SCALAR = {
    "title": "TITLE", "sort_title": "SORT_WITH", "original_title": "ORIGINAL_TITLE",
    "tagline": "TAGLINE", "summary": "SUMMARY", "content_rating": "LAW_RATING",
    "studio": "PRODUCTION_STUDIO", "imdb": "IMDB", "tmdb": "TMDB", "tvdb": "TVDB",
    # YEAR is written separately because Plex's year can differ from its
    # release date's year (common for scene releases); DATE_RELEASED alone
    # can't hold both.
    "year": "YEAR",
    "show_title": "SHOW", "season": "SEASON", "episode": "EPISODE",
}
TAG_LIST = {
    "genres": "GENRE", "collections": "COLLECTION", "countries": "COUNTRY",
    "directors": "DIRECTOR", "writers": "WRITTEN_BY", "producers": "PRODUCER",
}

# Callers plug in their own logger (editor -> its daily log, batch -> its progress log).
log = print


def set_logger(fn):
    global log
    log = fn


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def is_mp4(path):
    """True if the file is really an MP4/MOV container. Decided from the
    first bytes (Matroska EBML magic vs an MP4 atom), not the extension --
    some files are MP4s named .mkv. Falls back to the extension."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        if head[:4] == b"\x1a\x45\xdf\xa3":
            return False
        if head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"):
            return True
    except OSError:
        pass
    return os.path.splitext(path)[1].lower() in MP4_EXTENSIONS


def empty_fields():
    f = {k: "" for k in SCALAR_FIELDS}
    for k in LIST_FIELDS:
        f[k] = []
    f["roles"] = []
    f["extra_tags"] = []
    return f


def set_date(f, value):
    """DATE_RELEASED -> release_date (+ year, unless an explicit YEAR tag
    already set it)."""
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        f["release_date"] = value
        f["year"] = f["year"] or value[:4]
    elif re.fullmatch(r"\d{4}", value):
        f["year"] = f["year"] or value
    elif value:
        f["release_date"] = value


_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _txt(v):
    """One field value as clean text: \r\n / \r -> \n (Matroska tag XML
    normalizes them on read-back anyway) and control characters XML can't
    hold removed, so what's written is exactly what reads back."""
    s = str(v or "").replace("\r\n", "\n").replace("\r", "\n")
    return _XML_INVALID.sub("", s).strip()


def clean_fields(raw):
    """Normalize a fields dict coming from the UI (or anywhere): strip
    strings, normalize line endings, drop empty list entries/roles/extra
    tags."""
    f = empty_fields()
    raw = raw or {}
    for k in SCALAR_FIELDS:
        f[k] = _txt(raw.get(k))
    if not f["year"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["release_date"]):
        f["year"] = f["release_date"][:4]   # a full date implies its year
    for k in LIST_FIELDS:
        vals = raw.get(k) or []
        if isinstance(vals, str):
            vals = vals.splitlines()
        seen = []
        for v in vals:
            v = _txt(v)
            if v and v not in seen:
                seen.append(v)
        f[k] = seen
    for r in raw.get("roles") or []:
        actor = _txt(r.get("actor"))
        character = _txt(r.get("character"))
        if actor or character:
            f["roles"].append({"actor": actor, "character": character})
    for t in raw.get("extra_tags") or []:
        name = _txt(t.get("name")).upper()
        value = _txt(t.get("value"))
        if name and value:
            f["extra_tags"].append({"name": name, "value": value})
    return f


def validate_fields(f):
    errors = []
    for k in ("year", "season", "episode"):
        if f[k] and not f[k].isdigit():
            errors.append(f"{k} must be a whole number")
    if f["release_date"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["release_date"]):
        errors.append("release_date must be YYYY-MM-DD")
    reserved = set(TAG_SCALAR.values()) | set(TAG_LIST.values()) | {
        "ACTOR", "CHARACTER", "DATE_RELEASED", lib.SENTINEL_TAG_NAME}
    for t in f["extra_tags"]:
        if t["name"] in reserved:
            errors.append(f"other tag {t['name']} clashes with a form field -- edit it in the form")
    return errors


def fields_to_simples(f, include_extras=True, pair_all_characters=False, sentinel_value=SENTINEL_VALUE_UI):
    """Fields -> ordered [(TAG_NAME, value)]. pair_all_characters writes a
    CHARACTER for every ACTOR (even empty) -- needed for MP4 where ACTOR and
    CHARACTER live in separate atoms and only index position pairs them."""
    out = []
    for key, name in TAG_SCALAR.items():
        if f[key]:
            out.append((name, f[key]))
    date = f["release_date"] or f["year"]
    if date:
        out.append(("DATE_RELEASED", date))
    for key, name in TAG_LIST.items():
        for v in f[key]:
            out.append((name, v))
    for r in f["roles"]:
        out.append(("ACTOR", r["actor"]))
        if r["character"] or pair_all_characters:
            out.append(("CHARACTER", r["character"]))
    if include_extras:
        for t in f["extra_tags"]:
            out.append((t["name"], t["value"]))
    out.append((lib.SENTINEL_TAG_NAME, sentinel_value))
    return out


def simples_to_fields(simples):
    """Ordered [(TAG_NAME, value)] -> fields. A CHARACTER belongs to the
    ACTOR just before it (same pairing Matroska nesting / lib uses)."""
    f = empty_fields()
    rev_scalar = {v: k for k, v in TAG_SCALAR.items()}
    rev_list = {v: k for k, v in TAG_LIST.items()}
    for name, value in simples:
        value = (value or "").strip()
        if name == lib.SENTINEL_TAG_NAME:
            f["_sentinel"] = value  # who tagged it before; not an editable field
            continue
        if name in rev_scalar:
            if value and not f[rev_scalar[name]]:
                f[rev_scalar[name]] = value
        elif name == "DATE_RELEASED":
            set_date(f, value)
        elif name in rev_list:
            if value and value not in f[rev_list[name]]:
                f[rev_list[name]].append(value)
        elif name == "ACTOR":
            f["roles"].append({"actor": value, "character": ""})
        elif name == "CHARACTER":
            if f["roles"] and not f["roles"][-1]["character"]:
                f["roles"][-1]["character"] = value
            elif value:
                f["roles"].append({"actor": "", "character": value})
        elif value:
            f["extra_tags"].append({"name": name, "value": value})
    f["roles"] = [r for r in f["roles"] if r["actor"] or r["character"]]
    return f


def has_content(f):
    return any(f[k] for k in SCALAR_FIELDS) or any(f[k] for k in LIST_FIELDS) or bool(f["roles"])


def compare_fields(a, b, include_extras=True):
    """Names of fields that differ between two cleaned field dicts."""
    diff = [k for k in SCALAR_FIELDS + LIST_FIELDS + ["roles"] if a[k] != b[k]]
    if include_extras and a["extra_tags"] != b["extra_tags"]:
        diff.append("extra_tags")
    return diff


def mkv_identify(path):
    try:
        return json.loads(run(["mkvmerge", "-J", path], timeout=60).stdout)
    except Exception:
        return {}


_XML_BAD_CHARREF = re.compile(r"&#(?:x0*(?:[0-8bcBC]|1[0-9a-fA-F])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));")


def _parse_tags_xml(xml_path):
    """Parse mkvextract's tags XML. Encoders sometimes leave control
    characters in (e.g. HANDLER_NAME "&#31;Mainconcept..."), which XML 1.0
    forbids -- ElementTree would reject the WHOLE file and every tag would
    look missing. Strip them (as raw bytes or &#NN; references) first."""
    with open(xml_path, encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read()
    text = _XML_INVALID.sub("", _XML_BAD_CHARREF.sub("", text))
    return ET.fromstring(text.encode("utf-8"))


def read_mkv(path, tmp_prefix):
    info = mkv_identify(path)
    tmp = f"{tmp_prefix}_read_tags.xml"
    simples = []
    try:
        r = run(["mkvextract", path, "tags", tmp], timeout=120)
        if r.returncode in (0, 1) and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            root = _parse_tags_xml(tmp)
            for tag in root.findall("Tag"):
                targets = tag.find("Targets")
                if targets is not None and any(
                        targets.find(x) is not None
                        for x in ("TrackUID", "EditionUID", "ChapterUID", "AttachmentUID")):
                    continue  # per-track/chapter tags: not ours, left untouched

                def walk(simple):
                    simples.append(((simple.findtext("Name") or "").strip().upper(),
                                    simple.findtext("String") or ""))
                    for child in simple.findall("Simple"):
                        walk(child)
                for s in tag.findall("Simple"):
                    walk(s)
    except Exception as exc:
        log(f"WARNING: could not read MKV tags from {path}: {exc}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    f = simples_to_fields(simples)
    segment_title = (info.get("container", {}).get("properties", {}).get("title") or "").strip()
    if not f["title"] and segment_title:
        f["title"] = segment_title
    return f, info


def mkv_cover_attachment(info):
    images = [a for a in info.get("attachments", [])
              if str(a.get("content_type", "")).startswith("image/")]
    if not images:
        return None
    images.sort(key=lambda a: 0 if str(a.get("file_name", "")).lower().startswith("cover") else 1)
    return images[0]


def extract_mkv_cover(path, info, dest):
    att = mkv_cover_attachment(info)
    if not att:
        return False
    r = run(["mkvextract", path, "attachments", f"{att['id']}:{dest}"], timeout=120)
    return r.returncode in (0, 1) and os.path.exists(dest) and os.path.getsize(dest) > 0


def write_mkv(path, f, cover_jpg, tmp_prefix, sentinel_value=SENTINEL_VALUE_UI, progress=None):
    xml_path = f"{tmp_prefix}_write_tags.xml"
    body = "".join(
        f"      <Simple>\n        <Name>{escape(n)}</Name>\n        <String>{escape(v)}</String>\n      </Simple>\n"
        for n, v in fields_to_simples(f, sentinel_value=sentinel_value))
    with open(xml_path, "w") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<!DOCTYPE Tags SYSTEM "matroskatags.dtd">\n'
                 f"<Tags>\n  <Tag>\n    <Targets/>\n{body}  </Tag>\n</Tags>\n")
    cmd = ["mkvpropedit", path, "--tags", f"global:{xml_path}", "--edit", "info"]
    # Segment Info title is what file managers read; keep it in sync with TITLE.
    cmd += ["--set", f"title={f['title']}"] if f["title"] else ["--delete", "title"]
    if cover_jpg:
        for uid in lib.mkvmerge_attachment_uids(path, "cover.jpg"):
            cmd += ["--delete-attachment", f"={uid}"]
        cmd += ["--attachment-name", "cover.jpg", "--attachment-mime-type", "image/jpeg",
                "--add-attachment", cover_jpg]
    log("mkvpropedit command: " + " ".join(cmd))
    # Streamed (not run()) so any "Progress: NN%" lines reach the UI. In
    # practice mkvpropedit rarely prints them for tag edits -- it writes in
    # place / appends and never moves the media data, so it's quick.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    watchdog = threading.Timer(600, proc.kill)
    watchdog.start()
    output, line = [], ""
    try:
        for ch in iter(lambda: proc.stdout.read(1), ""):
            if ch in "\r\n":
                m = re.search(r"Progress:\s*(\d+)%", line)
                if m and progress:
                    progress(int(m.group(1)))
                if line.strip():
                    output.append(line)
                line = ""
            else:
                line += ch
        if line.strip():
            output.append(line)
        rc = proc.wait()
    finally:
        watchdog.cancel()
        os.remove(xml_path)
    # mkvpropedit: 0 = ok, 1 = ok with warnings, 2 = error (or killed by the 600s watchdog)
    return rc in (0, 1), "\n".join(output).strip()


def read_mp4(path):
    video = MP4(path)
    tags = video.tags or {}
    simples, actors, characters = [], [], []
    for key, vals in tags.items():
        if not key.startswith(MP4_PREFIX):
            continue
        name = key[len(MP4_PREFIX):].upper()
        for v in vals:
            text = bytes(v).decode("utf-8", "replace")
            if name == "ACTOR":
                actors.append(text)
            elif name == "CHARACTER":
                characters.append(text)
            else:
                simples.append((name, text))
    f = simples_to_fields(simples)
    paired = len(characters) == len(actors)
    f["roles"] = [{"actor": a.strip(), "character": characters[i].strip() if paired else ""}
                  for i, a in enumerate(actors) if a.strip()]

    # Standard iTunes atoms as fallback for files other tools tagged.
    def first(key):
        v = tags.get(key)
        return str(v[0]).strip() if v else ""
    f["title"] = f["title"] or first("\xa9nam")
    f["sort_title"] = f["sort_title"] or first("sonm")
    f["summary"] = f["summary"] or first("ldes") or first("desc")
    f["show_title"] = f["show_title"] or first("tvsh")
    f["season"] = f["season"] or first("tvsn")
    f["episode"] = f["episode"] or first("tves")
    if not f["release_date"] and not f["year"]:
        set_date(f, first("\xa9day")[:10])
    if not f["genres"] and tags.get("\xa9gen"):
        f["genres"] = [str(g).strip() for g in tags["\xa9gen"] if str(g).strip()]
    f["extra_tags"] = []  # other MP4 atoms are left alone, not edited here
    return f, video


def write_mp4(path, f, cover_jpg, sentinel_value=SENTINEL_VALUE_UI):
    try:
        video = MP4(path)
        if video.tags is None:
            video.add_tags()
        for key in list(video.tags.keys()):
            if key.startswith(MP4_PREFIX):
                del video.tags[key]
        grouped = {}
        for name, value in fields_to_simples(f, include_extras=False, pair_all_characters=True, sentinel_value=sentinel_value):
            grouped.setdefault(name, []).append(MP4FreeForm(value.encode("utf-8")))
        for name, vals in grouped.items():
            video.tags[MP4_PREFIX + name] = vals

        def set_or_del(key, value):
            if value:
                video.tags[key] = value
            elif key in video.tags:
                del video.tags[key]
        set_or_del("\xa9nam", [f["title"]] if f["title"] else None)
        set_or_del("sonm", [f["sort_title"]] if f["sort_title"] else None)
        set_or_del("desc", [f["summary"][:255]] if f["summary"] else None)
        set_or_del("ldes", [f["summary"]] if f["summary"] else None)
        date = f["release_date"] or f["year"]
        set_or_del("\xa9day", [date] if date else None)
        set_or_del("\xa9gen", f["genres"] or None)
        set_or_del("tvsh", [f["show_title"]] if f["show_title"] else None)
        set_or_del("tvsn", [int(f["season"])] if f["season"] else None)
        set_or_del("tves", [int(f["episode"])] if f["episode"] else None)
        if cover_jpg:
            with open(cover_jpg, "rb") as fh:
                video.tags["covr"] = [MP4Cover(fh.read(), imageformat=MP4Cover.FORMAT_JPEG)]
        video.save()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def extract_mp4_cover(video, dest):
    covers = (video.tags or {}).get("covr")
    if not covers:
        return False
    with open(dest, "wb") as fh:
        fh.write(bytes(covers[0]))
    return True


def parse_plex(video):
    f = empty_fields()
    f["title"] = video.get("title") or ""
    f["sort_title"] = video.get("titleSort") or ""
    f["original_title"] = video.get("originalTitle") or ""
    f["tagline"] = video.get("tagline") or ""
    f["summary"] = video.get("summary") or ""
    f["content_rating"] = video.get("contentRating") or ""
    f["release_date"] = video.get("originallyAvailableAt") or ""
    f["year"] = video.get("year") or ""
    f["studio"] = video.get("studio") or ""
    for key, tag in (("genres", "Genre"), ("collections", "Collection"), ("countries", "Country"),
                     ("directors", "Director"), ("writers", "Writer"), ("producers", "Producer")):
        f[key] = [e.get("tag") for e in video.findall(tag) if e.get("tag")]
    f["roles"] = [{"actor": e.get("tag") or "", "character": e.get("role") or ""}
                  for e in video.findall("Role")]
    for guid in video.findall("Guid"):
        gid = guid.get("id", "")
        for prefix in ("imdb", "tmdb", "tvdb"):
            if gid.startswith(prefix + "://"):
                f[prefix] = gid.split("://", 1)[1]
    if video.get("type") == "episode":
        f["show_title"] = video.get("grandparentTitle") or ""
        f["season"] = video.get("parentIndex") or ""
        f["episode"] = video.get("index") or ""
    return clean_fields(f)


def copy_with_progress(src, dst, progress=None):
    """shutil.copy2 equivalent that reports (bytes_done, bytes_total) after
    every COPY_CHUNK. Uses os.copy_file_range (kernel-side copy, same speed
    as shutil) and falls back to plain read/write if that isn't supported."""
    total = os.path.getsize(src)
    done = 0
    fin = os.open(src, os.O_RDONLY)
    try:
        fout = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            use_range = hasattr(os, "copy_file_range")
            while done < total:
                n = 0
                if use_range:
                    try:
                        n = os.copy_file_range(fin, fout, min(COPY_CHUNK, total - done))
                    except OSError:
                        use_range = False
                if not use_range:
                    buf = os.read(fin, COPY_CHUNK)
                    n = os.write(fout, buf) if buf else 0
                if n == 0:
                    break
                done += n
                if progress:
                    progress(done, total)
        finally:
            os.close(fout)
    finally:
        os.close(fin)
    shutil.copystat(src, dst)
    return done


def media_snapshot(path):
    """Duration, stream layout and raw-packet MD5s of a video file. The MD5s
    cover HASH_SECONDS of every video/audio stream's packets at the start and
    middle of the file, copied (not decoded), so this takes seconds even on
    large files. Embedded cover art is excluded -- that's what an edit changes."""
    r = run(["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height,channels,sample_rate:stream_disposition=attached_pic",
             "-of", "json", path], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:200]}")
    data = json.loads(r.stdout)
    duration = float(data.get("format", {}).get("duration") or 0)
    streams = [(s.get("codec_type"), s.get("codec_name"), s.get("width"), s.get("height"),
                s.get("channels"), s.get("sample_rate"))
               for s in data.get("streams", [])
               if s.get("codec_type") in ("video", "audio", "subtitle")
               and not s.get("disposition", {}).get("attached_pic")]
    hashes = []
    for start in sorted({0.0, round(duration / 2, 3) if duration > HASH_SECONDS * 3 else 0.0}):
        h = run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", path,
                 "-t", str(HASH_SECONDS), "-map", "0:V", "-map", "0:a?", "-c", "copy",
                 "-f", "streamhash", "-hash", "md5", "-"], timeout=120)
        if h.returncode != 0:
            raise RuntimeError(f"packet hash at {start:.0f}s failed: {h.stderr.strip()[:200]}")
        hashes.append((start, h.stdout.strip()))
    return {"size": os.path.getsize(path), "duration": duration, "streams": streams, "hashes": hashes}


def snapshot_checks(before, after):
    """Per-check comparison of two media_snapshot()s: {check: None if OK,
    else a human-readable problem}."""
    c = {"duration": None, "streams": None, "packets": None, "size": None}
    if abs(after["duration"] - before["duration"]) >= 0.5:
        c["duration"] = f"duration changed {before['duration']:.2f}s -> {after['duration']:.2f}s"
    if after["streams"] != before["streams"]:
        c["streams"] = f"stream layout changed: {before['streams']} -> {after['streams']}"
    bad = [f"{start:.0f}s" for (start, h1), (_, h2) in zip(before["hashes"], after["hashes"]) if h1 != h2]
    if bad or len(before["hashes"]) != len(after["hashes"]):
        c["packets"] = f"audio/video data differs at {', '.join(bad) or 'sample points'} (packet MD5 mismatch)"
    delta = after["size"] - before["size"]
    if abs(delta) > MAX_SIZE_CHANGE:
        c["size"] = f"file size changed by {delta / 1024**2:+.1f}MB (more than tags + cover can explain)"
    return c


def compare_snapshots(before, after):
    """List of human-readable problems (empty = edited file matches)."""
    return [p for p in snapshot_checks(before, after).values() if p]


def read_sentinel(path):
    """Value of the PLEX_METADATA_SYNCED tag, or None if the file doesn't
    have it. MP4 via mutagen freeform atom. MKV via ffprobe first: when
    mkvpropedit has no room near the start of the file it puts the Tags
    element at the END, and mediainfo's default quick scan never reaches it
    (so lib.already_synced can report "not tagged" for a tagged file).
    ffprobe follows the SeekHead to the Tags wherever they are. mediainfo is
    kept as a fallback."""
    if is_mp4(path):
        try:
            vals = (MP4(path).tags or {}).get(MP4_PREFIX + lib.SENTINEL_TAG_NAME)
        except Exception:
            return None
        return bytes(vals[0]).decode("utf-8", "replace").strip() if vals else None
    try:
        out = run(["ffprobe", "-v", "error", "-show_entries", f"format_tags={lib.SENTINEL_TAG_NAME}",
                   "-of", "default=noprint_wrappers=1:nokey=1", path], timeout=60)
        if out.stdout.strip():
            return out.stdout.strip().splitlines()[0].strip()
    except subprocess.TimeoutExpired:
        pass
    tags = lib.mediainfo_tags(path)
    if lib.SENTINEL_TAG_NAME not in tags:
        return None
    return str(tags[lib.SENTINEL_TAG_NAME]).strip()


def read_file_fields(path, tmp_prefix):
    """Field model from the file's own tags (MKV or MP4)."""
    return read_mp4(path)[0] if is_mp4(path) else read_mkv(path, tmp_prefix)[0]


class _NoProgress:
    """Default progress hooks for callers without a UI (the batch job)."""
    def step(self, name): pass
    def bytes(self, done, total): pass
    def pct(self, pct): pass
    def fail(self): pass
    def skip_pending(self, names): pass


def safe_write(path, f, cover, sentinel_value, tmp_prefix, backup_dir="", safety_tag="safety",
               progress=None, extra_check=None, min_free_bytes=1024 ** 3):
    """Write field model `f` (+ optional cover JPEG) into `path` safely:

      1. safety copy -- hidden .<name>.<safety_tag>_<stamp> next to the file,
         or <name>.bak.<stamp> in backup_dir (kept) if backup_dir is set
      2. snapshot the original, write tags (sentinel_value as
         PLEX_METADATA_SYNCED)
      3. verify the edited file against the original (see module docstring);
         extra_check(path) -> problem string or None runs last (optional)
      4. all OK -> delete the temporary safety copy; anything failed ->
         restore the original from the copy and re-verify it

    progress: object with step(name)/bytes(done,total)/pct(p)/fail()/
    skip_pending(names) hooks (UI progress bar); None = no progress.

    Returns a dict: ok, restored, steps [{step, ok, detail}], safety_copy,
    file_write, verification, checks {name: "PASS" | "FAIL (...)" |
    "SKIPPED"}, restored_text, error, tags_written."""
    p = progress or _NoProgress()
    name = os.path.basename(path)
    mp4 = is_mp4(path)
    res = {"ok": False, "restored": False, "steps": [], "safety_copy": "", "file_write": "",
           "verification": "", "restored_text": "", "error": "", "tags_written": 0,
           "checks": {k: "SKIPPED" for k in ("duration", "streams", "packets", "size", "structural",
                                             "tags", "cover", "extra")}}
    safety, keep_safety, written, before = None, False, False, None

    def step(label, ok, detail=""):
        res["steps"].append({"step": label, "ok": ok, "detail": detail})
        log(f"{'OK' if ok else 'FAIL'}:{label}:{path}:{detail}")

    try:
        size = os.path.getsize(path)
        # 1. Safety copy
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if backup_dir:
            os.makedirs(backup_dir, exist_ok=True)
            safety = os.path.join(backup_dir, f"{name}.bak.{stamp}")
            keep_safety = True
        else:
            safety = os.path.join(os.path.dirname(path), f"{name}.{safety_tag}_{stamp}")
        need = size + min_free_bytes
        free = shutil.disk_usage(os.path.dirname(safety)).free
        if free < need:
            safety = None
            raise RuntimeError(f"not enough free space for the safety copy: need "
                               f"{need / 1024**3:.1f}GB, have {free / 1024**3:.1f}GB")
        p.step("Safety copy")
        copy_with_progress(path, safety, p.bytes)
        if os.path.getsize(safety) != size:
            raise RuntimeError(f"safety copy size mismatch at {safety}")
        res["safety_copy"] = safety
        step("safety copy", True, safety)

        # 2. Snapshot of the original, then write
        p.step("Check original")
        before = media_snapshot(path)
        written = True
        p.step("Write tags")
        ok, detail = (write_mp4(path, f, cover, sentinel_value=sentinel_value) if mp4
                      else write_mkv(path, f, cover, tmp_prefix, sentinel_value=sentinel_value, progress=p.pct))
        res["file_write"] = "PASS" if ok else "FAIL"
        res["tags_written"] = len(fields_to_simples(f, include_extras=not mp4, pair_all_characters=mp4,
                                                    sentinel_value=sentinel_value))
        step("write file tags", ok, "" if ok else detail[:300])
        if not ok:
            raise RuntimeError(f"file write failed: {detail[:300]}")

        # 3. Verify edited file vs original
        p.step("Verify")
        checks = {}
        try:
            checks.update(snapshot_checks(before, media_snapshot(path)))
        except Exception as exc:
            checks["duration"] = f"edited file can't be read: {exc}"
        structural_ok, structural_err = lib.structural_check(path)
        checks["structural"] = None if structural_ok else f"container structure check failed: {structural_err[:200]}"
        try:
            reread = read_file_fields(path, tmp_prefix)
            mismatch = compare_fields(clean_fields(reread), f, include_extras=not mp4)
            if reread.get("_sentinel") != sentinel_value:
                mismatch.append(f"{lib.SENTINEL_TAG_NAME} (expected {sentinel_value})")
            checks["tags"] = f"tags read back differently: {', '.join(mismatch)}" if mismatch else None
            if cover:
                has_cover = (bool((MP4(path).tags or {}).get("covr")) if mp4
                             else bool(lib.mkvmerge_attachment_uids(path, "cover.jpg")))
                checks["cover"] = None if has_cover else "cover not found after write"
        except Exception as exc:
            checks["tags"] = f"tags can't be read back: {exc}"
        if extra_check:
            checks["extra"] = extra_check(path)
        for k, v in checks.items():
            res["checks"][k] = "PASS" if not v else f"FAIL ({v[:200]})"
        problems = [v for v in checks.values() if v]
        res["verification"] = "PASS" if not problems else "FAIL: " + "; ".join(problems)
        step("verify vs original", not problems, "; ".join(problems) or
             f"duration, streams, A/V packet MD5 ({len(before['hashes'])} points), size, structure, tags"
             + (", cover" if cover else "") + (", extra check" if extra_check else ""))
        if problems:
            raise RuntimeError("verification failed")
        res["ok"] = True
    except Exception as exc:
        p.fail()
        res["error"] = str(exc)[:300]
        if str(exc) != "verification failed":
            step("save", False, str(exc)[:300])
            log(traceback.format_exc())
        if written and safety and os.path.exists(safety):
            p.step("Restore original")
            res["restored"] = _restore(path, safety, keep_safety, before, step, p)
            if not res["restored"]:
                p.fail()
                keep_safety = True
            res["restored_text"] = "YES" if res["restored"] else "FAILED -- safety copy kept"
    finally:
        # 4. Drop the temporary safety copy unless it's needed/kept
        p.skip_pending({"Safety copy", "Check original", "Write tags", "Verify"})
        p.step("Clean up")
        if safety and os.path.exists(safety) and not keep_safety:
            os.remove(safety)
            step("remove safety copy", True, "")
        elif safety and os.path.exists(safety):
            step("safety copy kept", True, safety)
    return res


def _restore(path, safety, keep_safety, before, step, p):
    """Put the original back from the safety copy and prove it matches the
    pre-save snapshot. Returns True if the original is back."""
    try:
        if keep_safety:
            copy_with_progress(safety, path, p.bytes)   # copy: the kept backup stays
        else:
            os.replace(safety, path)                    # same folder: instant rename
        if before:
            problems = compare_snapshots(before, media_snapshot(path))
            if problems or os.path.getsize(path) != before["size"]:
                raise RuntimeError("; ".join(problems) or "size differs from original")
        step("restore original", True, "file is back exactly as before the save")
        return True
    except Exception as exc:
        step("restore original", False, f"{exc} -- original is still at {safety}")
        return False
