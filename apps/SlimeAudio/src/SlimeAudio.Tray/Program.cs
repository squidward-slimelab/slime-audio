using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Diagnostics;
using NAudio.CoreAudioApi;
using SlimeAudio.Protocol;

namespace SlimeAudio.Tray;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        var port = TryParsePort(args) ?? 47777;
        var multicast = MulticastOptions.Parse(args);
        using var multicastReceiver = new MulticastReceiver(multicast);
        using var receiver = new AudioReceiver(port, multicastReceiver);
        Application.Run(new TrayContext(receiver, multicastReceiver));
    }

    private static int? TryParsePort(string[] args)
    {
        for (var i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == "--port" && int.TryParse(args[i + 1], out var port))
            {
                return port;
            }
        }
        return null;
    }
}

internal sealed record MulticastOptions(string Group, int Port, int SnapcastPort, int SnapcastControlPort)
{
    public static MulticastOptions Parse(string[] args)
    {
        var group = "239.77.77.77";
        var port = 47778;
        var snapcastPort = 1704;
        var snapcastControlPort = 1705;
        for (var i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == "--multicast-group")
            {
                group = args[i + 1];
            }
            else if (args[i] == "--multicast-port" && int.TryParse(args[i + 1], out var parsedPort))
            {
                port = parsedPort;
            }
            else if (args[i] == "--snapcast-port" && int.TryParse(args[i + 1], out var parsedSnapcastPort))
            {
                snapcastPort = parsedSnapcastPort;
            }
            else if (args[i] == "--snapcast-control-port" && int.TryParse(args[i + 1], out var parsedSnapcastControlPort))
            {
                snapcastControlPort = parsedSnapcastControlPort;
            }
        }
        return new MulticastOptions(group, port, snapcastPort, snapcastControlPort);
    }
}

internal sealed class TrayContext : ApplicationContext
{
    private readonly AudioReceiver _receiver;
    private readonly MulticastReceiver _multicast;
    private readonly NotifyIcon _icon;
    private readonly ToolStripMenuItem _muteItem;
    private readonly ToolStripMenuItem _volumeMenu;
    private readonly ToolStripMenuItem _outputDeviceMenu;
    private readonly List<ToolStripMenuItem> _volumeItems = new();
    private bool _updatingMuteMenu;
    private bool _updatingVolumeMenu;
    private bool _updatingOutputDeviceMenu;

    public TrayContext(AudioReceiver receiver, MulticastReceiver multicast)
    {
        _receiver = receiver;
        _multicast = multicast;
        _icon = new NotifyIcon
        {
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath) ?? SystemIcons.Application,
            Text = TrimForTray(DefaultStatus),
            Visible = true,
            ContextMenuStrip = new ContextMenuStrip(),
        };
        _receiver.StatusChanged += (_, message) => _icon.Text = TrimForTray(message);
        _multicast.StatusChanged += (_, message) => _icon.Text = TrimForTray(message);
        _icon.ContextMenuStrip.Opening += (_, _) =>
        {
            UpdateMuteMenu();
            UpdateVolumeMenu();
            UpdateOutputDeviceMenu();
        };
        _icon.ContextMenuStrip.Items.Add($"Slime Audio {VersionInfo.DisplayVersion}", null, (_, _) => MessageBox.Show(DefaultStatus, "Slime Audio"));
        _icon.ContextMenuStrip.Items.Add("Status", null, (_, _) => MessageBox.Show(_icon.Text, "Slime Audio"));
        _muteItem = new ToolStripMenuItem("Receive stream here")
        {
            CheckOnClick = true,
        };
        _muteItem.CheckedChanged += (_, _) => ApplyMuteMenuChange();
        _icon.ContextMenuStrip.Items.Add(_muteItem);
        _volumeMenu = new ToolStripMenuItem("Volume");
        foreach (var percent in new[] { 100, 85, 70, 55, 40, 25, 10 })
        {
            var item = new ToolStripMenuItem($"{percent}%") { CheckOnClick = true, Tag = percent };
            item.CheckedChanged += (_, _) => ApplyVolumeMenuChange(item);
            _volumeItems.Add(item);
            _volumeMenu.DropDownItems.Add(item);
        }
        _icon.ContextMenuStrip.Items.Add(_volumeMenu);
        _outputDeviceMenu = new ToolStripMenuItem("Output device");
        _icon.ContextMenuStrip.Items.Add(_outputDeviceMenu);
        _icon.ContextMenuStrip.Items.Add("Check for updates", null, async (_, _) => await CheckForUpdates());
        _icon.ContextMenuStrip.Items.Add("Quit", null, (_, _) => ExitThread());
        UpdateMuteMenu();
        _receiver.Start();
    }

    private void UpdateOutputDeviceMenu()
    {
        _updatingOutputDeviceMenu = true;
        try
        {
            var selected = _multicast.OutputDevice;
            _outputDeviceMenu.Text = string.IsNullOrWhiteSpace(selected) ? "Output device: Default" : $"Output device: {selected}";
            _outputDeviceMenu.DropDownItems.Clear();

            var defaultItem = new ToolStripMenuItem("System default")
            {
                CheckOnClick = true,
                Checked = string.IsNullOrWhiteSpace(selected),
            };
            defaultItem.CheckedChanged += (_, _) => ApplyOutputDeviceMenuChange(defaultItem, null);
            _outputDeviceMenu.DropDownItems.Add(defaultItem);

            var devices = _multicast.ListOutputDevices(refresh: true);
            if (devices.Count > 0)
            {
                _outputDeviceMenu.DropDownItems.Add(new ToolStripSeparator());
            }
            foreach (var device in devices)
            {
                var item = new ToolStripMenuItem(device.DisplayName)
                {
                    CheckOnClick = true,
                    Checked = string.Equals(selected, device.Soundcard, StringComparison.Ordinal),
                    Tag = device.Soundcard,
                };
                item.CheckedChanged += (_, _) => ApplyOutputDeviceMenuChange(item, device.Soundcard);
                _outputDeviceMenu.DropDownItems.Add(item);
            }

            if (devices.Count == 0)
            {
                var item = new ToolStripMenuItem("No devices reported by snapclient") { Enabled = false };
                _outputDeviceMenu.DropDownItems.Add(item);
            }
        }
        finally
        {
            _updatingOutputDeviceMenu = false;
        }
    }

    private void ApplyOutputDeviceMenuChange(ToolStripMenuItem item, string? soundcard)
    {
        if (_updatingOutputDeviceMenu || !item.Checked)
        {
            return;
        }

        _multicast.SetOutputDevice(soundcard);
        _icon.Text = TrimForTray(string.IsNullOrWhiteSpace(soundcard) ? "Output device set to default" : $"Output device: {soundcard}");
        UpdateOutputDeviceMenu();
    }

    private string DefaultStatus => $"Slime Audio {VersionInfo.DisplayVersion} listening on UDP {_receiver.Port}";

    private void ApplyMuteMenuChange()
    {
        if (_updatingMuteMenu)
        {
            return;
        }

        _receiver.SetStreamMuted(!_muteItem.Checked);
        UpdateMuteMenu();
        _icon.Text = TrimForTray(_receiver.StreamMuted ? "Slime Audio stream muted here" : DefaultStatus);
    }

    private void UpdateMuteMenu()
    {
        _updatingMuteMenu = true;
        try
        {
            _muteItem.Checked = !_receiver.StreamMuted;
            _muteItem.Text = "Receive stream here";
        }
        finally
        {
            _updatingMuteMenu = false;
        }
    }

    private void ApplyVolumeMenuChange(ToolStripMenuItem item)
    {
        if (_updatingVolumeMenu || !item.Checked || item.Tag is not int percent)
        {
            return;
        }

        _ = SetVolumeAsync(percent);
    }

    private async Task SetVolumeAsync(int percent)
    {
        try
        {
            await _multicast.SetVolumeAsync(percent);
            _icon.Text = TrimForTray($"Slime Audio volume {percent}%");
        }
        catch (Exception ex)
        {
            _icon.Text = TrimForTray($"Volume failed: {ex.Message}");
        }
        finally
        {
            UpdateVolumeMenu();
        }
    }

    private void UpdateVolumeMenu()
    {
        _updatingVolumeMenu = true;
        try
        {
            _volumeMenu.Text = $"Volume {_multicast.VolumePercent}%";
            foreach (var item in _volumeItems)
            {
                item.Checked = item.Tag is int percent && percent == _multicast.VolumePercent;
            }
        }
        finally
        {
            _updatingVolumeMenu = false;
        }
    }

    private async Task CheckForUpdates()
    {
        try
        {
            _icon.Text = TrimForTray("Slime Audio checking for updates");
            var message = await UpdateService.DownloadAndRunLatestInstallerAsync();
            _icon.Text = TrimForTray(message);
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, "Slime Audio update failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _icon.Visible = false;
            _icon.Dispose();
            _receiver.Dispose();
            _multicast.Dispose();
        }
        base.Dispose(disposing);
    }

    private static string TrimForTray(string text) => text.Length > 63 ? text[..63] : text;
}

internal sealed class MulticastReceiver : IDisposable
{
    private static readonly TimeSpan ReconnectDelay = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan MaxReconnectDelay = TimeSpan.FromSeconds(60);
    private const int MaxReconnectAttempts = 12;
    private const long StableRunMs = 60_000;
    private const int NoExitCode = int.MinValue;
    private readonly MulticastOptions _options;
    private readonly object _processLock = new();
    private Process? _process;
    private string? _lastStatus;
    private string? _lastExitStatus;
    private string? _lastStderrLine;
    private string? _lastStartCommand;
    private string? _serverHost;
    private readonly ClientSettings _settings = ClientSettings.Load();
    private IReadOnlyList<SnapclientOutputDevice>? _outputDevices;
    private CancellationTokenSource? _reconnectStop;
    private long _startedUnixTimeMs;
    private long _lastExitUnixTimeMs;
    private long _lastStderrUnixTimeMs;
    private int _lastExitCode = NoExitCode;
    private int _exitCount;
    private int _reconnectAttempts;
    private bool _stopRequested;
    private int _volumePercent = 100;

    public event EventHandler<string>? StatusChanged;
    public bool IsRunning => _process is { HasExited: false };
    public int? ExitCode
    {
        get
        {
            if (_process is { HasExited: true })
            {
                return _process.ExitCode;
            }
            var lastExitCode = Volatile.Read(ref _lastExitCode);
            return lastExitCode == NoExitCode ? null : lastExitCode;
        }
    }
    public string? LastStatus => _lastStatus;
    public string? LastExitStatus => _lastExitStatus;
    public string? LastStderrLine => _lastStderrLine;
    public string? LastStartCommand => _lastStartCommand;
    public string? ServerHost => _serverHost;
    public int? ProcessId => _process is { HasExited: false } ? _process.Id : null;
    public long StartedUnixTimeMs => Interlocked.Read(ref _startedUnixTimeMs);
    public long LastExitUnixTimeMs => Interlocked.Read(ref _lastExitUnixTimeMs);
    public long LastStderrUnixTimeMs => Interlocked.Read(ref _lastStderrUnixTimeMs);
    public long UptimeMs
    {
        get
        {
            var startedMs = StartedUnixTimeMs;
            if (startedMs <= 0)
            {
                return 0;
            }
            var endMs = IsRunning ? DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() : LastExitUnixTimeMs;
            return endMs > startedMs ? endMs - startedMs : 0;
        }
    }
    public int ExitCount => Volatile.Read(ref _exitCount);
    public int ReconnectAttempts => Volatile.Read(ref _reconnectAttempts);
    public string TelemetryPath => ClientTelemetry.Path;
    public int VolumePercent => _volumePercent;
    public string? OutputDevice => _settings.OutputDevice;
    public SnapserverClientDiagnostics SnapserverDiagnostics => string.IsNullOrWhiteSpace(_serverHost)
        ? new SnapserverClientDiagnostics(false, "no remembered snapserver host", false, null, null)
        : SnapcastControl.GetClientDiagnostics(
            _serverHost,
            _options.SnapcastControlPort,
            Environment.MachineName,
            "default",
            TimeSpan.FromMilliseconds(750));

    public MulticastReceiver(MulticastOptions options)
    {
        _options = options;
    }

    public void Start(string serverHost) => Start(serverHost, resetReconnectAttempts: true);

    private void Start(string serverHost, bool resetReconnectAttempts)
    {
        lock (_processLock)
        {
            _serverHost = serverHost;
            _stopRequested = false;
            if (resetReconnectAttempts)
            {
                _reconnectAttempts = 0;
            }
            CancelReconnect();
            if (_process is { HasExited: false })
            {
                SetStatus($"Snapclient already connected to {serverHost}:{_options.SnapcastPort}");
                return;
            }

            DisposeExitedProcess();
        }

        try
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = ResolveToolPath("snapclient.exe"),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardError = true,
            };
            startInfo.ArgumentList.Add("-h");
            startInfo.ArgumentList.Add(serverHost);
            startInfo.ArgumentList.Add("-p");
            startInfo.ArgumentList.Add(_options.SnapcastPort.ToString());
            startInfo.ArgumentList.Add("--hostID");
            startInfo.ArgumentList.Add(Environment.MachineName);
            startInfo.ArgumentList.Add("--logsink");
            startInfo.ArgumentList.Add("stderr");
            startInfo.ArgumentList.Add("--logfilter");
            startInfo.ArgumentList.Add("*:info");
            if (!string.IsNullOrWhiteSpace(_settings.OutputDevice))
            {
                startInfo.ArgumentList.Add("--soundcard");
                startInfo.ArgumentList.Add(_settings.OutputDevice);
            }

            _lastStartCommand = startInfo.FileName + " " + string.Join(" ", startInfo.ArgumentList.Select(QuoteArgument));
            ClientTelemetry.Write("snapclient_starting", new { serverHost, snapcastPort = _options.SnapcastPort, outputDevice = _settings.OutputDevice, command = _lastStartCommand });
            _process = Process.Start(startInfo);
            if (_process is not null)
            {
                var process = _process;
                Interlocked.Exchange(ref _startedUnixTimeMs, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
                Interlocked.Exchange(ref _lastExitCode, NoExitCode);
                ClientTelemetry.Write("snapclient_started", new { serverHost, snapcastPort = _options.SnapcastPort, processId = process.Id, outputDevice = _settings.OutputDevice, command = _lastStartCommand });
                process.EnableRaisingEvents = true;
                process.ErrorDataReceived += (_, e) =>
                {
                    if (!string.IsNullOrWhiteSpace(e.Data))
                    {
                        _lastStderrLine = TrimStatus(e.Data);
                        Interlocked.Exchange(ref _lastStderrUnixTimeMs, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
                        ClientTelemetry.Write("snapclient_stderr", new { processId = process.Id, line = _lastStderrLine });
                        SetStatus(_lastStderrLine);
                    }
                };
                process.Exited += (_, _) =>
                {
                    var exitedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                    Interlocked.Increment(ref _exitCount);
                    Interlocked.Exchange(ref _lastExitUnixTimeMs, exitedAtMs);
                    Interlocked.Exchange(ref _lastExitCode, process.ExitCode);
                    var stopRequested = _stopRequested;
                    var uptimeMs = Math.Max(0, exitedAtMs - Interlocked.Read(ref _startedUnixTimeMs));
                    if (uptimeMs >= StableRunMs)
                    {
                        // A stable run means the last crash episode is over; give the
                        // next disconnect a fresh reconnect budget so occasional
                        // snapclient WASAPI aborts keep self-healing indefinitely.
                        Interlocked.Exchange(ref _reconnectAttempts, 0);
                    }
                    _lastExitStatus = stopRequested ? $"Shared stream stopped: {process.ExitCode}" : $"Shared stream disconnected: {process.ExitCode}";
                    ClientTelemetry.Write("snapclient_exited", new { processId = process.Id, process.ExitCode, serverHost, stopRequested, uptimeMs, lastStderr = _lastStderrLine, command = _lastStartCommand });
                    SetStatus(_lastExitStatus);
                    if (!stopRequested)
                    {
                        ScheduleReconnect(serverHost, process.ExitCode);
                    }
                };
                process.BeginErrorReadLine();
            }
            SetStatus($"Snapclient connected to {serverHost}:{_options.SnapcastPort}");
            _ = SetVolumeAsync(_volumePercent);
        }
        catch (Exception ex)
        {
            _lastExitStatus = $"Snapclient failed: {ex.Message}";
            ClientTelemetry.Write("snapclient_start_failed", new { serverHost, snapcastPort = _options.SnapcastPort, outputDevice = _settings.OutputDevice, error = ex.Message, command = _lastStartCommand });
            SetStatus(_lastExitStatus);
        }
    }

    public void RememberServer(string serverHost)
    {
        _serverHost = serverHost;
        ClientTelemetry.Write("snapclient_server_remembered", new { serverHost, snapcastPort = _options.SnapcastPort });
        SetStatus($"Shared stream available at {serverHost}:{_options.SnapcastPort}");
    }

    public bool StartLastServer()
    {
        if (string.IsNullOrWhiteSpace(_serverHost))
        {
            return false;
        }

        Start(_serverHost);
        return true;
    }

    public void Stop()
    {
        lock (_processLock)
        {
            _stopRequested = true;
            CancelReconnect();
            if (_process is { HasExited: false })
            {
                ClientTelemetry.Write("snapclient_stopping", new { processId = _process.Id, serverHost = _serverHost });
                _process.Kill(entireProcessTree: true);
            }
            _process?.Dispose();
            _process = null;
        }
        SetStatus("Snapclient stopped");
    }

    public async Task SetVolumeAsync(int percent)
    {
        _volumePercent = Math.Clamp(percent, 0, 100);
        if (string.IsNullOrWhiteSpace(_serverHost))
        {
            SetStatus($"Volume {_volumePercent}% saved for next stream");
            return;
        }
        await SnapcastControl.SetVolumeAsync(
            _serverHost,
            _options.SnapcastControlPort,
            Environment.MachineName,
            _volumePercent).ConfigureAwait(false);
        ClientTelemetry.Write("snapclient_volume", new { serverHost = _serverHost, percent = _volumePercent });
        SetStatus($"Snapclient volume {_volumePercent}%");
    }

    public void SetOutputDevice(string? soundcard)
    {
        _settings.OutputDevice = string.IsNullOrWhiteSpace(soundcard) ? null : soundcard.Trim();
        _settings.Save();
        ClientTelemetry.Write("snapclient_output_device", new { outputDevice = _settings.OutputDevice });
        var serverHost = _serverHost;
        var wasRunning = IsRunning;
        if (wasRunning)
        {
            Stop();
        }
        if (wasRunning && !string.IsNullOrWhiteSpace(serverHost))
        {
            Start(serverHost);
        }
        else
        {
            SetStatus(string.IsNullOrWhiteSpace(_settings.OutputDevice) ? "Output device set to default" : $"Output device set: {_settings.OutputDevice}");
        }
    }

    public IReadOnlyList<SnapclientOutputDevice> ListOutputDevices(bool refresh = false)
    {
        if (!refresh && _outputDevices is not null)
        {
            return _outputDevices;
        }

        try
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = ResolveToolPath("snapclient.exe"),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            startInfo.ArgumentList.Add("--list");
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                return Array.Empty<SnapclientOutputDevice>();
            }
            var outputTask = process.StandardOutput.ReadToEndAsync();
            var errorTask = process.StandardError.ReadToEndAsync();
            if (!process.WaitForExit(2500))
            {
                process.Kill(entireProcessTree: true);
                ClientTelemetry.Write("snapclient_output_devices_failed", new { error = "snapclient --list timed out" });
                return Array.Empty<SnapclientOutputDevice>();
            }
            var output = outputTask.GetAwaiter().GetResult() + Environment.NewLine + errorTask.GetAwaiter().GetResult();
            _outputDevices = SnapclientOutputDevice.ParseList(output, WindowsAudioDevices.GetFriendlyNames());
            ClientTelemetry.Write("snapclient_output_devices", new { devices = _outputDevices.Select(device => device.Soundcard).ToArray() });
            return _outputDevices;
        }
        catch (Exception ex)
        {
            ClientTelemetry.Write("snapclient_output_devices_failed", new { error = ex.Message });
            return Array.Empty<SnapclientOutputDevice>();
        }
    }

    private void SetStatus(string status)
    {
        _lastStatus = status;
        ClientTelemetry.Write("status", new { status = TrimStatus(status), snapclientRunning = IsRunning, snapclientExitCode = ExitCode });
        StatusChanged?.Invoke(this, status);
    }

    private static string QuoteArgument(string value)
    {
        return value.Any(char.IsWhiteSpace) ? '"' + value.Replace("\"", "\\\"", StringComparison.Ordinal) + '"' : value;
    }

    private void ScheduleReconnect(string serverHost, int exitCode)
    {
        lock (_processLock)
        {
            if (_stopRequested || _reconnectStop is not null)
            {
                return;
            }

            _reconnectStop = new CancellationTokenSource();
        }

        var tokenSource = _reconnectStop;
        _ = Task.Run(async () =>
        {
            try
            {
                while (tokenSource is not null && !tokenSource.IsCancellationRequested)
                {
                    var attempt = Interlocked.Increment(ref _reconnectAttempts);
                    if (attempt > MaxReconnectAttempts)
                    {
                        ClientTelemetry.Write("snapclient_reconnect_abandoned", new { serverHost, exitCode, attempts = attempt - 1 });
                        SetStatus($"Shared stream disconnected after {attempt - 1} reconnect attempts");
                        return;
                    }

                    var delay = ReconnectDelayFor(attempt);
                    ClientTelemetry.Write("snapclient_reconnect_waiting", new { serverHost, exitCode, attempt, delayMs = (long)delay.TotalMilliseconds });
                    SetStatus($"Shared stream reconnecting ({attempt}/{MaxReconnectAttempts})");
                    await Task.Delay(delay, tokenSource.Token).ConfigureAwait(false);
                    if (tokenSource.IsCancellationRequested)
                    {
                        return;
                    }

                    lock (_processLock)
                    {
                        if (_stopRequested || _process is { HasExited: false })
                        {
                            return;
                        }

                        DisposeExitedProcess();
                    }

                    Start(serverHost, resetReconnectAttempts: false);
                    return;
                }
            }
            catch (OperationCanceledException)
            {
            }
            finally
            {
                lock (_processLock)
                {
                    if (ReferenceEquals(_reconnectStop, tokenSource))
                    {
                        _reconnectStop.Dispose();
                        _reconnectStop = null;
                    }
                }
            }
        });
    }

    private static TimeSpan ReconnectDelayFor(int attempt)
    {
        var seconds = ReconnectDelay.TotalSeconds * Math.Pow(2, Math.Max(0, attempt - 1));
        return TimeSpan.FromSeconds(Math.Min(MaxReconnectDelay.TotalSeconds, seconds));
    }

    private void CancelReconnect()
    {
        _reconnectStop?.Cancel();
        _reconnectStop = null;
    }

    private void DisposeExitedProcess()
    {
        if (_process is { HasExited: true })
        {
            _process.Dispose();
            _process = null;
        }
    }

    private static string TrimStatus(string status) => status.Length > 180 ? status[..180] : status;

    private static string ResolveToolPath(string fileName)
    {
        var local = Path.Combine(AppContext.BaseDirectory, fileName);
        return File.Exists(local) ? local : fileName;
    }

    public void Dispose()
    {
        Stop();
    }
}

internal sealed class ClientSettings
{
    public string? OutputDevice { get; set; }

    private static string Path => System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "SlimeAudio",
        "settings.json");

    public static ClientSettings Load()
    {
        try
        {
            if (!File.Exists(Path))
            {
                return new ClientSettings();
            }

            return JsonSerializer.Deserialize<ClientSettings>(File.ReadAllText(Path)) ?? new ClientSettings();
        }
        catch
        {
            return new ClientSettings();
        }
    }

    public void Save()
    {
        var directory = System.IO.Path.GetDirectoryName(Path);
        if (!string.IsNullOrWhiteSpace(directory))
        {
            Directory.CreateDirectory(directory);
        }

        File.WriteAllText(Path, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }), Encoding.UTF8);
    }
}

internal sealed record SnapclientOutputDevice(string Soundcard, string DisplayName)
{
    public static IReadOnlyList<SnapclientOutputDevice> ParseList(string output, IReadOnlyDictionary<string, string>? friendlyNames = null)
    {
        var devices = new List<SnapclientOutputDevice>();
        foreach (var rawLine in output.Split(new[] { "\r\n", "\n" }, StringSplitOptions.None))
        {
            var line = rawLine.Trim();
            var separator = line.IndexOf(':');
            if (separator <= 0 || !int.TryParse(line[..separator].Trim(), out _))
            {
                continue;
            }

            var soundcard = line[(separator + 1)..].Trim();
            if (string.IsNullOrWhiteSpace(soundcard))
            {
                continue;
            }

            var displayName = friendlyNames is not null && friendlyNames.TryGetValue(soundcard, out var friendlyName)
                ? friendlyName
                : soundcard;
            devices.Add(new SnapclientOutputDevice(soundcard, $"{devices.Count}: {displayName}"));
        }

        return devices;
    }
}

internal static class WindowsAudioDevices
{
    public static IReadOnlyDictionary<string, string> GetFriendlyNames()
    {
        try
        {
            using var enumerator = new MMDeviceEnumerator();
            var devices = enumerator.EnumerateAudioEndPoints(DataFlow.Render, DeviceState.Active);
            return devices.ToDictionary(device => device.ID, device => device.FriendlyName, StringComparer.OrdinalIgnoreCase);
        }
        catch
        {
            return new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        }
    }
}

internal static class ClientTelemetry
{
    private const long MaxFileBytes = 64 * 1024 * 1024;
    private static readonly object Lock = new();

    public static string Path { get; } = System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "SlimeAudio",
        "telemetry.jsonl");

    private static string RotatedPath => Path + ".1";

    public static void Write(string eventName, object? data = null)
    {
        try
        {
            var directory = System.IO.Path.GetDirectoryName(Path);
            if (!string.IsNullOrWhiteSpace(directory))
            {
                Directory.CreateDirectory(directory);
            }

            var payload = JsonSerializer.Serialize(new
            {
                ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                local_time = DateTimeOffset.Now.ToString("O"),
                machine = Environment.MachineName,
                version = VersionInfo.DisplayVersion,
                event_name = eventName,
                data
            });
            lock (Lock)
            {
                var info = new FileInfo(Path);
                if (info.Exists && info.Length >= MaxFileBytes)
                {
                    File.Move(Path, RotatedPath, overwrite: true);
                }
                File.AppendAllText(Path, payload + Environment.NewLine, Encoding.UTF8);
            }
        }
        catch
        {
            // Telemetry must never break playback or tray startup.
        }
    }
}

internal sealed record SnapserverClientDiagnostics(
    bool Ok,
    string? Error,
    bool ClientConnected,
    string? ClientStream,
    string? StreamStatus);

internal static class SnapcastControl
{
    public static SnapserverClientDiagnostics GetClientDiagnostics(
        string host,
        int port,
        string clientId,
        string expectedStreamId,
        TimeSpan timeout)
    {
        try
        {
            using var client = new TcpClient();
            client.ConnectAsync(host, port).WaitAsync(timeout).GetAwaiter().GetResult();
            using var stream = client.GetStream();
            var request = JsonSerializer.Serialize(new
            {
                id = 1,
                jsonrpc = "2.0",
                method = "Server.GetStatus"
            }) + "\n";
            var payload = Encoding.UTF8.GetBytes(request);
            stream.WriteAsync(payload).AsTask().WaitAsync(timeout).GetAwaiter().GetResult();

            using var timeoutSource = new CancellationTokenSource(timeout);
            var buffer = new byte[65536];
            var read = stream.ReadAsync(buffer, timeoutSource.Token).AsTask().GetAwaiter().GetResult();
            var response = Encoding.UTF8.GetString(buffer, 0, read);
            return ParseClientDiagnostics(response, clientId, expectedStreamId);
        }
        catch (Exception ex)
        {
            return new SnapserverClientDiagnostics(false, ex.Message, false, null, null);
        }
    }

    private static SnapserverClientDiagnostics ParseClientDiagnostics(string response, string clientId, string expectedStreamId)
    {
        try
        {
            using var document = JsonDocument.Parse(response);
            var server = document.RootElement.GetProperty("result").GetProperty("server");
            var streamStatuses = new Dictionary<string, string?>(StringComparer.Ordinal);
            if (server.TryGetProperty("streams", out var streams))
            {
                foreach (var streamElement in streams.EnumerateArray())
                {
                    var id = streamElement.TryGetProperty("id", out var idValue) ? idValue.GetString() : null;
                    if (!string.IsNullOrWhiteSpace(id))
                    {
                        streamStatuses[id] = streamElement.TryGetProperty("status", out var statusValue)
                            ? statusValue.GetString()
                            : null;
                    }
                }
            }
            streamStatuses.TryGetValue(expectedStreamId, out var expectedStreamStatus);

            if (server.TryGetProperty("groups", out var groups))
            {
                foreach (var group in groups.EnumerateArray())
                {
                    var groupStream = group.TryGetProperty("stream_id", out var streamValue) ? streamValue.GetString() : null;
                    if (!group.TryGetProperty("clients", out var clients))
                    {
                        continue;
                    }

                    foreach (var snapclient in clients.EnumerateArray())
                    {
                        var id = snapclient.TryGetProperty("id", out var idValue) ? idValue.GetString() : null;
                        if (!string.Equals(id, clientId, StringComparison.OrdinalIgnoreCase))
                        {
                            continue;
                        }

                        var connected = snapclient.TryGetProperty("connected", out var connectedValue) && connectedValue.GetBoolean();
                        var streamStatus = groupStream is not null && streamStatuses.TryGetValue(groupStream, out var actualStreamStatus)
                            ? actualStreamStatus
                            : null;
                        return new SnapserverClientDiagnostics(true, null, connected, groupStream, streamStatus);
                    }
                }
            }

            return new SnapserverClientDiagnostics(true, "client not present in snapserver status", false, null, expectedStreamStatus);
        }
        catch (Exception ex)
        {
            return new SnapserverClientDiagnostics(false, ex.Message, false, null, null);
        }
    }

    public static async Task SetVolumeAsync(string host, int port, string clientId, int percent)
    {
        using var client = new TcpClient();
        await client.ConnectAsync(host, port).WaitAsync(TimeSpan.FromSeconds(2)).ConfigureAwait(false);
        using var stream = client.GetStream();
        var request = JsonSerializer.Serialize(new
        {
            id = 1,
            jsonrpc = "2.0",
            method = "Client.SetVolume",
            @params = new
            {
                id = clientId,
                volume = new
                {
                    muted = false,
                    percent
                }
            }
        }) + "\n";
        var payload = Encoding.UTF8.GetBytes(request);
        await stream.WriteAsync(payload).ConfigureAwait(false);
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        var buffer = new byte[4096];
        var read = await stream.ReadAsync(buffer, timeout.Token).ConfigureAwait(false);
        var response = Encoding.UTF8.GetString(buffer, 0, read);
        if (response.Contains("\"error\"", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("snapcast rejected volume update");
        }
    }
}

internal sealed class AudioReceiver : IDisposable
{
    private const int ReceiveBufferBytes = 4 * 1024 * 1024;
    private readonly CancellationTokenSource _stop = new();
    private readonly MulticastReceiver _multicast;
    private readonly object _sessionsLock = new();
    private readonly Dictionary<Guid, PlaybackSession> _sessions = new();
    private bool _streamMuted;
    private UdpClient? _udp;
    private long _decodeFailures;
    private long _droppedMutedPackets;
    private long _lastPacketUnixTimeMs;
    private long _receivedBytes;
    private long _receivedPackets;
    private long _resetCount;

    public int Port { get; }
    public bool StreamMuted => _streamMuted;
    public event EventHandler<string>? StatusChanged;

    public AudioReceiver(int port, MulticastReceiver multicast)
    {
        Port = port;
        _multicast = multicast;
    }

    public void Start()
    {
        _udp = new UdpClient(Port);
        _udp.Client.ReceiveBufferSize = ReceiveBufferBytes;
        _ = Task.Run(ReceiveLoop);
        StatusChanged?.Invoke(this, $"Slime Audio listening on UDP {Port}");
    }

    private async Task ReceiveLoop()
    {
        if (_udp is null)
        {
            return;
        }

        while (!_stop.IsCancellationRequested)
        {
            try
            {
                var result = await _udp.ReceiveAsync(_stop.Token).ConfigureAwait(false);
                if (TryHandleControl(result))
                {
                    continue;
                }

                if (!AudioPacket.TryDecode(result.Buffer, out var packet))
                {
                    Interlocked.Increment(ref _decodeFailures);
                    continue;
                }
                Handle(packet);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                StatusChanged?.Invoke(this, $"Slime Audio error: {ex.Message}");
            }
        }
    }

    private bool TryHandleControl(UdpReceiveResult result)
    {
        var text = Encoding.UTF8.GetString(result.Buffer).Trim();
        if (text == ControlMessages.Discover)
        {
            var response = DiscoveryResponse.Current(Port, VersionInfo.DisplayVersion, StreamMuted, Diagnostics()).ToJson();
            var bytes = Encoding.UTF8.GetBytes(response);
            _udp?.Send(bytes, bytes.Length, result.RemoteEndPoint);
            StatusChanged?.Invoke(this, $"Discovery response sent to {result.RemoteEndPoint.Address}");
            return true;
        }

        if (text == ControlMessages.Update)
        {
            _ = Task.Run(async () =>
            {
                try
                {
                    var message = await UpdateService.DownloadAndRunLatestInstallerAsync().ConfigureAwait(false);
                    StatusChanged?.Invoke(this, message);
                }
                catch (Exception ex)
                {
                    StatusChanged?.Invoke(this, $"Update failed: {ex.Message}");
                }
            });
            return true;
        }

        if (text == ControlMessages.SharedStreamStart)
        {
            var serverHost = result.RemoteEndPoint.Address.ToString();
            if (StreamMuted)
            {
                _multicast.RememberServer(serverHost);
                StatusChanged?.Invoke(this, "Shared stream ignored while muted");
            }
            else
            {
                _multicast.Start(serverHost);
            }
            return true;
        }

        if (text == ControlMessages.SharedStreamStop)
        {
            _multicast.Stop();
            return true;
        }

        if (text == ControlMessages.ResetAudio)
        {
            ResetAudio();
            return true;
        }

        var outputDevice = OutputDeviceSelection.FromControlMessage(text);
        if (outputDevice is not null)
        {
            _multicast.SetOutputDevice(outputDevice.Soundcard);
            return true;
        }

        var effect = EffectEnvelope.FromControlMessage(text);
        if (effect is not null)
        {
            List<PlaybackSession> sessions;
            lock (_sessionsLock)
            {
                sessions = _sessions.Values.ToList();
            }
            foreach (var session in sessions)
            {
                session.Apply(effect);
            }
            StatusChanged?.Invoke(this, "Applied audio effect envelope");
            return true;
        }

        return false;
    }

    private void Handle(AudioPacket packet)
    {
        if (StreamMuted)
        {
            Interlocked.Increment(ref _droppedMutedPackets);
            return;
        }

        Interlocked.Increment(ref _receivedPackets);
        Interlocked.Add(ref _receivedBytes, packet.Payload.Length);
        Interlocked.Exchange(ref _lastPacketUnixTimeMs, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());

        PlaybackSession session;
        lock (_sessionsLock)
        {
            if (!_sessions.TryGetValue(packet.SessionId, out session!))
            {
                session = new PlaybackSession(packet);
                _sessions[packet.SessionId] = session;
                StatusChanged?.Invoke(this, $"Buffered session {packet.SessionId:N}");
            }
        }

        if (packet.Type == AudioPacketType.End)
        {
            session.MarkEnded(packet);
            return;
        }

        session.Add(packet);
        if (session.TryStart())
        {
            StatusChanged?.Invoke(this, $"Playing synced audio session {packet.SessionId:N}");
        }
    }

    public void SetStreamMuted(bool muted)
    {
        if (_streamMuted == muted)
        {
            return;
        }

        _streamMuted = muted;
        if (muted)
        {
            ResetAudio();
            StatusChanged?.Invoke(this, "Stream muted here");
        }
        else
        {
            if (!_multicast.StartLastServer())
            {
                StatusChanged?.Invoke(this, $"Slime Audio listening on UDP {Port}");
            }
        }
    }

    private void ResetAudio()
    {
        Interlocked.Increment(ref _resetCount);
        _multicast.Stop();
        List<PlaybackSession> sessions;
        lock (_sessionsLock)
        {
            sessions = _sessions.Values.ToList();
            _sessions.Clear();
        }
        foreach (var session in sessions)
        {
            session.Dispose();
        }
        StatusChanged?.Invoke(this, "Audio engine reset");
    }

    private AudioDiagnostics Diagnostics()
    {
        long missingFrames = 0;
        long readCalls = 0;
        var maxBufferedPackets = 0;
        var maxBufferedPacketSpan = 0;
        var latestSequence = -1;
        string? latestSessionId = null;

        List<KeyValuePair<Guid, PlaybackSession>> sessions;
        lock (_sessionsLock)
        {
            sessions = _sessions.ToList();
        }

        foreach (var pair in sessions)
        {
            var diagnostics = pair.Value.Diagnostics;
            missingFrames += diagnostics.MissingFrames;
            readCalls += diagnostics.ReadCalls;
            maxBufferedPackets = Math.Max(maxBufferedPackets, diagnostics.BufferedPackets);
            maxBufferedPacketSpan = Math.Max(maxBufferedPacketSpan, diagnostics.BufferedPacketSpan);
            if (diagnostics.LatestSequence > latestSequence)
            {
                latestSequence = diagnostics.LatestSequence;
                latestSessionId = pair.Key.ToString("N");
            }
        }

        var snapserver = _multicast.SnapserverDiagnostics;
        return new AudioDiagnostics(
            sessions.Count,
            Interlocked.Read(ref _receivedPackets),
            Interlocked.Read(ref _receivedBytes),
            Interlocked.Read(ref _droppedMutedPackets),
            Interlocked.Read(ref _decodeFailures),
            Interlocked.Read(ref _resetCount),
            missingFrames,
            readCalls,
            Interlocked.Read(ref _lastPacketUnixTimeMs),
            maxBufferedPackets,
            maxBufferedPacketSpan,
            latestSequence,
            latestSessionId,
            _multicast.IsRunning,
            _multicast.ExitCode,
            _multicast.LastStatus,
            _multicast.ServerHost,
            _multicast.ProcessId,
            _multicast.StartedUnixTimeMs,
            _multicast.LastExitUnixTimeMs,
            _multicast.ExitCount,
            _multicast.LastStderrUnixTimeMs,
            _multicast.TelemetryPath,
            _multicast.OutputDevice,
            _multicast.ListOutputDevices().Select(device => device.Soundcard).ToArray(),
            _multicast.LastExitStatus,
            _multicast.LastStderrLine,
            _multicast.LastStartCommand,
            _multicast.UptimeMs,
            _multicast.ReconnectAttempts,
            snapserver.Ok,
            snapserver.Error,
            snapserver.ClientConnected,
            snapserver.ClientStream,
            snapserver.StreamStatus);
    }

    public void Dispose()
    {
        _stop.Cancel();
        _udp?.Dispose();
        List<PlaybackSession> sessions;
        lock (_sessionsLock)
        {
            sessions = _sessions.Values.ToList();
            _sessions.Clear();
        }
        foreach (var session in sessions)
        {
            session.Dispose();
        }
        _stop.Dispose();
    }
}
