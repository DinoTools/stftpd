#!/usr/bin/env python2
import errno
import logging
import os

from stftpd import Server, ClientConnection

logging.basicConfig(
    level=logging.DEBUG
)


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:  # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


class Test(ClientConnection):
    def get_local_filename(self, filename):
        # Some clients send a leading /
        filename = filename.lstrip("/")
        filename = os.path.join(self.server.root_path, self.socket[0], filename)
        filename = os.path.realpath(filename)
        if not filename.startswith(self.server.root_path):
            raise Exception("File not in the root path")
        return filename

s = Server(root_path="dst/", port=5000, client_cls=Test)
s.run()
