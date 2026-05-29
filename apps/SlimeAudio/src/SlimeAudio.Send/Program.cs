using System.Net;
using System.Net.Sockets;
using NAudio.Wave;
using SlimeAudio.Protocol;

var options = Options.Parse(args);
if (options is null)
{
    Options.PrintUsage();
    return 2;
}

var sessionId = Guid.NewGuid();
var startMs = DateTimeOffset.UtcNow.AddMilliseconds(options.DelayMs).ToUnixTimeMilliseconds();
var targets = options.Targets.Select(ParseEndpoint).ToArray();

using var reader = new WaveFileReader(options.File);
if (reader.WaveFormat.Encoding != WaveFormatEncoding.Pcm || reader.WaveFormat.BitsPerSample != 16)
{
    Console.Error.WriteLine("Only PCM 16-bit WAV files are supported in this first pass.");
    return 1;
}

using var udp = new UdpClient();
var chunkBytes = reader.WaveFormat.AverageBytesPerSecond / 20;
chunkBytes -= chunkBytes % reader.WaveFormat.BlockAlign;
chunkBytes = Math.Max(chunkBytes, reader.WaveFormat.BlockAlign);
var buffer = new byte[chunkBytes];
var sequence = 0;

Console.WriteLine($"session={sessionId:N} start={DateTimeOffset.FromUnixTimeMilliseconds(startMs):O} targets={targets.Length}");

int read;
while ((read = reader.Read(buffer, 0, buffer.Length)) > 0)
{
    var payload = buffer.AsSpan(0, read).ToArray();
    var packet = new AudioPacket(
        AudioPacketType.Audio,
        sessionId,
        sequence++,
        startMs,
        reader.WaveFormat.SampleRate,
        (short)reader.WaveFormat.Channels,
        (short)reader.WaveFormat.BitsPerSample,
        payload).Encode();

    foreach (var target in targets)
    {
        await udp.SendAsync(packet, packet.Length, target).ConfigureAwait(false);
    }

    await Task.Delay(options.PacketDelayMs).ConfigureAwait(false);
}

var end = new AudioPacket(AudioPacketType.End, sessionId, sequence, startMs, reader.WaveFormat.SampleRate, (short)reader.WaveFormat.Channels, 16, Array.Empty<byte>()).Encode();
foreach (var target in targets)
{
    await udp.SendAsync(end, end.Length, target).ConfigureAwait(false);
}

return 0;

static IPEndPoint ParseEndpoint(string value)
{
    var parts = value.Split(':', 2);
    if (parts.Length != 2 || !int.TryParse(parts[1], out var port))
    {
        throw new ArgumentException($"target must be host:port: {value}");
    }
    var addresses = Dns.GetHostAddresses(parts[0]);
    return new IPEndPoint(addresses.First(a => a.AddressFamily == AddressFamily.InterNetwork), port);
}

internal sealed record Options(string File, IReadOnlyList<string> Targets, int DelayMs, int PacketDelayMs)
{
    public static Options? Parse(string[] args)
    {
        string? file = null;
        var targets = new List<string>();
        var delayMs = 1500;
        var packetDelayMs = 50;

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--file" when i + 1 < args.Length:
                    file = args[++i];
                    break;
                case "--target" when i + 1 < args.Length:
                    targets.Add(args[++i]);
                    break;
                case "--delay-ms" when i + 1 < args.Length && int.TryParse(args[++i], out var delay):
                    delayMs = delay;
                    break;
                case "--packet-delay-ms" when i + 1 < args.Length && int.TryParse(args[++i], out var packetDelay):
                    packetDelayMs = packetDelay;
                    break;
            }
        }

        return file is null || targets.Count == 0 ? null : new Options(file, targets, delayMs, packetDelayMs);
    }

    public static void PrintUsage()
    {
        Console.Error.WriteLine("usage: SlimeAudio.Send --file bumper.wav --target SPATULA:47777 [--target SPONGEBOT:47777] [--delay-ms 1500]");
    }
}
