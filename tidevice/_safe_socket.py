# coding: utf-8
# codeskyblue 2020/06/03
#

__all__ = ['SafeStreamSocket', 'PlistSocket']

import logging
import os
import plistlib
import socket
import ssl
import struct
import threading
import typing
import weakref
from typing import Any, Union

from ._proto import PROGRAM_NAME
from ._utils import set_socket_timeout
from .exceptions import *

logger = logging.getLogger(PROGRAM_NAME)

_n = [0]
_nlock = threading.Lock()

def get_uniq_id() -> int:
    with _nlock:
        _n[0] += 1
        return _n[0]


class SafeStreamSocket:
    def __init__(self, addr: Union[str, tuple, socket.socket,
                                   Any]):
        """
        Args:
            addr: can be /var/run/usbmuxd or (localhost, 27015)
        """
        self._id = get_uniq_id()
        self._sock = None
        self._name = None

        if isinstance(addr, socket.socket):
            self._sock = addr
        else:
            if isinstance(addr, str):
                if ':' in addr:
                    host, port = addr.split(":", 1)
                    addr = (host, int(port))
                    family = socket.AF_INET
                elif os.path.exists(addr):
                    family = socket.AF_UNIX
                else:
                    raise MuxError("socket unix:{} unable to connect".format(addr))
            else:
                family = socket.AF_INET
            self._sock = socket.socket(family, socket.SOCK_STREAM)
            self._sock.connect(addr)
        
        self._sock_gclist = [self._sock]

        def _cleanup(socks: typing.List[socket.socket]):
            _id = str(self.id)
            if self.name:
                _id = self.name + ":" + str(self.id)
            logger.debug("CLOSE(%s)", _id)
            for sock in socks:
                sock.close()

        self._finalizer = weakref.finalize(self, _cleanup, self._sock_gclist)
    
    def close(self):
        self._finalizer()
        
    @property
    def closed(self) -> bool:
        return not self._finalizer.alive

    @property
    def id(self) -> int:
        return self._id

    @property
    def name(self) -> str:
        return self._name
    
    @name.setter
    def name(self, new_name: str):
        self._name = new_name

    def get_socket(self) -> socket.socket:
        return self._sock

    def recv(self, bufsize: int = 4096) -> bytes:
        return self._sock.recv(bufsize)

    def recvall(self, size: int) -> bytearray:
        buf = bytearray()
        while len(buf) < size:
            chunk = self._sock.recv(size - len(buf))
            if not chunk:
                raise MuxError("socket connection broken")
            buf.extend(chunk)
        return buf

    def sendall(self, data: Union[bytes, bytearray]) -> int:
        with set_socket_timeout(self._sock, 10):
            return self._sock.sendall(data)

    def switch_to_ssl(self, pemfile):
        """ wrap socket to SSLSocket """
        # logger.debug("Switch to ssl")
        assert os.path.isfile(pemfile)
        self._dup_sock = self._sock.dup()
        self._sock_gclist.append(self._dup_sock)
        
        # https://docs.python.org/zh-cn/3/library/ssl.html#ssl.SSLContext
        context = ssl.SSLContext(ssl.PROTOCOL_TLS)
        try:
            context.set_ciphers("ALL:@SECLEVEL=0") # fix md_too_weak error
        except ssl.SSLError:
            # ignore: no ciphers can be selected.
            pass
        context.load_cert_chain(pemfile, keyfile=pemfile)
        context.check_hostname = False
        ssock = context.wrap_socket(self._sock, server_hostname="iphone.localhost")
        
        self._sock = ssock

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class PlistSocket(SafeStreamSocket):
    def __init__(self, addr: str, tag: int = 0):
        super().__init__(addr)
        if isinstance(addr, PlistSocket):
            self._tag = addr._tag
            self._first = addr._first
        else:
            self._tag = tag
            self._first = True
        self.prepare()

    def prepare(self):
        pass

    def is_secure(self):
        return isinstance(self._sock, ssl.SSLSocket)

    def send_packet(self, payload: dict, message_type: int = 8):
        """
        Args:
            payload: required

            # The following args only used in the first request
            message_type: 8 (Plist)
            tag: int
        """
        #if self.is_secure():
        #    logger.debug(secure_text + " send: %s", payload)
        #else:
        logger.debug("SEND(%d): %s", self.id, payload)

        body_data = plistlib.dumps(payload)
        if self._first:  # first package
            length = 16 + len(body_data)
            header = struct.pack(
                "IIII", length, 1, message_type,
                self._tag)  # version: 1, request: 8(?), tag: 1(?)
        else:
            header = struct.pack(">I", len(body_data))
        self.sendall(header + body_data)

    def recv_packet(self, header_size=None) -> dict:
        if self._first or header_size == 16:  # first receive
            header = self.recvall(16)
            (length, version, resp, tag) = struct.unpack("IIII", header)
            length -= 16  # minus header length
            self._first = False
        else:
            header = self.recvall(4)
            (length, ) = struct.unpack(">I", header)

        body_data = self.recvall(length)
        payload = plistlib.loads(body_data)
        if 'PairRecordData' in payload:
            logger.debug("Recv pair record data ...")
        else:
            # if self.is_secure():
            #    logger.debug(secure_text + " recv" + Color.END + ": %s",
            #                 payload)
            # else:
            logger.debug("RECV(%d): %s", self.id, payload)
        return payload

    def send_recv_packet(self, payload: dict) -> dict:
        self.send_packet(payload)
        return self.recv_packet()


class PlistSocketProperty:
    def __init__(self, psock: PlistSocket):
        self._psock = psock
        self.prepare()
        self._finalizer = weakref.finalize(self, self.psock.close)
    
    @property
    def psock(self) -> PlistSocket:
        return self._psock
    
    def prepare(self):
        pass

    def send_packet(self, payload: dict, message_type: int = 8):
        return self.psock.send_packet(payload, message_type)
    
    def recv_packet(self, header_size=None) -> dict:
        return self.psock.recv_packet(header_size)
    
    def close(self):
        self._finalizer()
    
    @property
    def closed(self) -> bool:
        return not self._finalizer.alive
    
    