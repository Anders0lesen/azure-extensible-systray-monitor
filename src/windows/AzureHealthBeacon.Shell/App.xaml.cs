using System;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;

namespace AzureHealthBeacon;

public partial class App : System.Windows.Application
{
    private Mutex? _mutex;
    internal CoreClient? Core { get; private set; }
    internal TrayService? Tray { get; private set; }

    private async void Application_Startup(object sender, StartupEventArgs e)
    {
        var mutexName = Environment.GetEnvironmentVariable("AZURE_HEALTH_BEACON_ALLOW_SECOND_INSTANCE") == "1"
            ? $@"Local\AzureHealthBeacon.Development.{Environment.ProcessId}"
            : @"Local\AzureHealthBeacon.SingleInstance";
        _mutex = new Mutex(true, mutexName, out var created);
        if (!created)
        {
            System.Windows.MessageBox.Show("Azure Health Beacon is already running in the notification area.", "Azure Health Beacon");
            Shutdown();
            return;
        }

        try
        {
            Core = new CoreClient();
            await Core.StartAsync();
            var window = new MainWindow(Core);
            MainWindow = window;
            Tray = new TrayService(window);
            window.BeaconStateChanged += (_, state) => Tray.SetState(state);
            await window.InitializeAsync(e.Args.Contains("--startup", StringComparer.OrdinalIgnoreCase));
        }
        catch (Exception error)
        {
            System.Windows.MessageBox.Show(
                $"Azure Health Beacon could not start its private monitoring engine.\n\n{error.Message}",
                "Azure Health Beacon", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown();
        }
    }

    private void Application_Exit(object sender, ExitEventArgs e)
    {
        Tray?.Dispose();
        Core?.Dispose();
        _mutex?.Dispose();
    }
}
