import logging
import os
import socket
import struct
import sys
import time
import threading

PY2 = sys.version_info[0] == 2
PY3 = sys.version_info[0] == 3

logger = logging.getLogger("tftpd")

ERROR_FILE_NOT_FOUND = 1
ERROR_FILE_OPEN = 2
ERROR_FILE_WRITE = 3
ERROR_FILE_EXISTS = 4
ERROR_UNKNOWN_ID = 5
ERROR_UNKNOWN = 6


class ClientConnection(object):
    error_messages = {
        ERROR_FILE_NOT_FOUND: (1, "File not found"),
        ERROR_FILE_OPEN: (2, "Can not open file"),
        ERROR_FILE_WRITE: (2, "Can not write file"),
        ERROR_FILE_EXISTS: (6, "File already exists"),
        ERROR_UNKNOWN_ID: (5, "Unknown transfer ID"),
        ERROR_UNKNOWN: (4, "Illegal TFTP operation")
    }

    def __init__(self, remote_socket, server):
        self.current_block_number = 1
        self.current_data = None
        self.current_data_length = 0
        self.data_finished = False
        self.fp = None
        self.socket = remote_socket
        self.server = server
        self.watchdog = Watchdog(self)

    def get_local_filename(self, filename):
        # Some clients send a leading /
        filename = filename.lstrip("/")
        filename = os.path.join(self.server.root_path, filename)
        filename = os.path.realpath(filename)
        if not filename.startswith(self.server.root_path):
            raise Exception("File not in the root path")
        return filename

    def process(self, data):
        self.watchdog.reset_timeout()
        op_code = struct.unpack('!H', data[0:2])[0]

        if op_code == 1:
            # Read request
            #          2 bytes    string     1 byte  string  1 byte
            #   RRQ   |  01    |  Filename  |   0  |  Mode  |  0
            filename = self.get_local_filename(bytes.decode(data[2:].split(b'\x00')[0]))
            logger.debug(
                "Read request from:%s:%s, filename:%s",
                self.socket[0],
                self.socket[1],
                filename
            )

            if not os.path.isfile(filename):
                logger.debug(
                    "Requested file not found. Closing session %s:%s",
                    self.socket[0],
                    self.socket[1]
                )
                self.send_error(ERROR_FILE_NOT_FOUND)
                return

            try:
                self.fp = open(filename, "rb")
            except Exception:
                logger.info(
                    "Unable to open file '%s'. Closing session %s:%s",
                    filename,
                    self.socket[0],
                    self.socket[1],
                    exc_info=True
                )
                self.send_error(ERROR_FILE_OPEN)

            data = self.fp.read(512)
            self.current_block_number = 1
            self.current_data_length = len(data)
            self.send(struct.pack(b'!2H', 3, self.current_block_number) + data)

            if len(data) < 512:
                self.data_finished = True

            self.watchdog.start()

        elif op_code == 2:
            # Write request
            #          2 bytes    string    1 byte  string  1 byte
            #   WRQ   |  02   |  Filename  |   0  |  Mode  |   0
            filename = self.get_local_filename(bytes.decode(data[2:].split(b'\x00')[0]))
            logger.debug(
                "Write request from:%s:%s, filename:%s",
                self.socket[0],
                self.socket[1],
                filename
            )

            if os.path.isfile(filename):
                logger.debug(
                    "File already exist. Closing session %s:%s",
                    self.socket[0],
                    self.socket[1]
                )
                self.send_error(ERROR_FILE_EXISTS)
                return

            try:
                self.fp = open(filename, "wb")
            except Exception:
                logger.info(
                    "Unable to open file '%s'. Closing session %s:%s",
                    filename,
                    self.socket[0],
                    self.socket[1],
                    exc_info=True
                )
                self.send_error(ERROR_FILE_OPEN)
                return

            self.current_data_length = 0
            self.current_block_number = 1

            self.send(struct.pack(b'!2H', 4, 0))
            self.watchdog.start()

        elif op_code == 3:
            # Data
            #          2 bytes  2 bytes  n bytes
            #   DATA  | 03    | Block # | Data
            block_number = struct.unpack('!H', data[2:4])[0]
            if block_number != self.current_block_number:
                logger.debug(
                    "Receive wrong block. Resend data. (%s:%s)",
                    self.socket[0],
                    self.socket[1]
                )
                return

            data = data[4:]
            self.current_data_length += len(data)
            try:
                self.fp.write(data)
            except Exception:
                logger.info(
                    "Unable to write data. Closing session %s:%s",
                    self.socket[0],
                    self.socket[1],
                    exc_info=True
                )
                self.send_error(ERROR_FILE_WRITE)
                return

            self.current_block_number += 1
            if self.current_block_number == 65536:
                self.current_block_number = 0

            self.send(struct.pack("!2H", 4, block_number))
            self.watchdog.reset_timeout()

            if len(data) < 512:
                logger.info(
                    "Data receive finished. Bytes: %d, Session: %s:%s",
                    self.current_data_length,
                    self.socket[0],
                    self.socket[1]
                )
                self.clear()

        elif op_code == 4:
            # ACK
            #          2 bytes  2 bytes
            #   ACK   | 04    | Block #
            if self.data_finished:
                logger.info(
                    "Data send finished. Bytes: %d, Session: %s:%s",
                    self.current_data_length,
                    self.socket[0],
                    self.socket[1]
                )
                self.clear()
                return

            block_number = struct.unpack('!H',data[2:4])[0]
            if block_number != self.current_block_number:
                logger.debug(
                    "Receive wrong block. Resend data. (%s:%s)",
                    self.socket[0],
                    self.socket[1]
                )
                return

            try:
                data = self.fp.read(512)
            except Exception:
                data = b""

            data_length = len(data)

            self.current_block_number += 1
            self.current_data_length += data_length
            if self.current_block_number == 65536:
                self.current_block_number = 0

            self.send(struct.pack(b'!2H', 3, self.current_block_number) + data)
            self.watchdog.reset_timeout()

            if data_length < 512:
                self.data_finished = True

        elif op_code == 5:
            # Error
            #          2 bytes  2 bytes        string    1 byte
            #   ERROR | 05    |  ErrorCode |   ErrMsg   |   0  |
            error_code = struct.unpack('!H', data[2:4])[0]
            error_msg = data[4:-1]
            logger.debug(
                "Received error code %d:%s Session closed.(%s:%s)",
                error_code,
                error_msg,
                self.socket[0],
                self.socket[1]
            )
            self.clear()

        else:
            # Unknown op_code
            logger.debug(
                "Unknown op code. Closing session %s:%s",
                self.socket[0],
                self.socket[1]
            )
            self.send_error(ERROR_UNKNOWN)

    def clear(self):
        logging.debug(
            "Clear session %s:%s",
            self.socket[0],
            self.socket[1]
        )
        try:
            if self.fp is not None:
                self.fp.close()
                self.fp = None
        except Exception:
            logger.info("Error closing file", exc_info=True)

        if self.socket in self.server.remote_sockets:
            del self.server.remote_sockets[self.socket]
        self.watchdog.stop()

    def retry_send(self):
        logger.debug(
            "Retry sending data for session %s:%s",
            self.socket[0],
            self.socket[1]
        )
        self.server.socket.sendto(self.current_data, self.socket)

    def send(self, data):
        self.current_data = data
        self.server.socket.sendto(data, self.socket)

    def send_error(self, code, msg=None, clear=True):
        if msg is None:
            (response_code, msg) = self.error_messages[code]
        else:
            response_code = code
        format_string = "!2H%dsB" % len(msg)
        data = struct.pack(format_string, 5, response_code, msg.encode("ASCII"), 0)
        self.server.socket.sendto(data, self.socket)
        if clear:
            self.clear()


class Server(object):
    def __init__(self, host="", port=69, socket_family=socket.AF_INET, root_path=".", client_cls=ClientConnection):
        self.root_path = root_path
        self.socket = socket.socket(socket_family, socket.SOCK_DGRAM)
        self.socket.bind((host, port))
        self.remote_sockets = {}
        self.ClientConnection = client_cls

    def run(self):
        while True:
            try:
                data, remote_socket = self.socket.recvfrom(4096)
                if remote_socket not in self.remote_sockets:
                    self.remote_sockets[remote_socket] = self.ClientConnection(remote_socket, server=self)
                self.remote_sockets[remote_socket].process(data)
            except KeyboardInterrupt as e:
                raise e


class Watchdog(threading.Thread):
    def __init__(self, client_connection):
        threading.Thread.__init__(self)
        self.setDaemon(True)

        self.client_connection = client_connection
        self.event_reset = threading.Event()
        self.event_stop = threading.Event()
        self.server = client_connection.server

    def run(self):
        second_count = 0

        while True:
            if self.event_stop.isSet():
                return

            if second_count >= 25:
                logger.info(
                    "Session timeout. Closing sessing %s:%s",
                    self.client_connection.socket[0],
                    self.client_connection.socket[1]
                )
                self.server.remote_sockets[self.client_connection.socket].clear()
                return
            
            if second_count > 0 and second_count % 5 == 0:
                self.server.remote_sockets[self.client_connection.socket].retry_send()

            if self.event_reset.isSet():
                second_count = 0
                self.event_reset.clear()

            time.sleep(1)
            second_count += 1

    def reset_timeout(self):
        self.event_reset.set()

    def stop(self):
        self.event_stop.set()


def main():
    logging.basicConfig(
        level=logging.WARNING
    )
    s = Server(root_path="files/", port=69)
    s.run()

if __name__ == "__main__":
    main()
