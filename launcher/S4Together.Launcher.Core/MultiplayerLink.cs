using System.Diagnostics;
using System.Text.Json.Nodes;

namespace S4Together.Launcher.Core;

/// <summary>
/// Liaison complète : session serveur du launcher + pont local du mod. Le launcher reste
/// l'unique client réseau du serveur ; le mod passe par lui.
/// </summary>
public sealed class MultiplayerLink : IBridgeUpstream, IAsyncDisposable
{
    public const int PreferredBridgePort = 47015;

    public ServerSession Session { get; }
    public LocalBridge Bridge { get; }
    public ServerAddress Address { get; }
    public string PlayerId => Session.Hello!.PlayerId;

    public event Action<JsonObject>? EventReceived;
    public event Action<string>? Closed;

    MultiplayerLink(ServerSession session, ServerAddress address, Func<IBridgeUpstream, LocalBridge> bridgeFactory)
    {
        Session = session;
        Address = address;
        Bridge = bridgeFactory(this);
        session.EventReceived += message =>
        {
            EventReceived?.Invoke(message);
            _ = Bridge.DeliverEventAsync(message);
        };
        session.ForeignReplyReceived += reply => _ = Bridge.DeliverReplyAsync(reply);
        session.Closed += reason =>
        {
            Bridge.DropMod();
            Closed?.Invoke(reason);
        };
    }

    public static async Task<MultiplayerLink> ConnectAsync(ServerAddress address, string name, IReadOnlyList<string> dlcs, string? password,
        (string PlayerId, string Token)? identity, int bridgePort = PreferredBridgePort, CancellationToken cancel = default)
    {
        var session = await ServerSession.ConnectAsync(address, name, dlcs, password, identity, cancel);
        return new MultiplayerLink(session, address, upstream => LocalBridge.Start(upstream, bridgePort));
    }

    Task IBridgeUpstream.ForwardAsync(JsonObject envelope, CancellationToken cancel) => Session.ForwardAsync(envelope, cancel);

    async Task<JsonObject> IBridgeUpstream.SnapshotAsync(CancellationToken cancel)
    {
        var reply = await Session.RequestAsync("snapshot", cancel: cancel);
        return reply.Ok && reply.Payload is { } snapshot ? (JsonObject)snapshot.DeepClone() : (JsonObject)Session.Hello!.Snapshot.DeepClone();
    }

    public async ValueTask DisposeAsync()
    {
        await Bridge.DisposeAsync();
        await Session.DisposeAsync();
    }
}

/// <summary>Lancement du jeu par la boutique d'origine (Steam doit lancer sa propre copie).</summary>
public static class GameLauncher
{
    public static ProcessStartInfo StartInfo(GameInstall install) => install.Store == GameStore.Steam
        ? new ProcessStartInfo($"steam://rungameid/{GameLocator.SteamAppId}") { UseShellExecute = true }
        : new ProcessStartInfo(install.Executable) { UseShellExecute = true, WorkingDirectory = Path.GetDirectoryName(install.Executable)! };

    public static void Launch(GameInstall install)
    {
        if (install.Store != GameStore.Steam && !File.Exists(install.Executable)) throw new FileNotFoundException("TS4_x64.exe introuvable : vérifie le dossier du jeu.");
        using var _ = Process.Start(StartInfo(install));
    }
}
