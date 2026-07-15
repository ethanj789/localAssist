import gzip, json, sys
sys.path.insert(0, ".")
from pathlib import Path

data_root = Path("tool_apps/notes/data")
for t in ("notes", "art"):
    base = data_root / t
    if not base.exists():
        continue
    for p in sorted(base.iterdir()):
        gz = p / "strokes.json.gz"
        if not gz.exists():
            continue
        with gzip.open(gz, "rt") as f:
            d = json.load(f)
        strokes = d.get("strokes", [])
        if strokes:
            colors = set(s.get("color", "NONE") for s in strokes[:10])
            print(f"{p.name}: {colors}")
