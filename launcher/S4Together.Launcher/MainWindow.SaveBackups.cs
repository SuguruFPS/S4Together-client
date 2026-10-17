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
    SaveUploadContext? loadedSaveContext;
    readonly PrivateSaveUploader saveUploader=new();

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
            var context=loadedSaveContext;
            var result=await Task.Run(()=>saveBackups.Capture(target.Profile,target.Server,target.Character,context?.Matches(target.Character)==true?context.SlotId:null));
            if(result.Status=="created")StatusBar.Text="Copie de securite multijoueur creee. La sauvegarde originale reste intacte.";
            else if(result.Status=="quota"&&lastBackupProblem!="quota")
                StatusBar.Text="Copies de securite : limite de 2 Go atteinte. Les copies existantes sont conservees.";
            lastBackupProblem=result.Status;
            // Envoi seulement pour le lien actuellement authentifié ET le slot observé par son mod.
            if(link is {} activeLink&&activeLink.Session.Connected&&activeLink.Bridge.ModConnected&&
                context?.Matches(target.Character)==true&&result.BackupPath is {} copy&&
                target.Server.EndsWith("\n"+activeLink.Address.WebSocketUri.AbsoluteUri,StringComparison.Ordinal))
            {
                using var cancel=new CancellationTokenSource(TimeSpan.FromMinutes(5));
                void Closed(string _)=>cancel.Cancel();
                void ModClosed(bool connected){if(!connected)cancel.Cancel();}
                activeLink.Closed+=Closed;
                activeLink.Bridge.ModConnectionChanged+=ModClosed;
                try
                {
                    var status=await Task.Run(()=>saveUploader.UploadAsync(activeLink.Address,activeLink.Session.Hello!,target.Character,context,copy,cancel.Token));
                    if(ReferenceEquals(activeLink,link)&&status=="stored")StatusBar.Text="Copie multijoueur envoyee au serveur prive. La sauvegarde originale reste intacte.";
                    else if(status=="unsupported")StatusBar.Text="Envoi de sauvegarde : mets a jour le serveur pour activer cette fonction.";
                }
                finally{activeLink.Closed-=Closed;activeLink.Bridge.ModConnectionChanged-=ModClosed;}
            }
        }
        catch(System.Net.Http.HttpRequestException)
        {if(!quitting)StatusBar.Text="Envoi de sauvegarde au serveur en attente. La copie locale est conservee ; nouvel essai automatique.";}
        catch(OperationCanceledException) { /* Déconnexion/fermeture : copie locale conservée. */ }
        catch(Exception ex)when(ex is IOException or UnauthorizedAccessException or InvalidDataException or JsonException)
        {
            if(lastBackupProblem!=ex.GetType().Name)
                StatusBar.Text="Copie de securite en attente : fichier indisponible ou protection Windows. L'original reste intact.";
            lastBackupProblem=ex.GetType().Name;
        }
        finally{copyingSave=false;}
    }
}
