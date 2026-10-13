namespace S4Together.Launcher.Core;

/// <summary>Reconnaissance d'une partie par IDs natifs ; le nom ne suffit jamais.</summary>
public sealed record MultiplayerSave(string FilePath, string Name, DateTime SavedAtUtc)
{
    public static MultiplayerSave? Find(CharacterInfo character, IEnumerable<LocalSim> sims)
    {
        if (!character.Bound || character.GameSimIds.Count == 0) return null;
        var candidates = sims.Where(s => s.Source == "Partie enregistrée" && s.HouseholdId.ToString(System.Globalization.CultureInfo.InvariantCulture) == character.GameHouseholdId)
            .GroupBy(s => s.FilePath, StringComparer.OrdinalIgnoreCase)
            .Where(group => character.GameSimIds.All(id => group.Any(s => s.SimId.ToString(System.Globalization.CultureInfo.InvariantCulture) == id)) &&
                group.All(s => s.IsPet || character.GameSimIds.Contains(s.SimId.ToString(System.Globalization.CultureInfo.InvariantCulture))))
            .SelectMany(group => group);
        var matches = candidates
            .OrderByDescending(s => s.SavedAtUtc).ThenBy(s => s.FilePath, StringComparer.OrdinalIgnoreCase).FirstOrDefault();
        return matches is null ? null : new(matches.FilePath, matches.SaveName, matches.SavedAtUtc);
    }
    public string Guidance => $"Ta partie multijoueur : « {Name} ». Reprends cette partie dans le jeu. Si le menu propose ta partie solo, utilise Charger la partie → {Name}.";
}
