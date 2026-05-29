using System.Net.Sockets;
using System.Text;
using SlimeAudio.Protocol;

namespace SlimeAudio.Tray;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        var port = TryParsePort(args) ?? 47777;
        using var receiver = new AudioReceiver(port);
        Application.Run(new TrayContext(receiver));
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

internal sealed class TrayContext : ApplicationContext
{
    private readonly AudioReceiver _receiver;
    private readonly NotifyIcon _icon;

    public TrayContext(AudioReceiver receiver)
    {
        _receiver = receiver;
        _icon = new NotifyIcon
        {
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath) ?? SystemIcons.Application,
            Text = TrimForTray($"Slime Audio listening on UDP {_receiver.Port}"),
            Visible = true,
            ContextMenuStrip = new ContextMenuStrip(),
        };
        _receiver.StatusChanged += (_, message) => _icon.Text = TrimForTray(message);
        _icon.ContextMenuStrip.Items.Add("Status", null, (_, _) => MessageBox.Show(_icon.Text, "Slime Audio"));
        _icon.ContextMenuStrip.Items.Add("Check for updates", null, async (_, _) => await CheckForUpdates());
        _icon.ContextMenuStrip.Items.Add("Quit", null, (_, _) => ExitThread());
        _receiver.Start();
    }

    private async Task CheckForUpdates()
    {
        try
        {
            _icon.Text = TrimForTray("Slime Audio checking for updates");
            var message = await UpdateService.OpenLatestInstallerAsync();
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
        }
        base.Dispose(disposing);
    }

    private static string TrimForTray(string text) => text.Length > 63 ? text[..63] : text;
}

internal sealed class AudioReceiver : IDisposable
{
    private readonly CancellationTokenSource _stop = new();
    private readonly Dictionary<Guid, PlaybackSession> _sessions = new();
    private UdpClient? _udp;

    public int Port { get; }
    public event EventHandler<string>? StatusChanged;

    public AudioReceiver(int port)
    {
        Port = port;
    }

    public void Start()
    {
        _udp = new UdpClient(Port);
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
            var response = DiscoveryResponse.Current(Port, Application.ProductVersion).ToJson();
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
                    var message = await UpdateService.OpenLatestInstallerAsync().ConfigureAwait(false);
                    StatusChanged?.Invoke(this, message);
                }
                catch (Exception ex)
                {
                    StatusChanged?.Invoke(this, $"Update failed: {ex.Message}");
                }
            });
            return true;
        }

        return false;
    }

    private void Handle(AudioPacket packet)
    {
        if (!_sessions.TryGetValue(packet.SessionId, out var session))
        {
            session = new PlaybackSession(packet);
            _sessions[packet.SessionId] = session;
            StatusChanged?.Invoke(this, $"Buffered session {packet.SessionId:N}");
        }

        if (packet.Type == AudioPacketType.End)
        {
            session.MarkEnded();
            return;
        }

        session.Add(packet);
        if (session.TryStart())
        {
            StatusChanged?.Invoke(this, $"Playing synced audio session {packet.SessionId:N}");
        }
    }

    public void Dispose()
    {
        _stop.Cancel();
        _udp?.Dispose();
        foreach (var session in _sessions.Values)
        {
            session.Dispose();
        }
        _stop.Dispose();
    }
}
