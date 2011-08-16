# dj.py, youtube mixer
#
# Copyright (c) 2011, Duponchelle Mathieu (mduponchelle1@gmail.com)
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin St, Fifth Floor,
# Boston, MA 02110-1301, USA.

import re
import urllib2
import httplib
import socket
import urllib
import htmlentitydefs
import os
import string
import gio
from urlparse import urlparse
import glib
import optparse
from gst import ges
import gst
from gobject import timeout_add
from gdata.youtube.service import YouTubeService
import sys
import gtk

simple_title_chars = string.ascii_letters.decode('ascii') + string.digits.decode('ascii')

try:
    from urlparse import parse_qs
except ImportError:
    from cgi import parse_qs

def htmlentity_transform(matchobj):
    """Transforms an HTML entity to a Unicode character.

    This function receives a match object and is intended to be used with
    the re.sub() function.
    """
    entity = matchobj.group(1)

    # Known non-numeric HTML entity
    if entity in htmlentitydefs.name2codepoint:
        return unichr(htmlentitydefs.name2codepoint[entity])

    # Unicode character
    mobj = re.match(ur'(?u)#(x?\d+)', entity)
    if mobj is not None:
        numstr = mobj.group(1)
        if numstr.startswith(u'x'):
            base = 16
            numstr = u'0%s' % numstr
        else:
            base = 10
        return unichr(long(numstr, base))

    # Unknown entity in name, return its literal representation
    return (u'&%s;' % entity)


def sanitize_title(utitle):
    """Sanitizes a video title so it could be used as part of a filename."""
    utitle = re.sub(ur'(?u)&(.+?);', htmlentity_transform, utitle)
    return utitle.replace(unicode(os.sep), u'%')

class Mixer:
    def __init__(self, app):
        ges.init()
        self.app = app
        self.tl = ges.timeline_new_audio_video()
        self.layer = ges.TimelineLayer()
        self.tl.add_layer(self.layer)
        self.pipeline = ges.TimelinePipeline()
        self.pipeline.add_timeline(self.tl)
        self.bus = self.pipeline.get_bus()
        self.bus.set_sync_handler(self._elementMessageCb)
        self.bus.connect("sync-message::element", self.on_sync_message)
        self.srclist = []
        self.prev_end = 0

    def add_source(self, uri):
        uri = "file://" + uri
        if len(self.srclist) > 0:
            prev_dur = long(self.srclist[len(self.srclist) - 1].get_property("duration"))
            if prev_dur < long (30000000000):
                trans_dur = prev_dur / 2
            else:
                trans_dur = long (20000000000)
            self.prev_end = long(self.srclist[len(self.srclist) - 1].get_property("duration")) - long(trans_dur)
            self.prev_end = self.prev_end + long(self.srclist[len(self.srclist) - 1].get_property("start"))
        src = ges.TimelineFileSource(uri)
        src.set_start(long(self.prev_end))
        if len(self.srclist) < 10:
            self.layer.add_object(src)
        self.srclist.append(src)

    def start_playing(self):
        self.pipeline.set_state(gst.STATE_PLAYING)

    def change_starts(self):
        self.pipeline.set_state(gst.STATE_PAUSED)
        self.tl.enable_update(False)
        print "started"
        pos = self.pipeline.query_position(gst.FORMAT_TIME)[0]
        current_src = None
        for src in self.srclist:
            if long(src.get_property("start")) < long(pos) < long(src.get_property("duration")) + long(src.get_property("start")):
                current_src = src
                break
        self.pipeline.seek(1.0, gst.FORMAT_TIME, gst.SEEK_FLAG_FLUSH,
                                  gst.SEEK_TYPE_SET, long(long(current_src.get_property("duration")) + long(current_src.get_property("start")) - long(25000000000)),
                                  gst.SEEK_TYPE_NONE, -1)
        self.pipeline.set_state(gst.STATE_PLAYING)
        self.tl.enable_update(True)
        print "ended seeking"

    def _elementMessageCb(self, unused_bus, message):
        if message.type == gst.MESSAGE_ELEMENT:
            name = message.structure.get_name()
            if name == 'prepare-xwindow-id':
                sink = message.src
                self.sink = sink
                gtk.gdk.threads_enter()
                self.sink.set_xwindow_id(self.app.movie_window.window.xid)
                gtk.gdk.threads_leave()
        return gst.BUS_PASS

    def on_sync_message(self, bus, message):
        print "rm"
        if message.structure is None:
            return
        message_name = message.structure.get_name()
        print message_name
        if message_name == "prepare-xwindow-id":
            print "zob"
            imagesink = message.src
            imagesink.set_property("force-aspect-ratio", True)
            gtk.gdk.threads_enter()
            imagesink.set_xwindow_id(self.app.movie_window.window.xid)
            gtk.gdk.threads_leave()

class YouTubeDl:
    _video_extensions = {
        '13': '3gp',
        '17': 'mp4',
        '18': 'mp4',
        '22': 'mp4',
        '37': 'mp4',
        '38': 'video', # You actually don't know if this will be MOV, AVI or whatever
        '43': 'webm',
        '45': 'webm',
    }

    _available_formats = ['38', '37', '22', '45', '35', '34', '43', '18', '6', '5', '17', '13']
    _VALID_URL = r'^((?:https?://)?(?:youtu\.be/|(?:\w+\.)?youtube(?:-nocookie)?\.com/)(?:(?:(?:v|embed|e)/)|(?:(?:watch(?:_popup)?(?:\.php)?)?(?:\?|#!?)(?:.+&)?v=)))?([0-9A-Za-z_-]+)(?(1).+)?$'
    def extractUrl(self, url):
        # Extract video id from URL
        mobj = re.match(self._VALID_URL, url)
        if mobj is None:
            self._downloader.trouble(u'ERROR: invalid URL: %s' % url)
            return
        video_id = mobj.group(2)

        # Get video webpage
        request = urllib2.Request('http://www.youtube.com/watch?v=%s&gl=US&hl=en&amp;has_verified=1' % video_id)
        try:
            video_webpage = urllib2.urlopen(request).read()
        except (urllib2.URLError, httplib.HTTPException, socket.error), err:
            self._downloader.trouble(u'ERROR: unable to download video webpage: %s' % str(err))
            return

        # Attempt to extract SWF player URL
        mobj = re.search(r'swfConfig.*?"(http:\\/\\/.*?watch.*?-.*?\.swf)"', video_webpage)
        if mobj is not None:
            player_url = re.sub(r'\\(.)', r'\1', mobj.group(1))
        else:
            player_url = None

        # Get video info
        for el_type in ['&el=embedded', '&el=detailpage', '&el=vevo', '']:
            video_info_url = ('http://www.youtube.com/get_video_info?&video_id=%s%s&ps=default&eurl=&gl=US&hl=en'
                       % (video_id, el_type))
            request = urllib2.Request(video_info_url)
            try:
                video_info_webpage = urllib2.urlopen(request).read()
                video_info = parse_qs(video_info_webpage)
                if 'token' in video_info:
                    break
            except (urllib2.URLError, httplib.HTTPException, socket.error), err:
                self._downloader.trouble(u'ERROR: unable to download video info webpage: %s' % str(err))
                return
        if 'token' not in video_info:
            if 'reason' in video_info:
                self._downloader.trouble(u'ERROR: YouTube said: %s' % video_info['reason'][0].decode('utf-8'))
            else:
                self._downloader.trouble(u'ERROR: "token" parameter not in video info for unknown reason')
            return

        # uploader
        if 'author' not in video_info:
            self._downloader.trouble(u'ERROR: unable to extract uploader nickname')
            return
        video_uploader = urllib.unquote_plus(video_info['author'][0])

        # title
        if 'title' not in video_info:
            self._downloader.trouble(u'ERROR: unable to extract video title')
            return
        video_title = urllib.unquote_plus(video_info['title'][0])
        video_title = video_title.decode('utf-8')
        video_title = sanitize_title(video_title)

        # simplified title
        simple_title = re.sub(ur'(?u)([^%s]+)' % simple_title_chars, ur'_', video_title)
        simple_title = simple_title.strip(ur'_')

        # thumbnail image
        if 'thumbnail_url' not in video_info:
            self._downloader.trouble(u'WARNING: unable to extract video thumbnail')
            video_thumbnail = ''
        else:   # don't panic if we can't find it
            video_thumbnail = urllib.unquote_plus(video_info['thumbnail_url'][0])

        # upload date
        upload_date = u'NA'
        mobj = re.search(r'id="eow-date.*?>(.*?)</span>', video_webpage, re.DOTALL)
        if mobj is not None:
            upload_date = ' '.join(re.sub(r'[/,-]', r' ', mobj.group(1)).split())
            format_expressions = ['%d %B %Y', '%B %d %Y', '%b %d %Y']
            for expression in format_expressions:
                try:
                    upload_date = datetime.datetime.strptime(upload_date, expression).strftime('%Y%m%d')
                except:
                    pass

        # description
        video_description = 'No description available.'
        mobj = re.search(r'<meta name="description" content="(.*)"(?:\s*/)?>', video_webpage)
        if mobj is not None:
            video_description = mobj.group(1)

        # token
        video_token = urllib.unquote_plus(video_info['token'][0])

        # Decide which formats to download
        req_format = None

        if 'url_encoded_fmt_stream_map' in video_info and len(video_info['url_encoded_fmt_stream_map']) >= 1:
            url_data_strs = video_info['url_encoded_fmt_stream_map'][0].split(',')
            url_data = [dict(pairStr.split('=') for pairStr in uds.split('&')) for uds in url_data_strs]
            url_map = dict((ud['itag'], urllib.unquote(ud['url'])) for ud in url_data)
            format_limit = 0
            if format_limit is not None and format_limit in self._available_formats:
                format_list = self._available_formats[self._available_formats.index(format_limit):]
            else:
                format_list = self._available_formats
            existing_formats = [x for x in format_list if x in url_map]
            if len(existing_formats) == 0:
                self._downloader.trouble(u'ERROR: no known formats available for video')
                return
            if req_format is None:
                video_url_list = [(existing_formats[0], url_map[existing_formats[0]])] # Best quality
            elif req_format == '-1':
                video_url_list = [(f, url_map[f]) for f in existing_formats] # All formats
            else:
                # Specific format
                if req_format not in url_map:
                    return
                video_url_list = [(req_format, url_map[req_format])] # Specific format

        elif 'conn' in video_info and video_info['conn'][0].startswith('rtmp'):
            self.report_rtmp_download()
            video_url_list = [(None, video_info['conn'][0])]

        else:
            return

        self.video_url_list = video_url_list
        return True

    def youtubedownload(self, uri, app):
        """download using gio"""
        self.app = app
        self.firstBuffer = False
        url = self.video_url_list[0][1]
        self.uri = uri
        self.path = urlparse(uri).path
        if os.path.exists(self.path):
            os.remove(self.path)
        dest = gio.File(uri)
        stream = gio.File(url)
        self.canc = gio.Cancellable()
        stream.copy_async(dest, self.app._downloadFileComplete,
            progress_callback = self._progressCb, cancellable = self.canc)

    def _progressCb(self, current, total):
        self.current = float(current)
        self.total = float(total)

class Application:
    def __init__(self):

        gtk.gdk.threads_init()
        window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        window.set_title("Video-Player")
        window.set_default_size(500, 400)
        window.connect("destroy", gtk.main_quit, "WM destroy")
        window.connect("destroy", self.destroy)
        vbox = gtk.VBox()
        window.add(vbox)
        hbox = gtk.HBox()
        vbox.pack_start(hbox, False)
        self.entry = gtk.Entry()
        hbox.add(self.entry)
        self.entry.connect
        self.button = gtk.Button("Start")
        hbox.pack_start(self.button, False)
        self.next_button = gtk.Button("Next")
        hbox.pack_start(self.next_button, False)
        self.next_button.connect("clicked", self._nextCb)
        self.button.connect("clicked", self._activatedCb)
        self.entry.connect("activate", self._activatedCb)
        self.movie_window = gtk.DrawingArea()
        vbox.add(self.movie_window)
        window.show_all()
        self.window = window
        self.movie_window.add_events(gtk.gdk.BUTTON_PRESS_MASK)
        self.movie_window.connect('button-press-event', self._on_movie_press_cb)
        self.movie_window.connect('button-release-event', self._on_movie_press_cb)

        self.dl = YouTubeDl()
        self.viewer = Mixer(self)
        self.dl_folder = None
        self.playing = False
        self.full = False

    def _on_movie_press_cb(self, widget, event):
        event.button = 1
        if event.type == gtk.gdk._2BUTTON_PRESS:
            if self.full == False:
                self.entry.hide()
                self.button.hide()
                self.next_button.hide()
                self.window.fullscreen()
                self.full = True
            else:
                self.entry.show()
                self.button.show()
                self.next_button.show()
                self.window.unfullscreen()
                self.full = False

    def _activatedCb(self, entry):
        text = self.entry.get_text()
        self.entry.set_text("")
        self.add_video(text)
        if not self.playing :
            timeout_add(2000, self.start_playing)
            self.playing = True

    def _nextCb(self, button):
        print "ok"
        self.viewer.change_starts()

    def destroy(self, unused):
        for the_file in os.listdir(self.dl_folder):
            file_path = os.path.join(self.dl_folder, the_file)
            try:
                os.unlink(file_path)
            except Exception, e:
                print e

    def start_playing(self):
        timeout_add(5000, self.viewer.start_playing)

    def add_video(self, url):
        if not (self.dl.extractUrl(url)):
            sys.exit(1)
        url = url.split("/", 10)
        self.dl.youtubedownload (self.dl_folder + "/" + url[3], self)
        self.short_name = url[3].split("?v=", 2)[1]
        self.short_name = self.short_name.split("&", 2)[0]
        timeout_add(1000, self.viewer.add_source, self.dl.uri)

    def _downloadFileComplete(self, gdaemonfile, result):
        print "get feed"
        related_feed = YouTubeService().GetYouTubeRelatedVideoFeed(video_id = self.short_name)
        print "set best"
        best = None
        best_ratio = 0
        print "start loop"
        for entry in related_feed.entry:
            ratio = float(entry.statistics.favorite_count) / float (entry.statistics.view_count)
            if ratio > best_ratio:
                best_ratio = ratio
                best = entry
        print best.media.title.text
        self.add_video(best.media.player.url)
        print "added video"

def main(args):
    usage = "usage : %s download folder\n" % args[0]
    if (len(args) < 2):
        sys.stderr.write(usage)
        sys.exit(1)
    parser = optparse.OptionParser (usage=usage)
    (opts, args) = parser.parse_args ()

    a = Application()
    a.dl_folder = (args[0])
    gtk.main()

if __name__ == "__main__":
    main(sys.argv)
