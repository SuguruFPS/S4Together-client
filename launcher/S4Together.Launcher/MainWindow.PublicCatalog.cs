using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Media;
using S4Together.Launcher.Core;

namespace S4Together.Launcher;

public partial class MainWindow
{
    IReadOnlyList<KnownServer> publicServers = [];
    Window? privateServerDialog;

    async Task RefreshPublicCatalogAsync()
    {
        try
        {
            publicServers = await PublicServerCatalog.FetchAsync();
            CatalogStatus.Text = $"{publicServers.Count} serveur(s) publié(s) dans le catalogue.";
        }
        catch (Exception ex) when (ex is System.Net.Http.HttpRequestException or OperationCanceledException or FormatException or IOException)
        { CatalogStatus.Text = "Catalogue public indisponible. Tes favoris et la connexion privée restent disponibles."; }
    }

    void PrivateServer_Click(object sender, RoutedEventArgs e)
    {
        if (privateServerDialog is not null || busy) return;
        ServerDirectoryList.SelectedItem = null;
        PrivateServerFormHost.Content = null;
        var status = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 0) };
        status.SetBinding(TextBlock.TextProperty, new Binding(nameof(TextBlock.Text)) { Source = ServerStatus });
        var body = new StackPanel { Margin = new Thickness(20) };
        var saved = new ComboBox { ItemsSource = ServerDirectory.Visible(settings.KnownServers),
            DisplayMemberPath = nameof(KnownServer.DisplayName), Margin = new Thickness(0, 4, 0, 14),
            ToolTip = "Retrouver une adresse déjà enregistrée sur ce PC" };
        saved.SelectionChanged += (_, _) =>
        {
            if (saved.SelectedItem is not KnownServer server || busy) return;
            ShowServerAddress(server.Address); ServerLabelBox.Text = server.Name;
        };
        body.Children.Add(new TextBlock { Text = "Mes adresses enregistrées" }); body.Children.Add(saved);
        body.Children.Add(PrivateServerForm); body.Children.Add(status);
        privateServerDialog = new Window { Owner = this, Title = "Serveur privé", Width = 650,
            SizeToContent = SizeToContent.Height, MaxHeight = Math.Max(400, SystemParameters.WorkArea.Height - 80),
            WindowStartupLocation = WindowStartupLocation.CenterOwner, ResizeMode = ResizeMode.CanResize,
            Background = (Brush)FindResource("Bg"), Foreground = (Brush)FindResource("Text"),
            Content = new ScrollViewer { Content = body, VerticalScrollBarVisibility = ScrollBarVisibility.Auto } };
        try { privateServerDialog.ShowDialog(); }
        finally
        {
            body.Children.Remove(PrivateServerForm);
            PrivateServerFormHost.Content = PrivateServerForm;
            privateServerDialog = null;
        }
    }

    void PrivateFavorite_Click(object sender, RoutedEventArgs e)
    {
        if (busy) return;
        try
        {
            var address = ReadAddress();
            var server = ServerDirectory.Remember(settings.KnownServers, address, ServerLabelBox.Text);
            server.Favorite = true;
            SaveSettings(); RefreshDirectory(); ServerStatus.Text = "Serveur ajouté aux favoris.";
        }
        catch (Exception ex) when (ex is FormatException or InvalidOperationException or ArgumentException)
        { ServerStatus.Text = ex.Message; }
    }

    async void CatalogDisconnect_Click(object sender, RoutedEventArgs e)
    {
        if (!busy && link is not null) await DisconnectAsync("Déconnecté.");
    }
}
