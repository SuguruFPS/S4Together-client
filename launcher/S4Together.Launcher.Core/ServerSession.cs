using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace S4Together.Launcher.Core;

/// <summary>Réponse du serveur à une requête : ack (Ok) ou error (Code, Message).</summary>
public sealed record ServerReply(bool Ok, JsonObject Raw)
{
    public string Code => Raw["code"]?.GetValue<string>() ?? "";
    public string Message => Raw["message"]?.GetValue<string>() ?? "";
    public JsonObject? Payload => Raw["payload"] as JsonObject;
}

public sealed record HelloResult(string PlayerId, string Token, string SessionId, JsonObject Snapshot);

public sealed class ServerRejectedException(string code, string message) : Exception(message)
{
    public string Code { get; } = code;
}

/// <summary>Messages d'erreur du protocole en français pour l'interface.</summary>
public static class ProtocolErrors
{
    /// <summary>Message d'une réponse refusée ; un bannissement indique son échéance.</summary>
    public static string French(ServerReply reply) =>
        reply.Code == "banned" ? AdminSanctions.BannedMessage(reply.Payload) : French(reply.Code, reply.Message);

    public static string French(string code, string fallback = "") => code switch
    {
        "invalid_password" => "Mot de passe du serveur incorrect.",
        "too_many_attempts" => "Trop de mots de passe faux : réessaie dans 15 minutes.",
        "server_full" => "Le serveur est plein.",
        "unauthorized" => "Identité refusée par le serveur.",
        "forbidden" => "Action non autorisée.",
        "dlc_missing" => "Il te manque un pack requis pour ce terrain.",
        "lot_taken" => "Ce terrain est déjà pris.",
        "stale_revision" => "Objet modifié entre-temps : réessaie.",
        "storage_unavailable" => "Stockage du serveur indisponible : réessaie plus tard.",
        "residence_exists" => "Ce foyer a déjà une résidence principale.",
        "rate_limited" => "Trop de requêtes : patiente un instant.",
        "invalid_request" => "Requête refusée par le serveur (données invalides ou trop nombreuses).",
        "unknown_type" => "Cette fonction nécessite une mise à jour du serveur. Clique « Actualiser » après sa mise à jour.",
        "role_forbidden" => "Ton rôle ne permet pas cette action.",
        "last_owner" => "Impossible de retirer le dernier propriétaire du serveur.",
        "muted" => "Tu es réduit au silence sur ce serveur pour le moment.",
        "build_suspended" => "Ta construction est suspendue sur ce serveur pour le moment.",
        "banned" => "Tu es banni de ce serveur.",
        "mods_required" => "Il te manque des mods du serveur (ou une ancienne version). Reconnecte-toi avec Les Sims 4 fermé pour les installer.",
        "sanction_inactive" => "Cette sanction est déjà levée ou expirée.",
        "sanction_limit" => "Trop de sanctions actives enregistrées sur ce serveur.",
        "not_found" => "Élément introuvable sur le serveur.",
        "invalid_season" => "Saison inconnue.",
        "season_not_owner" => "Un autre joueur choisit la saison, ou elle est déjà choisie.",
        "timeout" => "Le serveur n'a pas répondu à temps.",
        "disconnected" => "Connexion au serveur perdue.",
        _ => fallback.Length > 0 ? fallback : $"Refus du serveur ({code}).",
    };
}

/// <summary>
/// Session WebSocket unique entre le launcher et le serveur de jeu (protocole v1).
/// Les envois sont sérialisés ; les réponses sont routées par id ; les événements sont publiés à part.
/// </summary>
public sealed class ServerSession : IAsyncDisposable
{
    public const int MaxMessageBytes = 1 << 20;
    static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(15);
    static readonly TimeSpan PingInterval = TimeSpan.FromSeconds(20);

    readonly WebSocket socket;
    readonly SemaphoreSlim sendLock = new(1, 1);
    readonly ConcurrentDictionary<string, TaskCompletionSource<ServerReply>> pending = new();
    readonly CancellationTokenSource life = new();
    Task? receiveLoop, pingLoop;
    int disposing;

    public HelloResult? Hello { get; private set; }
    public bool Connected => socket.State == WebSocketState.Open && !life.IsCancellationRequested;

    /// <summary>Événement serveur (message sans id de requête) : players, chat, object…</summary>
    public event Action<JsonObject>? EventReceived;
    /// <summary>Réponse à une requête relayée pour quelqu'un d'autre (pont du mod).</summary>
    public event Action<JsonObject>? ForeignReplyReceived;
    public event Action<string>? Closed;

    ServerSession(WebSocket socket) => this.socket = socket;

    public static async Task<ServerSession> ConnectAsync(ServerAddress address, string name, IReadOnlyList<string> dlcs,
        string? password, (string PlayerId, string Token)? identity, CancellationToken cancel = default, IReadOnlyList<ServerModDeclaration>? mods = null)
    {
        address.RequireSafeTransport();
        var client = new ClientWebSocket();
        client.Options.Proxy = null;
        client.Options.KeepAliveInterval = TimeSpan.FromSeconds(30);
        try
        {
            using var connectTimeout = CancellationTokenSource.CreateLinkedTokenSource(cancel);
            connectTimeout.CancelAfter(TimeSpan.FromSeconds(8));
            await client.ConnectAsync(address.WebSocketUri, connectTimeout.Token);
        }
        catch
        {
            client.Dispose();
            throw;
        }
        var session = new ServerSession(client);
        session.Start();
        var payload = new JsonObject { ["name"] = name, ["dlcs"] = new JsonArray(dlcs.Select(d => (JsonNode)d).ToArray()), ["transportChunking"] = 1 };
        if (!string.IsNullOrEmpty(password)) payload["password"] = password;
        if (identity is { } id) { payload["playerId"] = id.PlayerId; payload["token"] = id.Token; }
        if (mods is { Count: > 0 })
            payload["mods"] = new JsonArray(mods.Select(m => (JsonNode)new JsonObject { ["id"] = m.Id, ["version"] = m.Version, ["hash"] = m.Hash }).ToArray());
        ServerReply reply;
        try { reply = await session.RequestAsync("hello", payload, cancel: cancel); }
        catch { await session.DisposeAsync(); throw; }
        if (!reply.Ok)
        {
            await session.DisposeAsync();
            throw new ServerRejectedException(reply.Code, ProtocolErrors.French(reply));
        }
        var p = reply.Payload;
        string? Text(string key) => p?[key] is JsonValue v && v.TryGetValue<string>(out var s) && s.Length is > 0 and <= 512 ? s : null;
        if (Text("playerId") is not { } playerId || Text("token") is not { } token || Text("sessionId") is not { } sessionId || p!["snapshot"] is not JsonObject snapshot)
        {
            await session.DisposeAsync();
            throw new ServerRejectedException("invalid_hello", "Réponse HELLO invalide du serveur.");
        }
        session.Hello = new HelloResult(playerId, token, sessionId, (JsonObject)snapshot.DeepClone());
        return session;
    }

    void Start()
    {
        receiveLoop = Task.Run(ReceiveAsync);
        pingLoop = Task.Run(PingAsync);
    }

    public Task<ServerReply> RequestAsync(string type, JsonObject? payload = null, string? id = null, CancellationToken cancel = default) =>
        SendRequestAsync(new JsonObject { ["v"] = 1, ["id"] = id ?? Guid.NewGuid().ToString(), ["type"] = type, ["payload"] = payload ?? new JsonObject() }, cancel);

    async Task<ServerReply> SendRequestAsync(JsonObject envelope, CancellationToken cancel)
    {
        var id = envelope["id"]!.GetValue<string>();
        var waiter = new TaskCompletionSource<ServerReply>(TaskCreationOptions.RunContinuationsAsynchronously);
        if (!pending.TryAdd(id, waiter)) throw new InvalidOperationException("Identifiant de requête déjà en attente.");
        try
        {
            await SendRawAsync(envelope, cancel);
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancel, life.Token);
            timeout.CancelAfter(RequestTimeout);
            try { return await waiter.Task.WaitAsync(timeout.Token); }
            catch (OperationCanceledException) when (!cancel.IsCancellationRequested)
            {
                throw new ServerRejectedException(life.IsCancellationRequested ? "disconnected" : "timeout",
                    ProtocolErrors.French(life.IsCancellationRequested ? "disconnected" : "timeout"));
            }
        }
        finally { pending.TryRemove(id, out _); }
    }

    /// <summary>Relais du pont : envoie la requête du mod telle quelle ; la réponse arrive via ForeignReplyReceived.</summary>
    public Task ForwardAsync(JsonObject envelope, CancellationToken cancel = default) => SendRawAsync(envelope, cancel);

    async Task SendRawAsync(JsonObject envelope, CancellationToken cancel)
    {
        var bytes = Encoding.UTF8.GetBytes(envelope.ToJsonString());
        if (bytes.Length > MaxMessageBytes) throw new InvalidOperationException("Message trop grand.");
        await sendLock.WaitAsync(cancel);
        try { await socket.SendAsync(bytes, WebSocketMessageType.Text, true, cancel); }
        finally { sendLock.Release(); }
    }

    async Task ReceiveAsync()
    {
        var reason = "disconnected";
        try
        {
            while (!life.IsCancellationRequested)
            {
                var text = await ReadMessageAsync(socket, life.Token, allowChunks:true);
                if (text is null) break;
                if (JsonNode.Parse(text) is not JsonObject message || message["v"]?.GetValue<int>() != 1 || message["type"] is null)
                {
                    reason = "protocol";
                    break;
                }
                var id = message["id"] is JsonValue idValue && idValue.TryGetValue<string>(out var s) ? s : null;
                if (id is null) { EventReceived?.Invoke(message); continue; }
                var ok = message["ok"] is JsonValue okValue && okValue.TryGetValue<bool>(out var b) && b;
                if (pending.TryGetValue(id, out var waiter)) waiter.TrySetResult(new ServerReply(ok, message));
                else ForeignReplyReceived?.Invoke(message);
            }
        }
        catch (Exception e) when (e is WebSocketException or JsonException or OperationCanceledException or InvalidOperationException or FormatException or IOException or DecoderFallbackException)
        {
            reason = e is OperationCanceledException ? "closed" : "disconnected";
        }
        finally
        {
            life.Cancel();
            foreach (var waiter in pending.Values) waiter.TrySetCanceled();
            Closed?.Invoke(reason);
        }
    }

    async Task PingAsync()
    {
        try
        {
            while (!life.IsCancellationRequested)
            {
                await Task.Delay(PingInterval, life.Token);
                await RequestAsync("ping", cancel: life.Token);
            }
        }
        catch (Exception e) when (e is OperationCanceledException or ServerRejectedException or WebSocketException or InvalidOperationException) { }
    }

    /// <summary>Lit un message texte complet, borné ; null si fermeture.</summary>
    internal static async Task<string?> ReadMessageAsync(WebSocket socket, CancellationToken cancel, bool allowChunks=false)
    {
        if(!allowChunks) return await ReadPhysicalAsync(socket,cancel);
        using var assembler=new S4Together.Transport.ChunkAssembler();
        using var timeout=CancellationTokenSource.CreateLinkedTokenSource(cancel);
        var started=false;
        while(true)
        {
            var physical=await ReadPhysicalAsync(socket,timeout.Token);
            if(physical is null) { if(assembler.Active)throw new InvalidDataException("Transmission incomplète.");return null; }
            var complete=assembler.Accept(physical);if(complete is not null)return complete;
            if(!assembler.Active)throw new InvalidDataException("Transmission invalide.");
            if(!started) { timeout.CancelAfter(TimeSpan.FromSeconds(30)); started=true; }
        }
    }
    static async Task<string?> ReadPhysicalAsync(WebSocket socket, CancellationToken cancel)
    {
        var buffer = new byte[16384];
        using var stream = new MemoryStream();
        while (true)
        {
            var result = await socket.ReceiveAsync(buffer, cancel);
            if (result.MessageType == WebSocketMessageType.Close) return null;
            if (result.MessageType != WebSocketMessageType.Text) throw new InvalidOperationException("Message binaire refusé.");
            stream.Write(buffer, 0, result.Count);
            if (stream.Length > MaxMessageBytes) throw new InvalidOperationException("Message trop grand.");
            if (result.EndOfMessage) return Encoding.UTF8.GetString(stream.GetBuffer(), 0, (int)stream.Length);
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref disposing, 1) != 0) return;
        if (socket.State == WebSocketState.Open)
        {
            using var closeTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            try { await socket.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "bye", closeTimeout.Token); }
            catch (Exception e) when (e is WebSocketException or OperationCanceledException) { }
            // Attendre la fin côté serveur avant de reconnecter la même identité.
            if (receiveLoop is not null) await Task.WhenAny(receiveLoop, Task.Delay(2000));
        }
        life.Cancel();
        socket.Abort();
        if (receiveLoop is not null) await Task.WhenAny(receiveLoop, Task.Delay(2000));
        if (pingLoop is not null) await Task.WhenAny(pingLoop, Task.Delay(500));
        socket.Dispose();
        sendLock.Dispose();
        life.Dispose();
    }
}
