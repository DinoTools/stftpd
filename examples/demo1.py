#!/usr/bin/env python2
import logging

from stftpd import Server

logging.basicConfig(
    level=logging.DEBUG
)
s = Server(root_path="dst/", port=5000)
s.run()
