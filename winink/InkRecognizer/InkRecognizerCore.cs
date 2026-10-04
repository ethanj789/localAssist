using Windows.Foundation;
using Windows.UI.Input.Inking;
using Windows.UI.Input.Inking.Analysis;

namespace InkRecognizer;

/// <summary>Axis-aligned bounding box in logical canvas coordinates.</summary>
public sealed class BBox
{
    public double MinX { get; init; }
    public double MinY { get; init; }
    public double MaxX { get; init; }
    public double MaxY { get; init; }

    public static BBox FromRect(Rect r) => new()
    {
        MinX = r.X,
        MinY = r.Y,
        MaxX = r.X + r.Width,
        MaxY = r.Y + r.Height,
    };
}

/// <summary>One recognized spatial region (WritingRegion / Paragraph / Line fallback).</summary>
public sealed class RecognizedRegion
{
    public string Kind { get; init; } = "";
    public string Text { get; init; } = "";
    public BBox Bbox { get; init; } = new();
}

/// <summary>Full recognition result for one page — serialized to stdout as JSON.</summary>
public sealed class RecognitionResult
{
    public string Status { get; init; } = "";
    public string PageText { get; init; } = "";
    public List<RecognizedRegion> Regions { get; init; } = new();
    public int WordCount { get; init; }
    public int StrokeCount { get; init; }
    public int PointCount { get; init; }
}

/// <summary>
/// Runs native Windows Ink handwriting recognition on parsed strokes.
///
/// Headless (no InkCanvas / XAML). Must be driven from an STA thread — see Program.cs.
/// Flow: StrokePoint -> InkPoint -> InkStrokeBuilder.CreateStrokeFromInkPoints
///       -> InkAnalyzer.AddDataForStrokes -> AnalyzeAsync -> traverse AnalysisRoot.
/// </summary>
public static class InkRecognizerCore
{
    public static async Task<RecognitionResult> RecognizeAsync(ParsedPage page)
    {
        var builder = new InkStrokeBuilder();
        var strokes = new List<InkStroke>();

        foreach (ParsedStroke s in page.Strokes)
        {
            if (s.Points.Count == 0) continue;

            var inkPoints = new List<InkPoint>(s.Points.Count);
            foreach (StrokePoint p in s.Points)
            {
                float pressure = (float)Math.Clamp(p.Pressure <= 0 ? 0.5 : p.Pressure, 0.05, 1.0);
                inkPoints.Add(new InkPoint(new Point(p.X, p.Y), pressure));
            }

            // A single-point stroke can't form a polyline; nudge a duplicate so it survives.
            if (inkPoints.Count == 1)
            {
                InkPoint only = inkPoints[0];
                inkPoints.Add(new InkPoint(new Point(only.Position.X + 1.0, only.Position.Y), only.Pressure));
            }

            InkStroke stroke = builder.CreateStrokeFromInkPoints(inkPoints, System.Numerics.Matrix3x2.Identity);
            strokes.Add(stroke);
        }

        if (strokes.Count == 0)
        {
            return new RecognitionResult
            {
                Status = "NoStrokes",
                StrokeCount = 0,
                PointCount = page.PointCount,
            };
        }

        var analyzer = new InkAnalyzer();
        analyzer.AddDataForStrokes(strokes);

        InkAnalysisResult result = await analyzer.AnalyzeAsync();

        var regions = new List<RecognizedRegion>();

        // Prefer WritingRegion (OneNote-style bubbles). Fall back to Paragraph, then Line.
        IReadOnlyList<IInkAnalysisNode> regionNodes =
            analyzer.AnalysisRoot.FindNodes(InkAnalysisNodeKind.WritingRegion);
        string usedKind = "WritingRegion";

        if (regionNodes.Count == 0)
        {
            regionNodes = analyzer.AnalysisRoot.FindNodes(InkAnalysisNodeKind.Paragraph);
            usedKind = "Paragraph";
        }
        if (regionNodes.Count == 0)
        {
            regionNodes = analyzer.AnalysisRoot.FindNodes(InkAnalysisNodeKind.Line);
            usedKind = "Line";
        }

        foreach (IInkAnalysisNode node in regionNodes)
        {
            string text = ExtractText(node);
            if (string.IsNullOrWhiteSpace(text)) continue;  // drop blank regions (drawings)
            regions.Add(new RecognizedRegion
            {
                Kind = usedKind,
                Text = text,
                Bbox = BBox.FromRect(node.BoundingRect),
            });
        }

        // Order regions top-to-bottom, then left-to-right for readable page text.
        regions.Sort((a, b) =>
        {
            int cy = a.Bbox.MinY.CompareTo(b.Bbox.MinY);
            return cy != 0 ? cy : a.Bbox.MinX.CompareTo(b.Bbox.MinX);
        });

        string pageText = string.Join("\n\n", regions.Select(r => r.Text));

        int wordCount = analyzer.AnalysisRoot.FindNodes(InkAnalysisNodeKind.InkWord).Count;

        analyzer.ClearDataForAllStrokes();

        return new RecognitionResult
        {
            Status = result.Status.ToString(),
            PageText = pageText,
            Regions = regions,
            WordCount = wordCount,
            StrokeCount = strokes.Count,
            PointCount = page.PointCount,
        };
    }

    /// <summary>
    /// RecognizedText lives on the concrete node types, not the IInkAnalysisNode
    /// interface. Pull it from whichever concrete type we were handed.
    /// </summary>
    private static string ExtractText(IInkAnalysisNode node) => node switch
    {
        InkAnalysisWritingRegion w => w.RecognizedText ?? "",
        InkAnalysisParagraph p     => p.RecognizedText ?? "",
        InkAnalysisLine l          => l.RecognizedText ?? "",
        InkAnalysisInkWord word    => word.RecognizedText ?? "",
        _ => "",
    };
}
