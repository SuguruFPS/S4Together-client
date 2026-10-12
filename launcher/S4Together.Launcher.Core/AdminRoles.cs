using System.Text.Json.Nodes;

namespace S4Together.Launcher.Core;

/// <summary>
/// Droits d'administration tels que le serveur les déclare (<c>admin.permissions.get</c>).
/// Le launcher n'en déduit rien : il affiche ce que le serveur accorde, et le serveur revérifie chaque action.
/// </summary>
public sealed record AdminAccess(string Role, IReadOnlyList<string> AssignableRoles, IReadOnlyList<string> Sanctions, SanctionLimits Limits, bool CanResetSeason = false)
{
    public bool CanManageRoles => AssignableRoles.Count > 0;
    public bool CanSanction => Sanctions.Count > 0;
    public bool HasAdminTab => CanManageRoles || CanSanction || CanResetSeason;

    /// <summary>Réponse absente, refusée ou sans rôle attribuable : aucun accès admin.</summary>
    public static AdminAccess? FromReply(ServerReply? reply)
    {
        if (reply is not { Ok: true } || reply.Payload is not { } payload) return null;
        var role = payload["role"] is JsonValue r && r.TryGetValue<string>(out var s) ? s : "";
        if (role.Length == 0) return null;
        var sanctions = Texts(payload["sanctions"]).Where(AdminSanctions.Kinds.Contains).ToList();
        return new AdminAccess(role, Texts(payload["assignableRoles"]), sanctions, SanctionLimits.FromJson(payload["sanctionLimits"] as JsonObject),
            Texts(payload["permissions"]).Contains("admin.season.reset"));
    }

    static List<string> Texts(JsonNode? node) => (node as JsonArray ?? [])
        .Select(n => n is JsonValue v && v.TryGetValue<string>(out var x) ? x : null)
        .OfType<string>()
        .Where(x => x.Length > 0)
        .Distinct(StringComparer.Ordinal)
        .ToList();

    public static string Label(string role) => role switch
    {
        "player" => "joueur",
        "moderator" => "modérateur",
        "admin" => "administrateur",
        "owner" => "propriétaire",
        _ => role,
    };
}
