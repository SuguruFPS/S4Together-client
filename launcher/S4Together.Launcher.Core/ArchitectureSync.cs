using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json;

namespace S4Together.Launcher.Core;

public sealed record ArchitectureRemote(string Lot, string Sha, int Bytes);
public sealed record ArchitectureSaveResult(string Status, IReadOnlyList<string> Applied, IReadOnlyList<string> Skipped, string? BackupPath = null);

/// <summary>
/// Transport HTTP de l'architecture des domiciles (murs, sols, toits) : le propriétaire envoie
/// le bloc LDNB extrait de sa copie de sauvegarde ; les autres joueurs le téléchargent.
/// </summary>
public sealed class ArchitectureSyncClient : IDisposable
{
    const int MaxListBytes = 256 * 1024, MaxLots = 256;
    readonly HttpClient http; readonly bool owns;
    public ArchitectureSyncClient()
    {
        http = new(new SocketsHttpHandler { AllowAutoRedirect = false, UseProxy = false, ConnectTimeout = TimeSpan.FromSeconds(10) })
        { Timeout = TimeSpan.FromMinutes(2), MaxResponseContentBufferSize = SaveArchitecture.MaxArchitectureBytes + 4096 };
        owns = true;
    }
    public ArchitectureSyncClient(HttpClient client) { http = client; }
    public void Dispose() { if (owns) http.Dispose(); }

    HttpRequestMessage Request(HttpMethod method, ServerAddress address, HelloResult hello, string path)
    {
        address.RequireSafeTransport();
        var request = new HttpRequestMessage(method, new Uri(address.HealthUri, path));
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", hello.Token);
        request.Headers.Add("X-S4T-Player", hello.PlayerId); request.Headers.Add("X-S4T-Session", hello.SessionId);
        return request;
    }

    /// <summary>Extrait l'architecture du domicile du personnage depuis une copie et l'envoie.</summary>
    public async Task<(string Status, string? Sha)> UploadHomeAsync(ServerAddress address, HelloResult hello, CharacterInfo character, string saveCopy, string? lastSha, CancellationToken cancel)
    {
        if (!character.Bound || !CharacterRoster.IsUuid(character.Id) || !ulong.TryParse(character.GameHouseholdId, out var household) || household == 0)
            return ("unbound", null);
        if ((File.GetAttributes(saveCopy) & FileAttributes.ReparsePoint) != 0) throw new IOException("Copie redirigée refusée.");
        if (new FileInfo(saveCopy).Length > 256L * 1024 * 1024) throw new InvalidDataException("Copie trop grande.");
        LotArchitecture home;
        try { home = SaveArchitecture.ExtractHome(await File.ReadAllBytesAsync(saveCopy, cancel), household); }
        catch (InvalidDataException) { return ("no_home", null); }
        if (home.Sha == lastSha) return ("unchanged", home.Sha);
        using var put = Request(HttpMethod.Put, address, hello, $"architecture/{character.Id}/{home.LotDescriptionId}/{home.Sha}");
        put.Content = new ByteArrayContent(home.Data);
        put.Content.Headers.ContentType = new MediaTypeHeaderValue("application/octet-stream");
        using var reply = await http.SendAsync(put, cancel);
        switch (reply.StatusCode)
        {
            case HttpStatusCode.NotFound or HttpStatusCode.MethodNotAllowed: return ("unsupported", null);
            case HttpStatusCode.Forbidden: return ("not_owner", null);
            case HttpStatusCode.TooManyRequests: return ("throttled", null);
        }
        reply.EnsureSuccessStatusCode();
        using var document = JsonDocument.Parse(await reply.Content.ReadAsStringAsync(cancel));
        if (document.RootElement.GetProperty("sha").GetString() != home.Sha || document.RootElement.GetProperty("lot").GetString() != home.LotDescriptionId)
            throw new InvalidDataException("Confirmation architecture invalide.");
        return ("stored", home.Sha);
    }

    /// <summary>Liste puis télécharge les architectures des autres domiciles, chacune vérifiée par SHA.</summary>
    public async Task<IReadOnlyList<LotArchitecture>?> DownloadAsync(ServerAddress address, HelloResult hello, CharacterInfo character, CancellationToken cancel)
    {
        if (!character.Bound || !CharacterRoster.IsUuid(character.Id)) return [];
        List<ArchitectureRemote> remote;
        using (var list = Request(HttpMethod.Get, address, hello, $"architecture/{character.Id}"))
        using (var reply = await http.SendAsync(list, cancel))
        {
            if (reply.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.MethodNotAllowed) return null;
            reply.EnsureSuccessStatusCode();
            var json = await reply.Content.ReadAsStringAsync(cancel);
            if (json.Length > MaxListBytes) throw new InvalidDataException("Liste architecture trop grande.");
            remote = ParseList(json);
        }
        var lots = new List<LotArchitecture>();
        foreach (var entry in remote)
        {
            await Task.Delay(TimeSpan.FromMilliseconds(50), cancel);
            using var get = Request(HttpMethod.Get, address, hello, $"architecture/{character.Id}/{entry.Lot}");
            using var reply = await http.SendAsync(get, cancel);
            if (reply.StatusCode == HttpStatusCode.NotFound) continue;
            reply.EnsureSuccessStatusCode();
            var data = await reply.Content.ReadAsByteArrayAsync(cancel);
            SaveArchitecture.ValidateBlock(data);
            var lot = LotArchitecture.Create(entry.Lot, data);
            // L'en-tête fait foi : le propriétaire a pu renvoyer une version plus récente depuis la liste.
            if (!reply.Headers.TryGetValues("X-S4T-Sha", out var shas) || shas.SingleOrDefault() != lot.Sha)
                throw new InvalidDataException("SHA architecture différent.");
            lots.Add(lot);
        }
        return lots;
    }

    internal static List<ArchitectureRemote> ParseList(string json)
    {
        using var document = JsonDocument.Parse(json);
        if (document.RootElement.ValueKind != JsonValueKind.Array || document.RootElement.GetArrayLength() > MaxLots)
            throw new InvalidDataException("Liste architecture invalide.");
        var result = new List<ArchitectureRemote>();
        foreach (var item in document.RootElement.EnumerateArray())
        {
            var lot = item.GetProperty("lot").GetString() ?? "";
            var sha = item.GetProperty("sha").GetString() ?? "";
            var bytes = item.GetProperty("bytes").GetInt32();
            if (lot.Length is < 1 or > 20 || !lot.All(char.IsAsciiDigit) || lot == "0" || lot[0] == '0' ||
                sha.Length != 64 || !sha.All(c => char.IsAsciiDigit(c) || c is >= 'a' and <= 'f') || bytes is < 16 or > SaveArchitecture.MaxArchitectureBytes)
                throw new InvalidDataException("Entrée architecture invalide.");
            if (result.All(r => r.Lot != lot)) result.Add(new(lot, sha, bytes));
        }
        return result;
    }
}

/// <summary>
/// Écrit les architectures distantes dans la sauvegarde multijoueur locale, jeu fermé seulement :
/// copie de sécurité vérifiée hors du dossier du jeu, fichier temporaire relu, remplacement atomique.
/// </summary>
public sealed class ArchitectureSaveWriter(string backupRoot, Func<bool>? gameRunning = null, Func<CharacterInfo, string, bool>? saveMatches = null)
{
    const long MaxSaveBytes = 256L * 1024 * 1024;
    const int MaxBackups = 5;
    readonly Func<bool> running = gameRunning ?? GameLocator.IsGameRunning;
    readonly Func<CharacterInfo, string, bool> matches = saveMatches ?? MultiplayerSaveMatches;
    static string Hash(byte[] data) => Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();
    sealed record State(string SaveSha, Dictionary<string, string> Lots);

    public ArchitectureSaveResult Apply(string savePath, CharacterInfo character, IReadOnlyList<LotArchitecture> lots)
    {
        if (lots.Count == 0) return new("nothing", [], []);
        if (running()) return new("game_running", [], []);
        var save = new FileInfo(Path.GetFullPath(savePath));
        if (!save.Exists || (save.Attributes & FileAttributes.ReparsePoint) != 0 || save.Length is <= 0 or > MaxSaveBytes ||
            !save.Name.StartsWith("Slot_", StringComparison.OrdinalIgnoreCase) || !save.Name.EndsWith(".save", StringComparison.OrdinalIgnoreCase))
            throw new IOException("Sauvegarde multijoueur invalide.");
        var root = Path.GetFullPath(backupRoot);
        if (root.StartsWith(save.DirectoryName! + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) || root.Equals(save.DirectoryName, StringComparison.OrdinalIgnoreCase))
            throw new IOException("Les copies doivent rester hors du dossier de jeu.");
        if (!matches(character, save.FullName)) return new("not_multiplayer", [], []);

        var length = save.Length; var stamp = save.LastWriteTimeUtc;
        var before = File.ReadAllBytes(save.FullName);
        var beforeSha = Hash(before);
        var folder = Path.Combine(root, character.Id); Directory.CreateDirectory(folder);
        var statePath = Path.Combine(folder, "architecture-state.json");
        var state = ReadState(statePath);
        if (state is not null && state.SaveSha == beforeSha && lots.All(l => state.Lots.TryGetValue(l.LotDescriptionId, out var s) && s == l.Sha))
            return new("unchanged", [], []);

        var result = SaveArchitecture.Apply(before, lots);
        if (result.Output is null) return new("nothing", result.Applied, result.Skipped);

        // Copie de sécurité AVANT toute écriture dans le dossier du jeu.
        var backup = Path.Combine(folder, "before-architecture-" + beforeSha + ".save");
        if (!File.Exists(backup)) WriteVerified(backup, before);
        else if (Hash(File.ReadAllBytes(backup)) != beforeSha) throw new InvalidDataException("Copie de sécurité altérée.");
        Prune(folder, backup);

        var temporary = Path.Combine(save.DirectoryName!, save.Name + ".s4t-" + Guid.NewGuid().ToString("N") + ".tmp");
        try
        {
            WriteVerified(temporary, result.Output);
            if (!matches(character, temporary)) throw new InvalidDataException("Sauvegarde modifiée illisible.");
            var now = new FileInfo(save.FullName);
            if (running() || now.Length != length || now.LastWriteTimeUtc != stamp || Hash(File.ReadAllBytes(save.FullName)) != beforeSha)
                return new("unstable", [], result.Skipped, backup);
            File.Replace(temporary, save.FullName, null, true);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
        var afterSha = Hash(File.ReadAllBytes(save.FullName));
        if (afterSha != Hash(result.Output)) throw new IOException("Remplacement de sauvegarde non confirmé ; copie : " + backup);
        WriteState(statePath, new(afterSha, lots.ToDictionary(l => l.LotDescriptionId, l => l.Sha)));
        return new("applied", result.Applied, result.Skipped, backup);
    }

    static bool MultiplayerSaveMatches(CharacterInfo character, string file)
    {
        try { return MultiplayerSave.Find(character, LocalSimCatalog.ReadSave(file)) is not null; }
        catch (Exception e) when (e is InvalidDataException or OverflowException or System.Text.DecoderFallbackException) { return false; }
    }
    static void WriteVerified(string path, byte[] data)
    {
        using (var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        { output.Write(data); output.Flush(true); }
        if (Hash(File.ReadAllBytes(path)) != Hash(data)) throw new IOException("Écriture non confirmée.");
    }
    static void Prune(string folder, string keep)
    {
        foreach (var old in new DirectoryInfo(folder).GetFiles("before-architecture-*.save").OrderByDescending(f => f.CreationTimeUtc).Skip(MaxBackups))
            if (!old.FullName.Equals(keep, StringComparison.OrdinalIgnoreCase) && (old.Attributes & FileAttributes.ReparsePoint) == 0) old.Delete();
    }
    static State? ReadState(string path)
    {
        try { return File.Exists(path) && new FileInfo(path).Length < 65536 ? JsonSerializer.Deserialize<State>(File.ReadAllText(path)) : null; }
        catch (JsonException) { return null; }
    }
    static void WriteState(string path, State state)
    {
        var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(state));
        File.Move(temporary, path, true);
    }
}
