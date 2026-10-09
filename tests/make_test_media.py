#!/usr/bin/env python3
"""Build a synthetic video + FCPXML to exercise fcp_squish.py.

The audio is silence with "lines" of noise in it. Each clip is one line cut
tight, with a known amount of silence left at its edges. At 25 fps (40 ms
frames) and the default 2-frame overlap, the expected result is:
  A -> B : room 0.250s -> 2 frames
  B -> C : room 0.060s -> 1 frame  (shortened so lines don't collide)
  C -> D : room 0.000s -> 1 frame  (lines touch, minimum overlap)
  D -> E : room 0.100s -> 2 frames
"""
import os, subprocess, sys, wave
import numpy as np

out = sys.argv[1] if len(sys.argv) > 1 else "test_out"
os.makedirs(out, exist_ok=True)
sr = 48000
rng = np.random.default_rng(1)
a = np.zeros(30 * sr)
# (clip name, clip in, clip out, speech start, speech end) in source seconds
clips = [("A", 1.00, 3.00, 1.00, 2.85),
         ("B", 5.00, 7.00, 5.10, 6.98),
         ("C", 9.00, 11.00, 9.04, 11.00),
         ("D", 13.00, 15.00, 13.00, 14.92),
         ("E", 17.00, 19.00, 17.02, 19.00)]
for _, _, _, s0, s1 in clips:
    a[int(s0 * sr):int(s1 * sr)] = rng.standard_normal(int(s1 * sr) - int(s0 * sr)) * 6000
a += rng.standard_normal(a.size) * 20  # room tone
a = a.clip(-32767, 32767).astype(np.int16)
wav = os.path.join(out, "audio.wav")
with wave.open(wav, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(a.tobytes())
video = os.path.join(out, "source.mov")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "testsrc=size=320x240:rate=25:duration=30", "-i", wav,
                "-c:v", "libx264", "-c:a", "pcm_s16le", "-shortest", video],
               check=True)

TC = 3600  # asset timecode start, to check offset handling
spine, off = [], 0
for name, i, o, _, _ in clips:
    spine.append('<asset-clip ref="r2" name="%s" offset="%ds" start="%ds" duration="%ds" format="r1" tcFormat="NDF">'
                 '<title ref="r3" lane="1" offset="%ds" duration="1s" name="label %s"/></asset-clip>'
                 % (name, off, TC + i, o - i, TC + i, name))
    off += int(o - i)
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
          <spine>%s</spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
''' % (TC, os.path.abspath(video), off, "".join(spine))
open(os.path.join(out, "Cuts.fcpxml"), "w").write(xml)
print("wrote", video, os.path.join(out, "Cuts.fcpxml"))
