"""Sites settings panel (spec §5.2) -- inline two-column replacement for
the old modal SiteManagerDialog. Load-from-file/URL/Update-from-GitHub
(the old dialog's own actions) live behind a "More" overflow menu in the
Saved Sites column header, keeping the two-column layout spec-literal.

GUI REWRITE IN PROGRESS: Delete is fully real (pure AppSettings.user_sites
mutation, no engine/background thread needed). Load/Edit/Detect/Save to
list/Test and the More menu's fetch actions need the background-thread +
status_queue dispatch machinery MainFrame reconnects in phase 6 -- until
then Load exposes an on_load_site callback MainFrame doesn't bind yet,
and the rest are disabled with a tooltip. See project_brief.md's GUI
rewrite progress log.
"""
from __future__ import annotations

from typing import Callable, Optional

import wx

from .. import theme
from ..fonts import label_font, value_font
from ..widgets import FlatButton
from ...config import AppSettings, KNOWN_SITES, WebSDRSite


def _site_from_dict(d: dict) -> WebSDRSite:
    return WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"])


class _SiteRow(wx.Panel):
    def __init__(self, parent: wx.Window, site: WebSDRSite, deletable: bool,
                 on_load: Callable[[WebSDRSite], None],
                 on_edit: Callable[[WebSDRSite], None],
                 on_delete: Callable[[WebSDRSite], None]) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.SetBackgroundColour(theme.BG)
        self.site = site
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        text_col = wx.BoxSizer(wx.VERTICAL)
        name = wx.StaticText(self, label=site.name)
        name.SetFont(value_font())
        name.SetForegroundColour(theme.TEXT)
        name.SetBackgroundColour(theme.BG)
        url = wx.StaticText(self, label=site.url)
        url.SetFont(label_font())
        url.SetForegroundColour(theme.FAINT)
        url.SetBackgroundColour(theme.BG)
        url.SetToolTip(site.url)
        text_col.Add(name)
        text_col.Add(url)
        sizer.Add(text_col, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, self.FromDIP(6))

        load_btn = FlatButton(self, "Load")
        load_btn.Bind(wx.EVT_BUTTON, lambda evt: on_load(site))
        sizer.Add(load_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(6))

        edit_btn = FlatButton(self, "Edit")
        edit_btn.Bind(wx.EVT_BUTTON, lambda evt: on_edit(site))
        edit_btn.Enable(deletable)  # only user-owned entries are editable
        if not deletable:
            edit_btn.SetToolTip("Only sites you've saved to the list can be edited")
        sizer.Add(edit_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(6))

        if deletable:
            delete_btn = FlatButton(self, "Delete")
            delete_btn.Bind(wx.EVT_BUTTON, lambda evt: on_delete(site))
            sizer.Add(delete_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(6))

        self.SetSizer(sizer)
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def _on_paint(self, evt: wx.PaintEvent) -> None:
        dc = wx.PaintDC(self)
        theme.draw_hairline(dc, self.GetClientRect(), "bottom")
        evt.Skip()


class SitesPanel(wx.Panel):
    def __init__(self, parent: wx.Window, settings: AppSettings) -> None:
        super().__init__(parent, style=wx.TAB_TRAVERSAL)
        self.settings = settings
        self.SetBackgroundColour(theme.BG)
        self.on_load_site: Optional[Callable[[WebSDRSite], None]] = None

        self._user_sites = [_site_from_dict(d) for d in settings.user_sites]
        self._curated_sites = [_site_from_dict(d) for d in settings.curated_sites]
        self._imported_sites = [_site_from_dict(d) for d in settings.imported_sites]
        self._editing: Optional[WebSDRSite] = None

        pad_top, pad_side, pad_bottom = self.FromDIP(18), self.FromDIP(16), self.FromDIP(20)
        gutter = self.FromDIP(40)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.AddSpacer(pad_top)
        columns = wx.BoxSizer(wx.HORIZONTAL)

        # --- Left: Saved sites ------------------------------------------------
        left = wx.BoxSizer(wx.VERTICAL)
        header_row = wx.BoxSizer(wx.HORIZONTAL)
        header_row.Add(self._kicker("Saved sites"), 0, wx.ALIGN_CENTER_VERTICAL)
        header_row.AddStretchSpacer(1)
        self.more_btn = FlatButton(self, "More ▾")
        self.more_btn.Bind(wx.EVT_BUTTON, self._on_more_clicked)
        self.more_btn.Enable(False)
        self.more_btn.SetToolTip("Load from file/URL and GitHub updates are wired up in a later phase")
        header_row.Add(self.more_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        left.Add(header_row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(9))

        self.rows_panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
        self.rows_panel.SetBackgroundColour(theme.BG)
        self.rows_panel.SetScrollRate(0, 20)
        self.rows_sizer = wx.BoxSizer(wx.VERTICAL)
        self.rows_panel.SetSizer(self.rows_sizer)
        left.Add(self.rows_panel, 1, wx.EXPAND)
        columns.Add(left, 1, wx.EXPAND | wx.RIGHT, gutter)

        # --- Right: Add a site --------------------------------------------------
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(self._kicker("Add a site"), 0, wx.BOTTOM, self.FromDIP(9))

        right.Add(self._field_label("Name"), 0, wx.BOTTOM, self.FromDIP(5))
        self.name_entry = wx.TextCtrl(self)
        self.name_entry.SetFont(value_font())
        right.Add(self.name_entry, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(9))

        right.Add(self._field_label("URL"), 0, wx.BOTTOM, self.FromDIP(5))
        url_row = wx.BoxSizer(wx.HORIZONTAL)
        self.url_entry = wx.TextCtrl(self)
        self.url_entry.SetFont(value_font())
        self.detect_btn = FlatButton(self, "Detect")
        self.detect_btn.Enable(False)
        self.detect_btn.SetToolTip("Wired up in a later phase of the GUI rewrite")
        url_row.Add(self.url_entry, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(9))
        url_row.Add(self.detect_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        right.Add(url_row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(9))

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.save_btn = FlatButton(self, "Save to list", is_primary=True)
        self.test_btn = FlatButton(self, "Test")
        for b in (self.save_btn, self.test_btn):
            b.Enable(False)
            b.SetToolTip("Wired up in a later phase of the GUI rewrite (needs a successful Detect first)")
        self.save_btn.Bind(wx.EVT_BUTTON, self._on_save_clicked)
        btn_row.Add(self.save_btn, 0, wx.RIGHT, self.FromDIP(9))
        btn_row.Add(self.test_btn, 0)
        right.Add(btn_row, 0, wx.BOTTOM, self.FromDIP(5))

        self.form_status = wx.StaticText(self, label="")
        self.form_status.SetFont(label_font())
        self.form_status.SetForegroundColour(theme.FAINT)
        right.Add(self.form_status, 0)

        columns.Add(right, 1, wx.EXPAND)
        outer.Add(columns, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad_side)
        outer.AddSpacer(pad_bottom)
        self.SetSizer(outer)

        self._rebuild_rows()

    def _kicker(self, text: str) -> wx.StaticText:
        w = wx.StaticText(self, label=theme.caps(text))
        w.SetFont(label_font())
        w.SetForegroundColour(theme.MUTED)
        w.SetBackgroundColour(theme.BG)
        return w

    def _field_label(self, text: str) -> wx.StaticText:
        return self._kicker(text)

    def _all_sites(self) -> list[WebSDRSite]:
        return KNOWN_SITES + self._user_sites + self._curated_sites + self._imported_sites

    def _rebuild_rows(self) -> None:
        self.rows_sizer.Clear(delete_windows=True)
        deletable_urls = {s.url for s in self._user_sites}
        for site in self._all_sites():
            row = _SiteRow(
                self.rows_panel, site, deletable=site.url in deletable_urls,
                on_load=self._on_load_clicked, on_edit=self._on_edit_clicked, on_delete=self._on_delete_clicked,
            )
            self.rows_sizer.Add(row, 0, wx.EXPAND)
        self.rows_panel.Layout()
        self.rows_panel.FitInside()

    def _on_load_clicked(self, site: WebSDRSite) -> None:
        if self.on_load_site is not None:
            self.on_load_site(site)

    def _on_edit_clicked(self, site: WebSDRSite) -> None:
        self._editing = site
        self.name_entry.SetValue(site.name)
        self.url_entry.SetValue(site.url)
        self.form_status.SetLabel(f"Editing \"{site.name}\" -- Save is wired up in a later phase")

    def _on_delete_clicked(self, site: WebSDRSite) -> None:
        confirm = wx.MessageBox(
            f"Remove '{site.name}' from the list?", "Delete WebSDR site",
            wx.YES_NO | wx.ICON_QUESTION, self,
        )
        if confirm != wx.YES:
            return
        self._user_sites = [s for s in self._user_sites if s.url != site.url]
        self.settings.user_sites = [
            {"name": s.name, "url": s.url, "driver_type": s.driver_type} for s in self._user_sites
        ]
        self.settings.save()
        self._rebuild_rows()

    def _on_save_clicked(self, _evt: wx.CommandEvent) -> None:
        pass  # disabled until phase 6 (needs a successful Detect first)

    def _on_more_clicked(self, _evt: wx.CommandEvent) -> None:
        pass  # disabled until phase 6
