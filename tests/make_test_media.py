#!/usr/bin/env python3
"""Build a synthetic video + FCPXML to exercise fcp_squish.py.

Audio is random noise, except that source seconds 15-16 are copied to 22-23,
so clip C (12-16s) ends with exactly the audio clip D (22-26s) starts with.
Expected overlaps (25 fps):
  A(0-5)  -> B(4-7)   : 25 frames (same source material)
  B(4-7)  -> C(12-16) : 0 (no match)
  C(12-16)-> D(22-26) : 25 frames (audio match)
  D(22-26)-> E(27-29) : 0 (no match)
"""
import os, subprocess, sys, wave
import numpy as np

out = sys.argv[1] if len(sys.argv) > 1 else "test_out"
os.makedirs(out, exist_ok=True)
sr = 48000
rng = np.random.default_rng(1)
a = (rng.standard_normal(30 * sr) * 4000).astype(np.int16)
a[22 * sr:23 * sr] = a[15 * sr:16 * sr]
wav = os.path.join(out, "audio.wav")
with wave.open(wav, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(a.tobytes())
video = os.path.join(out, "source.mov")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "testsrc=size=320x240:rate=25:duration=30", "-i", wav,
                "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k", "-shortest", video],
               check=True)

TC = 3600  # asset timecode start, to check offset handling
clips = [("A", 0, 5), ("B", 4, 7), ("C", 12, 16), ("D", 22, 26), ("E", 27, 29)]
spine, off = [], 0
for name, i, o in clips:
    spine.append('<asset-clip ref="r2" name="%s" offset="%ds" start="%ds" duration="%ds" format="r1" tcFormat="NDF">'
                 '<title ref="r3" lane="1" offset="%ds" duration="1s" name="label %s"/></asset-clip>'
                 % (name, off, TC + i, o - i, TC + i, name))
    off += o - i
xml = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
  <resources>
    <format id="r1" name="FFVideoFormat1080p25" frameDuration="1/25s" width="1920" height="1080"/>
    <asset id="r2" name="source" start="%ds" duration="30s" hasVideo="1" hasAudio="1" format="r1" audioSources="1" audioChannels="1" audioRate="48000">
      <media-rep kind="original-media" src="file://%s"/>
    </asset>
    <effect id="r3" name="Basic Title" uid=".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"/>
  </resources>
  <library>
    <event name="Test">
      <project name="Cuts" uid="ABC">
        <sequence format="r1" duration="%ds" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>%s<transition name="Cross Dissolve" offset="4s" duration="1s"/></spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
''' % (TC, os.path.abspath(video), off, "".join(spine))
open(os.path.join(out, "Cuts.fcpxml"), "w").write(xml)
print("wrote", video, os.path.join(out, "Cuts.fcpxml"))
