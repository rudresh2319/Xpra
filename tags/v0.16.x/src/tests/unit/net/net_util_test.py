#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (C) 2011-2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest

from xpra.net.net_util import get_info


class TestVersionUtilModule(unittest.TestCase):

    def test_get_info(self):
        get_info()


def main():
    unittest.main()

if __name__ == '__main__':
    main()
