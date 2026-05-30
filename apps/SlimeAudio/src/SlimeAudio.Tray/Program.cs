using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Diagnostics;
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
    private readonly List<ToolStripMenuItem> _volumeItems = new();
    private bool _updatingMuteMenu;
    private bool _updatingVolumeMenu;

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
        _icon.ContextMenuStrip.Items.Add("Check for updates", null, async (_, _) => await CheckForUpdates());
        _icon.ContextMenuStrip.Items.Add("Quit", null, (_, _) => ExitThread());
        UpdateMuteMenu();
        _receiver.Start();
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
    private readonly MulticastOptions _options;
    private Process? _process;
    private string? _lastStatus;
    private string? _serverHost;
    private int _volumePercent = 100;

    public event EventHandler<string>? StatusChanged;
    public bool IsRunning => _process is { HasExited: false };
    public int? ExitCode => _process is { HasExited: true } ? _process.ExitCode : null;
    public string? LastStatus => _lastStatus;
    public int VolumePercent => _volumePercent;

    public MulticastReceiver(MulticastOptions options)
    {
        _options = options;
    }

    public void Start(string serverHost)
    {
        _serverHost = serverHost;
        if (_process is { HasExited: false })
        {
            SetStatus($"Snapclient already connected to {serverHost}:{_options.SnapcastPort}");
            return;
        }

        try
        {
            var args = $"-h \"{serverHost}\" -p {_options.SnapcastPort} --hostID \"{Environment.MachineName}\" --logsink stderr --logfilter \"*:warning\"";
            _process = Process.Start(new ProcessStartInfo
            {
                FileName = ResolveToolPath("snapclient.exe"),
                Arguments = args,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardError = true,
            });
            if (_process is not null)
            {
                var process = _process;
                process.EnableRaisingEvents = true;
                process.ErrorDataReceived += (_, e) =>
                {
                    if (!string.IsNullOrWhiteSpace(e.Data))
                    {
                        SetStatus(TrimStatus(e.Data));
                    }
                };
                process.Exited += (_, _) =>
                {
                    SetStatus($"Shared stream exited: {process.ExitCode}");
                };
                process.BeginErrorReadLine();
            }
            SetStatus($"Snapclient connected to {serverHost}:{_options.SnapcastPort}");
            _ = SetVolumeAsync(_volumePercent);
        }
        catch (Exception ex)
        {
            SetStatus($"Snapclient failed: {ex.Message}");
        }
    }

    public void RememberServer(string serverHost)
    {
        _serverHost = serverHost;
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
        if (_process is { HasExited: false })
        {
            _process.Kill(entireProcessTree: true);
        }
        _process?.Dispose();
        _process = null;
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
        SetStatus($"Snapclient volume {_volumePercent}%");
    }

    private void SetStatus(string status)
    {
        _lastStatus = status;
        StatusChanged?.Invoke(this, status);
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

internal static class SnapcastControl
{
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
            _multicast.LastStatus);
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
