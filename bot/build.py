#!/usr/bin/env python3
"""
Builds the המרפסת podcast from the app's recordings.

Each run:
  1. Reads the recordings list and lesson details from the app (and Supabase).
  2. Tags every recording: lecturer, topic, occasion, series, program.
  3. Downloads only NEW recordings from Google Drive, re-encodes them to
     speech-quality MP3 and stores them as GitHub Release assets.
  4. Rewrites docs/feed.xml (the podcast feed Spotify reads), docs/index.html
     and reports/review.md (a tagging report for humans).

Usage:
  python bot/build.py                 # normal run (used by GitHub Actions)
  python bot/build.py --dry-run       # tag + build feed, no download/upload
  python bot/build.py --fixtures DIR  # read sources from local JSON files
"""
from __future__ import annotations

import argparse
import difflib
import datetime as dt
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import unquote
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests
import yaml
from pyluach import dates as hdates

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state" / "episodes.json"
DOCS = ROOT / "docs"
REPORT_FILE = ROOT / "reports" / "review.md"
TZ = ZoneInfo("Asia/Jerusalem")
UA = {"User-Agent": "mirpeset-podcast-bot/1.0"}

START = time.time()


def log(*a):
    print(*a, flush=True)


# ─────────────────────────────── text helpers ───────────────────────────────

def clean(s) -> str:
    """Normalize spacing and Hebrew punctuation variants."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFC", str(s))
    s = s.replace("״", '"').replace("׳", "'").replace("״", '"').replace("׳", "'")
    s = s.replace("‏", "").replace("‎", "").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" .,:;-–|")
    return s


def fix_mojibake(s: str) -> str:
    """Drive sends UTF-8 file names that requests decodes as Latin-1; undo that."""
    if s and re.search(r"[\u00c0-\u00ff]", s):
        try:
            raw = s.encode("latin-1")
        except UnicodeEncodeError:
            return s
        # names saved earlier had the byte 0xA0 (part of the letter נ) turned into a space
        raw = raw.replace(b"\xd7 ", b"\xd7\xa0")
        return raw.decode("utf-8", errors="replace").replace("\ufffd", "")
    return s


def key(s: str) -> str:
    """Comparison key: no spaces/punctuation."""
    return re.sub(r"[\s\"'.,:;\-–|()]", "", clean(s))


def contains(text: str, needle: str) -> bool:
    return bool(needle) and key(needle) in key(text)


# ─────────────────────────────── config ───────────────────────────────

class Tags:
    def __init__(self, data: dict):
        self.rabbis: dict[str, str] = {}          # key(alias) -> canonical
        self.rabbi_names: list[tuple[str, str]] = []  # (alias, canonical), longest first
        for canon, aliases in (data.get("rabbis") or {}).items():
            canon = clean(canon)
            for name in [canon, *(aliases or [])]:
                name = clean(name)
                self.rabbis[key(name)] = canon
                self.rabbi_names.append((name, canon))
        self.rabbi_names.sort(key=lambda x: -len(x[0]))
        self.placeholders = {key(p) for p in data.get("rabbi_placeholders", [])}
        self.unknown_rabbi = data.get("unknown_rabbi", "")
        self.occasions = data.get("occasions", [])
        self.holiday_categories = {key(c) for c in data.get("holiday_categories", [])}
        self.lookahead = int(data.get("holiday_lookahead_days", 21))
        self.programs = data.get("programs", [])
        self.series = data.get("series", [])
        self.exclude = data.get("exclude", [])

    def canon_rabbi(self, raw: str, unknown: set) -> str | None:
        raw = clean(raw)
        if not raw or key(raw) in self.placeholders:
            return None
        if key(raw) in self.rabbis:
            return self.rabbis[key(raw)]
        found = self.find_rabbi(raw)
        if found:
            return found
        if raw.startswith("הרב ") or raw.startswith("ד\"ר ") or raw.startswith("הרבנית "):
            unknown.add(raw)
            return raw
        return None

    def find_rabbi(self, text: str) -> str | None:
        for alias, canon in self.rabbi_names:
            if contains(text, alias):
                return canon
        return None

    def strip_rabbis(self, text: str) -> str:
        t = clean(text)
        for alias, _ in self.rabbi_names:
            t = re.sub(re.escape(alias), " ", t)
        t = re.sub(r"(^|\s)(הרב|מעביר)(\s|$)", " ", t)
        return clean(t)


def match_list(entries, text):
    for e in entries:
        if any(contains(text, m) for m in e.get("match", [])):
            return e["name"]
    return None


# ─────────────────────────────── dates ───────────────────────────────

FESTIVAL_MAP = {
    "ראש השנה": "ראש השנה", "יום כיפור": "יום הכיפורים", "סוכות": "סוכות",
    "שמיני עצרת": "סוכות", "שמחת תורה": "סוכות", "חנוכה": "חנוכה",
    "ט\"ו בשבט": "ט\"ו בשבט", "פורים": "פורים", "שושן פורים": "פורים",
    "פסח": "פסח", "פסח שני": "פסח שני", "ל\"ג בעומר": "ל\"ג בעומר", "שבועות": "שבועות",
    "ט' באב": "תשעה באב", "עשרה בטבת": "עשרה בטבת",
}


def parse_date(s) -> dt.date | None:
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(s))
    if not m:
        return None
    d = dt.date(int(m[1]), int(m[2]), int(m[3]))
    return d if d.year >= 2000 else None


def hebrew_date(d: dt.date) -> str:
    return hdates.HebrewDate.from_pydate(d).hebrew_date_string()


def upcoming_holiday(d: dt.date, days: int) -> str | None:
    for i in range(0, days + 1):
        h = hdates.HebrewDate.from_pydate(d + dt.timedelta(days=i))
        for name in (h.festival(israel=True, hebrew=True), h.fast_day(hebrew=True)):
            if name:
                n = clean(name)
                if n in FESTIVAL_MAP:
                    return FESTIVAL_MAP[n]
    return None


# ─────────────────────────────── sources ───────────────────────────────

def fetch_json(url, headers=None):
    r = requests.get(url, headers={**UA, **(headers or {})}, timeout=60)
    r.raise_for_status()
    return r.json()


def load_sources(cfg, fixtures: Path | None):
    src = cfg["sources"]
    if fixtures:
        recs = json.loads((fixtures / "recordings.json").read_text("utf-8"))
        lj = json.loads((fixtures / "lessons.json").read_text("utf-8"))
        sbp = fixtures / "supabase_lessons.json"
        sb = json.loads(sbp.read_text("utf-8")) if sbp.exists() else []
        return recs, lj, sb

    recs = None
    for url in src["recordings"]:
        try:
            data = fetch_json(url)
            if isinstance(data, dict):
                data = data.get("recordings") or data.get("data")
            if isinstance(data, list) and data:
                recs = data
                log(f"recordings: {len(recs)} from {url}")
                break
        except Exception as e:  # noqa: BLE001
            log(f"recordings source failed ({url}): {e}")
    if recs is None:
        sys.exit("ERROR: no recordings source worked; leaving the feed unchanged.")

    try:
        lj = fetch_json(src["lessons_json"])
    except Exception as e:  # noqa: BLE001
        log(f"lessons.json failed: {e}")
        lj = []

    sb = []
    sb_key = os.environ.get("SUPABASE_KEY", "").strip()
    if sb_key and src.get("supabase_url"):
        try:
            sb = fetch_json(
                src["supabase_url"].rstrip("/") + "/rest/v1/lessons"
                "?select=id,title,teacher,description,date,category,location",
                headers={"apikey": sb_key, **({"Authorization": f"Bearer {sb_key}"}
                                              if sb_key.startswith("eyJ") else {})},
            )
        except Exception as e:  # noqa: BLE001
            log(f"supabase failed: {e}")
    else:
        log("SUPABASE_KEY not set; using lessons.json and recording titles only")
    log(f"lessons: {len(lj)} from lessons.json, {len(sb)} from Supabase")
    return recs, lj, sb


# ─────────────────────────────── tagging ───────────────────────────────

TITLES = {"הרב", "הרבנית", "ד\"ר", "ר'"}
GENERIC_TOPICS = {key(x) for x in [
    "הכנה לחגים", "אירועים מיוחדים", "סדרות", "כללי", "שיעור", "הקלטה", "סדרה",
    "חדשה", "הקלטה חדשה", "חדש",
]}
PART_WORDS = {"ראשון": 1, "שני": 2, "שלישי": 3, "רביעי": 4, "חמישי": 5, "שישי": 6,
              "שביעי": 7, "שמיני": 8, "תשיעי": 9, "עשירי": 10}


def junk_filename(name: str) -> bool:
    if not name:
        return True
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name)
    if not re.search(r"[֐-׿]", base):          # no Hebrew at all
        return True
    return bool(re.search(r"הקלטה חדשה|^הקלטה\s*\d*$|whatsapp|ptt|^audio", base, re.I))


def detect_series(texts: list[str], tags: Tags):
    """Returns (series_name or None, part number or None)."""
    name, part = None, None
    for t in texts:
        t = clean(t)
        if not t:
            continue
        m = re.search(r"מפגש\s*(\d+|" + "|".join(PART_WORDS) + r")", t)
        if m and part is None:
            part = int(m[1]) if m[1].isdigit() else PART_WORDS[m[1]]
            before = clean(t[: m.start()])
            if before and key(before) not in {key("סדרה")} and not name:
                name = clean(re.sub(r"^סדרה בנושא", "", before))
        m = re.search(r"סדרה בנושא\s*(.+)$", t)
        if m and not name:
            name = clean(m[1].strip("'\"`"))
        m = re.match(r"^(.+?)\s*-\s*ס[י]?דרה$", t)
        if m and not name:
            name = clean(m[1])
    if not name:
        name = match_list(tags.series, " | ".join(texts))
    elif tags.series:
        name = match_list(tags.series, name) or name
    return name, part


def topic_from(text: str, tags: Tags) -> str:
    t = clean(text)
    t = re.sub(r"^הקלטת\s*", "", t)
    t = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", t)          # file extension
    t = tags.strip_rabbis(t)
    t = re.sub(r"\s-\s*(לא צוין)?$", "", t)
    t = re.sub(r"(^|\s)לא צוין(\s|$)", " ", t)
    t = re.sub(r"^סדרה בנושא\s*", "", t)
    t = re.sub(r"\s*-\s*ס[י]?דרה$", "", t)
    return clean(t.strip("'\"`"))


def filename_topic(name: str, rabbi: str | None, tags: Tags) -> str:
    if junk_filename(name):
        return ""
    fn = clean(name)
    fn = re.sub(r"(\.(mp3|m4a|mp4|mpeg|wav|aac|ogg|opus|wma))+$", "", fn, flags=re.I)
    fn = re.sub(r"_?compressed", "", fn, flags=re.I)
    fn = re.sub(r"\(\s*\d+(\.\d+)*\s*\)", "", fn)          # (1)  (9.7)
    fn = re.sub(r"\bתשפ\"?[א-ת]\b", "", fn)                  # year like תשפו
    if rabbi:                                                # partial / misspelled names
        parts = [x for x in rabbi.split() if x not in TITLES]
        sim = lambda t: any(difflib.SequenceMatcher(None, t.strip(".,"), x).ratio() >= 0.7 for x in parts)
        name_only = lambda toks: bool(toks) and all(t in TITLES or sim(t) for t in toks)
        segs = [x.strip() for x in fn.split(".")]
        while len(segs) > 1 and (not segs[-1] or name_only(segs[-1].split())):
            segs.pop()                                       # "…. הרב יוני מילוא"
        toks = ". ".join(x for x in segs if x).split()
        for i, t in enumerate(toks):                         # "… הרב לביא אנגלמן" at the end
            if t in TITLES and name_only(toks[i + 1:]):
                toks = toks[:i]
                break
        while toks and (toks[0] in TITLES or sim(toks[0])):  # "הרב עוזי …", "אלידע בר שאול …"
            toks.pop(0)
        fn = " ".join(toks)
    fn = tags.strip_rabbis(fn)
    fn = re.sub(r"[.\s\-–]*(הרב|הרבנית|ד\"ר)\s+\S+(\s+\S+)?[.\s]*$", "", fn)  # trailing unknown lecturer
    fn = re.sub(r"^(הרב|הרבנית)\s+", "", fn)
    fn = re.sub(r"\s*\.\s*", ". ", fn)
    return topic_from(fn, tags)


def tag_recording(rec: dict, lesson: dict, drive_name: str, tags: Tags, unknown: set) -> dict:
    rec_title = clean(rec.get("title"))
    parts = re.split(r"\s+-\s+", rec_title)
    rec_tail = parts[-1] if len(parts) > 1 else ""
    l_title = clean(lesson.get("title"))
    l_cat = clean(lesson.get("category"))
    l_desc = clean(lesson.get("description"))
    if "drive.google" in l_desc:
        l_desc = ""
    texts_all = " | ".join([l_title, l_cat, rec_title, drive_name, l_desc])

    # Lecturer
    rabbi = (tags.canon_rabbi(lesson.get("teacher"), unknown)
             or tags.canon_rabbi(rec_tail, unknown)
             or tags.find_rabbi(texts_all))
    if not rabbi:
        m = re.search(r"מעביר:\s*((?:הרב|ד\"ר|הרבנית)\s+\S+(?:\s+\S+)?)", l_desc)
        if m:
            rabbi = tags.canon_rabbi(m[1], unknown)
    rabbi_found = bool(rabbi)
    rabbi = rabbi or tags.unknown_rabbi

    program = match_list(tags.programs, " | ".join([l_title, l_cat, rec_title]))
    series, part = detect_series([l_title, l_cat, " - ".join(parts[:-1]) or rec_title], tags)

    # Occasion
    occasion = match_list(tags.occasions, texts_all)
    date = parse_date(lesson.get("date"))
    if not occasion and (key(l_cat) in tags.holiday_categories or "חול המועד" in texts_all
                         or re.search(r"(הכנה ל|לקראת)", texts_all)):
        if date:
            occasion = upcoming_holiday(date, tags.lookahead)
        elif parse_date(rec.get("uploadDate")):
            occasion = upcoming_holiday(parse_date(rec["uploadDate"]) - dt.timedelta(days=7),
                                        tags.lookahead)

    # Topic: most specific candidate wins
    cands = [topic_from(l_title, tags),
             topic_from(" - ".join(parts[:-1]) if len(parts) > 1 else rec_title, tags)]
    desc_topic = re.sub(r"מעביר:?", "", l_desc)
    if desc_topic and len(desc_topic) <= 60 and "http" not in desc_topic:
        cands.append(topic_from(desc_topic, tags))
    file_topic = filename_topic(drive_name, rabbi, tags)
    # A lesson titled only "הכנה ל…"/"לקראת …" is less informative than a real file name.
    if file_topic and re.match(r"^(הכנה ל|לקראת)", cands[0] or "") and key(file_topic) not in GENERIC_TOPICS:
        cands.insert(0, file_topic)
    elif file_topic:
        cands.append(file_topic)
    # a topic that is just a spelling of the series name becomes the series name
    for se in tags.series:
        if series == se["name"]:
            cands = [se["name"] if c and any(key(c) == key(m) for m in se["match"]) else c for c in cands]
    weak = set(GENERIC_TOPICS)
    for p in tags.programs:
        weak |= {key(m) for m in p["match"]} | {key(p["name"])}
    if series:
        weak.add(key(series))
    topic = next((c for c in cands if c and key(c) not in weak
                  and not (program and contains(program, c))), "")
    topic_specific = bool(topic)
    if not topic:
        if series:
            topic = series
        elif program:
            topic = program
        elif occasion:
            topic = f"שיעור ל{occasion}" if not occasion.startswith("חודש") else occasion
        else:
            topic = l_cat if l_cat and key(l_cat) not in GENERIC_TOPICS and not tags.find_rabbi(l_cat) else "שיעור"
    if series and topic_specific and not contains(topic, series):
        topic = f"{series} – {topic}"
    if series and part and "מפגש" not in topic:
        topic = f"{topic}, מפגש {part}"
    if program and topic_specific and occasion and not contains(topic, occasion) and key(topic) == key(program):
        topic = f"{topic} – {occasion}"

    location = clean(lesson.get("location"))
    if key(location) in tags.placeholders:
        location = ""

    return {
        "rabbi": rabbi, "rabbi_found": rabbi_found, "topic": topic,
        "topic_specific": topic_specific, "occasion": occasion, "series": series,
        "part": part, "program": program, "date": date, "location": location,
        "category": l_cat,
    }


def excluded(rec, lesson, tags) -> bool:
    blob = " ".join(clean(x) for x in [rec.get("title"), lesson.get("title"), lesson.get("teacher")])
    return any(x in blob for x in tags.exclude)


def drive_id(url: str) -> str | None:
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", url or "") or re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", url or "")
    return m[1] if m else None


def build_episodes(recs, lessons, state, tags, cfg):
    unknown: set = set()
    episodes, skipped = [], []
    for rec in recs:
        rid = str(rec.get("id") or "")
        did = drive_id(rec.get("url", ""))
        lesson = lessons.get(rec.get("lessonId"), {})
        if not rid or not did:
            skipped.append((rec.get("title"), "not a Google Drive link"))
            continue
        if excluded(rec, lesson, tags):
            skipped.append((rec.get("title"), "excluded (test entry)"))
            continue
        st = state["episodes"].get(rid, {})
        meta = tag_recording(rec, lesson, st.get("drive_name", ""), tags, unknown)
        meta.update(id=rid, drive_id=did, upload_date=parse_date(rec.get("uploadDate")),
                    lesson_found=bool(lesson), rec_title=clean(rec.get("title")))
        meta["pub_date"] = meta["date"] or meta["upload_date"] or dt.date.today()
        episodes.append(meta)

    episodes.sort(key=lambda e: (e["pub_date"], e["id"]))
    tpl = cfg.get("episode_title", "{rabbi} | {topic}")
    for e in episodes:
        e["title"] = tpl.format(rabbi=e["rabbi"], topic=e["topic"])
    # Disambiguate identical titles with the Hebrew date, then part numbers.
    by_title: dict[str, list] = {}
    for e in episodes:
        by_title.setdefault(e["title"], []).append(e)
    for title, group in by_title.items():
        if len(group) > 1:
            for e in group:
                e["title"] = f"{title} · {hebrew_date(e['pub_date'])}"
    by_title = {}
    for e in episodes:
        by_title.setdefault(e["title"], []).append(e)
    for title, group in by_title.items():
        if len(group) > 1:
            for i, e in enumerate(group, 1):
                e["title"] = f"{title} (חלק {i})"
    return episodes, skipped, unknown


# ─────────────────────────────── audio pipeline ───────────────────────────────

def download_drive(fid: str, dest: Path) -> str:
    """Download a public Drive file, getting past the large-file virus-scan page.
    Returns the original file name."""
    s = requests.Session()
    s.headers.update(UA)
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if api_key:
        r = s.get(f"https://www.googleapis.com/drive/v3/files/{fid}",
                  params={"alt": "media", "key": api_key}, stream=True, timeout=120)
        name = ""
        try:
            name = s.get(f"https://www.googleapis.com/drive/v3/files/{fid}",
                         params={"fields": "name", "key": api_key}, timeout=30).json().get("name", "")
        except Exception:  # noqa: BLE001
            pass
    else:
        r = s.get("https://drive.usercontent.google.com/download",
                  params={"id": fid, "export": "download", "confirm": "t"}, stream=True, timeout=120)
        if "text/html" in r.headers.get("content-type", ""):
            page = r.text
            action = re.search(r'action="([^"]+)"', page)
            fields = dict(re.findall(r'name="([^"]+)"\s+value="([^"]*)"', page))
            if not action:
                raise RuntimeError("Drive returned a web page instead of audio "
                                   "(file not shared publicly, or download quota exceeded)")
            r = s.get(html.unescape(action[1]), params=fields, stream=True, timeout=120)
        name = ""
        cd = r.headers.get("content-disposition", "")
        m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="([^"]+)"', cd)
        if m:
            name = fix_mojibake(unquote(m[1]))
    r.raise_for_status()
    if "text/html" in r.headers.get("content-type", ""):
        raise RuntimeError("Drive returned a web page instead of audio")
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    if dest.stat().st_size < 50_000:
        raise RuntimeError(f"downloaded file is suspiciously small ({dest.stat().st_size} bytes)")
    return clean(name)


def probe_duration(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return int(float(out))


def encode(src: Path, dst: Path, meta: dict, show_title: str, acfg: dict):
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn",
           "-ac", str(acfg.get("channels", 1)), "-ar", "44100",
           "-b:a", acfg.get("bitrate", "64k"), "-map_metadata", "-1", "-id3v2_version", "3",
           "-metadata", f"title={meta['title']}", "-metadata", f"artist={meta['rabbi']}",
           "-metadata", f"album={show_title}", "-metadata", f"date={meta['pub_date'].year}"]
    if acfg.get("normalize_loudness", True):
        cmd[cmd.index("-vn") + 1:cmd.index("-vn") + 1] = ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    cmd.append(str(dst))
    subprocess.run(cmd, check=True)


class Releases:
    def __init__(self, repo: str, prefix: str):
        self.repo, self.prefix, self.known = repo, prefix, set()

    def upload(self, path: Path, year: int) -> str:
        tag = f"{self.prefix}-{year}"
        if tag not in self.known:
            exists = subprocess.run(["gh", "release", "view", tag, "--repo", self.repo],
                                    capture_output=True).returncode == 0
            if not exists:
                subprocess.run(["gh", "release", "create", tag, "--repo", self.repo,
                                "--title", f"Audio {year}",
                                "--notes", "Podcast audio files (managed by the bot, do not edit)."],
                               check=True)
            self.known.add(tag)
        subprocess.run(["gh", "release", "upload", tag, str(path), "--repo", self.repo,
                        "--clobber"], check=True)
        return f"https://github.com/{self.repo}/releases/download/{tag}/{path.name}"


def process_new(episodes, recs_by_id, lessons, state, tags, cfg, args):
    todo = [e for e in episodes if e["id"] not in state["episodes"]]
    # reuse audio already uploaded for the same Drive file
    by_drive = {v["drive_id"]: v for v in state["episodes"].values() if v.get("drive_id")}
    log(f"new recordings to process: {len(todo)}")
    if args.dry_run or not todo:
        return
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo and not args.no_upload:
        sys.exit("ERROR: GITHUB_REPOSITORY not set (run inside GitHub Actions, or use --no-upload)")
    rel = Releases(repo, cfg["audio"].get("release_prefix", "audio")) if repo else None
    budget = cfg["run"].get("time_budget_minutes", 300) * 60
    done = 0
    for e in todo:
        if args.limit and done >= args.limit:
            log("limit reached"); break
        if time.time() - START > budget:
            log("time budget reached; the rest will be processed next run"); break
        rid = e["id"]
        if e["drive_id"] in by_drive:
            state["episodes"][rid] = {**by_drive[e["drive_id"]], "reused_from_drive": True}
            continue
        log(f"→ {e['title']}  [{e['drive_id']}]")
        tmp = Path(tempfile.mkdtemp())
        try:
            raw = tmp / "source"
            name = download_drive(e["drive_id"], raw) if not args.fake_download else fake_audio(raw)
            # re-tag now that the Drive file name is known
            rec = recs_by_id[rid]
            meta = tag_recording(rec, lessons.get(rec.get("lessonId"), {}), name, tags, set())
            meta.update(pub_date=e["pub_date"], title=e["title"])
            out = tmp / f"{e['drive_id']}.mp3"
            encode(raw, out, {**e, **{k: meta[k] for k in ("rabbi",)}}, cfg["show"]["title"], cfg["audio"])
            if rel and not args.no_upload:
                url = rel.upload(out, e["pub_date"].year)
            else:
                local = DOCS / "audio-test"
                local.mkdir(parents=True, exist_ok=True)
                shutil.copy(out, local / out.name)
                url = f"audio-test/{out.name}"
            state["episodes"][rid] = {
                "drive_id": e["drive_id"], "drive_name": name, "url": url,
                "bytes": out.stat().st_size, "duration": probe_duration(out),
                "processed": dt.datetime.now(TZ).isoformat(timespec="seconds"),
            }
            by_drive[e["drive_id"]] = state["episodes"][rid]
            state["failed"].pop(rid, None)
            done += 1
            save_state(state)
        except Exception as ex:  # noqa: BLE001
            f = state["failed"].setdefault(rid, {"attempts": 0})
            err = str(ex)
            if "signin" in err or "web page" in err:
                err = "הקובץ לא משותף לכל מי שיש לו את הקישור"
            f.update(attempts=f["attempts"] + 1, error=err[:300], title=e["title"], drive_id=e["drive_id"],
                     last=dt.date.today().isoformat())
            log(f"   FAILED: {ex}")
            save_state(state)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    log(f"processed {done} new recordings")


def fake_audio(dest: Path) -> str:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=3",
                    "-b:a", "128k", "-f", "mp3", str(dest)], check=True)
    return "שיעור לדוגמה. הרב ישראל ישראלי.mp3"


# ─────────────────────────────── state ───────────────────────────────

def load_state():
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text("utf-8"))
    else:
        s = {}
    s.setdefault("episodes", {})
    s.setdefault("failed", {})
    for v in s["episodes"].values():          # repair names saved before the encoding fix
        if v.get("drive_name"):
            v["drive_name"] = clean(fix_mojibake(v["drive_name"]))
    return s


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")


# ─────────────────────────────── outputs ───────────────────────────────

def pages_url() -> str:
    if os.environ.get("PAGES_URL"):
        return os.environ["PAGES_URL"].rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "owner/mirpeset-podcast")
    owner, name = repo.split("/")
    return f"https://{owner.lower()}.github.io/{name}"


def hashtag(s: str) -> str:
    return "#" + re.sub(r"[\s\-–]+", "_", re.sub(r"[\"'.,:()]", "", s))


def episode_text(e) -> tuple[str, str, list[str]]:
    lines = [e["topic"], f"מעביר: {e['rabbi']}"]
    if e["series"]:
        lines.append(f"סדרה: {e['series']}" + (f" · מפגש {e['part']}" if e["part"] else ""))
    if e["program"]:
        lines.append(f"מסגרת: {e['program']}")
    if e["occasion"]:
        lines.append(f"מועד: {e['occasion']}")
    lines.append(f"תאריך: {hebrew_date(e['pub_date'])} ({e['pub_date'].strftime('%d.%m.%Y')})")
    if e["location"]:
        lines.append(f"מקום: {e['location']}")
    tags = [e["rabbi"]] + [x for x in (e["occasion"], e["series"], e["program"]) if x]
    tag_line = " ".join(hashtag(t) for t in tags)
    plain = "\n".join(lines) + "\n\n" + tag_line
    rich = "<p>" + "<br>".join(html.escape(x) for x in lines) + "</p><p>" + html.escape(tag_line) + "</p>"
    return plain, rich, tags


def cdata(s: str) -> str:
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def write_feed(episodes, state, cfg):
    show = cfg["show"]
    base = pages_url()
    now = format_datetime(dt.datetime.now(TZ))
    items = []
    for e in sorted(episodes, key=lambda x: (x["pub_date"], x["id"]), reverse=True):
        st = state["episodes"].get(e["id"])
        if not st or not st.get("url"):
            continue
        url = st["url"] if st["url"].startswith("http") else f"{base}/{st['url']}"
        plain, rich, tags = episode_text(e)
        pub = dt.datetime.combine(e["pub_date"], dt.time(20, 0), TZ)
        items.append(f"""    <item>
      <title>{escape(e['title'])}</title>
      <description>{cdata(rich)}</description>
      <content:encoded>{cdata(rich)}</content:encoded>
      <itunes:summary>{escape(plain)}</itunes:summary>
      <itunes:author>{escape(e['rabbi'])}</itunes:author>
      <itunes:keywords>{escape(','.join(tags))}</itunes:keywords>
      <enclosure url="{escape(url)}" length="{st.get('bytes', 0)}" type="audio/mpeg"/>
      <guid isPermaLink="false">mirpeset-rec-{e['id']}</guid>
      <pubDate>{format_datetime(pub)}</pubDate>
      <itunes:duration>{st.get('duration', 0)}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
      <podcast:person role="host">{escape(e['rabbi'])}</podcast:person>
    </item>""")
    explicit = "true" if show.get("explicit") else "false"
    sub = f'\n      <itunes:category text="{escape(show["subcategory"])}"/>' if show.get("subcategory") else ""
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:atom="http://www.w3.org/2005/Atom"
  xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{escape(show['title'])}</title>
    <link>{escape(show.get('website') or base)}</link>
    <atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{cdata(show['description'])}</description>
    <itunes:summary>{escape(show['description'])}</itunes:summary>
    <language>{show.get('language', 'he')}</language>
    <copyright>{escape(show.get('author', ''))}</copyright>
    <itunes:author>{escape(show.get('author', ''))}</itunes:author>
    <itunes:owner>
      <itunes:name>{escape(show.get('owner_name', ''))}</itunes:name>
      <itunes:email>{escape(show['owner_email'])}</itunes:email>
    </itunes:owner>
    <itunes:image href="{base}/cover.jpg"/>
    <image><url>{base}/cover.jpg</url><title>{escape(show['title'])}</title><link>{escape(show.get('website') or base)}</link></image>
    <itunes:category text="{escape(show['category'])}">{sub}
    </itunes:category>
    <itunes:explicit>{explicit}</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <podcast:locked>no</podcast:locked>
    <lastBuildDate>{now}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""
    DOCS.mkdir(exist_ok=True)
    (DOCS / "feed.xml").write_text(feed, "utf-8")
    (DOCS / ".nojekyll").write_text("")
    return len(items)


def make_cover(cfg, fixtures: Path | None):
    out = DOCS / "cover.jpg"
    marker = DOCS / ".cover-source"
    src = cfg["show"].get("cover_source", "")
    if out.exists() and marker.exists() and marker.read_text().strip() == src:
        return
    from PIL import Image
    try:
        if fixtures and (fixtures / "cover_source.jpg").exists():
            data = (fixtures / "cover_source.jpg").read_bytes()
        else:
            data = requests.get(src, headers=UA, timeout=60).content
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        log(f"cover: could not load {src}: {e}")
        return
    w, h = im.size
    side = max(w, h)
    px = [im.getpixel(p) for p in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]]
    bg = tuple(sum(c[i] for c in px) // 4 for i in range(3))
    canvas = Image.new("RGB", (side, side), bg)
    canvas.paste(im, ((side - w) // 2, (side - h) // 2))
    canvas = canvas.resize((1500, 1500), Image.LANCZOS)
    DOCS.mkdir(exist_ok=True)
    canvas.save(out, "JPEG", quality=90)
    marker.write_text(src)
    log("cover: built docs/cover.jpg (1500x1500)")


def write_index(episodes, state, cfg):
    base = pages_url()
    show = cfg["show"]
    rows = []
    for e in sorted(episodes, key=lambda x: x["pub_date"], reverse=True):
        st = state["episodes"].get(e["id"])
        if not st:
            continue
        url = st["url"] if st["url"].startswith("http") else st["url"]
        rows.append({"t": e["title"], "r": e["rabbi"], "o": e["occasion"] or "", "s": e["series"] or "",
                     "p": e["program"] or "", "d": hebrew_date(e["pub_date"]),
                     "g": e["pub_date"].isoformat(), "u": url, "len": st.get("duration", 0)})
    data = json.dumps(rows, ensure_ascii=False)
    spotify = show.get("spotify_url", "")
    page = f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(show['title'])}</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI",Arial,sans-serif;max-width:900px;margin:0 auto;padding:16px;color:#1f2937;background:#fafafa}}
header{{display:flex;gap:16px;align-items:center}} header img{{width:96px;height:96px;border-radius:12px}}
h1{{margin:0;font-size:1.5rem}} .links a{{margin-inline-end:12px}}
.filters{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}} select,input{{padding:6px;font:inherit}}
li{{list-style:none;background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px 12px;margin:8px 0}}
ul{{padding:0}} .meta{{color:#6b7280;font-size:.85rem}} .tag{{background:#eef2ff;color:#3730a3;border-radius:999px;padding:1px 8px;margin-inline-start:4px;font-size:.8rem}}
audio{{width:100%;margin-top:6px}}
</style></head><body>
<header><img src="cover.jpg" alt=""><div><h1>{html.escape(show['title'])}</h1>
<p>{html.escape(show['description'])}</p>
<p class="links">{f'<a href="{html.escape(spotify)}">האזנה בספוטיפיי</a>' if spotify else ''}<a href="feed.xml">RSS</a></p></div></header>
<div class="filters">
<select id="fr"><option value="">כל המרצים</option></select>
<select id="fo"><option value="">כל המועדים</option></select>
<select id="fs"><option value="">כל הסדרות והמסגרות</option></select>
<input id="q" placeholder="חיפוש…">
<span id="n" class="meta"></span></div>
<ul id="list"></ul>
<script>
const D={data};
const fill=(id,vals)=>{{const s=document.getElementById(id);[...new Set(vals.filter(Boolean))].sort((a,b)=>a.localeCompare(b,'he')).forEach(v=>{{const o=document.createElement('option');o.textContent=v;s.appendChild(o)}})}};
fill('fr',D.map(x=>x.r));fill('fo',D.map(x=>x.o));fill('fs',D.flatMap(x=>[x.s,x.p]));
function render(){{const r=fr.value,o=fo.value,s=fs.value,q=document.getElementById('q').value.trim();
const L=D.filter(x=>(!r||x.r===r)&&(!o||x.o===o)&&(!s||x.s===s||x.p===s)&&(!q||x.t.includes(q)));
n.textContent=L.length+' שיעורים';
list.innerHTML=L.map(x=>`<li><b>${{x.t}}</b><div class="meta">${{x.d}} · ${{Math.round(x.len/60)}} דק׳${{[x.o,x.s,x.p].filter(Boolean).map(t=>`<span class="tag">${{t}}</span>`).join('')}}</div><audio controls preload="none" src="${{x.u}}"></audio></li>`).join('')}}
['fr','fo','fs','q'].forEach(i=>document.getElementById(i).addEventListener('input',render));render();
</script></body></html>
"""
    (DOCS / "index.html").write_text(page, "utf-8")


def write_report(episodes, skipped, unknown, state, recs_ids, n_feed):
    lines = ["# דוח תיוג – המרפסת פודקאסט", "",
             f"עודכן: {dt.datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}", "",
             f"- פרקים בפיד: **{n_feed}**",
             f"- הקלטות שזוהו: {len(episodes)}",
             f"- ממתינות לעיבוד: {sum(1 for e in episodes if e['id'] not in state['episodes'])}",
             f"- נכשלו: {len(state['failed'])}", ""]

    def section(title, rows, hint=""):
        if not rows:
            return
        lines.extend([f"## {title} ({len(rows)})", ""])
        if hint:
            lines.extend([hint, ""])
        lines.extend(f"- {r}" for r in rows)
        lines.append("")

    section("שמות מרצים שלא מופיעים ב-tags.yml", sorted(unknown),
            "אם זה שם נכון – הוסיפו אותו ל-rabbis. אם זה כתיב אחר של מרצה קיים – הוסיפו אותו לרשימת הכתיבים שלו.")
    section("הקלטות בלי מרצה מזוהה", [e["rec_title"] or e["title"] for e in episodes if not e["rabbi_found"]],
            "יופיעו תחת השם החלופי. כדאי להשלים את שם המרצה באפליקציה.")
    section("הקלטות בלי נושא ספציפי", [e["title"] for e in episodes if not e["topic_specific"]],
            "הנושא נגזר מהסדרה/המסגרת/המועד. כדאי לתת לשיעור כותרת באפליקציה.")
    section("הקלטות שלא נמצא להן שיעור", [e["rec_title"] for e in episodes if not e["lesson_found"]])
    section("הורדות שנכשלו", [f"{v.get('title')} – {v.get('error')} (ניסיונות: {v.get('attempts')})"
                               + (f" – https://drive.google.com/file/d/{v['drive_id']}/view" if v.get("drive_id") else "")
                               for v in state["failed"].values()],
            "פתחו כל קישור ← שיתוף ← \"כל מי שיש לו את הקישור\" (צופה). הבוט ינסה שוב בריצה הבאה.")
    section("דולגו", [f"{t} – {why}" for t, why in skipped])
    gone = [k for k in state["episodes"] if k not in recs_ids]
    section("הוסרו מהאפליקציה (ולכן מהפיד)", gone)

    lines.extend(["## כל הפרקים", "", "| תאריך | כותרת | מועד | סדרה / מסגרת |", "|---|---|---|---|"])
    for e in sorted(episodes, key=lambda x: x["pub_date"], reverse=True):
        sp = " / ".join(x for x in (e["series"], e["program"]) if x)
        cell = lambda x: (x or "").replace("|", "\\|")
        lines.append(f"| {e['pub_date'].strftime('%d.%m.%Y')} | {cell(e['title'])} | {cell(e['occasion'])} | {cell(sp)} |")
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines) + "\n", "utf-8")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write("\n".join(lines[:200]) + "\n")


# ─────────────────────────────── main ───────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no downloads/uploads")
    ap.add_argument("--no-upload", action="store_true", help="keep audio locally in docs/audio-test")
    ap.add_argument("--fake-download", action="store_true", help="testing: generate audio instead of Drive")
    ap.add_argument("--fixtures", type=Path, help="read sources from a local folder")
    ap.add_argument("--limit", type=int, default=0, help="max new recordings this run")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yml").read_text("utf-8"))
    tags = Tags(yaml.safe_load((ROOT / "tags.yml").read_text("utf-8")))
    state = load_state()

    recs, lj, sb = load_sources(cfg, args.fixtures)
    lessons = {}
    for l in lj:
        lessons[l.get("id")] = l
    for l in sb:  # Supabase is newer; it wins when both have the lesson
        lessons[l.get("id")] = l
    recs_by_id = {str(r.get("id")): r for r in recs}

    episodes, skipped, unknown = build_episodes(recs, lessons, state, tags, cfg)
    prev = len(state["episodes"])
    if prev and len(episodes) < prev * 0.5:
        sys.exit(f"ERROR: only {len(episodes)} recordings found vs {prev} already published. "
                 "Looks like a source problem; leaving the feed unchanged.")

    process_new(episodes, recs_by_id, lessons, state, tags, cfg, args)
    if not args.dry_run:
        save_state(state)
    # rebuild tags now that Drive file names are known
    episodes, skipped, unknown = build_episodes(recs, lessons, state, tags, cfg)

    make_cover(cfg, args.fixtures)
    if args.dry_run:
        # in a dry run, show every episode in the outputs with placeholder audio
        state = {**state, "episodes": {e["id"]: state["episodes"].get(e["id"]) or
                                       {"url": "#", "bytes": 0, "duration": 0} for e in episodes}}
    n = write_feed(episodes, state, cfg)
    write_index(episodes, state, cfg)
    write_report(episodes, skipped, unknown, state, set(recs_by_id), n)
    log(f"feed: {n} episodes → docs/feed.xml · report → reports/review.md")


if __name__ == "__main__":
    main()
