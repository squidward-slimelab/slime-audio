using NAudio.Wave;
using NAudio.Wave.SampleProviders;
using SlimeAudio.Protocol;

namespace SlimeAudio.Tray;

internal sealed class PlaybackSession : IDisposable
{
    private readonly WaveOutEvent _output;
    private readonly ClockedPacketSampleProvider _clockedSource;
    private readonly EffectSampleProvider _effects;
    private readonly long _startUnixTimeMs;
    private bool _started;

    public PlaybackSession(AudioPacket firstPacket)
    {
        _startUnixTimeMs = firstPacket.StartUnixTimeMs;
        var format = new WaveFormat(firstPacket.SampleRate, firstPacket.BitsPerSample, firstPacket.Channels);
        _clockedSource = new ClockedPacketSampleProvider(format, _startUnixTimeMs);
        _effects = new EffectSampleProvider(_clockedSource);
        _output = new WaveOutEvent { DesiredLatency = 100 };
        _output.Init(_effects);
    }

    public void Add(AudioPacket packet)
    {
        if (packet.Payload.Length > 0)
        {
            _clockedSource.Add(packet);
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

    public void MarkEnded(AudioPacket packet)
    {
        _clockedSource.MarkEnded(packet.Sequence);
    }

    public void Apply(EffectEnvelope envelope)
    {
        _effects.Apply(envelope);
    }

    public PlaybackSessionDiagnostics Diagnostics => _clockedSource.Diagnostics;

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

internal sealed record PlaybackSessionDiagnostics(
    long MissingFrames,
    long ReadCalls,
    int BufferedPackets,
    int BufferedPacketSpan,
    int LatestSequence,
    long CurrentFrame);

internal sealed class ClockedPacketSampleProvider : ISampleProvider
{
    private const int InitialPlaybackLatencyCompensationMs = 100;
    private const int CleanupSlackPackets = 200;
    private const int DriftNudgeFrames = 2;
    private const int DriftToleranceMs = 15;
    private readonly object _lock = new();
    private readonly Dictionary<int, byte[]> _packets = new();
    private readonly WaveFormat _sourceFormat;
    private readonly int _blockAlign;
    private readonly long _startUnixTimeMs;
    private int _packetFrames;
    private int _lastCleanupPacket = -1;
    private long? _nextFrame;
    private long _missingFrames;
    private long _readCalls;
    private int _latestSequence = -1;
    private long _lastCurrentFrame;
    private long? _endFrame;

    public WaveFormat WaveFormat { get; }

    public ClockedPacketSampleProvider(WaveFormat sourceFormat, long startUnixTimeMs)
    {
        _sourceFormat = sourceFormat;
        _blockAlign = Math.Max(1, sourceFormat.BlockAlign);
        _startUnixTimeMs = startUnixTimeMs;
        WaveFormat = WaveFormat.CreateIeeeFloatWaveFormat(sourceFormat.SampleRate, sourceFormat.Channels);
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

    public int Read(float[] buffer, int offset, int count)
    {
        Array.Clear(buffer, offset, count);
        if (_packetFrames <= 0)
        {
            return count;
        }

        var channels = Math.Max(1, _sourceFormat.Channels);
        var framesRequested = count / channels;
        var targetFrame = ClockFrame();
        var firstFrame = SmoothFrame(targetFrame);
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
        Interlocked.Exchange(ref _lastCurrentFrame, firstFrame);

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

                var byteIndex = packetFrame * _blockAlign;
                if (byteIndex + _blockAlign > payload.Length)
                {
                    Interlocked.Increment(ref _missingFrames);
                    continue;
                }

                for (var channel = 0; channel < channels; channel++)
                {
                    var sampleIndex = byteIndex + (channel * 2);
                    if (sampleIndex + 1 >= payload.Length)
                    {
                        break;
                    }

                    var sample = BitConverter.ToInt16(payload, sampleIndex);
                    buffer[offset + (frame * channels) + channel] = sample / 32768f;
                }
            }

            Cleanup((int)Math.Max(0, firstFrame / _packetFrames));
            _nextFrame = firstFrame + framesRequested;
        }

        return count;
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

    public PlaybackSessionDiagnostics Diagnostics
    {
        get
        {
            lock (_lock)
            {
                var min = _packets.Count == 0 ? 0 : _packets.Keys.Min();
                var max = _packets.Count == 0 ? 0 : _packets.Keys.Max();
                return new PlaybackSessionDiagnostics(
                    Interlocked.Read(ref _missingFrames),
                    Interlocked.Read(ref _readCalls),
                    _packets.Count,
                    _packets.Count == 0 ? 0 : max - min + 1,
                    _latestSequence,
                    Interlocked.Read(ref _lastCurrentFrame));
            }
        }
    }

    private long ClockFrame()
    {
        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        return (long)Math.Floor(
            (nowMs + InitialPlaybackLatencyCompensationMs - _startUnixTimeMs) * _sourceFormat.SampleRate / 1000.0);
    }

    private long SmoothFrame(long targetFrame)
    {
        if (_nextFrame is not { } nextFrame)
        {
            return targetFrame;
        }

        var toleranceFrames = _sourceFormat.SampleRate * DriftToleranceMs / 1000;
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
}

internal sealed class EffectSampleProvider : ISampleProvider
{
    private readonly ISampleProvider _source;
    private readonly float[] _lowPassState;
    private EffectEnvelope? _effect;

    public WaveFormat WaveFormat => _source.WaveFormat;

    public EffectSampleProvider(ISampleProvider source)
    {
        _source = source;
        _lowPassState = new float[Math.Max(1, source.WaveFormat.Channels)];
    }

    public void Apply(EffectEnvelope envelope)
    {
        _effect = envelope;
    }

    public int Read(float[] buffer, int offset, int count)
    {
        var read = _source.Read(buffer, offset, count);
        var effect = _effect;
        if (effect is null || read == 0)
        {
            return read;
        }

        var channels = Math.Max(1, WaveFormat.Channels);
        var sampleRate = Math.Max(1, WaveFormat.SampleRate);
        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var nyquist = sampleRate / 2f;

        for (var i = 0; i < read; i++)
        {
            var frame = i / channels;
            var sampleTimeMs = nowMs + (long)(frame * 1000.0 / sampleRate);
            var amount = AmountAt(effect, sampleTimeMs);
            if (amount <= 0)
            {
                continue;
            }

            var channel = i % channels;
            var volume = 1f + ((effect.Volume - 1f) * amount);
            var cutoff = nyquist + ((Math.Clamp(effect.LowPassHz, 80f, nyquist) - nyquist) * amount);
            var sample = buffer[offset + i] * volume;

            if (cutoff < nyquist * 0.98f)
            {
                sample = LowPass(sample, channel, cutoff, sampleRate);
            }

            buffer[offset + i] = sample;
        }

        return read;
    }

    private float LowPass(float input, int channel, float cutoff, int sampleRate)
    {
        var rc = 1.0 / (2.0 * Math.PI * cutoff);
        var dt = 1.0 / sampleRate;
        var alpha = (float)(dt / (rc + dt));
        _lowPassState[channel] += alpha * (input - _lowPassState[channel]);
        return _lowPassState[channel];
    }

    private static float AmountAt(EffectEnvelope effect, long nowMs)
    {
        var elapsed = nowMs - effect.StartUnixTimeMs;
        if (elapsed < 0)
        {
            return 0f;
        }

        if (elapsed < effect.FadeInMs)
        {
            return effect.FadeInMs <= 0 ? 1f : (float)elapsed / effect.FadeInMs;
        }

        elapsed -= effect.FadeInMs;
        if (elapsed < effect.HoldMs)
        {
            return 1f;
        }

        elapsed -= effect.HoldMs;
        if (elapsed < effect.FadeOutMs)
        {
            return effect.FadeOutMs <= 0 ? 0f : 1f - ((float)elapsed / effect.FadeOutMs);
        }

        return 0f;
    }
}
