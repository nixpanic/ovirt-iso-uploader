#!/usr/bin/python

import sys
from glfs_api import GlfsApi

source = sys.argv[1]
host = sys.argv[2]
volume = sys.argv[3]
dest = sys.argv[4]

fs = GlfsApi(host, volume)
fs.upload(source, "%s/%s" % (dest, source))
fs.umount()
