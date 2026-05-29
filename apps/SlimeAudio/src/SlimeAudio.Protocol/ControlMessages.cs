using System.Text.Json;

namespace SlimeAudio.Protocol;

public static class ControlMessages
{
    public const string Discover = "SLIME_AUDIO_DISCOVER_V1";
    public const string Update = "SLIME_AUDIO_UPDATE_V1";
    public const string SharedStreamStart = "SLIME_AUDIO_SHARED_STREAM_START_V1";
    public const string SharedStreamStop = "SLIME_AUDIO_SHARED_STREAM_STOP_V1";
}

public sealed record DiscoveryResponse(
    string App,
    string MachineName,
    string UserName,
    string Version,
    int Port,
    long UnixTimeMs)
{
    public static DiscoveryResponse Current(int port, string version) => new(
        "slime-audio",
        Environment.MachineName,
        Environment.UserName,
        version,
        port,
        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());

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
