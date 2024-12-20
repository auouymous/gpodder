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
#  opml.py -- OPML import and export functionality
#  Thomas Perl <thp@perli.net>   2007-08-19
#
#  based on: libopmlreader.py (2006-06-13)
#            libopmlwriter.py (2005-12-08)
#

"""OPML import and export functionality.

This module contains helper classes to import subscriptions
from OPML files on the web and to export a list of channel
objects to valid OPML 1.1 files that can be used to backup
or distribute gPodder's channel subscriptions.
"""

import io
import logging
import os
import os.path
import xml.dom.minidom
from email.utils import formatdate

import gpodder
from gpodder import util

logger = logging.getLogger(__name__)


class Importer(object):
    """Import an OPML feed and return a GTK ListStore.

    Helper class to import an OPML feed from protocols
    supported by urllib2 (e.g. HTTP) and return a GTK
    ListStore that can be displayed in the GUI.

    This class should support standard OPML feeds and
    contains workarounds to support odeo.com feeds.
    """

    VALID_TYPES = ('rss', 'link')

    def __init__(self, url):
        """Parse an OPML feed from an URL.

        Parses the feed into a local data structure containing channel metadata.
        """
        self.items = []
        if os.path.exists(url):
            doc = xml.dom.minidom.parse(url)
        else:
            doc = xml.dom.minidom.parse(io.BytesIO(util.urlopen(url).content))

        section = None
        for outline in doc.getElementsByTagName('outline'):
            # Make sure we are dealing with a valid link type (ignore case)
            otl_type = outline.getAttribute('type')
            if otl_type is None or otl_type.lower() not in self.VALID_TYPES:
                otl_title = outline.getAttribute('title')
                otl_text = outline.getAttribute('text')
                # gPodder sections will have name == text, if OPML accepts it type=section
                if otl_title is not None and otl_title == otl_text:
                    section = otl_title
                continue

            if outline.getAttribute('xmlUrl') or outline.getAttribute('url'):
                channel = {
                    'url':
                        outline.getAttribute('xmlUrl')
                        or outline.getAttribute('url'),
                    'title':
                        outline.getAttribute('title')
                        or outline.getAttribute('text')
                        or outline.getAttribute('xmlUrl')
                        or outline.getAttribute('url'),
                    'description':
                        outline.getAttribute('text')
                        or outline.getAttribute('xmlUrl')
                        or outline.getAttribute('url'),
                    'section': section,
                }

                if channel['description'] == channel['title']:
                    channel['description'] = channel['url']

                for attr in ('url', 'title', 'description'):
                    channel[attr] = channel[attr].strip()

                self.items.append(channel)
        if not len(self.items):
            logger.info('OPML import finished, but no items found: %s', url)


class Exporter(object):
    """Export a list of channel objects to a local file in OPML 1.1 format.

    See www.opml.org for the OPML specification.
    """

    FEED_TYPE = 'rss'

    def __init__(self, filename):
        if filename is None:
            self.filename = None
        elif filename.endswith('.opml') or filename.endswith('.xml'):
            self.filename = filename
        else:
            self.filename = '%s.opml' % (filename, )

    def create_node(self, doc, name, content):
        """Return an XML Element node with tag name "name" and text content "content".

        E.g. <name>content</name>.
        """
        node = doc.createElement(name)
        node.appendChild(doc.createTextNode(content))
        return node

    def create_outline(self, doc, channel):
        """Return an OPML outline as XML Element node in the supplied document."""
        outline = doc.createElement('outline')
        outline.setAttribute('title', channel.title)
        outline.setAttribute('text', channel.description)
        outline.setAttribute('xmlUrl', channel.url)
        outline.setAttribute('type', self.FEED_TYPE)
        return outline

    def create_section(self, doc, name):
        """Create an empty OPML outline element used to divide sections."""
        section = doc.createElement('outline')
        section.setAttribute('title', name)
        section.setAttribute('text', name)
        return section

    def write(self, channels):
        """Write an XML document containing metadata for each channel in channels.

        OPML 2.0 specification: http://www.opml.org/spec2

        Returns True on success or False when there was an
        error writing the file.
        """
        if self.filename is None:
            return False

        doc = xml.dom.minidom.Document()

        opml = doc.createElement('opml')
        opml.setAttribute('version', '2.0')
        doc.appendChild(opml)

        head = doc.createElement('head')
        head.appendChild(self.create_node(doc, 'title', 'gPodder subscriptions'))
        head.appendChild(self.create_node(doc, 'dateCreated', formatdate(localtime=True)))
        opml.appendChild(head)

        body = doc.createElement('body')

        sections = {}
        for channel in channels:
            if channel.section not in sections.keys():
                sections[channel.section] = self.create_section(doc, channel.section)
            sections[channel.section].appendChild(self.create_outline(doc, channel))

        for section in sections.values():
            body.appendChild(section)

        opml.appendChild(body)

        try:
            data = doc.toprettyxml(encoding='utf-8', indent='    ', newl=os.linesep)
            # We want to have at least 512 KiB free disk space after
            # saving the opml data, if this is not possible, don't
            # try to save the new file, but keep the old one so we
            # don't end up with a clobbed, empty opml file.
            FREE_DISK_SPACE_AFTER = 1024 * 512
            path = os.path.dirname(self.filename) or os.path.curdir
            available = util.get_free_disk_space(path)
            if available != -1 and available < 2 * len(data) + FREE_DISK_SPACE_AFTER:
                # On Windows, if we have zero bytes available, assume that we have
                # not had the win32file module available + assume enough free space
                if not gpodder.ui.win32 or available > 0:
                    logger.error('Not enough free disk space to save channel list to %s', self.filename)
                    return False
            fp = open(self.filename + '.tmp', 'wb')
            fp.write(data)
            fp.close()
            util.atomic_rename(self.filename + '.tmp', self.filename)
        except:
            logger.error('Could not open file for writing: %s', self.filename,
                    exc_info=True)
            return False

        return True
