using System.Text.Json;
using System.Net;
using System.Net.Sockets;

namespace S4Together.Launcher.Core;

/// <summary>Annuaire officiel publié volontairement. Aucun scan ni envoi de favoris/adresses locales.</summary>
public static class PublicServerCatalog
{
    public const string Url = "https://raw.githubusercontent.com/SuguruFPS/S4Together-client/main/servers.json";
    public const int MaxBytes = 64 * 1024;

    public static IReadOnlyList<KnownServer> Parse(ReadOnlyMemory<byte> data)
    {
        if (data.Length > MaxBytes) throw new FormatException("Catalogue de serveurs trop grand.");
        try
        {
            using var doc = JsonDocument.Parse(data, new JsonDocumentOptions { MaxDepth = 8 });
            var root = doc.RootElement;
            if (root.GetProperty("format").GetInt32() != 1) throw new FormatException();
            var entries = root.GetProperty("servers").EnumerateArray().ToArray();
            if (entries.Length > ServerDirectory.MaxServers) throw new FormatException();
            var result = new List<KnownServer>();
            foreach (var entry in entries)
            {
                if (entry.GetProperty("public").ValueKind != JsonValueKind.True) throw new FormatException();
                var host = entry.GetProperty("address").GetString() ?? "";
                var port = entry.GetProperty("port").GetInt32();
                var name = entry.GetProperty("name").GetString() ?? "";
                if (host.Length is < 1 or > 253 || name.Length is < 1 or > 80 || port is < 1 or > 65535) throw new FormatException();
                var address = ServerAddress.Parse(host, port.ToString(System.Globalization.CultureInfo.InvariantCulture));
                if (address.Scope != AddressScope.Internet || !PublicHost(address.Host)) throw new FormatException();
                address.RequireSafeTransport();
                result.Add(new KnownServer { Name = ServerDirectory.CleanAnnouncedName(name), Host = address.Host,
                    Port = address.Port, Secure = address.Secure, RoutePrefix = address.RoutePrefix });
            }
            return result.DistinctBy(s => s.Endpoint).ToArray();
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException or OverflowException)
        { throw new FormatException("Catalogue de serveurs invalide."); }
    }

    static bool PublicHost(string host)
    {
        if (!IPAddress.TryParse(host, out var ip))
            return host.Contains('.') && !new[] { ".local", ".lan", ".internal", ".localhost" }.Any(s => host.EndsWith(s, StringComparison.OrdinalIgnoreCase));
        if (ip.IsIPv4MappedToIPv6) ip = ip.MapToIPv4();
        if (IPAddress.IsLoopback(ip) || ip.Equals(IPAddress.Any) || ip.Equals(IPAddress.IPv6Any)) return false;
        var b = ip.GetAddressBytes();
        if (ip.AddressFamily == AddressFamily.InterNetworkV6)
            return !ip.IsIPv6LinkLocal && !ip.IsIPv6SiteLocal && !ip.IsIPv6Multicast && (b[0] & 0xfe) != 0xfc;
        return b[0] is not (0 or 10 or 127) && b[0] < 224
            && !(b[0] == 169 && b[1] == 254) && !(b[0] == 172 && b[1] is >= 16 and <= 31)
            && !(b[0] == 192 && b[1] == 168) && !(b[0] == 100 && b[1] is >= 64 and <= 127);
    }

    public static async Task<IReadOnlyList<KnownServer>> FetchAsync(HttpMessageHandler? handler = null, CancellationToken cancel = default)
    {
        using var http = handler is null ? new HttpClient(new SocketsHttpHandler { AllowAutoRedirect = false, UseProxy = false, UseCookies = false })
            : new HttpClient(handler, false);
        http.Timeout = TimeSpan.FromSeconds(5);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancel);
        deadline.CancelAfter(TimeSpan.FromSeconds(5));
        using var response = await http.GetAsync(Url, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
        response.EnsureSuccessStatusCode();
        if (response.Content.Headers.ContentLength > MaxBytes) throw new FormatException("Catalogue de serveurs trop grand.");
        await using var stream = await response.Content.ReadAsStreamAsync(deadline.Token);
        using var buffer = new MemoryStream();
        var block = new byte[4096];
        int read;
        while ((read = await stream.ReadAsync(block, deadline.Token)) > 0)
        {
            if (buffer.Length + read > MaxBytes) throw new FormatException("Catalogue de serveurs trop grand.");
            buffer.Write(block, 0, read);
        }
        return Parse(buffer.ToArray());
    }

    public static IReadOnlyList<KnownServer> Display(IEnumerable<KnownServer> local, IEnumerable<KnownServer> published)
    {
        var known = ServerDirectory.Visible(local);
        var favorites = known.Where(s => s.Favorite).ToArray();
        var rest = published.Select(p => known.FirstOrDefault(s => s.Endpoint == p.Endpoint) ?? p)
            .Where(s => !s.Favorite).DistinctBy(s => s.Endpoint).OrderBy(s => s.DisplayName);
        return favorites.Concat(rest).DistinctBy(s => s.Endpoint).ToArray();
    }
}
