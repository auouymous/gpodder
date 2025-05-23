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


#
#  gpodder.gtkui.services - UI parts for the services module (2009-08-24)
#


import logging

from gi.repository import GdkPixbuf

import gpodder
from gpodder import coverart, util
from gpodder.services import ObservableService

_ = gpodder.gettext

logger = logging.getLogger(__name__)


class CoverDownloader(ObservableService):
    """Manages downloading cover art and notification of other parts of the system.

    Downloading cover art can happen either synchronously via get_cover() or in
    asynchronous mode via request_cover(). When in async mode,
    the cover downloader will send the cover via the
    'cover-available' message (via the ObservableService).
    """

    def __init__(self):
        self.downloader = coverart.CoverDownloader()
        signal_names = ['cover-available', 'cover-removed']
        ObservableService.__init__(self, signal_names)

    def request_cover(self, channel, custom_url=None, avoid_downloading=False):
        """Send an asynchronous request to download a cover for the specific channel.

        After the cover has been downloaded, the
        "cover-available" signal will be sent with
        the channel url and new cover as pixbuf.

        If you specify a custom_url, the cover will
        be downloaded from the specified URL and not
        taken from the channel metadata.

        The optional parameter "avoid_downloading",
        when true, will make sure we return only
        already-downloaded covers and return None
        when we have no cover on the local disk.
        """
        logger.debug('cover download request for %s', channel.url)
        util.run_in_background(lambda: self.__get_cover(channel,
            custom_url, True, avoid_downloading))

    def get_cover(self, channel, custom_url=None, avoid_downloading=False):
        """Send a synchronous request to download a cover for the specified channel.

        The cover will be returned to the caller.

        The custom_url has the same semantics as
        in request_cover().

        The optional parameter "avoid_downloading",
        when true, will make sure we return only
        already-downloaded covers and return None
        when we have no cover on the local disk.
        """
        (url, pixbuf) = self.__get_cover(channel, custom_url, False, avoid_downloading)
        return pixbuf

    def replace_cover(self, channel, custom_url=None):
        """Delete the current cover file and request a new cover from the URL."""
        self.request_cover(channel, custom_url)

    def __get_cover(self, channel, url, async_mode=False, avoid_downloading=False):
        def get_filename():
            return self.downloader.get_cover(channel.cover_file,
                    url or channel.cover_url, channel.url, channel.title,
                    channel.auth_username, channel.auth_password,
                    not avoid_downloading)

        if url is not None:
            filename = get_filename()
            if filename.startswith(channel.cover_file):
                logger.info('Replacing cover: %s', filename)
                util.delete_file(filename)

        filename = get_filename()
        pixbuf = None

        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(filename)
        except Exception:
            logger.warning('Cannot load cover art', exc_info=True)
        if pixbuf is None and filename.startswith(channel.cover_file):
            logger.info('Deleting broken cover: %s', filename)
            util.delete_file(filename)
            filename = get_filename()
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(filename)
            except Exception:
                logger.warning('Corrupt cover art on server, deleting', exc_info=True)
                util.delete_file(filename)

        if async_mode:
            self.notify('cover-available', channel, pixbuf)
        else:
            return (channel.url, pixbuf)
