using System.Buffers.Binary;

namespace SlimeAudio.Protocol;

public enum AudioPacketType : byte
{
    Audio = 1,
    End = 2,
}

public sealed record AudioPacket(
    AudioPacketType Type,
    Guid SessionId,
    int Sequence,
    long StartUnixTimeMs,
    int SampleRate,
    short Channels,
    short BitsPerSample,
    byte[] Payload)
{
    public const int HeaderSize = 43;
    private static readonly byte[] Magic = "SLA1"u8.ToArray();

    public byte[] Encode()
    {
        var buffer = new byte[HeaderSize + Payload.Length];
        Magic.CopyTo(buffer, 0);
        buffer[4] = (byte)Type;
        SessionId.TryWriteBytes(buffer.AsSpan(5, 16));
        BinaryPrimitives.WriteInt32LittleEndian(buffer.AsSpan(21, 4), Sequence);
        BinaryPrimitives.WriteInt64LittleEndian(buffer.AsSpan(25, 8), StartUnixTimeMs);
        BinaryPrimitives.WriteInt32LittleEndian(buffer.AsSpan(33, 4), SampleRate);
        BinaryPrimitives.WriteInt16LittleEndian(buffer.AsSpan(37, 2), Channels);
        BinaryPrimitives.WriteInt16LittleEndian(buffer.AsSpan(39, 2), BitsPerSample);
        BinaryPrimitives.WriteInt16LittleEndian(buffer.AsSpan(41, 2), (short)Payload.Length);
        Payload.CopyTo(buffer.AsSpan(HeaderSize));
        return buffer;
    }

    public static bool TryDecode(ReadOnlySpan<byte> data, out AudioPacket packet)
    {
        packet = default!;
        if (data.Length < HeaderSize || !data[..4].SequenceEqual(Magic))
        {
            return false;
        }

        var payloadLength = BinaryPrimitives.ReadInt16LittleEndian(data.Slice(41, 2));
        if (payloadLength < 0 || data.Length != HeaderSize + payloadLength)
        {
            return false;
        }

        var payload = data.Slice(HeaderSize, payloadLength).ToArray();
        packet = new AudioPacket(
            (AudioPacketType)data[4],
            new Guid(data.Slice(5, 16)),
            BinaryPrimitives.ReadInt32LittleEndian(data.Slice(21, 4)),
            BinaryPrimitives.ReadInt64LittleEndian(data.Slice(25, 8)),
            BinaryPrimitives.ReadInt32LittleEndian(data.Slice(33, 4)),
            BinaryPrimitives.ReadInt16LittleEndian(data.Slice(37, 2)),
            BinaryPrimitives.ReadInt16LittleEndian(data.Slice(39, 2)),
            payload);
        return true;
    }
}
