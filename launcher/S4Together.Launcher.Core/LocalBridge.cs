using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace S4Together.Launcher.Core;

/// <summary>Ce que le pont attend du serveur : relais des requêtes du mod et instantané à jour.</summary>
public interface IBridgeUpstream
{
    string PlayerId { get; }
    Task ForwardAsync(JsonObject envelope, CancellationToken cancel);
    Task<JsonObject> SnapshotAsync(CancellationToken cancel);
}

/// <summary>
/// Pont local entre le mod du jeu et la session du launcher. Écoute uniquement sur 127.0.0.1,
/// un seul mod à la fois, jeton secret aléatoire vérifié en temps constant. Le mod n'a jamais
/// le mot de passe ni le jeton serveur ; le TLS éventuel est fait par le launcher.
/// </summary>
public sealed class LocalBridge : IAsyncDisposable
{
    const int MaxHeaderBytes = 8192;
    static readonly TimeSpan HandshakeTimeout = TimeSpan.FromSeconds(5);
    static readonly TimeSpan HelloTimeout = TimeSpan.FromSeconds(10);
    static readonly HashSet<string> RelayedTypes =
        ["snapshot", "household.create", "household.select", "property.claim", "property.release", "property.move", "visit", "chat", "object.set", "ping", "ack",
         "character.resume", "character.bind", "character.checkpoint", "game.join", "game.state", "game.leave", "game.snapshot", "game.interaction", "game.speed.get", "game.speed.set", "game.appearance.publish", "game.appearance.list", "game.appearance.get", "game.visibility.report", "zone.snapshot", "terrain.publish", "terrain.get", "terrain.catalog"];

    readonly TcpListener listener;
    readonly IBridgeUpstream upstream;
    readonly byte[] secret;
    readonly CancellationTokenSource life = new();
    readonly SemaphoreSlim sendLock = new(1, 1);
    readonly Dictionary<string, string> modRequests = [];
    readonly Lock gate = new();
    WebSocket? mod;
    readonly System.Collections.Concurrent.ConcurrentDictionary<WebSocket,bool> chunking=new();
    Task? acceptLoop;

    public int Port { get; }
    /// <summary>Jeton à écrire dans l'identité du mod. Secret local : jamais journalisé.</summary>
    public string Token { get; }
    public bool ModConnected { get { lock (gate) return mod is { State: WebSocketState.Open }; } }
    public Uri Endpoint => new($"ws://127.0.0.1:{Port}/ws");

    public event Action<bool>? ModConnectionChanged;
    public event Action<string>? Rejected;
    /// <summary>Refus corrélé à une requête réelle du mod ; aucun texte libre du serveur.</summary>
    public event Action<string, string>? RequestRejected;

    LocalBridge(TcpListener listener, IBridgeUpstream upstream)
    {
        this.listener = listener;
        this.upstream = upstream;
        Port = ((IPEndPoint)listener.LocalEndpoint).Port;
        secret = RandomNumberGenerator.GetBytes(32);
        Token = Convert.ToHexStringLower(secret);
    }

    /// <summary>Port préféré s'il est libre, sinon un port choisi par Windows.</summary>
    public static LocalBridge Start(IBridgeUpstream upstream, int preferredPort = 0)
    {
        TcpListener listener;
        try
        {
            listener = new TcpListener(IPAddress.Loopback, preferredPort);
            listener.Server.ExclusiveAddressUse = true;
            listener.Start(4);
        }
        catch (SocketException) when (preferredPort != 0)
        {
            listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Server.ExclusiveAddressUse = true;
            listener.Start(4);
        }
        var bridge = new LocalBridge(listener, upstream);
        bridge.acceptLoop = Task.Run(bridge.AcceptAsync);
        return bridge;
    }

    async Task AcceptAsync()
    {
        while (!life.IsCancellationRequested)
        {
            TcpClient client;
            try { client = await listener.AcceptTcpClientAsync(life.Token); }
            catch (Exception e) when (e is OperationCanceledException or SocketException or ObjectDisposedException) { return; }
            _ = Task.Run(() => ServeAsync(client));
        }
    }

    async Task ServeAsync(TcpClient client)
    {
        using var _ = client;
        WebSocket? socket = null;
        try
        {
            client.NoDelay = true;
            var stream = client.GetStream();
            using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(life.Token))
            {
                timeout.CancelAfter(HandshakeTimeout);
                var key = await ReadUpgradeAsync(stream, Port, timeout.Token);
                if (key is null) { await RejectHttpAsync(stream, timeout.Token); Rejected?.Invoke("handshake"); return; }
                var accept = Convert.ToBase64String(SHA1.HashData(Encoding.ASCII.GetBytes(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
                var response = $"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n";
                await stream.WriteAsync(Encoding.ASCII.GetBytes(response), timeout.Token);
            }
            socket = WebSocket.CreateFromStream(stream, new WebSocketCreationOptions { IsServer = true, KeepAliveInterval = TimeSpan.Zero });
            if (!await HelloAsync(socket)) return;
            lock (gate)
            {
                if (mod is { State: WebSocketState.Open }) mod.Abort();
                mod = socket;
                modRequests.Clear();
            }
            ModConnectionChanged?.Invoke(true);
            await RelayFromModAsync(socket);
        }
        catch (Exception e) when (e is IOException or WebSocketException or OperationCanceledException or SocketException or InvalidOperationException or System.Text.Json.JsonException) { }
        finally
        {
            var wasCurrent = false;
            lock (gate)
            {
                if (ReferenceEquals(mod, socket)) { mod = null; wasCurrent = true; }
            }
            if(socket is not null) chunking.TryRemove(socket,out var removedCapability);
            socket?.Abort();
            if (wasCurrent) ModConnectionChanged?.Invoke(false);
        }
    }

    /// <summary>
    /// Requête d'upgrade minimale : GET /ws, Host local, aucune Origin (un navigateur en envoie toujours : refusé).
    /// Renvoie la clé WebSocket, ou null si la requête est refusée.
    /// </summary>
    internal static async Task<string?> ReadUpgradeAsync(Stream stream, int port, CancellationToken cancel)
    {
        var buffer = new byte[MaxHeaderBytes];
        var length = 0;
        while (true)
        {
            if (length == buffer.Length) return null;
            var read = await stream.ReadAsync(buffer.AsMemory(length, 1), cancel);
            if (read == 0) return null;
            length++;
            if (length >= 4 && buffer[length - 4] == '\r' && buffer[length - 3] == '\n' && buffer[length - 2] == '\r' && buffer[length - 1] == '\n') break;
        }
        var lines = Encoding.ASCII.GetString(buffer, 0, length - 4).Split("\r\n");
        if (lines[0] != "GET /ws HTTP/1.1") return null;
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in lines.Skip(1))
        {
            var colon = line.IndexOf(':');
            if (colon <= 0 || !headers.TryAdd(line[..colon].Trim(), line[(colon + 1)..].Trim())) return null;
        }
        if (headers.ContainsKey("Origin")) return null;
        if (!headers.TryGetValue("Host", out var host) || (host != $"127.0.0.1:{port}" && host != $"localhost:{port}")) return null;
        if (!headers.TryGetValue("Upgrade", out var upgrade) || !upgrade.Equals("websocket", StringComparison.OrdinalIgnoreCase)) return null;
        if (!headers.TryGetValue("Connection", out var connection) || !connection.Split(',').Any(c => c.Trim().Equals("upgrade", StringComparison.OrdinalIgnoreCase))) return null;
        if (!headers.TryGetValue("Sec-WebSocket-Version", out var version) || version != "13") return null;
        if (!headers.TryGetValue("Sec-WebSocket-Key", out var key)) return null;
        try { if (Convert.FromBase64String(key).Length != 16) return null; }
        catch (FormatException) { return null; }
        return key;
    }

    static Task RejectHttpAsync(Stream stream, CancellationToken cancel) =>
        stream.WriteAsync(Encoding.ASCII.GetBytes("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"), cancel).AsTask();

    /// <summary>HELLO du mod : jeton du pont et playerId du compte du launcher. Réponse locale avec un instantané frais du serveur.</summary>
    async Task<bool> HelloAsync(WebSocket socket)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(life.Token);
        timeout.CancelAfter(HelloTimeout);
        var text = await ServerSession.ReadMessageAsync(socket, timeout.Token);
        if (text is null || JsonNode.Parse(text) is not JsonObject hello || hello["type"]?.GetValue<string>() != "hello" || hello["id"] is not JsonValue idNode
            || !idNode.TryGetValue<string>(out var id) || id.Length is 0 or > 128)
        {
            Rejected?.Invoke("hello");
            return false;
        }
        var payload = hello["payload"] as JsonObject;
        var token = payload?["token"] is JsonValue t && t.TryGetValue<string>(out var s) ? s : "";
        var playerId = payload?["playerId"] is JsonValue p && p.TryGetValue<string>(out var pid) ? pid : "";
        var tokenOk = CryptographicOperations.FixedTimeEquals(SHA256.HashData(Encoding.UTF8.GetBytes(token)), SHA256.HashData(Encoding.ASCII.GetBytes(Token)));
        if (!tokenOk || playerId != upstream.PlayerId)
        {
            await SendToAsync(socket, new JsonObject { ["v"] = 1, ["id"] = id, ["type"] = "error", ["ok"] = false, ["code"] = "unauthorized", ["message"] = "Identité du pont refusée." }, timeout.Token);
            Rejected?.Invoke("token");
            return false;
        }
        var supports=payload?["transportChunking"] is JsonValue capability && capability.TryGetValue<int>(out var chunkVersion) && chunkVersion==1;
        if(payload?.ContainsKey("transportChunking")==true && !supports) { Rejected?.Invoke("chunking"); return false; }
        chunking[socket]=supports;
        var snapshot = await upstream.SnapshotAsync(timeout.Token);
        await SendToAsync(socket, new JsonObject
        {
            ["v"] = 1, ["id"] = id, ["type"] = "ack", ["ok"] = true,
            ["payload"] = new JsonObject { ["playerId"] = upstream.PlayerId, ["token"] = Token, ["sessionId"] = "bridge-" + Guid.NewGuid().ToString("N"), ["snapshot"] = snapshot },
        }, timeout.Token);
        return true;
    }

    async Task RelayFromModAsync(WebSocket socket)
    {
        while (!life.IsCancellationRequested)
        {
            var text = await ServerSession.ReadMessageAsync(socket, life.Token);
            if (text is null) return;
            if (JsonNode.Parse(text) is not JsonObject request || request["v"]?.GetValue<int>() != 1
                || request["id"] is not JsonValue idNode || !idNode.TryGetValue<string>(out var id) || id.Length is 0 or > 128
                || request["type"] is not JsonValue typeNode || !typeNode.TryGetValue<string>(out var type))
                return;
            if (!RelayedTypes.Contains(type))
            {
                await SendToAsync(socket, new JsonObject { ["v"] = 1, ["id"] = id, ["type"] = "error", ["ok"] = false, ["code"] = "forbidden", ["message"] = "Requête non relayée." }, life.Token);
                continue;
            }
            lock (gate) modRequests[id] = type;
            try { await upstream.ForwardAsync(request, life.Token); }
            catch (Exception e) when (e is WebSocketException or InvalidOperationException or ObjectDisposedException)
            {
                lock (gate) modRequests.Remove(id);
                await SendToAsync(socket, new JsonObject { ["v"] = 1, ["id"] = id, ["type"] = "error", ["ok"] = false, ["code"] = "disconnected", ["message"] = "Serveur injoignable." }, life.Token);
            }
        }
    }

    /// <summary>Réponse du serveur à une requête du mod : renvoyée au mod seulement si elle lui appartient.</summary>
    public async Task DeliverReplyAsync(JsonObject reply)
    {
        var id = reply["id"] is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
        WebSocket? target;
        string? requestType;
        lock (gate)
        {
            if (id is null || !modRequests.Remove(id, out requestType)) return;
            target = mod;
        }
        // Le refus atteint toujours le mod avant le retour UI. Ne jamais consommer son ACK pour afficher un statut.
        if (target is not null) await SafeSendAsync(target, reply);
        if (reply["ok"] is JsonValue ok && ok.TryGetValue<bool>(out var accepted) && !accepted
            && reply["code"] is JsonValue value && value.TryGetValue<string>(out var code)
            && code.Length is > 0 and <= 40 && code.All(c => c is >= 'a' and <= 'z' or '_'))
            RequestRejected?.Invoke(requestType, code);
    }

    /// <summary>Événement serveur diffusé au mod connecté.</summary>
    public Task DeliverEventAsync(JsonObject message)
    {
        WebSocket? target;
        lock (gate) target = mod;
        return target is null ? Task.CompletedTask : SafeSendAsync(target, message);
    }

    async Task SafeSendAsync(WebSocket socket, JsonObject message)
    {
        try { await SendToAsync(socket, message, life.Token); }
        catch (Exception e) when (e is WebSocketException or OperationCanceledException or ObjectDisposedException or IOException) { socket.Abort(); }
    }

    async Task SendToAsync(WebSocket socket, JsonObject message, CancellationToken cancel)
    {
        var bytes = Encoding.UTF8.GetBytes(message.ToJsonString());
        await sendLock.WaitAsync(cancel);
        try { foreach(var frame in S4Together.Transport.ChunkTransport.Encode(bytes,chunking.GetValueOrDefault(socket))) await socket.SendAsync(frame, WebSocketMessageType.Text, true, cancel); }
        finally { sendLock.Release(); }
    }

    /// <summary>Coupe le mod (déconnexion du launcher) sans arrêter l'écoute.</summary>
    public void DropMod()
    {
        lock (gate) mod?.Abort();
    }

    public async ValueTask DisposeAsync()
    {
        life.Cancel();
        listener.Stop();
        DropMod();
        if (acceptLoop is not null) await Task.WhenAny(acceptLoop, Task.Delay(1000));
        CryptographicOperations.ZeroMemory(secret);
    }
}
