# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2015 Antoine Martin <antoine@devloop.org.uk>
# Copyright (C) 2008, 2010 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import sys
import time
import datetime
import traceback
import logging
from collections import deque

from xpra.log import Logger, set_global_logging_handler
log = Logger("client")
windowlog = Logger("client", "window")
paintlog = Logger("client", "paint")
focuslog = Logger("client", "focus")
soundlog = Logger("client", "sound")
traylog = Logger("client", "tray")
keylog = Logger("client", "keyboard")
workspacelog = Logger("client", "workspace")
dbuslog = Logger("client", "dbus")
grablog = Logger("client", "grab")
iconlog = Logger("client", "icon")
screenlog = Logger("client", "screen")
mouselog = Logger("mouse")

from xpra import __version__ as XPRA_VERSION
from xpra.gtk_common.gobject_util import no_arg_signal
from xpra.client.client_base import XpraClientBase, EXIT_TIMEOUT, EXIT_MMAP_TOKEN_FAILURE
from xpra.client.client_tray import ClientTray
from xpra.client.keyboard_helper import KeyboardHelper
from xpra.platform import set_application_name
from xpra.platform.features import MMAP_SUPPORTED, SYSTEM_TRAY_SUPPORTED, CLIPBOARD_WANT_TARGETS, CLIPBOARD_GREEDY, CLIPBOARDS, REINIT_WINDOWS
from xpra.platform.gui import (ready as gui_ready, get_vrefresh, get_antialias_info, get_double_click_time, show_desktop,
                               get_double_click_distance, get_native_notifier_classes, get_native_tray_classes, get_native_system_tray_classes,
                               get_native_tray_menu_helper_classes, get_dpi, get_xdpi, get_ydpi, get_number_of_desktops, get_desktop_names, ClientExtras)
from xpra.codecs.codec_constants import get_PIL_decodings
from xpra.codecs.loader import codec_versions, has_codec, get_codec, PREFERED_ENCODING_ORDER, PROBLEMATIC_ENCODINGS
from xpra.codecs.video_helper import getVideoHelper, NO_GFX_CSC_OPTIONS
from xpra.scripts.main import sound_option
from xpra.scripts.config import parse_bool_or_int
from xpra.simple_stats import std_unit
from xpra.net import compression, packet_encoding
from xpra.child_reaper import reaper_cleanup
from xpra.daemon_thread import make_daemon_thread
from xpra.os_util import Queue, os_info, platform_name, get_machine_id, get_user_uuid, bytestostr
from xpra.util import nonl, std, AtomicInteger, AdHocStruct, log_screen_sizes, typedict, CLIENT_EXIT
try:
    from xpra.clipboard.clipboard_base import ALL_CLIPBOARDS
except:
    ALL_CLIPBOARDS = []

FAKE_BROKEN_CONNECTION = int(os.environ.get("XPRA_FAKE_BROKEN_CONNECTION", "0"))
PING_TIMEOUT = int(os.environ.get("XPRA_PING_TIMEOUT", "60"))
UNGRAB_KEY = os.environ.get("XPRA_UNGRAB_KEY", "Escape")

MONITOR_CHANGE_REINIT = os.environ.get("XPRA_MONITOR_CHANGE_REINIT")

PYTHON3 = sys.version_info[0] == 3
WIN32 = sys.platform.startswith("win")


"""
Utility superclass for client classes which have a UI.
See gtk_client_base and its subclasses.
"""
class UIXpraClient(XpraClientBase):
    #NOTE: these signals aren't registered because this class
    #does not extend GObject.
    __gsignals__ = {
        "handshake-complete"        : no_arg_signal,
        "first-ui-received"         : no_arg_signal,

        "clipboard-toggled"         : no_arg_signal,
        "keyboard-sync-toggled"     : no_arg_signal,
        "speaker-changed"           : no_arg_signal,        #bitrate or pipeline state has changed
        "microphone-changed"        : no_arg_signal,        #bitrate or pipeline state has changed
        }

    def __init__(self):
        XpraClientBase.__init__(self)
        try:
            from xpra.src_info import REVISION
            rev_info = " (r%s)" % REVISION
        except:
            rev_info = ""
        log.info("xpra %s client version %s%s", self.client_toolkit(), XPRA_VERSION, rev_info)
        self.start_time = time.time()
        self._window_to_id = {}
        self._id_to_window = {}
        self._ui_events = 0
        self.title = ""
        self.session_name = ""
        self.auto_refresh_delay = -1
        self.max_window_size = 0, 0
        self.dpi = 0

        #draw thread:
        self._draw_queue = None
        self._draw_thread = None

        #statistics and server info:
        self.server_start_time = -1
        self.server_platform = ""
        self.server_actual_desktop_size = None
        self.server_max_desktop_size = None
        self.server_display = None
        self.server_randr = False
        self.pixel_counter = deque(maxlen=1000)
        self.server_ping_latency = deque(maxlen=1000)
        self.server_load = None
        self.client_ping_latency = deque(maxlen=1000)
        self._server_ok = True
        self.last_ping_echoed_time = 0
        self.server_info_request = False
        self.server_last_info = None
        self.info_request_pending = False
        self.screen_size_change_pending = False
        self.allowed_encodings = []
        self.core_encodings = None
        self.encoding = None

        #sound:
        self.sound_source_plugin = None
        self.speaker_allowed = False
        self.speaker_enabled = False
        self.speaker_codecs = []
        self.microphone_allowed = False
        self.microphone_enabled = False
        self.microphone_codecs = []
        try:
            from xpra.sound.gstreamer_util import has_gst, get_sound_codecs
            self.speaker_allowed = has_gst
            if self.speaker_allowed:
                self.speaker_codecs = get_sound_codecs(True, False)
                self.speaker_allowed = len(self.speaker_codecs)>0
            self.microphone_allowed = has_gst
            self.microphone_enabled = False
            self.microphone_codecs = []
            if self.microphone_allowed:
                self.microphone_codecs = get_sound_codecs(False, False)
                self.microphone_allowed = len(self.microphone_codecs)>0
            if has_gst:
                soundlog("speaker_allowed=%s, speaker_codecs=%s", self.speaker_allowed, self.speaker_codecs)
                soundlog("microphone_allowed=%s, microphone_codecs=%s", self.microphone_allowed, self.microphone_codecs)
        except Exception as e:
            soundlog("sound support unavailable: %s", e)
            has_gst = False
        #sound state:
        self.on_sink_ready = None
        self.sound_sink = None
        self.server_sound_sequence = False
        self.min_sound_sequence = 0
        self.server_sound_eos_sequence = False
        self.sound_source = None
        self.sound_in_bytecount = 0
        self.sound_out_bytecount = 0
        self.server_pulseaudio_id = None
        self.server_pulseaudio_server = None
        self.server_sound_decoders = []
        self.server_sound_encoders = []
        self.server_sound_receive = False
        self.server_sound_send = False

        #dbus:
        self.dbus_counter = AtomicInteger()
        self.dbus_pending_requests = {}

        #mmap:
        self.mmap_enabled = False
        self.mmap = None
        self.mmap_token = None
        self.mmap_filename = None
        self.mmap_size = 0
        self.mmap_group = None
        self.mmap_tempfile = None

        #features:
        self.opengl_enabled = False
        self.opengl_props = {}
        self.toggle_cursors_bell_notify = False
        self.toggle_keyboard_sync = False
        self.force_ungrab = False
        self.window_unmap = False
        self.window_refresh_config = False
        self.server_encodings = []
        self.server_core_encodings = []
        self.server_encodings_problematic = PROBLEMATIC_ENCODINGS
        self.server_encodings_with_speed = ()
        self.server_encodings_with_quality = ()
        self.server_encodings_with_lossless = ()
        self.change_quality = False
        self.change_min_quality = False
        self.change_speed = False
        self.readonly = False
        self.windows_enabled = True
        self.pings = False
        self.xsettings_enabled = False
        self.server_dbus_proxy = False
        self.start_new_commands = False

        self.client_supports_opengl = False
        self.client_supports_notifications = False
        self.client_supports_system_tray = False
        self.client_supports_clipboard = False
        self.client_supports_cursors = False
        self.client_supports_bell = False
        self.client_supports_sharing = False
        self.client_supports_remote_logging = False
        self.notifications_enabled = False
        self.clipboard_enabled = False
        self.cursors_enabled = False
        self.bell_enabled = False
        self.border = None

        self.supports_mmap = MMAP_SUPPORTED

        #helpers and associated flags:
        self.client_extras = None
        self.keyboard_helper = None
        self.kh_warning = False
        self.clipboard_helper = None
        self.menu_helper = None
        self.tray = None
        self.notifier = None
        self.in_remote_logging = False
        self.local_logging = None

        #state:
        self._focused = None
        self._window_with_grab = None
        self._last_screen_settings = None
        self._suspended_at = 0
        self._button_state = {}

        self.init_aliases()


    def init(self, opts):
        """ initialize variables from configuration """
        self.allowed_encodings = opts.encodings
        self.encoding = opts.encoding
        self.scaling = parse_bool_or_int("scaling", opts.scaling)
        self.title = opts.title
        self.session_name = opts.session_name
        self.auto_refresh_delay = opts.auto_refresh_delay
        if opts.max_size:
            try:
                self.max_window_size = [int(x.strip()) for x in opts.max_size.split("x", 1)]
                assert len(self.max_window_size)==2
            except:
                #the main script does some checking, but we could be called from a config file launch
                log.warn("Warning: invalid window max-size specified: %s", opts.max_size)
                self.max_window_size = 0, 0
        self.dpi = int(opts.dpi)
        self.xsettings_enabled = opts.xsettings
        self.supports_mmap = MMAP_SUPPORTED and opts.mmap
        self.mmap_group = opts.mmap_group

        try:
            from xpra.sound.gstreamer_util import has_gst, get_sound_codecs
        except:
            has_gst = False
        self.sound_source_plugin = opts.sound_source
        self.speaker_allowed = sound_option(opts.speaker) in ("on", "off") and has_gst
        self.speaker_enabled = sound_option(opts.speaker)=="on" and has_gst
        self.microphone_allowed = sound_option(opts.microphone) in ("on", "off") and has_gst
        self.microphone_enabled = sound_option(opts.microphone)=="on" and has_gst
        self.speaker_codecs = opts.speaker_codec
        if len(self.speaker_codecs)==0 and self.speaker_allowed:
            assert has_gst
            self.speaker_codecs = get_sound_codecs(True, False)
            self.speaker_allowed = len(self.speaker_codecs)>0
        self.microphone_codecs = opts.microphone_codec
        if len(self.microphone_codecs)==0 and self.microphone_allowed:
            assert has_gst
            self.microphone_codecs = get_sound_codecs(False, False)
            self.microphone_allowed = len(self.microphone_codecs)>0

        self.readonly = opts.readonly
        self.windows_enabled = opts.windows
        self.pings = opts.pings

        self.client_supports_notifications = opts.notifications
        self.client_supports_system_tray = opts.system_tray and SYSTEM_TRAY_SUPPORTED
        self.client_supports_clipboard = opts.clipboard
        self.client_supports_cursors = opts.cursors
        self.client_supports_bell = opts.bell
        self.client_supports_sharing = opts.sharing
        self.client_supports_remote_logging = opts.remote_logging

        #until we add the ability to choose decoders, use all of them:
        #(and default to non grahics card csc modules if not specified)
        vh = getVideoHelper()
        vh.set_modules(video_decoders=opts.video_decoders, csc_modules=opts.csc_modules or NO_GFX_CSC_OPTIONS)
        vh.init()


    def init_ui(self, opts, extra_args=[]):
        """ initialize user interface """
        self.init_opengl(opts.opengl)

        if not self.readonly:
            self.keyboard_helper = self.make_keyboard_helper(opts.keyboard_sync, opts.key_shortcut)

        tray_icon_filename = opts.tray_icon
        if opts.tray:
            self.menu_helper = self.make_tray_menu_helper()
            self.tray = self.setup_xpra_tray(opts.tray_icon)
            if self.tray:
                tray_icon_filename = self.tray.get_tray_icon_filename(tray_icon_filename)
                #keep tray widget hidden until:
                self.tray.hide()
                if opts.delay_tray:
                    def show_tray(*args):
                        traylog("first ui received, showing tray %s", self.tray)
                        self.tray.show()
                    self.connect("first-ui-received", show_tray)
                else:
                    #show when the main loop is running:
                    self.idle_add(self.tray.show)

        if self.client_supports_notifications:
            self.notifier = self.make_notifier()
            traylog("using notifier=%s", self.notifier)
            self.client_supports_notifications = self.notifier is not None

        #audio tagging:
        if tray_icon_filename and os.path.exists(tray_icon_filename):
            try:
                from xpra.sound.pulseaudio_util import add_audio_tagging_env
                add_audio_tagging_env(tray_icon_filename)
            except ImportError as e:
                log("failed to set pulseaudio audio tagging: %s", e)

        if ClientExtras is not None:
            self.client_extras = ClientExtras(self, opts)

        if opts.border:
            self.parse_border(opts.border, extra_args)

        #draw thread:
        self._draw_queue = Queue()
        self._draw_thread = make_daemon_thread(self._draw_thread_loop, "draw")

    def setup_connection(self, conn):
        XpraClientBase.setup_connection(self, conn)
        if self.supports_mmap:
            self.init_mmap(self.mmap_group, conn.filename)


    def parse_border(self, border_str, extra_args):
        #not implemented here (see gtk2 client)
        pass


    def run(self):
        XpraClientBase.run(self)    #start network threads
        self._draw_thread.start()
        self.send_hello()


    def quit(self, exit_code=0):
        raise Exception("override me!")

    def cleanup(self):
        log("UIXpraClient.cleanup()")
        XpraClientBase.cleanup(self)
        for x in (self.keyboard_helper, self.clipboard_helper, self.tray, self.notifier, self.menu_helper, self.client_extras, getVideoHelper()):
            if x is None:
                continue
            log("UIXpraClient.cleanup() calling %s.cleanup()", type(x))
            try:
                x.cleanup()
            except:
                log.error("error on %s cleanup", type(x), exc_info=True)
        #the protocol has been closed, it is now safe to close all the windows:
        #(cleaner and needed when we run embedded in the client launcher)
        self.destroy_all_windows()
        self.clean_mmap()
        if self.sound_source:
            self.stop_sending_sound()
        if self.sound_sink:
            self.stop_receiving_sound()
        reaper_cleanup()
        log("UIXpraClient.cleanup() done")

    def destroy_all_windows(self):
        for wid, window in self._id_to_window.items():
            try:
                windowlog("destroy_all_windows() destroying %s / %s", wid, window)
                self.destroy_window(wid, window)
            except:
                pass
        self._id_to_window = {}
        self._window_to_id = {}


    def suspend(self):
        log.info("system is suspending")
        self._suspended_at = time.time()
        #tell the server to slow down refresh for all the windows:
        self.control_refresh(-1, True, False)

    def resume(self):
        elapsed = 0
        if self._suspended_at>0:
            elapsed = time.time()-self._suspended_at
            self._suspended_at = 0
        delta = datetime.timedelta(seconds=int(elapsed))
        log.info("system resumed, was suspended for %s", delta)
        #this will reset the refresh rate too:
        self.send_refresh_all()


    def control_refresh(self, wid, suspend_resume, refresh, quality=100, options={}, client_properties={}):
        packet = ["buffer-refresh", wid, 0, quality]
        if self.window_refresh_config:
            options["refresh-now"] = bool(refresh)
            if suspend_resume is True:
                options["batch"] = {"reset"     : True,
                                    "delay"     : 1000,
                                    "locked"    : True,
                                    "always"    : True}
            elif suspend_resume is False:
                options["batch"] = {"reset"     : True}
            else:
                pass    #batch unchanged
            log("sending buffer refresh: options=%s, client_properties=%s", options, client_properties)
            packet.append(options)
            packet.append(client_properties)
        elif not refresh:
            #we don't really want a refresh, we want to use the "window_refresh_config" feature
            #but since the server doesn't support it, we can't do anything
            return
        self.send(*packet)

    def send_refresh(self, wid):
        packet = ["buffer-refresh", wid, 0, 100]
        if self.window_refresh_config:
            #explicit refresh (should be assumed True anyway),
            #also force a reset of batch configs:
            packet.append({
                           "refresh-now"    : True,
                           "batch"          : {"reset" : True}
                           })
            packet.append({})   #no client_properties
        self.send(*packet)

    def send_refresh_all(self):
        log("Automatic refresh for all windows ")
        self.send_refresh(-1)


    def show_session_info(self, *args):
        log.warn("show_session_info() is not implemented in %s", self)

    def show_bug_report(self, *args):
        log.warn("show_bug_report() is not implemented in %s", self)


    def get_encodings(self):
        """
            Unlike get_core_encodings(), this method returns "rgb" for both "rgb24" and "rgb32".
            That's because although we may support both, the encoding chosen is plain "rgb",
            and the actual encoding used ("rgb24" or "rgb32") depends on the window's bit depth.
            ("rgb32" if there is an alpha channel, and if the client supports it)
        """
        cenc = self.get_core_encodings()
        if ("rgb24" in cenc or "rgb32" in cenc) and "rgb" not in cenc:
            cenc.append("rgb")
        return [x for x in PREFERED_ENCODING_ORDER if x in cenc and x not in ("rgb32", "rgb24")]

    def get_core_encodings(self):
        if self.core_encodings is None:
            self.core_encodings = self.do_get_core_encodings()
        return self.core_encodings

    def do_get_core_encodings(self):
        """
            This method returns the actual encodings supported.
            ie: ["rgb24", "vp8", "webp", "png", "png/L", "png/P", "jpeg", "h264", "vpx"]
            It is often overriden in the actual client class implementations,
            where extra encodings can be added (generally just 'rgb32' for transparency),
            or removed if the toolkit implementation class is more limited.
        """
        #we always support rgb24:
        core_encodings = ["rgb24"]
        #PIL:
        core_encodings += get_PIL_decodings(get_codec("PIL"))
        if (has_codec("dec_webp")) and "webp" not in core_encodings:
            core_encodings.append("webp")
        #we enable all the video decoders we know about,
        #what will actually get used by the server will still depend on the csc modes supported
        video_decodings = getVideoHelper().get_decodings()
        log("video_decodings=%s", video_decodings)
        for encoding in video_decodings:
            if encoding not in core_encodings:
                core_encodings.append(encoding)
        #remove duplicates and use prefered encoding order:
        core_encodings = [x for x in PREFERED_ENCODING_ORDER if x in set(core_encodings) and x in self.allowed_encodings]
        log("do_get_core_encodings()=%s", core_encodings)
        return core_encodings


    def get_supported_window_layouts(self):
        return  []

    def make_keyboard_helper(self, keyboard_sync, key_shortcuts):
        return KeyboardHelper(self.send, keyboard_sync, key_shortcuts)

    def make_clipboard_helper(self):
        raise Exception("override me!")


    def make_notifier(self):
        nc = self.get_notifier_classes()
        traylog("make_notifier() notifier classes: %s", nc)
        return self.make_instance(nc)

    def get_notifier_classes(self):
        #subclasses will generally add their toolkit specific variants
        #by overriding this method
        #use the native ones first:
        return get_native_notifier_classes()


    def make_system_tray(self, *args):
        """ tray used for application systray forwarding """
        tc = self.get_system_tray_classes()
        traylog("make_system_tray%s system tray classes=%s", args, tc)
        return self.make_instance(tc, *args)

    def get_system_tray_classes(self):
        #subclasses may add their toolkit specific variants, if any
        #by overriding this method
        #use the native ones first:
        return get_native_system_tray_classes()


    def make_tray(self, *args):
        """ tray used by our own application """
        tc = self.get_tray_classes()
        traylog("make_tray%s tray classes=%s", args, tc)
        return self.make_instance(tc, *args)

    def get_tray_classes(self):
        #subclasses may add their toolkit specific variants, if any
        #by overriding this method
        #use the native ones first:
        return get_native_tray_classes()


    def make_tray_menu_helper(self):
        """ menu helper class used by our tray (make_tray / setup_xpra_tray) """
        mhc = self.get_tray_menu_helper_classes()
        traylog("make_tray_menu_helper() tray menu helper classes: %s", mhc)
        return self.make_instance(mhc, self)

    def get_tray_menu_helper_classes(self):
        #subclasses may add their toolkit specific variants, if any
        #by overriding this method
        #use the native ones first:
        return get_native_tray_menu_helper_classes()


    def make_instance(self, class_options, *args):
        log("make_instance%s", [class_options]+list(args))
        for c in class_options:
            try:
                v = c(*args)
                log("make_instance(..) %s()=%s", c, v)
                if v:
                    return v
            except:
                log.error("make_instance%s failed to instantiate %s", class_options+list(args), c, exc_info=True)
        return None


    def show_menu(self, *args):
        if self.menu_helper:
            self.menu_helper.activate()

    def setup_xpra_tray(self, tray_icon_filename):
        tray = None
        #this is our own tray
        def xpra_tray_click(button, pressed, time=0):
            traylog("xpra_tray_click(%s, %s)", button, pressed)
            if button==1 and pressed:
                self.menu_helper.activate()
            elif button==3 and not pressed:
                self.menu_helper.popup(button, time)
        def xpra_tray_mouseover(*args):
            traylog("xpra_tray_mouseover(%s)", args)
        def xpra_tray_exit(*args):
            traylog("xpra_tray_exit(%s)", args)
            self.disconnect_and_quit(0, CLIENT_EXIT)
        def xpra_tray_geometry(*args):
            if tray:
                traylog("xpra_tray_geometry%s geometry=%s", args, tray.get_geometry())
        menu = None
        if self.menu_helper:
            menu = self.menu_helper.build()
        tray = self.make_tray(menu, self.get_tray_title(), tray_icon_filename, xpra_tray_geometry, xpra_tray_click, xpra_tray_mouseover, xpra_tray_exit)
        traylog("setup_xpra_tray(%s)=%s", tray_icon_filename, tray)
        return tray

    def get_tray_title(self):
        t = []
        if self.session_name:
            t.append(self.session_name)
        if self._protocol and self._protocol._conn:
            t.append(self._protocol._conn.target)
        if len(t)==0:
            t.insert(0, "Xpra")
        v = "\n".join(t)
        traylog("get_tray_title()=%s", nonl(v))
        return v

    def setup_system_tray(self, client, wid, w, h, title):
        tray_widget = None
        #this is a tray forwarded for a remote application
        def tray_click(button, pressed, time=0):
            tray = self._id_to_window.get(wid)
            traylog("tray_click(%s, %s, %s) tray=%s", button, pressed, time, tray)
            if tray:
                x, y = self.get_mouse_position()
                modifiers = self.get_current_modifiers()
                self.send_positional(["button-action", wid, button, pressed, (x, y), modifiers])
                tray.reconfigure()
        def tray_mouseover(x, y):
            tray = self._id_to_window.get(wid)
            traylog("tray_mouseover(%s, %s) tray=%s", x, y, tray)
            if tray:
                pointer = x, y
                modifiers = self.get_current_modifiers()
                buttons = []
                self.send_mouse_position(["pointer-position", wid, pointer, modifiers, buttons])
        def do_tray_geometry(*args):
            #tell the "ClientTray" where it now lives
            #which should also update the location on the server if it has changed
            tray = self._id_to_window.get(wid)
            if tray_widget:
                geom = tray_widget.get_geometry()
            else:
                geom = None
            traylog("tray_geometry(%s) widget=%s, geometry=%s tray=%s", args, tray_widget, geom, tray)
            if tray and geom:
                tray.move_resize(*geom)
        def tray_geometry(*args):
            #the tray widget may still be None if we haven't returned from make_system_tray yet,
            #in which case we will check the geometry a little bit later:
            if tray_widget:
                do_tray_geometry(*args)
            else:
                self.idle_add(do_tray_geometry, *args)
        def tray_exit(*args):
            traylog("tray_exit(%s)", args)
        tray_widget = self.make_system_tray(None, title, None, tray_geometry, tray_click, tray_mouseover, tray_exit)
        traylog("setup_system_tray%s tray_widget=%s", (client, wid, w, h, title), tray_widget)
        assert tray_widget, "could not instantiate a system tray for tray id %s" % wid
        tray_widget.show()
        return ClientTray(client, wid, w, h, tray_widget, self.mmap_enabled, self.mmap)


    def desktops_changed(self, *args):
        workspacelog("desktops_changed%s", args)
        self.screen_size_changed(*args)

    def workspace_changed(self, *args):
        workspacelog("workspace_changed%s", args)
        for win in self._id_to_window.values():
            win.workspace_changed()

    def screen_size_changed(self, *args):
        screenlog("screen_size_changed(%s) pending=%s", args, self.screen_size_change_pending)
        if self.screen_size_change_pending:
            return
        def update_screen_size():
            self.screen_size_change_pending = False
            root_w, root_h = self.get_root_size()
            ss = self.get_screen_sizes()
            ndesktops = get_number_of_desktops()
            desktop_names = get_desktop_names()
            screenlog("update_screen_size() sizes=%s, %s desktops: %s", ss, ndesktops, desktop_names)
            screen_settings = (root_w, root_h, ss, ndesktops, desktop_names)
            screenlog("update_screen_size()     new settings=%s", screen_settings)
            screenlog("update_screen_size() current settings=%s", self._last_screen_settings)
            if self._last_screen_settings==screen_settings:
                log("screen size unchanged")
                return
            screenlog.info("sending updated screen size to server: %sx%s with %s screens", root_w, root_h, len(ss))
            log_screen_sizes(root_w, root_h, ss)
            self.send("desktop_size", *screen_settings)
            self._last_screen_settings = screen_settings
            #update the max packet size (may have gone up):
            self.set_max_packet_size()
            if MONITOR_CHANGE_REINIT and MONITOR_CHANGE_REINIT=="0":
                return
            if MONITOR_CHANGE_REINIT or REINIT_WINDOWS:
                screenlog("screen size change: will reinit the windows")
                for window in self._id_to_window.values():
                    if not window.is_OR() or window.is_tray():
                        window.send_configure()

        #update via timer so the data is more likely to be final (up to date) when we query it,
        #some properties (like _NET_WORKAREA for X11 clients via xposix "ClientExtras") may
        #trigger multiple calls to screen_size_changed, delayed by some amount
        #(sometimes up to 1s..)
        self.screen_size_change_pending = True
        delay = 1000
        #if we are suspending, wait longer:
        #(better chance that the suspend-resume cycle will have completed)
        if self._suspended_at>0 and self._suspended_at-time.time()<5*1000:
            delay = 5*1000
        self.timeout_add(delay, update_screen_size)

    def get_screen_sizes(self):
        raise Exception("override me!")

    def get_root_size(self):
        raise Exception("override me!")

    def set_windows_cursor(self, client_windows, new_cursor):
        raise Exception("override me!")

    def get_mouse_position(self):
        raise Exception("override me!")

    def get_current_modifiers(self):
        raise Exception("override me!")

    def window_bell(self, window, device, percent, pitch, duration, bell_class, bell_id, bell_name):
        raise Exception("override me!")


    def init_mmap(self, mmap_group, socket_filename):
        log("init_mmap(%s, %s)", mmap_group, socket_filename)
        from xpra.os_util import get_int_uuid
        from xpra.net.mmap_pipe import init_client_mmap
        #calculate size:
        root_w, root_h = self.get_root_size()
        #at least 128MB, or 8 fullscreen RGBX frames:
        mmap_size = max(128*1024*1024, root_w*root_h*4*8)
        mmap_size = min(1024*1024*1024, mmap_size)
        self.mmap_token = get_int_uuid()
        self.mmap_enabled, self.mmap, self.mmap_size, self.mmap_tempfile, self.mmap_filename = \
            init_client_mmap(self.mmap_token, mmap_group, socket_filename, mmap_size)

    def clean_mmap(self):
        log("XpraClient.clean_mmap() mmap_filename=%s", self.mmap_filename)
        if self.mmap_tempfile:
            try:
                self.mmap_tempfile.close()
            except Exception as e:
                log("clean_mmap error closing file %s: %s", self.mmap_tempfile, e)
            self.mmap_tempfile = None
        #this should be redundant: closing the tempfile should get it deleted
        if self.mmap_filename and os.path.exists(self.mmap_filename):
            os.unlink(self.mmap_filename)
            self.mmap_filename = None


    def init_opengl(self, enable_opengl):
        self.opengl_enabled = False
        self.client_supports_opengl = False
        self.opengl_props = {"info" : "not supported"}


    def send_button(self, wid, button, pressed, pointer, modifiers, buttons):
        def send_button(state):
            self.send_positional(["button-action", wid,
                                              button, state,
                                              pointer, modifiers, buttons])
        pressed_state = self._button_state.get(button, False)
        if PYTHON3 and WIN32 and pressed_state==pressed:
            mouselog("button action: unchanged state, ignoring event")
            return
        self._button_state[button] = pressed
        send_button(pressed)


    def get_keymap_properties(self):
        props = self.keyboard_helper.get_keymap_properties()
        props["modifiers"] = self.get_current_modifiers()
        return  props

    def handle_key_action(self, window, key_event):
        if self.readonly or self.keyboard_helper is None:
            return
        wid = self._window_to_id[window]
        keylog("handle_key_action(%s, %s) wid=%s", window, key_event, wid)
        self.keyboard_helper.handle_key_action(window, wid, key_event)

    def mask_to_names(self, mask):
        if self.keyboard_helper is None:
            return []
        return self.keyboard_helper.mask_to_names(mask)


    def send_start_command(self, name, command, ignore):
        log("send_start_command(%s, %s, %s)", name, command, ignore)
        self.send("start-command", name, command, ignore)


    def send_focus(self, wid):
        focuslog("send_focus(%s)", wid)
        self.send("focus", wid, self.get_current_modifiers())

    def update_focus(self, wid, gotit):
        focuslog("update_focus(%s, %s) focused=%s, grabbed=%s", wid, gotit, self._focused, self._window_with_grab)
        if gotit and self._focused is not wid:
            if self.keyboard_helper:
                self.keyboard_helper.clear_repeat()
            self.send_focus(wid)
            self._focused = wid
        if not gotit:
            if self._window_with_grab:
                self.window_ungrab()
                self.do_force_ungrab(self._window_with_grab)
                self._window_with_grab = None
            if wid and self._focused and self._focused!=wid:
                #if this window lost focus, it must have had it!
                #(catch up - makes things like OR windows work:
                # their parent receives the focus-out event)
                focuslog("window %s lost a focus it did not have!? (simulating focus before losing it)", wid)
                self.send_focus(wid)
            if self.keyboard_helper:
                self.keyboard_helper.clear_repeat()
            if self._focused:
                #send the lost-focus via a timer and re-check it
                #(this allows a new window to gain focus without having to do a reset_focus)
                def send_lost_focus():
                    #check that a new window has not gained focus since:
                    if self._focused is None:
                        self.send_focus(0)
                self.timeout_add(20, send_lost_focus)
                self._focused = None

    def do_force_ungrab(self, wid):
        grablog("do_force_ungrab(%s) server supports force ungrab: %s", wid, self.force_ungrab)
        if self.force_ungrab:
            #ungrab via dedicated server packet:
            self.send_force_ungrab(wid)
            return
        #fallback for older servers: try to find a key to press:
        kh = self.keyboard_helper
        if not kh:
            if not self.kh_warning:
                self.kh_warning = True
                grablog.warn("no keyboard support, cannot simulate keypress to lose grab!")
            return
        #xkbmap_keycodes is a list of: (keyval, name, keycode, group, level)
        ungrab_keys = [x for x in kh.xkbmap_keycodes if x[1]==UNGRAB_KEY]
        if len(ungrab_keys)==0:
            if not self.kh_warning:
                self.kh_warning = True
                grablog.warn("ungrab key %s not found, cannot simulate keypress to lose grab!", UNGRAB_KEY)
            return
        #ungrab_keys.append((65307, "Escape", 27, 0, 0))     #ugly hardcoded default value
        ungrab_key = ungrab_keys[0]
        grablog("lost focus whilst window %s has grab, simulating keypress: %s", wid, ungrab_key)
        key_event = AdHocStruct()
        key_event.keyname = ungrab_key[1]
        key_event.pressed = True
        key_event.modifiers = []
        key_event.keyval = ungrab_key[0]
        keycode = ungrab_key[2]
        try:
            key_event.string = chr(keycode)
        except:
            key_event.string = str(keycode)
        key_event.keycode = keycode
        key_event.group = 0
        #press:
        kh.send_key_action(wid, key_event)
        #unpress:
        key_event.pressed = False
        kh.send_key_action(wid, key_event)

    def _process_pointer_grab(self, packet):
        wid = packet[1]
        window = self._id_to_window.get(wid)
        grablog("grabbing %s: %s", wid, window)
        if window:
            self.window_grab(window)
            self._window_with_grab = wid

    def window_grab(self, window):
        #subclasses should implement this method
        pass

    def _process_pointer_ungrab(self, packet):
        wid = packet[1]
        window = self._id_to_window.get(wid)
        grablog("ungrabbing %s: %s", wid, window)
        self.window_ungrab()
        self._window_with_grab = None

    def window_ungrab(self):
        #subclasses should implement this method
        pass


    def make_hello(self):
        capabilities = XpraClientBase.make_hello(self)
        if self.readonly:
            #don't bother sending keyboard info, as it won't be used
            capabilities["keyboard"] = False
        else:
            for k,v in self.get_keymap_properties().items():
                capabilities[k] = v
            #show the user a summary of what we have detected:
            kb_info = {}
            xkbq = capabilities.get("xkbmap_query")
            xkbqs = capabilities.get("xkbmap_query_struct")
            if xkbqs or xkbq:
                if not xkbqs:
                    #parse query into a dict
                    from xpra.keyboard.layouts import parse_xkbmap_query
                    xkbqs = parse_xkbmap_query(xkbq)
                for x in ["rules", "model", "layout"]:
                    v = xkbqs.get(x)
                    if v:
                        kb_info[x] = v
            if self.keyboard_helper.xkbmap_layout:
                kb_info["layout"] = self.keyboard_helper.xkbmap_layout
            if len(kb_info)==0:
                log.info("using default keyboard settings")
            else:
                log.info("detected keyboard: %s", ", ".join(["%s=%s" % (std(k), std(v)) for k,v in kb_info.items()]))

        capabilities["modifiers"] = self.get_current_modifiers()
        root_w, root_h = self.get_root_size()
        capabilities["desktop_size"] = [root_w, root_h]
        ndesktops = get_number_of_desktops()
        capabilities["desktops"] = ndesktops
        desktop_names = get_desktop_names()
        capabilities["desktop.names"] = desktop_names
        capabilities["desktop_size"] = [root_w, root_h]
        ss = self.get_screen_sizes()
        log.info("desktop size is %sx%s with %s screen(s):", root_w, root_h, len(ss))
        log_screen_sizes(root_w, root_h, ss)
        capabilities["screen_sizes"] = ss
        self._last_screen_settings = (root_w, root_h, ss, ndesktops, desktop_names)
        if self.keyboard_helper:
            key_repeat = self.keyboard_helper.keyboard.get_keyboard_repeat()
            if key_repeat:
                delay_ms,interval_ms = key_repeat
                capabilities["key_repeat"] = (delay_ms,interval_ms)
            else:
                #cannot do keyboard_sync without a key repeat value!
                #(maybe we could just choose one?)
                self.keyboard_helper.keyboard_sync = False
            capabilities["keyboard_sync"] = self.keyboard_helper.keyboard_sync
            log("keyboard capabilities: %s", [(k,v) for k,v in capabilities.items() if k.startswith("key")])
        if self.mmap_enabled:
            capabilities["mmap_file"] = self.mmap_filename
            capabilities["mmap_token"] = self.mmap_token
        #don't try to find the server uuid if this platform cannot run servers..
        #(doing so causes lockups on win32 and startup errors on osx)
        if MMAP_SUPPORTED:
            #we may be running inside another server!
            try:
                from xpra.server.server_uuid import get_uuid
                capabilities["server_uuid"] = get_uuid() or ""
            except:
                pass
        capabilities.update({
            "wants_events"              : True,
            "randr_notify"              : True,
            "compressible_cursors"      : True,
            "clipboard"                 : self.client_supports_clipboard,
            "clipboard.notifications"   : self.client_supports_clipboard,
            "clipboard.selections"      : CLIPBOARDS,
            #buggy osx clipboards:
            "clipboard.want_targets"    : CLIPBOARD_WANT_TARGETS,
            #buggy osx and win32 clipboards:
            "clipboard.greedy"          : CLIPBOARD_GREEDY,
            "clipboard.set_enabled"     : True,
            "notifications"             : self.client_supports_notifications,
            "cursors"                   : self.client_supports_cursors,
            "bell"                      : self.client_supports_bell,
            "vrefresh"                  : get_vrefresh(),
            "double_click.time"         : get_double_click_time(),
            "double_click.distance"     : get_double_click_distance(),
            "sound.server_driven"       : True,
            "sound.ogg-latency-fix"     : True,
            "encoding.scaling.control"  : self.scaling,
            "encoding.client_options"   : True,
            "encoding_client_options"   : True,
            "encoding.csc_atoms"        : True,
            #TODO: check for csc support (swscale only?)
            "encoding.video_subregion"  : True,
            "encoding.video_reinit"     : True,
            "encoding.video_scaling"    : True,
            #separate plane is only supported by avcodec2:
            "encoding.video_separateplane"  : get_codec("dec_avcodec") is None and get_codec("dec_avcodec2") is not None,
            "encoding.webp_leaks"       : False,
            "encoding.transparency"     : self.has_transparency(),
            "rgb24zlib"                 : True,
            "encoding.rgb24zlib"        : True,
            "named_cursors"             : False,
            "share"                     : self.client_supports_sharing,
            "auto_refresh_delay"        : int(self.auto_refresh_delay*1000),
            "windows"                   : self.windows_enabled,
            "window.raise"              : True,
            #only implemented on posix with the gtk client:
            "window.initiate-moveresize": False,
            "show-desktop"              : True,
            "raw_window_icons"          : True,
            "system_tray"               : self.client_supports_system_tray,
            "xsettings-tuple"           : True,
            "generic_window_types"      : True,
            "server-window-move-resize" : True,
            "server-window-resize"      : True,
            "window.resize-counter"     : True,
            "notify-startup-complete"   : True,
            "generic-rgb-encodings"     : True,
            "encodings"                 : self.get_encodings(),
            "encodings.core"            : self.get_core_encodings(),
            })
        if self.dpi>0:
            #command line (or config file) override supplied:
            capabilities["dpi"] = self.dpi
        else:
            #use platform detection code:
            capabilities.update({
                                 "dpi"      : get_dpi(),
                                 "dpi.x"    : get_xdpi(),
                                 "dpi.y"    : get_ydpi(),
                                 })
        capabilities["antialias"] = get_antialias_info()
        #generic rgb compression flags:
        for x in compression.ALL_COMPRESSORS:
            capabilities["encoding.rgb_%s" % x] = x in compression.get_enabled_compressors()

        control_commands = ["show_session_info", "show_bug_report", "debug"]
        for x in compression.get_enabled_compressors():
            control_commands.append("enable_"+x)
        for x in packet_encoding.get_enabled_encoders():
            control_commands.append("enable_"+x)
        capabilities["control_commands"] = control_commands
        log("control_commands=%s", control_commands)
        for k,v in codec_versions.items():
            capabilities["encoding.%s.version" % k] = v
        if self.encoding:
            capabilities["encoding"] = self.encoding
        if self.quality>0:
            capabilities.update({
                         "jpeg"             : self.quality,
                         "quality"          : self.quality,
                         "encoding.quality" : self.quality
                         })
        if self.min_quality>0:
            capabilities["encoding.min-quality"] = self.min_quality
        if self.speed>=0:
            capabilities["speed"] = self.speed
            capabilities["encoding.speed"] = self.speed
        if self.min_speed>=0:
            capabilities["encoding.min-speed"] = self.min_speed

        #note: this is mostly for old servers, with newer ones we send the properties
        #again (and more accurately) once the window is instantiated.
        #figure out the CSC modes supported:
        #these are the RGB modes we want (the ones we can paint with):
        rgb_formats = ["RGB", "RGBX"]
        if not sys.platform.startswith("win") and not sys.platform.startswith("darwin"):
            #only win32 and osx cannot handle transparency
            rgb_formats.append("RGBA")
        capabilities["encodings.rgb_formats"] = rgb_formats
        #figure out which CSC modes (usually YUV) can give us those RGB modes:
        full_csc_modes = getVideoHelper().get_server_full_csc_modes_for_rgb(*rgb_formats)
        log("supported full csc_modes=%s", full_csc_modes)
        capabilities["encoding.full_csc_modes"] = full_csc_modes
        #for older servers (remove per-encoding and hope for the best..):
        csc_modes = []
        for modes in full_csc_modes.values():
            csc_modes += modes
        capabilities["encoding.csc_modes"] = list(set(csc_modes))

        log("encoding capabilities: %s", [(k,v) for k,v in capabilities.items() if k.startswith("encoding")])
        capabilities["encoding.uses_swscale"] = True
        if "h264" in self.get_core_encodings():
            # some profile options: "baseline", "main", "high", "high10", ...
            # set the default to "high10" for I420/YUV420P
            # as the python client always supports all the profiles
            # whereas on the server side, the default is baseline to accomodate less capable clients.
            # I422/YUV422P requires high422, and
            # I444/YUV444P requires high444,
            # so we don't bother specifying anything for those two.
            for old_csc_name, csc_name, default_profile in (
                        ("I420", "YUV420P", "high10"),
                        ("I422", "YUV422P", ""),
                        ("I444", "YUV444P", "")):
                profile = default_profile
                #try with the old prefix (X264) as well as the more correct one (H264):
                for H264_NAME in ("X264", "H264"):
                    profile = os.environ.get("XPRA_%s_%s_PROFILE" % (H264_NAME, old_csc_name), profile)
                    profile = os.environ.get("XPRA_%s_%s_PROFILE" % (H264_NAME, csc_name), profile)
                if profile:
                    #send as both old and new names:
                    for h264_name in ("x264", "h264"):
                        capabilities["encoding.%s.%s.profile" % (h264_name, old_csc_name)] = profile
                        capabilities["encoding.%s.%s.profile" % (h264_name, csc_name)] = profile
            log("x264 encoding options: %s", str([(k,v) for k,v in capabilities.items() if k.startswith("encoding.x264.")]))
        iq = max(self.min_quality, self.quality)
        if iq<0:
            iq = 70
        capabilities["encoding.initial_quality"] = iq
        sound_caps = {}
        try:
            import xpra.sound
            log("loaded %s", xpra.sound)
            try:
                from xpra.sound.gstreamer_util import has_gst, get_info as get_gst_info
                sound_caps.update(get_gst_info(receive=self.speaker_allowed, send=self.microphone_allowed,
                                     receive_codecs=self.speaker_codecs, send_codecs=self.microphone_codecs))
            except Exception as e:
                log.error("failed to setup sound: %s", e, exc_info=True)
                self.speaker_allowed = False
                self.microphone_allowed = False
                has_gst = False
            if has_gst:
                try:
                    from xpra.sound.pulseaudio_util import get_info as get_pa_info
                    sound_caps.update(get_pa_info())
                    sound_caps.update(get_gst_info(receive=self.speaker_allowed, send=self.microphone_allowed,
                                         receive_codecs=self.speaker_codecs, send_codecs=self.microphone_codecs))
                except Exception:
                    pass
            from xpra.util import updict
            updict(capabilities, "sound", sound_caps)
            soundlog("sound capabilities: %s", sound_caps)
        except ImportError as e:
            soundlog.warn("sound support not available: %s", e)
        #batch options:
        for bprop in ("always", "min_delay", "max_delay", "delay", "max_events", "max_pixels", "time_unit"):
            evalue = os.environ.get("XPRA_BATCH_%s" % bprop.upper())
            if evalue:
                try:
                    capabilities["batch.%s" % bprop] = int(evalue)
                except:
                    log.error("invalid environment value for %s: %s", bprop, evalue)
        log("batch props=%s", [("%s=%s" % (k,v)) for k,v in capabilities.items() if k.startswith("batch.")])
        return capabilities

    def has_transparency(self):
        return False


    def server_ok(self):
        return self._server_ok

    def check_server_echo(self, ping_sent_time):
        if self._protocol is None:
            #no longer connected!
            return False
        last = self._server_ok
        if FAKE_BROKEN_CONNECTION>0:
            self._server_ok = (int(time.time()) % FAKE_BROKEN_CONNECTION) <= (FAKE_BROKEN_CONNECTION//2)
        else:
            self._server_ok = not FAKE_BROKEN_CONNECTION and self.last_ping_echoed_time>=ping_sent_time
        log("check_server_echo(%s) last=%s, server_ok=%s", ping_sent_time, last, self._server_ok)
        if last!=self._server_ok and not self._server_ok:
            log.info("server is not responding, drawing spinners over the windows")
            def timer_redraw():
                if self._protocol is None:
                    #no longer connected!
                    return False
                ok = self.server_ok()
                self.redraw_spinners()
                if ok:
                    log.info("server is OK again")
                return not ok
            self.idle_add(self.redraw_spinners)
            self.timeout_add(250, timer_redraw)
        return False

    def redraw_spinners(self):
        #draws spinner on top of the window, or not (plain repaint)
        #depending on whether the server is ok or not
        ok = self.server_ok()
        for w in self._id_to_window.values():
            if not w.is_tray():
                w.spinner(ok)

    def check_echo_timeout(self, ping_time):
        log("check_echo_timeout(%s) last_ping_echoed_time=%s", ping_time, self.last_ping_echoed_time)
        if self.last_ping_echoed_time<ping_time:
            #no point trying to use disconnect_and_quit() to tell the server here..
            self.warn_and_quit(EXIT_TIMEOUT, "server ping timeout - waited %s seconds without a response" % PING_TIMEOUT)

    def send_ping(self):
        now_ms = int(1000.0*time.time())
        self.send("ping", now_ms)
        self.timeout_add(PING_TIMEOUT*1000, self.check_echo_timeout, now_ms)
        wait = 2.0
        if len(self.server_ping_latency)>0:
            l = [x for _,x in list(self.server_ping_latency)]
            avg = sum(l) / len(l)
            wait = min(5, 1.0+avg*2.0)
            log("average server latency=%.1f, using max wait %.2fs", 1000.0*avg, wait)
        self.timeout_add(int(1000.0*wait), self.check_server_echo, now_ms)
        return True

    def _process_ping_echo(self, packet):
        echoedtime, l1, l2, l3, cl = packet[1:6]
        self.last_ping_echoed_time = echoedtime
        self.check_server_echo(0)
        server_ping_latency = time.time()-echoedtime/1000.0
        self.server_ping_latency.append((time.time(), server_ping_latency))
        self.server_load = l1, l2, l3
        if cl>=0:
            self.client_ping_latency.append((time.time(), cl/1000.0))
        log("ping echo server load=%s, measured client latency=%sms", self.server_load, cl)

    def _process_ping(self, packet):
        echotime = packet[1]
        l1,l2,l3 = 0,0,0
        if os.name=="posix":
            try:
                (fl1, fl2, fl3) = os.getloadavg()
                l1,l2,l3 = int(fl1*1000), int(fl2*1000), int(fl3*1000)
            except (OSError, AttributeError):
                pass
        sl = -1
        if len(self.server_ping_latency)>0:
            _, sl = self.server_ping_latency[-1]
        self.send("ping_echo", echotime, l1, l2, l3, int(1000.0*sl))


    def _process_server_event(self, packet):
        log(u": ".join((str(x) for x in packet[1:])))


    def _process_info_response(self, packet):
        self.info_request_pending = False
        self.server_last_info = packet[1]
        log("info-response: %s", packet)

    def send_info_request(self):
        assert self.server_info_request
        if not self.info_request_pending:
            self.info_request_pending = True
            self.send("info-request", [self.uuid], list(self._id_to_window.keys()))


    def send_quality(self):
        q = self.quality
        assert q==-1 or (q>=0 and q<=100), "invalid quality: %s" % q
        if self.change_quality:
            self.send("quality", q)

    def send_min_quality(self):
        q = self.min_quality
        assert q==-1 or (q>=0 and q<=100), "invalid quality: %s" % q
        if self.change_min_quality:
            #v0.8 onwards: set min
            self.send("min-quality", q)

    def send_speed(self):
        assert self.change_speed
        s = self.speed
        assert s==-1 or (s>=0 and s<=100), "invalid speed: %s" % s
        self.send("speed", s)

    def send_min_speed(self):
        assert self.change_speed
        s = self.min_speed
        assert s==-1 or (s>=0 and s<=100), "invalid speed: %s" % s
        self.send("min-speed", s)


    def server_connection_established(self):
        if XpraClientBase.server_connection_established(self):
            #process the rest from the UI thread:
            self.idle_add(self.process_ui_capabilities)


    def parse_server_capabilities(self):
        if not XpraClientBase.parse_server_capabilities(self):
            return  False
        c = self.server_capabilities
        #enable remote logging asap:
        if self.client_supports_remote_logging and c.boolget("remote-logging"):
            log.info("enabled remote logging, see server log file for output")
            self.local_logging = set_global_logging_handler(self.remote_logging_handler)
        if not self.session_name:
            self.session_name = c.strget("session_name", "")
        set_application_name(self.session_name or "Xpra")
        self.window_unmap = c.boolget("window_unmap")
        self.window_configure_skip_geometry = c.boolget("window.configure.skip-geometry")
        self.force_ungrab = c.boolget("force_ungrab")
        self.window_refresh_config = c.boolget("window_refresh_config")
        self.suspend_resume = c.boolget("suspend-resume")
        self.server_supports_notifications = c.boolget("notifications")
        self.notifications_enabled = self.server_supports_notifications and self.client_supports_notifications
        self.server_supports_cursors = c.boolget("cursors", True)    #added in 0.5, default to True!
        self.cursors_enabled = self.server_supports_cursors and self.client_supports_cursors
        self.server_supports_bell = c.boolget("bell")          #added in 0.5, default to True!
        self.bell_enabled = self.server_supports_bell and self.client_supports_bell
        self.server_supports_clipboard = c.boolget("clipboard")
        self.server_clipboards = c.strlistget("clipboards", ALL_CLIPBOARDS)
        self.server_compressors = c.strlistget("compressors", ["zlib"])
        self.clipboard_enabled = self.client_supports_clipboard and self.server_supports_clipboard
        self.server_dbus_proxy = c.boolget("dbus_proxy")
        self.start_new_commands = c.boolget("start-new-commands")
        self.mmap_enabled = self.supports_mmap and self.mmap_enabled and c.boolget("mmap_enabled")
        if self.mmap_enabled:
            mmap_token = c.intget("mmap_token")
            from xpra.net.mmap_pipe import read_mmap_token
            token = read_mmap_token(self.mmap)
            if token!=mmap_token:
                log.warn("mmap token verification failed!")
                log.warn("expected '%s', found '%s'", mmap_token, token)
                self.mmap_enabled = False
                self.quit(EXIT_MMAP_TOKEN_FAILURE)
                return
        server_auto_refresh_delay = c.intget("auto_refresh_delay", 0)/1000.0
        if server_auto_refresh_delay==0 and self.auto_refresh_delay>0:
            log.warn("server does not support auto-refresh!")
        self.server_encodings = c.strlistget("encodings")
        self.server_core_encodings = c.strlistget("encodings.core", self.server_encodings)
        self.server_encodings_problematic = c.strlistget("encodings.problematic", PROBLEMATIC_ENCODINGS)  #server is telling us to try to avoid those
        self.server_encodings_with_speed = c.strlistget("encodings.with_speed", ("h264",)) #old servers only supported x264
        self.server_encodings_with_quality = c.strlistget("encodings.with_quality", ("jpeg", "webp", "h264"))
        self.server_encodings_with_lossless_mode = c.strlistget("encodings.with_lossless_mode", ())
        self.change_quality = c.boolget("change-quality")
        self.change_min_quality = c.boolget("change-min-quality")
        self.change_speed = c.boolget("change-speed")
        self.change_min_speed = c.boolget("change-min-speed")
        self.xsettings_tuple = c.boolget("xsettings-tuple")
        if self.mmap_enabled:
            log.info("mmap is enabled using %sB area in %s", std_unit(self.mmap_size, unit=1024), self.mmap_filename)
        #the server will have a handle on the mmap file by now, safe to delete:
        self.clean_mmap()
        self.server_start_time = c.intget("start_time", -1)
        self.server_platform = c.strget("platform")
        self.toggle_cursors_bell_notify = c.boolget("toggle_cursors_bell_notify")
        self.toggle_keyboard_sync = c.boolget("toggle_keyboard_sync")

        self.server_display = c.strget("display")
        self.server_max_desktop_size = c.intpair("max_desktop_size")
        self.server_actual_desktop_size = c.intpair("actual_desktop_size")
        log("server actual desktop size=%s", self.server_actual_desktop_size)
        self.server_randr = c.boolget("resize_screen")
        log("server has randr: %s", self.server_randr)
        self.server_sound_sequence = c.boolget("sound_sequence")
        self.server_sound_eos_sequence = c.boolget("sound.eos-sequence")
        self.server_info_request = c.boolget("info-request")
        e = c.strget("encoding")
        if e:
            if self.encoding and e!=self.encoding:
                if self.encoding not in self.server_core_encodings:
                    log.warn("server does not support %s encoding and has switched to %s", self.encoding, e)
                else:
                    log.info("server is using %s encoding instead of %s", e, self.encoding)
            self.encoding = e
        i = " ".join(os_info(self._remote_platform, self._remote_platform_release, self._remote_platform_platform, self._remote_platform_linux_distribution))
        r = self._remote_version
        if self._remote_revision:
            r += " (r%s)" % self._remote_revision
        log.info("server: %s, Xpra version %s", i, r)
        if c.boolget("proxy"):
            proxy_hostname = c.strget("proxy.hostname")
            proxy_platform = c.strget("proxy.platform")
            proxy_release = c.strget("proxy.platform.release")
            proxy_version = c.strget("proxy.version")
            proxy_version = c.strget("proxy.build.version", proxy_version)
            msg = "via: %s proxy version %s" % (platform_name(proxy_platform, proxy_release), std(proxy_version))
            if proxy_hostname:
                msg += " on '%s'" % std(proxy_hostname)
            log.info(msg)
        return True

    def process_ui_capabilities(self):
        #figure out the maximum actual desktop size and use it to
        #calculate the maximum size of a packet (a full screen update packet)
        if self.clipboard_enabled:
            self.clipboard_helper = self.make_clipboard_helper()
            self.clipboard_enabled = self.clipboard_helper is not None
        self.set_max_packet_size()
        self.send_deflate_level()
        c = self.server_capabilities
        server_desktop_size = c.intlistget("desktop_size")
        log("server desktop size=%s", server_desktop_size)
        if not c.boolget("shadow"):
            assert server_desktop_size
            avail_w, avail_h = server_desktop_size
            root_w, root_h = self.get_root_size()
            if avail_w<root_w or avail_h<root_h:
                log.warn("Server's virtual screen is too small -- "
                         "(server: %sx%s vs. client: %sx%s)\n"
                         "You may see strange behavior.\n"
                         "Please see "
                         "http://xpra.org/trac/wiki/Xdummy#Configuration"
                         % (avail_w, avail_h, root_w, root_h))
        if self.keyboard_helper:
            modifier_keycodes = c.dictget("modifier_keycodes")
            if modifier_keycodes:
                self.keyboard_helper.set_modifier_mappings(modifier_keycodes)

        #sound:
        self.server_pulseaudio_id = c.strget("sound.pulseaudio.id")
        self.server_pulseaudio_server = c.strget("sound.pulseaudio.server")
        self.server_sound_decoders = c.strlistget("sound.decoders", [])
        self.server_sound_encoders = c.strlistget("sound.encoders", [])
        self.server_sound_receive = c.boolget("sound.receive")
        self.server_sound_send = c.boolget("sound.send")
        soundlog("pulseaudio id=%s, server=%s, sound decoders=%s, sound encoders=%s, receive=%s, send=%s",
                 self.server_pulseaudio_id, self.server_pulseaudio_server, self.server_sound_decoders,
                 self.server_sound_encoders, self.server_sound_receive, self.server_sound_send)
        if self.server_sound_send and self.speaker_enabled:
            self.start_receiving_sound()
        if self.server_sound_receive and self.microphone_enabled:
            self.start_sending_sound()

        self.key_repeat_delay, self.key_repeat_interval = c.intpair("key_repeat", (-1,-1))
        self.emit("handshake-complete")
        #ui may want to know this is now set:
        self.emit("clipboard-toggled")
        if self.server_supports_clipboard:
            #from now on, we will send a message to the server whenever the clipboard flag changes:
            self.connect("clipboard-toggled", self.send_clipboard_enabled_status)
        if self.toggle_keyboard_sync:
            self.connect("keyboard-sync-toggled", self.send_keyboard_sync_enabled_status)
        self.send_ping()
        if self.pings:
            self.timeout_add(1000, self.send_ping)
        else:
            self.timeout_add(10*1000, self.send_ping)
        if not c.boolget("notify-startup-complete"):
            #we won't get notified, so assume it is now:
            self._startup_complete()

    def _startup_complete(self, *args):
        log("all the existing windows and system trays have been received: %s items", len(self._id_to_window))
        gui_ready()
        if self.tray:
            self.tray.ready()


    def remote_logging_handler(self, log, level, msg, *args, **kwargs):
        #prevent loops (if our send call ends up firing another logging call):
        if self.in_remote_logging:
            return
        self.in_remote_logging = True
        try:
            self.send("logging", level, str(msg % args))
            exc_info = kwargs.get("exc_info")
            if exc_info:
                for x in traceback.format_tb(exc_info[2]):
                    self.send("logging", level, str(x))
        except Exception as e:
            if self.exit_code is not None:
                #errors can happen during exit, don't care
                return
            self.local_logging(log, logging.WARNING, "failed to send logging packet: %s" % e)
            self.local_logging(log, level, msg, *args, **kwargs)
        finally:
            self.in_remote_logging = False

    def dbus_call(self, wid, bus_name, path, interface, function, reply_handler=None, error_handler=None, *args):
        if not self.server_dbus_proxy:
            log.error("cannot use dbus_call: this server does not support dbus-proxying")
            return
        rpcid = self.dbus_counter.increase()
        self.dbus_filter_pending()
        req = (time.time(), bus_name, path, interface, function, reply_handler, error_handler)
        dbuslog("sending dbus request %s to server: %s", rpcid, req)
        self.dbus_pending_requests[rpcid] = req
        self.send("rpc", "dbus", rpcid, wid, bus_name, path, interface, function, args)
        self.timeout_add(5000, self.dbus_filter_pending)

    def dbus_filter_pending(self):
        """ removes timed out dbus requests """
        for k in list(self.dbus_pending_requests.keys()):
            v = self.dbus_pending_requests.get(k)
            if v is None:
                continue
            t, bn, p, i, fn, _, ecb = v
            if time.time()-t>=5:
                dbuslog.warn("dbus request: %s:%s (%s).%s has timed out", bn, p, i, fn)
                del self.dbus_pending_requests[k]
                if ecb is not None:
                    ecb("timeout")

    def _process_rpc_reply(self, packet):
        rpc_type, rpcid, success, args = packet[1:5]
        assert rpc_type=="dbus", "unsupported rpc reply type: %s" % rpc_type
        dbuslog("rpc_reply: %s", (rpc_type, rpcid, success, args))
        v = self.dbus_pending_requests.get(rpcid)
        assert v is not None, "pending dbus handler not found for id %s" % rpcid
        del self.dbus_pending_requests[rpcid]
        if success:
            ctype = "ok"
            rh = v[-2]      #ok callback
        else:
            ctype = "error"
            rh = v[-1]      #error callback
        if rh is None:
            dbuslog("no %s rpc callback defined, return values=%s", ctype, args)
            return
        dbuslog("calling %s callback %s(%s)", ctype, rh, args)
        try:
            rh(*args)
        except Exception as e:
            dbuslog.warn("error processing rpc reply handler %s(%s) : %s", rh, args, e)


    def _process_control(self, packet):
        command = packet[1]
        if command=="show_session_info":
            args = packet[2:]
            log("calling show_session_info%s on server request", args)
            self.show_session_info(*args)
        elif command=="show_bug_report":
            self.show_bug_report()
        elif command in ("enable_%s" % x for x in compression.get_enabled_compressors()):
            compressor = command.split("_")[1]
            log.info("switching to %s on server request", compressor)
            self._protocol.enable_compressor(compressor)
        elif command in ("enable_%s" % x for x in packet_encoding.get_enabled_encoders()):
            pe = command.split("_")[1]
            log.info("switching to %s on server request", pe)
            self._protocol.enable_encoder(pe)
        elif command=="name":
            assert len(args)>=3
            self.session_name = args[2]
            log.info("session name updated from server: %s", self.session_name)
            #TODO: reset tray tooltip, session info title, etc..
        elif command=="debug":
            args = packet[2:]
            if len(args)<2:
                log.warn("not enough arguments for debug control command")
                return
            log_cmd = args[0]
            if log_cmd not in ("enable", "disable"):
                log.warn("invalid debug control mode: '%s' (must be 'enable' or 'disable')", log_cmd)
                return
            categories = args[1:]
            from xpra.log import add_debug_category, add_disabled_category, enable_debug_for, disable_debug_for
            if log_cmd=="enable":
                add_debug_category(*categories)
                loggers = enable_debug_for(*categories)
            else:
                assert log_cmd=="disable"
                add_disabled_category(*categories)
                loggers = disable_debug_for(*categories)
            log.info("%sd debugging for: %s", log_cmd, loggers)
            return
        else:
            log.warn("received invalid control command from server: %s", command)


    def start_sending_sound(self):
        """ (re)start a sound source and emit client signal """
        soundlog("start_sending_sound()")
        assert self.microphone_allowed, "microphone forwarding is disabled"
        assert self.server_sound_receive, "client support for receiving sound is disabled"
        from xpra.sound.gstreamer_util import ALLOW_SOUND_LOOP
        if self._remote_machine_id and self._remote_machine_id==get_machine_id() and not ALLOW_SOUND_LOOP:
            #looks like we're on the same machine, verify it's a different user:
            if self._remote_uuid==get_user_uuid():
                log.warn("cannot start sound: identical user environment as the server (loop)")
                return

        ss = self.sound_source
        if ss:
            if ss.get_state()=="active":
                log.error("already sending sound!")
                return
            ss.start()
        elif not self.start_sound_source():
            return
        self.microphone_enabled = True
        self.emit("microphone-changed")
        soundlog("start_sending_sound() done")

    def start_sound_source(self):
        soundlog("start_sound_source()")
        assert self.sound_source is None
        def sound_source_state_changed(*args):
            self.emit("microphone-changed")
        try:
            from xpra.sound.wrapper import start_sending_sound
            ss = start_sending_sound(self.sound_source_plugin, None, 1.0, self.server_sound_decoders, self.server_pulseaudio_server, self.server_pulseaudio_id)
            if not ss:
                return False
            self.sound_source = ss
            ss.connect("new-buffer", self.new_sound_buffer)
            ss.connect("state-changed", sound_source_state_changed)
            ss.connect("new-stream", self.new_stream)
            ss.start()
            soundlog("start_sound_source() sound source %s started", ss)
            return True
        except Exception as e:
            log.error("error setting up sound: %s", e)
            return False

    def new_stream(self, sound_source, codec):
        soundlog("new_stream(%s)", codec)
        if self.sound_source!=sound_source:
            soundlog("dropping new-stream signal (current source=%s, signal source=%s)", self.sound_source, sound_source)
            return
        sound_source.codec = codec
        #tell the server this is the start:
        self.send("sound-data", sound_source.codec, "",
                  {"start-of-stream"    : True,
                   "codec"              : sound_source.codec,
                   "sequence"           : sound_source.sequence})

    def stop_sending_sound(self):
        """ stop the sound source and emit client signal """
        soundlog("stop_sending_sound() sound source=%s", self.sound_source)
        ss = self.sound_source
        self.microphone_enabled = False
        self.sound_source = None
        if ss is None:
            log.warn("stop_sending_sound: sound not started!")
            return
        #tell the server to stop:
        self.send("sound-data", ss.codec or "", "", {"end-of-stream" : True})
        ss.cleanup()
        self.emit("microphone-changed")

    def start_receiving_sound(self):
        """ ask the server to start sending sound and emit the client signal """
        soundlog("start_receiving_sound() sound sink=%s", self.sound_sink)
        if self.sound_sink is not None:
            soundlog("start_receiving_sound: we already have a sound sink")
            return
        elif not self.server_sound_send:
            log.error("cannot start receiving sound: support not enabled on the server")
            return
        #choose a codec:
        from xpra.sound.gstreamer_util import CODEC_ORDER
        matching_codecs = [x for x in self.server_sound_encoders if x in self.speaker_codecs]
        ordered_codecs = [x for x in CODEC_ORDER if x in matching_codecs]
        if len(ordered_codecs)==0:
            log.error("no matching codecs between server (%s) and client (%s)", ",".join(self.server_sound_encoders), ",".join(self.speaker_codecs))
            return
        codec = ordered_codecs[0]
        self.speaker_enabled = True
        self.emit("speaker-changed")
        def sink_ready(*args):
            soundlog("sink_ready(%s) codec=%s", args, codec)
            self.send("sound-control", "start", codec)
            return False
        self.on_sink_ready = sink_ready
        self.start_sound_sink(codec)

    def stop_receiving_sound(self, sequence=None):
        """ ask the server to stop sending sound, toggle flag so we ignore further packets and emit client signal """
        soundlog("stop_receiving_sound(%s) sound sink=%s", sequence, self.sound_sink)
        if sequence is None:
            sequence = self.min_sound_sequence
        ss = self.sound_sink
        self.speaker_enabled = False
        if sequence>=0:
            self.send("sound-control", "stop", sequence)
        if ss is None:
            return
        self.sound_sink = None
        soundlog("stop_receiving_sound sequence used=%s, calling %s", sequence, ss.cleanup)
        ss.cleanup()
        self.emit("speaker-changed")
        soundlog("stop_receiving_sound done")

    def bump_sound_sequence(self):
        if self.server_sound_sequence:
            #server supports the "sound-sequence" feature
            #tell it to use a new one:
            self.min_sound_sequence += 1
            soundlog("bump_sound_sequence() sequence is now %s", self.min_sound_sequence)
            #via idle add so this will wait for UI thread to catch up if needed:
            self.idle_add(self.send_new_sound_sequence)

    def send_new_sound_sequence(self):
        soundlog("send_new_sound_sequence() sequence=%s", self.min_sound_sequence)
        self.send("sound-control", "new-sequence", self.min_sound_sequence)


    def sound_sink_state_changed(self, sound_sink, state):
        if sound_sink!=self.sound_sink:
            soundlog("sound_sink_state_changed(%s, %s) not the current sink, ignoring it", sound_sink, state)
            return
        soundlog("sound_sink_state_changed(%s, %s) on_sink_ready=%s", sound_sink, state, self.on_sink_ready)
        if state=="ready" and self.on_sink_ready:
            if not self.on_sink_ready():
                self.on_sink_ready = None
        self.emit("speaker-changed")
    def sound_sink_bitrate_changed(self, sound_sink, bitrate):
        if sound_sink!=self.sound_sink:
            soundlog("sound_sink_bitrate_changed(%s, %s) not the current sink, ignoring it", sound_sink, bitrate)
            return
        soundlog("sound_sink_bitrate_changed(%s, %s)", sound_sink, bitrate)
        #not shown in the UI, so don't bother with emitting a signal:
        #self.emit("speaker-changed")
    def sound_sink_error(self, sound_sink, error):
        if sound_sink!=self.sound_sink:
            soundlog("sound_sink_error(%s, %s) not the current sink, ignoring it", sound_sink, error)
            return
        soundlog.warn("stopping speaker because of error: %s", error)
        self.stop_receiving_sound()
    def sound_process_stopped(self, sound_sink, *args):
        if sound_sink!=self.sound_sink:
            soundlog("sound_process_stopped(%s, %s) not the current sink, ignoring it", sound_sink, args)
            return
        soundlog.warn("the sound process has stopped")
        self.stop_receiving_sound()

    def sound_sink_overrun(self, sound_sink, *args):
        if sound_sink!=self.sound_sink:
            soundlog("sound_sink_overrun() not the current sink, ignoring it")
            return
        soundlog.warn("re-starting speaker because of overrun")
        codec = self.sound_sink.codec
        sequence = self.min_sound_sequence
        if self.server_sound_sequence:
            self.min_sound_sequence += 1
        self.stop_receiving_sound(sequence)
        def restart():
            soundlog("restarting sound sound_sink=%s, codec=%s, server_sound_sequence=%s", self.sound_sink, codec, self.server_sound_sequence)
            if self.server_sound_sequence:
                self.send_new_sound_sequence()
            self.start_receiving_sound()
        #by default for older servers,
        #wait before restarting so we can process the "end-of-stream" message:
        delay = 500 * int(not self.server_sound_eos_sequence)
        soundlog("sound_sink_overrun() will restart in %ims (server supports eos sequence: %s)", delay, self.server_sound_eos_sequence)
        self.timeout_add(delay, restart)

    def sound_sink_exit(self, sound_sink, *args):
        log("sound_sink_exit(%s, %s) sound_sink=%s", sound_sink, args, self.sound_sink)
        ss = self.sound_sink
        if sound_sink!=ss:
            soundlog("sound_sink_exit() not the current sink, ignoring it")
            return
        if ss and ss.codec:
            #the mandatory "I've been naughty warning":
            #we use the "codec" field as guard to ensure we only print this warning once..
            log.warn("the %s sound sink has stopped", ss.codec)
            ss.codec = ""
        #if we had an overrun, we should have restarted things already
        #(and the guard at the top ensures we don't end up stopping the new sink)
        self.stop_receiving_sound()

    def start_sound_sink(self, codec):
        soundlog("start_sound_sink(%s)", codec)
        assert self.sound_sink is None, "sound sink already exists!"
        try:
            soundlog("starting %s sound sink", codec)
            from xpra.sound.wrapper import start_receiving_sound
            ss = start_receiving_sound(codec)
            if not ss:
                return False
            self.sound_sink = ss
            ss.connect("state-changed", self.sound_sink_state_changed)
            ss.connect("error", self.sound_sink_error)
            ss.connect("overrun", self.sound_sink_overrun)
            ss.connect("exit", self.sound_sink_exit)
            from xpra.net.protocol import Protocol
            ss.connect(Protocol.CONNECTION_LOST, self.sound_process_stopped)
            ss.start()
            soundlog("%s sound sink started", codec)
            return True
        except Exception as e:
            log.error("failed to start sound sink", exc_info=True)
            self.sound_sink_error(self.sound_sink, e)
            return False

    def new_sound_buffer(self, sound_source, data, metadata):
        soundlog("new_sound_buffer(%s, %s, %s)", sound_source, len(data or []), metadata)
        if self.sound_source:
            self.sound_out_bytecount += len(data)
            self.send("sound-data", self.sound_source.codec, compression.Compressed(self.sound_source.codec, data), metadata)

    def _process_sound_data(self, packet):
        codec, data, metadata = packet[1:4]
        codec = bytestostr(codec)
        metadata = typedict(metadata)
        if data:
            self.sound_in_bytecount += len(data)
        #verify sequence number if present:
        seq = metadata.intget("sequence", -1)
        if self.min_sound_sequence>0 and seq>=0 and seq<self.min_sound_sequence:
            soundlog("ignoring sound data with old sequence number %s (now on %s)", seq, self.min_sound_sequence)
            return

        if not self.speaker_enabled:
            if metadata.boolget("start-of-stream"):
                #server is asking us to start playing sound
                if not self.speaker_allowed:
                    #no can do!
                    self.stop_receiving_sound()
                    return
                self.speaker_enabled = True
                self.emit("speaker-changed")
                self.on_sink_ready = None
                codec = metadata.strget("codec")
                soundlog("starting speaker on server request using codec %s", codec)
                self.start_sound_sink(codec)
            else:
                soundlog("speaker is now disabled - dropping packet")
                return
        ss = self.sound_sink
        if ss is None:
            soundlog("no sound sink to process sound data, dropping it")
            return
        if metadata.boolget("end-of-stream"):
            soundlog("server sent end-of-stream for sequence %s, closing sound pipeline", seq)
            self.stop_receiving_sound(-1)
            return
        if codec!=ss.codec:
            log.error("sound codec change not supported! (from %s to %s)", ss.codec, codec)
            ss.stop()
            return
        elif ss.get_state()=="stopped":
            soundlog("sound data received, sound sink is stopped - starting it")
            ss.start()
        #(some packets (ie: sos, eos) only contain metadata)
        if len(data)>0:
            ss.add_data(data, metadata)


    def send_notify_enabled(self):
        assert self.client_supports_notifications, "cannot toggle notifications: the feature is disabled by the client"
        assert self.server_supports_notifications, "cannot toggle notifications: the feature is disabled by the server"
        assert self.toggle_cursors_bell_notify, "cannot toggle notifications: server lacks the feature"
        self.send("set-notify", self.notifications_enabled)

    def send_bell_enabled(self):
        assert self.client_supports_bell, "cannot toggle bell: the feature is disabled by the client"
        assert self.server_supports_bell, "cannot toggle bell: the feature is disabled by the server"
        assert self.toggle_cursors_bell_notify, "cannot toggle bell: server lacks the feature"
        self.send("set-bell", self.bell_enabled)

    def send_cursors_enabled(self):
        assert self.client_supports_cursors, "cannot toggle cursors: the feature is disabled by the client"
        assert self.server_supports_cursors, "cannot toggle cursors: the feature is disabled by the server"
        assert self.toggle_cursors_bell_notify, "cannot toggle cursors: server lacks the feature"
        self.send("set-cursors", self.cursors_enabled)

    def send_force_ungrab(self, wid):
        assert self.force_ungrab
        self.send("force-ungrab", wid)

    def set_deflate_level(self, level):
        self.compression_level = level
        self.send_deflate_level()

    def send_deflate_level(self):
        self._protocol.set_compression_level(self.compression_level)
        self.send("set_deflate", self.compression_level)


    def _process_clipboard_enabled_status(self, packet):
        clipboard_enabled, reason = packet[1:3]
        if self.clipboard_enabled!=clipboard_enabled:
            log.info("clipboard toggled to %s by the server, reason: %s", ["off", "on"][int(clipboard_enabled)], reason)
            self.clipboard_enabled = bool(clipboard_enabled)
            self.emit("clipboard-toggled")

    def send_clipboard_enabled_status(self, *args):
        self.send("set-clipboard-enabled", self.clipboard_enabled)

    def send_keyboard_sync_enabled_status(self, *args):
        self.send("set-keyboard-sync-enabled", self.keyboard_sync)


    def set_encoding(self, encoding):
        log("set_encoding(%s)", encoding)
        assert encoding in self.get_encodings(), "encoding %s is not supported!" % encoding
        assert encoding in self.server_encodings, "encoding %s is not supported by the server! (only: %s)" % (encoding, self.server_encodings)
        self.encoding = encoding
        self.send("encoding", encoding)


    def reset_cursor(self):
        self.set_windows_cursor(self._id_to_window.values(), [])

    def _ui_event(self):
        if self._ui_events==0:
            self.emit("first-ui-received")
        self._ui_events += 1

    def _process_new_common(self, packet, override_redirect):
        self._ui_event()
        wid, x, y, w, h, metadata = packet[1:7]
        windowlog("process_new_common: %s, OR=%s", packet[1:7], override_redirect)
        assert wid not in self._id_to_window, "we already have a window %s" % wid
        if w<=0 or h<=0:
            windowlog.error("window dimensions are wrong: %sx%s", w, h)
            w, h = 1, 1
        client_properties = {}
        if len(packet)>=8:
            client_properties = packet[7]
        self.make_new_window(wid, x, y, w, h, metadata, override_redirect, client_properties)

    def make_new_window(self, wid, x, y, w, h, metadata, override_redirect, client_properties):
        metadata = typedict(metadata)
        client_window_classes = self.get_client_window_classes(w, h, metadata, override_redirect)
        group_leader_window = self.get_group_leader(wid, metadata, override_redirect)
        #workaround for "popup" OR windows without a transient-for (like: google chrome popups):
        #prevents them from being pushed under other windows on OSX
        #find a "transient-for" value using the pid to find a suitable window
        #if possible, choosing the currently focused window (if there is one..)
        pid = metadata.intget("pid", 0)
        if override_redirect and pid>0 and metadata.intget("transient-for", 0)>0 is None and metadata.get("role")=="popup":
            tfor = None
            for twid, twin in self._id_to_window.items():
                if not twin._override_redirect and twin._metadata.intget("pid", -1)==pid:
                    tfor = twin
                    if twid==self._focused:
                        break
            if tfor:
                windowlog("forcing transient for=%s for new window %s", twid, wid)
                metadata["transient-for"] = twid
        window = None
        windowlog("make_new_window(..) client_window_classes=%s, group_leader_window=%s", client_window_classes, group_leader_window)
        for cwc in client_window_classes:
            try:
                window = cwc(self, group_leader_window, wid, x, y, w, h, metadata, override_redirect, client_properties, self.border, self.max_window_size)
                break
            except:
                windowlog.warn("failed to instantiate %s", cwc, exc_info=True)
        if window is None:
            windowlog.warn("no more options.. this window will not be shown, sorry")
            return None
        self._id_to_window[wid] = window
        self._window_to_id[window] = wid
        window.show()
        return window

    def get_group_leader(self, wid, metadata, override_redirect):
        #subclasses that wish to implement the feature may override this method
        return None


    def get_client_window_classes(self, w, h, metadata, override_redirect):
        return [self.ClientWindowClass]

    def _process_new_window(self, packet):
        self._process_new_common(packet, False)

    def _process_new_override_redirect(self, packet):
        self._process_new_common(packet, True)

    def _process_new_tray(self, packet):
        assert SYSTEM_TRAY_SUPPORTED
        self._ui_event()
        wid, w, h = packet[1:4]
        metadata = {}
        if len(packet)>=5:
            metadata = packet[4]
        assert wid not in self._id_to_window, "we already have a window %s" % wid
        tray = self.setup_system_tray(self, wid, w, h, metadata.get("title", ""))
        traylog("process_new_tray(%s) tray=%s", packet, tray)
        self._id_to_window[wid] = tray
        self._window_to_id[tray] = wid

    def _process_window_move_resize(self, packet):
        (wid, x, y, w, h) = packet[1:6]
        resize_counter = -1
        if len(packet)>4:
            resize_counter = packet[4]
        window = self._id_to_window.get(wid)
        windowlog("_process_window_resized moving / resizing window %s (id=%s) to %s", window, wid, (x, y, w, h))
        if window:
            window.move_resize(x, y, w, h, resize_counter)

    def _process_window_resized(self, packet):
        (wid, w, h) = packet[1:4]
        resize_counter = -1
        if len(packet)>4:
            resize_counter = packet[4]
        window = self._id_to_window.get(wid)
        windowlog("_process_window_resized resizing window %s (id=%s) to %s", window, wid, (w,h))
        if window:
            window.resize(w, h, resize_counter)

    def _process_draw(self, packet):
        self._draw_queue.put(packet)

    def send_damage_sequence(self, wid, packet_sequence, width, height, decode_time):
        self.send_now("damage-sequence", packet_sequence, wid, width, height, decode_time)

    def _draw_thread_loop(self):
        while self.exit_code is None:
            packet = self._draw_queue.get()
            try:
                self._do_draw(packet)
                time.sleep(0)
            except KeyboardInterrupt:
                raise
            except:
                log.error("error processing draw packet", exc_info=True)

    def _do_draw(self, packet):
        """ this runs from the draw thread above """
        wid, x, y, width, height, coding, data, packet_sequence, rowstride = packet[1:10]
        #rename old encoding aliases early:
        window = self._id_to_window.get(wid)
        if not window:
            #window is gone
            def draw_cleanup():
                if coding=="mmap":
                    assert self.mmap_enabled
                    from xpra.net.mmap_pipe import int_from_buffer
                    def free_mmap_area():
                        #we need to ack the data to free the space!
                        data_start = int_from_buffer(self.mmap, 0)
                        offset, length = data[-1]
                        data_start.value = offset+length
                    #clear the mmap area via idle_add so any pending draw requests
                    #will get a chance to run first (preserving the order)
                self.send_damage_sequence(wid, packet_sequence, width, height, -1)
            self.idle_add(draw_cleanup)
            return
        options = {}
        if len(packet)>10:
            options = packet[10]
        options = typedict(options)
        paintlog("process_draw %s bytes for window %s using %s encoding with options=%s", len(data), wid, coding, options)
        start = time.time()
        def record_decode_time(success):
            if success:
                end = time.time()
                decode_time = int(end*1000*1000-start*1000*1000)
                self.pixel_counter.append((start, end, width*height))
                dms = "%sms" % (int(decode_time/100)/10.0)
                paintlog("record_decode_time(%s) wid=%s, %s: %sx%s, %s", success, wid, coding, width, height, dms)
            else:
                decode_time = -1
                paintlog("record_decode_time(%s) decoding error on wid=%s, %s: %sx%s", success, wid, coding, width, height)
            self.send_damage_sequence(wid, packet_sequence, width, height, decode_time)
        try:
            window.draw_region(x, y, width, height, coding, data, rowstride, packet_sequence, options, [record_decode_time])
        except KeyboardInterrupt:
            raise
        except:
            log.error("draw error", exc_info=True)
            self.idle_add(record_decode_time, False)
            raise

    def _process_cursor(self, packet):
        if not self.cursors_enabled:
            return
        if len(packet)==2:
            new_cursor = packet[1]
        elif len(packet)>=8:
            new_cursor = packet[1:]
        else:
            raise Exception("invalid cursor packet: %s items" % len(packet))
        self.set_windows_cursor(self._id_to_window.values(), new_cursor)

    def _process_bell(self, packet):
        if not self.bell_enabled:
            return
        (wid, device, percent, pitch, duration, bell_class, bell_id, bell_name) = packet[1:9]
        window = self._id_to_window.get(wid)
        self.window_bell(window, device, percent, pitch, duration, bell_class, bell_id, bell_name)


    def _process_notify_show(self, packet):
        if not self.notifications_enabled:
            return
        self._ui_event()
        dbus_id, nid, app_name, replaces_nid, app_icon, summary, body, expire_timeout = packet[1:9]
        log("_process_notify_show(%s)", packet)
        assert self.notifier
        #TODO: choose more appropriate tray if we have more than one shown?
        tray = self.tray
        self.notifier.show_notify(dbus_id, tray, nid, app_name, replaces_nid, app_icon, summary, body, expire_timeout)

    def _process_notify_close(self, packet):
        if not self.notifications_enabled:
            return
        assert self.notifier
        nid = packet[1]
        log("_process_notify_close(%s)", nid)
        self.notifier.close_notify(nid)


    def _process_raise_window(self, packet):
        #only implemented in gtk2 for now
        pass

    def _process_show_desktop(self, packet):
        show = packet[1]
        log("calling %s(%s)", show_desktop, show)
        show_desktop(show)


    def _process_initiate_moveresize(self, packet):
        wid = packet[1]
        window = self._id_to_window.get(wid)
        if window:
            window.initiate_moveresize(*packet[2:7])

    def _process_window_metadata(self, packet):
        wid, metadata = packet[1:3]
        window = self._id_to_window.get(wid)
        if window:
            metadata = typedict(metadata)
            window.update_metadata(metadata)

    def _process_window_icon(self, packet):
        wid, w, h, pixel_format, data = packet[1:6]
        window = self._id_to_window.get(wid)
        iconlog("_process_window_icon(%s, %s, %s, %s, %s bytes) window=%s", wid, w, h, pixel_format, len(data), window)
        if window:
            window.update_icon(w, h, pixel_format, data)

    def _process_configure_override_redirect(self, packet):
        wid, x, y, w, h = packet[1:6]
        window = self._id_to_window[wid]
        window.move_resize(x, y, w, h, -1)

    def _process_lost_window(self, packet):
        wid = packet[1]
        window = self._id_to_window.get(wid)
        if window:
            del self._id_to_window[wid]
            del self._window_to_id[window]
            self.destroy_window(wid, window)
        if len(self._id_to_window)==0:
            windowlog("last window gone, clearing key repeat")
            if self.keyboard_helper:
                self.keyboard_helper.clear_repeat()

    def destroy_window(self, wid, window):
        windowlog("destroy_window(%s, %s)", wid, window)
        window.destroy()
        if self._window_with_grab==wid:
            log("destroying window %s which has grab, ungrabbing!", wid)
            self.window_ungrab()
            self._window_with_grab = None

    def _process_desktop_size(self, packet):
        root_w, root_h, max_w, max_h = packet[1:5]
        log("server has resized the desktop to: %sx%s (max %sx%s)", root_w, root_h, max_w, max_h)
        self.server_max_desktop_size = max_w, max_h
        self.server_actual_desktop_size = root_w, root_h

    def set_max_packet_size(self):
        root_w, root_h = self.get_root_size()
        maxw, maxh = root_w, root_h
        try:
            server_w, server_h = self.server_actual_desktop_size
            maxw = max(root_w, server_w)
            maxh = max(root_h, server_h)
        except:
            pass
        assert maxw>0 and maxh>0 and maxw<32768 and maxh<32768, "problems calculating maximum desktop size: %sx%s" % (maxw, maxh)
        #max packet size to accomodate:
        # * full screen RGBX (32 bits) uncompressed
        # * file-size-limit
        # both with enough headroom for some metadata (4k)
        p = self._protocol
        if p:
            p.max_packet_size = max(maxw*maxh*4, self.file_size_limit*1024*1024) + 4*1024
            p.abs_max_packet_size = max(maxw*maxh*4 * 4, self.file_size_limit*1024*1024) + 4*1024
            log("maximum packet size set to %i", p.max_packet_size)


    def init_authenticated_packet_handlers(self):
        log("init_authenticated_packet_handlers()")
        XpraClientBase.init_authenticated_packet_handlers(self)
        def delhandler(k):
            #remove any existing mapping:
            if k in self._packet_handlers:
                del self._packet_handlers[k]
            if k in self._ui_packet_handlers:
                del self._ui_packet_handlers[k]
        for k,v in {
            "startup-complete":     self._startup_complete,
            "new-window":           self._process_new_window,
            "new-override-redirect":self._process_new_override_redirect,
            "new-tray":             self._process_new_tray,
            "raise-window":         self._process_raise_window,
            "initiate-moveresize":  self._process_initiate_moveresize,
            "show-desktop":         self._process_show_desktop,
            "window-move-resize":   self._process_window_move_resize,
            "window-resized":       self._process_window_resized,
            "cursor":               self._process_cursor,
            "bell":                 self._process_bell,
            "notify_show":          self._process_notify_show,
            "notify_close":         self._process_notify_close,
            "set-clipboard-enabled":self._process_clipboard_enabled_status,
            "window-metadata":      self._process_window_metadata,
            "configure-override-redirect":  self._process_configure_override_redirect,
            "lost-window":          self._process_lost_window,
            "desktop_size":         self._process_desktop_size,
            "window-icon":          self._process_window_icon,
            "rpc-reply":            self._process_rpc_reply,
            "control" :             self._process_control,
            "draw":                 self._process_draw,
            # "clipboard-*" packets are handled by a special case below.
            }.items():
            delhandler(k)
            self._ui_packet_handlers[k] = v
        #these handlers can run directly from the network thread:
        for k,v in {
            "ping":                 self._process_ping,
            "ping_echo":            self._process_ping_echo,
            "info-response":        self._process_info_response,
            "sound-data":           self._process_sound_data,
            "server-event":         self._process_server_event,
            }.items():
            delhandler(k)
            self._packet_handlers[k] = v

    def process_clipboard_packet(self, packet):
        self.idle_add(self.clipboard_helper.process_clipboard_packet, packet)

    def process_packet(self, proto, packet):
        packet_type = packet[0]
        self.check_server_echo(0)
        packet_type_str = bytestostr(packet_type)
        if packet_type_str.startswith("clipboard-"):
            if self.clipboard_enabled and self.clipboard_helper:
                self.process_clipboard_packet(packet)
        else:
            XpraClientBase.process_packet(self, proto, packet)
