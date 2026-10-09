# fcp-squish

For a voiceover or to-camera read that's already been cut down to tight
clips (one line per clip, silences and flubs removed): takes the video file
and the Final Cut Pro project, and writes a **new** project where each clip
slightly overlaps the one before it, by 2 frames by default.

- Clips alternate between lanes 1 and 2: every other clip sits **on top of**
  the clip before it, and the next one tucks **underneath**.
- Every clip is its own connected clip, so you can fine-tune any overlap by
  dragging that clip.
- The audio is checked at every cut. If the silence between two lines is
  shorter than the overlap, the overlap is shortened so the next line doesn't
  start before the previous one finishes.

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

## What it prints

A table with one row per cut:

```
#    outgoing clip   incoming clip   frames  seconds    room
1    Line 1          Line 2               2    0.080   0.245
2    Line 2          Line 3               1    0.040   0.055  shortened so lines don't collide
3    Line 3          Line 4               1    0.040   0.000  lines touch at 1 frame
```

**room** is the silence between the end of one line and the start of the
next. You can drag a clip earlier by up to that much before the lines run
into each other.

## Options

| option | default | meaning |
| --- | --- | --- |
| `--overlap` | `2` | overlap at each cut, in frames (`3`) or seconds (`0.1s`) |
| `--min-overlap` | `1` | never overlap less than this many frames, even where lines touch |
| `--ignore-audio` | off | use exactly `--overlap` everywhere, without checking the audio |
| `--quiet-db` | `25` | how far below your speaking level counts as silence |
| `--lanes` | `alternate` | `alternate`: on top / underneath, on lanes 1 and 2. `stack`: each clip one lane higher, so the incoming clip is always on top |
| `--no-fades` | off | don't add a short audio crossfade over each overlap |
| `--fade-type` | `linear` | `linear`, `easeIn`, `smooth` or `easeOut` |
| `-o, --output` | `<project> squished.fcpxml` | output file |
| `--project-name` | first project | which project to use if the XML holds several |

## Notes

- Dragging a clip moves just that clip; the clips after it stay put.
- Transitions in the main storyline are dropped. Titles and other clips
  connected to a clip move with it, on a lane above the clips.
- Retimed, compound and multicam clips get the overlap you asked for, but
  their audio isn't checked.

## Testing

`tests/make_test_media.py OUTDIR` builds a synthetic video and project with
known answers:

```sh
python3 tests/make_test_media.py /tmp/squish-test
python3 fcp_squish.py /tmp/squish-test/source.mov /tmp/squish-test/Cuts.fcpxml
```
