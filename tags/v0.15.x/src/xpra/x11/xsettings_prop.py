# This file is part of Xpra.
# Copyright (C) 2012-2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""
XSETTINGS

This code deals with:
* extracting data from XSETTINGS into nice python data structures
and
* converting those structures back into XSETTINGS format

It is used by xpra.x11.gtk_x11.prop
"""

import sys
import struct
from xpra.log import Logger
log = Logger("x11", "xsettings")


if sys.version > '3':
    unicode = str           #@ReservedAssignment


#undocumented XSETTINGS endianess values:
LITTLE_ENDIAN = 0
BIG_ENDIAN    = 1
def get_local_byteorder():
    if sys.byteorder=="little":
        return  LITTLE_ENDIAN
    else:
        return  BIG_ENDIAN

#the 3 types of settings supported:
XSettingsTypeInteger = 0
XSettingsTypeString = 1
XSettingsTypeColor = 2

XSettingsNames = {
                XSettingsTypeInteger    : "Integer",
                XSettingsTypeString     : "String",
                XSettingsTypeColor      : "Color",
                }

if sys.version>='3':
    long = int              #@ReservedAssignment


def get_settings(disp, d):
    #parse xsettings according to
    #http://standards.freedesktop.org/xsettings-spec/xsettings-spec-0.5.html
    assert len(d)>=12, "_XSETTINGS_SETTINGS property is too small: %s" % len(d)
    log("get_settings(%s)", list(d))
    byte_order, _, _, _, serial, n_settings = struct.unpack("=BBBBII", d[:12])
    log("get_settings(..) found byte_order=%s (local is %s), serial=%s, n_settings=%s", byte_order, get_local_byteorder(), serial, n_settings)
    settings = []
    pos = 12
    while n_settings>len(settings) and len(d)>0:
        istart = pos
        #parse header:
        setting_type, _, name_len = struct.unpack("=BBH", d[pos:pos+4])
        pos += 4
        #extract property name:
        prop_name = d[pos:pos+name_len]
        pos += (name_len + 0x3) & ~0x3
        #serial:
        assert len(d)>=pos+4, "not enough data (%s bytes) to extract serial (4 bytes needed)" % (len(d)-pos)
        last_change_serial = struct.unpack("=I", d[pos:pos+4])[0]
        pos += 4
        log("get_settings(..) found property %s of type %s, serial=%s", prop_name, XSettingsNames.get(setting_type, "INVALID!"), last_change_serial)
        #extract value:
        if setting_type==XSettingsTypeInteger:
            assert len(d)>=pos+4, "not enough data (%s bytes) to extract int (4 bytes needed)" % (len(d)-pos)
            value = int(struct.unpack("=I", d[pos:pos+4])[0])
            pos += 4
        elif setting_type==XSettingsTypeString:
            assert len(d)>=pos+4, "not enough data (%s bytes) to extract string length (4 bytes needed)" % (len(d)-pos)
            value_len = struct.unpack("=I", d[pos:pos+4])[0]
            assert len(d)>=pos+4+value_len, "not enough data (%s bytes) to extract string (%s bytes needed)" % (len(d)-pos-4, value_len)
            value = d[pos+4:pos+4+value_len]
            pos += 4 + ((value_len + 0x3) & ~0x3)
        elif setting_type==XSettingsTypeColor:
            assert len(d)>=pos+8, "not enough data (%s bytes) to extract color (8 bytes needed)" % (len(d)-pos)
            red, blue, green, alpha = struct.unpack("=HHHH", d[pos:pos+8])
            value = (red, blue, green, alpha)
            pos += 8
        else:
            log.error("invalid setting type: %s, cannot continue parsing XSETTINGS!", setting_type)
            break
        setting = setting_type, prop_name, value, last_change_serial
        log("get_settings(..) %s -> %s", list(d[istart:pos]), setting)
        settings.append(setting)
    log("get_settings(..) settings=%s", settings)
    return  serial, settings

def set_settings(disp, d):
    assert len(d)==2, "invalid format for XSETTINGS: %s" % str(d)
    serial, settings = d
    log("set_settings(%s) serial=%s, %s settings", d, serial, len(settings))
    all_bin_settings = []
    for setting in settings:
        setting_type, prop_name, value, last_change_serial = setting
        prop_name = str(prop_name)
        try:
            log("set_settings(..) processing property %s of type %s", prop_name, XSettingsNames.get(setting_type, "INVALID!"))
            x = struct.pack("=BBH", setting_type, 0, len(prop_name))
            x += struct.pack("="+"s"*len(prop_name), *list(prop_name))
            pad_len = ((len(prop_name) + 0x3) & ~0x3) - len(prop_name)
            x += '\0'*pad_len
            x += struct.pack("=I", last_change_serial)
            if setting_type==XSettingsTypeInteger:
                assert type(value) in (int, long), "invalid value type (int or long wanted): %s" % type(value)
                x += struct.pack("=I", int(value))
            elif setting_type==XSettingsTypeString:
                if type(value)==unicode:
                    value = str(value)
                else:
                    assert type(value)==str, "invalid value type (str wanted): %s" % type(value)
                x += struct.pack("=I", len(value))
                x += struct.pack("="+"s"*len(value), *list(value))
                pad_len = ((len(value) + 0x3) & ~0x3) - len(value)
                x += '\0'*pad_len
            elif setting_type==XSettingsTypeColor:
                red, blue, green, alpha = value
                x = struct.pack("=HHHH", red, blue, green, alpha)
            else:
                log.error("invalid xsetting type: %s, skipped %s", setting_type, prop_name)
                continue
            log("set_settings(..) %s -> %s", setting, list(x))
            all_bin_settings.append(x)
        except Exception as e:
            log.error("Error processing XSettings property %s:", prop_name)
            log.error(" type=%s, value=%s", XSettingsNames.get(setting_type, "INVALID!"), value)
            log.error(" %s", e)
    #header
    v = struct.pack("=BBBBII", get_local_byteorder(), 0, 0, 0, serial, len(all_bin_settings))
    v += "".join(all_bin_settings)  #values
    v += '\0'                       #null terminated
    log("set_settings(%s)=%s", d, list(v))
    return  v


def main():
    from xpra.platform.gui import init as gui_init
    from xpra.platform import init as platform_init,clean
    try:
        platform_init("XSettings")
        gui_init()
        verbose = "-v" in sys.argv or "--verbose" in sys.argv
        if verbose:
            from xpra.log import get_all_loggers
            for x in get_all_loggers():
                x.enable_debug()

        #naughty, but how else can I hook this up?
        import os
        if os.name!="posix":
            print("xsettings require a posix OS")
            return 1

        from xpra.x11.bindings import posix_display_source  #@UnresolvedImport - takes care of hooking up the display
        assert posix_display_source
        from xpra.x11.bindings.window_bindings import X11WindowBindings #@UnresolvedImport
        window_bindings = X11WindowBindings()
        selection = "_XSETTINGS_S0"
        owner = window_bindings.XGetSelectionOwner(selection)
        print("owner(%s)=%#x" % (selection, owner))
        XSETTINGS = "_XSETTINGS_SETTINGS"
        data = window_bindings.XGetWindowProperty(owner, XSETTINGS, XSETTINGS)
        serial, settings = get_settings(None, data)
        print("serial=%s" % serial)
        print("%s settings:" % len(settings))
        for s in settings:
            print(s)
        return 0
    finally:
        clean()


if __name__ == "__main__":
    sys.exit(main())
