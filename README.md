# fcp-squish

Takes a video file and a Final Cut Pro project made of cuts from that video,
and writes a **new** project where each clip overlaps the one before it.
The overlap is picked by matching audio, so the start of each clip's audio
lines up with the matching audio at the end of the clip before it, and the
sound flows across the cut with no seam. The video overlaps too.

## Requirements

- Python 3.8+
- numpy: `pip3 install numpy`
- ffmpeg: `brew install ffmpeg`

## Usage

1. In Final Cut Pro, select the project in the browser and choose
   **File > Export XML...**. This saves a `.fcpxml` (or `.fcpxmld`) file.
2. Run:

   ```sh
   python3 fcp_squish.py "/path/to/video.mov" "/path/to/My Project.fcpxml"
   ```

   This writes `My Project squished.fcpxml` next to the input file (choose
   another name with `-o out.fcpxml`) and prints a table showing how much
   each pair of clips was overlapped.
3. In Final Cut Pro choose **File > Import > XML...** and pick the new file.
   It comes in as a project named `<original name> (squished)`.

## What it does

For each pair of clips next to each other on the timeline:

- **Same source material**: if the next clip starts at a point in the video
  that the previous clip already covers, the overlap is exactly that shared
  part.
- **Otherwise it compares audio**: it tries every overlap from
  `--min-overlap` frames up to `--max-overlap` seconds and scores how closely
  the last N frames of the outgoing clip match the first N frames of the
  incoming clip (1.0 = identical audio). The best score is used if it beats
  `--threshold`. If nothing matches, the clips are left butted together, or
  overlapped by `--fallback-frames` if you set it.

Overlaps are whole frames so Final Cut accepts the edit points.

Clips in Final Cut's main storyline can't overlap, so the new project puts
one gap in the main storyline and every clip on a connected lane above it,
alternating between lanes 1 and 2. A linear audio crossfade covers each
overlap so the matching audio hands off cleanly. Titles and other clips that
were connected to a clip move with it, on lanes above the clips.

## Options

| option | default | meaning |
| --- | --- | --- |
| `-o, --output` | `<project> squished.fcpxml` | output file |
| `--max-overlap` | `3.0` | longest overlap to search for, in seconds |
| `--min-overlap` | `2` | shortest overlap to accept, in frames |
| `--threshold` | `0.7` | lowest audio match score (0-1) to accept |
| `--fallback-frames` | `0` | overlap to use when no match is found |
| `--lanes` | `alternate` | `alternate` puts clips on lanes 1/2; `stack` puts each clip one lane higher so the incoming clip's video is always on top |
| `--no-fades` | off | don't add audio crossfades |
| `--fade-type` | `linear` | `linear`, `easeIn`, `smooth` or `easeOut` |
| `--project-name` | first project | which project to use if the XML holds several |

## Limitations

- Audio matching works when the overlapping audio is actually the same
  recording (the same moment in the video, or a section that repeats). Two
  separate takes of the same line won't match closely enough. Lower
  `--threshold` or use `--fallback-frames` for those.
- With `--lanes alternate`, the video on top during an overlap switches back
  and forth between the incoming and outgoing clip. Use `--lanes stack` if
  the incoming clip should always be on top.
- Transitions in the main storyline are dropped (the overlaps replace them).
  Retimed clips and compound or multicam clips stay in order but don't get
  an overlap.

## Testing

`tests/make_test_media.py OUTDIR` builds a synthetic video and project with
known answers:

```sh
python3 tests/make_test_media.py /tmp/squish-test
python3 fcp_squish.py /tmp/squish-test/source.mov /tmp/squish-test/Cuts.fcpxml
```
