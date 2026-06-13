using System.Text.Json;

namespace SlimeAudio.Protocol;

public static class ControlMessages
{
    public const string Discover = "SLIME_AUDIO_DISCOVER_V1";
    public const string Update = "SLIME_AUDIO_UPDATE_V1";
    public const string SharedStreamStart = "SLIME_AUDIO_SHARED_STREAM_START_V1";
    public const string SharedStreamStop = "SLIME_AUDIO_SHARED_STREAM_STOP_V1";
    public const string ResetAudio = "SLIME_AUDIO_RESET_AUDIO_V1";
    public const string EffectPrefix = "SLIME_AUDIO_EFFECT_V1 ";
    public const string OutputDevicePrefix = "SLIME_AUDIO_OUTPUT_DEVICE_V1 ";
}

public sealed record DiscoveryResponse(
    string App,
    string MachineName,
    string UserName,
    string Version,
    int Port,
    long UnixTimeMs,
    bool StreamMuted = false,
    AudioDiagnostics? Diagnostics = null)
{
    public static DiscoveryResponse Current(int port, string version, bool streamMuted = false, AudioDiagnostics? diagnostics = null) => new(
        "slime-audio",
        Environment.MachineName,
        Environment.UserName,
        version,
        port,
        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        streamMuted,
        diagnostics);

    public string ToJson() => JsonSerializer.Serialize(this);

    public static DiscoveryResponse? FromJson(string json)
    {
        try
        {
            return JsonSerializer.Deserialize<DiscoveryResponse>(json);
        }
        catch (JsonException)
        {
            return null;
        }
    }
}

public sealed record AudioDiagnostics(
    int ActiveSessions,
    long ReceivedPackets,
    long ReceivedBytes,
    long DroppedMutedPackets,
    long DecodeFailures,
    long ResetCount,
    long MissingFrames,
    long ReadCalls,
    long LastPacketUnixTimeMs,
    int MaxBufferedPackets,
    int MaxBufferedPacketSpan,
    int LatestSequence,
    string? LatestSessionId,
    bool SharedStreamListening = false,
    int? SharedStreamExitCode = null,
    string? SharedStreamStatus = null,
    string? SharedStreamServerHost = null,
    int? SharedStreamProcessId = null,
    long SharedStreamStartedUnixTimeMs = 0,
    long SharedStreamLastExitUnixTimeMs = 0,
    int SharedStreamExitCount = 0,
    long SharedStreamLastStderrUnixTimeMs = 0,
    string? SharedStreamTelemetryPath = null,
    string? SharedStreamOutputDevice = null,
    string[]? SharedStreamOutputDevices = null,
    string? SharedStreamLastExitStatus = null,
    string? SharedStreamLastStderr = null,
    string? SharedStreamStartCommand = null,
    long SharedStreamUptimeMs = 0,
    int SharedStreamReconnectAttempts = 0,
    bool SharedStreamSnapserverOk = false,
    string? SharedStreamSnapserverError = null,
    bool SharedStreamSnapserverClientConnected = false,
    string? SharedStreamSnapserverClientStream = null,
    string? SharedStreamSnapserverStreamStatus = null);

public sealed record EffectEnvelope(
    long StartUnixTimeMs,
    int FadeInMs,
    int HoldMs,
    int FadeOutMs,
    float Volume,
    float LowPassHz)
{
    public string ToControlMessage() => ControlMessages.EffectPrefix + JsonSerializer.Serialize(this);

    public static EffectEnvelope? FromControlMessage(string message)
    {
        if (!message.StartsWith(ControlMessages.EffectPrefix, StringComparison.Ordinal))
        {
            return null;
        }

        try
        {
            return JsonSerializer.Deserialize<EffectEnvelope>(message[ControlMessages.EffectPrefix.Length..]);
        }
        catch (JsonException)
        {
            return null;
        }
    }
}

public sealed record OutputDeviceSelection(string? Soundcard)
{
    public string ToControlMessage() => ControlMessages.OutputDevicePrefix + JsonSerializer.Serialize(this);

    public static OutputDeviceSelection? FromControlMessage(string message)
    {
        if (!message.StartsWith(ControlMessages.OutputDevicePrefix, StringComparison.Ordinal))
        {
            return null;
        }

        try
        {
            return JsonSerializer.Deserialize<OutputDeviceSelection>(message[ControlMessages.OutputDevicePrefix.Length..]);
        }
        catch (JsonException)
        {
            return null;
        }
    }
}
