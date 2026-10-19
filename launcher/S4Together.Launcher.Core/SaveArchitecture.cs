using System.Buffers.Binary;
using System.IO.Compression;
using System.Security.Cryptography;

namespace S4Together.Launcher.Core;

/// <summary>Architecture native (murs, sols, toits) d'un terrain, identifiée par son lot_description.</summary>
public sealed record LotArchitecture(string LotDescriptionId, byte[] Data, string Sha)
{
    public static LotArchitecture Create(string lotDescriptionId, byte[] data) =>
        new(lotDescriptionId, data, SaveArchitecture.Hash(data));
}

/// <summary>Sortie null : aucune ressource modifiée (rien à appliquer).</summary>
public sealed record ArchitectureApplyResult(byte[]? Output, IReadOnlyList<string> Applied, IReadOnlyList<string> Skipped);

/// <summary>
/// Port C# de tools/native_zone_archive.py et prepare_native_house_trial.py.
/// Extrait l'architecture du domicile joué ; l'applique aux terrains NON joués d'une autre sauvegarde.
/// Meubles, Sims, foyers et inventaires ne sont jamais copiés. Toute ambiguïté refuse.
/// </summary>
public static class SaveArchitecture
{
    const long MaxFile = 512L * 1024 * 1024;
    const int MaxResource = 64 * 1024 * 1024;
    const int MaxFields = 100_000;
    /// <summary>Taille brute maximale acceptée pour un bloc d'architecture.</summary>
    public const int MaxArchitectureBytes = 4 * 1024 * 1024;
    const uint ArchitectureMagic = 0x424e444c; // octets « LDNB »
    const uint ArchitectureVersion = 3;

    public static string Hash(ReadOnlySpan<byte> data) => Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();

    public static void ValidateBlock(ReadOnlySpan<byte> data)
    {
        if (data.Length < 16 || data.Length > MaxArchitectureBytes ||
            BinaryPrimitives.ReadUInt32LittleEndian(data) != ArchitectureMagic ||
            BinaryPrimitives.ReadUInt32LittleEndian(data[4..]) != ArchitectureVersion)
            throw new InvalidDataException("En-tête architecture non supporté.");
    }

    // ---------- Extraction (propriétaire) ----------

    public static LotArchitecture ExtractHome(byte[] save, ulong householdId)
    {
        if (householdId == 0) throw new InvalidDataException("Foyer invalide.");
        var entries = ReadIndex(save);
        var root = Message(ReadResource(save, SingleRoot(entries)));
        var households = Repeated(root, 5).Where(h => OneU(h, 2, 1) == householdId).ToList();
        if (households.Count != 1) throw new InvalidDataException("Foyer non unique.");
        var hh = households[0];
        if (OneU(hh, 31, 0) != 1 || OneU(hh, 14, 0) != 0) throw new InvalidDataException("Foyer non joué.");
        var home = OneU(hh, 4, 1) ?? 0;
        if (home == 0) throw new InvalidDataException("Foyer sans domicile.");
        var zones = Repeated(root, 7).Where(z => OneU(z, 1, 1) == home).ToList();
        if (zones.Count != 1 || OneU(zones[0], 6, 1) != householdId) throw new InvalidDataException("Propriétaire du domicile incohérent.");
        var zone = zones[0];
        var plex = OneU(zone, 19, 1) ?? 0;
        if ((plex != 0 && plex != home) || (OneU(zone, 18, 0) ?? 0) != 0)
            throw new InvalidDataException("Zone partagée non supportée.");
        var lotDescription = OneU(zone, 12, 1) ?? 0;
        if (lotDescription == 0) throw new InvalidDataException("lot_description absent.");
        var matches = entries.Where(e => e.Type == 6 && e.Group == 0 && e.Instance == home).ToList();
        if (matches.Count != 1) throw new InvalidDataException("Zone native non unique.");
        var raw = ReadResource(save, matches[0]);
        var fields = Fields(raw);
        InspectZone(fields, home, out var activeIndex);
        var blocks = fields.Where(f => f.Number == 2 && f.Wire == 2).ToList();
        if (blocks.Count != 1 || activeIndex != 0 || fields.Any(f => f.Number == 5))
            throw new InvalidDataException("Variantes de zone non supportées.");
        var architecture = raw.AsSpan(blocks[0].ValueStart, blocks[0].ValueLength).ToArray();
        ValidateBlock(architecture);
        return LotArchitecture.Create(lotDescription.ToString(System.Globalization.CultureInfo.InvariantCulture), architecture);
    }

    static void InspectZone(List<Field> fields, ulong expectedZone, out ulong activeIndex)
    {
        var zoneIds = new List<ulong>(); var active = new List<ulong>(); int architecture = 0, objects = 0;
        var architectureNonEmpty = new List<bool>();
        foreach (var f in fields)
        {
            if (f.Number is < 1 or > 4) continue;
            int expectedWire = f.Number switch { 1 => 1, 4 => 0, _ => 2 };
            if (f.Wire != expectedWire) throw new InvalidDataException("Fil de zone invalide.");
            switch (f.Number)
            {
                case 1: zoneIds.Add(f.Value); break;
                case 2: architecture++; architectureNonEmpty.Add(f.ValueLength > 0); break;
                case 3: objects++; break;
                case 4: active.Add(f.Value); break;
            }
        }
        if (zoneIds.Count != 1 || zoneIds[0] != expectedZone || expectedZone == 0 || active.Count > 1)
            throw new InvalidDataException("Identité de zone incohérente.");
        activeIndex = active.Count == 1 ? active[0] : 0;
        if (architecture == 0 || architecture != objects || activeIndex >= (ulong)architecture || !architectureNonEmpty[(int)activeIndex])
            throw new InvalidDataException("Données de zone incomplètes.");
    }

    // ---------- Application (spectateur) ----------

    /// <summary>
    /// Applique chaque architecture à la zone de même lot_description, seulement si elle n'est
    /// le domicile d'aucun foyer joué ou non-PNJ. Les autres octets restent identiques.
    /// </summary>
    public static ArchitectureApplyResult Apply(byte[] save, IReadOnlyList<LotArchitecture> lots)
    {
        var entries = ReadIndex(save);
        var root = Message(ReadResource(save, SingleRoot(entries)));
        var zones = Repeated(root, 7);
        var households = Repeated(root, 5);
        var replacements = new Dictionary<Key, byte[]>();
        var originals = new Dictionary<Key, List<Field>>();
        var applied = new List<string>(); var skipped = new List<string>();
        foreach (var lot in lots)
        {
            var reason = Prepare(save, entries, zones, households, lot, out var key, out var rewritten, out var nativeFields);
            if (reason is not null) { skipped.Add(lot.LotDescriptionId + ":" + reason); continue; }
            if (replacements.ContainsKey(key)) { skipped.Add(lot.LotDescriptionId + ":duplicate"); continue; }
            replacements[key] = rewritten!; originals[key] = nativeFields!;
            applied.Add(lot.LotDescriptionId);
        }
        if (replacements.Count == 0) return new(null, applied, skipped);
        var output = Repack(save, entries, replacements);
        VerifyPostconditions(save, entries, output, replacements, originals);
        return new(output, applied, skipped);
    }

    static string? Prepare(byte[] save, List<Entry> entries, List<Msg> zones, List<Msg> households, LotArchitecture lot, out Key key, out byte[]? rewritten, out List<Field>? nativeFields)
    {
        key = default; rewritten = null; nativeFields = null;
        try { ValidateBlock(lot.Data); } catch (InvalidDataException) { return "invalid_block"; }
        if (Hash(lot.Data) != lot.Sha) return "sha_mismatch";
        if (!ulong.TryParse(lot.LotDescriptionId, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out var description) || description == 0)
            return "invalid_lot";
        var matching = zones.Where(z => OneU(z, 12, 1) == description).ToList();
        if (matching.Count != 1) return matching.Count == 0 ? "lot_absent" : "lot_not_unique";
        var meta = matching[0];
        var zone = OneU(meta, 1, 1) ?? 0;
        var plex = OneU(meta, 19, 1) ?? 0;
        if (zone == 0 || (OneU(meta, 18, 0) ?? 0) != 0 || (plex != 0 && plex != zone)) return "plex_not_supported";
        if (households.Any(h => OneU(h, 4, 1) == zone && (OneU(h, 31, 0) == 1 || OneU(h, 14, 0) != 1))) return "played_home";
        key = new Key(6, 0, zone);
        var k = key;
        var resources = entries.Where(e => e.Key == k).ToList();
        if (resources.Count != 1) return "zone_resource_missing";
        var native = ReadResource(save, resources[0]);
        var fields = Fields(native);
        var message = Group(fields);
        if (OneU(message, 1, 1) != zone || (OneU(message, 4, 0) ?? 0) != 0 || Count(message, 2) > 1 || Count(message, 5) != 0)
            return "zone_variants_not_supported";
        if (Count(message, 3) != 1 || message[3][0].Wire != 2) return "objects_not_unique";
        _ = Fields(native.AsSpan(message[3][0].ValueStart, message[3][0].ValueLength).ToArray());
        if (Count(message, 2) == 1 && message[2][0].Wire == 2 &&
            native.AsSpan(message[2][0].ValueStart, message[2][0].ValueLength).SequenceEqual(lot.Data))
            return "unchanged";
        rewritten = Rewrite(native, fields, new()
        {
            [2] = Encode(2, 2, 0, lot.Data),
            [4] = Encode(4, 0, 0, null),
            [6] = Encode(6, 0, 1, null),
        });
        nativeFields = fields;
        return null;
    }

    static void VerifyPostconditions(byte[] before, List<Entry> entries, byte[] output,
        Dictionary<Key, byte[]> replacements, Dictionary<Key, List<Field>> originals)
    {
        var after = ReadIndex(output).ToDictionary(e => e.Key);
        if (after.Count != entries.Count) throw new InvalidDataException("Postcondition : nombre de ressources changé.");
        foreach (var entry in entries)
        {
            var other = after[entry.Key];
            if (replacements.TryGetValue(entry.Key, out var expected))
            {
                var reread = ReadResource(output, other);
                if (!reread.AsSpan().SequenceEqual(expected)) throw new InvalidDataException("Postcondition : zone relue différente.");
                var kept = Untouched(originals[entry.Key], ReadResource(before, entry));
                var keptAfter = Untouched(Fields(reread), reread);
                if (!kept.SequenceEqual(keptAfter)) throw new InvalidDataException("Postcondition : objets de zone modifiés.");
                continue;
            }
            if (entry.CompressionFlags != other.CompressionFlags || entry.Unpacked != other.Unpacked ||
                !before.AsSpan(entry.Offset, entry.Packed).SequenceEqual(output.AsSpan(other.Offset, other.Packed)))
                throw new InvalidDataException("Postcondition : ressource sans rapport modifiée.");
        }

        static List<string> Untouched(List<Field> fields, byte[] source) => fields
            .Where(f => f.Number is not (2 or 4 or 6))
            .Select(f => Convert.ToHexString(source.AsSpan(f.Start, f.End - f.Start)))
            .ToList();
    }

    // ---------- DBPF ----------

    internal readonly record struct Key(uint Type, uint Group, ulong Instance);

    internal sealed record Entry(uint Type, uint Group, ulong Instance, int Offset, int Packed, int Unpacked, uint CompressionFlags)
    {
        public Key Key => new(Type, Group, Instance);
        public uint Compression => CompressionFlags & 0xffff;
    }

    static Entry SingleRoot(List<Entry> entries)
    {
        var roots = entries.Where(e => e.Type == 13).ToList();
        if (roots.Count != 1) throw new InvalidDataException("Métadonnées de sauvegarde ambiguës.");
        return roots[0];
    }

    static uint U32(byte[] data, int offset) => BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(offset, 4));

    internal static List<Entry> ReadIndex(byte[] data)
    {
        if (data.Length < 96 || data.LongLength > MaxFile || !data.AsSpan(0, 4).SequenceEqual("DBPF"u8))
            throw new InvalidDataException("DBPF invalide.");
        if (U32(data, 4) != 2 || U32(data, 8) > 1) throw new InvalidDataException("Version DBPF non supportée.");
        uint count = U32(data, 36), size = U32(data, 44);
        ulong position = U32(data, 40);
        if (position == 0) position = BinaryPrimitives.ReadUInt64LittleEndian(data.AsSpan(64, 8));
        if (count < 1 || count > MaxFields || size < 4 || size > 4 * 1024 * 1024 || position < 96 || position + size > (ulong)data.Length)
            throw new InvalidDataException("Index DBPF invalide.");
        int cursor = (int)position, end = cursor + (int)size;
        uint Next()
        {
            if (cursor > end - 4) throw new InvalidDataException("Index tronqué.");
            var v = U32(data, cursor); cursor += 4; return v;
        }
        uint flags = Next();
        if (flags > 7) throw new InvalidDataException("Index DBPF non supporté.");
        var shared = new uint?[3];
        for (int i = 0; i < 3; i++) if ((flags & (1u << i)) != 0) shared[i] = Next();
        var entries = new List<Entry>((int)count); var keys = new HashSet<Key>();
        for (int n = 0; n < count; n++)
        {
            var values = new uint[7];
            for (int i = 0; i < 7; i++) values[i] = i < 3 && shared[i] is { } s ? s : Next();
            uint compressionFlags = (values[5] & 0x80000000u) != 0 ? Next() : 0;
            uint packed = values[5] & 0x7fffffff;
            var key = new Key(values[0], values[1], ((ulong)values[2] << 32) | values[3]);
            if (!keys.Add(key)) throw new InvalidDataException("Ressource en double.");
            ulong offset = values[4];
            if (offset < 96 || offset + packed > (ulong)data.Length || (offset < (ulong)end && offset + packed > position))
                throw new InvalidDataException("Bornes de ressource invalides.");
            if (values[6] > MaxResource) throw new InvalidDataException("Ressource trop volumineuse.");
            entries.Add(new Entry(key.Type, key.Group, key.Instance, (int)offset, (int)packed, (int)values[6], compressionFlags));
        }
        if (cursor != end) throw new InvalidDataException("Octets en trop dans l'index.");
        var ranges = entries.Where(e => e.Packed > 0).Select(e => (Start: e.Offset, End: e.Offset + e.Packed)).OrderBy(r => r.Start).ThenBy(r => r.End).ToList();
        for (int i = 1; i < ranges.Count; i++)
            if (ranges[i - 1].End > ranges[i].Start) throw new InvalidDataException("Ressources chevauchantes.");
        return entries;
    }

    internal static byte[] ReadResource(byte[] data, Entry entry) =>
        Inflate(data.AsSpan(entry.Offset, entry.Packed), entry.Unpacked, entry.Compression);

    static byte[] Inflate(ReadOnlySpan<byte> data, int expected, uint compression)
    {
        if (expected < 0 || expected > MaxResource || data.Length > MaxResource) throw new InvalidDataException("Ressource trop volumineuse.");
        if (compression == 0)
        {
            if (data.Length != expected) throw new InvalidDataException("Taille ressource invalide.");
            return data.ToArray();
        }
        if (compression is not (0xffff or 0x5a42)) throw new InvalidDataException("Compression non supportée.");
        if (data.Length >= 2 && (data[0] & 15) == 8 && (data[0] >> 4) <= 7 && ((data[0] << 8) + data[1]) % 31 == 0)
        {
            try
            {
                using var source = new MemoryStream(data.ToArray());
                using var zlib = new ZLibStream(source, CompressionMode.Decompress);
                var output = new byte[expected];
                zlib.ReadExactly(output);
                if (zlib.ReadByte() != -1) throw new InvalidDataException("Zlib : taille incorrecte.");
                return output;
            }
            catch (EndOfStreamException) { throw new InvalidDataException("Zlib tronqué."); }
        }
        return RefPack(data, expected);
    }

    static byte[] RefPack(ReadOnlySpan<byte> data, int size)
    {
        int input = data.Length > 5 && data[1] == 0xfb ? 0 : 4;
        var bytes = data.ToArray();
        byte Next() { if (input >= bytes.Length) throw new InvalidDataException("RefPack tronqué."); return bytes[input++]; }
        var type = Next();
        if ((type & 0x3f) != 0x10 || Next() != 0xfb) throw new InvalidDataException("En-tête RefPack invalide.");
        ulong expected = 0;
        for (int i = 0, n = (type & 0x80) != 0 ? 4 : 3; i < n; i++) expected = (expected << 8) | Next();
        if (expected != (ulong)size) throw new InvalidDataException("Taille RefPack invalide.");
        var output = new byte[size]; int written = 0;
        while (true)
        {
            int code = Next(), plain, copy = 0, distance = 0;
            if (code < 0x80) { int b = Next(); plain = code & 3; copy = ((code & 0x1c) >> 2) + 3; distance = ((code & 0x60) << 3) + b + 1; }
            else if (code < 0xc0) { int b = Next(), c = Next(); plain = b >> 6; copy = (code & 0x3f) + 4; distance = ((b & 0x3f) << 8) + c + 1; }
            else if (code < 0xe0) { int b = Next(), c = Next(), d = Next(); plain = code & 3; copy = ((code & 0xc) << 6) + d + 5; distance = ((code & 0x10) << 12) + (b << 8) + c + 1; }
            else plain = code < 0xfc ? ((code & 0x1f) << 2) + 4 : code & 3;
            if (plain + copy > size - written || plain > bytes.Length - input) throw new InvalidDataException("RefPack hors borne.");
            for (int i = 0; i < plain; i++) output[written++] = Next();
            if (copy > 0 && distance > written) throw new InvalidDataException("Référence RefPack invalide.");
            for (int i = 0; i < copy; i++) { output[written] = output[written - distance]; written++; }
            if (code >= 0xfc)
            {
                if (written != size || input != bytes.Length) throw new InvalidDataException("Fin RefPack invalide.");
                return output;
            }
        }
    }

    static byte[] Repack(byte[] data, List<Entry> entries, Dictionary<Key, byte[]> replacements)
    {
        using var body = new MemoryStream();
        var rows = new MemoryStream();
        Span<byte> row = stackalloc byte[32];
        BinaryPrimitives.WriteUInt32LittleEndian(row, 0); rows.Write(row[..4]);
        foreach (var entry in entries)
        {
            byte[] packed; int unpacked; uint flags = entry.CompressionFlags;
            if (replacements.TryGetValue(entry.Key, out var payload))
            {
                if (payload.Length > MaxResource) throw new InvalidDataException("Remplacement trop volumineux.");
                using var compressed = new MemoryStream();
                using (var zlib = new ZLibStream(compressed, CompressionLevel.Optimal, leaveOpen: true)) zlib.Write(payload);
                packed = compressed.ToArray(); unpacked = payload.Length;
                flags = (flags & 0xffff0000u) | 0x5a42;
            }
            else { packed = data.AsSpan(entry.Offset, entry.Packed).ToArray(); unpacked = entry.Unpacked; }
            if (body.Length + packed.Length + 96 > MaxFile) throw new InvalidDataException("Sauvegarde trop volumineuse.");
            BinaryPrimitives.WriteUInt32LittleEndian(row[0..], entry.Type);
            BinaryPrimitives.WriteUInt32LittleEndian(row[4..], entry.Group);
            BinaryPrimitives.WriteUInt32LittleEndian(row[8..], (uint)(entry.Instance >> 32));
            BinaryPrimitives.WriteUInt32LittleEndian(row[12..], (uint)entry.Instance);
            BinaryPrimitives.WriteUInt32LittleEndian(row[16..], (uint)(96 + body.Length));
            BinaryPrimitives.WriteUInt32LittleEndian(row[20..], 0x80000000u | (uint)packed.Length);
            BinaryPrimitives.WriteUInt32LittleEndian(row[24..], (uint)unpacked);
            BinaryPrimitives.WriteUInt32LittleEndian(row[28..], flags);
            rows.Write(row); body.Write(packed);
        }
        var header = data.AsSpan(0, 96).ToArray();
        uint position = checked((uint)(96 + body.Length));
        BinaryPrimitives.WriteUInt32LittleEndian(header.AsSpan(36), (uint)entries.Count);
        BinaryPrimitives.WriteUInt32LittleEndian(header.AsSpan(40), position);
        BinaryPrimitives.WriteUInt32LittleEndian(header.AsSpan(44), (uint)rows.Length);
        header.AsSpan(48, 12).Clear(); // ancien index des trous invalidé
        BinaryPrimitives.WriteUInt64LittleEndian(header.AsSpan(64), position);
        var output = new byte[96 + body.Length + rows.Length];
        header.CopyTo(output, 0); body.ToArray().CopyTo(output, 96); rows.ToArray().CopyTo(output, 96 + (int)body.Length);
        if (output.LongLength > MaxFile) throw new InvalidDataException("Sauvegarde trop volumineuse.");
        ReadIndex(output);
        return output;
    }

    // ---------- Protobuf (lecture brute bornée) ----------

    /// <summary>Champ brut : Start/End couvrent tag+valeur ; ValueStart/ValueLength pour wire 2.</summary>
    internal sealed record Field(int Number, int Wire, ulong Value, int Start, int End, int ValueStart, int ValueLength);

    internal static List<Field> Fields(byte[] data)
    {
        if (data.Length > MaxResource) throw new InvalidDataException("Message trop volumineux.");
        var result = new List<Field>(); int cursor = 0;
        ulong Varint()
        {
            ulong value = 0;
            for (int i = 0; i < 10; i++)
            {
                if (cursor >= data.Length) throw new InvalidDataException("Varint tronqué.");
                byte b = data[cursor++];
                if (i == 9 && b > 1) throw new InvalidDataException("Varint hors borne.");
                value |= (ulong)(b & 127) << (i * 7);
                if (b < 128) return value;
            }
            throw new InvalidDataException("Varint hors borne.");
        }
        while (cursor < data.Length)
        {
            if (result.Count >= MaxFields) throw new InvalidDataException("Trop de champs.");
            int start = cursor;
            ulong tag = Varint();
            ulong number = tag >> 3; int wire = (int)(tag & 7);
            if (number is < 1 or > 0x1fffffff) throw new InvalidDataException("Champ invalide.");
            if (wire == 0) { var v = Varint(); result.Add(new((int)number, 0, v, start, cursor, cursor, 0)); continue; }
            ulong size = wire switch { 2 => Varint(), 1 => 8, 5 => 4, _ => throw new InvalidDataException("Type de fil invalide.") };
            if (size > (ulong)(data.Length - cursor)) throw new InvalidDataException("Longueur invalide.");
            int valueStart = cursor; cursor += (int)size;
            ulong value = wire == 1 ? BinaryPrimitives.ReadUInt64LittleEndian(data.AsSpan(valueStart, 8))
                : wire == 5 ? BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(valueStart, 4)) : 0;
            result.Add(new((int)number, wire, value, start, cursor, valueStart, (int)size));
        }
        return result;
    }

    static Dictionary<int, List<Field>> Group(List<Field> fields)
    {
        var result = new Dictionary<int, List<Field>>();
        foreach (var f in fields)
        {
            if (!result.TryGetValue(f.Number, out var list)) result[f.Number] = list = [];
            list.Add(f);
        }
        return result;
    }

    /// <summary>Message groupé par numéro ; garde ses octets pour lire les sous-messages.</summary>
    sealed class Msg(byte[] data, Dictionary<int, List<Field>> map) : Dictionary<int, List<Field>>(map)
    {
        public byte[] Data { get; } = data;
    }

    static Msg Message(byte[] data) => new(data, Group(Fields(data)));

    static int Count(Dictionary<int, List<Field>> message, int number) => message.TryGetValue(number, out var l) ? l.Count : 0;

    static ulong? OneU(Dictionary<int, List<Field>> message, int number, int wire)
    {
        if (!message.TryGetValue(number, out var values) || values.Count == 0) return null;
        if (values.Count != 1 || values[0].Wire != wire) throw new InvalidDataException("Métadonnée ambiguë.");
        return values[0].Value;
    }

    static List<Msg> Repeated(Msg message, int number)
    {
        if (!message.TryGetValue(number, out var values)) return [];
        var result = new List<Msg>(values.Count);
        foreach (var f in values)
        {
            if (f.Wire != 2) throw new InvalidDataException("Fil de métadonnée invalide.");
            result.Add(Message(message.Data.AsSpan(f.ValueStart, f.ValueLength).ToArray()));
        }
        return result;
    }

    static byte[] Varint(ulong value)
    {
        var bytes = new List<byte>(10);
        while (value > 127) { bytes.Add((byte)((value & 127) | 128)); value >>= 7; }
        bytes.Add((byte)value);
        return [.. bytes];
    }

    internal static byte[] Encode(int number, int wire, ulong value, byte[]? payload)
    {
        var tag = Varint((ulong)(number * 8 + wire));
        return wire switch
        {
            0 => [.. tag, .. Varint(value)],
            2 => [.. tag, .. Varint((ulong)payload!.Length), .. payload],
            _ => throw new ArgumentException("Remplacement non supporté."),
        };
    }

    /// <summary>Conserve exactement les octets des champs non remplacés ; ajoute les manquants par numéro croissant.</summary>
    internal static byte[] Rewrite(byte[] data, List<Field> fields, SortedDictionary<int, byte[]> replacements)
    {
        using var output = new MemoryStream(data.Length + 64);
        var seen = new HashSet<int>();
        foreach (var f in fields)
        {
            if (replacements.TryGetValue(f.Number, out var replacement))
            {
                if (!seen.Add(f.Number)) throw new InvalidDataException("Champ remplacé ambigu.");
                output.Write(replacement);
            }
            else output.Write(data, f.Start, f.End - f.Start);
        }
        foreach (var (number, replacement) in replacements)
            if (!seen.Contains(number)) output.Write(replacement);
        if (output.Length > MaxResource) throw new InvalidDataException("Message trop volumineux.");
        return output.ToArray();
    }
}
