using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace S4Together.Launcher.Core;

/// <summary>Identité serveur mémorisée : le jeton est chiffré par DPAPI (compte Windows courant).</summary>
public sealed class StoredIdentity
{
    public string PlayerId { get; set; } = "";
    public string ProtectedToken { get; set; } = "";
    /// <summary>worldId appris après un HELLO accepté : reconnaît le même serveur sous une autre adresse.</summary>
    public string WorldId { get; set; } = "";
}

public sealed class LauncherSettings
{
    public string ServerHost { get; set; } = "";
    public string ServerPort { get; set; } = ServerAddress.DefaultPort.ToString();
    public string PlayerName { get; set; } = "";
    public string ModsFolder { get; set; } = "";
    public string LocalSimProfile { get; set; } = "";
    public Dictionary<string, string> LocalCharacters { get; set; } = [];
    public Dictionary<string, SoloGuard> SoloGuards { get; set; } = [];
    public string GamePath { get; set; } = "";
    public bool RememberPassword { get; set; }
    public string ProtectedPassword { get; set; } = "";
    public bool MinimizeToTray { get; set; } = true;
    public List<string> Dlcs { get; set; } = [];
    public List<string> Favorites { get; set; } = [];
    public Dictionary<string, StoredIdentity> Identities { get; set; } = [];
    public List<KnownServer> KnownServers { get; set; } = [];
    public Dictionary<string, string> ServerPasswords { get; set; } = [];

    static readonly JsonSerializerOptions Json = new() { WriteIndented = true, DefaultIgnoreCondition = JsonIgnoreCondition.Never };
    static readonly byte[] Entropy = Encoding.UTF8.GetBytes("S4Together.Launcher.v2");
    const int MaxFileBytes = 4 * 1024 * 1024; // gardes solo jusqu'à 2 × 1024 identifiants par personnage

    public static string DefaultFolder => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "S4Together", "launcher");

    /// <summary>Fichier absent ou illisible : réglages par défaut (le fichier illisible est conservé à côté).</summary>
    public static LauncherSettings Load(string? folder = null)
    {
        var path = Path.Combine(folder ?? DefaultFolder, "settings.json");
        if (!File.Exists(path)) return new LauncherSettings();
        try
        {
            if (new FileInfo(path).Length > MaxFileBytes) throw new InvalidDataException();
            return JsonSerializer.Deserialize<LauncherSettings>(File.ReadAllText(path), Json) ?? new LauncherSettings();
        }
        catch (Exception e) when (e is JsonException or InvalidDataException or NotSupportedException)
        {
            File.Copy(path, path + ".illisible-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss"), true);
            return new LauncherSettings();
        }
    }

    public void Save(string? folder = null)
    {
        var dir = folder ?? DefaultFolder;
        Directory.CreateDirectory(dir);
        var path = Path.Combine(dir, "settings.json");
        var temporary = path + ".new";
        File.WriteAllText(temporary, JsonSerializer.Serialize(this, Json));
        File.Move(temporary, path, true);
    }

    public static string Protect(string secret) =>
        secret.Length == 0 ? "" : Convert.ToBase64String(ProtectedData.Protect(Encoding.UTF8.GetBytes(secret), Entropy, DataProtectionScope.CurrentUser));

    /// <summary>Chaîne vide si absent ou indéchiffrable (autre compte Windows, fichier copié).</summary>
    public static string Unprotect(string protectedValue)
    {
        if (protectedValue.Length == 0) return "";
        try { return Encoding.UTF8.GetString(ProtectedData.Unprotect(Convert.FromBase64String(protectedValue), Entropy, DataProtectionScope.CurrentUser)); }
        catch (Exception e) when (e is CryptographicException or FormatException) { return ""; }
    }

    public (string PlayerId, string Token)? IdentityFor(ServerAddress address)
    {
        if (!Identities.TryGetValue(address.ToString(), out var stored)) return null;
        var token = Unprotect(stored.ProtectedToken);
        return stored.PlayerId.Length > 0 && token.Length > 0 ? (stored.PlayerId, token) : null;
    }

    public void RememberIdentity(ServerAddress address, string playerId, string token, string worldId = "") =>
        Identities[address.ToString()] = new StoredIdentity { PlayerId = playerId, ProtectedToken = Protect(token), WorldId = worldId };

    /// <summary>Compte déjà créé sur le même monde sous une autre adresse (IP changée, LAN puis Internet).
    /// null si aucun ou ambigu : on ne choisit jamais entre deux comptes.</summary>
    public (string Endpoint, string PlayerId, string Token)? IdentityForWorld(string worldId, ServerAddress address, string exceptPlayerId)
    {
        if (worldId.Length is 0 or > 64) return null;
        var key = address.ToString();
        var matches = Identities
            .Where(e => e.Key != key && e.Value.WorldId == worldId && e.Value.PlayerId.Length > 0 && e.Value.PlayerId != exceptPlayerId)
            .Select(e => (Endpoint: e.Key, e.Value.PlayerId, Token: Unprotect(e.Value.ProtectedToken)))
            .Where(m => m.Token.Length > 0).ToArray();
        return matches.Select(m => m.PlayerId).Distinct(StringComparer.Ordinal).Count() == 1 ? matches[0] : null;
    }

    /// <summary>Le contenu chiffré porte aussi son endpoint : déplacer une entrée ne partage pas son secret.</summary>
    public string PasswordFor(ServerAddress address)
    {
        var key = ServerDirectory.Key(address);
        ServerPasswords ??= [];
        if (!ServerPasswords.TryGetValue(key, out var value) || string.IsNullOrEmpty(value)) return "";
        try
        {
            var entry = JsonSerializer.Deserialize<EndpointPassword>(Unprotect(value));
            return entry?.Endpoint == key ? entry.Password ?? "" : "";
        }
        catch (JsonException) { return ""; }
    }

    public void RememberServerPassword(ServerAddress address, string password, bool consent)
    {
        var key = ServerDirectory.Key(address);
        ServerPasswords ??= [];
        if (consent && password.Length > 0)
            ServerPasswords[key] = Protect(JsonSerializer.Serialize(new EndpointPassword(key, password)));
        else ServerPasswords.Remove(key);
        // L'ancien mot de passe global ne possède pas d'identité serveur prouvée : jamais réutilisé.
        ProtectedPassword = "";
    }

    sealed record EndpointPassword(string Endpoint, string? Password);
}

/// <summary>Configuration lue par le mod (même format que client/sims4_mod/s4t/config.py) : il se connecte au pont local.</summary>
/// <remarks>AllowedSaveSlotId : slot_id de la sauvegarde que le joueur a choisie pour ce personnage ; seule elle échappe aux listes.</remarks>
public sealed record SoloGuard(string CharacterId, IReadOnlyList<string> BlockedHouseholdIds, IReadOnlyList<string> BlockedSimIds, string? AllowedSaveSlotId = null);

public static class ModBridgeConfig
{
    public static string DefaultFolder => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "S4Together", "mod");

    public static string IdentityFileName(string endpoint) =>
        "identity-" + Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(endpoint))) + ".json";

    /// <summary>Écrit config.json (endpoint du pont, auto-connexion) et l'identité du pont. Écritures atomiques.</summary>
    public static void Write(Uri bridgeEndpoint, string playerName, IReadOnlyList<string> dlcs, string playerId, string bridgeToken, string? folder = null, PlayIntent? intent = null, SoloGuard? soloGuard = null)
    {
        var dir = folder ?? DefaultFolder;
        Directory.CreateDirectory(dir);
        var endpoint = bridgeEndpoint.ToString();
        var config = new Dictionary<string, object>
        {
            ["endpoint"] = endpoint,
            ["name"] = playerName,
            ["dlcs"] = dlcs,
            ["allow_lan"] = false,
            ["launcher_bridge"] = true,
        };
        if (intent is not null) config["play_intent"] = new Dictionary<string, string> { ["mode"] = intent.Mode, ["id"] = intent.Id };
        if (soloGuard is not null)
        {
            var guard = new Dictionary<string, object> { ["characterId"] = soloGuard.CharacterId, ["blockedHouseholdIds"] = soloGuard.BlockedHouseholdIds, ["blockedSimIds"] = soloGuard.BlockedSimIds };
            if (soloGuard.AllowedSaveSlotId is not null) guard["allowedSaveSlotId"] = soloGuard.AllowedSaveSlotId;
            config["solo_guard"] = guard;
        }
        WriteAtomic(Path.Combine(dir, "config.json"), JsonSerializer.Serialize(config));
        WriteAtomic(Path.Combine(dir, IdentityFileName(endpoint)), JsonSerializer.Serialize(new { playerId, token = bridgeToken }));
    }

    /// <summary>Le launcher se ferme : le mod ne doit plus tenter l'auto-connexion vers un pont disparu.</summary>
    public static void DisableAutoConnect(string? folder = null)
    {
        var path = Path.Combine(folder ?? DefaultFolder, "config.json");
        if (!File.Exists(path)) return;
        try
        {
            var node = System.Text.Json.Nodes.JsonNode.Parse(File.ReadAllText(path)) as System.Text.Json.Nodes.JsonObject;
            if (node is null || node["launcher_bridge"] is null) return;
            node["launcher_bridge"] = false;
            WriteAtomic(path, node.ToJsonString());
        }
        catch (JsonException) { }
    }

    static void WriteAtomic(string path, string content)
    {
        var temporary = path + ".launcher-new";
        File.WriteAllText(temporary, content, new UTF8Encoding(false));
        File.Move(temporary, path, true);
    }
}
