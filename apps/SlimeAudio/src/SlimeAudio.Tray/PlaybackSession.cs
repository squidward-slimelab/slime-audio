using NAudio.Wave;
using SlimeAudio.Protocol;

namespace SlimeAudio.Tray;

internal sealed class PlaybackSession : IDisposable
{
    private readonly BufferedWaveProvider _buffer;
    private readonly WaveOutEvent _output;
    private readonly long _startUnixTimeMs;
    private bool _started;

    public PlaybackSession(AudioPacket firstPacket)
    {
        _startUnixTimeMs = firstPacket.StartUnixTimeMs;
        var format = new WaveFormat(firstPacket.SampleRate, firstPacket.BitsPerSample, firstPacket.Channels);
        _buffer = new BufferedWaveProvider(format)
        {
            BufferDuration = TimeSpan.FromSeconds(30),
            DiscardOnBufferOverflow = true,
        };
        _output = new WaveOutEvent { DesiredLatency = 100 };
        _output.Init(_buffer);
    }

    public void Add(AudioPacket packet)
    {
        if (packet.Payload.Length > 0)
        {
            _buffer.AddSamples(packet.Payload, 0, packet.Payload.Length);
        }
    }

    public bool TryStart()
    {
        if (_started)
        {
            return false;
        }

        var delay = DateTimeOffset.FromUnixTimeMilliseconds(_startUnixTimeMs) - DateTimeOffset.UtcNow;
        if (delay > TimeSpan.Zero)
        {
            _ = Task.Run(async () =>
            {
                await Task.Delay(delay).ConfigureAwait(false);
                Start();
            });
        }
        else
        {
            Start();
        }
        _started = true;
        return true;
    }

    public void MarkEnded()
    {
        // BufferedWaveProvider drains naturally. A later pass can prune completed sessions.
    }

    private void Start()
    {
        if (_output.PlaybackState != PlaybackState.Playing)
        {
            _output.Play();
        }
    }

    public void Dispose()
    {
        _output.Dispose();
    }
}
