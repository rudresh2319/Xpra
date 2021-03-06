# This file is part of Parti.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2012 Antoine Martin <antoine@devloop.org.uk>
# Copyright (C) 2008, 2010 Nathaniel Smith <njs@pobox.com>
# Parti is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import gtk
import gobject
import cairo
import re
import os
import time

from wimpiggy.util import (n_arg_signal,
                           gtk_main_quit_really,
                           gtk_main_quit_on_fatal_exceptions_enable)

from wimpiggy.log import Logger
log = Logger()

from xpra.deque import maxdeque
from xpra.client_base import XpraClientBase
from xpra.keys import mask_to_names, DEFAULT_MODIFIER_MEANINGS, DEFAULT_MODIFIER_NUISANCE, DEFAULT_MODIFIER_IGNORE_KEYNAMES
from xpra.platform.gui import ClientExtras
from xpra.scripts.main import ENCODINGS
from xpra.version_util import is_compatible_with

def nn(x):
    if x is None:
        return  ""
    return x

class ClientWindow(gtk.Window):
    def __init__(self, client, wid, x, y, w, h, metadata, override_redirect):
        if override_redirect:
            gtk.Window.__init__(self, gtk.WINDOW_POPUP)
        else:
            gtk.Window.__init__(self, gtk.WINDOW_TOPLEVEL)
        self._client = client
        self._id = wid
        self._pos = (-1, -1)
        self._size = (1, 1)
        self._backing = None
        self._metadata = {}
        self._override_redirect = override_redirect
        self._new_backing(w, h)
        self._refresh_timer = None
        self._refresh_requested = False
        # used for only sending focus events *after* the window is mapped:
        self._been_mapped = False
        self._override_redirect_windows = []

        self.update_metadata(metadata)

        self.set_app_paintable(True)
        self.add_events(gtk.gdk.STRUCTURE_MASK
                        | gtk.gdk.KEY_PRESS_MASK | gtk.gdk.KEY_RELEASE_MASK
                        | gtk.gdk.POINTER_MOTION_MASK
                        | gtk.gdk.BUTTON_PRESS_MASK
                        | gtk.gdk.BUTTON_RELEASE_MASK)

        self.move(x, y)
        self.set_default_size(w, h)
        if override_redirect:
            transient_for = self.get_transient_for()
            type_hint = self.get_type_hint()
            if transient_for is not None and transient_for.window is not None and type_hint in [gtk.gdk.WINDOW_TYPE_HINT_DIALOG,
                            gtk.gdk.WINDOW_TYPE_HINT_MENU, gtk.gdk.WINDOW_TYPE_HINT_TOOLBAR,
                            #gtk.gdk.WINDOW_TYPE_HINT_SPLASHSCREEN, gtk.gdk.WINDOW_TYPE_HINT_UTILITY,
                            #gtk.gdk.WINDOW_TYPE_HINT_DOCK, gtk.gdk.WINDOW_TYPE_HINT_DESKTOP,
                            gtk.gdk.WINDOW_TYPE_HINT_DROPDOWN_MENU, gtk.gdk.WINDOW_TYPE_HINT_POPUP_MENU,
                            gtk.gdk.WINDOW_TYPE_HINT_TOOLTIP,
                            #gtk.gdk.WINDOW_TYPE_HINT_NOTIFICATION,
                            gtk.gdk.WINDOW_TYPE_HINT_COMBO,gtk.gdk.WINDOW_TYPE_HINT_DND]:
                transient_for._override_redirect_windows.append(self)
        self.connect("notify::has-toplevel-focus", self._focus_change)

    def update_metadata(self, metadata):
        self._metadata.update(metadata)

        title = self._client.title
        if title.find("@")>=0:
            #perform metadata variable substitutions:
            default_values = {"title" : u"<untitled window>",
                              "client-machine" : u"<unknown machine>"}
            def metadata_replace(match):
                atvar = match.group(0)          #ie: '@title@'
                var = atvar[1:len(atvar)-1]     #ie: 'title'
                default_value = default_values.get(var, u"<unknown %s>" % var)
                return self._metadata.get(var, default_value).decode("utf-8")
            title = re.sub("@[\w\-]*@", metadata_replace, title)
        self.set_title(u"%s" % title)

        if "size-constraints" in self._metadata:
            size_metadata = self._metadata["size-constraints"]
            hints = {}
            for (a, h1, h2) in [
                ("maximum-size", "max_width", "max_height"),
                ("minimum-size", "min_width", "min_height"),
                ("base-size", "base_width", "base_height"),
                ("increment", "width_inc", "height_inc"),
                ]:
                if a in self._metadata["size-constraints"]:
                    hints[h1], hints[h2] = size_metadata[a]
            for (a, h) in [
                ("minimum-aspect", "min_aspect_ratio"),
                ("maximum-aspect", "max_aspect_ratio"),
                ]:
                if a in self._metadata:
                    hints[h] = size_metadata[a][0] * 1.0 / size_metadata[a][1]
            self.set_geometry_hints(None, **hints)

        if not (self.flags() & gtk.REALIZED):
            self.set_wmclass(*self._metadata.get("class-instance",
                                                 ("xpra", "Xpra")))

        if "icon" in self._metadata:
            (width, height, coding, data) = self._metadata["icon"]
            if coding == "premult_argb32":
                cairo_surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
                cairo_surf.get_data()[:] = data
                # FIXME: We round-trip through PNG. This is ridiculous, but faster
                # than doing a bunch of alpha un-premultiplying and byte-swapping
                # by hand in Python (better still would be to write some Pyrex,
                # but I don't have time right now):
                loader = gtk.gdk.PixbufLoader()
                cairo_surf.write_to_png(loader)
                loader.close()
                pixbuf = loader.get_pixbuf()
            else:
                loader = gtk.gdk.PixbufLoader(coding)
                loader.write(data, len(data))
                loader.close()
                pixbuf = loader.get_pixbuf()
            self.set_icon(pixbuf)

        if "transient-for" in self._metadata:
            wid = self._metadata.get("transient-for")
            window = self._client._id_to_window.get(wid)
            log.debug("found transient-for: %s / %s", wid, window)
            if window:
                self.set_transient_for(window)

        if "window-type" in self._metadata:
            name_to_hint = {
                            "_NET_WM_WINDOW_TYPE_NORMAL"    : gtk.gdk.WINDOW_TYPE_HINT_NORMAL,
                            "_NET_WM_WINDOW_TYPE_DIALOG"    : gtk.gdk.WINDOW_TYPE_HINT_DIALOG,
                            "_NET_WM_WINDOW_TYPE_MENU"      : gtk.gdk.WINDOW_TYPE_HINT_MENU,
                            "_NET_WM_WINDOW_TYPE_TOOLBAR"   : gtk.gdk.WINDOW_TYPE_HINT_TOOLBAR,
                            "_NET_WM_WINDOW_TYPE_SPLASH"    : gtk.gdk.WINDOW_TYPE_HINT_SPLASHSCREEN,
                            "_NET_WM_WINDOW_TYPE_UTILITY"   : gtk.gdk.WINDOW_TYPE_HINT_UTILITY,
                            "_NET_WM_WINDOW_TYPE_DOCK"      : gtk.gdk.WINDOW_TYPE_HINT_DOCK,
                            "_NET_WM_WINDOW_TYPE_DESKTOP"   : gtk.gdk.WINDOW_TYPE_HINT_DESKTOP,
                            "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU" : gtk.gdk.WINDOW_TYPE_HINT_DROPDOWN_MENU,
                            "_NET_WM_WINDOW_TYPE_POPUP_MENU": gtk.gdk.WINDOW_TYPE_HINT_POPUP_MENU,
                            "_NET_WM_WINDOW_TYPE_TOOLTIP"   : gtk.gdk.WINDOW_TYPE_HINT_TOOLTIP,
                            "_NET_WM_WINDOW_TYPE_NOTIFICATION" : gtk.gdk.WINDOW_TYPE_HINT_NOTIFICATION,
                            "_NET_WM_WINDOW_TYPE_COMBO"     : gtk.gdk.WINDOW_TYPE_HINT_COMBO,
                            "_NET_WM_WINDOW_TYPE_DND"       : gtk.gdk.WINDOW_TYPE_HINT_DND
                            }
            window_types = self._metadata.get("window-type")
            log.debug("window types=%s", window_types)
            for window_type in window_types:
                hint = name_to_hint.get(window_type)
                if hint:
                    log.debug("setting window type to %s - %s", window_type, hint)
                    self.set_type_hint(hint)
                    break

    def _new_backing(self, w, h):
        old_backing = self._backing
        self._backing = gtk.gdk.Pixmap(gtk.gdk.get_default_root_window(), w, h)
        cr = self._backing.cairo_create()
        if old_backing is not None:
            # Really we should respect bit-gravity here but... meh.
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_pixmap(old_backing, 0, 0)
            cr.paint()
            old_w, old_h = old_backing.get_size()
            cr.move_to(old_w, 0)
            cr.line_to(w, 0)
            cr.line_to(w, h)
            cr.line_to(0, h)
            cr.line_to(0, old_h)
            cr.line_to(old_w, old_h)
            cr.close_path()
        else:
            cr.rectangle(0, 0, w, h)
        cr.set_source_rgb(1, 1, 1)
        cr.fill()

    def refresh_window(self):
        log.debug("Automatic refresh for id %s", self._id)
        self._client.send_refresh(self._id)

    def refresh_all_windows(self):
        #this method is only here because we may want to fire it
        #from a --key-shortcut action and the event is delivered to
        #the "ClientWindow"
        self._client.send_refresh_all()

    def draw(self, x, y, width, height, coding, img_data, rowstride):
        gc = self._backing.new_gc()
        if coding == "mmap":
            """ see _mmap_send() in server.py for details """
            assert self._client.supports_mmap
            import ctypes
            data_start = ctypes.c_uint.from_buffer(self._client.mmap, 0)
            if len(img_data)==1:
                #construct an array directly from the mmap zone:
                offset, length = img_data[0]
                arraytype = ctypes.c_char * length
                data = arraytype.from_buffer(self._client.mmap, offset)
                self._backing.draw_rgb_image(gc, x, y, width, height, gtk.gdk.RGB_DITHER_NONE, data, rowstride)
                data_start.value = offset+length
                self._backing.draw_rgb_image(gc, x, y, width, height, gtk.gdk.RGB_DITHER_NONE, data, rowstride)
            else:
                #re-construct the buffer from discontiguous chunks:
                log("drawing from discontiguous area: %s", img_data)
                data = ""
                for offset, length in img_data:
                    self._client.mmap.seek(offset)
                    data += self._client.mmap.read(length)
                    data_start.value = offset+length
                self._backing.draw_rgb_image(gc, x, y, width, height, gtk.gdk.RGB_DITHER_NONE, data, rowstride)
        elif coding == "rgb24":
            if rowstride>0:
                assert len(img_data) == rowstride * height
            else:
                assert len(img_data) == width * 3 * height
            self._backing.draw_rgb_image(gc, x, y, width, height, gtk.gdk.RGB_DITHER_NONE, img_data, rowstride)
        else:
            loader = gtk.gdk.PixbufLoader(coding)
            loader.write(img_data, len(img_data))
            loader.close()
            pixbuf = loader.get_pixbuf()
            if not pixbuf:
                log.error("failed %s pixbuf=%s data len=%s" % (coding, pixbuf, len(img_data)))
            else:
                self._backing.draw_pixbuf(gc, pixbuf, 0, 0, x, y, width, height)
        self.window.invalidate_rect(gtk.gdk.Rectangle(x, y, width, height), False)

        if self._refresh_requested:
            self._refresh_requested = False
        else:
            if self._refresh_timer:
                gobject.source_remove(self._refresh_timer)
                self._refresh_timer = None
            if self._client.auto_refresh_delay and coding == "jpeg":
                self._refresh_timer = gobject.timeout_add(int(1000 * self._client.auto_refresh_delay), self.refresh_window)

    def do_expose_event(self, event):
        if not self.flags() & gtk.MAPPED:
            return
        cr = self.window.cairo_create()
        cr.rectangle(event.area)
        cr.clip()
        cr.set_source_pixmap(self._backing, 0, 0)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.paint()
        return False

    def _geometry(self):
        (x, y) = self.window.get_origin()
        (_, _, w, h, _) = self.window.get_geometry()
        return (x, y, w, h)

    def do_map_event(self, event):
        log("Got map event")
        gtk.Window.do_map_event(self, event)
        if not self._override_redirect:
            x, y, w, h = self._geometry()
            self._client.send(["map-window", self._id, x, y, w, h])
            self._pos = (x, y)
            self._size = (w, h)
        self._been_mapped = True
        gobject.idle_add(self._focus_change)

    def do_configure_event(self, event):
        log("Got configure event")
        gtk.Window.do_configure_event(self, event)
        if not self._override_redirect:
            x, y, w, h = self._geometry()
            if (x, y) != self._pos:
                ox, oy = self._pos
                dx, dy = x-ox, y-oy
                self._pos = (x, y)
                self._client.send(["move-window", self._id, x, y])
                for window in self._override_redirect_windows:
                    x, y = window.get_position()
                    window.move(x+dx, y+dy)
            if (w, h) != self._size:
                self._size = (w, h)
                self._client.send(["resize-window", self._id, w, h])
                self._new_backing(w, h)

    def move_resize(self, x, y, w, h):
        assert self._override_redirect
        self.window.move_resize(x, y, w, h)
        self._new_backing(w, h)

    def destroy(self):
        self._unfocus()
        gtk.Window.destroy(self)

    def _unfocus(self):
        if self._client._focused==self._id:
            self._client.update_focus(self._id, False)

    def do_unmap_event(self, event):
        self._unfocus()
        if not self._override_redirect:
            self._client.send(["unmap-window", self._id])

    def do_delete_event(self, event):
        self._client.send(["close-window", self._id])
        return True

    def quit(self):
        self._client.quit()

    def void(self):
        pass

    def do_key_press_event(self, event):
        self._client.handle_key_action(event, self, True)

    def do_key_release_event(self, event):
        self._client.handle_key_action(event, self, False)

    def _pointer_modifiers(self, event):
        pointer = (int(event.x_root), int(event.y_root))
        modifiers = self._client.mask_to_names(event.state)
        return pointer, modifiers

    def do_motion_notify_event(self, event):
        if self._client.readonly:
            return
        (pointer, modifiers) = self._pointer_modifiers(event)
        self._client.send_mouse_position(["pointer-position", self._id,
                                          pointer, modifiers])

    def _button_action(self, button, event, depressed):
        if self._client.readonly:
            return
        (pointer, modifiers) = self._pointer_modifiers(event)
        self._client.send_positional(["button-action", self._id,
                                      button, depressed,
                                      pointer, modifiers])

    def do_button_press_event(self, event):
        if self._client.readonly:
            return
        self._button_action(event.button, event, True)

    def do_button_release_event(self, event):
        if self._client.readonly:
            return
        self._button_action(event.button, event, False)

    def do_scroll_event(self, event):
        if self._client.readonly:
            return
        # Map scroll directions back to mouse buttons.  Mapping is taken from
        # gdk/x11/gdkevents-x11.c.
        scroll_map = {gtk.gdk.SCROLL_UP: 4,
                      gtk.gdk.SCROLL_DOWN: 5,
                      gtk.gdk.SCROLL_LEFT: 6,
                      gtk.gdk.SCROLL_RIGHT: 7,
                      }
        self._button_action(scroll_map[event.direction], event, True)
        self._button_action(scroll_map[event.direction], event, False)

    def _focus_change(self, *args):
        log("_focus_change(%s)", args)
        if self._been_mapped:
            self._client.update_focus(self._id, self.get_property("has-toplevel-focus"))

gobject.type_register(ClientWindow)


class XpraClient(XpraClientBase):
    __gsignals__ = {
        "clipboard-toggled": n_arg_signal(0),
        }

    def __init__(self, conn, opts):
        XpraClientBase.__init__(self, opts)
        self.start_time = time.time()
        self._window_to_id = {}
        self._id_to_window = {}
        title = opts.title
        if opts.title_suffix is not None:
            title = "@title@ %s" % opts.title_suffix
        self.title = title
        self.readonly = opts.readonly
        self.session_name = opts.session_name
        self.compression_level = opts.compression_level
        self.auto_refresh_delay = opts.auto_refresh_delay
        self.max_bandwidth = opts.max_bandwidth
        if self.max_bandwidth>0.0 and self.jpegquality==0:
            """ jpegquality was not set, use a better start value """
            self.jpegquality = 50

        self.server_capabilities = {}

        self.mmap_enabled = False
        self.server_start_time = -1
        self.server_platform = ""
        self.server_actual_desktop_size = None
        self.server_desktop_size = None
        self.server_randr = False
        self.pixel_counter = maxdeque(maxlen=100)
        self.server_latency = maxdeque(maxlen=100)
        self.server_load = None
        self.client_latency = maxdeque(maxlen=100)
        self.toggle_cursors_bell_notify = False
        self.bell_enabled = True
        self.cursors_enabled = True
        self.notifications_enabled = True
        self.clipboard_enabled = False
        self.mmap = None
        self.mmap_token = None
        self.mmap_file = None
        self.mmap_size = 0

        self._client_extras = ClientExtras(self, opts)
        self.clipboard_enabled = not self.readonly and opts.clipboard and self._client_extras.supports_clipboard()
        self.supports_mmap = opts.mmap and self._client_extras.supports_mmap()
        if self.supports_mmap:
            try:
                import mmap
                import tempfile
                import uuid
                import ctypes
                from stat import S_IRUSR,S_IWUSR,S_IRGRP,S_IWGRP
                mmap_dir = os.getenv("TMPDIR", "/tmp")
                if not os.path.exists(mmap_dir):
                    raise Exception("TMPDIR %s does not exist!" % mmap_dir)
                #create the mmap file, the mkstemp that is called via NamedTemporaryFile ensures 
                #that the file is readable and writable only by the creating user ID
                temp = tempfile.NamedTemporaryFile(prefix="xpra.", suffix=".mmap", dir=mmap_dir)
                #keep a reference to it so it does not disappear!
                self._mmap_temp_file = temp
                self.mmap_file = temp.name
                fd = temp.file.fileno()
                #set the group permissions and gid if the mmap-group option is specified
                if opts.mmap_group and type(conn.target)==str and os.path.exists(conn.target):
                    s = os.stat(conn.target)
                    os.fchown(fd, -1, s.st_gid)
                    os.fchmod(fd, S_IRUSR|S_IWUSR|S_IRGRP|S_IWGRP)
                self.mmap_size = max(4096, mmap.PAGESIZE)*32*1024   #generally 128MB
                log("using mmap file %s, fd=%s, size=%s", self.mmap_file, fd, self.mmap_size)
                os.lseek(fd, self.mmap_size-1, os.SEEK_SET)
                assert os.write(fd, '\x00')
                os.lseek(fd, 0, os.SEEK_SET)
                self.mmap = mmap.mmap(fd, length=self.mmap_size)
                #write the 16 byte token one byte at a time - no endianness
                self.mmap_token = uuid.uuid4().int
                log.debug("mmap_token=%s", self.mmap_token)
                v = self.mmap_token
                for i in range(0,16):
                    poke = ctypes.c_ubyte.from_buffer(self.mmap, 512+i)
                    poke.value = v % 256
                    v = v>>8
                assert v==0
            except Exception, e:
                log.error("failed to setup mmap: %s", e)
                self.supports_mmap = False
                self.clean_mmap()
                self.mmap = None
                self.mmap_file = None
                self.mmap_size = 0

        self.init_packet_handlers()
        self.ready(conn)

        self.keyboard_sync = opts.keyboard_sync
        self.key_repeat_delay = -1
        self.key_repeat_interval = -1
        self.keys_pressed = {}
        self._remote_version = None
        self._keymap_changing = False
        self._keymap = gtk.gdk.keymap_get_default()
        self._do_keys_changed()
        self.key_shortcuts = self.parse_shortcuts(opts.key_shortcuts)
        self.send_hello()

        self._keymap.connect("keys-changed", self._keys_changed)
        self._xsettings_watcher = None
        self._root_props_watcher = None

        self._focused = None
        def compute_receive_bandwidth(delay):
            bw = (self._protocol._recv_counter / 1024) * 1000/ delay;
            self._protocol._recv_counter = 0;
            log.debug("Bandwidth is ", bw, "kB/s, max ", self.max_bandwidth, "kB/s")
            q = self.jpegquality
            if bw > self.max_bandwidth:
                q -= 10
            elif bw < self.max_bandwidth:
                q += 5
            q = max(10, min(95 ,q))
            self.send_jpeg_quality(q)
            return True
        if (self.max_bandwidth):
            gobject.timeout_add(2000, compute_receive_bandwidth, 2000);

    def init_packet_handlers(self):
        XpraClientBase.init_packet_handlers(self)
        for k,v in {
            "hello":                self._process_hello,
            "new-window":           self._process_new_window,
            "new-override-redirect":self._process_new_override_redirect,
            "draw":                 self._process_draw,
            "cursor":               self._process_cursor,
            "bell":                 self._process_bell,
            "notify_show":          self._process_notify_show,
            "notify_close":         self._process_notify_close,
            "ping":                 self._process_ping,
            "ping_echo":            self._process_ping_echo,
            "window-metadata":      self._process_window_metadata,
            "configure-override-redirect":  self._process_configure_override_redirect,
            "lost-window":          self._process_lost_window,
            # "clipboard-*" packets are handled by a special case below.
            }.items():
            self._packet_handlers[k] = v

    def run(self):
        gtk_main_quit_on_fatal_exceptions_enable()
        gtk.main()

    def quit(self, *args):
        gtk_main_quit_really()

    def cleanup(self):
        if self._client_extras:
            self._client_extras.exit()
            self._client_extras = None
        XpraClientBase.cleanup(self)
        self.clean_mmap()

    def clean_mmap(self):
        if self.mmap_file and os.path.exists(self.mmap_file):
            os.unlink(self.mmap_file)
            self.mmap_file = None

    def parse_shortcuts(self, strs):
        #TODO: maybe parse with re instead?
        if len(strs)==0:
            """ if none are defined, add this as default
            it would be nicer to specify it via OptionParser in main
            but then it would always have to be there with no way of removing it
            whereas now it is enough to define one (any shortcut)
            """
            strs = ["meta+shift+F4:quit"]
        log.debug("parse_shortcuts(%s)" % str(strs))
        shortcuts = {}
        #modifier names contains the internal modifiers list, ie: "mod1", "control", ...
        #but the user expects the name of the key to be used, ie: "alt" or "super"
        #whereas at best, we keep "Alt_L" : "mod1" mappings... (xposix)
        #so generate a map from one to the other:
        modifier_names = {}
        meanings = self.xkbmap_mod_meanings or DEFAULT_MODIFIER_MEANINGS
        for pub_name,mod_name in meanings.items():
            if mod_name in DEFAULT_MODIFIER_NUISANCE or pub_name in DEFAULT_MODIFIER_IGNORE_KEYNAMES:
                continue
            #just hope that xxx_L is mapped to the same modifier as xxx_R!
            if pub_name.endswith("_L") or pub_name.endswith("_R"):
                pub_name = pub_name[:-2]
            elif pub_name=="ISO_Level3_Shift":
                pub_name = "AltGr"
            if pub_name not in modifier_names:
                modifier_names[pub_name.lower()] = mod_name

        for s in strs:
            #example for s: Control+F8:some_action()
            parts = s.split(":", 1)
            if len(parts)!=2:
                log.error("invalid shortcut: %s" % s)
                continue
            #example for action: "quit"
            action = parts[1]
            #example for keyspec: ["Alt", "F8"]
            keyspec = parts[0].split("+")
            modifiers = []
            if len(keyspec)>1:
                valid = True
                #ie: ["Alt"]
                for mod in keyspec[:len(keyspec)-1]:
                    #ie: "alt_l" -> "mod1"
                    imod = modifier_names.get(mod.lower())
                    if not imod:
                        log.error("invalid modifier: %s, valid modifiers are: %s", mod, modifier_names.keys())
                        valid = False
                        break
                    modifiers.append(imod)
                if not valid:
                    continue
            keyname = keyspec[len(keyspec)-1]
            shortcuts[keyname] = (modifiers, action)
        log.debug("parse_shortcuts(%s)=%s" % (str(strs), shortcuts))
        return  shortcuts

    def key_handled_as_shortcut(self, window, key_name, modifiers, depressed):
        shortcut = self.key_shortcuts.get(key_name)
        if not shortcut:
            return  False
        (req_mods, action) = shortcut
        for rm in req_mods:
            if rm not in modifiers:
                #modifier is missing, bail out
                return False
        if not depressed:
            """ when the key is released, just ignore it - do NOT send it to the server! """
            return  True
        try:
            method = getattr(window, action)
            log.info("key_handled_as_shortcut(%s,%s,%s,%s) has been handled by shortcut=%s", window, key_name, modifiers, depressed, shortcut)
        except AttributeError, e:
            log.error("key dropped, invalid method name in shortcut %s: %s", action, e)
            return  True
        try:
            method()
        except Exception, e:
            log.error("key_handled_as_shortcut(%s,%s,%s,%s) failed to execute shortcut=%s: %s", window, key_name, modifiers, depressed, shortcut, e)
        return  True

    def handle_key_action(self, event, window, depressed):
        if self.readonly:
            return
        log.debug("handle_key_action(%s,%s,%s)" % (event, window, depressed))
        modifiers = self.mask_to_names(event.state)
        name = gtk.gdk.keyval_name(event.keyval)
        keyval = nn(event.keyval)
        keycode = event.hardware_keycode
        group = event.group
        #meant to be in PyGTK since 2.10, not used yet so just return False if we don't have it:
        is_modifier = hasattr(event, "is_modifier") and event.is_modifier
        translated = self._client_extras.translate_key(depressed, keyval, name, keycode, group, is_modifier, modifiers)
        if translated is None:
            return
        depressed, keyval, name, keycode, group, is_modifier, modifiers = translated
        if self.key_handled_as_shortcut(window, name, modifiers, depressed):
            return
        if keycode<=0:
            log.debug("key_action(%s,%s,%s) translated keycode is %s, ignoring it", event, window, depressed, keycode)
            return
        log.debug("key_action(%s,%s,%s) modifiers=%s, name=%s, state=%s, keyval=%s, string=%s, keycode=%s", event, window, depressed, modifiers, name, event.state, event.keyval, event.string, keycode)
        wid = self._window_to_id[window]
        self.send(["key-action", wid, nn(name), depressed, modifiers, keyval, nn(event.string), nn(keycode), group, is_modifier])
        if self.keyboard_sync and self.key_repeat_delay>0 and self.key_repeat_interval>0:
            self._key_repeat(wid, depressed, name, keyval, keycode)

    def _key_repeat(self, wid, depressed, name, keyval, keycode):
        """ this method takes care of scheduling the sending of
            "key-repeat" packets to the server so that it can
            maintain a consistent keyboard state.
        """
        #we keep track of which keys are still pressed in a dict,
        if keycode==0:
            key = name
        else:
            key = keycode
        if not depressed and key in self.keys_pressed:
            """ stop the timer and clear this keycode: """
            log.debug("key repeat: clearing timer for %s / %s", name, keycode)
            gobject.source_remove(self.keys_pressed[key])
            del self.keys_pressed[key]
        elif depressed and key not in self.keys_pressed:
            """ we must ping the server regularly for as long as the key is still pressed: """
            #TODO: we can have latency measurements (see ping).. use them?
            LATENCY_JITTER = 100
            MIN_DELAY = 5
            delay = max(self.key_repeat_delay-LATENCY_JITTER, MIN_DELAY)
            interval = max(self.key_repeat_interval-LATENCY_JITTER, MIN_DELAY)
            log.debug("scheduling key repeat for %s: delay=%s, interval=%s (from %s and %s)", name, delay, interval, self.key_repeat_delay, self.key_repeat_interval)
            def send_key_repeat():
                modifiers = self.get_current_modifiers()
                self.send_now(["key-repeat", wid, name, keyval, keycode, modifiers])
            def continue_key_repeat(*args):
                #if the key is still pressed (redundant check?)
                #confirm it and continue, otherwise stop
                log.debug("continue_key_repeat for %s / %s", name, keycode)
                if key in self.keys_pressed:
                    send_key_repeat()
                    return  True
                else:
                    del self.keys_pressed[key]
                    return  False
            def start_key_repeat(*args):
                #if the key is still pressed (redundant check?)
                #confirm it and start repeat:
                log.debug("start_key_repeat for %s / %s", name, keycode)
                if key in self.keys_pressed:
                    send_key_repeat()
                    self.keys_pressed[key] = gobject.timeout_add(interval, continue_key_repeat)
                else:
                    del self.keys_pressed[key]
                return  False   #never run this timer again
            log.debug("key repeat: starting timer for %s / %s with delay %s and interval %s", name, keycode, delay, interval)
            self.keys_pressed[key] = gobject.timeout_add(delay, start_key_repeat)

    def clear_repeat(self):
        for timer in self.keys_pressed.values():
            gobject.source_remove(timer)
        self.keys_pressed = {}

    def query_xkbmap(self):
        self.xkbmap_layout, self.xkbmap_variant, self.xkbmap_variants = self._client_extras.get_layout_spec()
        self.xkbmap_print, self.xkbmap_query = self._client_extras.get_keymap_spec()
        self.xkbmap_keycodes = self._client_extras.get_gtk_keymap()
        self.xkbmap_mod_meanings, self.xkbmap_mod_managed, self.xkbmap_mod_pointermissing = self._client_extras.get_keymap_modifiers()
        log.debug("layout=%s, variant=%s", self.xkbmap_layout, self.xkbmap_variant)
        log.debug("print=%s, query=%s", self.xkbmap_print, self.xkbmap_query)
        log.debug("keycodes=%s", str(self.xkbmap_keycodes)[:80]+"...")
        log.debug("xkbmap_mod_meanings: %s", self.xkbmap_mod_meanings)

    def _keys_changed(self, *args):
        log.debug("keys_changed")
        self._keymap = gtk.gdk.keymap_get_default()
        if not self._keymap_changing:
            self._keymap_changing = True
            gobject.timeout_add(500, self._do_keys_changed, True)

    def _do_keys_changed(self, sendkeymap=False):
        self._keymap_changing = False
        self.query_xkbmap()
        self._modifier_map = self._client_extras.grok_modifier_map(gtk.gdk.display_get_default(), self.xkbmap_mod_meanings)
        log.debug("do_keys_changed() modifier_map=%s" % self._modifier_map)
        if sendkeymap:
            if self.xkbmap_layout:
                self.send_layout()
            self.send_keymap()

    def send_layout(self):
        self.send(["layout-changed", nn(self.xkbmap_layout), nn(self.xkbmap_variant)])

    def send_keymap(self):
        self.send(["keymap-changed", self.get_keymap_properties()])

    def get_keymap_properties(self):
        props = {"modifiers" : self.get_current_modifiers()}
        for x in ["xkbmap_print", "xkbmap_query", "xkbmap_mod_meanings",
              "xkbmap_mod_managed", "xkbmap_mod_pointermissing", "xkbmap_keycodes"]:
            props[x] = nn(getattr(self, x))
        return  props

    def send_focus(self, wid):
        self.send(["focus", wid, self.get_current_modifiers()])

    def update_focus(self, wid, gotit):
        log("update_focus(%s,%s) _focused=%s", wid, gotit, self._focused)
        if gotit and self._focused is not wid:
            self.clear_repeat()
            self.send_focus(wid)
            self._focused = wid
        if not gotit and self._focused is wid:
            self.clear_repeat()
            self.send_focus(0)
            self._focused = None

    def get_current_modifiers(self):
        (_, _, current_mask) = gtk.gdk.get_default_root_window().get_pointer()
        return self.mask_to_names(current_mask)

    def mask_to_names(self, mask):
        mn = mask_to_names(mask, self._modifier_map)
        names = self._client_extras.current_modifiers(mn)
        return  names

    def send_positional(self, packet):
        self._protocol.source.queue_positional_packet(packet)

    def send_mouse_position(self, packet):
        self._protocol.source.queue_mouse_position_packet(packet)

    def make_hello(self, challenge_response=None):
        capabilities = XpraClientBase.make_hello(self, challenge_response)
        for k,v in self.get_keymap_properties().items():
            capabilities[k] = v
        capabilities["xkbmap_layout"] = nn(self.xkbmap_layout)
        capabilities["xkbmap_variant"] = nn(self.xkbmap_variant)
        capabilities["clipboard"] = self.clipboard_enabled
        capabilities["notifications"] = self._client_extras.can_notify()
        capabilities["modifiers"] = self.get_current_modifiers()
        root_w, root_h = gtk.gdk.get_default_root_window().get_size()
        capabilities["desktop_size"] = [root_w, root_h]
        key_repeat = self._client_extras.get_keyboard_repeat()
        if key_repeat:
            delay_ms,interval_ms = key_repeat
            capabilities["key_repeat"] = (delay_ms,interval_ms)
        capabilities["keyboard_sync"] = self.keyboard_sync and (key_repeat is not None)
        if self.mmap_file:
            capabilities["mmap_file"] = self.mmap_file
            capabilities["mmap_token"] = self.mmap_token
        #these should be turned into options:
        capabilities["cursors"] = True
        capabilities["bell"] = True
        #this is to keep compatibility with v0.0.7.36 only
        #and will be removed shortly
        capabilities["keyboard_as_properties"] = True
        capabilities["damage_sequence"] = True
        capabilities["rowstride"] = True
        capabilities["ping"] = True
        capabilities["png_window_icons"] = "png" in ENCODINGS
        return capabilities

    def send_ping(self):
        self.send(["ping", long(1000*time.time())])

    def _process_ping_echo(self, packet):
        (echoedtime, l1, l2, l3, cl) = packet[1:6]
        diff = long(1000*time.time()-echoedtime)
        self.server_latency.append(diff)
        self.server_load = (l1, l2, l3)
        if cl>=0:
            self.client_latency.append(cl)
        log("ping echo server load=%s, measured client latency=%s", self.server_load, cl)

    def _process_ping(self, packet):
        echotime = packet[1]
        try:
            (fl1, fl2, fl3) = os.getloadavg()
            l1,l2,l3 = long(fl1*1000), long(fl2*1000), long(fl3*1000)
        except:
            l1,l2,l3 = 0,0,0
        sl = -1
        if len(self.server_latency)>0:
            sl = self.server_latency[-1]
        self.send(["ping_echo", echotime, l1, l2, l3, sl])

    def send_jpeg_quality(self, q):
        assert q>0 and q<100
        self.jpegquality = q
        self.send(["jpeg-quality", self.jpegquality])

    def send_refresh(self, wid):
        self.send(["buffer-refresh", wid, True, 95])
        self._refresh_requested = True

    def send_refresh_all(self):
        log.debug("Automatic refresh for all windows ")
        self.send_refresh(-1)

    def _process_hello(self, packet):
        capabilities = packet[1]
        self.server_capabilities = capabilities
        if not self.session_name:
            self.session_name = capabilities.get("session_name", "Xpra")
        try:
            import glib
            glib.set_application_name(self.session_name)
        except ImportError, e:
            log.warn("glib is missing, cannot set the application name, please install glib's python bindings: %s", e)
        self._remote_version = capabilities.get("version") or capabilities.get("__prerelease_version")
        if not is_compatible_with(self._remote_version):
            self.quit()
            return
        #figure out the maximum actual desktop size and use to
        #calculate the maximum size of a packet (a full screen update packet)
        root_w, root_h = gtk.gdk.get_default_root_window().get_size()
        self.server_actual_desktop_size = capabilities.get("actual_desktop_size")
        maxw, maxh = root_w, root_h
        try:
            server_w, server_h = self.server_actual_desktop_size
            maxw = max(root_w, server_w)
            maxh = max(root_h, server_h)
        except:
            pass
        assert maxw>0 and maxh>0 and maxw<32768 and maxh<32768, "problems calculating maximum desktop size: %sx%s" % (maxw, maxh)
        #full screen at 32bits times 4 for safety
        self._protocol.max_packet_size = maxw*maxh*4*4
        log("set maximum packet size to %s", self._protocol.max_packet_size)
        self.server_desktop_size = capabilities.get("desktop_size")
        assert self.server_desktop_size
        avail_w, avail_h = self.server_desktop_size
        if avail_w<root_w or avail_h<root_h:
            log.warn("Server's virtual screen is too small -- "
                     "(server: %sx%s vs. client: %sx%s)\n"
                     "You may see strange behavior.\n"
                     "Please see "
                     "https://xpra.org/trac/ticket/10"
                     % (avail_w, avail_h, root_w, root_h))
        self.server_randr = capabilities.get("resize_screen", False)
        log.debug("server has randr: %s", self.server_randr)
        if self.server_randr:
            display = gtk.gdk.display_get_default()
            i=0
            while i<display.get_n_screens():
                screen = display.get_screen(i)
                screen.connect("size-changed", self._screen_size_changed)
                i += 1
        e = capabilities.get("encoding")
        if e and e!=self.encoding:
            log.debug("server is using %s encoding" % e)
            self.encoding = e
        self.notifications_enabled = capabilities.get("notifications", False)
        clipboard_server_support = capabilities.get("clipboard", True)
        self.clipboard_enabled = clipboard_server_support and self._client_extras.supports_clipboard()
        self.mmap_enabled = self.supports_mmap and self.mmap_file and capabilities.get("mmap_enabled")
        if self.mmap_enabled:
            log.info("mmap enabled using %s", self.mmap_file)
            if self.compression_level!=0:
                log.info("disabling compression")
                self.compression_level = 0
        self.send_deflate_level()
        self.server_start_time = capabilities.get("start_time", -1)
        self.server_platform = capabilities.get("platform")
        self.toggle_cursors_bell_notify = capabilities.get("toggle_cursors_bell_notify")

        #the server will have a handle on the mmap file by now, safe to delete:
        self.clean_mmap()
        #ui may want to know this is now set:
        self.emit("clipboard-toggled")
        self.key_repeat_delay, self.key_repeat_interval = capabilities.get("key_repeat", (-1,-1))
        self.emit("handshake-complete")
        if clipboard_server_support:
            #from now on, we will send a message to the server whenever the clipboard flag changes:
            self.connect("clipboard-toggled", self.send_clipboard_enabled_status)

    def send_notify_enabled(self):
        if self.toggle_cursors_bell_notify:
            self.send(["set-notify", self.notifications_enabled])

    def send_bell_enabled(self):
        if self.toggle_cursors_bell_notify:
            self.send(["set-bell", self.bell_enabled])

    def send_cursors_enabled(self):
        if self.toggle_cursors_bell_notify:
            self.send(["set-cursors", self.cursors_enabled])

    def send_deflate_level(self):
        self.send(["set_deflate", self.compression_level])

    def send_clipboard_enabled_status(self, *args):
        self.send(["set-clipboard-enabled", self.clipboard_enabled])

    def set_encoding(self, encoding):
        assert encoding in ENCODINGS
        assert encoding in self.server_capabilities.get("encodings", ["rgb24"])
        self.encoding = encoding
        self.send(["encoding", encoding])

    def _screen_size_changed(self, *args):
        root_w, root_h = gtk.gdk.get_default_root_window().get_size()
        log.debug("sending updated screen size to server: %sx%s", root_w, root_h)
        self.send(["desktop_size", root_w, root_h])

    def _process_new_common(self, packet, override_redirect):
        (wid, x, y, w, h, metadata) = packet[1:7]
        if w<=0 or h<=0:
            log.error("window dimensions are wrong: %sx%s", w, h)
            w = 10
            h = 5
        window = ClientWindow(self, wid, x, y, w, h, metadata, override_redirect)
        self._id_to_window[wid] = window
        self._window_to_id[window] = wid
        window.show_all()

    def _process_new_window(self, packet):
        self._process_new_common(packet, False)

    def _process_new_override_redirect(self, packet):
        self._process_new_common(packet, True)

    def _process_draw(self, packet):
        (wid, x, y, width, height, coding, data, packet_sequence, rowstride) = packet[1:10]
        window = self._id_to_window.get(wid)
        if not window:
            return      #window is already gone!
        window.draw(x, y, width, height, coding, data, rowstride)
        self.pixel_counter.append((time.time(), width*height))
        if packet_sequence:
            self.send_now(["damage-sequence", packet_sequence])

    def _process_cursor(self, packet):
        (_, new_cursor) = packet
        cursor = None
        if len(new_cursor)>0:
            (_, _, w, h, xhot, yhot, serial, pixels) = new_cursor
            log.debug("new cursor at %s,%s with serial=%s, dimensions: %sx%s, len(pixels)=%s" % (xhot,yhot, serial, w,h, len(pixels)))
            pixbuf = gtk.gdk.pixbuf_new_from_data(pixels, gtk.gdk.COLORSPACE_RGB, True, 8, w, h, w * 4)
            x = max(0, min(xhot, w-1))
            y = max(0, min(yhot, h-1))
            size = gtk.gdk.display_get_default().get_default_cursor_size()
            if size>0 and (size<w or size<h):
                ratio = float(max(w,h))/size
                pixbuf = pixbuf.scale_simple(int(w/ratio), int(h/ratio), gtk.gdk.INTERP_BILINEAR)
                x = int(x/ratio)
                y = int(y/ratio)
            cursor = gtk.gdk.Cursor(gtk.gdk.display_get_default(), pixbuf, x, y)
        for window in self._window_to_id.keys():
            window.window.set_cursor(cursor)

    def _process_bell(self, packet):
        if not self.bell_enabled:
            return
        (wid, device, percent, pitch, duration, bell_class, bell_id, bell_name) = packet[1:9]
        gdkwindow = None
        if wid!=0:
            try:
                gdkwindow = self._id_to_window[wid].window
            except:
                pass
        if gdkwindow is None:
            gdkwindow = gtk.gdk.get_default_root_window()
        log("_process_bell(%s) gdkwindow=%s", packet, gdkwindow)
        self._client_extras.system_bell(gdkwindow, device, percent, pitch, duration, bell_class, bell_id, bell_name)

    def _process_notify_show(self, packet):
        if not self.notifications_enabled:
            return
        (dbus_id, nid, app_name, replaces_nid, app_icon, summary, body, expire_timeout) = packet[1:9]
        log("_process_notify_show(%s)", packet)
        self._client_extras.show_notify(dbus_id, nid, app_name, replaces_nid, app_icon, summary, body, expire_timeout)

    def _process_notify_close(self, packet):
        if not self.notifications_enabled:
            return
        nid = packet[1]
        log("_process_notify_close(%s)", nid)
        self._client_extras.close_notify(nid)

    def _process_window_metadata(self, packet):
        (wid, metadata) = packet[1:3]
        window = self._id_to_window[wid]
        window.update_metadata(metadata)

    def _process_configure_override_redirect(self, packet):
        (wid, x, y, w, h) = packet[1:6]
        window = self._id_to_window[wid]
        window.move_resize(x, y, w, h)

    def _process_lost_window(self, packet):
        wid = packet[1]
        window = self._id_to_window.get(wid)
        if window:
            del self._id_to_window[wid]
            del self._window_to_id[window]
            if window._refresh_timer:
                gobject.source_remove(window._refresh_timer)
            window.destroy()
        if len(self._id_to_window)==0:
            log.debug("last window gone, clearing key repeat")
            self.clear_repeat()

    def process_packet(self, proto, packet):
        packet_type = packet[0]
        assert isinstance(packet_type, str)
        if packet_type.startswith("clipboard-"):
            if self.clipboard_enabled:
                self._client_extras.process_clipboard_packet(packet)
        else:
            XpraClientBase.process_packet(self, proto, packet)

gobject.type_register(XpraClient)
