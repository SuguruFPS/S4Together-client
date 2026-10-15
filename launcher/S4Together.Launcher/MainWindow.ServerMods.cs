using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using S4Together.Launcher.Core;

namespace S4Together.Launcher;

public partial class MainWindow
{
    readonly DispatcherTimer serverModTimer = new() { Interval = TimeSpan.FromSeconds(3) };
    readonly HashSet<string> serverModFolders = new(StringComparer.OrdinalIgnoreCase);
    bool reviewingServerMods;
    string? activeServerModFolder;

    void StartServerModMaintenance()
    {
        serverModTimer.Tick += async (_, _) => await FinishServerModRemovalAsync();
        serverModTimer.Start();
    }

    async Task FinishServerModRemovalAsync()
    {
        if (busy || reviewingServerMods || quitting || link is not null || GameLocator.IsGameRunning()) return;
        var current = ModsFolder;
        if (GameLocator.ValidateModsFolder(current).Length == 0) serverModFolders.Add(current);
        reviewingServerMods = true;
        busy = true;
        try
        {
            foreach (var folder in serverModFolders.ToArray())
            {
                var result = await Task.Run(() => ServerModSync.CompletePendingRemoval(folder, GameLocator.IsGameRunning()));
                if (result.Removed > 0 || result.Preserved > 0)
                    StatusBar.Text = $"Mods serveur retirés : {result.Removed}. Fichiers modifiés conservés : {result.Preserved}.";
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ServerModSyncException)
        { StatusBar.Text = "Retrait des mods en attente : " + ex.Message; serverModTimer.Stop(); }
        finally { busy = false; reviewingServerMods = false; }
    }

    async void ManageServerMods_Click(object sender, RoutedEventArgs e)
    {
        if (link is not null) { StatusBar.Text = "Déconnecte-toi du serveur avant de gérer ses mods."; return; }
        await ReviewServerModsAsync(manual: true);
    }

    async Task ReviewServerModsAsync(bool manual = false)
    {
        if (busy || reviewingServerMods) return;
        var folder = manual ? ModsFolder : activeServerModFolder ?? ModsFolder;
        if (GameLocator.ValidateModsFolder(folder).Length > 0) return;
        reviewingServerMods = true;
        busy = true;
        try
        {
            serverModFolders.Add(folder);
            var info = await Task.Run(() => ServerModSync.Installed(folder, includeRetained: manual));
            if (info is null || info.Count == 0 || (info.RemovalPending && !manual))
            { if (manual) StatusBar.Text = "Aucun mod serveur à gérer dans ce profil."; return; }
            var keep = true; // Fermer la fenêtre conserve les fichiers.
            var panel = new StackPanel { Margin = new Thickness(20) };
            var dialog = new Window { Owner = this, Title = "Mods du serveur", Width = 490,
                SizeToContent = SizeToContent.Height, ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = Background, Foreground = Foreground, Content = panel };
            panel.Children.Add(new TextBlock { Text = $"{info.Count} mod(s) installé(s) pour ce serveur. Les garder pour tes autres parties ?",
                TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 12) });
            panel.Children.Add(new TextBlock { Text = "Le retrait attend la fermeture des Sims 4. Les fichiers personnels ou modifiés sont conservés.",
                TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 16) });
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var retain = new Button { Content = "Garder", IsDefault = true };
            var remove = new Button { Content = "Retirer les mods" };
            retain.Click += (_, _) => dialog.Close();
            remove.Click += (_, _) => { keep = false; dialog.Close(); };
            buttons.Children.Add(retain); buttons.Children.Add(remove); panel.Children.Add(buttons);
            dialog.ShowDialog();
            var result = await Task.Run(() => ServerModSync.Leave(folder, info.Endpoint, keep, GameLocator.IsGameRunning()));
            StatusBar.Text = keep ? "Mods serveur conservés. Tu peux les retirer dans Paramètres."
                : result.Deferred ? "Retrait prévu à la fermeture du jeu ; si tu quittes le launcher, il reprendra au prochain lancement."
                : $"Mods serveur retirés : {result.Removed}. Fichiers modifiés conservés : {result.Preserved}.";
            serverModTimer.Start();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ServerModSyncException)
        { StatusBar.Text = "Gestion des mods : " + ex.Message; }
        finally { busy = false; reviewingServerMods = false; }
    }
}
