using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using S4Together.Launcher.Core;

namespace S4Together.Launcher;

/// <summary>Fermeture pendant une connexion : prévenir que fermer le launcher déconnecte du serveur.</summary>
sealed class CloseWarningDialog : Window
{
    public sealed record Answer(CloseChoice Choice, bool DontAskAgain);

    Answer? answer;

    CloseWarningDialog()
    {
        Title = "Fermer S4Together ?";
        Width = 520; SizeToContent = SizeToContent.Height; ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        Background = new SolidColorBrush(Color.FromRgb(0x1E, 0x22, 0x2B));
        var text = new SolidColorBrush(Color.FromRgb(0xEE, 0xF1, 0xF6));
        var panel = new StackPanel { Margin = new Thickness(18) };
        panel.Children.Add(new TextBlock
        {
            Foreground = text, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 12),
            Text = "Tu es connecté au serveur.\n\n" +
                   "Si tu fermes le launcher, tu seras déconnecté du serveur et le jeu perdra sa connexion multijoueur.\n\n" +
                   "Pour rester connecté, réduis-le dans les icônes cachées (à côté de l'horloge).",
        });
        var dontAsk = new CheckBox { Content = "Ne plus afficher ce message", Foreground = text, Margin = new Thickness(0, 0, 0, 12) };
        panel.Children.Add(dontAsk);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        var hide = new Button { Content = "Réduire (rester connecté)", Padding = new Thickness(12, 6, 12, 6), Margin = new Thickness(0, 0, 8, 0), IsDefault = true };
        var quit = new Button { Content = "Fermer et me déconnecter", Padding = new Thickness(12, 6, 12, 6), Margin = new Thickness(0, 0, 8, 0) };
        var cancel = new Button { Content = "Annuler", Padding = new Thickness(12, 6, 12, 6), IsCancel = true };
        hide.Click += (_, _) => { answer = new(CloseChoice.HideToTray, dontAsk.IsChecked == true); DialogResult = true; };
        quit.Click += (_, _) => { answer = new(CloseChoice.Quit, dontAsk.IsChecked == true); DialogResult = true; };
        buttons.Children.Add(hide); buttons.Children.Add(quit); buttons.Children.Add(cancel);
        panel.Children.Add(buttons);
        Content = panel;
    }

    /// <summary>null : annulé, la fenêtre reste ouverte.</summary>
    public static Answer? Ask(Window owner)
    {
        var dialog = new CloseWarningDialog { Owner = owner };
        return dialog.ShowDialog() == true ? dialog.answer : null;
    }
}
