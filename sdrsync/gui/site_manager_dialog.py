"""wx.Dialog for managing the WebSDR site list's imported/curated buckets:
load from a local file, load from an arbitrary URL, update from the
maintainer-curated GitHub list, and per-entry delete.

Genuinely new to this codebase -- previously only wx.MessageBox was used,
so there's no existing dialog to mirror. Talks directly to AppSettings
(reads/writes settings.imported_sites/settings.curated_sites, replace-all
on each load/update, single-entry removal on delete) rather than
duplicating MainFrame's _user_sites-style in-memory bookkeeping --
MainFrame re-reads both buckets from settings and refreshes its dropdown
once this dialog closes (see gui/app.py's _on_manage_sites_clicked).

Shown via ShowModal() (blocks input to the rest of the app while open,
but the same thread's event loop keeps running underneath, so
MainFrame's own status-queue timer is unaffected) -- this dialog runs its
own background fetch thread + its own queue.Queue + its own wx.Timer,
mirroring MainFrame's status_queue pattern but kept fully self-contained
rather than routed through MainFrame's dispatch table.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Optional, Sequence

import wx

from sdrsync.config import AppSettings, WebSDRSite
from sdrsync.gui_messages import GuiMessage
from sdrsync.sitesource import (
    CURATED_LIST_URL,
    SiteListFetchResult,
    fetch_site_list,
    load_site_list_from_file,
)

logger = logging.getLogger("sdrsync.gui.site_manager_dialog")

QUEUE_POLL_MS = 150


class SiteManagerDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, settings: AppSettings, existing_sites: Sequence[WebSDRSite]) -> None:
        super().__init__(parent, title="Manage WebSDR sites", size=(480, 420))
        self.settings = settings
        # KNOWN_SITES + user_sites, for collision validation -- passed in
        # rather than imported directly, so this module doesn't need its
        # own copy of MainFrame's site-list bookkeeping.
        self._existing_sites = list(existing_sites)

        self.status_queue: "queue.Queue[GuiMessage]" = queue.Queue()
        self._fetch_thread: Optional[threading.Thread] = None

        self._build_widgets()
        self._refresh_list()

        self._poll_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_status_queue, self._poll_timer)
        self._poll_timer.Start(QUEUE_POLL_MS)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.load_file_btn = wx.Button(self, label="Load from file...")
        self.load_file_btn.Bind(wx.EVT_BUTTON, self._on_load_from_file)
        btn_row.Add(self.load_file_btn, flag=wx.RIGHT, border=6)
        self.load_url_btn = wx.Button(self, label="Load from URL...")
        self.load_url_btn.Bind(wx.EVT_BUTTON, self._on_load_from_url)
        btn_row.Add(self.load_url_btn, flag=wx.RIGHT, border=6)
        self.update_github_btn = wx.Button(self, label="Update from GitHub")
        self.update_github_btn.Bind(wx.EVT_BUTTON, self._on_update_from_github)
        btn_row.Add(self.update_github_btn)
        sizer.Add(btn_row, flag=wx.ALL, border=8)

        self.status_text = wx.StaticText(self, label="")
        sizer.Add(self.status_text, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=8)

        self.site_list = wx.ListBox(self)
        sizer.Add(self.site_list, proportion=1, flag=wx.EXPAND | wx.ALL, border=8)

        remove_btn = wx.Button(self, label="Remove selected")
        remove_btn.Bind(wx.EVT_BUTTON, self._on_remove_selected)
        sizer.Add(remove_btn, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=8)

        close_btn = wx.Button(self, label="Close")
        close_btn.Bind(wx.EVT_BUTTON, self._on_close)
        sizer.Add(close_btn, flag=wx.ALIGN_RIGHT | wx.ALL, border=8)

        self.SetSizer(sizer)

    # ------------------------------------------------------------------
    def _bucket_entries(self) -> list[tuple[str, dict]]:
        entries = [("imported", d) for d in self.settings.imported_sites]
        entries += [("curated", d) for d in self.settings.curated_sites]
        return entries

    def _refresh_list(self) -> None:
        self.site_list.Set([f"[{bucket}] {d['name']} ({d['url']})" for bucket, d in self._bucket_entries()])

    def _current_all_sites(self, exclude_bucket: str) -> list[WebSDRSite]:
        # Includes the OTHER bucket's current entries (so a fetch into
        # "imported" can't collide with what's in "curated" and vice
        # versa), but deliberately excludes exclude_bucket's own current
        # entries -- that bucket is about to be replaced wholesale by
        # this fetch's result, so validating the incoming list against
        # its own previous contents would reject every entry as a
        # collision on every re-fetch after the first (intra-batch
        # collisions within the new list are already handled by
        # validate_site_list's own seen_names/seen_urls accumulation, so
        # nothing is lost by excluding it here).
        return self._existing_sites + [
            WebSDRSite(name=d["name"], url=d["url"], driver_type=d["driver_type"])
            for bucket, d in self._bucket_entries() if bucket != exclude_bucket
        ]

    # ------------------------------------------------------------------
    def _on_load_from_file(self, _event=None) -> None:
        if self._fetch_thread is not None and self._fetch_thread.is_alive():
            return  # a background fetch is in flight; avoid a racing "existing" snapshot
        with wx.FileDialog(
            self, "Load WebSDR site list", wildcard="JSON files (*.json)|*.json|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        sites, message = load_site_list_from_file(path, self._current_all_sites("imported"))
        self._apply_bucket_result("imported", sites, message)

    def _on_load_from_url(self, _event=None) -> None:
        dlg = wx.TextEntryDialog(self, "WebSDR site list URL:", "Load from URL")
        result = dlg.ShowModal()
        url = dlg.GetValue().strip()
        dlg.Destroy()
        if result != wx.ID_OK or not url:
            return
        self._start_fetch("imported", url)

    def _on_update_from_github(self, _event=None) -> None:
        self._start_fetch("curated", CURATED_LIST_URL)

    def _start_fetch(self, bucket: str, url: str) -> None:
        if self._fetch_thread is not None and self._fetch_thread.is_alive():
            return  # a fetch is already in flight; ignore double-clicks
        self.load_file_btn.Enable(False)
        self.load_url_btn.Enable(False)
        self.update_github_btn.Enable(False)
        self.status_text.SetLabel(f"Fetching {url}...")
        existing = self._current_all_sites(bucket)
        self._fetch_thread = threading.Thread(
            target=self._run_fetch, args=(bucket, url, existing, self.status_queue), daemon=True,
        )
        self._fetch_thread.start()

    @staticmethod
    def _run_fetch(bucket: str, url: str, existing: list, status_queue: "queue.Queue") -> None:
        try:
            sites, message = asyncio.run(fetch_site_list(url, existing))
        except Exception as e:
            logger.exception("Site list fetch crashed")
            sites, message = None, f"Fetch failed: {e}"
        try:
            status_queue.put_nowait(SiteListFetchResult(bucket, sites, message))
        except queue.Full:
            pass

    def _poll_status_queue(self, _event=None) -> None:
        try:
            while True:
                item = self.status_queue.get_nowait()
                if isinstance(item, SiteListFetchResult):
                    self._apply_bucket_result(item.bucket, item.sites, item.message)
        except queue.Empty:
            pass

    def _apply_bucket_result(self, bucket: str, sites: Optional[list[dict]], message: str) -> None:
        self.load_file_btn.Enable(True)
        self.load_url_btn.Enable(True)
        self.update_github_btn.Enable(True)
        self.status_text.SetLabel(message)
        if sites is None:
            return
        if bucket == "imported":
            self.settings.imported_sites = sites
        else:
            self.settings.curated_sites = sites
        self.settings.save()
        self._refresh_list()

    def _on_remove_selected(self, _event=None) -> None:
        idx = self.site_list.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        bucket, entry = self._bucket_entries()[idx]
        if bucket == "imported":
            self.settings.imported_sites = [d for d in self.settings.imported_sites if d["url"] != entry["url"]]
        else:
            self.settings.curated_sites = [d for d in self.settings.curated_sites if d["url"] != entry["url"]]
        self.settings.save()
        self._refresh_list()

    def _on_close(self, _event=None) -> None:
        self._poll_timer.Stop()
        if self.IsModal():
            self.EndModal(wx.ID_CLOSE)
