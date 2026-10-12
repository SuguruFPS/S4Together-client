namespace S4Together.Launcher.Core;

/// <summary>Consigne visible avant lancement ; aucun chargement de sauvegarde ni accès au profil EA.</summary>
public sealed record GameStartPlan(string Action, string Details, bool ResumeFound)
{
    public static GameStartPlan For(CharacterInfo character, MultiplayerSave? save)
    {
        if (character.Bound)
            return save is null
                ? new("Charger la partie", "Sauvegarde multijoueur introuvable sur ce PC. Choisis ta partie multijoueur dans le jeu ; ta partie solo ne sera pas liée.", false)
                : new($"Charger la partie → {save.Name}", "Choisis cette sauvegarde dans Les Sims 4. « Reprendre » convient uniquement si le menu affiche cette partie.", true);
        return character.Origin == "import"
            ? new("Nouvelle partie", "Dans Créer un Sim : Galerie → Ma bibliothèque → ton foyer. Choisis ensuite le monde et la maison, puis enregistre une partie multijoueur distincte.", false)
            : new("Nouvelle partie", "Crée ton Sim dans Créer un Sim, choisis le monde et la maison, puis enregistre une partie multijoueur distincte.", false);
    }
}
