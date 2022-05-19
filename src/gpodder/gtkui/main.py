# -*- coding: utf-8 -*-
#
# gPodder - A media aggregator and podcast client
# Copyright (c) 2005-2018 The gPodder Team
#
# gPodder is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# gPodder is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import collections
import html
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse

import dbus.service
import requests.exceptions
import urllib3.exceptions

import gpodder
from gpodder import (common, download, extensions, feedcore, my, opml, player,
                     util, youtube)
from gpodder.dbusproxy import DBusPodcastsProxy
from gpodder.model import Model, PodcastEpisode
from gpodder.syncui import gPodderSyncUI

from . import shownotes
from .adaptive.episodeselector import gPodderEpisodeSelector
from .desktop.channel import gPodderChannel
from .desktop.exportlocal import gPodderExportToLocalFolder
from .desktop.podcastdirectory import gPodderPodcastDirectory
from .desktop.welcome import gPodderWelcome
from .desktopfile import UserAppsReader
from .download import DownloadStatusModel
from .draw import (cake_size_from_widget, draw_cake_pixbuf,
                   draw_iconcell_scale, draw_text_box_centered)
from .interface.addpodcast import gPodderAddPodcast
from .interface.common import (BuilderWidget, Dummy, ExtensionMenuHelper,
                               TreeViewHelper)
from .interface.progress import ProgressIndicator
from .interface.searchtree import SearchTreeBar
from .model import EpisodeListModel, PodcastChannelProxy, PodcastListModel
from .services import CoverDownloader
from .widgets import SimpleMessageArea

import gi  # isort:skip
gi.require_version('Gtk', '3.0')  # isort:skip
gi.require_version('Gdk', '3.0')  # isort:skip
gi.require_version('Handy', '1')  # isort:skip
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango  # isort:skip
from gi.repository import Handy  # isort:skip


logger = logging.getLogger(__name__)

_ = gpodder.gettext
N_ = gpodder.ngettext


class gPodder(BuilderWidget, dbus.service.Object):

    def __init__(self, app, bus_name, gpodder_core, options, **kwargs):
        Handy.init()
        self.menu2sort = {
            "SORT_PUBLISHED": EpisodeListModel.C_PUBLISHED,
            "SORT_TOTAL_TIME": EpisodeListModel.C_TOTAL_TIME,
            "SORT_TITLE": EpisodeListModel.C_TITLE,
        }
        self.sort2menu = {v: k for (k, v) in self.menu2sort.items()}
        dbus.service.Object.__init__(self, object_path=gpodder.dbus_gui_object_path, bus_name=bus_name)
        self.podcasts_proxy = DBusPodcastsProxy(lambda: self.channels,
                self.on_itemUpdate_activate,
                self.playback_episodes,
                self.download_episode_list,
                self.episode_object_by_uri,
                bus_name)
        self.application = app
        self.core = gpodder_core
        self.config = self.core.config
        self.db = self.core.db
        self.model = self.core.model
        self.options = options
        self.extensions_menu = None
        self.extensions_actions = []
        self._search_podcasts = None
        self._search_episodes = None
        BuilderWidget.__init__(self, None,
            _gtk_properties={('gPodder', 'application'): app}, **kwargs)

        self.last_episode_date_refresh = None
        self.refresh_episode_dates()

    def new(self):
        if self.application.want_headerbar:
            # Plus menu button
            self.header_bar_plus_button = Gtk.MenuButton.new()
            self.header_bar_plus_button.set_image(Gtk.Image.new_from_icon_name('list-add-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
            self.header_bar_plus_button.set_use_popover(True)
            self.plus_popover = Gtk.Popover.new_from_model(self.header_bar_plus_button,
                self.application.builder.get_object('plus-menu'))
            self.header_bar_plus_button.set_popover(self.plus_popover)
            # Search button
            self.header_bar_search_button = Gtk.ToggleButton.new()
            self.header_bar_search_button.set_image(Gtk.Image.new_from_icon_name('system-search-symbolic', Gtk.IconSize.SMALL_TOOLBAR))

            self.header_bar.pack_end(self.application.header_bar_menu_button)
            self.header_bar.pack_start(self.application.header_bar_refresh_button)
            self.header_bar.pack_start(self.header_bar_plus_button)
            self.header_bar.pack_end(self.header_bar_search_button)
            self.header_bar.set_show_close_button(True)
            self.header_bar.show_all()

        self.transfer_button = Gtk.Button.new_with_label("Progress")
        self.transfer_button.connect("clicked", self.on_show_progress_activate)
        self.labelDownloads = self.transfer_button.get_child()
        self.transfer_revealer = Gtk.Revealer()
        self.transfer_revealer.set_property("halign", Gtk.Align.END)
        self.transfer_revealer.set_property("valign", Gtk.Align.END)
        self.transfer_revealer.add(self.transfer_button)
        self.transfer_revealer.set_reveal_child(False)
        self.transfer_revealer.show_all()
        self.main_overlay.add_overlay(self.transfer_revealer)
        self.main_overlay.set_overlay_pass_through(self.transfer_revealer, True)

        self.dl_del_label = self.dl_del_button.get_child()
        self.dl_del_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.dl_del_label.set_max_width_chars(8)

        gpodder.user_extensions.on_ui_object_available('gpodder-gtk', self)

        self.bluetooth_available = util.bluetooth_available()

        self.config.connect_gtk_window(self.main_window, 'main_window')

        self.progress_window.connect("delete-event", self.on_progress_close_button_clicked)
        self.progress_window.hide()
        self.application.add_window(self.progress_window)

        self.main_window.show()

        self.player_receiver = player.MediaPlayerDBusReceiver(self.on_played)

        self.gPodder.connect('key-press-event', self.on_key_press)

#        self.episode_columns_menu = None
        self.config.add_observer(self.on_config_changed)

        def on_key_press_shownotes(widget, event):
            if event.keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace, Gdk.KEY_Left, Gdk.KEY_h):
                self.navigate_from_shownotes()
            elif event.keyval in (Gdk.KEY_p, Gdk.KEY_s):
                self.play_button.emit("clicked")
            elif event.keyval in (Gdk.KEY_c, Gdk.KEY_d):
                self.dl_del_button.emit("clicked")
            elif event.keyval == Gdk.KEY_n:
                self.episode_new_action.activate()

#        self.shownotes_pane = Gtk.Box()
        self.shownotes_object = shownotes.get_shownotes(self.config.ui.gtk.html_shownotes, self.shownotes_box)

        self.detailsbox.connect('key-press-event', on_key_press_shownotes)

#        # Vertical paned for the episode list and shownotes
#        self.vpaned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
#        paned = self.vbox_episode_list.get_parent()
#        self.vbox_episode_list.reparent(self.vpaned)
#        self.vpaned.child_set_property(self.vbox_episode_list, 'resize', True)
#        self.vpaned.child_set_property(self.vbox_episode_list, 'shrink', False)
#        self.vpaned.pack2(self.shownotes_pane, resize=False, shrink=False)
#        self.vpaned.show()
#
#        # Minimum height for both episode list and shownotes
#        self.vbox_episode_list.set_size_request(-1, 100)
#        self.shownotes_pane.set_size_request(-1, 100)
#
#        self.config.connect_gtk_paned('ui.gtk.state.main_window.episode_list_size',
#                self.vpaned)
#        paned.add2(self.vpaned)
#
        self.new_episodes_window = None

        self.download_status_model = DownloadStatusModel()
        self.download_queue_manager = download.DownloadQueueManager(self.config, self.download_status_model)

        self.config.connect_gtk_spinbutton('limit.downloads.concurrent', self.spinMaxDownloads,
                                           self.config.limit.downloads.concurrent_max)
        self.config.connect_gtk_togglebutton('max_downloads_enabled', self.cbMaxDownloads)
        self.config.connect_gtk_spinbutton('limit_rate_value', self.spinLimitDownloads)
        self.config.connect_gtk_togglebutton('limit_rate', self.cbLimitDownloads)

        # When the amount of maximum downloads changes, notify the queue manager
        def changed_cb(spinbutton):
            return self.download_queue_manager.update_max_downloads()

        self.spinMaxDownloads.connect('value-changed', changed_cb)
        self.cbMaxDownloads.connect('toggled', changed_cb)

        # Keep a reference to the last add podcast dialog instance
        self._add_podcast_dialog = None

        self.default_title = None
        self.set_title(_('gPodder'))

        self.cover_downloader = CoverDownloader()

        # Generate list models for podcasts and their episodes
        self.podcast_list_model = PodcastListModel(self.cover_downloader)
        self.apply_podcast_list_hide_boring()

        self.cover_downloader.register('cover-available', self.cover_download_finished)

        # Source IDs for timeouts for search-as-you-type
        self._podcast_list_search_timeout = None
        self._episode_list_search_timeout = None

        # Subscribed channels
        self.active_channel = None
        self.channels = self.model.get_podcasts()

        # For loading the list model
        self.episode_list_model = EpisodeListModel(self.config, self.on_episode_list_filter_changed)

        self.create_actions()

        # Init the treeviews that we use
        self.init_podcast_list_treeview()
        self.init_episode_list_treeview()
        self.init_download_list_treeview()

        if self.leaflet.get_folded():
            self.leaflet.set_visible_child(self.channelsbox)
        else:
            self.header_bar.set_show_close_button(False)
            self.leaflet.set_visible_child(self.contentbox)

        self.download_tasks_seen = set()
        self.download_list_update_enabled = False
        self.things_adding_tasks = 0
        self.download_task_monitors = set()

        # Set up the first instance of MygPoClient
        self.mygpo_client = my.MygPoClient(self.config)

        # Extensions section in app menu
        extensions_menu = Gio.Menu()
        self.application.app_menu.insert_section(2, "Extensions", extensions_menu)
        self.extensions_menu_helper = ExtensionMenuHelper(self.gPodder,
            extensions_menu, 'extensions.action_',
            lambda fun: lambda action, param: fun())
        self.extensions_menu_helper.replace_entries(gpodder.user_extensions.on_create_menu())

        gpodder.user_extensions.on_ui_initialized(self.model,
                self.extensions_podcast_update_cb,
                self.extensions_episode_download_cb)

        gpodder.user_extensions.on_application_started()

        # load list of user applications for audio playback
        self.user_apps_reader = UserAppsReader(['audio', 'video'])
        util.run_in_background(self.user_apps_reader.read)

        # Now, update the feed cache, when everything's in place
        self.feed_cache_update_cancelled = False
        self.update_podcast_list_model()

        self.message_area = None

        self.partial_downloads_indicator = None
        util.run_in_background(self.find_partial_downloads)

        # Start the auto-update procedure
        self._auto_update_timer_source_id = None
        if self.config.auto_update_feeds:
            self.restart_auto_update_timer()

        # Find expired (old) episodes and delete them
        old_episodes = list(common.get_expired_episodes(self.channels, self.config))
        if len(old_episodes) > 0:
            self.delete_episode_list(old_episodes, confirm=False)
            updated_urls = set(e.channel.url for e in old_episodes)
            self.update_podcast_list_model(updated_urls)

        # Do the initial sync with the web service
        if self.mygpo_client.can_access_webservice():
            util.idle_add(self.mygpo_client.flush, True)

        self.treeChannels.grab_focus()

        # First-time users should be asked if they want to see the OPML
        if self.options.subscribe:
            util.idle_add(self.subscribe_to_url, self.options.subscribe)
        elif not self.channels:
            self.on_itemUpdate_activate()
        elif self.config.software_update.check_on_startup:
            # Check for software updates from gpodder.org
            diff = time.time() - self.config.software_update.last_check
            if diff > (60 * 60 * 24) * self.config.software_update.interval:
                self.config.software_update.last_check = int(time.time())
                if not os.path.exists(gpodder.no_update_check_file):
                    self.check_for_updates(silent=True)

        if self.options.close_after_startup:
            logger.warning("Startup done, closing (--close-after-startup)")
            self.core.db.close()
            sys.exit()

    def create_actions(self):
        g = self.gPodder

        action = Gio.SimpleAction.new_stateful(
            'showEpisodeDescription', None, GLib.Variant.new_boolean(self.config.episode_list_descriptions))
        action.connect('activate', self.on_itemShowDescription_activate)
        g.add_action(action)

        action = Gio.SimpleAction.new_stateful(
            'viewHideBoringPodcasts', None, GLib.Variant.new_boolean(self.config.podcast_list_hide_boring))
        action.connect('activate', self.on_item_view_hide_boring_podcasts_toggled)
        g.add_action(action)

        action = Gio.SimpleAction.new_stateful(
            'viewAlwaysShowNewEpisodes', None, GLib.Variant.new_boolean(self.config.ui.gtk.episode_list.always_show_new))
        action.connect('activate', self.on_item_view_always_show_new_episodes_toggled)
        g.add_action(action)

        action = Gio.SimpleAction.new_stateful(
            'viewCtrlClickToSortEpisodes', None, GLib.Variant.new_boolean(self.config.ui.gtk.episode_list.ctrl_click_to_sort))
        action.connect('activate', self.on_item_view_ctrl_click_to_sort_episodes_toggled)
        g.add_action(action)

        action = Gio.SimpleAction.new_stateful(
            'searchAlwaysVisible', None, GLib.Variant.new_boolean(self.config.ui.gtk.search_always_visible))
        action.connect('activate', self.on_item_view_search_always_visible_toggled)
        g.add_action(action)

        action = Gio.SimpleAction.new_stateful(
            'channelAutoArchive', None, GLib.Variant.new_boolean(False))
        action.connect('activate', self.on_channel_toggle_lock_activate)
        g.add_action(action)

        action = Gio.SimpleAction.new_stateful(
            'episodeNew', None, GLib.Variant.new_boolean(False))
        action.connect('activate', self.on_new_action)
        action.connect("notify::state", self.on_new_state_changed)
        g.add_action(action)

        action = Gio.SimpleAction.new_stateful(
            'episodeLock', None, GLib.Variant.new_boolean(False))
        action.connect('activate', self.on_episode_lock_action)
        g.add_action(action)

        value = EpisodeListModel.VIEWS[
            self.config.episode_list_view_mode or EpisodeListModel.VIEW_ALL]
        action = Gio.SimpleAction.new_stateful(
            'viewEpisodes', GLib.VariantType.new('s'),
            GLib.Variant.new_string(value))
        action.connect('activate', self.on_item_view_episodes_changed)
        action.connect('activate', self.relabel_view_menubutton)
        g.add_action(action)

        value = self.sort2menu.get(self.config.ui.gtk.state.main_window.episode_column_sort_id, "SORT_PUBLISHED")
        action = Gio.SimpleAction.new_stateful('sortEpisodes', GLib.VariantType.new('s'),
            GLib.Variant.new_string(value))
        action.connect('activate', self.on_episode_list_sort_changed)
        action.connect('activate', self.relabel_sort_menubutton)
        g.add_action(action)

        action_defs = [
            ('update', self.on_itemUpdate_activate),
            ('downloadAllNew', self.on_itemDownloadAllNew_activate),
            ('removeOldEpisodes', self.on_itemRemoveOldEpisodes_activate),
            ('discover', self.on_itemImportChannels_activate),
            ('addChannel', self.on_itemAddChannel_activate),
            ('removeChannel', self.on_itemRemoveChannel_activate),
            ('massUnsubscribe', self.on_itemMassUnsubscribe_activate),
            ('updateChannel', self.on_itemUpdateChannel_activate),
            ('editChannel', self.on_itemEditChannel_activate),
            ('importFromFile', self.on_item_import_from_file_activate),
            ('exportChannels', self.on_itemExportChannels_activate),
            ('play', self.on_playback_selected_episodes),
            ('open', self.on_playback_selected_episodes),
            ('download', self.on_download_selected_episodes),
            ('pause', self.on_pause_selected_episodes),
            ('cancelFromEpisodes', self.on_episodes_cancel_download_activate),
            ('cancelFromProgress', self.on_progress_cancel_download_activate),
            ('delete', self.on_btnDownloadedDelete_clicked),
            # ('toggleEpisodeNew', self.on_item_toggle_played_activate),
            # ('toggleEpisodeLock', self.on_item_toggle_lock_activate),
            ('openEpisodeDownloadFolder', self.on_open_episode_download_folder),
            ('openChannelDownloadFolder', self.on_open_download_folder),
            ('toggleShownotes', self.on_shownotes_selected_episodes),
            ('sync', self.on_sync_to_device_activate),
            ('findPodcast', self.on_find_podcast_activate),
            ('findEpisode', self.on_find_episode_activate),
            ('markEpisodesAsOld', self.on_mark_episodes_as_old),
            ('refreshImage', self.on_itemRefreshCover_activate),
            ('showProgress', self.on_show_progress_activate),
            ('saveEpisodes', self.on_save_episodes_activate),
            ('bluetoothEpisodes', self.on_bluetooth_episodes_activate),
        ]

        for name, callback in action_defs:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            g.add_action(action)

        self.update_action = g.lookup_action('update')
        self.update_channel_action = g.lookup_action('updateChannel')
        self.edit_channel_action = g.lookup_action('editChannel')
        self.play_action = g.lookup_action('play')
        self.open_action = g.lookup_action('open')
        self.download_action = g.lookup_action('download')
        self.pause_action = g.lookup_action('pause')
        self.episodes_cancel_action = g.lookup_action('cancelFromEpisodes')
        self.delete_action = g.lookup_action('delete')
#        self.toggle_episode_new_action = g.lookup_action('toggleEpisodeNew')
#        self.toggle_episode_lock_action = g.lookup_action('toggleEpisodeLock')
        self.episode_new_action = g.lookup_action('episodeNew')
        self.open_episode_download_folder_action = g.lookup_action('openEpisodeDownloadFolder')
        self.auto_archive_action = g.lookup_action('channelAutoArchive')
        self.episode_lock_action = g.lookup_action('episodeLock')

        action = Gio.SimpleAction.new_stateful(
            'showToolbar', None, GLib.Variant.new_boolean(self.config.show_toolbar))
        action.connect('activate', self.on_itemShowToolbar_activate)
        g.add_action(action)

    def inject_extensions_menu(self):
        # NOTE: Not used with popover menus in adaptive version
        """
        Update Extras/Extensions menu.
        Called at startup and when en/dis-abling extenstions.
        """
        def gen_callback(label, callback):
            return lambda action, param: callback()

        for a in self.extensions_actions:
            self.gPodder.remove_action(a.get_property('name'))
        self.extensions_actions = []

        if self.extensions_menu is None:
            # insert menu section at startup (hides when empty)
            self.extensions_menu = Gio.Menu.new()
            self.application.menu_extras.append_section(_('Extensions'), self.extensions_menu)
        else:
            self.extensions_menu.remove_all()

        extension_entries = gpodder.user_extensions.on_create_menu()
        if extension_entries:
            # populate menu
            for i, (label, callback) in enumerate(extension_entries):
                action_id = 'extensions.action_%d' % i
                action = Gio.SimpleAction.new(action_id)
                action.connect('activate', gen_callback(label, callback))
                self.extensions_actions.append(action)
                self.gPodder.add_action(action)
                itm = Gio.MenuItem.new(label, 'win.' + action_id)
                self.extensions_menu.append_item(itm)

    def find_partial_downloads(self):
        def start_progress_callback(count):
            if count:
                self.partial_downloads_indicator = ProgressIndicator(
                        _('Loading incomplete downloads'),
                        _('Some episodes have not finished downloading in a previous session.'),
                        False, self.get_dialog_parent())
                self.partial_downloads_indicator.on_message(N_(
                    '%(count)d partial file', '%(count)d partial files',
                    count) % {'count': count})

                self.on_show_progress_activate()

        def progress_callback(title, progress):
            self.partial_downloads_indicator.on_message(title)
            self.partial_downloads_indicator.on_progress(progress)

        def finish_progress_callback(resumable_episodes):
            def offer_resuming():
                if resumable_episodes:
                    self.download_episode_list_paused(resumable_episodes)

                    def on_resume_all(button):
                        selection = self.treeDownloads.get_selection()
                        selection.select_all()
                        selected_tasks, _, _, _, _, _ = self.downloads_list_get_selection()
                        selection.unselect_all()
                        self._for_each_task_set_status(selected_tasks, download.DownloadTask.QUEUED)
                        self.message_area.hide()

                    resume_all = Gtk.Button(_('Resume all'))
                    resume_all.connect('clicked', on_resume_all)

                    self.message_area = SimpleMessageArea(
                            _('Incomplete downloads from a previous session were found.'),
                            (resume_all,))
                    self.vboxDownloadStatusWidgets.attach(self.message_area, 0, -1, 1, 1)
                    self.message_area.show_all()
                    self.on_show_progress_activate()
                logger.debug("find_partial_downloads done, calling extensions")
                gpodder.user_extensions.on_find_partial_downloads_done()

            if self.partial_downloads_indicator:
                util.idle_add(self.partial_downloads_indicator.on_finished)
                self.partial_downloads_indicator = None
            util.idle_add(offer_resuming)

        common.find_partial_downloads(self.channels,
                start_progress_callback,
                progress_callback,
                finish_progress_callback)

    def episode_object_by_uri(self, uri):
        """Get an episode object given a local or remote URI

        This can be used to quickly access an episode object
        when all we have is its download filename or episode
        URL (e.g. from external D-Bus calls / signals, etc..)
        """
        if uri.startswith('/'):
            uri = 'file://' + urllib.parse.quote(uri)

        prefix = 'file://' + urllib.parse.quote(gpodder.downloads)

        # By default, assume we can't pre-select any channel
        # but can match episodes simply via the download URL

        def is_channel(c):
            return True

        def is_episode(e):
            return e.url == uri

        if uri.startswith(prefix):
            # File is on the local filesystem in the download folder
            # Try to reduce search space by pre-selecting the channel
            # based on the folder name of the local file

            filename = urllib.parse.unquote(uri[len(prefix):])
            file_parts = [_f for _f in filename.split(os.sep) if _f]

            if len(file_parts) != 2:
                return None

            foldername, filename = file_parts

            def is_channel(c):
                return c.download_folder == foldername

            def is_episode(e):
                return e.download_filename == filename

        # Deep search through channels and episodes for a match
        for channel in filter(is_channel, self.channels):
            for episode in filter(is_episode, channel.get_all_episodes()):
                return episode

        return None

    def on_played(self, start, end, total, file_uri):
        """Handle the "played" signal from a media player"""
        if start == 0 and end == 0 and total == 0:
            # Ignore bogus play event
            return
        elif end < start + 5:
            # Ignore "less than five seconds" segments,
            # as they can happen with seeking, etc...
            return

        logger.debug('Received play action: %s (%d, %d, %d)', file_uri, start, end, total)
        episode = self.episode_object_by_uri(file_uri)

        if episode is not None:
            file_type = episode.file_type()

            now = time.time()
            if total > 0:
                episode.total_time = total
            elif total == 0:
                # Assume the episode's total time for the action
                total = episode.total_time

            assert (episode.current_position_updated is None or
                    now >= episode.current_position_updated)

            episode.current_position = end
            episode.current_position_updated = now
            episode.mark(is_played=True)
            episode.save()
            self.episode_list_status_changed([episode])

            # Submit this action to the webservice
            self.mygpo_client.on_playback_full(episode, start, end, total)

    def on_add_remove_podcasts_mygpo(self):
        actions = self.mygpo_client.get_received_actions()
        if not actions:
            return False

        existing_urls = [c.url for c in self.channels]

        # Columns for the episode selector window - just one...
        columns = (
            ('description', None, None, _('Action')),
        )

        # A list of actions that have to be chosen from
        changes = []

        # Actions that are ignored (already carried out)
        ignored = []

        for action in actions:
            if action.is_add and action.url not in existing_urls:
                changes.append(my.Change(action))
            elif action.is_remove and action.url in existing_urls:
                podcast_object = None
                for podcast in self.channels:
                    if podcast.url == action.url:
                        podcast_object = podcast
                        break
                changes.append(my.Change(action, podcast_object))
            else:
                ignored.append(action)

        # Confirm all ignored changes
        self.mygpo_client.confirm_received_actions(ignored)

        def execute_podcast_actions(selected):
            # In the future, we might retrieve the title from gpodder.net here,
            # but for now, we just use "None" to use the feed-provided title
            title = None
            add_list = [(title, c.action.url)
                    for c in selected if c.action.is_add]
            remove_list = [c.podcast for c in selected if c.action.is_remove]

            # Apply the accepted changes locally
            self.add_podcast_list(add_list)
            self.remove_podcast_list(remove_list, confirm=False)

            # All selected items are now confirmed
            self.mygpo_client.confirm_received_actions(c.action for c in selected)

            # Revert the changes on the server
            rejected = [c.action for c in changes if c not in selected]
            self.mygpo_client.reject_received_actions(rejected)

        def ask():
            # We're abusing the Episode Selector again ;) -- thp
            gPodderEpisodeSelector(self.main_window,
                    ui_folder=os.path.join(gpodder.ui_folders[0], '..', 'adaptive'),
                    title=_('Confirm changes from gpodder.net'),
                    instructions=_('Select the actions you want to carry out.'),
                    episodes=changes,
                    columns=columns,
                    size_attribute=None,
                    ok_button=_('A_pply'),
                    callback=execute_podcast_actions,
                    _config=self.config)

        # There are some actions that need the user's attention
        if changes:
            util.idle_add(ask)
            return True

        # We have no remaining actions - no selection happens
        return False

    def rewrite_urls_mygpo(self):
        # Check if we have to rewrite URLs since the last add
        rewritten_urls = self.mygpo_client.get_rewritten_urls()
        changed = False

        for rewritten_url in rewritten_urls:
            if not rewritten_url.new_url:
                continue

            for channel in self.channels:
                if channel.url == rewritten_url.old_url:
                    logger.info('Updating URL of %s to %s', channel,
                            rewritten_url.new_url)
                    channel.url = rewritten_url.new_url
                    channel.save()
                    changed = True
                    break

        if changed:
            util.idle_add(self.update_episode_list_model)

    def on_send_full_subscriptions(self):
        # Send the full subscription list to the gpodder.net client
        # (this will overwrite the subscription list on the server)
        indicator = ProgressIndicator(_('Uploading subscriptions'),
                _('Your subscriptions are being uploaded to the server.'),
                False, self.get_dialog_parent())

        try:
            self.mygpo_client.set_subscriptions([c.url for c in self.channels])
            util.idle_add(self.show_message, _('List uploaded successfully.'))
        except Exception as e:
            def show_error(e):
                message = str(e)
                if not message:
                    message = e.__class__.__name__
                if message == 'NotFound':
                    message = _(
                        'Could not find your device.\n'
                        '\n'
                        'Check login is a username (not an email)\n'
                        'and that the device name matches one in your account.'
                    )
                self.show_message(html.escape(message),
                        _('Error while uploading'),
                        important=True)
            util.idle_add(show_error, e)

        util.idle_add(indicator.on_finished)

    def on_button_subscribe_clicked(self, button):
        self.on_itemImportChannels_activate(button)

    def on_button_downloads_clicked(self, widget):
        self.downloads_window.show()

    def on_treeview_button_pressed(self, treeview, event):
        if event.window != treeview.get_bin_window():
            return False

        role = getattr(treeview, TreeViewHelper.ROLE)
        if role == TreeViewHelper.ROLE_EPISODES and event.button == 1:
            # Toggle episode "new" status by clicking the icon (bug 1432)
            result = treeview.get_path_at_pos(int(event.x), int(event.y))
            if result is not None:
                path, column, x, y = result
                # The user clicked the icon if she clicked in the first column
                # and the x position is in the area where the icon resides
                if (x < self.EPISODE_LIST_ICON_WIDTH and
                        column == treeview.get_columns()[0]):
                    model = treeview.get_model()
                    cursor_episode = model.get_value(model.get_iter(path),
                            EpisodeListModel.C_EPISODE)

                    new_value = cursor_episode.is_new
                    selected_episodes = self.get_selected_episodes()

                    # Avoid changing anything if the clicked episode is not
                    # selected already - otherwise update all selected
                    if cursor_episode in selected_episodes:
                        for episode in selected_episodes:
                            episode.mark(is_played=new_value)

                        self.update_episode_list_icons(selected=True)
                        self.update_podcast_list_model(selected=True)
                        return True

        return event.button == 3

    def context_popover_show(self, popover, x, y):
        rec = Gdk.Rectangle()
        rec.x, rec.y = x, y
        popover.set_pointing_to(rec)
        popover.popup()

    def on_treeview_podcasts_button_released(self, treeview, event):
        if event.window != treeview.get_bin_window():
            return False
        if event.button == 3:
            return self.treeview_channels_show_context_menu(treeview, event)
        else:
            return False

    def on_treeview_podcasts_long_press(self, gesture, x, y, treeview):
        ev = Dummy(x=x, y=y, button=3)
        return self.treeview_channels_show_context_menu(treeview, ev)

    def on_treeview_episodes_button_released(self, treeview, event):
        if event.window != treeview.get_bin_window():
            return False

        if event.button == 3:
            return self.treeview_available_show_context_menu(treeview, event)
        else:
            return False

    def on_treeview_episodes_long_press(self, gesture, x, y, treeview):
        ev = Dummy(x=x, y=y, button=3, time=Gtk.get_current_event_time())
        return self.treeview_available_show_context_menu(treeview, ev)

    def on_treeview_downloads_button_released(self, treeview, event):
        if event.window != treeview.get_bin_window():
            return False

        return self.treeview_downloads_show_context_menu(treeview, event)

    def on_treeview_downloads_long_press(self, gesture, x, y, treeview):
        ev = Dummy(x=x, y=y, button=3)
        return self.treeview_downloads_show_context_menu(treeview, ev)

    def on_find_podcast_activate(self, *args):
        if self._search_podcasts:
            self._search_podcasts.show_search()

    def init_podcast_list_treeview(self):
        size = cake_size_from_widget(self.treeChannels) * 2
        scale = self.treeChannels.get_scale_factor()
        self.podcast_list_model.set_max_image_size(size, scale)
        # Set up podcast channel tree view widget
        column = Gtk.TreeViewColumn('')
        iconcell = Gtk.CellRendererPixbuf()
        iconcell.set_property('width', size + 10)
        column.pack_start(iconcell, False)
        column.add_attribute(iconcell, 'pixbuf', PodcastListModel.C_COVER)
        column.add_attribute(iconcell, 'visible', PodcastListModel.C_COVER_VISIBLE)
        if scale != 1:
            column.set_cell_data_func(iconcell, draw_iconcell_scale, scale)

        namecell = Gtk.CellRendererText()
        namecell.set_property('ellipsize', Pango.EllipsizeMode.END)
        column.pack_start(namecell, True)
        column.add_attribute(namecell, 'markup', PodcastListModel.C_DESCRIPTION)

        iconcell = Gtk.CellRendererPixbuf()
        iconcell.set_property('xalign', 1.0)
        column.pack_start(iconcell, False)
        column.add_attribute(iconcell, 'pixbuf', PodcastListModel.C_PILL)
        column.add_attribute(iconcell, 'visible', PodcastListModel.C_PILL_VISIBLE)
        if scale != 1:
            column.set_cell_data_func(iconcell, draw_iconcell_scale, scale)

        self.treeChannels.append_column(column)

        self.treeChannels.set_model(self.podcast_list_model.get_filtered_model())
        self.podcast_list_model.widget = self.treeChannels

        # When no podcast is selected, clear the episode list model
        selection = self.treeChannels.get_selection()

        # Set up context menu
        menu = self.application.builder.get_object('channels-context')
        # extensions section, updated in signal handler
        extmenu = Gio.Menu()
        menu.insert_section(3, None, extmenu)
        self.channel_context_menu_helper = ExtensionMenuHelper(
            self.gPodder, extmenu, 'channel_context_action_')
        self.channels_popover = Gtk.Popover.new_from_model(self.treeChannels, menu)
        self.channels_popover.set_position(Gtk.PositionType.BOTTOM)

        # Long press gesture
        lp = Gtk.GestureLongPress.new(self.treeChannels)
        lp.set_touch_only(True)
        lp.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        lp.connect("pressed", self.on_treeview_podcasts_long_press, self.treeChannels)
        setattr(self.treeChannels, "long-press-gesture", lp)

        # Set up type-ahead find for the podcast list
        def on_key_press(treeview, event):
            if event.get_state() & Gdk.ModifierType.CONTROL_MASK:
                # Don't handle type-ahead when control is pressed (so shortcuts
                # with the Ctrl key still work, e.g. Ctrl+A, ...)
                return True
            elif event.keyval in (Gdk.KEY_Right, Gdk.KEY_Return, Gdk.KEY_l):
                path, column = self.treeChannels.get_cursor()
                self.on_treeChannels_row_activated(self.treeChannels, path)
            elif event.keyval in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_j, Gdk.KEY_k):
                # If section markers exist in the treeview, we want to
                # "jump over" them when moving the cursor up and down
                if event.keyval in (Gdk.KEY_Up, Gdk.KEY_k):
                    step = -1
                else:
                    step = 1

                selection = self.treeChannels.get_selection()
                model, it = selection.get_selected()
                if it is None:
                    it = model.get_iter_first()
                    if it is None:
                        return False
                    step = 1

                path = model.get_path(it)
                path = (path[0] + step,)

                if path[0] < 0:
                    # Valid paths must have a value >= 0
                    return True

                try:
                    it = model.get_iter(path)
                except ValueError:
                    # Already at the end of the list
                    return True

                self.treeChannels.set_cursor(path)
            elif event.keyval == Gdk.KEY_Escape:
                self._search_podcasts.hide_search()
            elif event.keyval == Gdk.KEY_Delete:
                return False
            elif event.keyval == Gdk.KEY_BackSpace and self._search_podcasts.search_box.get_property('visible'):
                self._search_podcasts.search_entry.grab_focus_without_selecting()
            else:
                unicode_char_id = Gdk.keyval_to_unicode(event.keyval)
                # < 32 to intercept Delete and Tab events
                if unicode_char_id < 32:
                    return False
                input_char = chr(unicode_char_id)
                self._search_podcasts.show_search(input_char)
            return True

        # Consume arrow keys before native TreeView keyboard handlers
        def on_key_press_treeview(widget, event):
            if event.keyval in (Gdk.KEY_Right, Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_Down):
                return on_key_press(widget, event)
            return False

        self.channelsbox.connect('key-press-event', on_key_press)
        self.treeChannels.connect('key-press-event', on_key_press_treeview)

        self.treeChannels.connect('popup-menu', self.treeview_channels_show_context_menu)

        # Enable separators to the podcast list to separate special podcasts
        # from others (this is used for the "all episodes" view)
        self.treeChannels.set_row_separator_func(PodcastListModel.row_separator_func)

        TreeViewHelper.set(self.treeChannels, TreeViewHelper.ROLE_PODCASTS)

        self._search_podcasts = SearchTreeBar(self.channels_search_bar,
                                           self.entry_search_channels,
                                           self.treeChannels,
                                           self.podcast_list_model,
                                           self.config,
                                           toggle_button=self.header_bar_search_button,
                                           return_cb=self.on_channel_list_go_forward)
        if self.config.ui.gtk.search_always_visible:
            self._search_podcasts.show_search(grab_focus=False)
        else:
            self._search_podcasts.hide_search()

    def on_entry_search_icon_release(self, entry, icon_pos, event):
        """Delete icon on search entry clicked"""
        if icon_pos == Gtk.EntryIconPosition.SECONDARY:
            entry.set_text("")
        return False

    def on_find_episode_activate(self, *args):
        if self._search_episodes:
            self._search_episodes.show_search()

    def set_episode_list_column(self, index, new_value):
        mask = (1 << index)
        if new_value:
            self.config.episode_list_columns |= mask
        else:
            self.config.episode_list_columns &= ~mask

    def update_episode_list_columns_visibility(self):
        columns = TreeViewHelper.get_columns(self.treeAvailable)
        for index, column in enumerate(columns):
            visible = bool(self.config.episode_list_columns & (1 << index))
            column.set_visible(visible)
            self.view_column_actions[index].set_state(GLib.Variant.new_boolean(visible))
        self.treeAvailable.columns_autosize()

    def on_episode_list_header_reordered(self, treeview):
        self.config.ui.gtk.state.main_window.episode_column_order = \
            [column.get_sort_column_id() for column in treeview.get_columns()]

    def on_episode_list_header_sorted(self, column):
        self.config.ui.gtk.state.main_window.episode_column_sort_id = column.get_sort_column_id()
        self.config.ui.gtk.state.main_window.episode_column_sort_order = \
            (column.get_sort_order() is Gtk.SortType.ASCENDING)

    def episode_list_update_sort(self):
        column_id = self.config.ui.gtk.state.main_window.episode_column_sort_id
        sorttype = Gtk.SortType.ASCENDING if self.config.ui.gtk.state.main_window.episode_column_sort_order else Gtk.SortType.DESCENDING
        self.episode_list_model._sorter.set_sort_column_id(column_id, sorttype)

    def on_episode_list_sort_changed(self, action, param):
        column_id = self.menu2sort.get(param.get_string(), EpisodeListModel.C_PUBLISHED)
        prev_id = self.config.ui.gtk.state.main_window.episode_column_sort_id
        order = self.config.ui.gtk.state.main_window.episode_column_sort_order
        if EpisodeListModel.C_PUBLISHED in [column_id, prev_id] and column_id != prev_id:
            order = not order
        self.config.ui.gtk.state.main_window.episode_column_sort_id = column_id
        self.config.ui.gtk.state.main_window.episode_column_sort_order = order
        action.set_state(param)
        self.episode_list_update_sort()
        return True

    def relabel_sort_menubutton(self, action, param):
        sort2text = {
            EpisodeListModel.C_PUBLISHED: 'by date',
            EpisodeListModel.C_TOTAL_TIME: 'by length',
            EpisodeListModel.C_TITLE: 'by title',
        }
        label = self.sort_menubutton.get_child()
        label.set_text(sort2text.get(
            self.config.ui.gtk.state.main_window.episode_column_sort_id, 'by (unknown)'))
        self.sort_popover.popdown()

    def set_sortorder_button_image(self):
        order = self.config.ui.gtk.state.main_window.episode_column_sort_order
        column_id = self.config.ui.gtk.state.main_window.episode_column_sort_id
        order = not order if (column_id == EpisodeListModel.C_PUBLISHED) else order
        image = self.sortorder_button.get_child()
        icon = "view-sort-ascending-symbolic" if order else "view-sort-descending-symbolic"
        image.set_from_icon_name(icon, Gtk.IconSize.BUTTON)

    def on_episode_list_sortorder_clicked(self, button):
        order = not self.config.ui.gtk.state.main_window.episode_column_sort_order
        self.config.ui.gtk.state.main_window.episode_column_sort_order = order
        self.set_sortorder_button_image()
        self.episode_list_update_sort()
        return True

    def on_episode_list_header_clicked(self, button, event):
        if event.button == 1:
            # Require control click to sort episodes, when enabled
            if self.config.ui.gtk.episode_list.ctrl_click_to_sort and (event.state & Gdk.ModifierType.CONTROL_MASK) == 0:
                return True
        elif event.button == 3:
            if self.episode_columns_menu is not None:
                self.episode_columns_menu.popup(None, None, None, None, event.button, event.time)

        return False

    def init_episode_list_treeview(self):
        self.episode_list_model.set_view_mode(self.config.episode_list_view_mode)

        self.view_popover = Gtk.Popover.new_from_model(self.view_menubutton,
            self.application.builder.get_object('view-menu'))
        self.view_menubutton.set_popover(self.view_popover)
        self.relabel_view_menubutton(None, None)

        self.sort_popover = Gtk.Popover.new_from_model(self.sort_menubutton,
            self.application.builder.get_object('sort-menu'))
        self.sort_menubutton.set_popover(self.sort_popover)
        self.relabel_sort_menubutton(None, None)

        # Set up context menu
        menu = self.application.builder.get_object('episodes-context')
        # extensions section, updated in signal handler
        extmenu = Gio.Menu()
        menu.insert_section(3, None, extmenu)
        self.episode_context_menu_helper = ExtensionMenuHelper(
            self.gPodder, extmenu, 'episode_context_action_')
        self.episodes_popover = Gtk.Popover.new_from_model(self.treeAvailable, menu)
        self.episodes_popover.set_position(Gtk.PositionType.BOTTOM)
        self.have_episode_menu_file_items = False

        # Long press gesture
        lp = Gtk.GestureLongPress.new(self.treeAvailable)
        lp.set_touch_only(True)
        lp.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        lp.connect("pressed", self.on_treeview_episodes_long_press, self.treeAvailable)
        setattr(self.treeAvailable, "long-press-gesture", lp)

        # Initialize progress icons
        cake_size = cake_size_from_widget(self.treeAvailable)
        for i in range(EpisodeListModel.PROGRESS_STEPS + 1):
            pixbuf = draw_cake_pixbuf(i /
                   EpisodeListModel.PROGRESS_STEPS, size=cake_size)
            icon_name = 'gpodder-progress-%d' % i
            Gtk.IconTheme.add_builtin_icon(icon_name, cake_size, pixbuf)

        self.treeAvailable.set_model(self.episode_list_model.get_filtered_model())

        TreeViewHelper.set(self.treeAvailable, TreeViewHelper.ROLE_EPISODES)

        iconcell = Gtk.CellRendererPixbuf()
        episode_list_icon_size = Gtk.icon_size_register('episode-list',
            cake_size, cake_size)
        iconcell.set_property('stock-size', episode_list_icon_size)
        iconcell.set_fixed_size(cake_size + 20, -1)
        self.EPISODE_LIST_ICON_WIDTH = cake_size

        namecell = Gtk.CellRendererText()
        namecell.set_property('ellipsize', Pango.EllipsizeMode.END)
        namecolumn = Gtk.TreeViewColumn(_('Episode'))
        namecolumn.pack_start(iconcell, False)
        namecolumn.add_attribute(iconcell, 'icon-name', EpisodeListModel.C_STATUS_ICON)
        namecolumn.pack_start(namecell, True)
        namecolumn.add_attribute(namecell, 'markup', EpisodeListModel.C_DESCRIPTION)
        namecolumn.set_sort_column_id(EpisodeListModel.C_DESCRIPTION)
        namecolumn.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        namecolumn.set_resizable(True)
        namecolumn.set_expand(True)

        lockcell = Gtk.CellRendererPixbuf()
        lockcell.set_fixed_size(40, -1)
        lockcell.set_property('stock-size', Gtk.IconSize.MENU)
        lockcell.set_property('icon-name', 'emblem-readonly')
        namecolumn.pack_start(lockcell, False)
        namecolumn.add_attribute(lockcell, 'visible', EpisodeListModel.C_LOCKED)

#        sizecell = Gtk.CellRendererText()
#        sizecell.set_property('xalign', 1)
#        sizecolumn = Gtk.TreeViewColumn(_('Size'), sizecell, text=EpisodeListModel.C_FILESIZE_TEXT)
#        sizecolumn.set_sort_column_id(EpisodeListModel.C_FILESIZE)

#        timecell = Gtk.CellRendererText()
#        timecell.set_property('xalign', 1)
#        timecolumn = Gtk.TreeViewColumn(_('Duration'), timecell, text=EpisodeListModel.C_TIME)
#        timecolumn.set_sort_column_id(EpisodeListModel.C_TOTAL_TIME)

        releasecell = Gtk.CellRendererText()
#        releasecolumn = Gtk.TreeViewColumn(_('Released'), releasecell, text=EpisodeListModel.C_PUBLISHED_TEXT)
#        releasecolumn.set_sort_column_id(EpisodeListModel.C_PUBLISHED)
        namecolumn.pack_end(releasecell, True)
        namecolumn.add_attribute(releasecell, 'text', EpisodeListModel.C_TIME_AND_PUBLISHED_TEXT)

        namecolumn.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        namecolumn.set_resizable(True)
        namecolumn.set_expand(True)
        namecolumn.set_reorderable(True)
        self.treeAvailable.append_column(namecolumn)

        # EpisodeListModel.C_PUBLISHED is not available in config.py, set it here on first run
        if not self.config.ui.gtk.state.main_window.episode_column_sort_id:
            self.config.ui.gtk.state.main_window.episode_column_sort_id = EpisodeListModel.C_PUBLISHED

#        for itemcolumn in (sizecolumn, timecolumn, releasecolumn):
#            itemcolumn.set_reorderable(True)
#            self.treeAvailable.append_column(itemcolumn)
#            TreeViewHelper.register_column(self.treeAvailable, itemcolumn)
#
#        # Add context menu to all tree view column headers
#        for column in self.treeAvailable.get_columns():
#            label = Gtk.Label(label=column.get_title())
#            label.show_all()
#            column.set_widget(label)
#
#            w = column.get_widget()
#            while w is not None and not isinstance(w, Gtk.Button):
#                w = w.get_parent()
#
#            w.connect('button-release-event', self.on_episode_list_header_clicked)
#
#            # Restore column sorting
#            if column.get_sort_column_id() == self.config.ui.gtk.state.main_window.episode_column_sort_id:
#                self.episode_list_model._sorter.set_sort_column_id(Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID,
#                    Gtk.SortType.DESCENDING)
#                self.episode_list_model._sorter.set_sort_column_id(column.get_sort_column_id(),
#                    Gtk.SortType.ASCENDING if self.config.ui.gtk.state.main_window.episode_column_sort_order
#                        else Gtk.SortType.DESCENDING)
#            # Save column sorting when user clicks column headers
#            column.connect('clicked', self.on_episode_list_header_sorted)

        # Restore column sorting
        self.episode_list_model._sorter.set_sort_column_id(Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID,
            Gtk.SortType.DESCENDING)
        self.relabel_sort_menubutton(None, None)
        self.set_sortorder_button_image()
        self.episode_list_update_sort()

        def restore_column_ordering():
            prev_column = None
            for col in self.config.ui.gtk.state.main_window.episode_column_order:
                for column in self.treeAvailable.get_columns():
                    if col is column.get_sort_column_id():
                        break
                else:
                    # Column ID not found, abort
                    # Manually re-ordering columns should fix the corrupt setting
                    break
                self.treeAvailable.move_column_after(column, prev_column)
                prev_column = column
#            # Save column ordering when user drags column headers
#            self.treeAvailable.connect('columns-changed', self.on_episode_list_header_reordered)

        # Delay column ordering until shown to prevent "Negative content height" warnings for themes with vertical padding or borders
        util.idle_add(restore_column_ordering)

#        # For each column that can be shown/hidden, add a menu item
#        self.view_column_actions = []
#        columns = TreeViewHelper.get_columns(self.treeAvailable)
#
#        def on_visible_toggled(action, param, index):
#            state = action.get_state()
#            self.set_episode_list_column(index, not state)
#            action.set_state(GLib.Variant.new_boolean(not state))
#
#        for index, column in enumerate(columns):
#            name = 'showColumn%i' % index
#            action = Gio.SimpleAction.new_stateful(
#                name, None, GLib.Variant.new_boolean(False))
#            action.connect('activate', on_visible_toggled, index)
#            self.main_window.add_action(action)
#            self.view_column_actions.append(action)
#            self.application.menu_view_columns.insert(index, column.get_title(), 'win.' + name)
#
#        self.episode_columns_menu = Gtk.Menu.new_from_model(self.application.menu_view_columns)
#        self.episode_columns_menu.attach_to_widget(self.main_window)
#        # Update the visibility of the columns and the check menu items
#        self.update_episode_list_columns_visibility()

        # Set up type-ahead find for the episode list
        def on_key_press(treeview, event):
            if event.get_state() & Gdk.ModifierType.CONTROL_MASK:
                # Don't handle type-ahead when control is pressed (so shortcuts
                # with the Ctrl key still work, e.g. Ctrl+A, ...)
                return False
            elif event.keyval in (Gdk.KEY_Left, Gdk.KEY_Escape, Gdk.KEY_BackSpace, Gdk.KEY_h):
                if event.keyval == Gdk.KEY_Escape and self._search_episodes.search_box.get_property('visible'):
                    self._search_episodes.hide_search()
                elif event.keyval == Gdk.KEY_BackSpace and self._search_episodes.search_box.get_property('visible'):
                    self._search_episodes.search_entry.grab_focus_without_selecting()
                else:
                    self.treeChannels.grab_focus()
                    self.leaflet.navigate(Handy.NavigationDirection.BACK)
            elif event.keyval in (Gdk.KEY_Right, Gdk.KEY_Return, Gdk.KEY_l):
                path, column = self.treeAvailable.get_cursor()
                if path is not None and path[0] >= 0:
                    self.on_treeAvailable_row_activated(self.treeAvailable, path, column)
            elif event.keyval in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_j, Gdk.KEY_k):
                path, column = self.treeAvailable.get_cursor()
                step = -1 if event.keyval in (Gdk.KEY_Up, Gdk.KEY_k) else 1
                model = self.treeAvailable.get_model()
                if path is None:
                    if model is None or model.get_iter_first() is None:
                        return True
                    else:
                        path = (0,)
                else:
                    path = (path[0] + step,)
                if path[0] < 0:
                    return True
                try:
                    it = model.get_iter(path)
                except ValueError:
                    return True
                self.treeAvailable.set_cursor(path)
            else:
                unicode_char_id = Gdk.keyval_to_unicode(event.keyval)
                # < 32 to intercept Delete and Tab events
                if unicode_char_id < 32:
                    return False
                input_char = chr(unicode_char_id)
                self._search_episodes.show_search(input_char)
            return True

        # Consume arrow keys before native TreeView keyboard handlers
        def on_key_press_treeview(widget, event):
            if event.keyval in (Gdk.KEY_Right, Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_Down):
                return on_key_press(widget, event)
            return False

        self.episodesbox.connect('key-press-event', on_key_press)
        self.treeAvailable.connect('key-press-event', on_key_press_treeview)

        self.treeAvailable.connect('popup-menu', self.treeview_available_show_context_menu)

#        self.treeAvailable.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK,
#                (('text/uri-list', 0, 0),), Gdk.DragAction.COPY)
#
#        def drag_data_get(tree, context, selection_data, info, timestamp):
#            uris = ['file://' + e.local_filename(create=False)
#                    for e in self.get_selected_episodes()
#                    if e.was_downloaded(and_exists=True)]
#            selection_data.set_uris(uris)
#        self.treeAvailable.connect('drag-data-get', drag_data_get)

        selection = self.treeAvailable.get_selection()
        selection.set_mode(Gtk.SelectionMode.MULTIPLE)
        self.episode_selection_handler_id = selection.connect('changed', self.on_episode_list_selection_changed)

        self._search_episodes = SearchTreeBar(self.episodes_search_bar,
                                           self.entry_search_episodes,
                                           self.treeAvailable,
                                           self.episode_list_model,
                                           self.config,
                                           toggle_button=self.episode_search_toggle,
                                           return_cb=self.on_shownotes_selected_episodes)
        if self.config.ui.gtk.search_always_visible:
            self._search_episodes.show_search(grab_focus=False)
        else:
            self._search_episodes.hide_search()

    def on_episode_list_selection_changed(self, selection):
        # Update the toolbar buttons
        self.play_or_download()
        # and the shownotes
        eps = self.get_selected_episodes()
        if len(eps) > 1:
            self.deck.set_can_swipe_forward(False)
        self.shownotes_object.set_episodes(eps)

    def on_download_list_selection_changed(self, selection):
        # if self.wNotebook.get_current_page() > 0:
        # # Update the toolbar buttons
        # self.play_or_download()
        pass

    def init_download_list_treeview(self):
        # columns and renderers for "download progress" tab
        # First column: [ICON] Episodename
        column = Gtk.TreeViewColumn(_('Episode'))

        cell = Gtk.CellRendererPixbuf()
        cell.set_property('stock-size', Gtk.IconSize.BUTTON)
        column.pack_start(cell, False)
        column.add_attribute(cell, 'icon-name',
                DownloadStatusModel.C_ICON_NAME)

        cell = Gtk.CellRendererText()
        cell.set_property('ellipsize', Pango.EllipsizeMode.END)
        column.pack_start(cell, True)
        column.add_attribute(cell, 'markup', DownloadStatusModel.C_NAME)
        column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column.set_expand(True)
        self.treeDownloads.append_column(column)

        # Second column: Progress
        cell = Gtk.CellRendererProgress()
        cell.set_property('yalign', .5)
        cell.set_property('ypad', 6)
        column = Gtk.TreeViewColumn(_('Progress'), cell,
                value=DownloadStatusModel.C_PROGRESS,
                text=DownloadStatusModel.C_PROGRESS_TEXT)
        column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column.set_expand(False)
        self.treeDownloads.append_column(column)
        column.set_property('min-width', 150)
        column.set_property('max-width', 150)

        self.treeDownloads.set_model(self.download_status_model)
        TreeViewHelper.set(self.treeDownloads, TreeViewHelper.ROLE_DOWNLOADS)

        # Set up context menu
        menu = self.application.builder.get_object('downloads-context')
        self.downloads_popover = Gtk.Popover.new_from_model(self.treeDownloads, menu)
        self.downloads_popover.set_position(Gtk.PositionType.BOTTOM)

        # Long press gesture
        lp = Gtk.GestureLongPress.new(self.treeDownloads)
        lp.set_touch_only(True)
        lp.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        lp.connect("pressed", self.on_treeview_downloads_long_press, self.treeDownloads)
        setattr(self.treeDownloads, "long-press-gesture", lp)

        self.treeDownloads.connect('popup-menu', self.treeview_downloads_show_context_menu)

        # enable multiple selection support
        selection = self.treeDownloads.get_selection()
        selection.set_mode(Gtk.SelectionMode.MULTIPLE)
        self.download_selection_handler_id = selection.connect('changed', self.on_download_list_selection_changed)
        self.treeDownloads.set_search_equal_func(TreeViewHelper.make_search_equal_func(DownloadStatusModel))

    def on_treeview_expose_event(self, treeview, ctx):
        model = treeview.get_model()
        if (model is not None and model.get_iter_first() is not None):
            return False

        role = getattr(treeview, TreeViewHelper.ROLE, None)
        if role is None:
            return False

        width = treeview.get_allocated_width()
        height = treeview.get_allocated_height()

        if role == TreeViewHelper.ROLE_EPISODES:
            if self.config.episode_list_view_mode != EpisodeListModel.VIEW_ALL:
                text = _('No episodes in current view')
            else:
                text = _('No episodes available')
        elif role == TreeViewHelper.ROLE_PODCASTS:
            if self.config.episode_list_view_mode != \
                    EpisodeListModel.VIEW_ALL and \
                    self.config.podcast_list_hide_boring and \
                    len(self.channels) > 0:
                text = _('No podcasts in this view')
            else:
                text = _('No subscriptions')
        elif role == TreeViewHelper.ROLE_DOWNLOADS:
            text = _('No active tasks')
        else:
            raise Exception('on_treeview_expose_event: unknown role')

        draw_text_box_centered(ctx, treeview, width, height, text, None, None)
        return True

    def on_show_progress_activate(self, *args):
        self.progress_window.show_all()
        self.progress_window.present()

    def on_progress_close_button_clicked(self, *args):
        self.progress_window.hide()
        return True

    def set_download_list_state(self, state):
        if state == gPodderSyncUI.DL_ADDING_TASKS:
            self.things_adding_tasks += 1
        elif state == gPodderSyncUI.DL_ADDED_TASKS:
            self.things_adding_tasks -= 1
        if not self.download_list_update_enabled:
            self.update_downloads_list()
            GObject.timeout_add(1500, self.update_downloads_list)
            self.download_list_update_enabled = True

    def cleanup_downloads(self):
        model = self.download_status_model

        all_tasks = [(Gtk.TreeRowReference.new(model, row.path), row[0]) for row in model]
        changed_episode_urls = set()
        for row_reference, task in all_tasks:
            if task.status in (task.DONE, task.CANCELLED):
                model.remove(model.get_iter(row_reference.get_path()))
                try:
                    # We don't "see" this task anymore - remove it;
                    # this is needed, so update_episode_list_icons()
                    # below gets the correct list of "seen" tasks
                    self.download_tasks_seen.remove(task)
                except KeyError as key_error:
                    pass
                changed_episode_urls.add(task.url)
                # Tell the task that it has been removed (so it can clean up)
                task.removed_from_list()

        # Tell the podcasts tab to update icons for our removed podcasts
        self.update_episode_list_icons(changed_episode_urls)

        # Update the downloads list one more time
        self.update_downloads_list(can_call_cleanup=False)

    def add_download_task_monitor(self, monitor):
        self.download_task_monitors.add(monitor)
        model = self.download_status_model
        if model is None:
            model = ()
        for row in model.get_model():
            task = row[self.download_status_model.C_TASK]
            monitor.task_updated(task)

    def remove_download_task_monitor(self, monitor):
        self.download_task_monitors.remove(monitor)

    def set_download_progress(self, progress):
        gpodder.user_extensions.on_download_progress(progress)

    def update_downloads_list(self, can_call_cleanup=True):
        try:
            model = self.download_status_model

            downloading, synchronizing, pausing, cancelling, queued, paused, failed, finished, others = (0,) * 9
            total_speed, total_size, done_size = 0, 0, 0
            files_downloading = 0

            # Keep a list of all download tasks that we've seen
            download_tasks_seen = set()

            # Do not go through the list of the model is not (yet) available
            if model is None:
                model = ()

            for row in model:
                self.download_status_model.request_update(row.iter)

                task = row[self.download_status_model.C_TASK]
                speed, size, status, progress, activity = task.speed, task.total_size, task.status, task.progress, task.activity

                # Let the download task monitors know of changes
                for monitor in self.download_task_monitors:
                    monitor.task_updated(task)

                total_size += size
                done_size += size * progress

                download_tasks_seen.add(task)

                if status == download.DownloadTask.DOWNLOADING:
                    if activity == download.DownloadTask.ACTIVITY_DOWNLOAD:
                        downloading += 1
                        files_downloading += 1
                        total_speed += speed
                    elif activity == download.DownloadTask.ACTIVITY_SYNCHRONIZE:
                        synchronizing += 1
                    else:
                        others += 1
                elif status == download.DownloadTask.PAUSING:
                    pausing += 1
                    if activity == download.DownloadTask.ACTIVITY_DOWNLOAD:
                        files_downloading += 1
                elif status == download.DownloadTask.CANCELLING:
                    cancelling += 1
                    if activity == download.DownloadTask.ACTIVITY_DOWNLOAD:
                        files_downloading += 1
                elif status == download.DownloadTask.QUEUED:
                    queued += 1
                elif status == download.DownloadTask.PAUSED:
                    paused += 1
                elif status == download.DownloadTask.FAILED:
                    failed += 1
                elif status == download.DownloadTask.DONE:
                    finished += 1
                else:
                    others += 1

            # TODO: 'others' is not used

            # Remember which tasks we have seen after this run
            self.download_tasks_seen = download_tasks_seen

            text = [_('Progress')]
            if downloading + synchronizing + pausing + cancelling + queued + paused + failed > 0:
                s = []
                if downloading > 0:
                    s.append(N_('%(count)d active', '%(count)d active', downloading) % {'count': downloading})
                if synchronizing > 0:
                    s.append(N_('%(count)d active', '%(count)d active', synchronizing) % {'count': synchronizing})
                if pausing > 0:
                    s.append(N_('%(count)d pausing', '%(count)d pausing', pausing) % {'count': pausing})
                if cancelling > 0:
                    s.append(N_('%(count)d cancelling', '%(count)d cancelling', cancelling) % {'count': cancelling})
                if queued > 0:
                    s.append(N_('%(count)d queued', '%(count)d queued', queued) % {'count': queued})
                if paused > 0:
                    s.append(N_('%(count)d paused', '%(count)d paused', paused) % {'count': paused})
                if failed > 0:
                    s.append(N_('%(count)d failed', '%(count)d failed', failed) % {'count': failed})
                text.append(' (' + ', '.join(s) + ')')
                self.labelDownloads.set_text(''.join(text))
                self.transfer_revealer.set_reveal_child(True)
            else:
                # Try to change progress text after revealer transition
                util.idle_add(self.labelDownloads.set_text, ''.join(text))
                self.transfer_revealer.set_reveal_child(False)
            self.progress_window.set_title(''.join(text))

            title = [self.default_title]

            # Accessing task.status_changed has the side effect of re-setting
            # the changed flag, but we only do it once here so that's okay
            channel_urls = [task.podcast_url for task in
                    self.download_tasks_seen if task.status_changed]
            episode_urls = [task.url for task in self.download_tasks_seen]

            if files_downloading > 0:
                title.append(N_('downloading %(count)d file',
                                'downloading %(count)d files',
                                files_downloading) % {'count': files_downloading})

                if total_size > 0:
                    percentage = 100.0 * done_size / total_size
                else:
                    percentage = 0.0
                self.set_download_progress(percentage / 100)
                total_speed = util.format_filesize(total_speed)
                title[1] += ' (%d%%, %s/s)' % (percentage, total_speed)
            if synchronizing > 0:
                title.append(N_('synchronizing %(count)d file',
                                'synchronizing %(count)d files',
                                synchronizing) % {'count': synchronizing})
            if queued > 0:
                title.append(N_('%(queued)d task queued',
                                '%(queued)d tasks queued',
                                queued) % {'queued': queued})
            if (downloading + synchronizing + pausing + cancelling + queued) == 0 and self.things_adding_tasks == 0:
                self.set_download_progress(1.)
                self.downloads_finished(self.download_tasks_seen)
                gpodder.user_extensions.on_all_episodes_downloaded()
                logger.info('All tasks have finished.')

                # Remove finished episodes
                if self.config.ui.gtk.download_list.remove_finished and can_call_cleanup:
                    self.cleanup_downloads()

                # Stop updating the download list here
                self.download_list_update_enabled = False

            self.gPodder.set_title(' - '.join(title))

            self.update_episode_list_icons(episode_urls)
            self.play_or_download()
            if channel_urls:
                self.update_podcast_list_model(channel_urls)

            return self.download_list_update_enabled
        except Exception as e:
            logger.error('Exception happened while updating download list.', exc_info=True)
            self.show_message(
                '%s\n\n%s' % (_('Please report this problem and restart gPodder:'), html.escape(str(e))),
                _('Unhandled exception'), important=True)
            # We return False here, so the update loop won't be called again,
            # that's why we require the restart of gPodder in the message.
            return False

    def on_config_changed(self, *args):
        util.idle_add(self._on_config_changed, *args)

    def _on_config_changed(self, name, old_value, new_value):
        if name == 'ui.gtk.toolbar':
            # self.toolbar.set_property('visible', new_value)
            pass
        elif name in ('ui.gtk.episode_list.descriptions',
                'ui.gtk.episode_list.always_show_new'):
            self.update_episode_list_model()
        elif name in ('auto.update.enabled', 'auto.update.frequency'):
            self.restart_auto_update_timer()
        elif name in ('ui.gtk.podcast_list.all_episodes',
                'ui.gtk.podcast_list.sections'):
            # Force a update of the podcast list model
            self.update_podcast_list_model()
        elif name == 'ui.gtk.episode_list.columns':
            # self.update_episode_list_columns_visibility()
            pass

    def on_treeview_query_tooltip(self, treeview, x, y, keyboard_tooltip, tooltip):
        # With get_bin_window, we get the window that contains the rows without
        # the header. The Y coordinate of this window will be the height of the
        # treeview header. This is the amount we have to subtract from the
        # event's Y coordinate to get the coordinate to pass to get_path_at_pos
        (x_bin, y_bin) = treeview.get_bin_window().get_position()
        x -= x_bin
        y -= y_bin
        (path, column, rx, ry) = treeview.get_path_at_pos(x, y) or (None,) * 4

        if not getattr(treeview, TreeViewHelper.CAN_TOOLTIP) or x > 50 or (column is not None and column != treeview.get_columns()[0]):
            setattr(treeview, TreeViewHelper.LAST_TOOLTIP, None)
            return False

        if path is not None:
            model = treeview.get_model()
            iter = model.get_iter(path)
            role = getattr(treeview, TreeViewHelper.ROLE)

            if role == TreeViewHelper.ROLE_EPISODES:
                id = model.get_value(iter, EpisodeListModel.C_URL)
            elif role == TreeViewHelper.ROLE_PODCASTS:
                id = model.get_value(iter, PodcastListModel.C_URL)
                if id == '-':
                    # Section header - no tooltip here (for now at least)
                    return False

            last_tooltip = getattr(treeview, TreeViewHelper.LAST_TOOLTIP)
            if last_tooltip is not None and last_tooltip != id:
                setattr(treeview, TreeViewHelper.LAST_TOOLTIP, None)
                return False
            setattr(treeview, TreeViewHelper.LAST_TOOLTIP, id)

            if role == TreeViewHelper.ROLE_EPISODES:
                description = model.get_value(iter, EpisodeListModel.C_TOOLTIP)
                if description:
                    tooltip.set_text(description)
                else:
                    return False
            elif role == TreeViewHelper.ROLE_PODCASTS:
                channel = model.get_value(iter, PodcastListModel.C_CHANNEL)
                if channel is None or not hasattr(channel, 'title'):
                    return False
                error_str = model.get_value(iter, PodcastListModel.C_ERROR)
                if error_str:
                    error_str = _('Feedparser error: %s') % html.escape(error_str.strip())
                    error_str = '<span foreground="#ff0000">%s</span>' % error_str

                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                box.set_border_width(5)

                heading = Gtk.Label()
                heading.set_max_width_chars(60)
                heading.set_alignment(0, 1)
                heading.set_markup('<b><big>%s</big></b>\n<small>%s</small>' % (html.escape(channel.title), html.escape(channel.url)))
                box.add(heading)

                box.add(Gtk.HSeparator())

                if channel._update_error is not None:
                    description = _('ERROR: %s') % channel._update_error
                elif len(channel.description) < 500:
                    description = channel.description
                else:
                    pos = channel.description.find('\n\n')
                    if pos == -1 or pos > 500:
                        description = channel.description[:498] + '[...]'
                    else:
                        description = channel.description[:pos]

                description = Gtk.Label(label=description)
                description.set_max_width_chars(60)
                if error_str:
                    description.set_markup(error_str)
                description.set_alignment(0, 0)
                description.set_line_wrap(True)
                box.add(description)

                box.show_all()
                tooltip.set_custom(box)

            return True

        setattr(treeview, TreeViewHelper.LAST_TOOLTIP, None)
        return False

    def treeview_allow_tooltips(self, treeview, allow):
        setattr(treeview, TreeViewHelper.CAN_TOOLTIP, allow)

    def treeview_handle_context_menu_click(self, treeview, event):
        if event is None:
            selection = treeview.get_selection()
            return selection.get_selected_rows()

        x, y = int(event.x), int(event.y)
        path, column, rx, ry = treeview.get_path_at_pos(x, y) or (None,) * 4

        selection = treeview.get_selection()
        model, paths = selection.get_selected_rows()

        if path is None or (path not in paths and
                event.button == 3):
            # We have right-clicked, but not into the selection,
            # assume we don't want to operate on the selection
            paths = []

        if (path is not None and not paths and
                event.button == 3):
            # No selection or clicked outside selection;
            # select the single item where we clicked
            treeview.grab_focus()
            treeview.set_cursor(path, column, 0)
            paths = [path]

        if not paths:
            # Unselect any remaining items (clicked elsewhere)
            if not treeview.is_rubber_banding_active():
                selection.unselect_all()

        return model, paths

    def downloads_list_get_selection(self, model=None, paths=None):
        if model is None and paths is None:
            selection = self.treeDownloads.get_selection()
            model, paths = selection.get_selected_rows()

        can_force, can_queue, can_pause, can_cancel, can_remove = (True,) * 5
        selected_tasks = [(Gtk.TreeRowReference.new(model, path),
                           model.get_value(model.get_iter(path),
                           DownloadStatusModel.C_TASK)) for path in paths]

        for row_reference, task in selected_tasks:
            if task.status != download.DownloadTask.QUEUED:
                can_force = False
            if not task.can_queue():
                can_queue = False
            if not task.can_pause():
                can_pause = False
            if not task.can_cancel():
                can_cancel = False
            if not task.can_remove():
                can_remove = False

        return selected_tasks, can_force, can_queue, can_pause, can_cancel, can_remove

    def downloads_finished(self, download_tasks_seen):
        # Separate tasks into downloads & syncs
        # Since calling notify_as_finished or notify_as_failed clears the flag,
        # need to iterate through downloads & syncs separately, else all sync
        # tasks will have their flags cleared if we do downloads first

        def filter_by_activity(activity, tasks):
            return [task for task in tasks if task.activity == activity]

        download_tasks = filter_by_activity(download.DownloadTask.ACTIVITY_DOWNLOAD,
                download_tasks_seen)

        finished_downloads = [str(task)
                for task in download_tasks if task.notify_as_finished()]
        failed_downloads = ['%s (%s)' % (task, task.error_message)
                for task in download_tasks if task.notify_as_failed()]

        sync_tasks = filter_by_activity(download.DownloadTask.ACTIVITY_SYNCHRONIZE,
                download_tasks_seen)

        finished_syncs = [task for task in sync_tasks if task.notify_as_finished()]
        failed_syncs = [task for task in sync_tasks if task.notify_as_failed()]

        # Note that 'finished_ / failed_downloads' is a list of strings
        # Whereas 'finished_ / failed_syncs' is a list of SyncTask objects

        if finished_downloads and failed_downloads:
            message = self.format_episode_list(finished_downloads, 5)
            message += '\n\n<i>%s</i>\n' % _('Could not download some episodes:')
            message += self.format_episode_list(failed_downloads, 5)
            self.show_message(message, _('Downloads finished'))
        elif finished_downloads:
            message = self.format_episode_list(finished_downloads)
            self.show_message(message, _('Downloads finished'))
        elif failed_downloads:
            message = self.format_episode_list(failed_downloads)
            self.show_message(message, _('Downloads failed'))

        if finished_syncs and failed_syncs:
            message = self.format_episode_list(list(map((
                lambda task: str(task)), finished_syncs)), 5)
            message += '\n\n<i>%s</i>\n' % _('Could not sync some episodes:')
            message += self.format_episode_list(list(map((
                lambda task: str(task)), failed_syncs)), 5)
            self.show_message(message, _('Device synchronization finished'), True)
        elif finished_syncs:
            message = self.format_episode_list(list(map((
                lambda task: str(task)), finished_syncs)))
            self.show_message(message, _('Device synchronization finished'))
        elif failed_syncs:
            message = self.format_episode_list(list(map((
                lambda task: str(task)), failed_syncs)))
            self.show_message(message, _('Device synchronization failed'), True)

        # Do post-sync processing if required
        for task in finished_syncs:
            if self.config.device_sync.after_sync.mark_episodes_played:
                logger.info('Marking as played on transfer: %s', task.episode.url)
                task.episode.mark(is_played=True)

            if self.config.device_sync.after_sync.delete_episodes:
                logger.info('Removing episode after transfer: %s', task.episode.url)
                task.episode.delete_from_disk()

            self.sync_ui.device.close()

        # Update icon list to show changes, if any
        self.update_episode_list_icons(all=True)
        self.update_podcast_list_model()

    def format_episode_list(self, episode_list, max_episodes=10):
        """
        Format a list of episode names for notifications

        Will truncate long episode names and limit the amount of
        episodes displayed (max_episodes=10).

        The episode_list parameter should be a list of strings.
        """
        MAX_TITLE_LENGTH = 100

        result = []
        for title in episode_list[:min(len(episode_list), max_episodes)]:
            # Bug 1834: make sure title is a unicode string,
            # so it may be cut correctly on UTF-8 char boundaries
            title = util.convert_bytes(title)
            if len(title) > MAX_TITLE_LENGTH:
                middle = (MAX_TITLE_LENGTH // 2) - 2
                title = '%s...%s' % (title[0:middle], title[-middle:])
            result.append(html.escape(title))
            result.append('\n')

        more_episodes = len(episode_list) - max_episodes
        if more_episodes > 0:
            result.append('(...')
            result.append(N_('%(count)d more episode',
                             '%(count)d more episodes',
                             more_episodes) % {'count': more_episodes})
            result.append('...)')

        return (''.join(result)).strip()

    def queue_task(self, task, force_start):
        if force_start:
            self.download_queue_manager.force_start_task(task)
        else:
            self.download_queue_manager.queue_task(task)

    def _for_each_task_set_status(self, tasks, status, force_start=False):
        episode_urls = set()
        model = self.treeDownloads.get_model()
        for row_reference, task in tasks:
            with task:
                if status == download.DownloadTask.QUEUED:
                    # Only queue task when it's paused/failed/cancelled (or forced)
                    if task.can_queue() or force_start:
                        # add the task back in if it was already cleaned up
                        # (to trigger this cancel one downloads in the active list, cancel all
                        # other downloads, quickly right click on the cancelled on one to get
                        # the context menu, wait until the active list is cleared, and then
                        # then choose download)
                        if task not in self.download_tasks_seen:
                            self.download_status_model.register_task(task, False)
                            self.download_tasks_seen.add(task)

                        self.queue_task(task, force_start)
                        self.set_download_list_state(gPodderSyncUI.DL_ONEOFF)
                elif status == download.DownloadTask.CANCELLING:
                    logger.info(("cancelling task %s" % task.status))
                    task.cancel()
                elif status == download.DownloadTask.PAUSING:
                    task.pause()
                elif status is None:
                    if task.can_cancel():
                        task.cancel()
                    path = row_reference.get_path()
                    # path isn't set if the item has already been removed from the list
                    # (to trigger this cancel one downloads in the active list, cancel all
                    # other downloads, quickly right click on the cancelled on one to get
                    # the context menu, wait until the active list is cleared, and then
                    # then choose remove from list)
                    if path:
                        model.remove(model.get_iter(path))
                        # Remember the URL, so we can tell the UI to update
                        try:
                            # We don't "see" this task anymore - remove it;
                            # this is needed, so update_episode_list_icons()
                            # below gets the correct list of "seen" tasks
                            self.download_tasks_seen.remove(task)
                        except KeyError as key_error:
                            pass
                        episode_urls.add(task.url)
                        # Tell the task that it has been removed (so it can clean up)
                        task.removed_from_list()
                else:
                    # We can (hopefully) simply set the task status here
                    task.status = status
        # Tell the podcasts tab to update icons for our removed podcasts
        self.update_episode_list_icons(episode_urls)
        # Update the tab title and downloads list
        self.update_downloads_list()

    def treeview_downloads_show_context_menu(self, treeview, event=None):
        model, paths = self.treeview_handle_context_menu_click(treeview, event)
        if not paths:
            return not treeview.is_rubber_banding_active()

        if event is None or event.button == 3:
            selected_tasks, can_force, can_queue, can_pause, can_cancel, can_remove = \
                    self.downloads_list_get_selection(model, paths)

            def make_menu_item(label, action_name, tasks=None, status=None, sensitive=True, force_start=False):
                self.application.remove_action(action_name)
                action = Gio.SimpleAction.new(action_name)
                action.connect("activate",
                        lambda _a, _b: self._for_each_task_set_status(
                            tasks, status, force_start=force_start))
                self.application.add_action(action)
                action.set_enabled(sensitive)
                return Gio.MenuItem.new(label, 'app.' + action_name)

            menu = self.application.builder.get_object('downloads-context')
            menu.remove_all()
            if can_force:
                item = make_menu_item(_('Start download now'), 'setDownloadQueued',
                    selected_tasks, download.DownloadTask.QUEUED, force_start=True)
            else:
                item = make_menu_item(_('Download'), 'setDownloadQueued',
                    selected_tasks, download.DownloadTask.QUEUED, can_queue)
            menu.append_item(item)
            menu.append_item(make_menu_item(_('Cancel'), 'setDownloadCancelled',
                selected_tasks, download.DownloadTask.CANCELLING))
            menu.append_item(make_menu_item(_('Pause'), 'media-playback-pause',
                                       selected_tasks,
                                       download.DownloadTask.PAUSING, can_pause))
            rmenu = Gio.Menu()
            rmenu.append_item(make_menu_item(_('Remove from list'), 'setDownloadRemove',
                selected_tasks, sensitive=can_remove))
            menu.append_section(None, rmenu)

            if event is None:
                func = TreeViewHelper.make_popup_position_func(treeview)
                x, y, unused = func(None)
            else:
                x, y = event.x, event.y
            self.context_popover_show(self.downloads_popover, x, y)
            return True

    def on_mark_episodes_as_old(self, item, *args):
        assert self.active_channel is not None

        for episode in self.active_channel.get_all_episodes():
            if not episode.was_downloaded(and_exists=True):
                episode.mark(is_played=True)

        self.update_podcast_list_model(selected=True)
        self.update_episode_list_icons(all=True)

    def on_open_download_folder(self, item, *args):
        assert self.active_channel is not None
        util.gui_open(self.active_channel.save_dir, gui=self)

    def on_open_episode_download_folder(self, unused1=None, unused2=None):
        episodes = self.get_selected_episodes()
        assert len(episodes) == 1
        util.gui_open(episodes[0].parent.save_dir, gui=self)

    def treeview_channels_show_context_menu(self, treeview, event=None):
        model, paths = self.treeview_handle_context_menu_click(treeview, event)
        if not paths:
            return True

        # Check for valid channel id, if there's no id then
        # assume that it is a proxy channel or equivalent
        # and cannot be operated with right click
        if self.active_channel.id is None:
            return True

        if event is None or event.button == 3:
            entries = [(label, None if func is None else lambda a, b: func(self.active_channel))
                for label, func in list(gpodder.user_extensions.on_channel_context_menu(self.active_channel) or [])]
            self.channel_context_menu_helper.replace_entries(entries)

            if event is None:
                func = TreeViewHelper.make_popup_position_func(treeview)
                x, y, unused = func(None)
            else:
                x, y = event.x, event.y
            self.context_popover_show(self.channels_popover, x, y)
            return True

#        if event is None or event.button == 3:
#            menu = Gtk.Menu()
#
#            item = Gtk.ImageMenuItem(_('Update podcast'))
#            item.set_image(Gtk.Image.new_from_icon_name('view-refresh', Gtk.IconSize.MENU))
#            item.set_action_name('win.updateChannel')
#            menu.append(item)
#
#            menu.append(Gtk.SeparatorMenuItem())
#
#            item = Gtk.MenuItem(_('Open download folder'))
#            item.connect('activate', self.on_open_download_folder)
#            menu.append(item)
#
#            menu.append(Gtk.SeparatorMenuItem())
#
#            item = Gtk.MenuItem(_('Mark episodes as old'))
#            item.connect('activate', self.on_mark_episodes_as_old)
#            menu.append(item)
#
#            item = Gtk.CheckMenuItem(_('Archive'))
#            item.set_active(self.active_channel.auto_archive_episodes)
#            item.connect('activate', self.on_channel_toggle_lock_activate)
#            menu.append(item)
#
#            item = Gtk.ImageMenuItem(_('Delete podcast'))
#            item.set_image(Gtk.Image.new_from_icon_name('edit-delete', Gtk.IconSize.MENU))
#            item.connect('activate', self.on_itemRemoveChannel_activate)
#            menu.append(item)
#
#            result = gpodder.user_extensions.on_channel_context_menu(self.active_channel)
#            if result:
#                menu.append(Gtk.SeparatorMenuItem())
#                for label, callback in result:
#                    item = Gtk.MenuItem(label)
#                    item.connect('activate', lambda item, callback: callback(self.active_channel), callback)
#                    menu.append(item)
#
#            menu.append(Gtk.SeparatorMenuItem())
#
#            item = Gtk.ImageMenuItem(_('Podcast settings'))
#            item.set_image(Gtk.Image.new_from_icon_name('document-properties', Gtk.IconSize.MENU))
#            item.set_action_name('win.editChannel')
#            menu.append(item)
#
#            menu.attach_to_widget(treeview)
#            menu.show_all()
#            # Disable tooltips while we are showing the menu, so
#            # the tooltip will not appear over the menu
#            self.treeview_allow_tooltips(self.treeChannels, False)
#            menu.connect('deactivate', lambda menushell: self.treeview_allow_tooltips(self.treeChannels, True))
#
#            if event is None:
#                func = TreeViewHelper.make_popup_position_func(treeview)
#                menu.popup(None, None, func, None, 3, Gtk.get_current_event_time())
#            else:
#                menu.popup(None, None, None, None, event.button, event.time)
#
#            return True
#
    def cover_download_finished(self, channel, pixbuf):
        """
        The Cover Downloader calls this when it has finished
        downloading (or registering, if already downloaded)
        a new channel cover, which is ready for displaying.
        """
        util.idle_add(self.podcast_list_model.add_cover_by_channel,
                channel, pixbuf)

    @staticmethod
    def build_filename(filename, extension):
        filename, extension = util.sanitize_filename_ext(
            filename,
            extension,
            PodcastEpisode.MAX_FILENAME_LENGTH,
            PodcastEpisode.MAX_FILENAME_WITH_EXT_LENGTH)
        if not filename.endswith(extension):
            filename += extension
        return filename

    def on_save_episodes_activate(self, action, *args):
        episodes = self.get_selected_episodes()
        util.idle_add(self.save_episodes_as_file, episodes)

    def save_episodes_as_file(self, episodes):
        def do_save_episode(copy_from, copy_to):
            if os.path.exists(copy_to):
                logger.warning(copy_from)
                logger.warning(copy_to)
                title = _('File already exists')
                d = {'filename': os.path.basename(copy_to)}
                message = _('A file named "%(filename)s" already exists. Do you want to replace it?') % d
                if not self.show_confirmation(message, title):
                    return
            try:
                shutil.copyfile(copy_from, copy_to)
            except (OSError, IOError) as e:
                logger.warning('Error copying from %s to %s: %r', copy_from, copy_to, e, exc_info=True)
                folder, filename = os.path.split(copy_to)
                # Remove characters not supported by VFAT (#282)
                new_filename = re.sub(r"[\"*/:<>?\\|]", "_", filename)
                destination = os.path.join(folder, new_filename)
                if (copy_to != destination):
                    shutil.copyfile(copy_from, destination)
                else:
                    raise

        PRIVATE_FOLDER_ATTRIBUTE = '_save_episodes_as_file_folder'
        folder = getattr(self, PRIVATE_FOLDER_ATTRIBUTE, None)
        allRemainingDefault = False
        remaining = len(episodes)
        dialog = gPodderExportToLocalFolder(self.main_window,
                                            _config=self.config)
        for episode in episodes:
            remaining -= 1
            if episode.was_downloaded(and_exists=True):
                copy_from = episode.local_filename(create=False)
                assert copy_from is not None

                base, extension = os.path.splitext(copy_from)
                filename = self.build_filename(episode.sync_filename(), extension)

                try:
                    if allRemainingDefault:
                        do_save_episode(copy_from, os.path.join(folder, filename))
                    else:
                        (notCancelled, folder, dest_path, allRemainingDefault) = dialog.save_as(folder, filename, remaining)
                        if notCancelled:
                            do_save_episode(copy_from, dest_path)
                        else:
                            break
                except (OSError, IOError) as e:
                    if remaining:
                        msg = _('Error saving to local folder: %(error)r.\n'
                                'Would you like to continue?') % dict(error=e)
                        if not self.show_confirmation(msg, _('Error saving to local folder')):
                            logger.warning("Save to Local Folder cancelled following error")
                            break
                    else:
                        self.notification(_('Error saving to local folder: %(error)r') % dict(error=e),
                                          _('Error saving to local folder'), important=True)

        setattr(self, PRIVATE_FOLDER_ATTRIBUTE, folder)

    def on_bluetooth_episodes_activate(self, action, *args):
        episodes = self.get_selected_episodes()
        util.idle_add(self.copy_episodes_bluetooth, episodes)

    def copy_episodes_bluetooth(self, episodes):
        episodes_to_copy = [e for e in episodes if e.was_downloaded(and_exists=True)]

        def convert_and_send_thread(episode):
            for episode in episodes:
                filename = episode.local_filename(create=False)
                assert filename is not None
                (base, ext) = os.path.splitext(filename)
                destfile = self.build_filename(episode.sync_filename(), ext)
                destfile = os.path.join(tempfile.gettempdir(), destfile)

                try:
                    shutil.copyfile(filename, destfile)
                    util.bluetooth_send_file(destfile)
                except:
                    logger.error('Cannot copy "%s" to "%s".', filename, destfile)
                    self.notification(_('Error converting file.'), _('Bluetooth file transfer'), important=True)

                util.delete_file(destfile)

        util.run_in_background(lambda: convert_and_send_thread(episodes_to_copy))

    def _add_sub_menu(self, menu, label):
        root_item = Gtk.MenuItem(label)
        menu.append(root_item)
        sub_menu = Gtk.Menu()
        root_item.set_submenu(sub_menu)
        return sub_menu

    def _submenu_item_activate_hack(self, item, callback, *args):
        # See http://stackoverflow.com/questions/5221326/submenu-item-does-not-call-function-with-working-solution
        # Note that we can't just call the callback on button-press-event, as
        # it might be blocking (see http://gpodder.org/bug/1778), so we run
        # this in the GUI thread at a later point in time (util.idle_add).
        # Also, we also have to connect to the activate signal, as this is the
        # only signal that is fired when keyboard navigation is used.

        # It can happen that both (button-release-event and activate) signals
        # are fired, and we must avoid calling the callback twice. We do this
        # using a semaphore and only acquiring (but never releasing) it, making
        # sure that the util.idle_add() call below is only ever called once.
        only_once = threading.Semaphore(1)

        def handle_event(item, event=None):
            if only_once.acquire(False):
                util.idle_add(callback, *args)

        item.connect('button-press-event', handle_event)
        item.connect('activate', handle_event)

    def treeview_available_show_context_menu(self, treeview, event=None):
        model, paths = self.treeview_handle_context_menu_click(treeview, event)
        if not paths:
            return not treeview.is_rubber_banding_active()

        if event is None or event.button == 3:
            episodes = self.get_selected_episodes()
            any_locked = any(e.archive for e in episodes)
            any_new = any(e.is_new and e.state != gpodder.STATE_DELETED for e in episodes)
            downloaded = all(e.was_downloaded(and_exists=True) for e in episodes)
            downloading = any(e.downloading for e in episodes)
            (open_instead_of_play, can_play, can_download, can_pause, can_cancel, can_delete, can_lock) = self.play_or_download()
            menu = self.application.builder.get_object('episodes-context')

            # Play
            menu.remove(0)
            if downloaded:
                menu.insert(0, _('Play'), 'win.play')
            else:
                if downloading:
                    menu.insert(0, _('Preview'), 'win.play')
                else:
                    menu.insert(0, _('Stream'), 'win.play')

            # New
            self.gPodder.lookup_action('episodeNew').change_state(GLib.Variant.new_boolean(any_new))

            # Extensions section
            entries = [(label, None if func is None else lambda a, b: func(episodes))
                for label, func in list(gpodder.user_extensions.on_episodes_context_menu(episodes) or [])]
            self.episode_context_menu_helper.replace_entries(entries)

            if downloaded and not self.have_episode_menu_file_items:
                # Send to submenu
                menu.insert_submenu(3, _('Send to'),
                    self.application.builder.get_object('episodes-context-send'))
                # Archive checkbox
                item = Gio.MenuItem.new(_('Archive'), 'win.episodeLock')
                action = self.gPodder.lookup_action('episodeLock')
                action.change_state(GLib.Variant.new_boolean(any_locked))
                menu.insert_item(6, item)
                self.have_episode_menu_file_items = True
            elif not downloaded and self.have_episode_menu_file_items:
                menu.remove(3)
                menu.remove(5)
                self.have_episode_menu_file_items = False

#            if len(self.get_selected_episodes()) == 1:
#                item = Gtk.MenuItem(_('Open download folder'))
#                item.connect('activate', self.on_open_episode_download_folder)
#                menu.append(item)

            if event is None:
                func = TreeViewHelper.make_popup_position_func(treeview)
                x, y, unused = func(None)
            else:
                x, y = event.x, event.y
            self.context_popover_show(self.episodes_popover, x, y)
            return True

    def set_episode_actions(self, open_instead_of_play=False, can_play=False, can_download=False, can_pause=False, can_cancel=False,
                            can_delete=False, can_lock=False, is_episode_selected=False):
        episodes = self.get_selected_episodes() if is_episode_selected else []

        # play icon and label
#        if open_instead_of_play or not is_episode_selected:
#            self.toolPlay.set_icon_name('document-open')
#            self.toolPlay.set_label(_('Open'))
#        else:
#            self.toolPlay.set_icon_name('media-playback-start')
#
#            downloaded = all(e.was_downloaded(and_exists=True) for e in episodes)
#            downloading = any(e.downloading for e in episodes)
#
#            if downloaded:
#                self.toolPlay.set_label(_('Play'))
#            elif downloading:
#                self.toolPlay.set_label(_('Preview'))
#            else:
#                self.toolPlay.set_label(_('Stream'))

        # toolbar
#        self.toolPlay.set_sensitive(can_play)
#        self.toolDownload.set_sensitive(can_download)
#        self.toolPause.set_sensitive(can_pause)
#        self.toolCancel.set_sensitive(can_cancel)

        # Episodes menu
        self.play_action.set_enabled(can_play and not open_instead_of_play)
        self.open_action.set_enabled(can_play and open_instead_of_play)
        self.download_action.set_enabled(can_download)
        self.pause_action.set_enabled(can_pause)
        self.episodes_cancel_action.set_enabled(can_cancel)
        self.delete_action.set_enabled(can_delete)
#        self.toggle_episode_new_action.set_enabled(is_episode_selected)
#        self.toggle_episode_lock_action.set_enabled(can_lock)
        self.episode_lock_action.set_enabled(can_play)
        self.open_episode_download_folder_action.set_enabled(len(episodes) == 1)

    def set_title(self, new_title):
        self.default_title = new_title
        self.gPodder.set_title(new_title)

    def update_episode_list_icons(self, urls=None, selected=False, all=False):
        """
        Updates the status icons in the episode list.

        If urls is given, it should be a list of URLs
        of episodes that should be updated.

        If urls is None, set ONE OF selected, all to
        True (the former updates just the selected
        episodes and the latter updates all episodes).
        """
        descriptions = self.config.episode_list_descriptions

        if urls is not None:
            # We have a list of URLs to walk through
            self.episode_list_model.update_by_urls(urls, descriptions)
        elif selected and not all:
            # We should update all selected episodes
            selection = self.treeAvailable.get_selection()
            model, paths = selection.get_selected_rows()
            for path in reversed(paths):
                iter = model.get_iter(path)
                self.episode_list_model.update_by_filter_iter(iter, descriptions)
        elif all and not selected:
            # We update all (even the filter-hidden) episodes
            self.episode_list_model.update_all(descriptions)
        else:
            # Wrong/invalid call - have to specify at least one parameter
            raise ValueError('Invalid call to update_episode_list_icons')

    def episode_list_status_changed(self, episodes):
        self.update_episode_list_icons(set(e.url for e in episodes))
        self.update_podcast_list_model(set(e.channel.url for e in episodes))
        self.db.commit()

    def playback_episodes_for_real(self, episodes, mark_as_played=True):
        groups = collections.defaultdict(list)
        for episode in episodes:
            episode._download_error = None

            if episode.download_task is not None and episode.download_task.status == episode.download_task.FAILED:
                if not episode.can_stream(self.config):
                    # Do not cancel failed tasks that can not be streamed
                    continue
                # Cancel failed task and remove from progress list
                episode.download_task.cancel()
                self.cleanup_downloads()

            player = episode.get_player(self.config)

            try:
                allow_partial = (player != 'default')
                filename = episode.get_playback_url(self.config, allow_partial)
            except Exception as e:
                episode._download_error = str(e)
                continue

            if mark_as_played:
                # Mark episode as played in the database
                episode.playback_mark()
                self.mygpo_client.on_playback([episode])

            # Determine the playback resume position - if the file
            # was played 100%, we simply start from the beginning
            resume_position = episode.current_position
            if resume_position == episode.total_time:
                resume_position = 0

            # If Panucci is configured, use D-Bus to call it
            if player == 'panucci':
                try:
                    PANUCCI_NAME = 'org.panucci.panucciInterface'
                    PANUCCI_PATH = '/panucciInterface'
                    PANUCCI_INTF = 'org.panucci.panucciInterface'
                    o = gpodder.dbus_session_bus.get_object(PANUCCI_NAME, PANUCCI_PATH)
                    i = dbus.Interface(o, PANUCCI_INTF)

                    def on_reply(*args):
                        pass

                    def error_handler(filename, err):
                        logger.error('Exception in D-Bus call: %s', str(err))

                        # Fallback: use the command line client
                        for command in util.format_desktop_command('panucci',
                                [filename]):
                            logger.info('Executing: %s', repr(command))
                            util.Popen(command, close_fds=True)

                    def on_error(err):
                        return error_handler(filename, err)

                    # This method only exists in Panucci > 0.9 ('new Panucci')
                    i.playback_from(filename, resume_position,
                            reply_handler=on_reply, error_handler=on_error)

                    continue  # This file was handled by the D-Bus call
                except Exception as e:
                    logger.error('Calling Panucci using D-Bus', exc_info=True)

            groups[player].append(filename)

        # Open episodes with system default player
        if 'default' in groups:
            for filename in groups['default']:
                logger.debug('Opening with system default: %s', filename)
                util.gui_open(filename, gui=self)
            del groups['default']

        # For each type now, go and create play commands
        for group in groups:
            for command in util.format_desktop_command(group, groups[group], resume_position):
                logger.debug('Executing: %s', repr(command))
                util.Popen(command, close_fds=True)

        # Persist episode status changes to the database
        self.db.commit()

        # Flush updated episode status
        if self.mygpo_client.can_access_webservice():
            self.mygpo_client.flush()

    def playback_episodes(self, episodes, mark_as_played=True):
        # We need to create a list, because we run through it more than once
        episodes = list(Model.sort_episodes_by_pubdate(e for e in episodes if e.can_play(self.config)))

        try:
            self.playback_episodes_for_real(episodes, mark_as_played)
        except Exception as e:
            logger.error('Error in playback!', exc_info=True)
            self.show_message(_('Please check your media player settings in the preferences dialog.'),
                    _('Error opening player'))

        self.episode_list_status_changed(episodes)

    def play_or_download(self, current_page=None):
        # if current_page is None:
        #     current_page = self.wNotebook.get_current_page()
        # if current_page == 0:
        if True:
            (open_instead_of_play, can_play, can_download, can_pause, can_cancel, can_delete, can_lock) = (False,) * 7

            selection = self.treeAvailable.get_selection()
            if selection.count_selected_rows() > 0:
                (model, paths) = selection.get_selected_rows()

                for path in paths:
                    try:
                        episode = model.get_value(model.get_iter(path), EpisodeListModel.C_EPISODE)
                        if episode is None:
                            logger.info('Invalid episode at path %s', str(path))
                            continue
                    except TypeError as te:
                        logger.error('Invalid episode at path %s', str(path))
                        continue

                    # These values should only ever be set, never unset them once set.
                    # Actions filter episodes using these methods.
                    open_instead_of_play = open_instead_of_play or episode.file_type() not in ('audio', 'video')
                    can_play = can_play or episode.can_play(self.config)
                    can_download = can_download or episode.can_download()
                    can_pause = can_pause or episode.can_pause()
                    can_cancel = can_cancel or episode.can_cancel()
                    can_delete = can_delete or episode.can_delete()
                    can_lock = can_lock or episode.can_lock()

            self.set_episode_actions(open_instead_of_play, can_play, can_download, can_pause, can_cancel, can_delete, can_lock,
                                    selection.count_selected_rows() > 0)

            return (open_instead_of_play, can_play, can_download, can_pause, can_cancel, can_delete, can_lock)
        else:
            (can_queue, can_pause, can_cancel, can_remove) = (False,) * 4

            selection = self.treeDownloads.get_selection()
            if selection.count_selected_rows() > 0:
                (model, paths) = selection.get_selected_rows()

                for path in paths:
                    try:
                        task = model.get_value(model.get_iter(path), 0)
                        if task is None:
                            logger.info('Invalid task at path %s', str(path))
                            continue
                    except TypeError as te:
                        logger.error('Invalid task at path %s', str(path))
                        continue

                    # These values should only ever be set, never unset them once set.
                    # Actions filter tasks using these methods.
                    can_queue = can_queue or task.can_queue()
                    can_pause = can_pause or task.can_pause()
                    can_cancel = can_cancel or task.can_cancel()
                    can_remove = can_remove or task.can_remove()

            self.set_episode_actions(False, False, can_queue, can_pause, can_cancel, can_remove, False, False)

            return (False, False, can_queue, can_pause, can_cancel, can_remove, False)

    def on_cbMaxDownloads_toggled(self, widget, *args):
        self.spinMaxDownloads.set_sensitive(self.cbMaxDownloads.get_active())

    def on_cbLimitDownloads_toggled(self, widget, *args):
        self.spinLimitDownloads.set_sensitive(self.cbLimitDownloads.get_active())

    def episode_new_status_changed(self, urls):
        self.update_podcast_list_model()
        self.update_episode_list_icons(urls)

    def refresh_episode_dates(self):
        t = time.localtime()
        current_day = t[:3]
        if self.last_episode_date_refresh is not None and self.last_episode_date_refresh != current_day:
            # update all episodes in current view
            for row in self.episode_list_model:
                row[EpisodeListModel.C_PUBLISHED_TEXT] = row[EpisodeListModel.C_EPISODE].cute_pubdate()

        self.last_episode_date_refresh = current_day

        remaining_seconds = 86400 - 3600 * t.tm_hour - 60 * t.tm_min - t.tm_sec
        if remaining_seconds > 3600:
            # timeout an hour early in the event daylight savings changes the clock forward
            remaining_seconds = remaining_seconds - 3600
        GObject.timeout_add(remaining_seconds * 1000, self.refresh_episode_dates)

    def update_podcast_list_model(self, urls=None, selected=False, select_url=None,
            sections_changed=False):
        """Update the podcast list treeview model

        If urls is given, it should list the URLs of each
        podcast that has to be updated in the list.

        If selected is True, only update the model contents
        for the currently-selected podcast - nothing more.

        The caller can optionally specify "select_url",
        which is the URL of the podcast that is to be
        selected in the list after the update is complete.
        This only works if the podcast list has to be
        reloaded; i.e. something has been added or removed
        since the last update of the podcast list).
        """
        selection = self.treeChannels.get_selection()
        model, iter = selection.get_selected()

        def is_section(r):
            return r[PodcastListModel.C_URL] == '-'

        def is_separator(r):
            return r[PodcastListModel.C_SEPARATOR]

        sections_active = any(is_section(x) for x in self.podcast_list_model)

        if self.config.podcast_list_view_all:
            # Update "all episodes" view in any case (if enabled)
            self.podcast_list_model.update_first_row()
            # List model length minus 1, because of "All"
            list_model_length = len(self.podcast_list_model) - 1
        else:
            list_model_length = len(self.podcast_list_model)

        force_update = (sections_active != self.config.podcast_list_sections or
                sections_changed)

        # Filter items in the list model that are not podcasts, so we get the
        # correct podcast list count (ignore section headers and separators)

        def is_not_podcast(r):
            return is_section(r) or is_separator(r)

        list_model_length -= len(list(filter(is_not_podcast, self.podcast_list_model)))

        if selected and not force_update:
            # very cheap! only update selected channel
            if iter is not None:
                # If we have selected the "all episodes" view, we have
                # to update all channels for selected episodes:
                if self.config.podcast_list_view_all and \
                        self.podcast_list_model.iter_is_first_row(iter):
                    urls = self.get_podcast_urls_from_selected_episodes()
                    self.podcast_list_model.update_by_urls(urls)
                else:
                    # Otherwise just update the selected row (a podcast)
                    self.podcast_list_model.update_by_filter_iter(iter)

                if self.config.podcast_list_sections:
                    self.podcast_list_model.update_sections()
        elif list_model_length == len(self.channels) and not force_update:
            # we can keep the model, but have to update some
            if urls is None:
                # still cheaper than reloading the whole list
                self.podcast_list_model.update_all()
            else:
                # ok, we got a bunch of urls to update
                self.podcast_list_model.update_by_urls(urls)
                if self.config.podcast_list_sections:
                    self.podcast_list_model.update_sections()
        else:
            if model and iter and select_url is None:
                # Get the URL of the currently-selected podcast
                select_url = model.get_value(iter, PodcastListModel.C_URL)

            # Update the podcast list model with new channels
            self.podcast_list_model.set_channels(self.db, self.config, self.channels)

            try:
                selected_iter = model.get_iter_first()
                # Find the previously-selected URL in the new
                # model if we have an URL (else select first)
                if select_url is not None:
                    pos = model.get_iter_first()
                    while pos is not None:
                        url = model.get_value(pos, PodcastListModel.C_URL)
                        if url == select_url:
                            selected_iter = pos
                            break
                        pos = model.iter_next(pos)

                if selected_iter is not None:
                    selection.select_iter(selected_iter)
                self.on_treeChannels_cursor_changed(self.treeChannels)
            except:
                logger.error('Cannot select podcast in list', exc_info=True)

    def on_episode_list_filter_changed(self, has_episodes):
        self.play_or_download()

    def on_episode_list_back_clicked(self, widget):
        self.leaflet.navigate(Handy.NavigationDirection.BACK)
        self.treeChannels.grab_focus()
        return True

    def navigate_to_shownotes(self):
        self.deck.navigate(Handy.NavigationDirection.FORWARD)
        self.deck_back.grab_focus()

    def navigate_from_shownotes(self):
        self.deck.navigate(Handy.NavigationDirection.BACK)
        self.shownotes_object.hide_pane()
        self.treeAvailable.grab_focus()

    def on_deck_back_clicked(self, widget):
        self.navigate_from_shownotes()
        return True

    def update_episode_list_model(self):
        if self.channels and self.active_channel is not None:
            if self.treeAvailable.get_realized():
                self.treeAvailable.get_selection().unselect_all()
                self.treeAvailable.scroll_to_point(0, 0)

            descriptions = self.config.episode_list_descriptions
            with self.treeAvailable.get_selection().handler_block(self.episode_selection_handler_id):
                # have to block the on_episode_list_selection_changed handler because
                # when selecting any channel from All Episodes, on_episode_list_selection_changed
                # is called once per episode (4k time in my case), causing episode shownotes
                # to be updated as many time, resulting in UI freeze for 10 seconds.
                self.episode_list_model.replace_from_channel(self.active_channel, descriptions)
        else:
            self.episode_list_model.clear()

    @dbus.service.method(gpodder.dbus_interface)
    def offer_new_episodes(self, channels=None):
        new_episodes = self.get_new_episodes(channels)
        if new_episodes:
            self.new_episodes_show(new_episodes)
            return True
        return False

    def add_podcast_list(self, podcasts, auth_tokens=None):
        """Subscribe to a list of podcast given (title, url) pairs

        If auth_tokens is given, it should be a dictionary
        mapping URLs to (username, password) tuples."""

        if auth_tokens is None:
            auth_tokens = {}

        existing_urls = set(podcast.url for podcast in self.channels)

        # For a given URL, the desired title (or None)
        title_for_url = {}

        # Sort and split the URL list into five buckets
        queued, failed, existing, worked, authreq = [], [], [], [], []
        for input_title, input_url in podcasts:
            url = util.normalize_feed_url(input_url)

            # Check if it's a YouTube channel, user, or playlist and resolves it to its feed if that's the case
            url = youtube.parse_youtube_url(url)

            if url is None:
                # Fail this one because the URL is not valid
                failed.append(input_url)
            elif url in existing_urls:
                # A podcast already exists in the list for this URL
                existing.append(url)
                # XXX: Should we try to update the title of the existing
                # subscription from input_title here if it is different?
            else:
                # This URL has survived the first round - queue for add
                title_for_url[url] = input_title
                queued.append(url)
                if url != input_url and input_url in auth_tokens:
                    auth_tokens[url] = auth_tokens[input_url]

        error_messages = {}
        redirections = {}

        progress = ProgressIndicator(_('Adding podcasts'),
                _('Please wait while episode information is downloaded.'),
                parent=self.get_dialog_parent())

        def on_after_update():
            progress.on_finished()
            # Report already-existing subscriptions to the user
            if existing:
                title = _('Existing subscriptions skipped')
                message = _('You are already subscribed to these podcasts:') \
                    + '\n\n' + '\n'.join(html.escape(url) for url in existing)
                self.show_message(message, title, widget=self.treeChannels)

            # Report subscriptions that require authentication
            retry_podcasts = {}
            if authreq:
                for url in authreq:
                    title = _('Podcast requires authentication')
                    message = _('Please login to %s:') % (html.escape(url),)
                    success, auth_tokens = self.show_login_dialog(title, message)
                    if success:
                        retry_podcasts[url] = auth_tokens
                    else:
                        # Stop asking the user for more login data
                        retry_podcasts = {}
                        for url in authreq:
                            error_messages[url] = _('Authentication failed')
                            failed.append(url)
                        break

            # Report website redirections
            for url in redirections:
                title = _('Website redirection detected')
                message = _('The URL %(url)s redirects to %(target)s.') \
                    + '\n\n' + _('Do you want to visit the website now?')
                message = message % {'url': url, 'target': redirections[url]}
                if self.show_confirmation(message, title):
                    util.open_website(url)
                else:
                    break

            # Report failed subscriptions to the user
            if failed:
                title = _('Could not add some podcasts')
                message = _('Some podcasts could not be added to your list:')
                details = '\n\n'.join('<b>{}</b>:\n{}'.format(html.escape(url),
                    html.escape(error_messages.get(url, _('Unknown')))) for url in failed)
                self.show_message_details(title, message, details)

            # Upload subscription changes to gpodder.net
            self.mygpo_client.on_subscribe(worked)

            # Fix URLs if mygpo has rewritten them
            self.rewrite_urls_mygpo()

            # If only one podcast was added, select it after the update
            if len(worked) == 1:
                url = worked[0]
            else:
                url = None

            # Update the list of subscribed podcasts
            self.update_podcast_list_model(select_url=url)

            # If we have authentication data to retry, do so here
            if retry_podcasts:
                podcasts = [(title_for_url.get(url), url)
                        for url in list(retry_podcasts.keys())]
                self.add_podcast_list(podcasts, retry_podcasts)
                # This will NOT show new episodes for podcasts that have
                # been added ("worked"), but it will prevent problems with
                # multiple dialogs being open at the same time ;)
                return

            # Offer to download new episodes
            episodes = []
            for podcast in self.channels:
                if podcast.url in worked:
                    episodes.extend(podcast.get_all_episodes())

            if episodes:
                episodes = list(Model.sort_episodes_by_pubdate(episodes,
                        reverse=True))
                self.new_episodes_show(episodes,
                        selected=[e.check_is_new() for e in episodes])

        @util.run_in_background
        def thread_proc():
            # After the initial sorting and splitting, try all queued podcasts
            length = len(queued)
            for index, url in enumerate(queued):
                title = title_for_url.get(url)
                progress.on_progress(float(index) / float(length))
                progress.on_message(title or url)
                try:
                    # The URL is valid and does not exist already - subscribe!
                    channel = self.model.load_podcast(url=url, create=True,
                            authentication_tokens=auth_tokens.get(url, None),
                            max_episodes=self.config.max_episodes_per_feed)

                    try:
                        username, password = util.username_password_from_url(url)
                    except ValueError as ve:
                        username, password = (None, None)

                    if title is not None:
                        # Prefer title from subscription source (bug 1711)
                        channel.title = title

                    if username is not None and channel.auth_username is None and \
                            password is not None and channel.auth_password is None:
                        channel.auth_username = username
                        channel.auth_password = password

                    channel.save()

                    self._update_cover(channel)
                except feedcore.AuthenticationRequired as e:
                    # use e.url because there might have been a redirection (#571)
                    if e.url in auth_tokens:
                        # Fail for wrong authentication data
                        error_messages[e.url] = _('Authentication failed')
                        failed.append(e.url)
                    else:
                        # Queue for login dialog later
                        authreq.append(e.url)
                    continue
                except feedcore.WifiLogin as error:
                    redirections[url] = error.data
                    failed.append(url)
                    error_messages[url] = _('Redirection detected')
                    continue
                except Exception as e:
                    logger.error('Subscription error: %s', e, exc_info=True)
                    error_messages[url] = str(e)
                    failed.append(url)
                    continue

                assert channel is not None
                worked.append(channel.url)

            util.idle_add(on_after_update)

    def find_episode(self, podcast_url, episode_url):
        """Find an episode given its podcast and episode URL

        The function will return a PodcastEpisode object if
        the episode is found, or None if it's not found.
        """
        for podcast in self.channels:
            if podcast_url == podcast.url:
                for episode in podcast.get_all_episodes():
                    if episode_url == episode.url:
                        return episode

        return None

    def process_received_episode_actions(self):
        """Process/merge episode actions from gpodder.net

        This function will merge all changes received from
        the server to the local database and update the
        status of the affected episodes as necessary.
        """
        indicator = ProgressIndicator(_('Merging episode actions'),
                _('Episode actions from gpodder.net are merged.'),
                False, self.get_dialog_parent())

        Gtk.main_iteration()

        self.mygpo_client.process_episode_actions(self.find_episode)

        indicator.on_finished()
        self.db.commit()

    def _update_cover(self, channel):
        if channel is not None:
            self.cover_downloader.request_cover(channel)

    def show_update_feeds_buttons(self):
        # Make sure that the buttons for updating feeds
        # appear - this should happen after a feed update
        self.update_action.set_enabled(True)
        self.update_channel_action.set_enabled(True)

    def on_btnCancelFeedUpdate_clicked(self, widget):
        if not self.feed_cache_update_cancelled:
            self.pbFeedUpdate.set_text(_('Cancelling...'))
            self.feed_cache_update_cancelled = True
            self.btnCancelFeedUpdate.set_sensitive(False)
        else:
            self.show_update_feeds_buttons()

    def update_feed_cache(self, channels=None,
                          show_new_episodes_dialog=True):
        if self.config.check_connection and not util.connection_available():
            self.show_message(_('Please connect to a network, then try again.'),
                    _('No network connection'), important=True)
            return

        # Fix URLs if mygpo has rewritten them
        self.rewrite_urls_mygpo()

        if channels is None:
            # Only update podcasts for which updates are enabled
            channels = [c for c in self.channels if not c.pause_subscription]

        self.update_action.set_enabled(False)
        self.update_channel_action.set_enabled(False)

        self.feed_cache_update_cancelled = False
        self.btnCancelFeedUpdate.show()
        self.btnCancelFeedUpdate.set_sensitive(True)
        self.btnCancelFeedUpdate.set_image(Gtk.Image.new_from_icon_name('process-stop', Gtk.IconSize.BUTTON))
        self.update_revealer.set_reveal_child(True)

        count = len(channels)
        text = N_('Updating %(count)d feed...', 'Updating %(count)d feeds...',
                  count) % {'count': count}

        self.pbFeedUpdate.set_text(text)
        self.pbFeedUpdate.set_fraction(0)

        @util.run_in_background
        def update_feed_cache_proc():
            updated_channels = []
            nr_update_errors = 0
            for updated, channel in enumerate(channels):
                if self.feed_cache_update_cancelled:
                    break

                def indicate_updating_podcast(channel):
                    d = {'podcast': channel.title, 'position': updated + 1, 'total': count}
                    progression = _('Updating %(podcast)s (%(position)d/%(total)d)') % d
                    logger.info(progression)
                    self.pbFeedUpdate.set_text(progression)

                try:
                    channel._update_error = None
                    util.idle_add(indicate_updating_podcast, channel)
                    channel.update(max_episodes=self.config.max_episodes_per_feed)
                    self._update_cover(channel)
                except Exception as e:
                    message = str(e)
                    if message:
                        channel._update_error = message
                    else:
                        channel._update_error = '?'
                    nr_update_errors += 1
                    logger.error('Error: %s', message, exc_info=(e.__class__ not in [
                        gpodder.feedcore.BadRequest,
                        gpodder.feedcore.AuthenticationRequired,
                        gpodder.feedcore.Unsubscribe,
                        gpodder.feedcore.NotFound,
                        gpodder.feedcore.InternalServerError,
                        gpodder.feedcore.UnknownStatusCode,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.RetryError,
                        urllib3.exceptions.MaxRetryError,
                        urllib3.exceptions.ReadTimeoutError,
                    ]))

                updated_channels.append(channel)

                def update_progress(channel):
                    self.update_podcast_list_model([channel.url])

                    # If the currently-viewed podcast is updated, reload episodes
                    if self.active_channel is not None and \
                            self.active_channel == channel:
                        logger.debug('Updated channel is active, updating UI')
                        self.update_episode_list_model()

                    self.pbFeedUpdate.set_fraction(float(updated + 1) / float(count))

                util.idle_add(update_progress, channel)

            if nr_update_errors > 0:
                self.notification(
                    N_('%(count)d channel failed to update',
                       '%(count)d channels failed to update',
                       nr_update_errors) % {'count': nr_update_errors},
                    _('Error while updating feeds'), widget=self.treeChannels)

            def update_feed_cache_finish_callback():
                # Process received episode actions for all updated URLs
                self.process_received_episode_actions()

                # If we are currently viewing "All episodes" or a section, update its episode list now
                if self.active_channel is not None and \
                        isinstance(self.active_channel, PodcastChannelProxy):
                    self.update_episode_list_model()

                if self.feed_cache_update_cancelled:
                    # The user decided to abort the feed update
                    self.show_update_feeds_buttons()

                # Only search for new episodes in podcasts that have been
                # updated, not in other podcasts (for single-feed updates)
                episodes = self.get_new_episodes([c for c in updated_channels])

                if self.config.downloads.chronological_order:
                    # download older episodes first
                    episodes = list(Model.sort_episodes_by_pubdate(episodes))

                # Remove episodes without downloadable content
                downloadable_episodes = [e for e in episodes if e.url]

                if not downloadable_episodes:
                    # Nothing new here - but inform the user
                    self.pbFeedUpdate.set_fraction(1.0)
                    self.pbFeedUpdate.set_text(
                        _('No new episodes with downloadable content') if episodes else _('No new episodes'))
                    self.feed_cache_update_cancelled = True
                    self.btnCancelFeedUpdate.show()
                    self.btnCancelFeedUpdate.set_sensitive(True)
                    self.update_action.set_enabled(True)
                    self.btnCancelFeedUpdate.set_image(Gtk.Image.new_from_icon_name('edit-clear', Gtk.IconSize.BUTTON))
                else:
                    episodes = downloadable_episodes

                    count = len(episodes)
                    # New episodes are available
                    self.pbFeedUpdate.set_fraction(1.0)

                    if self.config.auto_download == 'download':
                        self.download_episode_list(episodes)
                        title = N_('Downloading %(count)d new episode.',
                                   'Downloading %(count)d new episodes.',
                                   count) % {'count': count}
                        self.show_message(title, _('New episodes available'))
                    elif self.config.auto_download == 'queue':
                        self.download_episode_list_paused(episodes)
                        title = N_(
                            '%(count)d new episode added to download list.',
                            '%(count)d new episodes added to download list.',
                            count) % {'count': count}
                        self.show_message(title, _('New episodes available'))
                    else:
                        if (show_new_episodes_dialog and
                                self.config.auto_download == 'show'):
                            self.new_episodes_show(episodes, notification=True)
                        else:  # !show_new_episodes_dialog or auto_download == 'ignore'
                            message = N_('%(count)d new episode available',
                                         '%(count)d new episodes available',
                                         count) % {'count': count}
                            self.pbFeedUpdate.set_text(message)

                    self.show_update_feeds_buttons()

                def hide_update():
                    self.update_revealer.set_reveal_child(False)
                    self.update_channel_action.set_enabled(True)
                    return False

                GLib.timeout_add(2000, hide_update)

            util.idle_add(update_feed_cache_finish_callback)

    def on_gPodder_delete_event(self, *args):
        """Called when the GUI wants to close the window
        Displays a confirmation dialog (and closes/hides gPodder)
        """

        if self.confirm_quit():
            self.close_gpodder()

        return True

    def confirm_quit(self):
        """Called when the GUI wants to close the window
        Displays a confirmation dialog
        """

        downloading = self.download_status_model.are_downloads_in_progress()

        if downloading:
            dialog = Gtk.MessageDialog(self.gPodder, Gtk.DialogFlags.MODAL, Gtk.MessageType.QUESTION, Gtk.ButtonsType.NONE)
            dialog.add_button(_('_Cancel'), Gtk.ResponseType.CANCEL)
            quit_button = dialog.add_button(_('_Quit'), Gtk.ResponseType.CLOSE)

            title = _('Quit gPodder')
            message = _('You are downloading episodes. You can resume downloads the next time you start gPodder. Do you want to quit now?')

            dialog.set_title(title)
            dialog.set_markup('<span weight="bold" size="larger">%s</span>\n\n%s' % (title, message))

            quit_button.grab_focus()
            result = dialog.run()
            dialog.destroy()

            return result == Gtk.ResponseType.CLOSE
        else:
            return True

    def close_gpodder(self):
        """ clean everything and exit properly
        """
        # Cancel any running background updates of the episode list model
        self.episode_list_model.background_update = None

        self.gPodder.hide()

        # Notify all tasks to to carry out any clean-up actions
        self.download_status_model.tell_all_tasks_to_quit()

        while Gtk.events_pending() or self.download_queue_manager.has_workers():
            Gtk.main_iteration()

        self.core.shutdown()

        self.application.remove_window(self.gPodder)

    def format_delete_message(self, message, things, max_things, max_length):
        titles = []
        for index, thing in zip(range(max_things), things):
            titles.append('• ' + (html.escape(thing.title if len(thing.title) <= max_length else thing.title[:max_length] + '…')))
        if len(things) > max_things:
            titles.append('+%(count)d more…' % {'count': len(things) - max_things})
        return '\n'.join(titles) + '\n\n' + message

    def delete_episode_list(self, episodes, confirm=True, callback=None):
        #        if self.wNotebook.get_current_page() > 0:
        #            selection = self.treeDownloads.get_selection()
        #            (model, paths) = selection.get_selected_rows()
        #            selected_tasks = [(Gtk.TreeRowReference.new(model, path),
        #                               model.get_value(model.get_iter(path),
        #                               DownloadStatusModel.C_TASK)) for path in paths]
        #            self._for_each_task_set_status(selected_tasks, status=None, force_start=False)
        #            return
        #
        if not episodes:
            return False

        episodes = [e for e in episodes if not e.archive]

        if not episodes:
            title = _('Episodes are locked')
            message = _(
                'The selected episodes are locked. Please unlock the '
                'episodes that you want to delete before trying '
                'to delete them.')
            self.notification(message, title, widget=self.treeAvailable)
            return False

        count = len(episodes)
        title = N_('Delete %(count)d episode?', 'Delete %(count)d episodes?',
                   count) % {'count': count}
        message = _('Deleting episodes removes downloaded files.')

        message = self.format_delete_message(message, episodes, 5, 60)

        if confirm and not self.show_confirmation(message, title):
            return False

        self.on_episodes_cancel_download_activate(force=True)

        progress = ProgressIndicator(_('Deleting episodes'),
                _('Please wait while episodes are deleted'),
                parent=self.get_dialog_parent())

        def finish_deletion(episode_urls, channel_urls):
            progress.on_finished()

            # Episodes have been deleted - persist the database
            self.db.commit()

            self.update_episode_list_icons(episode_urls)
            self.update_podcast_list_model(channel_urls)
            self.play_or_download()

        @util.run_in_background
        def thread_proc():
            episode_urls = set()
            channel_urls = set()

            episodes_status_update = []
            for idx, episode in enumerate(episodes):
                progress.on_progress(idx / len(episodes))
                if not episode.archive:
                    progress.on_message(episode.title)
                    episode.delete_from_disk()
                    episode_urls.add(episode.url)
                    channel_urls.add(episode.channel.url)
                    episodes_status_update.append(episode)

            # Notify the web service about the status update + upload
            if self.mygpo_client.can_access_webservice():
                self.mygpo_client.on_delete(episodes_status_update)
                self.mygpo_client.flush()

            if callback is None:
                util.idle_add(finish_deletion, episode_urls, channel_urls)
            else:
                util.idle_add(callback, episode_urls, channel_urls, progress)

        return True

    def on_itemRemoveOldEpisodes_activate(self, action, param):
        self.show_delete_episodes_window()

    def show_delete_episodes_window(self, channel=None):
        """Offer deletion of episodes

        If channel is None, offer deletion of all episodes.
        Otherwise only offer deletion of episodes in the channel.
        """
        columns = (
            ('markup_delete_episodes', None, None, _('Episode')),
        )

        msg_older_than = N_('Select older than %(count)d day', 'Select older than %(count)d days', self.config.episode_old_age)
        selection_buttons = {
                _('Select played'): lambda episode: not episode.is_new,
                _('Select finished'): lambda episode: episode.is_finished(),
                msg_older_than % {'count': self.config.episode_old_age}: lambda episode: episode.age_in_days() > self.config.episode_old_age,
        }

        instructions = _('Select the episodes you want to delete:')

        if channel is None:
            channels = self.channels
        else:
            channels = [channel]

        episodes = []
        for channel in channels:
            for episode in channel.get_episodes(gpodder.STATE_DOWNLOADED):
                # Disallow deletion of locked episodes that still exist
                if not episode.archive or not episode.file_exists():
                    episodes.append(episode)

        selected = [not e.is_new or not e.file_exists() for e in episodes]

        gPodderEpisodeSelector(
            self.main_window, title=_('Delete episodes'),
            ui_folder=os.path.join(gpodder.ui_folders[0], '..', 'adaptive'),
            instructions=instructions,
            episodes=episodes, selected=selected, columns=columns,
            ok_button=_('_Delete'), callback=self.delete_episode_list,
            selection_buttons=selection_buttons, _config=self.config,
            gPodder=self)

    def on_selected_episodes_status_changed(self):
        # The order of the updates here is important! When "All episodes" is
        # selected, the update of the podcast list model depends on the episode
        # list selection to determine which podcasts are affected. Updating
        # the episode list could remove the selection if a filter is active.
        self.update_podcast_list_model(selected=True)
        self.update_episode_list_icons(selected=True)
        self.db.commit()

        self.play_or_download()

    def mark_selected_episodes_new(self):
        for episode in self.get_selected_episodes():
            episode.mark(is_played=False)
        self.on_selected_episodes_status_changed()

    def mark_selected_episodes_old(self):
        for episode in self.get_selected_episodes():
            episode.mark(is_played=True)
        self.on_selected_episodes_status_changed()

    def on_item_toggle_played_activate(self, action, param):
        for episode in self.get_selected_episodes():
            episode.mark(is_played=episode.is_new and episode.state != gpodder.STATE_DELETED)
        self.on_selected_episodes_status_changed()

#    def on_item_toggle_lock_activate(self, unused, toggle=True, new_value=False):
#        for episode in self.get_selected_episodes():
#            # Gio.SimpleAction activate signal passes None (see #681)
#            if toggle or toggle is None:
#                episode.mark(is_locked=not episode.archive)
#            else:
#                episode.mark(is_locked=new_value)
#        self.on_selected_episodes_status_changed()

    def on_episode_lock_action(self, action, *params):
        new_state = not action.get_state().get_boolean()
        for episode in self.get_selected_episodes():
            episode.mark(is_locked=new_state)
        self.on_selected_episodes_status_changed()
        self.play_or_download()
        action.change_state(GLib.Variant.new_boolean(new_state))
        self.episodes_popover.popdown()
        return True

    def on_channel_toggle_lock_activate(self, widget, toggle=True, new_value=False):
        if self.active_channel is None:
            return

        self.active_channel.auto_archive_episodes = not self.active_channel.auto_archive_episodes
        self.active_channel.save()

        for episode in self.active_channel.get_all_episodes():
            episode.mark(is_locked=self.active_channel.auto_archive_episodes)

        self.update_podcast_list_model(selected=True)
        self.update_episode_list_icons(all=True)

    def on_itemUpdateChannel_activate(self, *params):
        if self.active_channel is None:
            title = _('No podcast selected')
            message = _('Please select a podcast in the podcasts list to update.')
            self.show_message(message, title, widget=self.treeChannels)
            return

        # Dirty hack to check for "All episodes" (see gpodder.gtkui.model)
        if getattr(self.active_channel, 'ALL_EPISODES_PROXY', False):
            self.update_feed_cache()
        else:
            self.update_feed_cache(channels=[self.active_channel])

    def on_itemUpdate_activate(self, action=None, param=None):
        # Check if we have outstanding subscribe/unsubscribe actions
        self.on_add_remove_podcasts_mygpo()

        if self.channels:
            self.update_feed_cache()
        else:
            def show_welcome_window():
                def on_show_example_podcasts(widget):
                    welcome_window.main_window.response(Gtk.ResponseType.CANCEL)
                    self.on_itemImportChannels_activate(None)

                def on_add_podcast_via_url(widget):
                    welcome_window.main_window.response(Gtk.ResponseType.CANCEL)
                    self.on_itemAddChannel_activate(None)

                def on_setup_my_gpodder(widget):
                    welcome_window.main_window.response(Gtk.ResponseType.CANCEL)
                    self.on_download_subscriptions_from_mygpo(None)

                welcome_window = gPodderWelcome(self.main_window,
                        center_on_widget=self.main_window,
                        on_show_example_podcasts=on_show_example_podcasts,
                        on_add_podcast_via_url=on_add_podcast_via_url,
                        on_setup_my_gpodder=on_setup_my_gpodder)

                welcome_window.main_window.run()
                welcome_window.main_window.destroy()

            util.idle_add(show_welcome_window)

    def download_episode_list_paused(self, episodes):
        self.download_episode_list(episodes, True)

    def download_episode_list(self, episodes, add_paused=False, force_start=False, downloader=None):
        def queue_tasks(tasks, queued_existing_task):
            for task in tasks:
                with task:
                    if add_paused:
                        task.status = task.PAUSED
                    else:
                        self.mygpo_client.on_download([task.episode])
                        self.queue_task(task, force_start)
            if tasks or queued_existing_task:
                self.set_download_list_state(gPodderSyncUI.DL_ONEOFF)
            # Flush updated episode status
            if self.mygpo_client.can_access_webservice():
                self.mygpo_client.flush()

        queued_existing_task = False
        new_tasks = []

        if self.config.downloads.chronological_order:
            # Download episodes in chronological order (older episodes first)
            episodes = list(Model.sort_episodes_by_pubdate(episodes))

        for episode in episodes:
            logger.debug('Downloading episode: %s', episode.title)
            if not episode.was_downloaded(and_exists=True):
                episode._download_error = None
                if episode.state == gpodder.STATE_DELETED:
                    episode.state = gpodder.STATE_NORMAL
                    episode.save()
                task_exists = False
                for task in self.download_tasks_seen:
                    if episode.url == task.url:
                        task_exists = True
                        task.unpause()
                        task.reuse()
                        if task.status not in (task.DOWNLOADING, task.QUEUED):
                            if downloader:
                                # replace existing task's download with forced one
                                task.downloader = downloader
                            self.queue_task(task, force_start)
                            queued_existing_task = True
                            continue

                if task_exists:
                    continue

                try:
                    task = download.DownloadTask(episode, self.config, downloader=downloader)
                except Exception as e:
                    episode._download_error = str(e)
                    d = {'episode': html.escape(episode.title), 'message': html.escape(str(e))}
                    message = _('Download error while downloading %(episode)s: %(message)s')
                    self.show_message(message % d, _('Download error'), important=True)
                    logger.error('While downloading %s', episode.title, exc_info=True)
                    continue

                # New Task, we must wait on the GTK Loop
                self.download_status_model.register_task(task)
                new_tasks.append(task)

        # Executes after tasks have been registered
        util.idle_add(queue_tasks, new_tasks, queued_existing_task)

    def cancel_task_list(self, tasks, force=False):
        if not tasks:
            return

        for task in tasks:
            task.cancel()

        self.update_episode_list_icons([task.url for task in tasks])
        self.play_or_download()

        # Update the tab title and downloads list
        self.update_downloads_list()

    def new_episodes_show(self, episodes, notification=False, selected=None):
        columns = (
            ('markup_new_episodes', None, None, _('Episode')),
        )

        instructions = _('Select the episodes you want to download:')

        if self.new_episodes_window is not None:
            self.new_episodes_window.main_window.destroy()
            self.new_episodes_window = None

        def download_episodes_callback(episodes):
            self.new_episodes_window = None
            self.download_episode_list(episodes)

        if selected is None:
            # Select all by default
            selected = [True] * len(episodes)

        self.new_episodes_window = gPodderEpisodeSelector(self.main_window,
                ui_folder=os.path.join(gpodder.ui_folders[0], '..', 'adaptive'),
                title=_('New episodes available'),
                instructions=instructions,
                episodes=episodes,
                columns=columns,
                selected=selected,
                ok_button='gpodder-download',
                callback=download_episodes_callback,
                remove_callback=lambda e: e.mark_old(),
                remove_action=_('_Mark as old'),
                remove_finished=self.episode_new_status_changed,
                _config=self.config,
                gPodder=self,
                show_notification=False)

    def on_itemDownloadAllNew_activate(self, action, param):
        if not self.offer_new_episodes():
            self.show_message(_('Please check for new episodes later.'),
                    _('No new episodes available'))

    def get_new_episodes(self, channels=None):
        return [e for c in channels or self.channels for e in
                [e for e in c.get_all_episodes() if e.check_is_new()]]

    def commit_changes_to_database(self):
        """This will be called after the sync process is finished"""
        self.db.commit()

    def on_itemShowToolbar_activate(self, action, param):
        state = action.get_state()
        self.config.show_toolbar = not state
        action.set_state(GLib.Variant.new_boolean(not state))

    def on_itemShowDescription_activate(self, action, param):
        state = action.get_state()
        self.config.episode_list_descriptions = not state
        action.set_state(GLib.Variant.new_boolean(not state))

    def on_item_view_hide_boring_podcasts_toggled(self, action, param):
        state = action.get_state()
        self.config.podcast_list_hide_boring = not state
        action.set_state(GLib.Variant.new_boolean(not state))
        self.apply_podcast_list_hide_boring()

    def on_item_view_always_show_new_episodes_toggled(self, action, param):
        state = action.get_state()
        self.config.ui.gtk.episode_list.always_show_new = not state
        action.set_state(GLib.Variant.new_boolean(not state))

    def on_item_view_ctrl_click_to_sort_episodes_toggled(self, action, param):
        state = action.get_state()
        self.config.ui.gtk.episode_list.ctrl_click_to_sort = not state
        action.set_state(GLib.Variant.new_boolean(not state))

    def on_item_view_search_always_visible_toggled(self, action, param):
        state = action.get_state()
        self.config.ui.gtk.search_always_visible = not state
        action.set_state(GLib.Variant.new_boolean(not state))
        for search in (self._search_episodes, self._search_podcasts):
            if search:
                if self.config.ui.gtk.search_always_visible:
                    search.show_search(grab_focus=False)
                else:
                    search.hide_search()

    def on_item_view_episodes_changed(self, action, param):
        self.config.episode_list_view_mode = getattr(EpisodeListModel, param.get_string()) or EpisodeListModel.VIEW_ALL
        action.set_state(param)

        self.episode_list_model.set_view_mode(self.config.episode_list_view_mode)
        self.apply_podcast_list_hide_boring()

    def relabel_view_menubutton(self, action, param):
        view2text = {
            EpisodeListModel.VIEW_ALL: 'All',
            EpisodeListModel.VIEW_UNDELETED: 'Undeleted',
            EpisodeListModel.VIEW_DOWNLOADED: 'Downloaded',
            EpisodeListModel.VIEW_UNPLAYED: 'Unplayed',
        }
        label = self.view_menubutton.get_child()
        label.set_text(view2text[self.config.episode_list_view_mode])
        self.view_popover.popdown()

    def apply_podcast_list_hide_boring(self):
        if self.config.podcast_list_hide_boring:
            self.podcast_list_model.set_view_mode(self.config.episode_list_view_mode)
        else:
            self.podcast_list_model.set_view_mode(-1)

    def on_download_subscriptions_from_mygpo(self, action=None):
        def after_login():
            title = _('Subscriptions on %(server)s') \
                    % {'server': self.config.mygpo.server}
            dir = gPodderPodcastDirectory(self.gPodder,
                                          ui_folder=os.path.join(gpodder.ui_folders[0], '..', 'adaptive'),
                                          _config=self.config,
                                          custom_title=title,
                                          add_podcast_list=self.add_podcast_list,
                                          hide_url_entry=True)

            url = self.mygpo_client.get_download_user_subscriptions_url()
            dir.download_opml_file(url)

        title = _('Login to gpodder.net')
        message = _('Please login to download your subscriptions.')

        def on_register_button_clicked():
            util.open_website('http://gpodder.net/register/')

        success, (root_url, username, password) = self.show_login_dialog(title, message,
                self.config.mygpo.server,
                self.config.mygpo.username, self.config.mygpo.password,
                register_callback=on_register_button_clicked,
                ask_server=True)
        if not success:
            return

        self.config.mygpo.server = root_url
        self.config.mygpo.username = username
        self.config.mygpo.password = password

        util.idle_add(after_login)

    def on_itemAddChannel_activate(self, action=None, param=None):
        self._add_podcast_dialog = gPodderAddPodcast(self.gPodder,
                add_podcast_list=self.add_podcast_list)

    def on_itemEditChannel_activate(self, action, param=None):
        if self.active_channel is None:
            title = _('No podcast selected')
            message = _('Please select a podcast in the podcasts list to edit.')
            self.show_message(message, title, widget=self.treeChannels)
            return

        gPodderChannel(self.main_window,
                ui_folder=os.path.join(
                    gpodder.ui_folders[0], '..', 'gtk'),
                channel=self.active_channel,
                update_podcast_list_model=self.update_podcast_list_model,
                cover_downloader=self.cover_downloader,
                sections=set(c.section for c in self.channels),
                clear_cover_cache=self.podcast_list_model.clear_cover_cache,
                _config=self.config)

    def on_itemMassUnsubscribe_activate(self, action, param):
        columns = (
            ('title_markup', None, None, _('Podcast')),
        )

        # We're abusing the Episode Selector for selecting Podcasts here,
        # but it works and looks good, so why not? -- thp
        gPodderEpisodeSelector(self.main_window,
                ui_folder=os.path.join(gpodder.ui_folders[0], '..', 'adaptive'),
                title=_('Delete podcasts'),
                instructions=_('Select the podcast you want to delete.'),
                episodes=self.channels,
                columns=columns,
                size_attribute=None,
                ok_button=_('_Delete'),
                callback=self.remove_podcast_list,
                _config=self.config)

    def remove_podcast_list(self, channels, confirm=True):
        if not channels:
            return

        if len(channels) == 1:
            title = _('Deleting podcast')
            info = _('Please wait while the podcast is deleted')
            message = _('This podcast and all its episodes will be PERMANENTLY DELETED.\nAre you sure you want to continue?')
        else:
            title = _('Deleting podcasts')
            info = _('Please wait while the podcasts are deleted')
            message = _('These podcasts and all their episodes will be PERMANENTLY DELETED.\nAre you sure you want to continue?')

        message = self.format_delete_message(message, channels, 5, 60)

        if confirm and not self.show_confirmation(message, title):
            return

        progress = ProgressIndicator(title, info, parent=self.get_dialog_parent())

        def finish_deletion(select_url):
            # Upload subscription list changes to the web service
            self.mygpo_client.on_unsubscribe([c.url for c in channels])

            # Re-load the channels and select the desired new channel
            self.update_podcast_list_model(select_url=select_url)
            progress.on_finished()

        @util.run_in_background
        def thread_proc():
            select_url = None

            for idx, channel in enumerate(channels):
                # Update the UI for correct status messages
                progress.on_progress(idx / len(channels))
                progress.on_message(channel.title)

                # Delete downloaded episodes
                channel.remove_downloaded()

                # cancel any active downloads from this channel
                for episode in channel.get_all_episodes():
                    if episode.downloading:
                        episode.download_task.cancel()

                if len(channels) == 1:
                    # get the URL of the podcast we want to select next
                    if channel in self.channels:
                        position = self.channels.index(channel)
                    else:
                        position = -1

                    if position == len(self.channels) - 1:
                        # this is the last podcast, so select the URL
                        # of the item before this one (i.e. the "new last")
                        select_url = self.channels[position - 1].url
                    else:
                        # there is a podcast after the deleted one, so
                        # we simply select the one that comes after it
                        select_url = self.channels[position + 1].url

                # Remove the channel and clean the database entries
                channel.delete()

            # Clean up downloads and download directories
            common.clean_up_downloads()

            # The remaining stuff is to be done in the GTK main thread
            util.idle_add(finish_deletion, select_url)

    def on_itemRefreshCover_activate(self, widget, *args):
        assert self.active_channel is not None

        self.podcast_list_model.clear_cover_cache(self.active_channel.url)
        self.cover_downloader.replace_cover(self.active_channel, custom_url=False)

    def on_itemRemoveChannel_activate(self, widget, *args):
        if self.active_channel is None:
            title = _('No podcast selected')
            message = _('Please select a podcast in the podcasts list to remove.')
            self.show_message(message, title, widget=self.treeChannels)
            return

        self.remove_podcast_list([self.active_channel])

    def get_opml_filter(self):
        filter = Gtk.FileFilter()
        filter.add_pattern('*.opml')
        filter.add_pattern('*.xml')
        filter.set_name(_('OPML files') + ' (*.opml, *.xml)')
        return filter

    def on_item_import_from_file_activate(self, action, filename=None):
        if filename is None:
            dlg = Gtk.FileChooserDialog(title=_('Import from OPML'),
                    parent=self.main_window,
                    action=Gtk.FileChooserAction.OPEN)
            dlg.add_button(_('_Cancel'), Gtk.ResponseType.CANCEL)
            dlg.add_button(_('_Open'), Gtk.ResponseType.OK)
            dlg.set_filter(self.get_opml_filter())
            response = dlg.run()
            filename = None
            if response == Gtk.ResponseType.OK:
                filename = dlg.get_filename()
            dlg.destroy()

        if filename is not None:
            dir = gPodderPodcastDirectory(self.gPodder, _config=self.config,
                    ui_folder=os.path.join(gpodder.ui_folders[0], '..', 'adaptive'),
                    custom_title=_('Import podcasts from OPML file'),
                    add_podcast_list=self.add_podcast_list,
                    hide_url_entry=True)
            dir.download_opml_file(filename)

    def on_itemExportChannels_activate(self, widget, *args):
        if not self.channels:
            title = _('Nothing to export')
            message = _('Your list of podcast subscriptions is empty. '
                        'Please subscribe to some podcasts first before '
                        'trying to export your subscription list.')
            self.show_message(message, title, widget=self.treeChannels)
            return

        dlg = Gtk.FileChooserDialog(title=_('Export to OPML'),
                                    parent=self.gPodder,
                                    action=Gtk.FileChooserAction.SAVE)
        dlg.add_button(_('_Cancel'), Gtk.ResponseType.CANCEL)
        dlg.add_button(_('_Save'), Gtk.ResponseType.OK)
        dlg.set_filter(self.get_opml_filter())
        response = dlg.run()
        if response == Gtk.ResponseType.OK:
            filename = dlg.get_filename()
            dlg.destroy()
            exporter = opml.Exporter(filename)
            if filename is not None and exporter.write(self.channels):
                count = len(self.channels)
                title = N_('%(count)d subscription exported',
                           '%(count)d subscriptions exported',
                           count) % {'count': count}
                self.show_message(_('Your podcast list has been successfully '
                                    'exported.'),
                                  title, widget=self.treeChannels)
            else:
                self.show_message(_('Could not export OPML to file. '
                                    'Please check your permissions.'),
                                  _('OPML export failed'), important=True)
        else:
            dlg.destroy()

    def on_itemImportChannels_activate(self, widget, *args):
        self._podcast_directory = gPodderPodcastDirectory(self.main_window,
                ui_folder=os.path.join(gpodder.ui_folders[0], '..', 'adaptive'),
                _config=self.config,
                add_podcast_list=self.add_podcast_list)

    def on_homepage_activate(self, widget, *args):
        util.open_website(gpodder.__url__)

    def check_for_distro_updates(self):
        title = _('Managed by distribution')
        message = _('Please check your distribution for gPodder updates.')
        self.show_message(message, title, important=True)

    def check_for_updates(self, silent):
        """Check for updates and (optionally) show a message

        If silent=False, a message will be shown even if no updates are
        available (set silent=False when the check is manually triggered).
        """
        try:
            up_to_date, version, released, days = util.get_update_info()
        except Exception as e:
            if silent:
                logger.warning('Could not check for updates.', exc_info=True)
            else:
                title = _('Could not check for updates')
                message = _('Please try again later.')
                self.show_message(message, title, important=True)
            return

        if up_to_date and not silent:
            title = _('No updates available')
            message = _('You have the latest version of gPodder.')
            self.show_message(message, title, important=True)

        if not up_to_date:
            title = _('New version available')
            message = '\n'.join([
                _('Installed version: %s') % gpodder.__version__,
                _('Newest version: %s') % version,
                _('Release date: %s') % released,
                '',
                _('Download the latest version from gpodder.org?'),
            ])

            if self.show_confirmation(message, title):
                util.open_website('http://gpodder.org/downloads')

#            self.toolDownload.set_sensitive(False)
#            self.toolPause.set_sensitive(False)

    def on_treeChannels_row_activated(self, widget, path, *args):
        # double-click action of the podcast list or enter
        self.treeChannels.set_cursor(path)
        self.navigate_from_shownotes()
        self.leaflet.set_can_swipe_forward(True)
        epath, ecolumn = self.treeAvailable.get_cursor()
        have_eps = True
        if epath is None or epath[0] < 0:
            have_eps = TreeViewHelper.set_cursor_to_first(self.treeAvailable)
        else:
            self.treeAvailable.set_cursor(epath)
        if have_eps:
            self.deck.set_can_swipe_forward(True)
        self.treeAvailable.grab_focus()
        self.leaflet.navigate(Handy.NavigationDirection.FORWARD)
        return True

        # open channel settings
        channel = self.get_selected_channels()[0]
        if channel and not isinstance(channel, PodcastChannelProxy):
            self.on_itemEditChannel_activate(None)

    def get_selected_channels(self):
        """Get a list of selected channels from treeChannels"""
        selection = self.treeChannels.get_selection()
        model, paths = selection.get_selected_rows()

        channels = [model.get_value(model.get_iter(path), PodcastListModel.C_CHANNEL) for path in paths]
        channels = [c for c in channels if c is not None]
        return channels

    def on_treeChannels_cursor_changed(self, widget, *args):
        (model, iter) = self.treeChannels.get_selection().get_selected()

        if model is not None and iter is not None:
            old_active_channel = self.active_channel
            self.active_channel = model.get_value(iter, PodcastListModel.C_CHANNEL)

            if self.active_channel == old_active_channel:
                return

            # Dirty hack to check for "All episodes" or a section (see gpodder.gtkui.model)
            if isinstance(self.active_channel, PodcastChannelProxy):
                self.edit_channel_action.set_enabled(False)
            else:
                self.edit_channel_action.set_enabled(True)
        else:
            self.active_channel = None
            self.edit_channel_action.set_enabled(False)

        self.deck.set_can_swipe_forward(False)
        self.update_episode_list_model()

    def on_treeChannels_unselect_all(self, widget, *params):
        self.leaflet.set_can_swipe_forward(False)
        return True

    def on_channel_list_go_forward(self):
        path, column = self.treeChannels.get_cursor()
        self.on_treeChannels_row_activated(self.treeChannels, path)
        self.leaflet.navigate(Handy.NavigationDirection.FORWARD)
        self.treeAvailable.grab_focus()
        return True

    def on_btnEditChannel_clicked(self, widget, *args):
        self.on_itemEditChannel_activate(widget, args)

    def get_podcast_urls_from_selected_episodes(self):
        """Get a set of podcast URLs based on the selected episodes"""
        return set(episode.channel.url for episode in
                self.get_selected_episodes())

    def get_selected_episodes(self):
        """Get a list of selected episodes from treeAvailable"""
        selection = self.treeAvailable.get_selection()
        model, paths = selection.get_selected_rows()

        episodes = [model.get_value(model.get_iter(path), EpisodeListModel.C_EPISODE) for path in paths]
        episodes = [e for e in episodes if e is not None]
        return episodes

    def on_playback_selected_episodes(self, *params):
        self.playback_episodes(self.get_selected_episodes())

    def on_new_toggled(self, button, *params):
        self.episode_new_action.activate()
        return True

    def on_new_state_changed(self, action, *params):
        state = action.get_state().get_boolean()
        self.new_toggle.handler_block_by_func(self.on_new_toggled)
        self.new_toggle.set_active(state)
        self.new_toggle.handler_unblock_by_func(self.on_new_toggled)

    def on_new_action(self, action, *params):
        state = not action.get_state().get_boolean()
        if state:
            self.mark_selected_episodes_new()
        else:
            self.mark_selected_episodes_old()
        action.change_state(GLib.Variant.new_boolean(state))
        self.episodes_popover.popdown()
        return True

    def on_shownotes_selected_episodes(self, *params):
        episodes = self.get_selected_episodes()
        assert episodes
        self.shownotes_object.show_pane(episodes)
        ep = episodes[0]
        # New button
        self.new_toggle.set_active(ep.is_new)
        # Download / Delete button
        # First reset everything ...
        # FIXME: there must be a better way
        try:
            self.dl_del_button.disconnect_by_func(self.on_episode_delete_clicked)
        except TypeError:
            pass
        try:
            self.dl_del_button.disconnect_by_func(self.on_episode_download_clicked)
        except TypeError:
            pass
        self.dl_del_button.set_sensitive(True)
        # ... then connect the correct handler
        if ep.downloading:
            self.dl_del_label.set_text(_("Cancel"))
            self.dl_del_button.connect("clicked", self.on_episodes_cancel_download_activate)
        elif ep.was_downloaded(and_exists=True):
            self.dl_del_label.set_text(_("Delete"))
            self.dl_del_button.connect("clicked", self.on_episode_delete_clicked)
        else:
            self.dl_del_label.set_text(_("Download"))
            self.dl_del_button.connect("clicked", self.on_episode_download_clicked)
        # Play / Stream button
        if ep.was_downloaded(and_exists=True):
            self.play_button.set_label(_("Play"))
            self.play_button.set_sensitive(True)
        else:
            self.play_button.set_label(_("Stream"))
            self.play_button.set_sensitive(ep.can_stream(self.config))
        self.navigate_to_shownotes()
        return True

    def on_download_selected_episodes(self, action_or_widget, param=None):
        episodes = [e for e in self.get_selected_episodes() if e.can_download()]
        self.download_episode_list(episodes)
        self.update_downloads_list()

    def on_pause_selected_episodes(self, action_or_widget, param=None):
        for episode in self.get_selected_episodes():
            if episode.can_pause():
                episode.download_task.pause()
        self.update_downloads_list()

    def on_episode_download_clicked(self, button):
        self.dl_del_label.set_text("Downloading")
        self.dl_del_button.set_sensitive(False)
        self.on_download_selected_episodes(button)
        self.navigate_from_shownotes()
        return True

    def on_episode_delete_clicked(self, button, *args):
        self.on_btnDownloadedDelete_clicked(button, *args)
        self.navigate_from_shownotes()
        return True

    def on_treeAvailable_row_activated(self, widget, path, view_column):
        """Double-click/enter action handler for treeAvailable"""
        self.on_shownotes_selected_episodes(widget)
        self.deck.set_can_swipe_forward(True)
        return True

    def on_treeAvailable_unselect_all(self, widget, *params):
        self.deck.set_can_swipe_forward(False)
        return True

    def restart_auto_update_timer(self):
        if self._auto_update_timer_source_id is not None:
            logger.debug('Removing existing auto update timer.')
            GObject.source_remove(self._auto_update_timer_source_id)
            self._auto_update_timer_source_id = None

        if (self.config.auto_update_feeds and
                self.config.auto_update_frequency):
            interval = 60 * 1000 * self.config.auto_update_frequency
            logger.debug('Setting up auto update timer with interval %d.',
                    self.config.auto_update_frequency)
            self._auto_update_timer_source_id = GObject.timeout_add(
                    interval, self._on_auto_update_timer)

    def _on_auto_update_timer(self):
        if self.config.check_connection and not util.connection_available():
            logger.debug('Skipping auto update (no connection available)')
            return True

        logger.debug('Auto update timer fired.')
        self.update_feed_cache()

        # Ask web service for sub changes (if enabled)
        if self.mygpo_client.can_access_webservice():
            self.mygpo_client.flush()

        return True

    def on_treeDownloads_row_activated(self, widget, *args):
        # Use the standard way of working on the treeview
        selection = self.treeDownloads.get_selection()
        (model, paths) = selection.get_selected_rows()
        selected_tasks = [(Gtk.TreeRowReference.new(model, path), model.get_value(model.get_iter(path), 0)) for path in paths]

        for tree_row_reference, task in selected_tasks:
            with task:
                if task.status in (task.DOWNLOADING, task.QUEUED):
                    task.pause()
                elif task.status in (task.CANCELLED, task.PAUSED, task.FAILED):
                    self.download_queue_manager.queue_task(task)
                    self.set_download_list_state(gPodderSyncUI.DL_ONEOFF)
                elif task.status == task.DONE:
                    model.remove(model.get_iter(tree_row_reference.get_path()))

        self.play_or_download()

        # Update the tab title and downloads list
        self.update_downloads_list()

    def on_episodes_cancel_download_activate(self, *params, force=False):
        selection = self.treeAvailable.get_selection()
        (model, paths) = selection.get_selected_rows()
        urls = [model.get_value(model.get_iter(path),
                self.episode_list_model.C_URL) for path in paths]
        selected_tasks = [task for task in self.download_tasks_seen
                if task.url in urls]
        self.cancel_task_list(selected_tasks, force=force)

    def on_progress_cancel_download_activate(self, *params, force=False):
        selection = self.treeDownloads.get_selection()
        (model, paths) = selection.get_selected_rows()
        selected_tasks = [model.get_value(model.get_iter(path),
                self.download_status_model.C_TASK) for path in paths]
        self.cancel_task_list(selected_tasks, force=force)

    def on_btnCancelAll_clicked(self, widget, *args):
        self.cancel_task_list(self.download_tasks_seen)

    def on_btnDownloadedDelete_clicked(self, widget, *args):
        episodes = self.get_selected_episodes()
        self.delete_episode_list(episodes)

    def on_key_press(self, widget, event):
        views = {
            Gdk.KEY_1: 'VIEW_ALL',
            Gdk.KEY_2: 'VIEW_UNDELETED',
            Gdk.KEY_3: 'VIEW_DOWNLOADED',
            Gdk.KEY_4: 'VIEW_UNPLAYED',
        }
        sorts = {
            Gdk.KEY_7: 'SORT_PUBLISHED',
            Gdk.KEY_8: 'SORT_TOTAL_TIME',
            Gdk.KEY_9: 'SORT_TITLE',
        }
        if event.keyval == Gdk.KEY_Delete:
            if isinstance(widget.get_focus(), Gtk.Entry):
                logger.debug("Entry has focus, ignoring Delete")
            else:
                self.main_window.activate_action('delete')
                return True
        elif event.keyval == Gdk.KEY_plus:
            self.header_bar_plus_button.emit("clicked")
            return True
        elif event.state & Gdk.ModifierType.CONTROL_MASK:
            if event.keyval == Gdk.KEY_0:
                self.sortorder_button.emit("clicked")
            elif event.keyval in views.keys():
                self.gPodder.lookup_action('viewEpisodes').activate(
                    GLib.Variant.new_string(views[event.keyval]))
            elif event.keyval in sorts.keys():
                self.gPodder.lookup_action('sortEpisodes').activate(
                    GLib.Variant.new_string(sorts[event.keyval]))
            elif event.keyval == Gdk.KEY_e:
                self.gPodder.lookup_action('episodeNew').activate()
            elif event.keyval == Gdk.KEY_k:
                self.gPodder.lookup_action('removeOldEpisodes').activate()
            elif event.keyval == Gdk.KEY_l:
                self.gPodder.lookup_action('addChannel').activate()
            elif event.keyval == Gdk.KEY_m:
                self.application.lookup_action('menu').activate()
            elif event.keyval == Gdk.KEY_n:
                self.gPodder.lookup_action('downloadAllNew').activate()
            elif event.keyval == Gdk.KEY_r:
                self.gPodder.lookup_action('update').activate()
            elif event.keyval == Gdk.KEY_s:
                self.sort_popover.popup()
            elif event.keyval == Gdk.KEY_v:
                self.view_popover.popup()
            else:
                return False
            return True
        return False

    def uniconify_main_window(self):
        if self.is_iconified():
            # We need to hide and then show the window in WMs like Metacity
            # or KWin4 to move the window to the active workspace
            # (see http://gpodder.org/bug/1125)
            self.gPodder.hide()
            self.gPodder.show()
            self.gPodder.present()

    def iconify_main_window(self):
        if not self.is_iconified():
            self.gPodder.iconify()

    @dbus.service.method(gpodder.dbus_interface)
    def show_gui_window(self):
        parent = self.get_dialog_parent()
        parent.present()

    @dbus.service.method(gpodder.dbus_interface)
    def subscribe_to_url(self, url):
        # Strip leading application protocol, so these URLs work:
        # gpodder://example.com/episodes.rss
        # gpodder:https://example.org/podcast.xml
        if url.startswith('gpodder:'):
            url = url[len('gpodder:'):]
            while url.startswith('/'):
                url = url[1:]

        self._add_podcast_dialog = gPodderAddPodcast(self.gPodder,
                add_podcast_list=self.add_podcast_list,
                preset_url=url)

    @dbus.service.method(gpodder.dbus_interface)
    def mark_episode_played(self, filename):
        if filename is None:
            return False

        for channel in self.channels:
            for episode in channel.get_all_episodes():
                fn = episode.local_filename(create=False, check_only=True)
                if fn == filename:
                    episode.mark(is_played=True)
                    self.db.commit()
                    self.update_episode_list_icons([episode.url])
                    self.update_podcast_list_model([episode.channel.url])
                    return True

        return False

    def extensions_podcast_update_cb(self, podcast):
        logger.debug('extensions_podcast_update_cb(%s)', podcast)
        self.update_feed_cache(channels=[podcast],
                show_new_episodes_dialog=False)

    def extensions_episode_download_cb(self, episode):
        logger.debug('extension_episode_download_cb(%s)', episode)
        self.download_episode_list(episodes=[episode])

    def mount_volume_cb(self, file, res, mount_result):
        result = True
        try:
            file.mount_enclosing_volume_finish(res)
        except GLib.Error as err:
            if (not err.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED) and
                    not err.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED)):
                logger.error('mounting volume %s failed: %s' % (file.get_uri(), err.message))
                result = False
        finally:
            mount_result["result"] = result
            Gtk.main_quit()

    def mount_volume_for_file(self, file):
        op = Gtk.MountOperation.new(self.main_window)
        result, message = util.mount_volume_for_file(file, op)
        if not result:
            logger.error('mounting volume %s failed: %s' % (file.get_uri(), message))
        return result

    def on_sync_to_device_activate(self, widget, episodes=None, force_played=True):
        self.sync_ui = gPodderSyncUI(self.config, self.notification,
                self.main_window,
                self.show_confirmation,
                self.application.on_itemPreferences_activate,
                self.channels,
                self.download_status_model,
                self.download_queue_manager,
                self.set_download_list_state,
                self.commit_changes_to_database,
                self.delete_episode_list,
                gPodderEpisodeSelector,
                self.mount_volume_for_file)

        def done_cb():
            self.set_download_list_state(gPodderSyncUI.DL_ONEOFF)
            util.idle_add(self.transfer_revealer.set_reveal_child, False)

        self.transfer_revealer.set_reveal_child(True)
        self.sync_ui.on_synchronize_episodes(self.channels, episodes, force_played,
                                             done_cb)

    def on_extension_enabled(self, extension):
        if getattr(extension, 'on_ui_object_available', None) is not None:
            extension.on_ui_object_available('gpodder-gtk', self)
        if getattr(extension, 'on_ui_initialized', None) is not None:
            extension.on_ui_initialized(self.model,
                    self.extensions_podcast_update_cb,
                    self.extensions_episode_download_cb)
        self.extensions_menu_helper.replace_entries(gpodder.user_extensions.on_create_menu())

    def on_extension_disabled(self, extension):
        self.extensions_menu_helper.replace_entries(gpodder.user_extensions.on_create_menu())
