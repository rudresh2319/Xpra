# This file is part of Xpra.
# Copyright (C) 2013, 2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.client.gtk_base.gtk_client_base import GTKXpraClient, xor_str

import sys
from gi.repository import GObject               #@UnresolvedImport
from gi.repository import Gtk                   #@UnresolvedImport
from gi.repository import Gdk                   #@UnresolvedImport

from xpra.client.gtk3.client_window import ClientWindow
from xpra.client.gtk3.tray_menu import GTK3TrayMenu
from xpra.log import Logger
log = Logger("gtk", "client")

WIN32 = sys.platform.startswith("win")


class XpraClient(GTKXpraClient):

    ClientWindowClass = ClientWindow

    def gtk_main(self):
        Gtk.main()

    def install_signal_handlers(self):
        #doing so interferes with gtk..
        pass

    def make_hello(self):
        capabilities = GTKXpraClient.make_hello(self)
        if xor_str is not None:
            capabilities["encoding.supports_delta"] = [x for x in ("rgb24", "rgb32") if x in self.get_core_encodings()]
        return capabilities

    def client_type(self):
        return "Python/Gtk3"

    def client_toolkit(self):
        return "gtk3"

    def get_notifier_classes(self):
        ncs = GTKXpraClient.get_notifier_classes(self)
        if not WIN32:
            #for some reason, not included in win32 builds?
            try:
                from xpra.client.gtk3.gtk3_notifier import GTK3_Notifier
                ncs.append(GTK3_Notifier)
            except Exception, e:
                log("failed to load the GTK3 notification class: %s", e)
        return ncs

    def do_get_core_encodings(self):
        cencs = GTKXpraClient.do_get_core_encodings(self)
        for x in ("webp", ):
            if x in cencs:
                cencs.remove(x)
        return cencs


    def get_tray_menu_helper_classes(self):
        tmhc = GTKXpraClient.get_tray_menu_helper_classes(self)
        tmhc.append(GTK3TrayMenu)
        return tmhc

    def make_clipboard_helper(self):
        return None


    def get_root_window(self):
        return Gdk.Screen.get_default().get_root_window()

    def get_root_size(self):
        if WIN32:
            #FIXME: hopefully, we can remove this code once GTK3 on win32 is fixed?
            #we do it the hard way because the root window geometry is invalid on win32:
            #and even just querying it causes this warning:
            #"GetClientRect failed: Invalid window handle."
            display = Gdk.Display.get_default()
            n = display.get_n_screens()
            w, h = 0, 0
            for i in range(n):
                screen = display.get_screen(i)
                w += screen.get_width()
                h += screen.get_height()
        else:
            #the easy way for platforms that work out of the box:
            root = self.get_root_window()
            w, h = root.get_geometry()[2:]
        if w<=0 or h<=0 or w>32768 or h>32768:
            log("Gdk returned invalid root window dimensions: %sx%s", w, h)
            w, h = 1920, 1080
        return w, h

    def set_windows_cursor(self, gtkwindows, new_cursor):
        pass

    def init_opengl(self, enable_opengl):
        self.opengl_enabled = False
        self.opengl_props = {"info" : "GTK3 does not support OpenGL"}


GObject.type_register(XpraClient)
