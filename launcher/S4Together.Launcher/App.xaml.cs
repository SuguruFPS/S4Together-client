using System.IO;
using System.Net.Http;
using System.Windows;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using S4Together.Launcher.Core;

namespace S4Together.Launcher;

public partial class App : Application
{
    // Une seule instance : deux launchers se disputeraient le pont local et la configuration du mod.
    static Mutex? single;

    protected override void OnStartup(StartupEventArgs e)
    {
        void Trace(string step) { try { Directory.CreateDirectory(Core.LauncherSettings.DefaultFolder); File.AppendAllText(Path.Combine(Core.LauncherSettings.DefaultFolder, "launcher-demarrage.log"), $"{DateTime.UtcNow:O} {step}{Environment.NewLine}"); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
        Trace("startup");
        single = new Mutex(true, @"Local\S4Together.Launcher", out var first);
        if (!first)
        {
            MessageBox.Show("Le launcher S4Together est déjà ouvert (voir la zone de notification).", "S4Together", MessageBoxButton.OK, MessageBoxImage.Information);
            Shutdown();
            return;
        }
        DispatcherUnhandledException += (_, args) =>
        {
            args.Handled = true;
            Crash(args.Exception);
        };
        if (e.Args.Contains("--startup-smoke-test"))
        {
            // Le démarrage WPF normal crée la fenêtre ; vérifier après son premier rendu.
            Dispatcher.BeginInvoke(System.Windows.Threading.DispatcherPriority.ApplicationIdle, new Action(() =>
            {
                if (MainWindow is not { IsLoaded: true, IsVisible: true }) { Trace("STARTUP_WINDOW_MISSING"); Environment.Exit(2); }
                Trace("STARTUP_RENDERED");
                Environment.Exit(0);
            }));
        }
        base.OnStartup(e);
        if (!e.Args.Contains("--startup-smoke-test"))
            Dispatcher.BeginInvoke(System.Windows.Threading.DispatcherPriority.ApplicationIdle,
                new Action(async () =>
                {
                    try { SignalVerifiedStartup(e.Args); }
                    catch (Exception error) { Crash(error); return; }
                    await CheckUpdateAsync();
                }));
    }

    void SignalVerifiedStartup(string[] args)
    {
        var markerIndex = Array.IndexOf(args, "--update-verify");
        if (markerIndex < 0) return;
        var nonceIndex = Array.IndexOf(args, "--update-nonce");
        if (markerIndex + 1 >= args.Length || nonceIndex < 0 || nonceIndex + 1 >= args.Length)
            throw new InvalidDataException("Confirmation de mise à jour invalide.");
        var marker = Path.GetFullPath(args[markerIndex + 1]);
        var nonce = args[nonceIndex + 1];
        var updates = Path.GetFullPath(Path.Combine(LauncherSettings.DefaultFolder, "updates"));
        var parent = Directory.GetParent(marker);
        if (!Regex.IsMatch(nonce, "^[0-9a-f]{32}$") || parent is null ||
            Path.GetFileName(marker) != "startup-ready.json" || !Regex.IsMatch(parent.Name, "^[0-9a-f]{32}$") ||
            !string.Equals(parent.Parent?.FullName, updates, StringComparison.OrdinalIgnoreCase) ||
            MainWindow is not { IsLoaded: true, IsVisible: true })
            throw new InvalidDataException("Chemin de confirmation de mise à jour refusé.");
        for (var directory = parent; directory is not null; directory = directory.Parent)
            if ((directory.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("Lien dans le chemin de confirmation refusé.");
        using var file = new FileStream(marker, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        var bytes = Encoding.UTF8.GetBytes(nonce);
        file.Write(bytes);
        file.Flush(true);
    }

    async Task CheckUpdateAsync()
    {
        if (MainWindow is not MainWindow window) return;
        window.IsEnabled = false;
        window.StatusBar.Text = "Vérification des mises à jour…";
        try
        {
            var updater = new LauncherUpdater();
            using var checkTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            var version = Assembly.GetExecutingAssembly().GetName().Version ?? new Version(0, 0, 0);
            var release = await updater.CheckAsync(version, checkTimeout.Token);
            if (release is null) { window.StatusBar.Text = "Launcher à jour."; return; }
            if (GameLocator.IsGameRunning())
            {
                window.StatusBar.Text = "Mise à jour disponible. Ferme Les Sims 4 puis relance le launcher pour l'installer.";
                return;
            }
            window.StatusBar.Text = $"Téléchargement de la mise à jour {release.Version.ToString(3)}…";
            using var downloadTimeout = new CancellationTokenSource(TimeSpan.FromMinutes(3));
            var prepared = await updater.PrepareAsync(release, AppContext.BaseDirectory, downloadTimeout.Token);
            if (GameLocator.IsGameRunning())
            { window.StatusBar.Text = "Mise à jour reportée : Les Sims 4 est ouvert."; return; }
            updater.LaunchAfterExit(prepared, Environment.ProcessId);
            Shutdown();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or HttpRequestException
                                     or OperationCanceledException or InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            // Une panne du service de mise à jour ne bloque jamais le launcher installé.
            window.StatusBar.Text = "Mise à jour indisponible. Tu peux utiliser le launcher installé.";
        }
        finally { window.IsEnabled = true; }
    }

    /// <summary>Erreur inattendue : journal local (sans secret), message clair, arrêt propre plutôt qu'un processus sans fenêtre.</summary>
    void Crash(Exception error)
    {
        var log = Path.Combine(Core.LauncherSettings.DefaultFolder, "launcher-erreurs.log");
        try
        {
            Directory.CreateDirectory(Core.LauncherSettings.DefaultFolder);
            File.AppendAllText(log, $"{DateTime.Now:O} {error}{Environment.NewLine}{Environment.NewLine}");
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
        MessageBox.Show($"Le launcher a rencontré une erreur et va se fermer.{Environment.NewLine}{Environment.NewLine}{error.GetBaseException().Message}{Environment.NewLine}{Environment.NewLine}Détails : {log}",
            "S4Together", MessageBoxButton.OK, MessageBoxImage.Error);
        try { Core.ModBridgeConfig.DisableAutoConnect(); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
        Shutdown(1);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        single?.Dispose();
        base.OnExit(e);
    }
}
