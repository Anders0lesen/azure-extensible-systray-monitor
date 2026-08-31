using System;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace AzureHealthBeacon;

public sealed class CoreClient : IDisposable
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Process? _process;
    private int _nextId;

    public async Task StartAsync()
    {
        var overridePath = Environment.GetEnvironmentVariable("AZURE_HEALTH_BEACON_CORE");
        var executable = string.IsNullOrWhiteSpace(overridePath)
            ? Path.Combine(AppContext.BaseDirectory, "AzureHealthBeaconCore.exe")
            : overridePath;
        if (!File.Exists(executable))
            throw new FileNotFoundException("The monitoring engine is missing. Reinstall Azure Health Beacon.", executable);

        var start = new ProcessStartInfo(executable)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = AppContext.BaseDirectory,
        };
        // The shell can be restarted by an installer that was launched from an
        // older PyInstaller onefile process. Never let that process's deleted
        // _MEI runtime leak into the new private engine.
        start.Environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1";
        _process = Process.Start(start) ?? throw new InvalidOperationException("The monitoring engine did not start.");
        _process.ErrorDataReceived += (_, _) => { };
        _process.BeginErrorReadLine();
        var pong = await CallAsync("ping");
        if (pong["version"] is null)
            throw new InvalidOperationException("The monitoring engine returned an invalid startup response.");
    }

    public async Task<JsonObject> CallAsync(string command, JsonObject? payload = null)
    {
        await _gate.WaitAsync();
        try
        {
            if (_process is null || _process.HasExited)
                throw new InvalidOperationException("The private monitoring engine is not running.");
            var id = Interlocked.Increment(ref _nextId);
            var request = new JsonObject
            {
                ["id"] = id,
                ["command"] = command,
                ["payload"] = payload ?? new JsonObject(),
            };
            await _process.StandardInput.WriteLineAsync(request.ToJsonString());
            await _process.StandardInput.FlushAsync();
            var line = await _process.StandardOutput.ReadLineAsync();
            if (line is null)
                throw new InvalidOperationException("The monitoring engine stopped unexpectedly.");
            var response = JsonNode.Parse(line)?.AsObject()
                ?? throw new InvalidOperationException("The monitoring engine returned invalid data.");
            if (response["id"]?.GetValue<int>() != id)
                throw new InvalidOperationException("The monitoring engine response was out of sequence.");
            if (response["ok"]?.GetValue<bool>() != true)
                throw new InvalidOperationException(response["error"]?.GetValue<string>() ?? "The operation failed.");
            return response["result"]?.AsObject() ?? new JsonObject();
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task ShutdownAsync()
    {
        if (_process is null || _process.HasExited) return;
        try { await CallAsync("shutdown"); }
        catch { /* Disposal must still complete. */ }
        if (!_process.WaitForExit(2000)) _process.Kill(true);
    }

    public void Dispose()
    {
        if (_process is { HasExited: false })
        {
            try { _process.Kill(true); } catch { }
        }
        _process?.Dispose();
        _gate.Dispose();
    }
}
