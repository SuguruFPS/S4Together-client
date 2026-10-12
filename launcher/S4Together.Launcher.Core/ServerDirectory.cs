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
