using NAudio.Wave;
using NAudio.Wave.SampleProviders;
using SlimeAudio.Protocol;

namespace SlimeAudio.Tray;

internal sealed class PlaybackSession : IDisposable
{
    private readonly BufferedWaveProvider _buffer;
    private readonly WaveOutEvent _output;
    private readonly EffectSampleProvider _effects;
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
        _effects = new EffectSampleProvider(_buffer.ToSampleProvider());
        _output = new WaveOutEvent { DesiredLatency = 100 };
        _output.Init(_effects);
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

    public void Apply(EffectEnvelope envelope)
    {
        _effects.Apply(envelope);
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
