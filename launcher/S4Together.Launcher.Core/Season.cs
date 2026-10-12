using System.Text.Json.Nodes;

namespace S4Together.Launcher.Core;

/// <summary>
/// Saison initiale commune (#138, extension saison v1) telle que le serveur la publie dans
/// <c>snapshot.season</c>, les ACK <c>season.*</c> et les diffusions <c>season.state</c> / <c>season.chosen</c>.
/// Le serveur reste l'autorité : le launcher n'affiche le choix qu'au détenteur du bail qui déclare EP05.
/// </summary>
public sealed record SeasonState(string Status, string? Season, string? Owner, DateTimeOffset? ExpiresAt, long Sequence)
{
    public const string RequiredPack = "EP05";
    public static readonly IReadOnlyList<string> Seasons = ["spring", "summer", "autumn", "winter"];

    /// <summary>Objet saison v1 et séquence du monde ; null si absent ou hors contrat (on garde l'état connu).</summary>
    public static SeasonState? Parse(JsonNode? season, JsonNode? sequence)
    {
        if (season is not JsonObject s || Text(s["status"]) is not { } status || !TryLong(sequence, out var seq)) return null;
        if (s["v"] is not JsonValue v || !v.TryGetValue<int>(out var version) || version != 1) return null;
        return status switch
        {
            "none" => new(status, null, null, null, seq),
            "pending" when Text(s["owner"]) is { } owner && DateTimeOffset.TryParse(Text(s["expiresAt"]), out var expires)
                => new(status, null, owner, expires, seq),
            "chosen" when Text(s["season"]) is { } chosen && Seasons.Contains(chosen) => new(status, chosen, null, null, seq),
            _ => null,
        };
    }

    /// <summary>Lit <c>{season, sequence}</c> (snapshot, ACK ou diffusion).</summary>
    public static SeasonState? From(JsonObject? payload) => payload is null ? null : Parse(payload["season"], payload["sequence"]);

    /// <summary>Un événement plus ancien que l'état affiché est ignoré ; même séquence acceptée (ACK puis diffusion).</summary>
    public static bool Supersedes(SeasonState? current, SeasonState incoming) => current is null || incoming.Sequence >= current.Sequence;

    public static bool HasPack(IEnumerable<string> dlcs) => dlcs.Contains(RequiredPack, StringComparer.OrdinalIgnoreCase);

    public bool CanChoose(string? me, IEnumerable<string> dlcs) => Status == "pending" && me is not null && Owner == me && HasPack(dlcs);

    public string Describe(string? me, IEnumerable<string> dlcs)
    {
        if (!HasPack(dlcs)) return "Saisons non installé : la partie se joue sans saison partagée.";
        return Status switch
        {
            "chosen" => $"Saison de départ du monde : {Label(Season!)}. Elle ne sera plus redemandée.",
            "pending" when Owner == me => "Tu es le premier joueur : choisis la saison de départ du monde.",
            "pending" => "Un autre joueur choisit la saison de départ du monde…",
            _ => "Aucune saison choisie pour l'instant.",
        };
    }

    public static string Label(string season) => season switch
    {
        "spring" => "printemps",
        "summer" => "été",
        "autumn" => "automne",
        "winter" => "hiver",
        _ => season,
    };

    static string? Text(JsonNode? node) => node is JsonValue v && v.TryGetValue<string>(out var s) && s.Length > 0 ? s : null;
    static bool TryLong(JsonNode? node, out long value)
    {
        value = 0;
        return node is JsonValue v && v.TryGetValue(out value) && value >= 0;
    }
}
