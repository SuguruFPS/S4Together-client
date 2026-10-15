using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Data;
using S4Together.Launcher.Core;

namespace S4Together.Launcher;

public sealed record ServerDirectoryRow(KnownServer Server, HealthResult? Health)
{
    public string Group => Server.Favorite ? "Favoris" : "Serveurs publics";
    public string Endpoint => Server.Endpoint;
    public string DisplayEndpoint => Server.Address.DisplayEndpoint;
    public string Title => (Server.Favorite ? "★ " : "") + Server.DisplayName;
    public string Detail => Health is null ? "Pas encore testé" : !Health.Ok ? Health.Message
        : $"Accessible · {Health.LatencyMs} ms · joueurs {(Health.Players is { } n ? n.ToString() : "?")}/{(Health.MaxPlayers is { } max ? max.ToString() : "?")}"
            + (Health.PasswordRequired is true ? " · 🔒 Mot de passe" : Health.PasswordRequired is false ? " · Sans mot de passe" : " · Protection inconnue")
            + $" · protocole {Health.ServerProtocol}" + (Health.Version is null ? "" : " · " + Health.Version)
            + (Health.World is null ? "" : " · " + Health.World);
}

public partial class MainWindow
{
    readonly Dictionary<string, HealthResult> directoryHealth = [];
    string gameEndpoint = "";
    string passwordEndpoint = "";
    bool refreshingDirectory;
    ServerAddress? retainedFormAddress;
    bool settingServerForm;

    ServerAddress ReadAddress() => ServerAddress.ParseForm(HostBox.Text, PortBox.Text, retainedFormAddress);

    void ShowServerAddress(ServerAddress address)
    {
        var endpoint = ServerDirectory.Key(address);
        if (endpoint != passwordEndpoint)
        {
            PasswordBox.Password = settings.PasswordFor(address);
            RememberBox.IsChecked = PasswordBox.Password.Length > 0;
        }
        passwordEndpoint = endpoint;
        retainedFormAddress = address;
        settingServerForm = true;
        try { HostBox.Text = address.DisplayHost; PortBox.Text = address.Port.ToString(); }
        finally { settingServerForm = false; }
    }

    void InitializeDirectory()
    {
        settings.KnownServers ??= [];
        settings.ServerPasswords ??= [];
        settings.ModSigningKeys ??= [];
        settings.KnownServers = ServerDirectory.Visible(settings.KnownServers).ToList();
        try
        {
            var address = ServerAddress.Parse(HostBox.Text, PortBox.Text);
            ShowServerAddress(address);
            gameEndpoint = passwordEndpoint = ServerDirectory.Key(address);
            if (settings.KnownServers.Count == 0) ServerDirectory.Remember(settings.KnownServers, address, ServerDirectory.DefaultLabel);
            PasswordBox.Password = settings.PasswordFor(address);
            RememberBox.IsChecked = PasswordBox.Password.Length > 0;
        }
        catch (FormatException) { PasswordBox.Password = ""; RememberBox.IsChecked = false; }
        HostBox.TextChanged += ServerEndpoint_Changed;
        PortBox.TextChanged += ServerEndpoint_Changed;
        Loaded += async (_, _) => { await RefreshPublicCatalogAsync(); RefreshDirectory(); };
        RefreshDirectory();
    }

    void ServerEndpoint_Changed(object sender, TextChangedEventArgs e)
    {
        if (settingServerForm) return;
        string next;
        try { next = ServerDirectory.Key(ReadAddress()); }
        catch (FormatException) { next = ""; }
        if (next == passwordEndpoint) return;
        passwordEndpoint = next;
        PasswordBox.Password = "";
        RememberBox.IsChecked = false;
        // Uniquement le secret consenti de cette adresse ; jamais celui de la fiche précédente.
        if (next.Length > 0)
        {
            var address = ReadAddress();
            PasswordBox.Password = settings.PasswordFor(address);
            RememberBox.IsChecked = PasswordBox.Password.Length > 0;
        }
    }

    void RefreshDirectory()
    {
        var selected = (ServerDirectoryList.SelectedItem as ServerDirectoryRow)?.Endpoint;
        var rows = PublicServerCatalog.Display(settings.KnownServers, publicServers)
            .Select(s => new ServerDirectoryRow(s, directoryHealth.GetValueOrDefault(s.Endpoint))).ToList();
        var view = CollectionViewSource.GetDefaultView(rows);
        view.GroupDescriptions.Add(new PropertyGroupDescription(nameof(ServerDirectoryRow.Group)));
        ServerDirectoryList.ItemsSource = view;
        if (selected is not null) ServerDirectoryList.SelectedItem = rows.FirstOrDefault(r => r.Endpoint == selected);
        DirectoryEmpty.Visibility = rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    void Directory_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (busy || ServerDirectoryList.SelectedItem is not ServerDirectoryRow row) return;
        ShowServerAddress(row.Server.Address);
        ServerLabelBox.Text = row.Server.Name;
        PasswordBox.Password = settings.PasswordFor(row.Server.Address);
        RememberBox.IsChecked = PasswordBox.Password.Length > 0;
    }

    async void Directory_DoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (ItemsControl.ContainerFromElement(ServerDirectoryList, e.OriginalSource as DependencyObject) is not ListBoxItem) return;
        await ConnectDirectoryAsync();
    }

    async void Directory_KeyDown(object sender, KeyEventArgs e)
    { if (e.Key == Key.Enter) { e.Handled = true; await ConnectDirectoryAsync(); } }

    async Task ConnectDirectoryAsync()
    {
        if (busy || ServerDirectoryList.SelectedItem is not ServerDirectoryRow) return;
        if (link is not null) { ServerStatus.Text = "Tu es déjà connecté. Déconnecte-toi avant de changer de serveur."; return; }
        await ConnectServerAsync(fromDirectory: true);
    }

    void DirectorySave_Click(object sender, RoutedEventArgs e)
    {
        if (busy) return;
        try
        {
            var address = ReadAddress();
            ServerDirectory.Remember(settings.KnownServers, address, ServerLabelBox.Text);
            SaveSettings(); RefreshDirectory(); ServerStatus.Text = "Serveur enregistré sur ce PC.";
        }
        catch (Exception ex) when (ex is FormatException or InvalidOperationException) { ServerStatus.Text = ex.Message; }
    }

    void DirectoryFavorite_Click(object sender, RoutedEventArgs e)
    {
        if (busy || ServerDirectoryList.SelectedItem is not ServerDirectoryRow row) return;
        try
        {
            var server = settings.KnownServers.FirstOrDefault(s => s.Endpoint == row.Endpoint)
                ?? ServerDirectory.Remember(settings.KnownServers, row.Server.Address, row.Server.DisplayName);
            server.Favorite = !server.Favorite; SaveSettings(); RefreshDirectory();
        }
        catch (InvalidOperationException ex) { ServerStatus.Text = ex.Message; }
    }

    void DirectoryRemove_Click(object sender, RoutedEventArgs e)
    {
        if (busy || ServerDirectoryList.SelectedItem is not ServerDirectoryRow row) return;
        settings.KnownServers.Remove(row.Server); settings.ServerPasswords.Remove(row.Endpoint); settings.ModSigningKeys.Remove(row.Endpoint);
        SaveSettings(); RefreshDirectory(); ServerStatus.Text = "Fiche retirée. Tes personnages restent sur le serveur.";
    }

    async void DirectoryRefresh_Click(object sender, RoutedEventArgs e)
    {
        if (refreshingDirectory || busy) return;
        refreshingDirectory = true; DirectoryRefreshButton.IsEnabled = false;
        try
        {
            await RefreshPublicCatalogAsync();
            using var concurrency = new SemaphoreSlim(4);
            var renamed = false;
            await Task.WhenAll(PublicServerCatalog.Display(settings.KnownServers, publicServers).Select(async server =>
            {
                await concurrency.WaitAsync();
                try
                {
                    var health = await HealthProbe.CheckAsync(server.Address, HealthTimeout);
                    directoryHealth[server.Endpoint] = health;
                    if (health.Ok && ServerDirectory.RememberAnnouncedName(settings.KnownServers, server.Address, health.Name)) renamed = true;
                }
                finally { concurrency.Release(); }
            }));
            if (renamed) SaveSettings();
            RefreshDirectory();
        }
        catch (Exception ex) when (ex is FormatException or System.IO.IOException or InvalidOperationException)
        { ServerStatus.Text = "Test des serveurs impossible : " + ex.Message; }
        finally { refreshingDirectory = false; DirectoryRefreshButton.IsEnabled = true; }
    }

    static string WorldIdOf(MultiplayerLink link) =>
        link.Session.Hello?.Snapshot["worldId"] is JsonValue value && value.TryGetValue(out string? id) ? id : "";

    /// <summary>Nouvelle adresse d'un serveur déjà connu (même worldId) : propose de reprendre l'ancien compte
    /// au lieu du compte vide que le serveur vient de créer. Refus ou échec : on garde le nouveau compte.</summary>
    async Task<MultiplayerLink> ResumeAccountOnSameWorld(MultiplayerLink created, ServerAddress address, IReadOnlyList<string> dlcs, string? password, bool hadIdentity,
        IReadOnlyList<ServerModDeclaration> mods)
    {
        if (hadIdentity) return created;
        var hello = created.Session.Hello!;
        if (settings.IdentityForWorld(WorldIdOf(created), address, hello.PlayerId) is not { } previous) return created;
        var answer = MessageBox.Show(this,
            $"Cette adresse annonce le même monde que {previous.Endpoint}, où tu as déjà un compte.\n\n" +
            "Vérifie que cette nouvelle adresse appartient bien à ton serveur : le nom du monde ne le garantit pas. " +
            "Reprendre le compte transmet son identité à cette adresse.\n\nReprendre ce compte ? (Non = nouveau compte vide)",
            "Même serveur, autre adresse", MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No);
        if (answer != MessageBoxResult.Yes) return created;
        await created.DisposeAsync();
        try { return await MultiplayerLink.ConnectAsync(address, settings.PlayerName, dlcs, password, (previous.PlayerId, previous.Token), mods: mods); }
        catch (ServerRejectedException ex)
        {
            Status("Ancien compte refusé (" + ProtocolErrors.French(ex.Code, ex.Message) + ") : nouveau compte conservé.");
            return await MultiplayerLink.ConnectAsync(address, settings.PlayerName, dlcs, password, (hello.PlayerId, hello.Token), mods: mods);
        }
    }

    bool RequestDirectoryPassword(ServerAddress address)
    {
        var input = new PasswordBox { MaxLength = 128, Password = settings.PasswordFor(address), Margin = new Thickness(0, 10, 0, 10) };
        var remember = new CheckBox { Content = "Mémoriser pour ce serveur (chiffré sur ce compte Windows)", IsChecked = input.Password.Length > 0 };
        var connect = new Button { Content = "Se connecter", IsDefault = true, Margin = new Thickness(0, 16, 8, 0) };
        var cancel = new Button { Content = "Annuler", IsCancel = true, Margin = new Thickness(0, 16, 0, 0) };
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        buttons.Children.Add(connect); buttons.Children.Add(cancel);
        var body = new StackPanel { Margin = new Thickness(20) };
        body.Children.Add(new TextBlock { Text = "Mot de passe du serveur", FontWeight = FontWeights.SemiBold });
        body.Children.Add(new TextBlock { Text = address.ToString(), TextWrapping = TextWrapping.Wrap });
        body.Children.Add(input); body.Children.Add(remember); body.Children.Add(buttons);
        var dialog = new Window { Owner = this, Title = "Rejoindre le serveur", Content = body, Width = 500,
            SizeToContent = SizeToContent.Height, ResizeMode = ResizeMode.NoResize, WindowStartupLocation = WindowStartupLocation.CenterOwner,
            Background = (Brush)FindResource("Panel"), Foreground = (Brush)FindResource("Text") };
        connect.Click += (_, _) => { if (input.Password.Length > 0) dialog.DialogResult = true; };
        dialog.Loaded += (_, _) => input.Focus();
        if (dialog.ShowDialog() != true) { ServerStatus.Text = "Connexion annulée."; return false; }
        PasswordBox.Password = input.Password; RememberBox.IsChecked = remember.IsChecked;
        return true;
    }
}
