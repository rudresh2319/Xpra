# This file is part of Xpra.
# Copyright (C) 2010 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2011-2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import gtk
import gobject
from xpra.gtk_common.gobject_util import n_arg_signal
from xpra.x11.gtk2.gdk_bindings import (add_event_receiver, remove_event_receiver,  #@UnresolvedImport
                                        cleanup_all_event_receivers,                #@UnresolvedImport
                                        init_x11_filter, cleanup_x11_filter)        #@UnresolvedImport
from xpra.gtk_common.error import xsync

from xpra.log import Logger
log = Logger("x11", "util")


class XRootPropWatcher(gobject.GObject):
    __gsignals__ = {
        "root-prop-changed": (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, (gobject.TYPE_STRING, )),
        "xpra-property-notify-event": n_arg_signal(1),
        }

    def __init__(self, props):
        gobject.GObject.__init__(self)
        self._props = props
        self._root = gtk.gdk.get_default_root_window()
        self._saved_event_mask = self._root.get_events()
        self._root.set_events(self._saved_event_mask | gtk.gdk.PROPERTY_CHANGE_MASK)
        self._own_x11_filter = init_x11_filter()
        add_event_receiver(self._root, self)

    def cleanup(self):
        #this must be called from the UI thread!
        remove_event_receiver(self._root, self)
        self._root.set_events(self._saved_event_mask)
        if self._own_x11_filter:
            #only remove the x11 filter if we initialized it (ie: when running in client)
            try:
                with xsync:
                    cleanup_x11_filter()
            except Exception as e:
                log.error("failed to remove x11 event filter: %s", e)
            #try a few times:
            #errors happen because windows are being destroyed
            #(even more so when we cleanup)
            #and we don't really care too much about this
            for l in (log, log, log, log, log.warn):
                try:
                    with xsync:
                        cleanup_all_event_receivers()
                        #all went well, we're done
                        return
                except Exception as e:
                    l("failed to remove event receivers: %s", e)

    def do_xpra_property_notify_event(self, event):
        log("XRootPropWatcher.do_xpra_property_notify_event(%s)", event)
        if event.atom in self._props:
            self.do_notify(event.atom)

    def do_notify(self, prop):
        log("XRootPropWatcher.do_notify(%s)", prop)
        self.emit("root-prop-changed", prop)

    def notify_all(self):
        for prop in self._props:
            self.do_notify(prop)


gobject.type_register(XRootPropWatcher)
