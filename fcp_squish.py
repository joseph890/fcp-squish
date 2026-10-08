#!/usr/bin/env python3
"""
fcp_squish.py - overlap the clips of a Final Cut Pro project so the audio flows.

Given a video file and a Final Cut Pro XML export (.fcpxml or .fcpxmld) whose
timeline is made of cuts from that video, this writes a NEW .fcpxml in which
every clip is pulled earlier on the timeline so that it overlaps the clip
before it. The amount of overlap is chosen by comparing audio: the start of
each clip's audio is lined up with the matching audio at the end of the
previous clip, so the two play the same sound during the overlap and the
audio flows seamlessly across the cut.

Because clips in the primary storyline cannot overlap, the new project puts
all clips on connected lanes (alternating lanes 1 and 2 by default) above a
single gap, so the video overlaps too. A linear audio crossfade is added
over each overlap (disable with --no-fades).

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


def best_overlap(audio, sr, a_end, b_start, frame, min_frames, max_frames):
    """
    Find how many frames k to overlap so that the last k frames of clip A
    (ending at a_end seconds in the file) sound the same as the first k
    frames of clip B (starting at b_start seconds). Returns (k, score) where
    score is the normalised correlation (1.0 = identical audio).
    """
    if max_frames < min_frames:
        return 0, 0.0
    width = int(round(max_frames * frame * sr))
    a_end_s = int(round(a_end * sr))
    b_start_s = int(round(b_start * sr))
    tail = segment(audio, a_end_s - width, width)
    head = segment(audio, b_start_s, width)

    # dot[k] = sum(tail[width-k:] * head[:k]) via FFT cross-correlation
    n = 1 << int(np.ceil(np.log2(2 * width)))
    corr = np.fft.irfft(np.fft.rfft(tail, n) * np.conj(np.fft.rfft(head, n)), n)
    e_tail = np.cumsum(tail[::-1] ** 2)
    e_head = np.cumsum(head ** 2)

    best_k, best_score = 0, 0.0
    for k in range(min_frames, max_frames + 1):
        ks = min(int(round(k * frame * sr)), width)
        if ks <= 0:
            continue
        denom = np.sqrt(e_tail[ks - 1] * e_head[ks - 1])
        if denom <= 1e-9:
            continue
        score = corr[width - ks] / denom
        if score > best_score:
            best_k, best_score = k, float(score)
    return best_k, best_score


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
    ap.add_argument("--max-overlap", type=float, default=3.0,
                    help="longest overlap to search for, in seconds (default 3.0)")
    ap.add_argument("--min-overlap", type=int, default=2,
                    help="shortest overlap to accept, in frames (default 2)")
    ap.add_argument("--threshold", type=float, default=0.7,
                    help="minimum audio match score 0-1 to accept an overlap (default 0.7)")
    ap.add_argument("--fallback-frames", type=int, default=0,
                    help="overlap (in frames) to use when no audio match is found (default 0)")
    ap.add_argument("--lanes", choices=["alternate", "stack"], default="alternate",
                    help="alternate: clips alternate between lanes 1 and 2. "
                         "stack: each clip goes one lane higher, so the incoming "
                         "clip's video is always on top (default alternate)")
    ap.add_argument("--no-fades", action="store_true",
                    help="don't add audio crossfades over the overlaps")
    ap.add_argument("--fade-type", default="linear",
                    choices=["linear", "easeIn", "smooth", "easeOut"],
                    help="crossfade shape (default linear, best for identical audio)")
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

    audio = decode_audio(args.video, args.sample_rate)
    sr = args.sample_rate
    max_frames_cfg = int(args.max_overlap / float(frame))

    # Work out the overlap for each clip with the media clip before it.
    overlaps = {}
    report = []
    prev = None
    for it in items:
        if it.tag == "gap":
            prev = None  # a gap breaks the chain; nothing to overlap with
            continue
        if prev is not None:
            ov_frames, score, method = 0, 0.0, "-"
            if prev.asset_id and it.asset_id:
                # leave at least one frame of each clip uncovered
                limit = min(int(prev.duration / frame), int(it.duration / frame)) - 1
                # Exact answer if the two clips share source material.
                if (prev.asset_id == it.asset_id and
                        prev.file_in <= it.file_in < prev.file_out):
                    ov_frames = int(round((prev.file_out - it.file_in) / frame))
                    ov_frames = max(0, min(ov_frames, limit))
                    score, method = 1.0, "same source"
                else:
                    k, score = best_overlap(audio, sr, float(prev.file_out), float(it.file_in),
                                            float(frame), args.min_overlap,
                                            min(max_frames_cfg, limit))
                    if score >= args.threshold and k > 0:
                        ov_frames, method = k, "audio match"
                    else:
                        ov_frames, method = 0, "no match"
            else:
                method = "skipped (%s)" % (prev.why_not or it.why_not)
            if ov_frames == 0 and args.fallback_frames > 0:
                limit = min(int(prev.duration / frame), int(it.duration / frame)) - 1
                ov_frames = max(0, min(args.fallback_frames, limit))
                method += ", fallback"
            overlaps[id(it)] = ov_frames * frame
            report.append((prev.name, it.name, ov_frames, ov_frames * frame, score, method))
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
    print("%-4s %-28s %-28s %8s %8s %6s  %s" % ("#", "outgoing clip", "incoming clip",
                                              "frames", "seconds", "score", "how"))
    for i, (a, b, fr, sec, score, method) in enumerate(report, 1):
        print("%-4d %-28.28s %-28.28s %8d %8.3f %6.2f  %s"
              % (i, a, b, fr, float(sec), score, method))
    unmatched = sum(1 for r in report if r[2] == 0)
    print()
    print("Clips: %d   overlaps found: %d   no overlap: %d"
          % (len(placed), len(report) - unmatched, unmatched))
    print("Timeline length: %.2fs -> %.2fs"
          % (float(sum(it.duration for it in items)), float(total)))
    if dropped:
        print("Dropped from main storyline: %s" % ", ".join(dropped))
    print("Wrote %s" % out)
    print("Import it in Final Cut Pro with File > Import > XML...")


if __name__ == "__main__":
    main()
