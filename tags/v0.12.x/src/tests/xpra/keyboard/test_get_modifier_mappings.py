#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (C) 2011-2013 Antoine Martin <antoine@devloop.org.uk>

import pygtk
pygtk.require('2.0')

from wimpiggy.lowlevel import get_modifier_mappings		#@UnresolvedImport

def main():
	mappings = get_modifier_mappings()
	print("mappings=%s" % mappings)
	print("")
	for k,v in mappings.items():
		print("%s\t\t:\t%s" % (k, v))


if __name__ == "__main__":
	main()
