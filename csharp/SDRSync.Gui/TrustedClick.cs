using System;
using System.Runtime.InteropServices;

namespace SDRSync.Gui;

/// <summary>
/// Real OS-level mouse click at absolute screen coordinates, ported from
/// sdrsync/websdr/browser_shim.py's WxPageAdapter._simulate_click (which
/// used wx.UIActionSimulator -- itself a thin cross-platform wrapper over
/// the same two OS primitives used directly here).
///
/// Needed because Chromium/WebView2 (and WebKitGTK/Cocoa WebKit)
/// deliberately treat a JS-dispatched click/dispatchEvent() as untrusted
/// and exclude it from satisfying the autoplay-gesture requirement every
/// WebSDR site's audio needs -- confirmed for wx during the Python
/// original's own Block A spike, and unchanged here since the requirement
/// is Chromium/WebKit's, not the GUI toolkit's.
///
/// Windows: P/Invoke SendInput (user32.dll) -- confirmed pattern, this
/// project already P/Invokes user32 elsewhere for window z-order handling
/// (see the Python original's gui/webview_host.py, not yet ported).
///
/// Linux: P/Invoke XTestFakeMotionEvent/XTestFakeButtonEvent
/// (libXtst.so.6), per the plan. Written directly against libXtst's
/// documented API shape, but -- unlike the Windows path above, which is
/// exercised by this session's own build -- this has NOT been live-
/// verified against a real X server this session (no WSL Debian GUI
/// available here). Flagged explicitly, matching this port's standing
/// practice of calling out anything Linux-specific that hasn't been
/// exercised yet (see PORT_BRIEF.md's step 8 note).
/// </summary>
public static class TrustedClick
{
    public static void Click(int screenX, int screenY)
    {
        if (OperatingSystem.IsWindows())
        {
            ClickWindows(screenX, screenY);
        }
        else if (OperatingSystem.IsLinux())
        {
            ClickLinuxX11(screenX, screenY);
        }
    }

    // ------------------------------------------------------------------ Windows
    private const uint InputMouse = 0;
    private const uint MouseEventFMove = 0x0001;
    private const uint MouseEventFAbsolute = 0x8000;
    private const uint MouseEventFVirtualDesk = 0x4000;
    private const uint MouseEventFLeftDown = 0x0002;
    private const uint MouseEventFLeftUp = 0x0004;

    [StructLayout(LayoutKind.Sequential)]
    private struct MouseInput
    {
        public int Dx;
        public int Dy;
        public uint MouseData;
        public uint DwFlags;
        public uint Time;
        public IntPtr DwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Input
    {
        public uint Type;
        public MouseInput Mi;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint nInputs, Input[] pInputs, int cbSize);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int nIndex);

    private const int SmCxvirtualscreen = 78;
    private const int SmCyvirtualscreen = 79;
    private const int SmXvirtualscreen = 76;
    private const int SmYvirtualscreen = 77;

    private static void ClickWindows(int screenX, int screenY)
    {
        // SendInput's absolute mode is normalized to 0..65535 across the
        // virtual desktop (which may not start at (0,0) with multiple
        // monitors), not raw pixel coordinates -- this is documented
        // SendInput/MOUSEEVENTF_ABSOLUTE behavior, not something this port
        // discovered independently.
        var vLeft = GetSystemMetrics(SmXvirtualscreen);
        var vTop = GetSystemMetrics(SmYvirtualscreen);
        var vWidth = GetSystemMetrics(SmCxvirtualscreen);
        var vHeight = GetSystemMetrics(SmCyvirtualscreen);
        if (vWidth <= 0 || vHeight <= 0) return;

        var normX = (int)(((double)(screenX - vLeft) * 65535) / vWidth);
        var normY = (int)(((double)(screenY - vTop) * 65535) / vHeight);

        var move = new Input
        {
            Type = InputMouse,
            Mi = new MouseInput
            {
                Dx = normX,
                Dy = normY,
                DwFlags = MouseEventFMove | MouseEventFAbsolute | MouseEventFVirtualDesk,
            },
        };
        var down = new Input { Type = InputMouse, Mi = new MouseInput { DwFlags = MouseEventFLeftDown } };
        var up = new Input { Type = InputMouse, Mi = new MouseInput { DwFlags = MouseEventFLeftUp } };

        SendInput(1, new[] { move }, Marshal.SizeOf<Input>());
        SendInput(1, new[] { down }, Marshal.SizeOf<Input>());
        SendInput(1, new[] { up }, Marshal.SizeOf<Input>());
    }

    // ------------------------------------------------------------------ Linux (X11/XTest)
    private const int XtestLeftButton = 1;

    [DllImport("libX11.so.6")]
    private static extern IntPtr XOpenDisplay(IntPtr display);

    [DllImport("libX11.so.6")]
    private static extern int XCloseDisplay(IntPtr display);

    [DllImport("libX11.so.6")]
    private static extern int XFlush(IntPtr display);

    [DllImport("libXtst.so.6")]
    private static extern int XTestFakeMotionEvent(IntPtr display, int screenNumber, int x, int y, int deltaMs);

    [DllImport("libXtst.so.6")]
    private static extern int XTestFakeButtonEvent(IntPtr display, uint button, [MarshalAs(UnmanagedType.Bool)] bool isPress, int deltaMs);

    private static void ClickLinuxX11(int screenX, int screenY)
    {
        var display = XOpenDisplay(IntPtr.Zero);
        if (display == IntPtr.Zero) return;
        try
        {
            XTestFakeMotionEvent(display, -1, screenX, screenY, 0);
            XFlush(display);
            XTestFakeButtonEvent(display, XtestLeftButton, true, 30);
            XTestFakeButtonEvent(display, XtestLeftButton, false, 0);
            XFlush(display);
        }
        finally
        {
            XCloseDisplay(display);
        }
    }
}
