using System.Net;
using System.Net.Sockets;
using System.Text;
using NAudio.Wave;
using SlimeAudio.Protocol;

if (args.Length > 0 && args[0] == "discover")
{
    return await Discover(Options.ParseDiscover(args.Skip(1).ToArray()));
}

if (args.Length > 0 && args[0] == "update")
{
    var updateOptions = Options.ParseUpdate(args.Skip(1).ToArray());
    if (updateOptions is null)
    {
        Options.PrintUsage();
        return 2;
    }
    return await SendControl(updateOptions.Targets, ControlMessages.Update);
}

var sendArgs = args.Length > 0 && args[0] == "send" ? args.Skip(1).ToArray() : args;
var options = Options.Parse(sendArgs);
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

static async Task<int> Discover(DiscoverOptions options)
{
    using var udp = new UdpClient();
    udp.EnableBroadcast = true;
    udp.Client.ReceiveTimeout = options.TimeoutMs;
    var payload = Encoding.UTF8.GetBytes(ControlMessages.Discover);
    await udp.SendAsync(payload, payload.Length, new IPEndPoint(IPAddress.Broadcast, options.Port)).ConfigureAwait(false);

    var deadline = DateTimeOffset.UtcNow.AddMilliseconds(options.TimeoutMs);
    var found = 0;
    while (DateTimeOffset.UtcNow < deadline)
    {
        try
        {
            var result = await udp.ReceiveAsync().WaitAsync(TimeSpan.FromMilliseconds(500)).ConfigureAwait(false);
            var json = Encoding.UTF8.GetString(result.Buffer);
            var response = DiscoveryResponse.FromJson(json);
            if (response is null)
            {
                continue;
            }
            found++;
            Console.WriteLine($"{result.RemoteEndPoint.Address}:{response.Port}\t{response.MachineName}\t{response.UserName}\t{response.Version}");
        }
        catch (TimeoutException)
        {
            break;
        }
        catch (SocketException)
        {
            break;
        }
    }

    if (found == 0)
    {
        Console.Error.WriteLine("No Slime Audio receivers discovered.");
        return 1;
    }
    return 0;
}

static async Task<int> SendControl(IReadOnlyList<string> targets, string message)
{
    using var udp = new UdpClient();
    var payload = Encoding.UTF8.GetBytes(message);
    foreach (var target in targets.Select(ParseEndpoint))
    {
        await udp.SendAsync(payload, payload.Length, target).ConfigureAwait(false);
        Console.WriteLine($"sent {message} to {target}");
    }
    return 0;
}

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

internal sealed record DiscoverOptions(int Port, int TimeoutMs);

internal sealed record UpdateOptions(IReadOnlyList<string> Targets);

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

    public static DiscoverOptions ParseDiscover(string[] args)
    {
        var port = 47777;
        var timeoutMs = 2500;
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--port" when i + 1 < args.Length && int.TryParse(args[++i], out var parsedPort):
                    port = parsedPort;
                    break;
                case "--timeout-ms" when i + 1 < args.Length && int.TryParse(args[++i], out var parsedTimeout):
                    timeoutMs = parsedTimeout;
                    break;
            }
        }
        return new DiscoverOptions(port, timeoutMs);
    }

    public static UpdateOptions? ParseUpdate(string[] args)
    {
        var targets = new List<string>();
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i] == "--target" && i + 1 < args.Length)
            {
                targets.Add(args[++i]);
            }
        }
        return targets.Count == 0 ? null : new UpdateOptions(targets);
    }

    public static void PrintUsage()
    {
        Console.Error.WriteLine("usage:");
        Console.Error.WriteLine("  SlimeAudio.Send discover [--port 47777] [--timeout-ms 2500]");
        Console.Error.WriteLine("  SlimeAudio.Send update --target SPATULA:47777");
        Console.Error.WriteLine("  SlimeAudio.Send send --file bumper.wav --target SPATULA:47777 [--target SPONGEBOT:47777] [--delay-ms 1500]");
    }
}
