namespace S4Together.Launcher.Core;

/// <summary>
/// Intention « Jouer » transmise au mod par config.json (jamais en ligne de commande).
/// Le menu principal du jeu est piloté par le client C++ : aucune API Python ni option de lancement
/// ne charge une partie ou n'ouvre CAS depuis ce menu (spike #137). Le launcher indique donc le
/// seul clic à faire ; « Nouvelle partie » mène nativement à CAS, puis au choix du monde et du terrain.
/// </summary>
public sealed record PlayIntent(string Mode, string Id, string Guidance)
{
    public const string NewPlayer = "new_player";
    public const string Resume = "resume";

    public static PlayIntent For(bool ownsHousehold) => ownsHousehold
        ? new(Resume, NewId(), "Reprends ta sauvegarde multijoueur. Si « Reprendre » affiche ta partie solo, choisis ta partie multijoueur dans « Charger la partie ».")
        : new(NewPlayer, NewId(), "Dans le menu du jeu, clique « Nouvelle partie » : tu arrives dans Créer un Sim, puis tu choisis le monde et le terrain.");

    /// <summary>Import sans lire la sauvegarde solo : le joueur partage son foyer dans la Bibliothèque native,
    /// puis le place dans la partie S4Together. Le mod relie ce foyer au personnage au chargement.</summary>
    public static PlayIntent Import(bool keepHouse) => new(NewPlayer, NewId(), keepHouse
        ? "Dans ta partie solo, enregistre ton foyer et sa maison dans la Bibliothèque. Puis « Nouvelle partie » : dans Créer un Sim, Galerie → Ma bibliothèque → ton foyer ; ensuite Gérer les mondes → terrain choisi → placer ta maison depuis la Bibliothèque et y emménager."
        : "Dans ta partie solo, enregistre ton foyer dans la Bibliothèque. Puis « Nouvelle partie » : dans Créer un Sim, Galerie → Ma bibliothèque → ton foyer, puis emménage sur le terrain de ton choix.");

    static string NewId() => Guid.NewGuid().ToString("N");
}
