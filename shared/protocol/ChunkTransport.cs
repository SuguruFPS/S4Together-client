using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace S4Together.Transport;

/// <summary>Découpage JSON négocié ; un groupe reste contigu dans la queue de son émetteur.</summary>
public static class ChunkTransport
{
    public const int Threshold = 49152, BlockBytes = 32768, MaxBytes = 16 * 1024 * 1024;
    public static IEnumerable<byte[]> Encode(byte[] message, bool enabled)
    {
        if (message.Length > MaxBytes) throw new InvalidDataException("Message logique trop grand.");
        if (!enabled || message.Length <= Threshold) { yield return message; yield break; }
        var id = Guid.NewGuid().ToString(); var hash = Convert.ToHexStringLower(SHA256.HashData(message));
        var total = (message.Length + BlockBytes - 1) / BlockBytes;
        for (var index = 0; index < total; index++)
        {
            var offset = index * BlockBytes;
            using var buffer=new MemoryStream();
            using(var writer=new Utf8JsonWriter(buffer))
            {
                writer.WriteStartObject();writer.WriteNumber("v",1);writer.WriteString("type","transport.chunk");writer.WriteStartObject("payload");
                writer.WriteString("transferId",id);writer.WriteNumber("index",index);writer.WriteNumber("total",total);writer.WriteNumber("size",message.Length);writer.WriteString("sha256",hash);
                // Écriture Base64 dédiée : les '+' ne sont pas développés en \u002B,
                // ce qui pourrait sinon dépasser64Kio avec certaines données.
                writer.WriteBase64String("data",message.AsSpan(offset,Math.Min(BlockBytes,message.Length-offset)));
                writer.WriteEndObject();writer.WriteEndObject();writer.Flush();
            }
            yield return buffer.ToArray();
        }
    }
}

/// <summary>Une instance par message logique, rejet fermé des transferts partiels ou incohérents.</summary>
public sealed class ChunkAssembler : IDisposable
{
    readonly Func<long> clock;
    MemoryStream? bytes;
    string? id, hash;
    int next, total, size;
    long started;
    public bool Active => bytes is not null;
    public ChunkAssembler(Func<long>? clock = null) => this.clock = clock ?? (() => Environment.TickCount64);
    static void Require(bool valid) { if (!valid) throw new InvalidDataException("Transmission découpée invalide."); }
    static bool Fields(JsonElement value, params string[] names) => value.ValueKind == JsonValueKind.Object
        && value.EnumerateObject().Select(x => x.Name).Order().SequenceEqual(names.Order());
    public string? Accept(string frame)
    {
        using var parsed = JsonDocument.Parse(frame, new JsonDocumentOptions { MaxDepth = 64 });
        var root = parsed.RootElement;
        var chunk = root.ValueKind == JsonValueKind.Object && root.TryGetProperty("type", out var type)
            && type.ValueKind == JsonValueKind.String && type.GetString() == "transport.chunk";
        if (!chunk) { Require(!Active); return frame; }
        Require(Encoding.UTF8.GetByteCount(frame) <= 65536 && Fields(root, "v", "type", "payload") && root.GetProperty("v").GetInt32() == 1);
        var p = root.GetProperty("payload");
        Require(Fields(p, "transferId", "index", "total", "size", "sha256", "data"));
        var transfer = p.GetProperty("transferId").GetString(); var checksum = p.GetProperty("sha256").GetString();
        var index = p.GetProperty("index").GetInt32(); var count = p.GetProperty("total").GetInt32();
        var length = p.GetProperty("size").GetInt32(); var data = p.GetProperty("data").GetString();
        Require(Guid.TryParseExact(transfer, "D", out var uuid) && uuid.ToString() == transfer
            && checksum is { Length: 64 } && checksum.All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f')
            && length is > ChunkTransport.Threshold and <= ChunkTransport.MaxBytes
            && count == (length + ChunkTransport.BlockBytes - 1) / ChunkTransport.BlockBytes
            && index >= 0 && index < count && data is { Length: > 0 and <= 43692 });
        var decoded = Convert.FromBase64String(data!);
        var expected = Math.Min(ChunkTransport.BlockBytes, length - index * ChunkTransport.BlockBytes);
        Require(decoded.Length == expected && Convert.ToBase64String(decoded) == data);
        if (!Active)
        {
            Require(index == 0); id = transfer; hash = checksum; total = count; size = length;
            started = clock(); bytes = new MemoryStream(length);
        }
        Require(clock() - started <= 30000 && transfer == id && checksum == hash && count == total && length == size && index == next);
        bytes!.Write(decoded); next++;
        if (next < total) return null;
        Require(bytes.Length == size);
        var assembled = bytes.ToArray();
        Require(Convert.ToHexStringLower(SHA256.HashData(assembled)) == hash);
        var message = new UTF8Encoding(false, true).GetString(assembled);
        using var original = JsonDocument.Parse(message, new JsonDocumentOptions { MaxDepth = 64 });
        Require(original.RootElement.ValueKind == JsonValueKind.Object && original.RootElement.GetProperty("type").GetString() != "transport.chunk");
        bytes.Dispose(); bytes = null; next = 0;
        return message;
    }
    public void Dispose() { bytes?.Dispose(); bytes = null; }
}
