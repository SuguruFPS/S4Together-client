using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace S4Together.Launcher.Core;

/// <summary>
/// Personnage serveur (T3, contrat <c>character.*</c> de shared/protocol/EXTENSIONS.md). Le launcher crée et
/// sélectionne ; le mod relie le foyer du jeu (bind) et envoie les checkpoints. Le serveur fait autorité.
/// </summary>
public sealed record CharacterInfo(string Id, string Name, string Origin, string Status, int? Money, string MoneyPolicy,
    bool HouseImported, string? LotId, int? CheckpointFunds)
{
    public bool Bound => Status == "bound";
    public IReadOnlyList<string> GameSimIds { get; init; } = [];
    public string? GameHouseholdId { get; init; }
    public long Revision { get; init; } = 1;

    public string Label(string? selectedId)
    {
        var origin = Origin == "import" ? "importé" : "nouveau Sim";
        var state = Bound ? "lié au jeu" : "à placer dans le jeu";
        var money = (CheckpointFunds ?? Money) is { } m ? $" · {m:N0} §" : MoneyPolicy == "imported" ? " · argent de ta partie" : "";
        return $"{(Id == selectedId ? "▶ " : "")}{Name} ({origin}, {state}){money}";
    }

    public static CharacterInfo? Parse(JsonNode? node)
    {
        if (node is not JsonObject c || Text(c["characterId"]) is not { } id || !CharacterRoster.IsUuid(id) || Text(c["name"]) is not { } name
            || Text(c["origin"]) is not ("cas" or "import") || Text(c["status"]) is not ("pending_game" or "bound")) return null;
        int? checkpoint = c["checkpoint"] is JsonObject cp ? Int(cp["funds"]) : null;
        return new(id, name, Text(c["origin"])!, Text(c["status"])!, Int(c["money"]), Text(c["moneyPolicy"]) ?? "server_start",
            c["houseImported"] is JsonValue h && h.TryGetValue<bool>(out var house) && house, Text(c["lotId"]), checkpoint)
        { GameSimIds = (c["binding"]?["simIds"] as JsonArray ?? []).Select(Text).OfType<string>().ToList(), GameHouseholdId = Text(c["binding"]?["gameHouseholdId"]), Revision = c["revision"] is JsonValue revision && revision.TryGetValue<long>(out var r) ? r : 1 };
    }

    static string? Text(JsonNode? node) => node is JsonValue v && v.TryGetValue<string>(out var s) && s.Length > 0 ? s : null;
    static int? Int(JsonNode? node) => node is JsonValue v && v.TryGetValue<int>(out var i) ? i : null;
}

public sealed record CharacterRoster(IReadOnlyList<CharacterInfo> Characters, string? SelectedId, int StartingMoney, int MaxCharacters)
{
    public const int NameMax = 40;
    static readonly Regex Uuid = new("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", RegexOptions.CultureInvariant);

    public static bool IsUuid(string value) => Uuid.IsMatch(value);

    public CharacterInfo? Selected => Characters.FirstOrDefault(c => c.Id == SelectedId);
    public bool Full => Characters.Count >= MaxCharacters;

    public CharacterInfo? BoundFor(LocalSim sim) => Characters.FirstOrDefault(c => c.Bound &&
        c.GameHouseholdId == sim.HouseholdId.ToString(System.Globalization.CultureInfo.InvariantCulture) &&
        c.GameSimIds.Contains(sim.SimId.ToString(System.Globalization.CultureInfo.InvariantCulture)));

    /// <summary>ACK de <c>character.list</c> ; null hors contrat.</summary>
    public static CharacterRoster? Parse(JsonObject? payload)
    {
        if (payload?["characters"] is not JsonArray list) return null;
        var selected = payload["selectedCharacterId"] is JsonValue s && s.TryGetValue<string>(out var id) && IsUuid(id) ? id : null;
        var start = payload["startingMoney"] is JsonValue m && m.TryGetValue<int>(out var money) ? money : 0;
        var max = payload["maxCharacters"] is JsonValue x && x.TryGetValue<int>(out var limit) ? limit : 4;
        return new(list.Select(CharacterInfo.Parse).OfType<CharacterInfo>().ToList(), selected, start, max);
    }

    /// <summary>Payload exact de <c>character.create</c>, ou message d'erreur en français.</summary>
    public static (JsonObject? Payload, string Error) CreatePayload(string name, bool import, bool keepMoney, bool keepHouse, string? lotId)
    {
        var trimmed = name.Trim();
        if (trimmed.Length == 0 || name.Length > NameMax || trimmed.Any(char.IsControl)) return (null, $"Donne un nom de 1 à {NameMax} caractères.");
        if (keepHouse && !import) return (null, "Garder la maison n'a de sens que pour un Sim importé.");
        if (keepHouse && string.IsNullOrEmpty(lotId)) return (null, "Choisis le terrain où tu placeras ta maison.");
        var payload = new JsonObject { ["name"] = name, ["origin"] = import ? "import" : "cas", ["keepMoney"] = keepMoney, ["keepHouse"] = keepHouse };
        if (!string.IsNullOrEmpty(lotId)) payload["lotId"] = lotId;
        return (payload, "");
    }

    /// <summary>Ce que le joueur doit faire dans le jeu pour le personnage sélectionné.</summary>
    public static PlayIntent Intent(CharacterInfo? selected) => selected switch
    {
        null => PlayIntent.For(ownsHousehold: false),
        { Bound: true } => PlayIntent.For(ownsHousehold: true),
        { Origin: "import" } => PlayIntent.Import(selected.HouseImported),
        _ => PlayIntent.For(ownsHousehold: false),
    };
}
