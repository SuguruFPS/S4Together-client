using System.Buffers.Binary;
using System.IO.Compression;
using System.Text;

namespace S4Together.Launcher.Core;

public sealed record LocalSim(string Name, string Household, string SaveName, string Source, string FilePath, ulong SimId)
{
    public string Label => $"{Name} — {Household} · {SaveName} ({Source})";
    public DateTime SavedAtUtc { get; init; }
    public ulong HouseholdId { get; init; }
    public bool IsActiveHousehold { get; init; }
    public bool IsPet { get; init; }
    public string SaveId { get; init; } = "";
    public IReadOnlyList<LocalSim> PortraitSources { get; init; } = [];
}

public sealed record LocalSimScan(IReadOnlyList<LocalSim> Sims, IReadOnlyList<string> Folders, IReadOnlyList<string> Problems, int FilesRead);

/// <summary>Lecture locale des métadonnées Saves/Tray. Aucun fichier modifié et aucune sauvegarde envoyée au serveur.</summary>
public static class LocalSimCatalog
{
    const int MaxResource = 128 * 1024 * 1024;
    const int MaxFiles = 2048;
    static readonly UTF8Encoding Utf8 = new(false, true);

    public static IEnumerable<string> FindProfiles(string modsFolder)
    {
        var mods = GameLocator.FindModsFolders().ToList();
        if (!string.IsNullOrWhiteSpace(modsFolder)) mods.Insert(0, modsFolder);
        return mods.Select(p => Path.GetDirectoryName(Path.GetFullPath(p)))
            .OfType<string>().Distinct(StringComparer.OrdinalIgnoreCase).Where(Directory.Exists);
    }

    public static LocalSimScan Scan(IEnumerable<string> profiles)
    {
        var sims = new List<LocalSim>(); var problems = new List<string>(); var folders = new List<string>(); int files = 0, attempted = 0;
        foreach (var candidate in profiles.Distinct(StringComparer.OrdinalIgnoreCase).Take(16))
        {
            var root = Path.GetFullPath(candidate);
            if (Path.GetFileName(root).Equals("saves", StringComparison.OrdinalIgnoreCase) || Path.GetFileName(root).Equals("Tray", StringComparison.OrdinalIgnoreCase))
                root = Path.GetDirectoryName(root)!;
            if (folders.Contains(root, StringComparer.OrdinalIgnoreCase)) continue;
            folders.Add(root);
            foreach (var (subdir, pattern, tray) in new[] { ("saves", "*.save", false), ("Tray", "*.trayitem", true) })
            {
                var dir = Path.Combine(root, subdir);
                if (!Directory.Exists(dir)) continue;
                try
                {
                    foreach (var file in Directory.EnumerateFiles(dir, pattern, SearchOption.TopDirectoryOnly).Order(StringComparer.OrdinalIgnoreCase))
                    {
                        if (attempted++ >= MaxFiles) { problems.Add("Trop de fichiers locaux : choisis un autre dossier ou une sauvegarde dans le jeu."); break; }
                        try { sims.AddRange(tray ? ReadTray(file) : ReadSave(file)); files++; }
                        catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidDataException or OverflowException or DecoderFallbackException)
                        { problems.Add($"{Path.GetFileName(file)} : lecture impossible ({(e is InvalidDataException ? e.Message : e.GetType().Name)})."); }
                    }
                }
                catch (Exception e) when (e is IOException or UnauthorizedAccessException) { problems.Add($"{subdir} : dossier inaccessible."); }
            }
        }
        // Identité native, jamais le nom : deux Sims homonymes restent distincts.
        var latest = sims.GroupBy(s => s.SimId == 0 ? "file:" + s.FilePath + ":" + s.Name : "sim:" + s.SimId)
            .Select(g => g.OrderBy(s => s.Source == "Partie enregistrée" ? 0 : 1)
                .ThenByDescending(s => s.SavedAtUtc).ThenBy(s => s.FilePath, StringComparer.OrdinalIgnoreCase).First()
                with { PortraitSources = g.OrderByDescending(s => s.SavedAtUtc).Take(32).ToList() });
        return new(latest.OrderBy(s => s.Name, StringComparer.CurrentCultureIgnoreCase).ToList(), folders, problems, files);
    }

    public static IReadOnlyList<LocalSim> ReadSave(string path)
    {
        // Ressource SaveGameData 0x0000000D dans un conteneur DBPF v2.
        var root = Proto.Read(ReadSaveResource(path));
        if (!root.Has(2)) throw new InvalidDataException("Métadonnées de sauvegarde absentes.");
        var slot = Proto.Read(root.Bytes(2));
        var saveName = slot.Text(9); if (saveName.Length == 0) saveName = Path.GetFileNameWithoutExtension(path);
        var active = slot.Number(11);
        var homes = new Dictionary<ulong, Proto>();
        foreach (var data in root.Messages(5))
        {
            var home = Proto.Read(data);
            if (!homes.TryAdd(home.Number(2), home)) throw new InvalidDataException("Foyer dupliqué.");
        }
        var result = new List<LocalSim>();
        foreach (var data in root.Messages(6))
        {
            var sim = Proto.Read(data); var homeId = sim.Number(4);
            if (!homes.TryGetValue(homeId, out var home) || home.Number(26) == 1) continue;
            // L'origine EA prime sur les drapeaux de jeu : ils ne signifient pas « créé par ce joueur ».
            if (home.Number(29) != 0 || home.Number(32) != 0 || IsRemoteHousehold(home.Text(18))) continue;
            // HouseholdData : 31=is_player, 14=is_unplayed. Le champ 9 indique seulement
            // le Sim à sélectionner dans ce foyer, y compris les foyers précréés du jeu.
            // Son existence ne prouve donc jamais que l'utilisateur a joué ce foyer.
            if (homeId != active && home.Number(31) != 1 && !(home.Has(14) && home.Number(14) == 0)) continue;
            var name = Name(sim.Text(5), sim.Text(6));
            if (name.Length == 0) continue;
            result.Add(new(name, home.Text(3), saveName, "Partie enregistrée", path, sim.Number(1))
            { SavedAtUtc = File.GetLastWriteTimeUtc(path), HouseholdId = homeId, IsActiveHousehold = homeId == active,
                IsPet = sim.Number(60) is 2 or 3 or 4 or 6, SaveId = slot.Number(1).ToString(System.Globalization.CultureInfo.InvariantCulture) });
        }
        return result;
    }

    public static IReadOnlyList<LocalSim> ReadTray(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        if (stream.Length > 4 * 1024 * 1024) throw new InvalidDataException("Tray trop volumineux.");
        var data = new byte[checked((int)stream.Length)]; stream.ReadExactly(data);
        // Le fichier local peut précéder TrayMetadata de deux DWORD (version/longueur).
        ReadOnlyMemory<byte> payload = data;
        if (data.Length >= 8 && BinaryPrimitives.ReadUInt32LittleEndian(data) == 0)
        {
            var length = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(4));
            if (length != data.Length - 8) throw new InvalidDataException("Longueur Tray invalide.");
            payload = data.AsMemory(8);
        }
        var meta = Proto.Read(payload);
        if (!meta.Has(2)) throw new InvalidDataException("Type de bibliothèque absent.");
        if (meta.Number(2) != 2) return []; // Les terrains et pièces ne sont pas des Sims.
        var specific = Proto.Read(meta.Bytes(10));
        // Ne pas présenter les modèles Maxis, caches masqués ou téléchargements incomplets comme créations locales.
        if (specific.Number(3) != 0 || specific.Number(4) != 0 || specific.Number(11) != 0) return [];
        var household = Proto.Read(specific.Bytes(2));
        var result = new List<LocalSim>();
        foreach (var item in household.Messages(2))
        {
            var sim = Proto.Read(item); var name = Name(sim.Text(3), sim.Text(4));
            if (name.Length > 0) result.Add(new(name, meta.Text(4), "Ma bibliothèque", "Bibliothèque locale", path, sim.Number(5))
            { SavedAtUtc = File.GetLastWriteTimeUtc(path) });
        }
        return result;
    }

    static string Name(string first, string last) => (first + " " + last).Trim();

    static bool IsRemoteHousehold(string description)
    {
        var parts = description.Split(':');
        return parts is ["S4T", var owner, var hash] && Guid.TryParseExact(owner, "D", out _) &&
            (hash == "PENDING" || hash.Length == 16 && hash.All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f'));
    }

    public static byte[] ReadThumbnail(LocalSim sim)
    {
        // Une autosave peut omettre l'image présente dans une autre sauvegarde ou dans Tray.
        // Les sources sont groupées par SimId réel, jamais par nom de foyer.
        foreach (var source in new[] { sim }.Concat(sim.PortraitSources).DistinctBy(s => s.FilePath, StringComparer.OrdinalIgnoreCase))
        {
            try { var image = ReadSourceThumbnail(source); if (image.Length > 0) return image; }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or InvalidDataException) { }
        }
        return [];
    }

    static byte[] ReadSourceThumbnail(LocalSim sim)
    {
        if (Path.GetExtension(sim.FilePath).Equals(".save", StringComparison.OrdinalIgnoreCase))
        {
            var image = ReadSaveResource(sim.FilePath, 0x14, sim.HouseholdId);
            var portrait = ImagePayload(image);
            if (portrait.Length > 0) return portrait;
            // Les saves EA actuelles stockent l'image du foyer actif sous l'instance zéro.
            // Revalider le foyer du fichier : jamais utiliser l'image d'un autre foyer de cette partie.
            if (!sim.IsActiveHousehold || sim.HouseholdId == 0) return [];
            var metadata = Proto.Read(ReadSaveResource(sim.FilePath));
            var slot = Proto.Read(metadata.Bytes(2));
            if (slot.Number(11) != sim.HouseholdId) return [];
            return ImagePayload(ReadSaveResource(sim.FilePath, 0x14, 0));
        }
        var directory = Path.GetDirectoryName(sim.FilePath)!;
        var id = Path.GetFileNameWithoutExtension(sim.FilePath).Split('!').Last();
        foreach (var file in Directory.EnumerateFiles(directory, "*.hhi").Order(StringComparer.OrdinalIgnoreCase).Take(MaxFiles))
        {
            if (!Path.GetFileNameWithoutExtension(file).Split('!').Last().Equals(id, StringComparison.OrdinalIgnoreCase)) continue;
            using var stream = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            if (stream.Length > 8 * 1024 * 1024) continue;
            var bytes = new byte[(int)stream.Length]; stream.ReadExactly(bytes);
            var image = ImagePayload(bytes); if (image.Length > 0) return image;
        }
        return [];
    }

    static byte[] ImagePayload(byte[] data)
    {
        // Les images de foyer peuvent avoir un en-tête EA avant le JPEG/PNG standard.
        ReadOnlySpan<byte> png = [137,80,78,71,13,10,26,10];
        for (int i = 0; i <= Math.Min(data.Length - 8, 65536); i++)
            if (data.AsSpan(i, 8).SequenceEqual(png) ||
                (data[i] == 255 && data[i + 1] == 216 && data[i + 2] == 255)) return data.AsSpan(i).ToArray();
        return [];
    }

    static byte[] ReadSaveResource(string path, uint resourceType = 0xD, ulong? instance = null)
    {
        using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        if (file.Length < 96 || file.Length > 512L * 1024 * 1024) throw new InvalidDataException("Taille sauvegarde invalide.");
        var header = new byte[96]; file.ReadExactly(header);
        if (!header.AsSpan(0, 4).SequenceEqual("DBPF"u8)) throw new InvalidDataException("Signature DBPF absente.");
        uint major = U32(header, 4), minor = U32(header, 8);
        if (major != 2 || minor > 1) throw new InvalidDataException($"Version DBPF non supportée : {major}.{minor}.");
        uint count = U32(header, 36), size = U32(header, 44);
        ulong position = U32(header, 40);
        if (position == 0) position = BinaryPrimitives.ReadUInt64LittleEndian(header.AsSpan(64));
        if (count == 0 || count > 100000 || size > 4 * 1024 * 1024 || position > (ulong)file.Length || size > (ulong)file.Length - position)
            throw new InvalidDataException("Index DBPF invalide.");
        var index = new byte[size]; file.Position = checked((long)position); file.ReadExactly(index);
        int cursor = 0;
        uint Next() { if (cursor > index.Length - 4) throw new InvalidDataException("Index tronqué."); var n = U32(index, cursor); cursor += 4; return n; }
        uint flags = Next(); if (flags > 7) throw new InvalidDataException("Index DBPF non supporté.");
        var shared = new uint[3]; for (int i = 0; i < 3; i++) if ((flags & (1u << i)) != 0) shared[i] = Next();
        for (int entry = 0; entry < count; entry++)
        {
            var values = new uint[8]; for (int i = 0; i < 7; i++) values[i] = i < 3 && (flags & (1u << i)) != 0 ? shared[i] : Next();
            // Les entrées compactes omettent le DWORD compression/committed.
            if ((values[5] & 0x80000000u) != 0) values[7] = Next();
            if (values[0] != resourceType || (instance is { } wanted && (((ulong)values[2] << 32) | values[3]) != wanted)) continue;
            uint offset = values[4], packed = values[5] & 0x7fffffff, unpacked = values[6], compression = values[7] & 0xffff;
            if (resourceType != 0xD && (packed > 8 * 1024 * 1024 || unpacked > 8 * 1024 * 1024)) throw new InvalidDataException("Image trop volumineuse.");
            if (packed > MaxResource || unpacked > MaxResource || offset > file.Length || packed > file.Length - offset) throw new InvalidDataException("Ressource trop volumineuse ou tronquée.");
            var bytes = new byte[packed]; file.Position = offset; file.ReadExactly(bytes);
            if (compression == 0) { if (packed != unpacked) throw new InvalidDataException("Taille ressource invalide."); return bytes; }
            if (bytes.Length >= 2 && (bytes[0] & 15) == 8 && (bytes[0] >> 4) <= 7 && ((bytes[0] << 8) + bytes[1]) % 31 == 0)
            {
                using var source = new MemoryStream(bytes); using var zlib = new ZLibStream(source, CompressionMode.Decompress);
                var output = new byte[unpacked]; zlib.ReadExactly(output);
                if (zlib.ReadByte() != -1) throw new InvalidDataException("Décompression hors borne.");
                return output;
            }
            if (compression is 0xffff or 0x5a42) return RefPack(bytes, checked((int)unpacked));
            throw new InvalidDataException("Compression sauvegarde non supportée.");
        }
        if (resourceType != 0xD) return [];
        throw new InvalidDataException("SaveGameData absent.");
    }

    static uint U32(byte[] bytes, int offset) => BinaryPrimitives.ReadUInt32LittleEndian(bytes.AsSpan(offset, 4));

    static byte[] RefPack(byte[] data, int size)
    {
        int input = data.Length > 5 && data[1] == 0xfb ? 0 : 4;
        byte Next() { if (input >= data.Length) throw new InvalidDataException("RefPack tronqué."); return data[input++]; }
        var type = Next(); if ((type & 0x3f) != 0x10 || Next() != 0xfb) throw new InvalidDataException("Compression inconnue.");
        int count = (type & 0x80) != 0 ? 4 : 3; ulong expected = 0;
        for (int i = 0; i < count; i++) expected = (expected << 8) | Next();
        if (expected != (ulong)size) throw new InvalidDataException("Taille RefPack invalide.");
        var output = new byte[size]; int written = 0;
        while (true)
        {
            int code = Next(), plain, copy = 0, distance = 0;
            if (code < 0x80) { int b = Next(); plain = code & 3; copy = ((code & 0x1c) >> 2) + 3; distance = ((code & 0x60) << 3) + b + 1; }
            else if (code < 0xc0) { int b = Next(), c = Next(); plain = b >> 6; copy = (code & 0x3f) + 4; distance = ((b & 0x3f) << 8) + c + 1; }
            else if (code < 0xe0) { int b = Next(), c = Next(), d = Next(); plain = code & 3; copy = ((code & 0xc) << 6) + d + 5; distance = ((code & 0x10) << 12) + (b << 8) + c + 1; }
            else plain = code < 0xfc ? ((code & 0x1f) << 2) + 4 : code & 3;
            if (plain + copy > size - written || plain > data.Length - input) throw new InvalidDataException("RefPack hors borne.");
            for (int i = 0; i < plain; i++) output[written++] = Next();
            if (copy > 0 && distance > written) throw new InvalidDataException("Référence RefPack invalide.");
            for (int i = 0; i < copy; i++) { output[written] = output[written - distance]; written++; }
            if (code >= 0xfc) { if (written != size) throw new InvalidDataException("RefPack incomplet."); return output; }
        }
    }

    // Lecture minimale des champs protobuf utiles ; aucun code ou schéma binaire du jeu distribué.
    sealed record Field(int Tag, int Wire, ulong Value, ReadOnlyMemory<byte> Data);
    sealed class Proto(List<Field> fields)
    {
        public bool Has(int tag) => fields.Any(f => f.Tag == tag);
        public ulong Number(int tag) => fields.FirstOrDefault(f => f.Tag == tag && f.Wire is 0 or 1 or 5)?.Value ?? 0;
        public ReadOnlyMemory<byte> Bytes(int tag) => fields.FirstOrDefault(f => f.Tag == tag && f.Wire == 2)?.Data ?? ReadOnlyMemory<byte>.Empty;
        public string Text(int tag)
        {
            var bytes = Bytes(tag); if (bytes.Length > 4096) throw new InvalidDataException("Texte trop long.");
            var text = Utf8.GetString(bytes.Span);
            return new string(text.Where(c => !char.IsControl(c)).ToArray());
        }
        public IEnumerable<ReadOnlyMemory<byte>> Messages(int tag) => fields.Where(f => f.Tag == tag && f.Wire == 2).Select(f => f.Data);
        public static Proto Read(ReadOnlyMemory<byte> data)
        {
            var fields = new List<Field>(); int pos = 0;
            ulong Varint()
            {
                ulong value = 0;
                for (int shift = 0; shift < 70; shift += 7)
                {
                    if (pos >= data.Length) throw new InvalidDataException("Protobuf tronqué.");
                    var b = data.Span[pos++]; if (shift == 63 && b > 1) throw new InvalidDataException("Varint hors borne.");
                    value |= (ulong)(b & 127) << shift; if (b < 128) return value;
                }
                throw new InvalidDataException("Varint invalide.");
            }
            while (pos < data.Length)
            {
                if (fields.Count >= 100000) throw new InvalidDataException("Trop de champs protobuf.");
                var key = Varint(); if (key >> 3 == 0 || key >> 3 > 0x1fffffff) throw new InvalidDataException("Champ protobuf invalide.");
                int wire = (int)(key & 7); ulong value = 0; ReadOnlyMemory<byte> content = default;
                if (wire == 0) value = Varint();
                else if (wire == 1 || wire == 5)
                {
                    int size = wire == 1 ? 8 : 4; if (size > data.Length - pos) throw new InvalidDataException("Champ tronqué.");
                    value = size == 8 ? BinaryPrimitives.ReadUInt64LittleEndian(data.Span.Slice(pos, size)) : BinaryPrimitives.ReadUInt32LittleEndian(data.Span.Slice(pos, size)); pos += size;
                }
                else if (wire == 2)
                {
                    var length = Varint(); if (length > (ulong)(data.Length - pos)) throw new InvalidDataException("Message tronqué.");
                    content = data.Slice(pos, (int)length); pos += (int)length;
                }
                else throw new InvalidDataException("Type protobuf non supporté.");
                fields.Add(new((int)(key >> 3), wire, value, content));
            }
            return new(fields);
        }
    }
}
