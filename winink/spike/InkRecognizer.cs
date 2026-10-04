using Windows.Foundation;
using Windows.UI.Input.Inking;
using Windows.UI.Input.Inking.Analysis;

namespace InkSpike;

/// <summary>One recognized spatial region (WritingRegion / Paragraph / Line fallback).</summary>
public sealed class RecognizedRegion
{
    public string Kind { get; init; } = "";
    public string Text { get; init; } = "";
    public Rect BoundingRect { get; init; }
}

/// <summary>Full recognition result for one page.</summary>
public sealed class RecognitionResult
{
    public string Status { get; init; } = "";
    public string PageText { get; init; } = "";
    public List<RecognizedRegion> Regions { get; init; } = new();
    public int WordCount { get; init; }
}

/// <summary>
/// Runs native Windows Ink handwriting recognition on parsed strokes.
///
/// Flow: StrokePoint -> InkPoint -> InkStrokeBuilder.CreateStrokeFromInkPoints
///       -> InkAnalyzer.AddDataForStrokes -> AnalyzeAsync
///       -> traverse AnalysisRoot by node kind.
///
/// Works headless (no InkCanvas / XAML). No UI thread needed, but we run it
/// from an STA thread in Program.cs to stay safe with WinRT apartment rules.
/// </summary>
public static class InkRecognizer
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
                // InkPoint(position, pressure). Pressure clamped to a sane (0,1] range.
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
            return new RecognitionResult { Status = "NoStrokes" };
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
            regions.Add(new RecognizedRegion
            {
                Kind = usedKind,
                Text = ExtractText(node),
                BoundingRect = node.BoundingRect,
            });
        }

        // Order regions top-to-bottom, then left-to-right for readable page text.
        regions.Sort((a, b) =>
        {
            int cy = a.BoundingRect.Y.CompareTo(b.BoundingRect.Y);
            return cy != 0 ? cy : a.BoundingRect.X.CompareTo(b.BoundingRect.X);
        });

        string pageText = string.Join(
            "\n\n",
            regions.Select(r => r.Text).Where(t => !string.IsNullOrWhiteSpace(t)));

        int wordCount = analyzer.AnalysisRoot.FindNodes(InkAnalysisNodeKind.InkWord).Count;

        analyzer.ClearDataForAllStrokes();

        return new RecognitionResult
        {
            Status = result.Status.ToString(),
            PageText = pageText,
            Regions = regions,
            WordCount = wordCount,
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
