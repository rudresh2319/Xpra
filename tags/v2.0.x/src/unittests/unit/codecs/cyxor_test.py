#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (C) 2015 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import time
import unittest

from xpra.os_util import strtobytes
try:
    from xpra.codecs.xor.cyxor import xor_str       #@UnresolvedImport
except:
    xor_str = None
import binascii
def h(v):
    return binascii.hexlify(v)


class TestHMAC(unittest.TestCase):

    def fail_xor(self, in1, in2):
        try:
            xor_str(in1, in2)
        except:
            return
        raise Exception("xor_str did not fail on %s / %s", h(in1), h(in2))

    def check_xor(self, in1, in2, expected):
        out = xor_str(in1, in2)
        #print("xor_str(%s, %s)=%s" % (h(in1), h(in2), h(out)))
        assert out==expected

    def test_xor_str(self):
        zeroes  = strtobytes(chr(0)*16)
        ones    = strtobytes(chr(1)*16)
        ff      = strtobytes(chr(255)*16)
        fe      = strtobytes(chr(254)*16)
        empty   = b""
        lstr    = b"\0x80"*64
        self.check_xor(zeroes, zeroes, zeroes)
        self.check_xor(ones, ones, zeroes)
        self.check_xor(ff, ones, fe)
        self.check_xor(fe, ones, ff)
        #feed some invalid data:
        self.fail_xor(ones, empty)
        self.fail_xor(empty, zeroes)
        self.fail_xor(lstr, ff)
        self.fail_xor(bool, int)


    def test_large_xor_speed(self):
        start = time.time()
        size = 1*1024*1024       #1MB
        zeroes  = strtobytes(chr(0)*size)
        ones    = strtobytes(chr(1)*size)
        count = 10
        for _ in range(count):
            self.check_xor(zeroes, ones, ones)
        end = time.time()
        if end>start:
            speed = size/(end-start)/1024/1024
            #print("%iMB/s: took %ims on average (%s iterations)" % (speed, 1000*(end-start)/count, count))
            assert speed>0, "running the xor speed test took too long"



def main():
    if xor_str:
        unittest.main()
    else:
        print("no cyxor module found, test skipped")

if __name__ == '__main__':
    main()
