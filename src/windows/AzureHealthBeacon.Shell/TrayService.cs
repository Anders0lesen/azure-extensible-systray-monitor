using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

namespace AzureHealthBeacon;

public enum BeaconState { Healthy, Unconnectable, Connecting, Checking, Failed }

public sealed class TrayService : IDisposable
{
    [DllImport("user32.dll")]
    private static extern bool DestroyIcon(IntPtr handle);

    private readonly MainWindow _window;
    private readonly Forms.NotifyIcon _notifyIcon;
    private readonly DispatcherTimer _animation;
    private BeaconState _state = BeaconState.Unconnectable;
    private int _frame;

    public TrayService(MainWindow window)
    {
        _window = window;
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("Open Azure Health Beacon", null, (_, _) => ShowWindow());
        menu.Items.Add("Check now", null, async (_, _) => await _window.CheckNowAsync());
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("Exit", null, (_, _) => _window.ExitApplication());
        _notifyIcon = new Forms.NotifyIcon
        {
            Text = "Azure Health Beacon — could not determine status",
            ContextMenuStrip = menu,
            Visible = true,
            Icon = DrawIcon(_state, 0),
        };
        _notifyIcon.DoubleClick += (_, _) => ShowWindow();
        _animation = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(180) };
        _animation.Tick += (_, _) => Animate();
    }

    public void SetState(BeaconState state)
    {
        _state = state;
        _frame = 0;
        _animation.IsEnabled = state is BeaconState.Checking or BeaconState.Connecting;
        UpdateIcon();
    }

    public void Notify(string title, string message)
    {
        _notifyIcon.BalloonTipTitle = title;
        _notifyIcon.BalloonTipText = message;
        _notifyIcon.ShowBalloonTip(6000);
    }

    private void ShowWindow()
    {
        _window.Show();
        if (_window.WindowState == System.Windows.WindowState.Minimized)
            _window.WindowState = System.Windows.WindowState.Normal;
        _window.Activate();
    }

    private void Animate() { _frame = (_frame + 1) % 20; UpdateIcon(); }

    private void UpdateIcon()
    {
        var old = _notifyIcon.Icon;
        _notifyIcon.Icon = DrawIcon(_state, _frame);
        old?.Dispose();
        _notifyIcon.Text = _state switch
        {
            BeaconState.Healthy => "Azure Health Beacon — all checks healthy",
            BeaconState.Failed => "Azure Health Beacon — confirmed Azure finding",
            BeaconState.Checking => "Azure Health Beacon — checking",
            BeaconState.Connecting => "Azure Health Beacon — connecting",
            _ => "Azure Health Beacon — could not determine status",
        };
    }

    private static Icon DrawIcon(BeaconState state, int frame)
    {
        using var bitmap = new Bitmap(32, 32);
        using var graphics = Graphics.FromImage(bitmap);
        graphics.SmoothingMode = SmoothingMode.AntiAlias;
        var bounds = new RectangleF(4, 4, 24, 24);
        if (state == BeaconState.Connecting)
        {
            using var pen = new Pen(Color.FromArgb(185, 200, 215), 4) { StartCap = LineCap.Round, EndCap = LineCap.Round };
            graphics.DrawArc(pen, bounds, frame * 18, 270);
        }
        else
        {
            var color = state switch
            {
                BeaconState.Healthy => Color.FromArgb(46, 212, 122),
                BeaconState.Failed => Color.FromArgb(255, 70, 82),
                BeaconState.Checking => Color.FromArgb(110 + Math.Abs(10 - frame) * 10, 245, 185, 66),
                _ => Color.FromArgb(118, 130, 145),
            };
            using var brush = new SolidBrush(color);
            graphics.FillEllipse(brush, bounds);
            if (state == BeaconState.Unconnectable)
            {
                using var hatch = new HatchBrush(HatchStyle.ForwardDiagonal, Color.FromArgb(210, 35, 42, 52), Color.Transparent);
                graphics.FillEllipse(hatch, bounds);
            }
        }
        var handle = bitmap.GetHicon();
        try
        {
            using var borrowed = Icon.FromHandle(handle);
            return (Icon)borrowed.Clone();
        }
        finally
        {
            DestroyIcon(handle);
        }
    }

    public void Dispose()
    {
        _animation.Stop();
        _notifyIcon.Visible = false;
        _notifyIcon.Icon?.Dispose();
        _notifyIcon.ContextMenuStrip?.Dispose();
        _notifyIcon.Dispose();
    }
}
