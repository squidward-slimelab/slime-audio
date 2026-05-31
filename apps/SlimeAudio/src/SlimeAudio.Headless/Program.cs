using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using SlimeAudio.Protocol;

namespace SlimeAudio.Headless;

internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        var options = HeadlessOptions.Parse(args);
        using var receiver = new HeadlessReceiver(options);
        receiver.StatusChanged += (_, message) => Console.Error.WriteLine($"[{DateTimeOffset.Now:O}] {message}");
        await receiver.RunAsync().ConfigureAwait(false);
        return 0;
    }
}

internal sealed record HeadlessOptions(
    int Port,
    string MulticastGroup,
    int MulticastPort,
    int SnapcastPort,
    bool NoAudio,
    int BufferMs,
    bool StartMuted)
{
    public static HeadlessOptions Parse(string[] args)
    {
        var port = 47777;
        var multicastGroup = "239.77.77.77";
        var multicastPort = 47778;
        var snapcastPort = 1704;
        var noAudio = false;
        var bufferMs = 100;
        var startMuted = false;

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--port" when i + 1 < args.Length && int.TryParse(args[i + 1], out var parsedPort):
                    port = parsedPort;
                    i++;
                    break;
                case "--multicast-group" when i + 1 < args.Length:
                    multicastGroup = args[++i];
                    break;
                case "--multicast-port" when i + 1 < args.Length && int.TryParse(args[i + 1], out var parsedMulticastPort):
                    multicastPort = parsedMulticastPort;
                    i++;
                    break;
                case "--snapcast-port" when i + 1 < args.Length && int.TryParse(args[i + 1], out var parsedSnapcastPort):
                    snapcastPort = parsedSnapcastPort;
                    i++;
                    break;
                case "--no-audio":
                    noAudio = true;
                    break;
                case "--muted":
                    startMuted = true;
                    break;
                case "--buffer-ms" when i + 1 < args.Length && int.TryParse(args[i + 1], out var parsedBufferMs):
                    bufferMs = Math.Max(10, parsedBufferMs);
                    i++;
                    break;
                case "--help":
                    Console.WriteLine("Usage: SlimeAudio.Headless [--port 47777] [--no-audio] [--snapcast-port 1704]");
                    Environment.Exit(0);
                    break;
            }
        }

        return new HeadlessOptions(port, multicastGroup, multicastPort, snapcastPort, noAudio, bufferMs, startMuted);
    }
}

internal sealed class HeadlessReceiver : IDisposable
{
    private const int ReceiveBufferBytes = 4 * 1024 * 1024;
    private readonly HeadlessOptions _options;
    private readonly CancellationTokenSource _stop = new();
    private readonly ConcurrentDictionary<Guid, HeadlessPlaybackSession> _sessions = new();
    private UdpClient? _udp;
    private Process? _multicastProcess;
    private string? _multicastStatus;
    private long _decodeFailures;
    private long _droppedMutedPackets;
    private long _lastPacketUnixTimeMs;
    private long _receivedBytes;
    private long _receivedPackets;
    private long _resetCount;
    private bool _streamMuted;

    public event EventHandler<string>? StatusChanged;

    public HeadlessReceiver(HeadlessOptions options)
    {
        _options = options;
        _streamMuted = options.StartMuted;
    }

    public async Task RunAsync()
    {
        Console.CancelKeyPress += (_, eventArgs) =>
        {
            eventArgs.Cancel = true;
            _stop.Cancel();
        };
        AppDomain.CurrentDomain.ProcessExit += (_, _) => _stop.Cancel();

        _udp = new UdpClient(_options.Port);
        _udp.Client.ReceiveBufferSize = ReceiveBufferBytes;
        StatusChanged?.Invoke(this, $"Slime Audio headless listening on UDP {_options.Port}");

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
                StatusChanged?.Invoke(this, $"receiver error: {ex.Message}");
            }
        }
    }

    private bool TryHandleControl(UdpReceiveResult result)
    {
        var text = Encoding.UTF8.GetString(result.Buffer).Trim();
        if (text == ControlMessages.Discover)
        {
            var response = DiscoveryResponse.Current(_options.Port, VersionInfo.DisplayVersion, _streamMuted, Diagnostics()).ToJson();
            var bytes = Encoding.UTF8.GetBytes(response);
            _udp?.Send(bytes, bytes.Length, result.RemoteEndPoint);
            return true;
        }
        if (text == ControlMessages.SharedStreamStart)
        {
            StartMulticast(result.RemoteEndPoint.Address.ToString());
            return true;
        }
        if (text == ControlMessages.SharedStreamStop)
        {
            StopMulticast();
            return true;
        }
        if (text == ControlMessages.ResetAudio)
        {
            ResetAudio();
            return true;
        }
        if (OutputDeviceSelection.FromControlMessage(text) is not null)
        {
            SetMulticastStatus("output device selection ignored by headless receiver");
            return true;
        }
        return EffectEnvelope.FromControlMessage(text) is not null;
    }

    private void Handle(AudioPacket packet)
    {
        if (_streamMuted)
        {
            Interlocked.Increment(ref _droppedMutedPackets);
            return;
        }

        Interlocked.Increment(ref _receivedPackets);
        Interlocked.Add(ref _receivedBytes, packet.Payload.Length);
        Interlocked.Exchange(ref _lastPacketUnixTimeMs, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());

        var session = _sessions.GetOrAdd(packet.SessionId, id =>
        {
            StatusChanged?.Invoke(this, $"buffered session {id:N}");
            return new HeadlessPlaybackSession(packet, _options);
        });

        if (packet.Type == AudioPacketType.End)
        {
            session.MarkEnded(packet.Sequence);
            return;
        }

        session.Add(packet);
        session.TryStart();
    }

    private void StartMulticast(string serverHost)
    {
        if (_options.NoAudio)
        {
            SetMulticastStatus("snapclient ignored because audio sink is disabled");
            return;
        }
        if (_multicastProcess is { HasExited: false })
        {
            SetMulticastStatus("snapclient already running");
            return;
        }

        var args = $"-h \"{serverHost}\" -p {_options.SnapcastPort} --hostID \"{Environment.MachineName}\" --logsink stderr --logfilter \"*:warning\"";
        _multicastProcess = Process.Start(new ProcessStartInfo
        {
            FileName = "snapclient",
            Arguments = args,
            UseShellExecute = false,
            CreateNoWindow = true,
        });
        SetMulticastStatus($"snapclient connected to {serverHost}:{_options.SnapcastPort}");
    }

    private void StopMulticast()
    {
        if (_multicastProcess is { HasExited: false })
        {
            _multicastProcess.Kill(entireProcessTree: true);
        }
        _multicastProcess?.Dispose();
        _multicastProcess = null;
        SetMulticastStatus("snapclient stopped");
    }

    private void SetMulticastStatus(string status)
    {
        _multicastStatus = status;
        StatusChanged?.Invoke(this, status);
    }

    private void ResetAudio()
    {
        Interlocked.Increment(ref _resetCount);
        StopMulticast();
        foreach (var pair in _sessions)
        {
            if (_sessions.TryRemove(pair.Key, out var session))
            {
                session.Dispose();
            }
        }
        StatusChanged?.Invoke(this, "audio reset");
    }

    private AudioDiagnostics Diagnostics()
    {
        var missingFrames = 0L;
        var readCalls = 0L;
        var maxBufferedPackets = 0;
        var maxBufferedPacketSpan = 0;
        var latestSequence = -1;
        string? latestSessionId = null;

        foreach (var pair in _sessions)
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
            _sessions.Count,
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
            _multicastProcess is { HasExited: false },
            _multicastProcess is { HasExited: true } ? _multicastProcess.ExitCode : null,
            _multicastStatus);
    }

    public void Dispose()
    {
        _stop.Cancel();
        _udp?.Dispose();
        ResetAudio();
        _stop.Dispose();
    }
}

internal sealed record HeadlessPlaybackDiagnostics(
    long MissingFrames,
    long ReadCalls,
    int BufferedPackets,
    int BufferedPacketSpan,
    int LatestSequence);

internal sealed class HeadlessPlaybackSession : IDisposable
{
    private const int CleanupSlackPackets = 200;
    private const int DriftNudgeFrames = 2;
    private const int DriftToleranceMs = 15;
    private readonly object _lock = new();
    private readonly Dictionary<int, byte[]> _packets = new();
    private readonly HeadlessOptions _options;
    private readonly long _startUnixTimeMs;
    private readonly int _sampleRate;
    private readonly short _channels;
    private readonly int _blockAlign;
    private Process? _sink;
    private Task? _playTask;
    private int _packetFrames;
    private int _lastCleanupPacket = -1;
    private long? _nextFrame;
    private long? _endFrame;
    private long _missingFrames;
    private long _readCalls;
    private int _latestSequence = -1;

    public HeadlessPlaybackSession(AudioPacket firstPacket, HeadlessOptions options)
    {
        _options = options;
        _startUnixTimeMs = firstPacket.StartUnixTimeMs;
        _sampleRate = firstPacket.SampleRate;
        _channels = firstPacket.Channels;
        _blockAlign = Math.Max(1, _channels * firstPacket.BitsPerSample / 8);
    }

    public void Add(AudioPacket packet)
    {
        if (packet.Payload.Length == 0)
        {
            return;
        }

        lock (_lock)
        {
            _packets[packet.Sequence] = packet.Payload;
            _latestSequence = Math.Max(_latestSequence, packet.Sequence);
            if (_packetFrames <= 0)
            {
                _packetFrames = Math.Max(1, packet.Payload.Length / _blockAlign);
            }
        }
    }

    public void MarkEnded(int endSequence)
    {
        lock (_lock)
        {
            if (_packetFrames > 0)
            {
                _endFrame = (long)Math.Max(0, endSequence) * _packetFrames;
            }
        }
    }

    public void TryStart()
    {
        if (_playTask is not null)
        {
            return;
        }

        _playTask = Task.Run(Play);
    }

    public HeadlessPlaybackDiagnostics Diagnostics
    {
        get
        {
            lock (_lock)
            {
                var min = _packets.Count == 0 ? 0 : _packets.Keys.Min();
                var max = _packets.Count == 0 ? 0 : _packets.Keys.Max();
                return new HeadlessPlaybackDiagnostics(
                    Interlocked.Read(ref _missingFrames),
                    Interlocked.Read(ref _readCalls),
                    _packets.Count,
                    _packets.Count == 0 ? 0 : max - min + 1,
                    _latestSequence);
            }
        }
    }

    private async Task Play()
    {
        var delay = DateTimeOffset.FromUnixTimeMilliseconds(_startUnixTimeMs) - DateTimeOffset.UtcNow;
        if (delay > TimeSpan.Zero)
        {
            await Task.Delay(delay).ConfigureAwait(false);
        }

        if (!_options.NoAudio)
        {
            StartSink();
        }

        var framesPerBuffer = Math.Max(1, _sampleRate * _options.BufferMs / 1000);
        var buffer = new byte[framesPerBuffer * _blockAlign];
        var started = Stopwatch.StartNew();
        var buffersWritten = 0L;

        while (true)
        {
            var frames = Read(buffer, framesPerBuffer);
            if (frames == 0)
            {
                break;
            }
            if (_sink?.StandardInput.BaseStream is { } output)
            {
                await output.WriteAsync(buffer.AsMemory(0, frames * _blockAlign)).ConfigureAwait(false);
            }

            buffersWritten += frames;
            var next = TimeSpan.FromSeconds(buffersWritten / (double)_sampleRate);
            var sleep = next - started.Elapsed;
            if (sleep > TimeSpan.Zero)
            {
                await Task.Delay(sleep).ConfigureAwait(false);
            }
        }
    }

    private void StartSink()
    {
        _sink = Process.Start(new ProcessStartInfo
        {
            FileName = "ffplay",
            Arguments =
                "-hide_banner -loglevel warning -nodisp -autoexit " +
                $"-f s16le -ar {_sampleRate} -ac {_channels} -i pipe:0",
            RedirectStandardInput = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        });
    }

    private int Read(byte[] buffer, int framesRequested)
    {
        Array.Clear(buffer);
        if (_packetFrames <= 0)
        {
            return framesRequested;
        }

        var firstFrame = SmoothFrame(ClockFrame());
        var endFrame = _endFrame;
        if (endFrame is not null && firstFrame >= endFrame.Value)
        {
            return 0;
        }
        if (endFrame is not null)
        {
            framesRequested = (int)Math.Min(framesRequested, Math.Max(0, endFrame.Value - firstFrame));
        }
        Interlocked.Increment(ref _readCalls);

        lock (_lock)
        {
            for (var frame = 0; frame < framesRequested; frame++)
            {
                var streamFrame = firstFrame + frame;
                if (streamFrame < 0)
                {
                    continue;
                }

                var packetSequence = (int)(streamFrame / _packetFrames);
                var packetFrame = (int)(streamFrame % _packetFrames);
                if (!_packets.TryGetValue(packetSequence, out var payload))
                {
                    Interlocked.Increment(ref _missingFrames);
                    continue;
                }

                var sourceOffset = packetFrame * _blockAlign;
                var targetOffset = frame * _blockAlign;
                if (sourceOffset + _blockAlign > payload.Length)
                {
                    Interlocked.Increment(ref _missingFrames);
                    continue;
                }

                Buffer.BlockCopy(payload, sourceOffset, buffer, targetOffset, _blockAlign);
            }

            Cleanup((int)Math.Max(0, firstFrame / _packetFrames));
            _nextFrame = firstFrame + framesRequested;
        }

        return framesRequested;
    }

    private long ClockFrame()
    {
        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        return (long)Math.Floor((nowMs + _options.BufferMs - _startUnixTimeMs) * _sampleRate / 1000.0);
    }

    private long SmoothFrame(long targetFrame)
    {
        if (_nextFrame is not { } nextFrame)
        {
            return targetFrame;
        }

        var toleranceFrames = _sampleRate * DriftToleranceMs / 1000;
        var drift = targetFrame - nextFrame;
        if (Math.Abs(drift) > toleranceFrames)
        {
            nextFrame += Math.Sign(drift) * Math.Min(Math.Abs(drift), DriftNudgeFrames);
        }

        return nextFrame;
    }

    private void Cleanup(int currentPacket)
    {
        if (currentPacket <= _lastCleanupPacket + CleanupSlackPackets)
        {
            return;
        }

        var cutoff = currentPacket - CleanupSlackPackets;
        foreach (var key in _packets.Keys.Where(key => key < cutoff).ToArray())
        {
            _packets.Remove(key);
        }
        _lastCleanupPacket = currentPacket;
    }

    public void Dispose()
    {
        if (_sink is { HasExited: false })
        {
            _sink.Kill(entireProcessTree: true);
        }
        _sink?.Dispose();
    }
}

internal static class VersionInfo
{
    public static string DisplayVersion => typeof(VersionInfo).Assembly.GetName().Version?.ToString(3) ?? "dev";
}
