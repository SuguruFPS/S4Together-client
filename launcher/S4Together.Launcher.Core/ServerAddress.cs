using System.Net;
using System.Net.Sockets;
using System.Text.Json;

namespace S4Together.Launcher.Core;

public enum AddressScope { ThisPc, Lan, Internet }

/// <summary>Adresse du serveur de jeu. Le launcher parle au serveur ; jamais à SuguruDB.</summary>
public sealed record ServerAddress(string Host, int Port, bool Secure, AddressScope Scope)
{
    public const int DefaultPort = 27015;
    public string RoutePrefix { get; init; } = "";

    string RoutePath => RoutePrefix is "" or "/sims" ? RoutePrefix : throw new FormatException("Route serveur invalide.");
    public Uri WebSocketUri => new($"{(Secure ? "wss" : "ws")}://{HostForUri}:{Port}{RoutePath}/ws");
    public Uri HealthUri => new($"{(Secure ? "https" : "http")}://{HostForUri}:{Port}{RoutePath}/health");
    string HostForUri => IPAddress.TryParse(Host, out var ip) && ip.AddressFamily == AddressFamily.InterNetworkV6 ? $"[{Host}]" : Host;
    public override string ToString() => $"{(Secure ? "wss" : "ws")}://{HostForUri}:{Port}{RoutePath}";
    public string DisplayHost => HostForUri + RoutePath;
    public string DisplayEndpoint => $"{HostForUri}:{Port}{RoutePath}";

    public static ServerAddress ParseForm(string? host, string? port, ServerAddress? retained)
    {
        var parsed = Parse(host, port);
        if (!(host ?? "").Contains("://") && retained is { Secure: true } &&
            parsed.Host.Equals(retained.Host, StringComparison.OrdinalIgnoreCase))
            parsed = parsed with { Secure = true };
        return parsed;
    }

    /// <summary>
    /// IP locale/publique ou domaine avec port ; wss:// conserve le TLS vérifié par Windows.
    /// Le TLS est fait par le launcher : le Python du jeu n'a pas de module ssl.
    /// </summary>
    public static ServerAddress Parse(string? input, string? portText)
    {
        var text = (input ?? "").Trim();
        if (text.Length == 0) throw new FormatException("Saisis l'adresse du serveur.");
        bool secure = false;
        var explicitScheme = text.Contains("://");
        if (text.Contains("://"))
        {
            if (text.StartsWith("wss://", StringComparison.OrdinalIgnoreCase)) { secure = true; text = text[6..]; }
            else if (text.StartsWith("ws://", StringComparison.OrdinalIgnoreCase)) text = text[5..];
            else throw new FormatException("Adresse : utilise une IP, un nom de domaine ou wss://domaine.");
        }
        text = text.TrimEnd('/');
        if (text.EndsWith("/ws", StringComparison.OrdinalIgnoreCase)) text = text[..^3];
        var route = text.EndsWith("/sims", StringComparison.Ordinal) ? "/sims" : "";
        if (route.Length > 0) text = text[..^route.Length];
        if (text.Contains('/') || text.Contains('?') || text.Contains('@') || text.Contains('#')) throw new FormatException("Adresse : pas de chemin, d'identifiant ni de paramètre.");
        // wss : port dans l'adresse (443 par défaut) ; la case Port (27015) ne sert qu'au réseau local.
        var port = secure ? 443 : ParsePort(portText, DefaultPort);
        var embedded = text.LastIndexOf(':');
        if (text.StartsWith('['))
        {
            var close = text.IndexOf(']');
            if (close < 0) throw new FormatException("Adresse IPv6 invalide.");
            var suffix = text[(close + 1)..];
            if (suffix.Length > 0)
            {
                if (!suffix.StartsWith(':')) throw new FormatException("Adresse IPv6 invalide.");
                port = ParsePort(suffix[1..], DefaultPort);
            }
            text = text[1..close];
        }
        else if (embedded > 0 && !text.Contains(']') && text.Count(c => c == ':') == 1)
        {
            port = ParsePort(text[(embedded + 1)..], DefaultPort);
            text = text[..embedded];
        }
        text = text.Trim('[', ']');
        if (text.Length is 0 or > 253) throw new FormatException("Adresse invalide.");
        var scope = ScopeOf(text);
        if (Uri.CheckHostName(text) == UriHostNameType.Unknown) throw new FormatException("Adresse du serveur invalide.");
        // Une IP publique reste acceptée, avec TLS automatique. Une demande explicite en clair est refusée.
        if (scope == AddressScope.Internet && !explicitScheme) secure = true;
        var address = new ServerAddress(text, port, secure, scope) { RoutePrefix = route };
        address.RequireSafeTransport();
        return address;
    }

    public void RequireSafeTransport()
    {
        _ = RoutePath;
        if (!Secure && ScopeOf(Host) == AddressScope.Internet)
            throw new FormatException("Connexion Internet : utilise wss:// avec un certificat valide. Le mot de passe ne peut pas être envoyé en clair.");
    }

    static int ParsePort(string? text, int fallback)
    {
        if (string.IsNullOrWhiteSpace(text)) return fallback;
        if (!int.TryParse(text.Trim(), out var port) || port is < 1 or > 65535) throw new FormatException("Port invalide (1 à 65535).");
        return port;
    }

    public static AddressScope ScopeOf(string host)
    {
        if (host.Equals("localhost", StringComparison.OrdinalIgnoreCase)) return AddressScope.ThisPc;
        if (!IPAddress.TryParse(host, out var ip)) return AddressScope.Internet;
        if (IPAddress.IsLoopback(ip)) return AddressScope.ThisPc;
        // Forme canonique uniquement : « 192.168.01.2 » est refusé comme IP privée.
        if (ip.AddressFamily != AddressFamily.InterNetwork || ip.ToString() != host) return AddressScope.Internet;
        var b = ip.GetAddressBytes();
        return b[0] == 10 || (b[0] == 172 && b[1] is >= 16 and <= 31) || (b[0] == 192 && b[1] == 168) ? AddressScope.Lan : AddressScope.Internet;
    }
}

public enum HealthCode { Ok, Unreachable, Timeout, NotS4Together, ProtocolMismatch, TlsFailure }

public sealed record HealthResult(HealthCode Code, string Message, int? ServerProtocol = null, long? LatencyMs = null)
{
    public bool Ok => Code == HealthCode.Ok;
    public string? Name { get; init; }
    public string? Version { get; init; }
    public string? World { get; init; }
    public int? Players { get; init; }
    public int? MaxPlayers { get; init; }
    public bool? PasswordRequired { get; init; }
}

/// <summary>Test du serveur : GET /health (même port que le jeu), protocole comparé. Aucun identifiant envoyé.</summary>
public static class HealthProbe
{
    public const int ProtocolVersion = 1;
    const int MaxBody = 4096;

    public static async Task<HealthResult> CheckAsync(ServerAddress address, TimeSpan timeout, HttpMessageHandler? handler = null, CancellationToken cancel = default)
    {
        address.RequireSafeTransport();
        using var client = handler is null
            ? new HttpClient(new SocketsHttpHandler { AllowAutoRedirect = false, UseProxy = false, UseCookies = false }) { Timeout = timeout }
            : new HttpClient(handler, false) { Timeout = timeout };
        var watch = System.Diagnostics.Stopwatch.StartNew();
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancel);
        deadline.CancelAfter(timeout);
        string body;
        try
        {
            using var response = await client.GetAsync(address.HealthUri, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
            if (!response.IsSuccessStatusCode) return NotS4T("Un service répond sur ce port, mais ce n'est pas un serveur S4Together.");
            if (response.Content.Headers.ContentLength > MaxBody) return NotS4T("Réponse trop grande : ce n'est pas un serveur S4Together.");
            await using var stream = await response.Content.ReadAsStreamAsync(deadline.Token);
            using var bounded = new MemoryStream();
            var block = new byte[MaxBody + 1];
            int read;
            while ((read = await stream.ReadAsync(block, deadline.Token)) > 0)
            {
                if (bounded.Length + read > MaxBody) return NotS4T("Réponse trop grande : ce n'est pas un serveur S4Together.");
                bounded.Write(block, 0, read);
            }
            body = System.Text.Encoding.UTF8.GetString(bounded.ToArray());
        }
        catch (OperationCanceledException) when (!cancel.IsCancellationRequested)
        {
            return new(HealthCode.Timeout, "Le serveur ne répond pas (délai dépassé) : PC serveur allumé ? pare-feu ? bonne IP ?");
        }
        catch (HttpRequestException e) when (e.InnerException is System.Security.Authentication.AuthenticationException)
        {
            return new(HealthCode.TlsFailure, "Connexion publique sécurisée indisponible : le serveur doit fournir le chiffrement et un certificat valide. Le mot de passe n'a pas été envoyé.");
        }
        catch (HttpRequestException e) when (e.HttpRequestError == HttpRequestError.NameResolutionError)
        {
            return new(HealthCode.Unreachable, "Nom de serveur introuvable : vérifie l'adresse.");
        }
        catch (HttpRequestException)
        {
            return new(HealthCode.Unreachable, "Connexion impossible : vérifie l'IP, le port, et que le serveur écoute sur le réseau.");
        }
        catch (IOException) { return new(HealthCode.Unreachable, "Réponse du serveur interrompue."); }
        var latency = watch.ElapsedMilliseconds;
        try
        {
            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("status", out var status) || status.GetString() != "ok")
                return NotS4T("Réponse inattendue : ce n'est pas un serveur S4Together.");
            var protocol = root.TryGetProperty("protocol", out var p) && p.ValueKind == JsonValueKind.Number ? p.GetInt32() : -1;
            if (protocol != ProtocolVersion)
                return new(HealthCode.ProtocolMismatch, $"Version de protocole différente (serveur {protocol}, launcher {ProtocolVersion}) : mets à jour.", protocol, latency);
            string? Text(string key) => root.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String && v.GetString() is { Length: > 0 and <= 80 } text ? text : null;
            int? Number(string key) => root.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out var n) && n is >= 0 and <= 100000 ? n : null;
            return new(HealthCode.Ok, $"Serveur S4Together joignable ({latency} ms).", protocol, latency)
            {
                Name = Text("name"), Version = Text("version"), World = Text("world"), Players = Number("players"), MaxPlayers = Number("maxPlayers"),
                PasswordRequired = root.TryGetProperty("passwordRequired", out var required) && required.ValueKind is JsonValueKind.True or JsonValueKind.False ? required.GetBoolean() : null,
            };
        }
        catch (Exception e) when (e is JsonException or InvalidOperationException or FormatException)
        {
            return NotS4T("Réponse inattendue : ce n'est pas un serveur S4Together.");
        }
    }

    static HealthResult NotS4T(string message) => new(HealthCode.NotS4Together, message);
}
