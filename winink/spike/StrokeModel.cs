using System.IO.Compression;
using System.Text.Json;

namespace InkSpike;

/// <summary>
/// A single parsed point in logical canvas coordinates.
/// Pressure defaults to 0.5 when absent (Windows Ink keys off geometry, not pressure).
/// </summary>
public readonly record struct StrokePoint(double X, double Y, double Pressure);

/// <summary>A single parsed stroke: an ordered list of points plus some metadata.</summary>
public sealed class ParsedStroke
{
    public string Id { get; init; } = "";
    public string Color { get; init; } = "";
    public double Width { get; init; }
    public List<StrokePoint> Points { get; init; } = new();
}

/// <summary>Everything parsed out of one strokes.json(.gz) file.</summary>
public sealed class ParsedPage
{
    public int Version { get; init; }
    public List<ParsedStroke> Strokes { get; init; } = new();

    public int PointCount => Strokes.Sum(s => s.Points.Count);
}

/// <summary>
/// Reads the notes-app v3 stroke format from a gzip-compressed (or plain) JSON file.
///
/// Format: {"v":3,"strokes":[{"id","color","width","points":[ ... ]}]}
/// Points may be either:
///   - list form  [x, y, pressure]   (what the app actually stores after RDP simplify)
///   - dict form  {"x","y","pressure"|"p","t"}
/// Both are handled defensively (mirrors the Python _decode_strokes_for_ocr logic).
/// </summary>
public static class StrokeReader
{
    public static ParsedPage ReadFile(string path)
    {
        string json = ReadRawJson(path);
        return Parse(json);
    }

    private static string ReadRawJson(string path)
    {
        // Detect gzip by magic bytes (0x1f 0x8b) so we tolerate a plain .json too.
        using var fs = File.OpenRead(path);
        int b0 = fs.ReadByte();
        int b1 = fs.ReadByte();
        fs.Seek(0, SeekOrigin.Begin);

        if (b0 == 0x1f && b1 == 0x8b)
        {
            using var gz = new GZipStream(fs, CompressionMode.Decompress);
            using var sr = new StreamReader(gz);
            return sr.ReadToEnd();
        }

        using var plain = new StreamReader(fs);
        return plain.ReadToEnd();
    }

    private static ParsedPage Parse(string json)
    {
        using var doc = JsonDocument.Parse(json);
        JsonElement root = doc.RootElement;

        int version = root.TryGetProperty("v", out var vEl) && vEl.TryGetInt32(out var v) ? v : 0;

        var strokes = new List<ParsedStroke>();
        if (root.TryGetProperty("strokes", out var strokesEl) &&
            strokesEl.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement s in strokesEl.EnumerateArray())
            {
                strokes.Add(ParseStroke(s));
            }
        }

        return new ParsedPage { Version = version, Strokes = strokes };
    }

    private static ParsedStroke ParseStroke(JsonElement s)
    {
        string id = s.TryGetProperty("id", out var idEl) && idEl.ValueKind == JsonValueKind.String
            ? idEl.GetString() ?? "" : "";
        string color = s.TryGetProperty("color", out var cEl) && cEl.ValueKind == JsonValueKind.String
            ? cEl.GetString() ?? "" : "";
        double width = s.TryGetProperty("width", out var wEl) && wEl.ValueKind == JsonValueKind.Number
            ? wEl.GetDouble() : 1.0;

        var points = new List<StrokePoint>();
        if (s.TryGetProperty("points", out var ptsEl) && ptsEl.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement p in ptsEl.EnumerateArray())
            {
                var pt = ParsePoint(p);
                if (pt is not null) points.Add(pt.Value);
            }
        }

        return new ParsedStroke { Id = id, Color = color, Width = width, Points = points };
    }

    private static StrokePoint? ParsePoint(JsonElement p)
    {
        // List form: [x, y, (pressure)]
        if (p.ValueKind == JsonValueKind.Array)
        {
            var arr = p.EnumerateArray().ToArray();
            if (arr.Length < 2) return null;
            double x = arr[0].GetDouble();
            double y = arr[1].GetDouble();
            double pressure = arr.Length > 2 && arr[2].ValueKind == JsonValueKind.Number
                ? arr[2].GetDouble() : 0.5;
            return new StrokePoint(x, y, pressure);
        }

        // Dict form: {"x","y","pressure"|"p","t"}
        if (p.ValueKind == JsonValueKind.Object)
        {
            double x = p.TryGetProperty("x", out var xEl) && xEl.ValueKind == JsonValueKind.Number
                ? xEl.GetDouble() : 0.0;
            double y = p.TryGetProperty("y", out var yEl) && yEl.ValueKind == JsonValueKind.Number
                ? yEl.GetDouble() : 0.0;
            double pressure = 0.5;
            if (p.TryGetProperty("pressure", out var prEl) && prEl.ValueKind == JsonValueKind.Number)
                pressure = prEl.GetDouble();
            else if (p.TryGetProperty("p", out var pEl2) && pEl2.ValueKind == JsonValueKind.Number)
                pressure = pEl2.GetDouble();
            return new StrokePoint(x, y, pressure);
        }

        return null;
    }
}
