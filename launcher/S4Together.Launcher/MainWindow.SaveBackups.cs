using System.IO;
using System.Text.Json;
using System.Windows.Threading;
using S4Together.Launcher.Core;

namespace S4Together.Launcher;

public partial class MainWindow
{
    void OpenSaveBackups_Click(object sender,System.Windows.RoutedEventArgs e)
    {
        try
        {
            var folder=Path.Combine(LauncherSettings.DefaultFolder,"save-backups");
            Directory.CreateDirectory(folder);
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(folder){UseShellExecute=true});
        }
        catch(Exception ex)when(ex is IOException or UnauthorizedAccessException or System.ComponentModel.Win32Exception)
        {StatusBar.Text="Impossible d'ouvrir les copies de sécurité : "+ex.Message;}
    }
    readonly DispatcherTimer saveBackupTimer=new(){Interval=TimeSpan.FromSeconds(30)};
    readonly MultiplayerSaveBackups saveBackups=new(Path.Combine(LauncherSettings.DefaultFolder,"save-backups"));
    bool copyingSave;
    (string Profile,string Server,CharacterInfo Character)? saveBackupTarget;
    string? lastBackupProblem;

    void StartSaveBackups()
    {
        saveBackupTimer.Tick+=async(_,_)=>await CopyMultiplayerSaveAsync();
        saveBackupTimer.Start();
    }

    async Task CopyMultiplayerSaveAsync()
    {
        if(copyingSave||quitting)return;
        copyingSave=true;
        try
        {
            if(link is { } current&&current.Session.Hello?.Snapshot["worldId"]?.GetValue<string>() is { Length:>0 } world)
            {
                // Le mod peut avoir termine une premiere liaison depuis la derniere liste affichee.
                // Relire le DTO prive authentifie, sans deviner le foyer depuis le nom ou un clone.
                try
                {
                    var reply=await current.Session.RequestAsync("character.list");
                    var fresh=reply.Ok?CharacterRoster.Parse(reply.Payload):null;
                    if(ReferenceEquals(current,link)&&fresh?.Selected is {Bound:true} character)
                    {
                        var profile=localProfile??Path.GetDirectoryName(ModsFolder);
                        if(!string.IsNullOrWhiteSpace(profile))
                        {
                            if(Path.GetFileName(profile).Equals("saves",StringComparison.OrdinalIgnoreCase)||Path.GetFileName(profile).Equals("Tray",StringComparison.OrdinalIgnoreCase))profile=Path.GetDirectoryName(profile);
                            if(profile is not null)saveBackupTarget=(profile,world+"\n"+current.Address.WebSocketUri.AbsoluteUri,character);
                        }
                    }
                }
                catch(Exception ex)when(ex is IOException or OperationCanceledException or System.Net.WebSockets.WebSocketException or ObjectDisposedException)
                { /* La derniere cible confirmee reste utile apres une coupure. */ }
            }
            // Apres crash, conserver la derniere cible authentifiee pour proteger son fichier disque.
            // Aucun fichier .save n'est charge, remplace ou restaure dans le dossier du jeu.
            if(quitting||saveBackupTarget is not {} target)return;
            var result=await Task.Run(()=>saveBackups.Capture(target.Profile,target.Server,target.Character));
            if(result.Status=="created")StatusBar.Text="Copie de securite multijoueur creee. La sauvegarde originale reste intacte.";
            else if(result.Status=="quota"&&lastBackupProblem!="quota")
                StatusBar.Text="Copies de securite : limite de 2 Go atteinte. Les copies existantes sont conservees.";
            lastBackupProblem=result.Status;
        }
        catch(Exception ex)when(ex is IOException or UnauthorizedAccessException or InvalidDataException or JsonException)
        {
            if(lastBackupProblem!=ex.GetType().Name)
                StatusBar.Text="Copie de securite en attente : fichier indisponible ou protection Windows. L'original reste intact.";
            lastBackupProblem=ex.GetType().Name;
        }
        finally{copyingSave=false;}
    }
}
