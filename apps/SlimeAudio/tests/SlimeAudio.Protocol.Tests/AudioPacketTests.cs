using SlimeAudio.Protocol;
using Xunit;

namespace SlimeAudio.Protocol.Tests;

public sealed class AudioPacketTests
{
    [Fact]
    public void PacketRoundTrips()
    {
        var original = new AudioPacket(
            AudioPacketType.Audio,
            Guid.NewGuid(),
            12,
            1_780_035_600_000,
            48_000,
            2,
            16,
            [1, 2, 3, 4]);

        Assert.True(AudioPacket.TryDecode(original.Encode(), out var decoded));
        Assert.Equal(original.Type, decoded.Type);
        Assert.Equal(original.SessionId, decoded.SessionId);
        Assert.Equal(original.Sequence, decoded.Sequence);
        Assert.Equal(original.StartUnixTimeMs, decoded.StartUnixTimeMs);
        Assert.Equal(original.SampleRate, decoded.SampleRate);
        Assert.Equal(original.Channels, decoded.Channels);
        Assert.Equal(original.BitsPerSample, decoded.BitsPerSample);
        Assert.Equal(original.Payload, decoded.Payload);
    }

    [Fact]
    public void RejectsBadMagic()
    {
        Assert.False(AudioPacket.TryDecode([0, 1, 2, 3], out _));
    }

    [Fact]
    public void DiscoveryResponseRoundTrips()
    {
        var original = new DiscoveryResponse("slime-audio", "SPATULA", "slimeq", "0.3.0", 47777, 123, StreamMuted: true);

        var decoded = DiscoveryResponse.FromJson(original.ToJson());

        Assert.NotNull(decoded);
        Assert.Equal(original, decoded);
        Assert.True(decoded.StreamMuted);
    }

    [Fact]
    public void EffectEnvelopeRoundTripsAsControlMessage()
    {
        var original = new EffectEnvelope(1_780_035_600_000, 350, 1200, 500, 0.45f, 1400f);

        var decoded = EffectEnvelope.FromControlMessage(original.ToControlMessage());

        Assert.NotNull(decoded);
        Assert.Equal(original, decoded);
    }

    [Fact]
    public void ResetAudioControlMessageIsStable()
    {
        Assert.Equal("SLIME_AUDIO_RESET_AUDIO_V1", ControlMessages.ResetAudio);
    }
}
