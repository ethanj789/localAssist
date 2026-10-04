using System.Globalization;
using InkSpike;

// Spike: parse each gz stroke file, run native Windows Ink recognition,
// and print the recognized page text + per-region (WritingRegion) breakdown.

if (args.Length == 0)
{
    Console.Error.WriteLine("usage: InkSpike <strokes.json.gz> [more.json.gz ...]");
    return 1;
}

// WinRT ink analysis is safest from an STA apartment. Run everything there and
// bridge the async recognition back synchronously.
int exitCode = 0;
var staThread = new Thread(() =>
{
    exitCode = RunAll(args);
});
staThread.SetApartmentState(ApartmentState.STA);
staThread.Start();
staThread.Join();
return exitCode;


static int RunAll(string[] paths)
{
    int exit = 0;
    foreach (string path in paths)
    {
        Console.WriteLine(new string('=', 70));
        Console.WriteLine($"FILE: {path}");

        if (!File.Exists(path))
        {
            Console.Error.WriteLine("  (not found)");
            exit = 1;
            continue;
        }

        try
        {
            ParsedPage page = StrokeReader.ReadFile(path);
            Console.WriteLine($"  parsed: version={page.Version}  strokes={page.Strokes.Count}  points={page.PointCount}");

            RecognitionResult res = InkRecognizer.RecognizeAsync(page).GetAwaiter().GetResult();

            Console.WriteLine($"  status={res.Status}  regions={res.Regions.Count}  words={res.WordCount}");
            Console.WriteLine();

            if (res.Regions.Count == 0 || string.IsNullOrWhiteSpace(res.PageText))
            {
                Console.WriteLine("  (no text recognized)");
                Console.WriteLine();
                continue;
            }

            Console.WriteLine("  ----- PAGE TEXT -----");
            foreach (string line in res.PageText.Split('\n'))
                Console.WriteLine($"  {line}");
            Console.WriteLine();

            Console.WriteLine("  ----- REGIONS -----");
            int i = 0;
            foreach (RecognizedRegion r in res.Regions)
            {
                string rect = string.Format(
                    CultureInfo.InvariantCulture,
                    "[x={0:0},y={1:0},w={2:0},h={3:0}]",
                    r.BoundingRect.X, r.BoundingRect.Y, r.BoundingRect.Width, r.BoundingRect.Height);
                string flat = r.Text.Replace("\n", " / ");
                Console.WriteLine($"  #{i} {r.Kind} {rect}: {flat}");
                i++;
            }
            Console.WriteLine();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"  FAILED: {ex.GetType().Name}: {ex.Message}");
            exit = 1;
        }
    }
    return exit;
}
