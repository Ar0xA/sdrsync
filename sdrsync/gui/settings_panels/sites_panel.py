"""Sites settings panel (spec §5.2) -- inline two-column replacement for
the old modal SiteManagerDialog. Load-from-file/URL/Update-from-GitHub
(the old dialog's own actions) live behind a "More" overflow menu in the
Saved Sites column header, keeping the two-column layout spec-literal.

Detect/Test/the background Load-from-URL and Update-from-GitHub fetches
need a background thread -- this panel doesn't own one, so it calls back
through MainFrame via on_detect/on_test/on_fetch (same status_queue +
GuiMessage dispatch pattern the rest of the app uses) and is told the
result via apply_detect_result()/apply_test_result()/apply_fetch_result().
Save to list and Delete are fully local (pure AppSettings.user_sites
mutation, no thread needed) and Load-from-file is synchronous (a local
file is small enough it doesn't need to hide latency behind a thread).
"""
from __future__ import annotations

from typing import Callable, Optional

import wx

from .. import theme
from ..fonts import label_font, value_font
from ..widgets import FlatButton
from ...config import AppSettings, KNOWN_SITES, WebSDRSite
from ...sitesource import CURATED_LIST_URL, load_site_list_from_file


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
        # url -> MainFrame runs detect_websdr_type(url) in a background
        # thread, then calls apply_detect_result() back.
        self.on_detect: Optional[Callable[[str], None]] = None
        # url -> MainFrame runs check_websdr_url(url) in a background
        # thread, then calls apply_test_result() back.
        self.on_test: Optional[Callable[[str], None]] = None
        # (bucket, url) -> MainFrame runs fetch_site_list(url, ...) in a
        # background thread, then calls apply_fetch_result() back and
        # (on success) this panel's own sync_from_settings().
        self.on_fetch: Optional[Callable[[str, str], None]] = None

        self._user_sites = [_site_from_dict(d) for d in settings.user_sites]
        self._curated_sites = [_site_from_dict(d) for d in settings.curated_sites]
        self._imported_sites = [_site_from_dict(d) for d in settings.imported_sites]
        self._editing: Optional[WebSDRSite] = None
        # Set on a successful Detect; the driver_type a Save to list may
        # use, valid ONLY while url_entry still reads _detected_url (see
        # _update_save_enabled) -- same staleness guard the pre-rewrite
        # Custom URL flow used.
        self._detected_driver_type: Optional[str] = None
        self._detected_url: Optional[str] = None

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
        header_row.Add(self.more_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        left.Add(header_row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(5))

        self.list_status_text = wx.StaticText(self, label="")
        self.list_status_text.SetFont(label_font())
        self.list_status_text.SetForegroundColour(theme.FAINT)
        left.Add(self.list_status_text, 0, wx.BOTTOM, self.FromDIP(5))

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
        self.name_entry.Bind(wx.EVT_TEXT, self._on_form_field_edited)
        right.Add(self.name_entry, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(9))

        right.Add(self._field_label("URL"), 0, wx.BOTTOM, self.FromDIP(5))
        url_row = wx.BoxSizer(wx.HORIZONTAL)
        self.url_entry = wx.TextCtrl(self)
        self.url_entry.SetFont(value_font())
        self.url_entry.Bind(wx.EVT_TEXT, self._on_form_field_edited)
        self.detect_btn = FlatButton(self, "Detect")
        self.detect_btn.Bind(wx.EVT_BUTTON, self._on_detect_clicked)
        url_row.Add(self.url_entry, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(9))
        url_row.Add(self.detect_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        right.Add(url_row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(9))

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.save_btn = FlatButton(self, "Save to list", is_primary=True)
        self.test_btn = FlatButton(self, "Test")
        self.save_btn.Enable(False)
        self.save_btn.SetToolTip("Detect this URL successfully first")
        self.save_btn.Bind(wx.EVT_BUTTON, self._on_save_clicked)
        self.test_btn.Bind(wx.EVT_BUTTON, self._on_test_clicked)
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

    def sync_from_settings(self) -> None:
        """Reloads user/curated/imported from AppSettings and rebuilds
        the row list -- called by MainFrame after anything outside this
        panel changes one of those buckets (the background curated-site
        auto-fetch on first run, or a user-triggered Load-from-URL/
        Update-from-GitHub that this panel itself requested but whose
        result MainFrame owns applying to AppSettings)."""
        self._user_sites = [_site_from_dict(d) for d in self.settings.user_sites]
        self._curated_sites = [_site_from_dict(d) for d in self.settings.curated_sites]
        self._imported_sites = [_site_from_dict(d) for d in self.settings.imported_sites]
        self._rebuild_rows()

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
        # Already has a known-good driver_type -- Save works immediately
        # without needing a fresh Detect, unless the URL is then edited
        # (see _on_form_field_edited's staleness check).
        self._detected_driver_type = site.driver_type
        self._detected_url = site.url
        self.form_status.SetLabel(f"Editing \"{site.name}\" -- change fields and Save to list")
        self._update_save_enabled()

    def _on_delete_clicked(self, site: WebSDRSite) -> None:
        confirm = wx.MessageBox(
            f"Remove '{site.name}' from the list?", "Delete WebSDR site",
            wx.YES_NO | wx.ICON_QUESTION, self,
        )
        if confirm != wx.YES:
            return
        self._user_sites = [s for s in self._user_sites if s.url != site.url]
        self._persist_user_sites()
        if self._editing is not None and self._editing.url == site.url:
            self._clear_form()
        self._rebuild_rows()

    def _persist_user_sites(self) -> None:
        self.settings.user_sites = [
            {"name": s.name, "url": s.url, "driver_type": s.driver_type} for s in self._user_sites
        ]
        self.settings.save()

    def _clear_form(self) -> None:
        self._editing = None
        self.name_entry.SetValue("")
        self.url_entry.SetValue("")
        self._detected_driver_type = None
        self._detected_url = None
        self.save_btn.Enable(False)

    # ------------------------------------------------------------------ Add-a-site form
    def _on_form_field_edited(self, evt: wx.CommandEvent) -> None:
        self._update_save_enabled()
        evt.Skip()

    def _update_save_enabled(self) -> None:
        matches_detected = (
            self._detected_driver_type is not None
            and self._detected_url == self.url_entry.GetValue().strip()
        )
        can_save = matches_detected and bool(self.name_entry.GetValue().strip())
        self.save_btn.Enable(can_save)
        self.save_btn.SetToolTip("" if matches_detected else "Detect this URL successfully first")

    def _on_detect_clicked(self, _evt: wx.CommandEvent) -> None:
        url = self.url_entry.GetValue().strip()
        if not url:
            self.form_status.SetLabel("Enter a URL first")
            return
        self._detected_driver_type = None
        self._detected_url = None
        self.save_btn.Enable(False)
        self.detect_btn.Enable(False)
        self.detect_btn.SetLabel("Detecting...")
        self.form_status.SetLabel(f"Detecting WebSDR type at {url}...")
        if self.on_detect is not None:
            self.on_detect(url)

    def apply_detect_result(self, url: str, driver_type: Optional[str], message: str) -> None:
        self.detect_btn.Enable(True)
        self.detect_btn.SetLabel("Detect")
        if url != self.url_entry.GetValue().strip():
            # The URL field was edited (or cleared) while this check was
            # in flight -- this result no longer describes what's in the
            # field, so don't let it set _detected_driver_type.
            return
        self.form_status.SetLabel(message)
        self._detected_driver_type = driver_type
        self._detected_url = url if driver_type is not None else None
        self._update_save_enabled()

    def _on_test_clicked(self, _evt: wx.CommandEvent) -> None:
        url = self.url_entry.GetValue().strip()
        if not url:
            self.form_status.SetLabel("Enter a URL first")
            return
        self.test_btn.Enable(False)
        self.test_btn.SetLabel("Testing...")
        self.form_status.SetLabel("Checking WebSDR reachability...")
        if self.on_test is not None:
            self.on_test(url)

    def apply_test_result(self, ok: bool, message: str) -> None:
        self.test_btn.Enable(True)
        self.test_btn.SetLabel("Test")
        self.form_status.SetLabel(("OK: " if ok else "FAIL: ") + message)

    def _on_save_clicked(self, _evt: wx.CommandEvent) -> None:
        name = self.name_entry.GetValue().strip()
        url = self.url_entry.GetValue().strip()
        if not name or not url or self._detected_driver_type is None or self._detected_url != url:
            return  # guarded by save_btn's own enabled state too
        if self._editing is not None:
            old_url = self._editing.url
            self._user_sites = [
                WebSDRSite(name=name, url=url, driver_type=self._detected_driver_type) if s.url == old_url else s
                for s in self._user_sites
            ]
            self.form_status.SetLabel(f"Updated: {name}")
        else:
            if any(s.url == url for s in KNOWN_SITES + self._user_sites):
                self.form_status.SetLabel("This URL is already saved")
                return
            self._user_sites.append(WebSDRSite(name=name, url=url, driver_type=self._detected_driver_type))
            self.form_status.SetLabel(f"Saved to list: {name}")
        self._persist_user_sites()
        self._clear_form()
        self._rebuild_rows()

    # ------------------------------------------------------------------ More menu (absorbs the old SiteManagerDialog)
    def _on_more_clicked(self, _evt: wx.CommandEvent) -> None:
        menu = wx.Menu()
        load_file_item = menu.Append(wx.ID_ANY, "Load from file...")
        load_url_item = menu.Append(wx.ID_ANY, "Load from URL...")
        update_github_item = menu.Append(wx.ID_ANY, "Update from GitHub")
        self.Bind(wx.EVT_MENU, self._on_load_from_file, load_file_item)
        self.Bind(wx.EVT_MENU, self._on_load_from_url, load_url_item)
        self.Bind(wx.EVT_MENU, self._on_update_from_github, update_github_item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _existing_sites_for_replace(self) -> list[WebSDRSite]:
        # KNOWN_SITES + user_sites only, matching the old dialog's own
        # scope -- collision-validates against the user's own sites, not
        # against imported/curated (which this fetch is about to replace
        # wholesale; validating against their own previous contents would
        # reject every entry as a collision on every re-fetch).
        return KNOWN_SITES + self._user_sites

    def _on_load_from_file(self, _evt: wx.CommandEvent) -> None:
        with wx.FileDialog(
            self, "Load WebSDR site list", wildcard="JSON files (*.json)|*.json|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        sites, message = load_site_list_from_file(path, self._existing_sites_for_replace())
        self._apply_bucket_result("imported", sites, message)

    def _on_load_from_url(self, _evt: wx.CommandEvent) -> None:
        dlg = wx.TextEntryDialog(self, "WebSDR site list URL:", "Load from URL")
        result = dlg.ShowModal()
        url = dlg.GetValue().strip()
        dlg.Destroy()
        if result != wx.ID_OK or not url:
            return
        self._start_fetch("imported", url)

    def _on_update_from_github(self, _evt: wx.CommandEvent) -> None:
        self._start_fetch("curated", CURATED_LIST_URL)

    def _start_fetch(self, bucket: str, url: str) -> None:
        self.list_status_text.SetLabel(f"Fetching {url}...")
        if self.on_fetch is not None:
            self.on_fetch(bucket, url)

    def apply_fetch_result(self, message: str) -> None:
        """Called back by MainFrame once a Load-from-URL/Update-from-GitHub
        background fetch completes -- AppSettings/self._curated_sites/
        self._imported_sites have already been updated by then (see
        MainFrame._apply_curated_autofetch_result), so this only needs
        to show the result and resync (sync_from_settings() handles the
        resync; called separately by MainFrame right before this)."""
        self.list_status_text.SetLabel(message)

    def _apply_bucket_result(self, bucket: str, sites: Optional[list[dict]], message: str) -> None:
        self.list_status_text.SetLabel(message)
        if sites is None:
            return
        if bucket == "imported":
            self.settings.imported_sites = sites
            self._imported_sites = [_site_from_dict(d) for d in sites]
        else:
            self.settings.curated_sites = sites
            self._curated_sites = [_site_from_dict(d) for d in sites]
        self.settings.save()
        self._rebuild_rows()
