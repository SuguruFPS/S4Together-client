using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace S4Together.Launcher.Core;

/// <summary>Mod déclaré au HELLO : le serveur compare id, version et empreinte avec son catalogue publié.</summary>
public sealed record ServerModDeclaration(string Id, string Version, string Hash);

public sealed record ServerModSyncResult(IReadOnlyList<ServerModDeclaration> Mods, int Downloaded, int Removed);
public sealed record ServerModInstallInfo(string Endpoint, int Count, bool RemovalPending);
public sealed record ServerModCleanupResult(int Removed, int Preserved, bool Deferred);

/// <summary>Synchronisation refusée : le message est affiché tel quel au joueur.</summary>
public sealed class ServerModSyncException(string message) : Exception(message);

/// <summary>
/// Mods publiés par le serveur (dossier « Mods » du serveur) : manifeste signé RSA-SHA256, clé épinglée
/// à la première connexion à cette adresse, fichiers vérifiés (taille + SHA-256) avant d'être déplacés
/// dans Mods\S4Together-Serveur. Seuls les fichiers que ce launcher a lui-même installés sont retirés.
/// </summary>
public static class ServerModSync
{
    public const string FolderName = "S4Together-Serveur";
    public const string StateFileName = ".s4t-server-mods.json";
    public const long MaxFileBytes = 16L * 1024 * 1024;
    public const long MaxTotalBytes = 256L * 1024 * 1024;
    public const int MaxMods = 64;
    const int MaxManifestBytes = 1024 * 1024;
    static readonly Regex IdPattern = new("^[a-z][a-z0-9-]{1,63}$", RegexOptions.CultureInvariant);
    static readonly Regex VersionPattern = new("^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$", RegexOptions.CultureInvariant);
    static readonly Regex FilePattern = new("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", RegexOptions.CultureInvariant);
    static readonly Regex HashPattern = new("^[0-9a-f]{64}$", RegexOptions.CultureInvariant);
    static readonly Regex Reserved = new("^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web) { WriteIndented = true };

    public sealed record ManifestMod(string Id, string Version, string Sha256, long Size, string FileName, bool Required);
    sealed record InstalledFile(string FileName, string Sha256, long Size, DateTime WriteUtc, bool Retained = false, string? Source = null);
    sealed record InstallState(string Endpoint, string Fingerprint, List<InstalledFile> Files, bool RemovalPending = false);

    static void RefuseRedirected(string path)
    {
        if (Path.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new ServerModSyncException("Dossier ou fichier de mods redirigé : opération refusée.");
    }

    static string Managed(string modsFolder)
    {
        if (GameLocator.ValidateModsFolder(modsFolder).Length > 0)
            throw new ServerModSyncException("Dossier Mods invalide : aucun fichier retiré.");
        var path = Path.Combine(Path.GetFullPath(modsFolder), FolderName);
        RefuseRedirected(path); RefuseRedirected(Path.Combine(path, StateFileName));
        return path;
    }

    static bool BelongsTo(InstalledFile file, InstallState state, string endpoint) =>
        string.Equals(file.Source ?? state.Endpoint, endpoint, StringComparison.Ordinal);

    public static ServerModInstallInfo? Installed(string modsFolder, bool includeRetained = false)
    {
        var managed = Managed(modsFolder);
        var state = ReadState(managed);
        return state is null ? null : new(state.Endpoint,
            state.Files.Count(f => BelongsTo(f, state, state.Endpoint) && (includeRetained || !f.Retained) && File.Exists(Path.Combine(managed, f.FileName))), state.RemovalPending);
    }

    /// <summary>Choix explicite au départ. Seuls nos fichiers inchangés de ce serveur sont retirés.</summary>
    public static ServerModCleanupResult Leave(string modsFolder, string endpoint, bool keep, bool gameRunning)
    {
        var managed = Managed(modsFolder);
        var state = ReadState(managed);
        if (state is null || state.Endpoint != endpoint) return new(0, 0, false);
        if (keep)
        {
            WriteState(managed, state with { RemovalPending = false,
                Files = state.Files.Select(f => BelongsTo(f, state, endpoint) ? f with { Retained = true, Source = endpoint } : f).ToList() });
            return new(0, 0, false);
        }
        if (gameRunning)
        {
            WriteState(managed, state with { RemovalPending = true });
            return new(0, 0, true);
        }
        var removed = 0; var preserved = 0; var files = new List<InstalledFile>();
        foreach (var file in state.Files)
        {
            if (!BelongsTo(file, state, endpoint)) { files.Add(file); continue; }
            var path = Path.Combine(managed, file.FileName);
            if (!File.Exists(path)) continue;
            RefuseRedirected(path);
            if (new FileInfo(path).Length != file.Size || HashFile(path) != file.Sha256)
            { files.Add(file with { Retained = true }); preserved++; continue; }
            File.Delete(path); removed++;
        }
        WriteState(managed, state with { Files = files, RemovalPending = false });
        return new(removed, preserved, false);
    }

    public static ServerModCleanupResult CompletePendingRemoval(string modsFolder, bool gameRunning)
    {
        var info = Installed(modsFolder);
        if (info?.RemovalPending != true) return new(0, 0, false);
        return gameRunning ? new(0, 0, true) : Leave(modsFolder, info.Endpoint, keep: false, gameRunning: false);
    }

    /// <summary>
    /// Met Mods\S4Together-Serveur en conformité avec le manifeste du serveur. <paramref name="pinnedKeys"/> (adresse → empreinte)
    /// est complété à la première connexion ; une clé différente ensuite est refusée.
    /// </summary>
    public static async Task<ServerModSyncResult> SyncAsync(ServerAddress address, string modsFolder, IDictionary<string, string> pinnedKeys,
        bool gameRunning, HttpMessageHandler? handler = null, CancellationToken cancel = default)
    {
        address.RequireSafeTransport();
        using var client = handler is null
            ? new HttpClient(new SocketsHttpHandler { AllowAutoRedirect = false, UseProxy = false, UseCookies = false }) { Timeout = TimeSpan.FromMinutes(2) }
            : new HttpClient(handler, false) { Timeout = TimeSpan.FromMinutes(2) };
        var baseUri = address.HealthUri.ToString()[..^"/health".Length];
        var endpoint = ServerDirectory.Key(address);
        var manifest = await FetchManifestAsync(client, baseUri, cancel);
        string? fingerprint = null;
        IReadOnlyList<ManifestMod> mods = [];
        if (manifest is not null)
        {
            (fingerprint, mods) = Verify(manifest);
            if (pinnedKeys.TryGetValue(endpoint, out var pinned) && !CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(pinned), Encoding.ASCII.GetBytes(fingerprint)))
                throw new ServerModSyncException("La signature des mods de ce serveur a changé depuis ta dernière connexion. Si le propriétaire a réinstallé son serveur, retire la fiche du répertoire puis ajoute-la à nouveau ; sinon ne te connecte pas.");
        }
        var folderProblem = GameLocator.ValidateModsFolder(modsFolder);
        if (folderProblem.Length > 0)
        {
            if (mods.Count > 0) throw new ServerModSyncException("Ce serveur demande des mods : choisis d'abord ton dossier Mods (" + folderProblem + ").");
            return new([], 0, 0);
        }
        var managed = Managed(modsFolder);
        CompletePendingRemoval(modsFolder, gameRunning);
        var state = ReadState(managed);
        if (state?.RemovalPending == true && gameRunning)
            throw new ServerModSyncException("Le retrait des mods est en attente : ferme Les Sims 4 avant de rejoindre à nouveau.");
        var plan = Plan(managed, state, mods);
        var files = state?.Files.Where(f => !plan.Remove.Contains(f.FileName, StringComparer.OrdinalIgnoreCase))
            .Select(f => f with { Source = !f.Retained && mods.Any(m => m.FileName.Equals(f.FileName, StringComparison.OrdinalIgnoreCase))
                ? endpoint : f.Source ?? state.Endpoint }).ToList() ?? [];
        if (plan.Download.Count == 0 && plan.Remove.Count == 0)
        {
            if (state is not null && state.Endpoint != endpoint) WriteState(managed, new(endpoint, fingerprint ?? "", files));
            if (fingerprint is not null) pinnedKeys[endpoint] = fingerprint;
            return new(Declarations(mods), 0, 0);
        }
        if (gameRunning) throw new ServerModSyncException("Ferme Les Sims 4 pour installer les mods du serveur, puis reconnecte-toi.");

        Directory.CreateDirectory(managed);
        var parts = Path.Combine(managed, ".telechargement-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(parts);
        try
        {
            foreach (var name in plan.Remove)
            {
                var path = Path.Combine(managed, name);
                if (File.Exists(path)) File.Delete(path);
            }
            WriteState(managed, new(endpoint, fingerprint ?? "", files));
            foreach (var mod in plan.Download)
            {
                cancel.ThrowIfCancellationRequested();
                var part = Path.Combine(parts, mod.Sha256 + ".part");
                await DownloadAsync(client, baseUri, mod, part, cancel);
                var target = Path.Combine(managed, mod.FileName);
                File.Move(part, target, true);
                files.RemoveAll(f => f.FileName.Equals(mod.FileName, StringComparison.OrdinalIgnoreCase));
                files.Add(new(mod.FileName, mod.Sha256, mod.Size, File.GetLastWriteTimeUtc(target), Source: endpoint));
                WriteState(managed, new(endpoint, fingerprint ?? "", files));
            }
        }
        finally { try { Directory.Delete(parts, true); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
        if (fingerprint is not null) pinnedKeys[endpoint] = fingerprint;
        return new(Declarations(mods), plan.Download.Count, plan.Remove.Count);
    }

    static IReadOnlyList<ServerModDeclaration> Declarations(IReadOnlyList<ManifestMod> mods) =>
        mods.Select(m => new ServerModDeclaration(m.Id, m.Version, m.Sha256)).ToArray();

    /// <summary>Null : serveur sans catalogue de mods (ancienne version) — rien à installer.</summary>
    static async Task<JsonDocument?> FetchManifestAsync(HttpClient client, string baseUri, CancellationToken cancel)
    {
        try
        {
            using var response = await client.GetAsync(baseUri + "/mods/manifest", HttpCompletionOption.ResponseHeadersRead, cancel);
            if (response.StatusCode == HttpStatusCode.NotFound) return null;
            if (!response.IsSuccessStatusCode) throw new ServerModSyncException($"Liste des mods du serveur indisponible (HTTP {(int)response.StatusCode}). Réessaie plus tard.");
            var bytes = await ReadBoundedAsync(response, MaxManifestBytes, cancel);
            return JsonDocument.Parse(bytes);
        }
        catch (JsonException) { throw new ServerModSyncException("Liste des mods du serveur illisible."); }
        catch (HttpRequestException) { throw new ServerModSyncException("Liste des mods du serveur injoignable. Réessaie plus tard."); }
        catch (TaskCanceledException) when (!cancel.IsCancellationRequested) { throw new ServerModSyncException("Le serveur met trop de temps à envoyer la liste des mods."); }
    }

    /// <summary>Signature, empreinte et contenu vérifiés ; tout écart refuse la liste entière.</summary>
    public static (string Fingerprint, IReadOnlyList<ManifestMod> Mods) Verify(JsonDocument envelope)
    {
        try
        {
            var root = envelope.RootElement;
            var payload = Convert.FromBase64String(root.GetProperty("payload").GetString()!);
            var signature = Convert.FromBase64String(root.GetProperty("signature").GetString()!);
            var spki = Convert.FromBase64String(root.GetProperty("publicKey").GetProperty("spki").GetString()!);
            if (root.GetProperty("format").GetInt32() != 1 || payload.Length > MaxManifestBytes || spki.Length > 1024) throw new FormatException();
            var fingerprint = Convert.ToHexString(SHA256.HashData(spki)).ToLowerInvariant();
            if (root.GetProperty("fingerprint").GetString() != fingerprint) throw new FormatException();
            using var rsa = RSA.Create();
            rsa.ImportSubjectPublicKeyInfo(spki, out var read);
            if (read != spki.Length || rsa.KeySize < 2048 || !rsa.VerifyData(payload, signature, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1))
                throw new ServerModSyncException("Liste des mods du serveur mal signée : connexion refusée.");
            using var body = JsonDocument.Parse(payload);
            if (body.RootElement.GetProperty("format").GetInt32() != 1) throw new FormatException();
            var mods = body.RootElement.GetProperty("mods").EnumerateArray().Select(m => new ManifestMod(
                m.GetProperty("id").GetString()!, m.GetProperty("version").GetString()!, m.GetProperty("sha256").GetString()!,
                m.GetProperty("size").GetInt64(), m.GetProperty("fileName").GetString()!, m.GetProperty("required").GetBoolean())).ToArray();
            Validate(mods);
            return (fingerprint, mods);
        }
        catch (Exception e) when (e is FormatException or KeyNotFoundException or InvalidOperationException or JsonException or CryptographicException or ArgumentException or NullReferenceException)
        {
            throw new ServerModSyncException("Liste des mods du serveur invalide : connexion refusée.");
        }
    }

    static void Validate(IReadOnlyList<ManifestMod> mods)
    {
        if (mods.Count > MaxMods || mods.Sum(m => m.Size) > MaxTotalBytes) throw new FormatException();
        foreach (var m in mods)
        {
            var extension = Path.GetExtension(m.FileName).ToLowerInvariant();
            if (!IdPattern.IsMatch(m.Id) || !VersionPattern.IsMatch(m.Version) || !HashPattern.IsMatch(m.Sha256) || m.Size is < 1 or > MaxFileBytes
                || !FilePattern.IsMatch(m.FileName) || m.FileName.Contains("..") || extension is not (".package" or ".ts4script")
                || Reserved.IsMatch(m.FileName.Split('.')[0]))
                throw new FormatException();
        }
        if (mods.Select(m => m.Id).Distinct(StringComparer.OrdinalIgnoreCase).Count() != mods.Count
            || mods.Select(m => m.FileName).Distinct(StringComparer.OrdinalIgnoreCase).Count() != mods.Count) throw new FormatException();
    }

    sealed record SyncPlan(List<ManifestMod> Download, List<string> Remove);

    static SyncPlan Plan(string managed, InstallState? state, IReadOnlyList<ManifestMod> mods)
    {
        var installed = state?.Files ?? [];
        var download = new List<ManifestMod>();
        foreach (var mod in mods)
        {
            var path = Path.Combine(managed, mod.FileName);
            var known = installed.FirstOrDefault(f => f.FileName.Equals(mod.FileName, StringComparison.OrdinalIgnoreCase));
            if (!File.Exists(path)) { download.Add(mod); continue; }
            var info = new FileInfo(path);
            RefuseRedirected(path);
            if (known is null && (info.Length != mod.Size || HashFile(path) != mod.Sha256))
                throw new ServerModSyncException($"Un fichier personnel porte déjà le nom « {mod.FileName} ». Déplace-le avant de rejoindre ; il est préservé.");
            if (known is not null && (info.Length != known.Size || HashFile(path) != known.Sha256))
                throw new ServerModSyncException($"Le fichier « {mod.FileName} » a été modifié localement. Déplace-le avant de rejoindre ; il est préservé.");
            if (known is { Retained: true } && (info.Length != mod.Size || HashFile(path) != mod.Sha256))
                throw new ServerModSyncException($"Le mod conservé « {mod.FileName} » entre en conflit avec celui du serveur. Ton fichier est préservé ; retire-le ou déplace-le avant de rejoindre.");
            var unchanged = known is not null && known.Sha256 == mod.Sha256 && known.Size == info.Length && known.WriteUtc == info.LastWriteTimeUtc;
            if (!unchanged && !(info.Length == mod.Size && HashFile(path) == mod.Sha256)) download.Add(mod);
        }
        var wanted = mods.Select(m => m.FileName).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var remove = installed.Where(f => !f.Retained && !wanted.Contains(f.FileName) && File.Exists(Path.Combine(managed, f.FileName)))
            .Where(f => { var path = Path.Combine(managed, f.FileName); RefuseRedirected(path); return new FileInfo(path).Length == f.Size && HashFile(path) == f.Sha256; })
            .Select(f => f.FileName).ToList();
        return new(download, remove);
    }

    static string HashFile(string path)
    {
        using var input = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(input)).ToLowerInvariant();
    }

    static async Task DownloadAsync(HttpClient client, string baseUri, ManifestMod mod, string part, CancellationToken cancel)
    {
        try
        {
            using var response = await client.GetAsync(baseUri + "/mods/files/" + mod.Sha256, HttpCompletionOption.ResponseHeadersRead, cancel);
            if (!response.IsSuccessStatusCode) throw new ServerModSyncException($"Mod « {mod.FileName} » indisponible sur le serveur (HTTP {(int)response.StatusCode}).");
            var bytes = await ReadBoundedAsync(response, mod.Size, cancel);
            if (bytes.Length != mod.Size || Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant() != mod.Sha256)
                throw new ServerModSyncException($"Mod « {mod.FileName} » reçu abîmé : rien n'a été installé pour ce fichier. Réessaie.");
            await File.WriteAllBytesAsync(part, bytes, cancel);
        }
        catch (HttpRequestException) { throw new ServerModSyncException($"Téléchargement du mod « {mod.FileName} » interrompu. Réessaie."); }
        catch (TaskCanceledException) when (!cancel.IsCancellationRequested) { throw new ServerModSyncException($"Téléchargement du mod « {mod.FileName} » trop long. Réessaie."); }
    }

    static async Task<byte[]> ReadBoundedAsync(HttpResponseMessage response, long max, CancellationToken cancel)
    {
        if (response.Content.Headers.ContentLength is > 0 and var length && length > max) throw new ServerModSyncException("Réponse du serveur trop grande : refusée.");
        await using var stream = await response.Content.ReadAsStreamAsync(cancel);
        using var buffer = new MemoryStream();
        var chunk = new byte[81920];
        int read;
        while ((read = await stream.ReadAsync(chunk, cancel)) > 0)
        {
            if (buffer.Length + read > max) throw new ServerModSyncException("Réponse du serveur trop grande : refusée.");
            buffer.Write(chunk, 0, read);
        }
        return buffer.ToArray();
    }

    static InstallState? ReadState(string managed)
    {
        var path = Path.Combine(managed, StateFileName);
        try
        {
            if (!File.Exists(path) || new FileInfo(path).Length > 256 * 1024) return null;
            var state = JsonSerializer.Deserialize<InstallState>(File.ReadAllText(path), Json);
            // Un état modifié à la main ne doit jamais faire supprimer un fichier hors du dossier géré.
            return state?.Files is null || string.IsNullOrWhiteSpace(state.Endpoint) ? null : state with { Files = state.Files.Where(f => f is not null
                && f.FileName is not null && f.Sha256 is not null
                && FilePattern.IsMatch(f.FileName) && !f.FileName.Contains("..") && HashPattern.IsMatch(f.Sha256)
                && f.Size is > 0 and <= MaxFileBytes && Path.GetExtension(f.FileName).ToLowerInvariant() is ".package" or ".ts4script").ToList() };
        }
        catch (Exception e) when (e is JsonException or IOException or NotSupportedException) { return null; }
    }

    static void WriteState(string managed, InstallState state)
    {
        var path = Path.Combine(managed, StateFileName);
        RefuseRedirected(managed); RefuseRedirected(path);
        var pending = path + "." + Guid.NewGuid().ToString("N") + ".new";
        try
        {
            File.WriteAllText(pending, JsonSerializer.Serialize(state, Json));
            File.Move(pending, path, true);
        }
        finally { if (File.Exists(pending)) File.Delete(pending); }
    }
}
