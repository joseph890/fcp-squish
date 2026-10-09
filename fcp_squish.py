#!/usr/bin/env python3
"""
fcp_squish.py - overlap the clips of a Final Cut Pro project so the audio flows.

Given a video file and a Final Cut Pro XML export (.fcpxml or .fcpxmld) whose
timeline is a run of tightly cut clips from that video (e.g. one line of a
script per clip), this writes a NEW .fcpxml in which every clip slightly
overlaps the clip before it - by 2 frames by default.

Clips alternate between lanes 1 and 2, so every other clip sits on top of the
clip before it and the rest tuck underneath. Each clip is its own connected
clip, so you can drag any one of them to fine-tune its overlap.

The audio is checked at every cut: if the silence between two lines is
shorter than the overlap, the overlap is shortened so the next line doesn't
start before the previous one has finished. A short audio crossfade covers
each overlap (disable with --no-fades).

Requirements: Python 3.8+, numpy, and ffmpeg on your PATH.

Usage:
    python3 fcp_squish.py VIDEO PROJECT.fcpxml [-o OUTPUT.fcpxml]

In Final Cut Pro: select the project, File > Export XML... to get the
.fcpxml. Afterwards, File > Import > XML... to bring the new project back.
"""

import argparse
import copy
import os
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from fractions import Fraction

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("This script needs numpy:  pip3 install numpy")


# --------------------------------------------------------------------------
# FCPXML time helpers ("1001/30000s", "5s", "0s")
# --------------------------------------------------------------------------

def parse_time(value, default=Fraction(0)):
    if value is None or value == "":
        return default
    v = value.strip()
    if v.endswith("s"):
        v = v[:-1]
    if "/" in v:
        num, den = v.split("/")
        return Fraction(int(num), int(den))
    return Fraction(v)


def fmt_time(t):
    t = Fraction(t)
    if t.denominator == 1:
        return "%ds" % t.numerator
    return "%d/%ds" % (t.numerator, t.denominator)


# --------------------------------------------------------------------------
# Loading the project
# --------------------------------------------------------------------------

MEDIA_TAGS = {"asset-clip", "clip", "ref-clip", "sync-clip", "mc-clip",
              "video", "audio", "title"}

# Children of a clip that must come before <adjust-volume> (DTD order).
BEFORE_VOLUME = {
    "note", "conform-rate", "timeMap",
    "adjust-crop", "adjust-corners", "adjust-conform", "adjust-transform",
    "adjust-blend", "adjust-stabilization", "adjust-rollingShutter",
    "adjust-360-transform", "adjust-reorient", "adjust-orientation",
    "adjust-cinematic", "adjust-colorConform", "adjust-stereo-3D",
    "adjust-loudness", "adjust-noiseReduction", "adjust-humReduction",
    "adjust-EQ", "adjust-matchEQ",
}


def load_fcpxml(path):
    if os.path.isdir(path):  # .fcpxmld bundle
        inner = os.path.join(path, "Info.fcpxml")
        if not os.path.exists(inner):
            sys.exit("Could not find Info.fcpxml inside %s" % path)
        path = inner
    if path.endswith(".fcpbundle"):
        sys.exit("That is a library, not an XML export. In Final Cut Pro use "
                 "File > Export XML... and pass the .fcpxml file.")
    return ET.parse(path)


def asset_path(asset):
    rep = asset.find("media-rep")
    src = rep.get("src") if rep is not None else asset.get("src")
    if not src:
        return None
    parsed = urllib.parse.urlparse(src)
    if parsed.scheme in ("file", ""):
        return urllib.parse.unquote(parsed.path)
    return None


class Item:
    """One element of the original primary storyline."""

    def __init__(self, elem, assets):
        self.elem = elem
        self.tag = elem.tag
        self.name = elem.get("name", elem.tag)
        self.offset = parse_time(elem.get("offset"))
        self.start = parse_time(elem.get("start"))
        self.duration = parse_time(elem.get("duration"))
        self.asset_id = None
        self.file_in = None  # seconds into the media file where the clip starts
        self.why_not = None
        self._resolve_source(assets)

    @property
    def file_out(self):
        return self.file_in + self.duration

    def _resolve_source(self, assets):
        e = self.elem
        if e.find("timeMap") is not None:
            self.why_not = "clip is retimed"
            return
        if self.tag == "asset-clip":
            ref, inner_start, inner_offset = e.get("ref"), Fraction(0), Fraction(0)
            media_time = self.start
        elif self.tag == "clip":
            inner = None
            for child in e:
                if child.tag in ("video", "audio", "asset-clip") and child.get("ref"):
                    inner = child
                    break
            if inner is None:
                self.why_not = "clip has no media reference"
                return
            if inner.find("timeMap") is not None:
                self.why_not = "clip is retimed"
                return
            ref = inner.get("ref")
            media_time = (parse_time(inner.get("start")) +
                          (self.start - parse_time(inner.get("offset"))))
        else:
            self.why_not = "%s is not supported for audio matching" % self.tag
            return
        asset = assets.get(ref)
        if asset is None:
            self.why_not = "referenced asset %s not found" % ref
            return
        self.asset_id = ref
        self.file_in = media_time - parse_time(asset.get("start"))


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

def decode_audio(path, sr):
    print("Decoding audio from %s ..." % path)
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn",
           "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, check=True).stdout
    except FileNotFoundError:
        sys.exit("ffmpeg was not found. Install it (e.g. `brew install ffmpeg`).")
    except subprocess.CalledProcessError:
        sys.exit("ffmpeg could not read audio from %s" % path)
    audio = np.frombuffer(out, dtype=np.int16)
    if audio.size == 0:
        sys.exit("No audio found in %s" % path)
    return audio


def segment(audio, start, length):
    """audio[start:start+length] as float, zero-padded outside the file."""
    out = np.zeros(length, dtype=np.float64)
    lo, hi = max(start, 0), min(start + length, audio.size)
    if hi > lo:
        out[lo - start:hi - start] = audio[lo:hi]
    return out


def quiet_edges(audio, sr, start, end, quiet_db):
    """
    Seconds of non-speech at the start and at the end of the part of the file
    between `start` and `end` (in seconds). Audio counts as non-speech when it
    is more than `quiet_db` dB below the clip's speaking level.
    """
    s0, s1 = int(round(start * sr)), int(round(end * sr))
    seg = segment(audio, s0, max(s1 - s0, 1))
    win = max(1, sr // 200)  # 5 ms windows
    n = seg.size // win
    if n == 0:
        return 0.0, 0.0
    rms = np.sqrt(np.mean(seg[:n * win].reshape(n, win) ** 2, axis=1)) + 1e-9
    db = 20 * np.log10(rms / 32768.0)
    level = np.percentile(db, 95)
    quiet = (db < level - quiet_db) | (db < -60)
    if quiet.all():
        return seg.size / sr, seg.size / sr
    head = int(np.argmin(quiet))          # first loud window
    tail = int(np.argmin(quiet[::-1]))    # first loud window from the end
    return head * win / sr, tail * win / sr


# --------------------------------------------------------------------------
# Building the new project
# --------------------------------------------------------------------------

def add_fades(elem, fade_in, fade_out, fade_type):
    if fade_in <= 0 and fade_out <= 0:
        return
    vol = elem.find("adjust-volume")
    if vol is None:
        idx = 0
        for i, child in enumerate(list(elem)):
            if child.tag in BEFORE_VOLUME:
                idx = i + 1
        vol = ET.Element("adjust-volume", {"amount": "0dB"})
        elem.insert(idx, vol)
    param = None
    for p in vol.findall("param"):
        if p.get("name") == "amount":
            param = p
    if param is None:
        param = ET.Element("param", {"name": "amount"})
        vol.insert(0, param)
    for tag in ("fadeIn", "fadeOut"):
        for old in param.findall(tag):
            param.remove(old)
    pos = 0
    if fade_in > 0:
        param.insert(pos, ET.Element("fadeIn", {"type": fade_type,
                                                "duration": fmt_time(fade_in)}))
        pos += 1
    if fade_out > 0:
        param.insert(pos, ET.Element("fadeOut", {"type": fade_type,
                                                 "duration": fmt_time(fade_out)}))


def main():
    ap = argparse.ArgumentParser(
        description="Overlap the clips of a Final Cut Pro project so each "
                    "clip's opening audio lines up with the end of the clip "
                    "before it.")
    ap.add_argument("video", help="the source video file used in the project")
    ap.add_argument("project", help="Final Cut Pro XML export (.fcpxml or .fcpxmld)")
    ap.add_argument("-o", "--output", help="output .fcpxml (default: <project> squished.fcpxml)")
    ap.add_argument("--project-name", help="which project to use if the XML has several")
    ap.add_argument("--overlap", default="2",
                    help="how much each clip overlaps the one before it: a number "
                         "of frames (e.g. 2) or seconds with an 's' (e.g. 0.1s). "
                         "Default 2 frames")
    ap.add_argument("--min-overlap", type=int, default=1,
                    help="never overlap less than this many frames, even if the "
                         "lines are so tight they touch (default 1)")
    ap.add_argument("--quiet-db", type=float, default=25.0,
                    help="audio this many dB below the speaking level counts as "
                         "silence when checking that lines don't collide (default 25)")
    ap.add_argument("--ignore-audio", action="store_true",
                    help="use exactly --overlap everywhere without checking the audio")
    ap.add_argument("--lanes", choices=["alternate", "stack"], default="alternate",
                    help="alternate: clips alternate between lanes 1 and 2, so every "
                         "other clip sits on top of the one before it and the rest "
                         "tuck underneath. stack: each clip goes one lane higher, so "
                         "the incoming clip is always on top (default alternate)")
    ap.add_argument("--no-fades", action="store_true",
                    help="don't add audio crossfades over the overlaps")
    ap.add_argument("--fade-type", default="linear",
                    choices=["linear", "easeIn", "smooth", "easeOut"],
                    help="crossfade shape (default linear)")
    ap.add_argument("--sample-rate", type=int, default=16000,
                    help="audio analysis sample rate (default 16000)")
    args = ap.parse_args()

    if not os.path.exists(args.video):
        sys.exit("Video not found: %s" % args.video)
    tree = load_fcpxml(args.project)
    root = tree.getroot()
    if root.tag != "fcpxml":
        sys.exit("%s does not look like an FCPXML file" % args.project)

    resources = root.find("resources")
    assets = {a.get("id"): a for a in resources.iter("asset")} if resources is not None else {}
    formats = {f.get("id"): f for f in resources.iter("format")} if resources is not None else {}

    projects = list(root.iter("project"))
    if not projects:
        sys.exit("No <project> found. Export the project (not just the event) as XML.")
    if args.project_name:
        projects = [p for p in projects if p.get("name") == args.project_name]
        if not projects:
            sys.exit("No project named %r" % args.project_name)
    elif len(projects) > 1:
        print("Note: XML contains %d projects; using %r (choose with --project-name)"
              % (len(projects), projects[0].get("name")))
    project = projects[0]
    sequence = project.find("sequence")
    spine = sequence.find("spine")

    fmt = formats.get(sequence.get("format"))
    frame = parse_time(fmt.get("frameDuration") if fmt is not None else None, Fraction(1, 30))
    if frame <= 0:
        frame = Fraction(1, 30)

    # Which asset is the video the user gave us?
    video_base = os.path.basename(args.video)
    used_assets = set()
    items, dropped = [], []
    for child in list(spine):
        if child.tag == "transition":
            dropped.append("transition %r" % child.get("name", ""))
            continue
        if child.tag == "gap":
            items.append(Item(child, assets))
            continue
        if child.tag not in MEDIA_TAGS:
            dropped.append(child.tag)
            continue
        item = Item(child, assets)
        items.append(item)
        if item.asset_id:
            used_assets.add(item.asset_id)

    matching = {aid for aid in used_assets
                if asset_path(assets[aid]) and os.path.basename(asset_path(assets[aid])) == video_base}
    if not matching and len(used_assets) == 1:
        matching = set(used_assets)
    if not matching:
        sys.exit("None of the clips in the project reference %s.\nAssets used: %s"
                 % (video_base, ", ".join(sorted(
                     os.path.basename(asset_path(assets[a]) or a) for a in used_assets))))
    for it in items:
        if it.asset_id and it.asset_id not in matching:
            it.why_not = "clip uses a different media file"
            it.asset_id = None

    media_items = [it for it in items if it.tag != "gap"]
    if len(media_items) < 2:
        sys.exit("The project needs at least two clips in its main storyline.")

    ov_text = args.overlap.strip()
    if ov_text.endswith("s"):
        target = int(round(float(ov_text[:-1]) / float(frame)))
    else:
        target = int(ov_text)
    target = max(target, args.min_overlap)

    audio = None
    if not args.ignore_audio:
        audio = decode_audio(args.video, args.sample_rate)
    sr = args.sample_rate

    # Work out the overlap for each clip with the media clip before it.
    overlaps = {}
    report = []
    prev = None
    for it in items:
        if it.tag == "gap":
            prev = None  # a gap breaks the chain; nothing to overlap with
            continue
        if prev is not None:
            # leave at least one frame of each clip uncovered
            limit = max(0, min(int(prev.duration / frame), int(it.duration / frame)) - 1)
            ov_frames, room, note = target, None, ""
            if audio is not None and prev.asset_id and it.asset_id:
                _, tail = quiet_edges(audio, sr, float(prev.file_in), float(prev.file_out),
                                      args.quiet_db)
                head, _ = quiet_edges(audio, sr, float(it.file_in), float(it.file_out),
                                      args.quiet_db)
                room = tail + head
                safe = int(room / float(frame))
                if safe < target:
                    ov_frames = max(safe, args.min_overlap)
                    note = ("lines touch at %d frame%s" % (ov_frames, "" if ov_frames == 1 else "s")
                            if safe < args.min_overlap else "shortened so lines don't collide")
            elif audio is not None:
                note = "audio not checked (%s)" % (prev.why_not or it.why_not)
            ov_frames = min(ov_frames, limit)
            overlaps[id(it)] = ov_frames * frame
            report.append((prev.name, it.name, ov_frames, ov_frames * frame, room, note))
        prev = it

    # Lay the clips out on the new timeline.
    base = items[0].offset
    pos = base
    placed = []  # (item, new_offset, lane)
    clip_index = 0
    for it in items:
        if it.tag == "gap":
            pos += it.duration
            continue
        ov = overlaps.get(id(it), Fraction(0))
        pos -= ov
        lane = (1 + clip_index % 2) if args.lanes == "alternate" else (1 + clip_index)
        placed.append((it, pos, lane, ov))
        pos += it.duration
        clip_index += 1
    total = pos - base

    # Build the new spine: one gap holding every clip as a connected clip.
    for child in list(spine):
        spine.remove(child)
    gap = ET.SubElement(spine, "gap", {
        "name": "Gap", "offset": fmt_time(base), "start": fmt_time(base),
        "duration": fmt_time(total)})
    lifted = []
    top_lane = max(l for _, _, l, _ in placed)
    for idx, (it, new_offset, lane, ov_in) in enumerate(placed):
        clip = copy.deepcopy(it.elem)
        clip.set("offset", fmt_time(new_offset))
        clip.set("lane", str(lane))
        # Clips that were connected to this clip move up to the gap.
        for child in list(clip):
            if child.get("lane") is not None:
                clip.remove(child)
                child_pos = new_offset + parse_time(child.get("offset")) - it.start
                child.set("offset", fmt_time(child_pos))
                child_lane = int(child.get("lane"))
                if child_lane > 0:
                    child.set("lane", str(child_lane + top_lane))
                lifted.append(child)
        if not args.no_fades:
            ov_out = Fraction(0)
            if idx + 1 < len(placed):
                nxt = placed[idx + 1]
                if nxt[1] < new_offset + it.duration:
                    ov_out = nxt[3]
            add_fades(clip, ov_in, ov_out, args.fade_type)
        gap.append(clip)
    for child in lifted:
        gap.append(child)

    sequence.set("duration", fmt_time(total))
    old_name = project.get("name", "Project")
    project.set("name", old_name + " (squished)")
    for attr in ("uid", "id", "modDate"):
        if attr in project.attrib:
            del project.attrib[attr]

    # Keep only the chosen project so importing creates just the new one.
    for parent in root.iter():
        for child in list(parent):
            if child.tag == "project" and child is not project:
                parent.remove(child)

    out = args.output
    if not out:
        stem = args.project.rstrip("/\\")
        for ext in (".fcpxmld", ".fcpxml"):
            if stem.endswith(ext):
                stem = stem[:-len(ext)]
        out = stem + " squished.fcpxml"
    if hasattr(ET, "indent"):
        ET.indent(tree, space="    ")
    with open(out, "wb") as fh:
        fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n')
        tree.write(fh, encoding="utf-8", xml_declaration=False)
        fh.write(b"\n")

    # Report
    print()
    print("'room' is the silence between the two lines: overlapping by more than")
    print("that makes the next line start before the previous one has finished.")
    print()
    print("%-4s %-24s %-24s %7s %8s %7s  %s" % ("#", "outgoing clip", "incoming clip",
                                              "frames", "seconds", "room", ""))
    for i, (a, b, fr, sec, room, note) in enumerate(report, 1):
        print("%-4d %-24.24s %-24.24s %7d %8.3f %7s  %s"
              % (i, a, b, fr, float(sec), "-" if room is None else "%.3f" % room, note))
    print()
    print("Clips: %d   cuts: %d   shortened or touching: %d"
          % (len(placed), len(report), sum(1 for r in report if r[5] and "audio" not in r[5])))
    print("Timeline length: %.2fs -> %.2fs"
          % (float(sum(it.duration for it in items)), float(total)))
    if dropped:
        print("Dropped from main storyline: %s" % ", ".join(dropped))
    print("Wrote %s" % out)
    print("Import it in Final Cut Pro with File > Import > XML...")


if __name__ == "__main__":
    main()
