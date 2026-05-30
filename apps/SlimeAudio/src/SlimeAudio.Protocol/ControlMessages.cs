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
}

public sealed record DiscoveryResponse(
    string App,
    string MachineName,
    string UserName,
    string Version,
    int Port,
    long UnixTimeMs,
    bool StreamMuted = false)
{
    public static DiscoveryResponse Current(int port, string version, bool streamMuted = false) => new(
        "slime-audio",
        Environment.MachineName,
        Environment.UserName,
        version,
        port,
        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        streamMuted);

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
