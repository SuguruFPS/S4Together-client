using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using S4Together.Launcher.Core;

namespace S4Together.Launcher;

/// <summary>Ami affiché dans le panneau de droite ; « en ligne » = présent sur le serveur où l'on est connecté.</summary>
public sealed record ShellFriend(string Name, bool Online)
{
    public string Initial => Name.Length > 0 ? Name[..1].ToUpperInvariant() : "?";
    public string Detail => Online ? "Sur ton serveur" : "Hors ligne";
    public Visibility OnlineVisibility => Online ? Visibility.Visible : Visibility.Collapsed;
}

// Cadre façon EA app : barre d'outils, menu « Mon jeu » et panneau profil / amis / partie en ligne.
public partial class MainWindow
{
    enum Readiness { GameMissing, ModMissing, Ready, Connected }

    Readiness CurrentReadiness() =>
        link is not null ? Readiness.Connected
        : SelectedGame is null ? Readiness.GameMissing
        : package is null || CurrentModState() is not InstallState.UpToDate ? Readiness.ModMissing
        : Readiness.Ready;

    void UpdateShell()
    {
        var name = settings.PlayerName.Trim();
        HomeGreeting.Text = name.Length > 0 ? $"Content de te revoir, {name}." : "Bienvenue sur S4Together.";
        ProfileName.Text = name.Length > 0 ? name : "Joueur";
        ProfileInitial.Text = name.Length > 0 ? name[..1].ToUpperInvariant() : "?";

        var readiness = CurrentReadiness();
        var (label, brushKey) = readiness switch
        {
            Readiness.Connected => ("Connecté au serveur", "Online"),
            Readiness.Ready => ("Prêt à jouer", "Online"),
            Readiness.ModMissing => ("Mod à installer", "Warn"),
            _ => ("Jeu introuvable", "Warn"),
        };
        var brush = (Brush)FindResource(brushKey);
        var ok = readiness is Readiness.Ready or Readiness.Connected;
        ToolbarStatus.Text = label;
        ToolbarStatus.Foreground = ok ? (Brush)FindResource("ReadyText") : brush;
        ToolbarDot.Fill = MyGameDot.Fill = brush;
        MyGameState.Text = label;

        ProfileState.Text = link is null ? "● Hors ligne" : "● En ligne";
        ProfileDot.Fill = (Brush)FindResource(link is null ? "MutedBrush" : "Online");

        PartyTitle.Text = link is null ? "Prêt à rejoindre tes amis." : "Connecté avec ton personnage.";
        PartyText.Text = link is null
            ? "Choisis ton personnage après avoir rejoint le serveur."
            : $"Serveur : {link.Address.DisplayEndpoint}.";
        PartyButton.Content = link is null ? "Rejoindre →" : "Déconnecter";
        PartyButton.IsEnabled = !busy;
        UpdateFriends();
    }

    void UpdateFriends()
    {
        var present = (PlayerList.ItemsSource as IEnumerable<Choice> ?? [])
            .Select(p => p.Label).ToHashSet(StringComparer.CurrentCultureIgnoreCase);
        var friends = settings.Favorites
            .Select(f => new ShellFriend(f, link is not null && present.Contains(f)))
            .OrderByDescending(f => f.Online).ThenBy(f => f.Name, StringComparer.CurrentCultureIgnoreCase).ToList();
        ShellFriendList.ItemsSource = friends;
        FriendsCount.Text = friends.Count == 0 ? "" : $"{friends.Count(f => f.Online)} / {friends.Count}";
        ShellFriendsEmpty.Visibility = friends.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    void Back_Click(object sender, RoutedEventArgs e) => NavTo("PageHome");

    void OpenProfile_Click(object sender, RoutedEventArgs e) => NavTo("PageSettings");

    void SearchBox_TextChanged(object sender, TextChangedEventArgs e) =>
        SearchHint.Visibility = SearchBox.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;

    // Entrée : ouvre la liste des serveurs filtrée par le texte saisi.
    void SearchBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter) return;
        e.Handled = true;
        DirectoryFilter.Text = SearchBox.Text.Trim();
        NavTo("PageServer");
    }

    void DirectoryFilter_Changed(object sender, RoutedEventArgs e)
    {
        if (IsInitialized && ServerDirectoryList is not null) RefreshDirectory();
    }

    void ApplyName_Click(object sender, RoutedEventArgs e)
    {
        var name = SettingsNameBox.Text.Trim();
        if (name.Length is < 2 or > 40) { SettingsNameStatus.Text = "Le pseudo doit faire entre 2 et 40 caractères."; return; }
        NameBox.Text = name;
        settings.PlayerName = name;
        SaveSettings();
        SettingsNameStatus.Text = link is null ? "Pseudo enregistré." : "Pseudo enregistré : il sera utilisé à la prochaine connexion.";
        UpdateShell();
    }

    // Aucun hébergement n'est lancé ici : le serveur S4Together reste un paquet séparé.
    void Host_Click(object sender, RoutedEventArgs e) =>
        HostStatus.Text = "L'hébergement depuis le launcher n'est pas encore disponible. Pour héberger, installe le paquet serveur S4Together ; "
            + "tes amis le rejoindront ensuite avec « Connexion directe ».";

    void PartyButton_Click(object sender, RoutedEventArgs e)
    {
        if (link is null) HomeJoin_Click(sender, e);
        else Connect_Click(sender, e);
    }

    void AddShellFriend_Click(object sender, RoutedEventArgs e)
    {
        if (link is null) { Status("Connecte-toi à un serveur pour ajouter des joueurs à tes amis."); return; }
        NavTo("PageCommunity");
    }

    void ShellFriendMenu_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string friend } button) return;
        var remove = new MenuItem { Header = "Retirer des amis" };
        remove.Click += (_, _) =>
        {
            settings.Favorites.Remove(friend);
            FriendList.ItemsSource = settings.Favorites.ToList();
            SaveSettings();
            UpdateFriends();
        };
        button.ContextMenu = new ContextMenu { Items = { remove }, PlacementTarget = button, IsOpen = true };
    }
}
