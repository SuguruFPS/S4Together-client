namespace S4Together.Launcher.Core;

/// <summary>Répertoire local explicite, sans découverte réseau ni publication des serveurs privés.</summary>
public sealed class KnownServer
{
    public string Name { get; set; } = "";
    public string Host { get; set; } = "";
    public int Port { get; set; }
    public bool Secure { get; set; }
    public string RoutePrefix { get; set; } = "";
    public bool Favorite { get; set; }
    public DateTimeOffset? LastConnectedUtc { get; set; }
    /// <summary>Nom public annoncé par le serveur (/health) ; affiché tant que le joueur n'a pas nommé la fiche.</summary>
    public string AnnouncedName { get; set; } = "";
    [System.Text.Json.Serialization.JsonIgnore]
    public string DisplayName => ServerDirectory.DisplayName(this);
    [System.Text.Json.Serialization.JsonIgnore]
    public ServerAddress Address => new(Host, Port, Secure, ServerAddress.ScopeOf(Host)) { RoutePrefix = RoutePrefix };
    [System.Text.Json.Serialization.JsonIgnore]
    public string Endpoint => ServerDirectory.Key(Address);
}

public static class ServerDirectory
{
    public const int MaxServers = 64;
    public static string Key(ServerAddress address) => address.ToString().ToLowerInvariant();

    public static KnownServer Remember(List<KnownServer> servers, ServerAddress address, string name)
    {
        address.RequireSafeTransport();
        var found = servers.FirstOrDefault(s => s.Endpoint == Key(address));
        if (found is null)
        {
            if (servers.Count >= MaxServers) throw new InvalidOperationException("Le répertoire contient déjà 64 serveurs. Retire une fiche avant d'en ajouter une.");
            found = new() { Host = address.Host, Port = address.Port, Secure = address.Secure, RoutePrefix = address.RoutePrefix };
            servers.Add(found);
        }
        found.Name = string.IsNullOrWhiteSpace(name) ? address.Host : name.Trim()[..Math.Min(name.Trim().Length, 80)];
        return found;
    }

    public const string DefaultLabel = "Mon serveur";
    public const int MaxAnnouncedName = 48;

    /// <summary>Nom choisi par le joueur, sinon nom annoncé par le serveur, sinon l'hôte.</summary>
    public static string DisplayName(KnownServer server)
    {
        var custom = server.Name.Trim();
        var generic = custom.Length == 0 || custom == DefaultLabel || custom.Equals(server.Host, StringComparison.OrdinalIgnoreCase);
        var announced = CleanAnnouncedName(server.AnnouncedName);
        return generic && announced.Length > 0 ? announced : custom.Length > 0 ? custom : server.Host;
    }

    /// <summary>Le nom vient du réseau : caractères de contrôle et d'inversion d'affichage retirés, longueur bornée.</summary>
    public static string CleanAnnouncedName(string? name)
    {
        if (string.IsNullOrWhiteSpace(name)) return "";
        var runes = new List<System.Text.Rune>();
        var space = false;
        foreach (var rune in name.EnumerateRunes())
        {
            if (System.Text.Rune.IsWhiteSpace(rune)) { space = runes.Count > 0; continue; }
            if (System.Text.Rune.GetUnicodeCategory(rune) is System.Globalization.UnicodeCategory.Control or System.Globalization.UnicodeCategory.Format
                or System.Globalization.UnicodeCategory.OtherNotAssigned || rune == System.Text.Rune.ReplacementChar) continue;
            if (runes.Count >= MaxAnnouncedName) break;
            if (space) { runes.Add(new System.Text.Rune(' ')); space = false; if (runes.Count >= MaxAnnouncedName) break; }
            runes.Add(rune);
        }
        return string.Concat(runes.Select(r => r.ToString())).TrimEnd();
    }

    /// <summary>Retient le nom annoncé ; vrai si la fiche a changé (à enregistrer).</summary>
    public static bool RememberAnnouncedName(IEnumerable<KnownServer> servers, ServerAddress address, string? announced)
    {
        var clean = CleanAnnouncedName(announced);
        var found = servers.FirstOrDefault(s => s.Endpoint == Key(address));
        if (found is null || clean.Length == 0 || found.AnnouncedName == clean) return false;
        found.AnnouncedName = clean;
        return true;
    }

    public static IReadOnlyList<KnownServer> Visible(IEnumerable<KnownServer> servers) => servers
        .Where(Valid).DistinctBy(s => s.Endpoint).Take(MaxServers)
        .OrderByDescending(s => s.Favorite).ThenByDescending(s => s.LastConnectedUtc).ThenBy(s => s.Name).ToArray();

    static bool Valid(KnownServer? server)
    {
        if (server is null || server.Port is < 1 or > 65535 || string.IsNullOrWhiteSpace(server.Host) || server.Host.Length > 253
            || Uri.CheckHostName(server.Host) == UriHostNameType.Unknown) return false;
        try { server.Address.RequireSafeTransport(); return true; }
        catch (FormatException) { return false; }
    }
}
