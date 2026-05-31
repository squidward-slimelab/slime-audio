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
        var diagnostics = new AudioDiagnostics(
            ActiveSessions: 1,
            ReceivedPackets: 42,
            ReceivedBytes: 1234,
            DroppedMutedPackets: 2,
            DecodeFailures: 3,
            ResetCount: 4,
            MissingFrames: 5,
            ReadCalls: 6,
            LastPacketUnixTimeMs: 456,
            MaxBufferedPackets: 10,
            MaxBufferedPacketSpan: 12,
            LatestSequence: 99,
            LatestSessionId: "abc",
            SharedStreamListening: true,
            SharedStreamExitCode: 12,
            SharedStreamStatus: "snapclient warning",
            SharedStreamServerHost: "192.168.0.122",
            SharedStreamProcessId: 1234,
            SharedStreamStartedUnixTimeMs: 1000,
            SharedStreamLastExitUnixTimeMs: 2000,
            SharedStreamExitCount: 2,
            SharedStreamLastStderrUnixTimeMs: 1500,
            SharedStreamTelemetryPath: @"C:\Users\slimeq\AppData\Local\SlimeAudio\telemetry.jsonl",
            SharedStreamOutputDevice: "Speakers",
            SharedStreamOutputDevices: ["Headphones", "Speakers"]);
        var original = new DiscoveryResponse("slime-audio", "SPATULA", "slimeq", "0.3.0", 47777, 123, StreamMuted: true, Diagnostics: diagnostics);

        var decoded = DiscoveryResponse.FromJson(original.ToJson());

        Assert.NotNull(decoded);
        Assert.Equal(original.App, decoded.App);
        Assert.Equal(original.MachineName, decoded.MachineName);
        Assert.Equal(original.UserName, decoded.UserName);
        Assert.Equal(original.Version, decoded.Version);
        Assert.Equal(original.Port, decoded.Port);
        Assert.Equal(original.UnixTimeMs, decoded.UnixTimeMs);
        Assert.True(decoded.StreamMuted);
        Assert.Equal(42, decoded.Diagnostics?.ReceivedPackets);
        Assert.Equal(5, decoded.Diagnostics?.MissingFrames);
        Assert.True(decoded.Diagnostics?.SharedStreamListening);
        Assert.Equal(2, decoded.Diagnostics?.SharedStreamExitCount);
        Assert.Equal("192.168.0.122", decoded.Diagnostics?.SharedStreamServerHost);
        Assert.EndsWith("telemetry.jsonl", decoded.Diagnostics?.SharedStreamTelemetryPath);
        Assert.Equal("Speakers", decoded.Diagnostics?.SharedStreamOutputDevice);
        Assert.Contains("Headphones", decoded.Diagnostics?.SharedStreamOutputDevices ?? []);
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

    [Fact]
    public void OutputDeviceSelectionRoundTripsAsControlMessage()
    {
        var original = new OutputDeviceSelection("Speakers");

        var decoded = OutputDeviceSelection.FromControlMessage(original.ToControlMessage());

        Assert.NotNull(decoded);
        Assert.Equal(original, decoded);
        Assert.Equal("SLIME_AUDIO_OUTPUT_DEVICE_V1 ", ControlMessages.OutputDevicePrefix);
    }
}
