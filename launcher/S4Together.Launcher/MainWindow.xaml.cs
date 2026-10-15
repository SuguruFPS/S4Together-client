using System.ComponentModel;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Microsoft.Win32;
using S4Together.Launcher.Core;
using Forms = System.Windows.Forms;

namespace S4Together.Launcher;

public sealed record Choice(string Id, string Label) : INotifyPropertyChanged
{
    ImageSource? portrait;
    public ImageSource? Portrait { get => portrait; set { portrait = value; PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Portrait))); } }
    public IReadOnlyList<string> SimIds { get; init; } = [];
    public event PropertyChangedEventHandler? PropertyChanged;
}
public sealed record GameChoice(GameInstall Install, string Label);
public sealed record DiagItem(string Mark, Brush Color, string Title, string Detail);
public sealed record SanctionRow(SanctionEntry Entry, string Label);

public partial class MainWindow : Window
{
    const int MaxChatLines = 300;
    AdminAccess? admin;
    static readonly TimeSpan HealthTimeout = TimeSpan.FromSeconds(5);

    readonly LauncherSettings settings = LauncherSettings.Load();
    readonly ModInstaller installer = new(Path.Combine(LauncherSettings.DefaultFolder, "backups"));
    readonly Forms.NotifyIcon tray = new();
    ModPackage? package;
    string packageProblem = "";
    MultiplayerLink? link;
    bool ownsHousehold;
    SeasonState? season;
    CharacterRoster? roster;
    bool busy;
    bool quitting;
    MultiplayerLink? charactersLoadingFor;
    bool loadingCharacters => link is not null && ReferenceEquals(charactersLoadingFor, link);
    bool joining;
    bool scanningLocalSims;
    bool localSimsScanned;
    int portraitRequest;
    enum CharacterChoice { None, Local, New, Server }
    CharacterChoice characterChoice;
    LocalSimCard? chosenLocal;
    string? chosenServer;
    string? preparedCharacter;
    List<LocalSimCard> localCards = [];
    CancellationTokenSource? joinWatch;
    string? observedSim;
    DateTime observedAt;
    string? localProfile;

    public MainWindow()
    {
        InitializeComponent();
        localProfile = string.IsNullOrWhiteSpace(settings.LocalSimProfile) ? null : settings.LocalSimProfile;
        VersionText.Text = "Launcher " + (Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "?");
        HostBox.Text = settings.ServerHost;
        PortBox.Text = settings.ServerPort;
        NameBox.Text = settings.PlayerName;
        RememberBox.IsChecked = settings.RememberPassword;
        TrayBox.IsChecked = settings.MinimizeToTray;
        InitializeDirectory();
        FriendList.ItemsSource = settings.Favorites.ToList();
        LoadPackage();
        SetupTray();
        DetectGames();
        DetectModsFolders();
        StartServerModMaintenance();
        Closing += OnClosing;
        RefreshAll();
        NavTo("PageServer");
    }

    // ---------- Navigation et état général ----------

    void Nav_Checked(object sender, RoutedEventArgs e)
    {
        if (sender is not RadioButton { Tag: string target } || !IsInitialized) return;
        foreach (var page in new FrameworkElement[] { PageHome, PageServer, PageSim, PageCommunity, PageGame, PageDiag, PageAdmin })
            page.Visibility = page.Name == target ? Visibility.Visible : Visibility.Collapsed;
        if (target == "PageHome") RefreshAll();
        if (target == "PageSim" && link is not null && roster is null && !loadingCharacters) _ = LoadCharactersAsync();
        if (target == "PageSim" && !localSimsScanned && !scanningLocalSims) _ = LoadLocalSimsAsync();
    }

    void Status(string text) => StatusBar.Text = text;

    void LoadPackage()
    {
        try { package = ModInstaller.LoadPackage(Path.Combine(AppContext.BaseDirectory, "mod")); }
        catch (Exception ex) when (ex is IOException or InstallRefusedException or System.Text.Json.JsonException or KeyNotFoundException or UnauthorizedAccessException)
        {
            package = null;
            packageProblem = ex is InstallRefusedException ? ex.Message : "Mod introuvable à côté du launcher (dossier « mod ») : retélécharge le launcher complet.";
        }
    }

    GameInstall? SelectedGame => (GameCombo.SelectedItem as GameChoice)?.Install;
    string ModsFolder => ModsCombo.Text.Trim();

    InstallState? CurrentModState()
    {
        if (package is null || GameLocator.ValidateModsFolder(ModsFolder).Length > 0) return null;
        try { return installer.State(ModsFolder, package.Sha256); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { return null; }
    }

    void RefreshAll()
    {
        var game = SelectedGame;
        HomeGame.Text = game is null ? "Les Sims 4 non trouvé : choisis le dossier dans « Paramètres »." : $"{game.StoreLabel} — {game.Path}" + (game.Version is null ? "" : $" (version {game.Version})");
        PacksText.Text = game is null ? "" : PacksLabel(GameLocator.DetectPacks(game.Path));

        var folderProblem = GameLocator.ValidateModsFolder(ModsFolder);
        var state = CurrentModState();
        var modText = package is null ? packageProblem
            : folderProblem.Length > 0 ? folderProblem
            : state is { } s ? ModInstaller.Label(s) + $" Version fournie : {package.Version}." : "État du mod illisible.";
        HomeMod.Text = modText;
        ModState.Text = modText;
        var options = folderProblem.Length == 0 ? GameLocator.ReadModSettings(ModsFolder) : null;
        ModOptions.Text = options is null ? "" : options.Problem.Length == 0 ? "Mods et mods de script activés dans Options.ini." : options.Problem;
        InstallButton.Content = state is InstallState.Absent or null ? "Installer" : state == InstallState.UpToDate ? "Réinstaller" : "Mettre à jour / réparer";
        InstallButton.IsEnabled = package is not null && folderProblem.Length == 0;
        UninstallButton.IsEnabled = folderProblem.Length == 0 && state is not InstallState.Absent and not null;
        EnableScriptsButton.IsEnabled = options is { } o && o.Problem.Length > 0;

        HomeServer.Text = link is null
            ? (settings.ServerHost.Length == 0 ? "Aucun serveur configuré." : $"Déconnecté — {settings.ServerHost}:{settings.ServerPort}")
            : $"Connecté à {link.Address.DisplayEndpoint} en tant que {settings.PlayerName}.";
        HomeBridge.Text = link is null ? "Inactive (connexion au serveur requise)."
            : link.Bridge.ModConnected ? "Le jeu est relié au launcher." : $"Prête sur le port {link.Bridge.Port} : en attente du jeu.";
        ConnectButton.Content = link is null ? "Se connecter" : "Se déconnecter";
        PlayHint.Text = PlayReadiness();
        NavStatus.Text = link is null ? "Hors ligne" : "En ligne";
        NavCharacters.Visibility = NavCommunity.Visibility = NavDiagnostic.Visibility = link is null ? Visibility.Collapsed : Visibility.Visible;
        NavCommunity.IsEnabled = link is not null;
        ChatComposer.IsEnabled = link is not null;
        ChatStatus.Text = link is null ? "Connecte-toi au serveur pour accéder au tchat."
            : "Tu es connecté au serveur. Le tchat reste disponible ici pendant que tu joues.";
        JoinButton.IsEnabled = link is not null && roster is not null && !loadingCharacters && !joining && !busy
            && characterChoice != CharacterChoice.None && (characterChoice == CharacterChoice.Server || CharacterName.Text.Trim().Length > 0);
        JoinButton.Content = joining ? "Préparation…" : "Rejoindre";
        LocalSimList.IsEnabled = CharacterList.IsEnabled = !joining;
        CharacterName.IsEnabled = !joining && characterChoice != CharacterChoice.Server;
        DeleteCharacterButton.Visibility = characterChoice == CharacterChoice.Server ? Visibility.Visible : Visibility.Collapsed;
        DeleteCharacterButton.IsEnabled = link is not null && chosenServer is not null && !joining && !busy && !GameLocator.IsGameRunning();
        CreationOptions.IsEnabled = CharacterSearch.IsEnabled = !joining;
        var existingChoice = ResolveChosenCharacter();
        KeepMoney.IsEnabled = !joining && existingChoice is null;
        var importedChoice = characterChoice == CharacterChoice.Local;
        KeepMoney.Visibility = ImportMoneyHint.Visibility = importedChoice ? Visibility.Visible : Visibility.Collapsed;
        ImportMoneyHint.Text = existingChoice is { } existing
            ? existing.MoneyPolicy == "imported" ? "Ce personnage conserve l'argent de son foyer importé." : $"Ce personnage utilise les fonds du serveur : {existing.Money ?? roster?.StartingMoney:N0} §."
            : KeepMoney.IsChecked == true ? "L'argent du foyer copié sera conservé." : roster is { } moneyRoster ? $"Nouveau départ : {moneyRoster.StartingMoney:N0} §, sans ta maison solo." : "Nouveau départ : fonds du serveur, sans ta maison solo.";
        RefreshGameStartGuide();
        RefreshCharactersButton.IsEnabled = link is not null && !loadingCharacters;
    }

    static string PacksLabel(IReadOnlyList<string> packs) =>
        packs.Count == 0 ? "Aucun pack additionnel détecté."
        : $"{packs.Count} pack(s) : {GameLocator.PacksSummary(packs)}" + (packs.Count > GameLocator.MaxPacksSent ? $" — seuls {GameLocator.MaxPacksSent} sont annoncés au serveur (extensions et packs de jeu en priorité)." : "");

    string PlayReadiness()
    {
        if (SelectedGame is null) return "Choisis d'abord l'installation du jeu.";
        if (package is null) return packageProblem;
        if (CurrentModState() is not InstallState.UpToDate) return "Le mod doit être installé ou mis à jour (onglet « Paramètres »).";
        if (link is null) return "Prêt : « Jouer » te connectera au serveur.";
        if (link.Bridge.ModConnected) return "Le jeu est connecté. Bon jeu !";
        return roster is { Selected: null } ? "Prêt. Conseil : crée ou choisis d'abord ton personnage (onglet « Personnages »)." : "Prêt à lancer le jeu.";
    }

    // ---------- Serveur ----------

    bool ReadServerForm(out ServerAddress? address)
    {
        address = null;
        var name = NameBox.Text.Trim();
        if (name.Length is < 2 or > 40) { ServerStatus.Text = "Le nom de joueur doit faire entre 2 et 40 caractères."; return false; }
        try { address = ReadAddress(); }
        catch (FormatException ex) { ServerStatus.Text = ex.Message; return false; }
        catch (ArgumentException ex) { ServerStatus.Text = ex.Message; return false; }
        ShowServerAddress(address);
        settings.ServerHost = address.ToString();
        settings.ServerPort = PortBox.Text.Trim();
        settings.PlayerName = name;
        settings.RememberPassword = RememberBox.IsChecked == true;
        SaveSettings();
        return true;
    }

    void SaveSettings()
    {
        try { settings.Save(); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { Status("Réglages non enregistrés : " + ex.Message); }
    }

    async void Test_Click(object sender, RoutedEventArgs e)
    {
        if (!ReadServerForm(out var address) || address is null) return;
        ServerStatus.Text = "Test en cours…";
        var result = await HealthProbe.CheckAsync(address, HealthTimeout);
        ServerStatus.Text = result.Ok ? $"Serveur S4Together joignable ({result.LatencyMs} ms)." : result.Message;
    }

    async void Connect_Click(object sender, RoutedEventArgs e)
    {
        if (link is not null) { await DisconnectAsync("Déconnecté."); return; }
        await ConnectAsync();
    }

    Task<bool> ConnectAsync() => ConnectServerAsync(fromDirectory: false);

    async Task<bool> ConnectServerAsync(bool fromDirectory)
    {
        if (busy || link is not null) return false;
        if (!ReadServerForm(out var address) || address is null) { NavTo("PageServer"); return false; }
        if (settings.KnownServers.Count >= ServerDirectory.MaxServers && !settings.KnownServers.Any(s => s.Endpoint == ServerDirectory.Key(address)))
        { ServerStatus.Text = "Le répertoire est plein. Retire une fiche avant d'ajouter ce serveur."; return false; }
        if (GameLocator.IsGameRunning() && gameEndpoint != ServerDirectory.Key(address))
        { ServerStatus.Text = "Ferme Les Sims 4 avant de changer de serveur."; return false; }
        busy = true;
        ConnectButton.IsEnabled = false;
        ServerDirectoryList.IsEnabled = false;
        ServerStatus.Text = "Connexion…";
        try
        {
            var password = PasswordBox.Password.Length > 0 ? PasswordBox.Password : null;
            HealthResult? health = null;
            if (fromDirectory || address.Scope == AddressScope.Internet)
            {
                health = await HealthProbe.CheckAsync(address, HealthTimeout);
                directoryHealth[ServerDirectory.Key(address)] = health;
                RefreshDirectory();
                if (!health.Ok) { ServerStatus.Text = health.Message; return false; }
                // Les anciens serveurs ne publient pas ce champ : première authentification sans secret,
                // puis demande uniquement après invalid_password. Le serveur reste l'autorité.
                if (fromDirectory)
                {
                    password = null;
                    if (health.PasswordRequired == true)
                    { if (!RequestDirectoryPassword(address)) return false; password = PasswordBox.Password; }
                }
            }
            var dlcs = SelectedGame is { } g ? GameLocator.PacksForServer(GameLocator.DetectPacks(g.Path)) : [];
            settings.Dlcs = dlcs.ToList();
            ServerStatus.Text = "Vérification des mods du serveur…";
            var modSync = await ServerModSync.SyncAsync(address, ModsFolder, settings.ModSigningKeys, GameLocator.IsGameRunning());
            activeServerModFolder = ModsFolder;
            var mods = modSync.Mods;
            if (modSync.Downloaded + modSync.Removed > 0)
                Status($"Mods du serveur : {modSync.Downloaded} installé(s), {modSync.Removed} retiré(s) dans Mods\\{ServerModSync.FolderName}.");
            ServerStatus.Text = "Connexion…";
            MultiplayerLink created;
            var hadIdentity = settings.IdentityFor(address) is not null;
            try { created = await MultiplayerLink.ConnectAsync(address, settings.PlayerName, dlcs, password, settings.IdentityFor(address), mods: mods); }
            catch (ServerRejectedException ex) when (fromDirectory && health?.PasswordRequired is null && ex.Code == "invalid_password")
            {
                if (!RequestDirectoryPassword(address)) return false;
                password = PasswordBox.Password;
                created = await MultiplayerLink.ConnectAsync(address, settings.PlayerName, dlcs, password, settings.IdentityFor(address), mods: mods);
            }
            created = await ResumeAccountOnSameWorld(created, address, dlcs, password, hadIdentity, mods);
            link = created;
            gameEndpoint = ServerDirectory.Key(address);
            var known = ServerDirectory.Remember(settings.KnownServers, address, ServerLabelBox.Text);
            known.LastConnectedUtc = DateTimeOffset.UtcNow;
            if (health?.Name is { } announced) ServerDirectory.RememberAnnouncedName(settings.KnownServers, address, announced);
            settings.RememberServerPassword(address, password ?? "", RememberBox.IsChecked == true);
            settings.RememberIdentity(address, created.Session.Hello!.PlayerId, created.Session.Hello.Token, WorldIdOf(created));
            SaveSettings();
            RefreshDirectory();
            created.EventReceived += message => Dispatcher.InvokeAsync(() => OnServerEvent(message));
            created.Closed += reason => Dispatcher.InvokeAsync(() => OnLinkClosed(created, reason));
            created.Bridge.ModConnectionChanged += connected => Dispatcher.InvokeAsync(() => { if (ReferenceEquals(link, created)) OnModConnection(connected); });
            created.Bridge.Rejected += reason => Dispatcher.InvokeAsync(() => Status("Connexion locale refusée : " + reason));
            created.Bridge.RequestRejected += (type, code) => Dispatcher.InvokeAsync(() =>
            {
                if (!ReferenceEquals(link, created) || type != "game.join") return;
                joinWatch?.Cancel();
                SimStatus.Text = code == "lot_taken"
                    ? "Emménagement refusé : cette maison appartient à un autre joueur. La cohabitation sans accord n'est pas autorisée. Choisis une maison libre ; tu peux lui rendre visite avec ton propre foyer."
                    : "Entrée en jeu refusée : " + ProtocolErrors.French(code);
                Status(SimStatus.Text);
            });
            ApplySnapshot(created.Session.Hello.Snapshot);
            ServerStatus.Text = "Connecté.";
            Status($"Connecté à {address.DisplayEndpoint}.");
            _ = LoadPermissionsAsync();
            await LoadCharactersAsync();
            if (!ReferenceEquals(link, created)) return false;
            NavTo("PageSim");
            return true;
        }
        catch (ServerRejectedException ex) { ServerStatus.Text = ProtocolErrors.French(ex.Code, ex.Message); }
        catch (ServerModSyncException ex) { ServerStatus.Text = ex.Message; }
        catch (UnauthorizedAccessException ex) { ServerStatus.Text = "Mods du serveur non installés (accès refusé au dossier Mods) : " + ex.Message; }
        catch (Exception ex) when (ex is IOException or System.Net.WebSockets.WebSocketException or TimeoutException or OperationCanceledException or System.Net.Sockets.SocketException or InvalidOperationException)
        {
            ServerStatus.Text = "Connexion impossible : " + ex.Message;
        }
        finally
        {
            busy = false;
            ConnectButton.IsEnabled = true;
            ServerDirectoryList.IsEnabled = true;
            RefreshAll();
        }
        NavTo("PageServer");
        return false;
    }

    async Task DisconnectAsync(string reason)
    {
        joinWatch?.Cancel();
        observedSim = null;
        var old = link;
        link = null;
        if (old is not null) await old.DisposeAsync();
        TryDisableAutoConnect();
        PlayerList.ItemsSource = null;
        ownsHousehold = false;
        season = null;
        ShowSeason();
        ShowCharacters(null);
        HideAdmin();
        ServerStatus.Text = reason;
        RefreshAll();
        await ReviewServerModsAsync();
    }

    void OnLinkClosed(MultiplayerLink closed, string reason)
    {
        if (!ReferenceEquals(closed, link)) return;
        joinWatch?.Cancel();
        link = null;
        ownsHousehold = false;
        season = null;
        PlayerList.ItemsSource = null;
        ShowCharacters(null);
        ShowSeason();
        TryDisableAutoConnect();
        _ = closed.DisposeAsync().AsTask();
        HideAdmin();
        ServerStatus.Text = "Connexion perdue. Reconnecte-toi au serveur pour retrouver tes personnages.";
        SimStatus.Text = ServerStatus.Text;
        NavTo("PageServer");
        Notify("Connexion au serveur perdue", reason);
        RefreshAll();
    }

    void OnModConnection(bool connected)
    {
        if (!connected && link is { } current) _ = current.Session.RequestAsync("game.leave").ContinueWith(_ => { }, TaskScheduler.Default);
        Notify(connected ? "Jeu connecté" : "Jeu déconnecté", connected ? "Les Sims 4 est relié au serveur via le launcher." : "Le jeu n'est plus relié au launcher.");
        RefreshAll();
    }

    static void TryDisableAutoConnect()
    {
        try { ModBridgeConfig.DisableAutoConnect(); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
    }

    void NavTo(string page)
    {
        foreach (var child in ((Panel)NavHome.Parent).Children.OfType<RadioButton>())
            if (child.Tag as string == page) child.IsChecked = true;
    }

    // ---------- Événements serveur ----------

    void OnServerEvent(JsonObject message)
    {
        var type = message["type"]?.GetValue<string>();
        if (message["payload"] is not JsonObject payload) return;
        switch (type)
        {
            case "game.presence":
                if (payload["presence"] is JsonObject presence && presence["playerId"]?.GetValue<string>() == link?.PlayerId
                    && presence["online"] is JsonValue online && online.TryGetValue<bool>(out var inGame))
                {
                    if (inGame && presence["characterId"]?.GetValue<string>() == roster?.SelectedId)
                    {
                        joinWatch?.Cancel();
                        SimStatus.Text = "En jeu · Présence enregistrée sur le serveur. Apparition des Sims et foyers distants en test ; mouvements en direct non synchronisés.";
                        Status(SimStatus.Text);
                        LocalSimPortraitStatus.Text = "Monde " + presence["worldId"]?.GetValue<string>() + " · terrain " + presence["lotId"]?.GetValue<string>();
                    }
                    else if (!inGame) { joinWatch?.Cancel(); SimStatus.Text = "Sim hors jeu · le launcher reste connecté au serveur."; }
                }
                break;
            case "object":
                if (payload["id"]?.GetValue<string>() == "s4t-observation-" + link?.PlayerId && payload["ownerId"]?.GetValue<string>() == link?.PlayerId
                    && payload["value"] is JsonObject observation && observation["loaded"] is JsonValue loaded && loaded.TryGetValue<bool>(out var active) && active)
                { observedSim = observation["activeSimId"]?.GetValue<string>(); observedAt = DateTime.UtcNow; }
                break;
            case "chat":
                var name = payload["name"]?.GetValue<string>() ?? "?";
                var text = payload["text"]?.GetValue<string>() ?? "";
                AddChat($"{name} : {text}");
                if (!IsVisible || WindowState == WindowState.Minimized) Notify(name, text);
                break;
            case "players":
                ShowPlayers(payload["players"] as JsonArray);
                break;
            case "snapshot":
                ApplySnapshot(payload);
                break;
            case "season.state" or "season.chosen":
                ApplySeason(SeasonState.From(payload));
                break;
        }
    }

    void ApplySnapshot(JsonObject snapshot)
    {
        ShowPlayers(snapshot["players"] as JsonArray);
        var me = link?.PlayerId;
        var households = (snapshot["households"] as JsonArray ?? []).OfType<JsonObject>().ToList();
        ownsHousehold = me is not null && households.Any(h => h["ownerId"]?.GetValue<string>() == me);
        HouseholdList.ItemsSource = households
            .Select(h => new Choice(h["id"]?.GetValue<string>() ?? "", (h["name"]?.GetValue<string>() ?? "?") + (h["ownerId"]?.GetValue<string>() == me ? " (à toi)" : "")))
            .ToList();
        ApplySeason(SeasonState.From(snapshot));
    }

    // ---------- Saison initiale commune (#138) : le serveur décide, le launcher n'affiche que son état ----------

    void ApplySeason(SeasonState? incoming)
    {
        if (incoming is null || !SeasonState.Supersedes(season, incoming)) return;
        season = incoming;
        ShowSeason();
    }

    void ShowSeason()
    {
        SeasonCard.Visibility = season is null ? Visibility.Collapsed : Visibility.Visible;
        if (season is null) return;
        SeasonText.Text = season.Describe(link?.PlayerId, settings.Dlcs);
        SeasonChoices.Visibility = season.CanChoose(link?.PlayerId, settings.Dlcs) ? Visibility.Visible : Visibility.Collapsed;
    }

    async void ChooseSeason_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string choice }) return;
        if (!Confirm($"Commencer le monde en {SeasonState.Label(choice)} ? Ce choix vaut pour tous les joueurs et ne sera plus redemandé.")) return;
        var reply = await RequestAsync("season.choose", new JsonObject { ["season"] = choice }, SeasonStatus);
        if (reply is not { Ok: true }) return;
        ApplySeason(SeasonState.From(reply.Payload));
        // Si un autre joueur a validé avant nous, le serveur renvoie la saison commune, pas la nôtre.
        SeasonStatus.Text = season is { Status: "chosen", Season: { } chosen } ? $"Saison de départ : {SeasonState.Label(chosen)}." : "";
    }

    async void AbandonSeason_Click(object sender, RoutedEventArgs e)
    {
        var reply = await RequestAsync("season.abandon", [], SeasonStatus);
        if (reply is not { Ok: true }) return;
        ApplySeason(SeasonState.From(reply.Payload));
        SeasonStatus.Text = "Le choix passe à un autre joueur.";
    }

    async void ResetSeason_Click(object sender, RoutedEventArgs e)
    {
        var reason = SeasonResetReason.Text.Trim();
        if (reason.Length == 0) { SeasonResetStatus.Text = "Indique un motif."; return; }
        if (!Confirm("Réinitialiser la saison du monde ? Un joueur présent avec Saisons devra en choisir une nouvelle.")) return;
        var reply = await RequestAsync("admin.season.reset", new JsonObject { ["reason"] = reason }, SeasonResetStatus);
        if (reply is not { Ok: true }) return;
        ApplySeason(SeasonState.From(reply.Payload));
        SeasonResetReason.Clear();
        SeasonResetStatus.Text = "Saison réinitialisée.";
    }

    void ShowPlayers(JsonArray? players)
    {
        var list = (players ?? []).OfType<JsonObject>()
            .Select(p => new Choice(p["playerId"]?.GetValue<string>() ?? "", p["name"]?.GetValue<string>() ?? "?")).ToList();
        PlayerList.ItemsSource = list;
        AdminPlayers.ItemsSource = list.Where(p => p.Id != link?.PlayerId).ToList();
    }

    // ---------- Admin (droits lus sur le serveur, jamais déduits localement) ----------

    async Task LoadPermissionsAsync()
    {
        if (link is null) return;
        ServerReply reply;
        try { reply = await link.Session.RequestAsync("admin.permissions.get", new JsonObject()); }
        catch (Exception ex) when (ex is IOException or TimeoutException or OperationCanceledException or InvalidOperationException or System.Net.WebSockets.WebSocketException)
        {
            HideAdmin();
            return;
        }
        // Serveur sans gestion des rôles, ou rôle sans aucun droit : onglet masqué.
        if (AdminAccess.FromReply(reply) is not { HasAdminTab: true } access) { HideAdmin(); return; }
        admin = access;
        AdminText.Text = $"Ton rôle sur ce serveur : {AdminAccess.Label(access.Role)}.";
        AdminRolesRow.Visibility = access.CanManageRoles ? Visibility.Visible : Visibility.Collapsed;
        AdminRole.ItemsSource = access.AssignableRoles.Select(r => new Choice(r, AdminAccess.Label(r))).ToList();
        AdminRole.SelectedIndex = 0;
        AdminSanctionsCard.Visibility = access.CanSanction ? Visibility.Visible : Visibility.Collapsed;
        AdminSeasonCard.Visibility = access.CanResetSeason ? Visibility.Visible : Visibility.Collapsed;
        SanctionKind.ItemsSource = access.Sanctions.Select(k => new Choice(k, AdminSanctions.KindLabel(k))).ToList();
        SanctionKind.SelectedIndex = 0;
        NavAdmin.Visibility = Visibility.Visible;
        if (access.CanSanction) await LoadSanctionsAsync();
    }

    void SanctionKind_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (admin is null || SanctionKind.SelectedItem is not Choice kind) return;
        var durations = AdminSanctions.Durations(kind.Id, admin.Limits);
        SanctionDuration.ItemsSource = durations;
        SanctionDuration.SelectedIndex = durations.Count > 0 ? 0 : -1;
        SanctionDuration.IsEnabled = durations.Count > 0;
    }

    async void AdminPlayers_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (admin?.CanSanction == true) await LoadSanctionsAsync();
    }

    async void AllSanctions_Click(object sender, RoutedEventArgs e)
    {
        AdminPlayers.SelectedItem = null;
        await LoadSanctionsAsync();
    }

    /// <summary>Sanctions du joueur choisi, ou toutes (joueurs hors ligne compris) sans sélection.</summary>
    async Task LoadSanctionsAsync()
    {
        var payload = new JsonObject();
        if (AdminPlayers.SelectedItem is Choice player) payload["targetPlayerId"] = player.Id;
        var reply = await RequestAsync("admin.sanction.list", payload, SanctionStatus);
        if (reply is not { Ok: true }) return;
        var names = (AdminPlayers.ItemsSource as IEnumerable<Choice> ?? []).DistinctBy(c => c.Id).ToDictionary(c => c.Id, c => c.Label);
        SanctionList.ItemsSource = AdminSanctions.FromList(reply).OrderByDescending(s => s.AppliedAt)
            .Select(s => new SanctionRow(s, (names.TryGetValue(s.TargetPlayerId, out var name) ? name : "Hors ligne " + s.TargetPlayerId[..Math.Min(8, s.TargetPlayerId.Length)])
                + " — " + AdminSanctions.Describe(s)))
            .ToList();
    }

    async void ApplySanction_Click(object sender, RoutedEventArgs e)
    {
        if (AdminPlayers.SelectedItem is not Choice player || SanctionKind.SelectedItem is not Choice kind)
        {
            SanctionStatus.Text = "Choisis un joueur connecté et un type de sanction.";
            return;
        }
        JsonObject payload;
        try { payload = AdminSanctions.ApplyPayload(player.Id, kind.Id, SanctionDuration.SelectedItem as SanctionDuration, SanctionReason.Text); }
        catch (ArgumentException ex) { SanctionStatus.Text = ex.Message; return; }
        if (kind.Id is "kick" or "ban" && !Confirm($"{kind.Label} de {player.Label} ?")) return;
        var reply = await RequestAsync("admin.sanction.apply", payload, SanctionStatus);
        if (reply is not { Ok: true }) return;
        SanctionStatus.Text = $"Sanction appliquée à {player.Label} : {kind.Label.ToLowerInvariant()}.";
        SanctionReason.Clear();
        await LoadSanctionsAsync();
    }

    async void RevokeSanction_Click(object sender, RoutedEventArgs e)
    {
        if (SanctionList.SelectedItem is not SanctionRow row) { SanctionStatus.Text = "Choisis une sanction dans la liste."; return; }
        if (!Confirm($"Lever cette sanction ?\n{row.Label}")) return;
        var reply = await RequestAsync("admin.sanction.revoke", new JsonObject { ["sanctionId"] = row.Entry.Id }, SanctionStatus);
        if (reply is not { Ok: true }) return;
        SanctionStatus.Text = "Sanction levée.";
        await LoadSanctionsAsync();
    }

    void HideAdmin()
    {
        admin = null;
        AdminSeasonCard.Visibility = Visibility.Collapsed;
        NavAdmin.Visibility = Visibility.Collapsed;
        if (NavAdmin.IsChecked == true) NavHome.IsChecked = true;
    }

    async void AssignRole_Click(object sender, RoutedEventArgs e)
    {
        if (AdminPlayers.SelectedItem is not Choice player || AdminRole.SelectedItem is not Choice role) { AdminStatus.Text = "Choisis un joueur et un rôle."; return; }
        var reply = await RequestAsync("admin.role.assign", new JsonObject { ["targetPlayerId"] = player.Id, ["role"] = role.Id }, AdminStatus);
        if (reply is { Ok: true }) AdminStatus.Text = $"{player.Label} est maintenant {role.Label}.";
    }

    async void RevokeRole_Click(object sender, RoutedEventArgs e)
    {
        if (AdminPlayers.SelectedItem is not Choice player) { AdminStatus.Text = "Choisis un joueur."; return; }
        if (!Confirm($"Retirer le rôle de {player.Label} (retour à joueur) ?")) return;
        var reply = await RequestAsync("admin.role.revoke", new JsonObject { ["targetPlayerId"] = player.Id }, AdminStatus);
        if (reply is { Ok: true }) AdminStatus.Text = $"{player.Label} est de nouveau joueur.";
    }

    void AddChat(string line)
    {
        ChatList.Items.Add($"[{DateTime.Now:HH:mm}] {line}");
        while (ChatList.Items.Count > MaxChatLines) ChatList.Items.RemoveAt(0);
        ChatList.ScrollIntoView(ChatList.Items[^1]);
    }

    async Task<ServerReply?> RequestAsync(string type, JsonObject payload, TextBlock feedback)
    {
        var current = link;
        if (current is null) { feedback.Text = "Connecte-toi d'abord au serveur."; return null; }
        try
        {
            var reply = await current.Session.RequestAsync(type, payload);
            if (!ReferenceEquals(link, current)) return null;
            if (!reply.Ok) feedback.Text = ProtocolErrors.French(reply);
            return reply;
        }
        catch (Exception ex) when (ex is IOException or TimeoutException or OperationCanceledException or InvalidOperationException or System.Net.WebSockets.WebSocketException)
        {
            if (ReferenceEquals(link, current)) feedback.Text = "Requête impossible : " + ex.Message;
            return null;
        }
    }

    async void SendChat_Click(object sender, RoutedEventArgs e)
    {
        var text = ChatBox.Text.Trim();
        if (text.Length == 0) return;
        var reply = await RequestAsync("chat", new JsonObject { ["text"] = text }, StatusBar);
        if (reply is { Ok: true }) ChatBox.Clear();
    }

    void ChatBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter) SendChat_Click(sender, e);
    }

    async void CreateHousehold_Click(object sender, RoutedEventArgs e)
    {
        var name = HouseholdName.Text.Trim();
        if (name.Length == 0) { SimStatus.Text = "Donne un nom au foyer."; return; }
        var reply = await RequestAsync("household.create", new JsonObject { ["name"] = name }, SimStatus);
        if (reply is not { Ok: true }) return;
        SimStatus.Text = "Foyer créé.";
        HouseholdName.Clear();
        await RefreshSnapshotAsync();
    }

    async void SelectHousehold_Click(object sender, RoutedEventArgs e)
    {
        if (HouseholdList.SelectedItem is not Choice household) { SimStatus.Text = "Choisis un foyer dans la liste."; return; }
        var reply = await RequestAsync("household.select", new JsonObject { ["householdId"] = household.Id }, SimStatus);
        if (reply is { Ok: true }) SimStatus.Text = $"Foyer « {household.Label} » choisi.";
    }

    // ---------- Personnages (T3) : le serveur garde argent, terrain et reprise ; le mod relie le foyer du jeu ----------

    async Task LoadCharactersAsync()
    {
        if (loadingCharacters || link is null) return;
        var current = link;
        charactersLoadingFor = current;
        RefreshAll();
        SimStatus.Text = "Chargement des personnages…";
        try
        {
        var reply = await RequestAsync("character.list", [], SimStatus);
        if (reply is not { Ok: true }) return;
        var loaded = CharacterRoster.Parse(reply.Payload);
        if (loaded is null) { SimStatus.Text = "La liste des personnages reçue est invalide. Vérifie la version du serveur."; return; }
        ShowCharacters(loaded);
        var lots = await RequestAsync("lot.list", [], SimStatus);
        if (lots is { Ok: true, Payload: { } payload })
            LotChoice.ItemsSource = new[] { new Choice("", "(aucun pour l'instant)") }
                .Concat((payload["lots"] as JsonArray ?? []).OfType<JsonObject>()
                    .Where(l => l["ownerId"] is null || l["ownerId"]?.GetValue<string>() == link?.PlayerId)
                    .Select(l => new Choice(l["id"]?.GetValue<string>() ?? "", l["name"]?.GetValue<string>() ?? "?")))
                .ToList();
        if (ReferenceEquals(link, current) && lots is { Ok: true }) SimStatus.Text = "";
        }
        finally
        {
            if (ReferenceEquals(charactersLoadingFor, current)) charactersLoadingFor = null;
            RefreshAll();
        }
    }

    void ShowCharacters(CharacterRoster? loaded)
    {
        roster = loaded;
        CharacterList.ItemsSource = loaded?.Characters.Select(c => new Choice(c.Id, c.Label(loaded.SelectedId)) { SimIds = c.GameSimIds, Portrait = localCards.FirstOrDefault(l => l.Data is { } sim && c.GameSimIds.Contains(sim.SimId.ToString(System.Globalization.CultureInfo.InvariantCulture)))?.Portrait }).ToList();
        CharacterEmpty.Visibility = loaded is { Characters.Count: > 0 } ? Visibility.Collapsed : Visibility.Visible;
        CharacterEmpty.Text = loaded is null ? "Connecte-toi au serveur pour charger tes personnages."
            : "Choisis ta sauvegarde locale ou la carte Nouveau personnage.";
        if (loaded is null) { characterChoice = CharacterChoice.None; chosenServer = preparedCharacter = null; chosenLocal = null; LocalSimList.SelectedIndex = -1; CharacterHint.Text = ""; return; }
        var choices = CharacterList.Items.OfType<Choice>().ToList();
        CharacterList.SelectedItem = characterChoice == CharacterChoice.Server ? choices.FirstOrDefault(c => c.Id == chosenServer) : null;
        if (characterChoice == CharacterChoice.Server && CharacterList.SelectedItem is null) { characterChoice = CharacterChoice.None; chosenServer = null; }
        if (loaded.Characters.Count == 0 && string.IsNullOrWhiteSpace(CharacterName.Text)) CharacterName.Text = settings.PlayerName;
        if (characterChoice == CharacterChoice.None) CharacterHint.Text = "Sélectionne une carte pour activer Rejoindre.";
        CharacterSearch_Changed(CharacterSearch, new TextChangedEventArgs(TextBox.TextChangedEvent, UndoAction.None));
        RefreshAll();
    }

    void NewCharacter_Click(object sender, RoutedEventArgs e)
    {
        ++portraitRequest;
        characterChoice = CharacterChoice.New;
        chosenLocal = null; chosenServer = preparedCharacter = null;
        if (LocalSimList.SelectedItem is not LocalSimCard { IsNew: true }) LocalSimList.SelectedIndex = -1;
        CharacterList.SelectedIndex = -1;
        CharacterName.Text = settings.PlayerName;
        CharacterName.IsEnabled = true;
        OriginCas.IsChecked = true; KeepHouse.IsChecked = KeepMoney.IsChecked = false;
        LocalSimPortrait.Source = null; LocalSimPortrait.Visibility = Visibility.Collapsed;
        LocalSimPortraitStatus.Text = "Ton Sim sera créé dans Les Sims 4."; LocalSimSaveDate.Text = "";
        CharacterHint.Text = "Rejoindre lance le jeu. Choisis Nouvelle partie pour créer ton Sim, puis ton monde et ton terrain.";
        SimStatus.Text = "Nouveau personnage sélectionné.";
        CharacterName.Focus(); RefreshAll();
    }

    void ServerCharacter_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!IsInitialized || CharacterList.SelectedItem is not Choice chosen) return;
        ++portraitRequest;
        characterChoice = CharacterChoice.Server; chosenServer = chosen.Id; chosenLocal = null; preparedCharacter = null;
        LocalSimList.SelectedIndex = -1;
        var info = roster?.Characters.FirstOrDefault(c => c.Id == chosen.Id);
        CharacterName.Text = info?.Name ?? chosen.Label; CharacterName.IsEnabled = false;
        LocalSimPortrait.Source = null; LocalSimPortrait.Visibility = Visibility.Collapsed;
        LocalSimPortrait.Source = chosen.Portrait;
        LocalSimPortrait.Visibility = chosen.Portrait is null ? Visibility.Collapsed : Visibility.Visible;
        LocalSimPortraitStatus.Text = "Personnage enregistré sur ce serveur."; LocalSimSaveDate.Text = "";
        var multiplayerSave = info is null ? null : MultiplayerSave.Find(info, localCards.Where(c => c.Data is not null).Select(c => c.Data!));
        if (multiplayerSave is not null) { LocalSimSaveDate.Text = "Partie multijoueur : " + multiplayerSave.Name; CharacterHint.Text = multiplayerSave.Guidance; }
        else CharacterHint.Text = CharacterRoster.Intent(info).Guidance;
        SimStatus.Text = "Personnage du serveur sélectionné."; RefreshAll();
    }

    void CharacterName_Changed(object sender, TextChangedEventArgs e)
    {
        if (!IsInitialized) return;
        preparedCharacter = null;
        RefreshAll();
    }

    void CharacterSearch_Changed(object sender, TextChangedEventArgs e)
    {
        if (!IsInitialized) return;
        var query = CharacterSearch.Text.Trim();
        System.Windows.Data.CollectionViewSource.GetDefaultView(LocalSimList.ItemsSource ?? localCards).Filter = item =>
            item is LocalSimCard sim && !sim.IsNew && (sim.Data is null || roster?.BoundFor(sim.Data) is null) &&
                (sim.Name.Contains(query, StringComparison.CurrentCultureIgnoreCase) || sim.Household.Contains(query, StringComparison.CurrentCultureIgnoreCase));
        if (CharacterList.ItemsSource is { } items)
            System.Windows.Data.CollectionViewSource.GetDefaultView(items).Filter = item => item is Choice c && c.Label.Contains(query, StringComparison.CurrentCultureIgnoreCase);
    }

    void Origin_Changed(object sender, RoutedEventArgs e)
    {
        if (!IsInitialized) return;
        KeepHouse.IsEnabled = OriginImport.IsChecked == true;
        if (!KeepHouse.IsEnabled) KeepHouse.IsChecked = false;
    }

    async void RefreshCharacters_Click(object sender, RoutedEventArgs e) => await LoadCharactersAsync();

    async void DeleteCharacter_Click(object sender, RoutedEventArgs e)
    {
        if (joining || busy || link is null || chosenServer is null || GameLocator.IsGameRunning()) return;
        var current = link;
        var character = roster?.Characters.FirstOrDefault(c => c.Id == chosenServer);
        if (character is null) return;
        if (MessageBox.Show(this, $"Supprimer « {character.Name} » de ce serveur ?\nTes fichiers de sauvegarde Les Sims 4 restent sur ton PC. Le foyer et sa propriété restent conservés sur le serveur.", "Supprimer le personnage", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
        joining = true; RefreshAll();
        try
        {
            var reply = await RequestAsync("character.delete", new JsonObject { ["characterId"] = character.Id, ["expectedRevision"] = character.Revision }, SimStatus);
            if (reply is not { Ok: true } || !ReferenceEquals(current, link)) return;
            foreach (var key in settings.LocalCharacters.Where(p => p.Value == character.Id).Select(p => p.Key).ToArray()) settings.LocalCharacters.Remove(key);
            settings.SoloGuards.Remove($"{current.Address}|{current.PlayerId}|{character.Id}"); SaveSettings();
            characterChoice = CharacterChoice.None; chosenServer = preparedCharacter = null;
            await LoadCharactersAsync(); SimStatus.Text = "Personnage supprimé du serveur. Tes sauvegardes locales sont conservées.";
        }
        catch (Exception ex) when (ex is IOException or InvalidOperationException or TimeoutException) { SimStatus.Text = "Suppression impossible : " + ex.Message; }
        finally { joining = false; RefreshAll(); }
    }

    async Task LoadLocalSimsAsync()
    {
        if (scanningLocalSims) return;
        scanningLocalSims = true;
        ScanLocalSimsButton.IsEnabled = false;
        LocalSimStatus.Text = "Lecture de tes parties enregistrées et de Ma bibliothèque…";
        try
        {
            var profiles = localProfile is { } chosen ? new[] { chosen } : LocalSimCatalog.FindProfiles(ModsFolder).ToArray();
            var found = await Task.Run(() => LocalSimCatalog.Scan(profiles));
            localCards = found.Sims.Select(s => new LocalSimCard(s)).ToList();
            localCards.Add(new LocalSimCard(null));
            LocalSimList.ItemsSource = localCards;
            LocalSimList.SelectedIndex = -1;
            if (characterChoice == CharacterChoice.Local) { characterChoice = CharacterChoice.None; chosenLocal = null; preparedCharacter = null; }
            CharacterSearch_Changed(CharacterSearch, new TextChangedEventArgs(TextBox.TextChangedEvent, UndoAction.None));
            _ = LoadCardPortraitsAsync(localCards);
            localSimsScanned = true;
            LocalSimStatus.Text = found.Sims.Count > 0 ? $"{found.Sims.Count} Sim(s) trouvé(s) dans {found.FilesRead} fichier(s). Choisis ton Sim dans la liste."
                : "Aucun Sim trouvé. Choisis le dossier Documents → Electronic Arts → Les Sims 4 (ou The Sims 4), qui contient saves et Tray.";
            if (found.Problems.Count > 0) LocalSimStatus.Text += "\n" + string.Join("\n", found.Problems.Take(3));
            if (found.Folders.Count > 0) LocalSimStatus.Text += "\nDossier(s) lu(s) : " + string.Join(" ; ", found.Folders);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException)
        { LocalSimStatus.Text = "Lecture locale impossible : " + ex.Message; }
        finally { scanningLocalSims = false; ScanLocalSimsButton.IsEnabled = true; RefreshAll(); }
    }

    static BitmapImage DecodePortrait(byte[] data)
    {
        using var stream = new MemoryStream(data);
        var bitmap = new BitmapImage(); bitmap.BeginInit(); bitmap.CacheOption = BitmapCacheOption.OnLoad;
        bitmap.DecodePixelWidth = 640; bitmap.StreamSource = stream; bitmap.EndInit(); bitmap.Freeze(); return bitmap;
    }

    async Task LoadCardPortraitsAsync(List<LocalSimCard> cards)
    {
        foreach (var card in cards.Where(c => !c.IsNew).Take(128))
        {
            if (!ReferenceEquals(localCards, cards)) break;
            try
            {
                var data = await Task.Run(() => LocalSimCatalog.ReadThumbnail(card.Data!));
                if (data.Length > 0)
                {
                    card.Portrait = DecodePortrait(data);
                    foreach (var serverCard in CharacterList.Items.OfType<Choice>().Where(c => c.SimIds.Contains(card.Data!.SimId.ToString(System.Globalization.CultureInfo.InvariantCulture)))) serverCard.Portrait = card.Portrait;
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidDataException or NotSupportedException or ArgumentException or FormatException) { }
        }
    }

    async void ScanLocalSims_Click(object sender, RoutedEventArgs e) => await LoadLocalSimsAsync();

    async void LocalSim_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        int request = ++portraitRequest;
        LocalSimPortrait.Source = null;
        LocalSimPortrait.Visibility = Visibility.Collapsed;
        LocalSimSaveDate.Text = "";
        if (LocalSimList.SelectedItem is not LocalSimCard card) { if (characterChoice == CharacterChoice.None) LocalSimPortraitStatus.Text = "Choisis une carte."; return; }
        if (card.IsNew) { NewCharacter_Click(sender, new RoutedEventArgs()); return; }
        var sim = card.Data!;
        if (roster?.BoundFor(sim) is { } bound)
        {
            CharacterList.SelectedItem = CharacterList.Items.OfType<Choice>().FirstOrDefault(c => c.Id == bound.Id);
            return;
        }
        characterChoice = CharacterChoice.Local; chosenLocal = card; chosenServer = preparedCharacter = null;
        CharacterList.SelectedIndex = -1; CharacterName.IsEnabled = true; CharacterName.Text = sim.Name;
        OriginImport.IsChecked = true; KeepHouse.IsChecked = KeepMoney.IsChecked = false;
        CharacterHint.Text = "Rejoindre lance le jeu pour importer ce foyer depuis Ma bibliothèque dans une nouvelle partie multijoueur. Ta sauvegarde solo reste intacte.";
        SimStatus.Text = $"{sim.Name} sélectionné."; RefreshAll();
        LocalSimSaveDate.Text = $"Dernière sauvegarde : {sim.SavedAtUtc.ToLocalTime():dd/MM/yyyy HH:mm} · {sim.SaveName}";
        LocalSimPortraitStatus.Text = "Lecture de l'image du foyer…";
        try
        {
            var data = await Task.Run(() => LocalSimCatalog.ReadThumbnail(sim));
            if (request != portraitRequest) return;
            if (data.Length == 0) { LocalSimPortraitStatus.Text = "Aucune image de ce foyer disponible dans ce fichier."; return; }
            var bitmap = DecodePortrait(data); card.Portrait = bitmap;
            LocalSimPortrait.Source = bitmap; LocalSimPortrait.Visibility = Visibility.Visible;
            LocalSimPortraitStatus.Text = "Image du foyer — elle peut montrer plusieurs Sims.";
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidDataException or NotSupportedException or ArgumentException or FormatException)
        { if (request == portraitRequest) LocalSimPortraitStatus.Text = "L'image de ce foyer n'a pas pu être lue."; }
    }

    async void BrowseLocalSims_Click(object sender, RoutedEventArgs e)
    {
        if (scanningLocalSims) return;
        var dialog = new OpenFolderDialog { Title = "Choisis le dossier Les Sims 4 contenant saves et Tray" };
        if (dialog.ShowDialog(this) != true) return;
        localProfile = dialog.FolderName;
        settings.LocalSimProfile = localProfile;
        SaveSettings();
        await LoadLocalSimsAsync();
    }

    async Task<bool> CreateCharacterAsync()
    {
        if (roster is { Full: true }) { SimStatus.Text = $"Limite de {roster.MaxCharacters} personnages atteinte."; return false; }
        var lot = (LotChoice.SelectedItem as Choice)?.Id;
        var (payload, error) = CharacterRoster.CreatePayload(CharacterName.Text, OriginImport.IsChecked == true,
            KeepMoney.IsChecked == true, KeepHouse.IsChecked == true, string.IsNullOrEmpty(lot) ? null : lot);
        if (payload is null) { SimStatus.Text = error; return false; }
        var reply = await RequestAsync("character.create", payload, SimStatus);
        if (reply is not { Ok: true }) return false;
        SimStatus.Text = "Personnage créé et choisi.";
        await LoadCharactersAsync();
        return roster?.Selected is not null;
    }

    void ImportOptions_Changed(object sender, RoutedEventArgs e)
    {
        if (!IsInitialized) return;
        RefreshAll();
    }

    string? ChosenLocalKey() => link is not null && characterChoice == CharacterChoice.Local && chosenLocal?.Data is { } sim
        ? $"{link.Address}|{link.PlayerId}|{Path.GetFullPath(sim.FilePath)}|{sim.SimId}" : null;

    CharacterInfo? ResolveChosenCharacter()
    {
        var id = characterChoice == CharacterChoice.Server ? chosenServer : preparedCharacter;
        if (id is null && chosenLocal?.Data is { } save) id = roster?.BoundFor(save)?.Id;
        if (id is null && ChosenLocalKey() is { } key && settings.LocalCharacters.TryGetValue(key, out var remembered)) id = remembered;
        return roster?.Characters.FirstOrDefault(c => c.Id == id);
    }

    void RefreshGameStartGuide()
    {
        CharacterInfo? character = ResolveChosenCharacter();
        if (characterChoice == CharacterChoice.None)
        { GameStartGuide.Visibility = Visibility.Collapsed; return; }
        character ??= new CharacterInfo("preview", CharacterName.Text,
            characterChoice == CharacterChoice.Local ? "import" : "cas", "pending_game", null, "server", false, null, null);
        var save = MultiplayerSave.Find(character, localCards.Where(c => c.Data is not null).Select(c => c.Data!));
        ShowGameStartPlan(GameStartPlan.For(character, save));
    }

    void ShowGameStartPlan(GameStartPlan plan)
    {
        GameStartGuide.Visibility = Visibility.Visible;
        GameStartAction.Text = plan.Action;
        GameStartDetails.Text = plan.Details;
    }

    async void SelectCharacter_Click(object sender, RoutedEventArgs e)
    {
        if (joining || link is null || roster is null || characterChoice == CharacterChoice.None) return;
        joining = true;
        RefreshAll();
        try
        {
        SimStatus.Text = "1/3 · Vérification du choix et du serveur…";
        if (SelectedGame is null) { SimStatus.Text = "Jeu introuvable. Ouvre Paramètres et choisis le dossier Les Sims 4."; Status(SimStatus.Text); return; }
        if (package is null) { SimStatus.Text = packageProblem; Status(SimStatus.Text); return; }
        var folderProblem = GameLocator.ValidateModsFolder(ModsFolder);
        if (folderProblem.Length > 0) { SimStatus.Text = folderProblem; Status(folderProblem); return; }
        var key = ChosenLocalKey();
        var id = ResolveChosenCharacter()?.Id;
        if (id is null)
        {
            SimStatus.Text = "2/3 · Enregistrement de ton choix sur le serveur…";
            if (!await CreateCharacterAsync()) return;
            id = roster?.SelectedId; preparedCharacter = id;
            if (id is not null && key is not null) { settings.LocalCharacters[key] = id; SaveSettings(); }
        }
        if (id is null) { SimStatus.Text = "Le serveur n'a pas confirmé le personnage. Réessaie."; return; }
        var reply = await RequestAsync("character.select", new JsonObject { ["characterId"] = id }, SimStatus);
        if (reply is not { Ok: true }) return;
        await LoadCharactersAsync();
        SimStatus.Text = "3/3 · Préparation du jeu et du mod…";
        await PlayAsync();
        }
        catch (Exception ex) when (ex is IOException or InvalidOperationException or TimeoutException or OperationCanceledException)
        { SimStatus.Text = "Rejoindre a échoué : " + ex.Message; Status(SimStatus.Text); }
        finally { joining = false; RefreshAll(); }
    }

    async Task RefreshSnapshotAsync()
    {
        var reply = await RequestAsync("snapshot", [], SimStatus);
        if (reply is { Ok: true, Payload: { } snapshot }) ApplySnapshot(snapshot);
    }

    void AddFriend_Click(object sender, RoutedEventArgs e)
    {
        if (PlayerList.SelectedItem is not Choice player || settings.Favorites.Contains(player.Label)) return;
        settings.Favorites.Add(player.Label);
        FriendList.ItemsSource = settings.Favorites.ToList();
        SaveSettings();
    }

    void RemoveFriend_Click(object sender, RoutedEventArgs e)
    {
        if (FriendList.SelectedItem is not string friend) return;
        settings.Favorites.Remove(friend);
        FriendList.ItemsSource = settings.Favorites.ToList();
        SaveSettings();
    }

    // ---------- Jeu et mod ----------

    void DetectGames(string? manual = null)
    {
        var installs = GameLocator.FindInstalls().ToList();
        var chosen = manual ?? settings.GamePath;
        if (chosen.Length > 0 && !installs.Any(i => SamePath(i.Path, chosen)) && File.Exists(Path.Combine(chosen, "Game", "Bin", "TS4_x64.exe")))
            installs.Insert(0, new GameInstall(GameStore.EaApp, chosen, null));
        GameCombo.ItemsSource = installs.Select(i => new GameChoice(i, $"{i.StoreLabel} — {i.Path}")).ToList();
        GameCombo.SelectedIndex = Math.Max(0, installs.FindIndex(i => SamePath(i.Path, chosen)));
        if (installs.Count == 0) GameCombo.SelectedIndex = -1;
    }

    static bool SamePath(string a, string b) =>
        a.Length > 0 && b.Length > 0 && string.Equals(Path.GetFullPath(a).TrimEnd('\\'), Path.GetFullPath(b).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);

    void DetectModsFolders()
    {
        var folders = GameLocator.FindModsFolders().ToList();
        if (settings.ModsFolder.Length > 0 && !folders.Contains(settings.ModsFolder, StringComparer.OrdinalIgnoreCase)) folders.Insert(0, settings.ModsFolder);
        ModsCombo.ItemsSource = folders;
        ModsCombo.Text = settings.ModsFolder.Length > 0 ? settings.ModsFolder : folders.FirstOrDefault() ?? "";
    }

    void GameCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!IsInitialized || SelectedGame is not { } game) return;
        settings.GamePath = game.Path;
        SaveSettings();
        RefreshAll();
    }

    void ModsCombo_Changed(object sender, RoutedEventArgs e)
    {
        if (!IsInitialized) return;
        if (e is SelectionChangedEventArgs && ModsCombo.SelectedItem is string picked) ModsCombo.Text = picked;
        if (string.Equals(settings.ModsFolder, ModsFolder, StringComparison.OrdinalIgnoreCase)) return;
        settings.ModsFolder = ModsFolder;
        SaveSettings();
        RefreshAll();
    }

    void BrowseGame_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog { Title = "Dossier d'installation des Sims 4 (contient Game\\Bin\\TS4_x64.exe)" };
        if (dialog.ShowDialog(this) != true) return;
        if (!File.Exists(Path.Combine(dialog.FolderName, "Game", "Bin", "TS4_x64.exe")))
        {
            MessageBox.Show(this, "Ce dossier ne contient pas Game\\Bin\\TS4_x64.exe.", "S4Together", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        settings.GamePath = dialog.FolderName;
        SaveSettings();
        DetectGames(dialog.FolderName);
        RefreshAll();
    }

    void Redetect_Click(object sender, RoutedEventArgs e)
    {
        DetectGames();
        DetectModsFolders();
        RefreshAll();
    }

    void BrowseMods_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog { Title = "Dossier Mods du profil Les Sims 4" };
        if (dialog.ShowDialog(this) != true) return;
        ModsCombo.Text = dialog.FolderName;
        ModsCombo_Changed(sender, e);
    }

    bool Confirm(string text) =>
        MessageBox.Show(this, text, "S4Together", MessageBoxButton.YesNo, MessageBoxImage.Question) == MessageBoxResult.Yes;

    bool RunModAction(Func<string> action)
    {
        if (busy) { Status("Une opération sur les mods est en cours. Réessaie dans un instant."); return false; }
        try
        {
            var message = action();
            Status(message);
            RefreshAll();
            return true;
        }
        catch (Exception ex) when (ex is InstallRefusedException or IOException or UnauthorizedAccessException)
        {
            MessageBox.Show(this, ex.Message, "S4Together", MessageBoxButton.OK, MessageBoxImage.Warning);
            RefreshAll();
            return false;
        }
    }

    bool InstallMod()
    {
        if (package is null) return false;
        return Confirm($"Installer le mod S4Together {package.Version} dans :\n{ModsFolder}\\{ModInstaller.TargetFolder}\n\nLes anciennes copies seront sauvegardées puis retirées. Aucun autre mod n'est touché.")
            && RunModAction(() => installer.Install(ModsFolder, package));
    }

    public void UpdateManagedModAtStartup()
    {
        Status("Launcher à jour.");
        if(package is null||GameLocator.ValidateModsFolder(ModsFolder).Length>0)return;
        if(GameLocator.IsGameRunning())
        { if(CurrentModState()!=InstallState.UpToDate)Status("Mod à mettre à jour : ferme Les Sims 4 puis relance le launcher.");return; }
        try
        {
            if(installer.UpdateManaged(ModsFolder,package))
            { RefreshAll();Status($"Launcher et mod S4Together {package.Version} mis à jour."); }
            else if(CurrentModState()!=InstallState.UpToDate)
                Status("Launcher à jour. Le mod nécessite une installation ou une réparation dans Paramètres.");
        }
        catch(Exception ex) when(ex is InstallRefusedException or IOException or UnauthorizedAccessException or System.Text.Json.JsonException or KeyNotFoundException)
        { Status("Mise à jour automatique du mod impossible. Version sauvegardée ; consulte Paramètres."); }
    }

    bool EnableScripts() =>
        Confirm("Activer les mods et les mods de script dans Options.ini ?\nUne copie de sauvegarde d'Options.ini est faite avant. Le jeu doit être fermé.")
        && RunModAction(() => installer.EnableScriptMods(ModsFolder));

    void Install_Click(object sender, RoutedEventArgs e) => InstallMod();
    void EnableScripts_Click(object sender, RoutedEventArgs e) => EnableScripts();

    void Uninstall_Click(object sender, RoutedEventArgs e)
    {
        if (Confirm("Retirer le mod S4Together de ce dossier Mods ? (une sauvegarde est conservée)"))
            RunModAction(() => { installer.Uninstall(ModsFolder, package?.Sha256 ?? ""); return "Mod retiré."; });
    }

    void TrayBox_Click(object sender, RoutedEventArgs e)
    {
        settings.MinimizeToTray = TrayBox.IsChecked == true;
        SaveSettings();
    }

    // ---------- Jouer ----------

    async void Play_Click(object sender, RoutedEventArgs e)
    {
        PlayButton.IsEnabled = false;
        try { await PlayAsync(); }
        finally { PlayButton.IsEnabled = true; RefreshAll(); }
    }

    async Task PlayAsync()
    {
        void Failure(string message) { SimStatus.Text = message; Status(message); }
        if (busy) { Failure("Une opération est en cours. Réessaie dans un instant."); return; }
        if (SelectedGame is not { } game) { Failure("Choisis l'installation du jeu dans Paramètres."); return; }
        if (package is null) { Failure(packageProblem); return; }
        var folderProblem = GameLocator.ValidateModsFolder(ModsFolder);
        if (folderProblem.Length > 0) { Failure(folderProblem); return; }
        var running = GameLocator.IsGameRunning();

        if (CurrentModState() is not InstallState.UpToDate)
        {
            if (running) { Failure("Ferme Les Sims 4 : le mod doit être mis à jour dans Paramètres."); return; }
            if (!InstallMod() || CurrentModState() is not InstallState.UpToDate) { Failure("L'installation du mod n'a pas été terminée. Vérifie Paramètres puis réessaie."); return; }
        }
        var options = GameLocator.ReadModSettings(ModsFolder);
        if (options.Problem.Length > 0)
        {
            if (running) { Failure(options.Problem); return; }
            if (!EnableScripts()) { Failure("Les mods de script doivent être activés pour rejoindre. Vérifie Paramètres."); return; }
        }
        if (link is null && !await ConnectAsync()) return;
        if (roster?.Selected is null) { NavTo("PageSim"); Status("Crée ou choisis un personnage puis clique « Rejoindre »."); return; }
        if (running && !roster.Selected.Bound)
        { Failure("Ferme Les Sims 4 avant de rejoindre avec ce nouveau personnage. Cela évite de lier le foyer solo déjà ouvert. Ensuite, crée une partie multijoueur dédiée."); return; }
        var current = link!;
        var selectedCharacter = roster!.Selected!;
        var intent = roster is not null ? CharacterRoster.Intent(roster.Selected) : PlayIntent.For(ownsHousehold);
        MultiplayerSave? saved = null;
        if (selectedCharacter.Bound)
        {
            var profiles = LocalSimCatalog.FindProfiles(ModsFolder).ToList();
            if (localProfile is not null) profiles.Insert(0, localProfile);
            var scan = await Task.Run(() => LocalSimCatalog.Scan(profiles));
            if (!ReferenceEquals(current, link) || roster?.Selected?.Id != selectedCharacter.Id) return;
            saved = MultiplayerSave.Find(selectedCharacter, scan.Sims);
            if (saved is not null) intent = intent with { Guidance = saved.Guidance };
        }
        ShowGameStartPlan(GameStartPlan.For(selectedCharacter, saved));
        CharacterHint.Text = intent.Guidance;
        NavTo("PageSim");
        try
        {
            var guardKey = $"{current.Address}|{current.PlayerId}|{selectedCharacter.Id}";
            settings.SoloGuards.TryGetValue(guardKey, out var guard);
            {
                var profiles = LocalSimCatalog.FindProfiles(ModsFolder).ToList();
                if (localProfile is not null) profiles.Insert(0, localProfile);
                var scan = await Task.Run(() => LocalSimCatalog.Scan(profiles));
                if (!ReferenceEquals(current, link) || roster?.Selected?.Id != selectedCharacter.Id) { Failure("La connexion ou le personnage a changé. Choisis ton personnage puis réessaie."); return; }
                if (scan.Problems.Count > 0) { Failure("Impossible de vérifier la protection de tes sauvegardes : " + scan.Problems[0]); return; }
                var known = scan.Sims.Where(c => roster.BoundFor(c) is null);
                guard = SoloProtection.Merge(selectedCharacter.Id, guard, known);
                if (!selectedCharacter.Bound)
                {
                    var saves = SoloProtection.Saves(scan.Sims);
                    if (saves.Count > 0)
                    {
                        var answer = SaveChoiceDialog.Ask(this, selectedCharacter.Name, saves, guard.AllowedSaveSlotId);
                        if (answer is null) { Status("Choix annulé : rien n'a été modifié."); return; }
                        if (!ReferenceEquals(current, link) || roster?.Selected?.Id != selectedCharacter.Id) { Failure("La connexion ou le personnage a changé. Choisis ton personnage puis réessaie."); return; }
                        guard = guard with { AllowedSaveSlotId = answer.Save?.SlotId };
                        if (answer.Save is not null)
                            intent = intent with { Guidance = $"Dans le jeu, « Charger la partie » puis « {answer.Save.Name} ». Ne prends pas une autre sauvegarde : elle resterait protégée." };
                        CharacterHint.Text = intent.Guidance;
                    }
                }
                settings.SoloGuards[guardKey] = guard;
                SaveSettings();
            }
            ModBridgeConfig.Write(current.Bridge.Endpoint, settings.PlayerName, settings.Dlcs, current.PlayerId, current.Bridge.Token, intent: intent, soloGuard: guard);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            Failure("Impossible de préparer la liaison avec le jeu : " + ex.Message);
            return;
        }
        if (running)
        {
            SimStatus.Text = "Le jeu est déjà ouvert. Charge ta partie multijoueur dédiée pour connecter le mod ; ne charge pas ta sauvegarde solo.";
            Status(SimStatus.Text);
            BeginJoinWatch(current, roster!.Selected!.Id);
            return;
        }
        try { GameLauncher.Launch(game); }
        catch (Exception ex) when (ex is FileNotFoundException or System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            Failure("Lancement impossible : " + ex.Message);
            return;
        }
        SimStatus.Text = $"Lancement demandé via {game.StoreLabel}. En attente du mod et du foyer chargé.";
        Status(SimStatus.Text);
        CharacterHint.Text = intent.Guidance;
        PlayHint.Text = intent.Guidance;
        NavTo("PageSim");
        BeginJoinWatch(current, roster!.Selected!.Id);
    }

    void BeginJoinWatch(MultiplayerLink current, string characterId)
    {
        joinWatch?.Cancel();
        observedSim = null;
        joinWatch = new CancellationTokenSource();
        _ = WatchJoinAsync(current, characterId, joinWatch.Token);
    }

    async Task WatchJoinAsync(MultiplayerLink current, string characterId, CancellationToken cancel)
    {
        var deadline = DateTime.UtcNow.AddMinutes(3);
        try
        {
            while (ReferenceEquals(link, current) && DateTime.UtcNow < deadline)
            {
                await Task.Delay(2000, cancel);
                if (!current.Bridge.ModConnected) continue;
                var reply = await current.Session.RequestAsync("character.list", cancel: cancel);
                cancel.ThrowIfCancellationRequested();
                if (!ReferenceEquals(link, current)) return;
                var character = CharacterRoster.Parse(reply.Payload)?.Characters.FirstOrDefault(c => c.Id == characterId);
                if (character is { Bound: true } && observedSim is { } simId && DateTime.UtcNow - observedAt < TimeSpan.FromSeconds(15) && character.GameSimIds.Contains(simId))
                {
                    SimStatus.Text = "Mod connecté · foyer lié à ton personnage sur le serveur. Le tchat est disponible.";
                    Status(SimStatus.Text); return;
                }
                SimStatus.Text = "Mod connecté · en attente de la liaison du foyer. Termine la création ou l'import dans ta partie multijoueur.";
            }
            if (ReferenceEquals(link, current) && !cancel.IsCancellationRequested)
            { SimStatus.Text = "Le jeu n'a pas confirmé la liaison après 3 minutes. Vérifie les mods de script et ouvre Diagnostic. Tu peux réessayer."; Status(SimStatus.Text); }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) when (ex is IOException or InvalidOperationException or TimeoutException or System.Net.WebSockets.WebSocketException)
        { if (ReferenceEquals(link, current) && !cancel.IsCancellationRequested) SimStatus.Text = "Vérification du jeu interrompue : " + ex.Message; }
    }

    // ---------- Diagnostic ----------

    static readonly Brush OkBrush = (Brush)Application.Current.Resources["Accent"];
    static readonly Brush WarnBrush = (Brush)Application.Current.Resources["Warn"];
    static readonly Brush BadBrush = (Brush)Application.Current.Resources["Bad"];

    static DiagItem Item(bool? ok, string title, string detail) =>
        new(ok switch { true => "OK", false => "✕", null => "!" }, ok switch { true => OkBrush, false => BadBrush, null => WarnBrush }, title, detail);

    async void RunDiag_Click(object sender, RoutedEventArgs e)
    {
        var items = new List<DiagItem>();
        var game = SelectedGame;
        items.Add(Item(game is not null, "Les Sims 4", game is null ? "Non trouvé." : $"{game.StoreLabel} — {game.Path}"));
        if (game is not null) items.Add(Item(true, "Packs", PacksLabel(GameLocator.DetectPacks(game.Path))));
        items.Add(Item(package is not null, "Mod fourni", package is null ? packageProblem : $"Version {package.Version}, intègre."));
        var folderProblem = GameLocator.ValidateModsFolder(ModsFolder);
        items.Add(Item(folderProblem.Length == 0, "Dossier Mods", folderProblem.Length == 0 ? ModsFolder : folderProblem));
        var state = CurrentModState();
        items.Add(Item(state == InstallState.UpToDate, "Mod installé", state is { } s ? ModInstaller.Label(s) : "Non vérifiable."));
        if (folderProblem.Length == 0)
        {
            var problem = GameLocator.ReadModSettings(ModsFolder).Problem;
            items.Add(Item(problem.Length == 0, "Options du jeu", problem.Length == 0 ? "Mods de script autorisés." : problem));
        }
        items.Add(Item(null, "Jeu en cours", GameLocator.IsGameRunning() ? "Oui." : "Non."));
        DiagList.ItemsSource = items.ToList();

        try
        {
            var address = ReadAddress();
            var health = await HealthProbe.CheckAsync(address, HealthTimeout);
            items.Add(Item(health.Ok, "Serveur", health.Ok ? $"{address.DisplayEndpoint} — {health.LatencyMs} ms, protocole {health.ServerProtocol}." : health.Message));
        }
        catch (Exception ex) when (ex is FormatException or ArgumentException) { items.Add(Item(false, "Serveur", ex.Message)); }
        items.Add(Item(link is not null, "Session", link is null ? "Non connecté." : "Connecté."));
        items.Add(Item(link is null ? null : link.Bridge.ModConnected, "Liaison jeu", link is null ? "Inactive." : link.Bridge.ModConnected ? "Le mod est connecté." : $"Port {link.Bridge.Port}, mod non connecté."));
        DiagList.ItemsSource = items;
    }

    void ExportDiag_Click(object sender, RoutedEventArgs e)
    {
        if (DiagList.ItemsSource is not IEnumerable<DiagItem> items) { Status("Lance d'abord le diagnostic."); return; }
        var dialog = new SaveFileDialog { FileName = $"S4Together-diagnostic-{DateTime.Now:yyyyMMdd-HHmm}.txt", Filter = "Texte|*.txt" };
        if (dialog.ShowDialog(this) != true) return;
        var text = new StringBuilder($"S4Together {VersionText.Text} — {DateTime.Now:yyyy-MM-dd HH:mm}\r\nWindows {Environment.OSVersion.Version}\r\n");
        foreach (var item in items) text.Append($"[{item.Mark}] {item.Title} : {item.Detail}\r\n");
        text.Append("(Aucun mot de passe, jeton ni identifiant de joueur n'est inclus.)\r\n");
        try { File.WriteAllText(dialog.FileName, text.ToString(), Encoding.UTF8); Status("Diagnostic exporté."); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { Status("Export impossible : " + ex.Message); }
    }

    // ---------- Zone de notification et fermeture ----------

    void SetupTray()
    {
        tray.Icon = System.Drawing.SystemIcons.Application;
        tray.Text = "S4Together";
        tray.Visible = true;
        tray.DoubleClick += (_, _) => ShowFromTray();
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("Ouvrir S4Together", null, (_, _) => ShowFromTray());
        menu.Items.Add("Quitter", null, (_, _) => Dispatcher.InvokeAsync(Quit));
        tray.ContextMenuStrip = menu;
    }

    void Notify(string title, string text)
    {
        if (text.Length > 200) text = text[..200] + "…";
        tray.ShowBalloonTip(4000, title, text.Length == 0 ? " " : text, Forms.ToolTipIcon.None);
    }

    void HideToTray(string message)
    {
        Hide();
        Notify("S4Together", message);
    }

    void ShowFromTray()
    {
        Dispatcher.InvokeAsync(() =>
        {
            Show();
            if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
            Activate();
            RefreshAll();
        });
    }

    void OnClosing(object? sender, CancelEventArgs e)
    {
        if (quitting) return;
        if (link is not null && settings.MinimizeToTray)
        {
            e.Cancel = true;
            HideToTray("Toujours connecté. Clic droit sur l'icône > Quitter pour fermer.");
            return;
        }
        e.Cancel = true;
        // Différé : Close() rappelé pendant Closing lève InvalidOperationException (journal 0.1.5).
        Dispatcher.BeginInvoke(Quit);
    }

    async void Quit()
    {
        if (quitting || reviewingServerMods) return;
        if (busy) { Status("Une opération est en cours. Attends sa fin avant de quitter."); return; }
        if (link is not null && GameLocator.IsGameRunning()
            && !Confirm("Le jeu est ouvert : quitter le launcher coupera sa connexion au serveur. Quitter quand même ?")) return;
        quitting = true;
        SaveSettings();
        TryDisableAutoConnect();
        var old = link;
        link = null;
        if (old is not null)
        {
            try { await old.DisposeAsync(); }
            catch (Exception ex) when (ex is IOException or System.Net.WebSockets.WebSocketException or ObjectDisposedException) { }
        }
        await ReviewServerModsAsync();
        serverModTimer.Stop();
        tray.Visible = false;
        tray.Dispose();
        Close();
        Application.Current.Shutdown();
    }
}
