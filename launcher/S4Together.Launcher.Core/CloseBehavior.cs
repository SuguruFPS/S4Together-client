namespace S4Together.Launcher.Core;

/// <summary>Ce que fait la croix de la fenêtre.</summary>
public enum CloseAction { Quit, HideToTray, Ask }

/// <summary>Choix de l'utilisateur dans l'avertissement de fermeture.</summary>
public enum CloseChoice { HideToTray, Quit }

public static class CloseBehavior
{
    /// <summary>Hors connexion : on quitte. Connecté : avertir, sinon appliquer le choix mémorisé.</summary>
    public static CloseAction Decide(bool connected, bool confirmWhileConnected, bool minimizeToTray) =>
        !connected ? CloseAction.Quit
        : confirmWhileConnected ? CloseAction.Ask
        : minimizeToTray ? CloseAction.HideToTray
        : CloseAction.Quit;

    /// <summary>« Ne plus afficher » : l'avertissement se coupe et le choix devient le comportement de la croix.</summary>
    public static void Remember(LauncherSettings settings, CloseChoice choice, bool dontAskAgain)
    {
        if (!dontAskAgain) return;
        settings.ConfirmCloseWhileConnected = false;
        settings.MinimizeToTray = choice == CloseChoice.HideToTray;
    }
}
