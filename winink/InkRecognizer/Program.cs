using System.Text.Json;
using System.Text.Json.Serialization;
using InkRecognizer;

// InkRecognizer CLI
// -----------------
// Usage:   InkRecognizer <strokes.json.gz>
// Output:  single JSON object on stdout:
//   {
//     "status": "Updated",
//     "page_text": "line one\nline two\n\nsecond region",
//     "regions": [ { "kind","text","bbox": {"minX","minY","maxX","maxY"} }, ... ],
//     "word_count": 10,
//     "stroke_count": 78,
//     "point_count": 684
//   }
// On failure: {"error":"...message..."} on stdout and a non-zero exit code.
//
// Diagnostics (if any) go to stderr so stdout stays pure JSON.

var jsonOpts = new JsonSerializerOptions
{
    PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    DefaultIgnoreCondition = JsonIgnoreCondition.Never,
    WriteIndented = false,
};

if (args.Length != 1)
{
    EmitError("usage: InkRecognizer <strokes.json.gz>", jsonOpts);
    return 2;
}

string path = args[0];
if (!File.Exists(path))
{
    EmitError($"file not found: {path}", jsonOpts);
    return 2;
}

// WinRT ink analysis must run from an STA apartment. Do all work there and
// bridge the result back out.
RecognitionResult? recognition = null;
Exception? failure = null;

var staThread = new Thread(() =>
{
    try
    {
        ParsedPage page = StrokeReader.ReadFile(path);
        recognition = InkRecognizerCore.RecognizeAsync(page).GetAwaiter().GetResult();
    }
    catch (Exception ex)
    {
        failure = ex;
    }
});
staThread.SetApartmentState(ApartmentState.STA);
staThread.Start();
staThread.Join();

if (failure is not null)
{
    EmitError($"{failure.GetType().Name}: {failure.Message}", jsonOpts);
    return 1;
}

Console.Out.Write(JsonSerializer.Serialize(recognition, jsonOpts));
Console.Out.Flush();
return 0;


static void EmitError(string message, JsonSerializerOptions opts)
{
    Console.Out.Write(JsonSerializer.Serialize(new ErrorPayload { Error = message }, opts));
    Console.Out.Flush();
}

sealed class ErrorPayload
{
    public string Error { get; init; } = "";
}
