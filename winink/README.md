# winink — Windows Ink handwriting recognition

A small C# CLI tool that runs **native Windows Ink handwriting recognition**
(`Windows.UI.Input.Inking.Analysis.InkAnalyzer`) on the notes app's stroke
files and prints recognized text as JSON. It replaces the old image-OCR
pipeline (TrOCR / Moondream), which has been retired to
`tool_apps/notes/ocr/`.

Recognition is fully local via the OS — no model download, no Ollama, no image
rendering step.

## Layout

```
winink/
  InkRecognizer/      C# CLI project (the real tool)
    InkRecognizer.csproj
    Program.cs          CLI entry — STA thread, JSON stdout
    StrokeModel.cs      reads v3 strokes.json.gz (list- and dict-form points)
    InkRecognizerCore.cs  InkAnalyzer recognition -> regions
  spike/              throwaway accuracy spike + FINDINGS.md (kept for reference)
  build.ps1           publishes the CLI to winink/bin/InkRecognizer.exe
  bin/                published exe (gitignored)
```

## Requirements

- Windows 10 1809+ (the ink analysis API floor).
- .NET 8 SDK or newer. The project targets **`net8.0-windows10.0.19041.0`** —
  the Windows TFM is required for the `Windows.UI.Input.Inking` WinRT
  projections. (Built and tested with .NET 9 SDK.)
- Framework-dependent build: needs the .NET runtime present at runtime
  (it is on the dev machine). Switch to `--self-contained` in `build.ps1`
  if deploying somewhere without the runtime.

## Build

```powershell
pwsh winink/build.ps1                        # Release -> winink/bin/InkRecognizer.exe
pwsh winink/build.ps1 -Configuration Debug
```

Re-run after changing any C# source. The Python wrapper
(`tool_apps/notes/winink_recognizer.py`) expects the exe at
`winink/bin/InkRecognizer.exe`.

## Usage

```
InkRecognizer <strokes.json.gz>
```

Takes a single path to a gzip-compressed (or plain) v3 stroke JSON file and
writes one JSON object to **stdout**. Diagnostics go to stderr so stdout stays
pure JSON.

### Success output

```json
{
  "status": "Updated",
  "page_text": "line one\nline two\n\nsecond region",
  "regions": [
    {
      "kind": "WritingRegion",
      "text": "line one\nline two",
      "bbox": { "min_x": 320.0, "min_y": 134.0, "max_x": 1316.0, "max_y": 671.0 }
    }
  ],
  "word_count": 10,
  "stroke_count": 78,
  "point_count": 684
}
```

- `page_text` joins regions with a blank line (`\n\n`); lines within a region
  are separated by `\r\n`.
- `regions` are the OneNote-style writing-region "bubbles", ordered
  top-to-bottom then left-to-right. Blank-text regions (pure drawings) are
  dropped.
- `status` is the `InkAnalysisStatus` string (`Updated` on success).

### Failure output

```json
{ "error": "file not found: ..." }
```

Exit codes: `0` success, `2` usage / file-not-found, `1` recognition
exception. On any non-zero exit the Python wrapper logs and skips the page
without raising.

## Stroke format

v3 compact: `{"v":3,"strokes":[{"id","color","width","points":[ ... ]}]}`.
Points are read in either form:

- list `[x, y, pressure]` (what the app stores after RDP simplification)
- dict `{"x","y","pressure"|"p","t"}`

Pressure is optional (defaults to 0.5); Windows Ink keys off geometry.
Coordinates are logical canvas pixels and may be negative (panned canvas).

## Notes / known behavior

- Writing-region grouping is spatial. Clearly separated clusters become
  separate regions; tightly aligned, evenly-spaced columns may be read as one
  region interleaved line-by-line. See `spike/FINDINGS.md` for details.
- Pure doodle pages recognize as zero regions and produce empty `page_text`.
