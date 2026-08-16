from threading import Thread, Lock
import socket
import time
import ctypes
import logging
import os

from enum import Enum, auto
import json

logger = logging.getLogger(__name__)

class ProxyIfaceHdrFields(Enum):
    
    UpdaterRouteAddr = 1
    WebAppRouteAddr  = auto()
    MainAppRouteAddr = auto()

class ProxyIfaceHdr(ctypes.Structure):
    _fields_ = [
        ("source", ctypes.c_int),
        ("dest",   ctypes.c_int),
        ("len",    ctypes.c_int)
    ]

class TcpClient:
    def __init__(self, port, host:str, timeout:float):
        super().__init__()
        self.__host = host
        self.__port = port
        self.__socket = None
        self.__timeout = 0


    def open(self, timeout:float) -> bool:
        """
        Opens socket

        Returns:
            bool: _description_
        """
        self.__timeout = timeout
        logger = logging.getLogger()
        logger.info("Opening socket connection at port %s with timeout %s seconds", self.__port, self.__timeout)
        try:
            self.__socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.__socket.connect((self.__host, self.__port))
            self.__socket.settimeout(self.__timeout)
            logger.info("Socket connection established at port %s", self.__port)
        except Exception as e:
            logger.exception("Failed to open socket connection at port %s. Exception: %s", self.__port, e)
            return False
        
        return True


    def close(self) -> None:
        """
        Closes socket
        """
        if self.__socket == None:
            return
        
        logger = logging.getLogger()
        logger.info("Closing socket")

        self.__socket.close()


    def send(self, data:bytes) -> bool:
        """
        Send data over socket

        Args:
            data (bytes): Data to be sent

        Returns:
            bool: _description_
        """
        if (data == None) or (self.__socket == None):
            return False
        
        if (type(data) != bytes):
            logger = logging.getLogger()
            logger.error("ERROR: Invalid data type. Expected bytes, got %s", type(data))
            return False
        
        num_retries : int = 0

        while True:
            try:
                self.__socket.sendall(data)
            except BrokenPipeError:
                if not self.open(5.0):
                    num_retries += 1

                    if num_retries > 5:
                        logging.log(logging.ERROR, "ERROR: Maximum number of retries reached")
                        return False
                    continue
            break
        return True
    

    def read(self) -> bytes:
        data : bytes
        try:
            data = self.__socket.recv(1024)
        except socket.timeout:
            return None
        
        return data
        

class UpdatePipe(TcpClient):
    # Port where the updater daemon listens for commands/progress polling.
    UPDATER_PORT = 8080 #int(os.environ.get("RC_CAR_UPDATER_PORT", "8080"))
    HOST = '127.0.0.1' 

    class commands(Enum):
        INIT_UPDATE   = 0
        READ_PROGRESS = auto()
        END_PROGRESS  = auto()

    def __init__(self, timeout: float = 5.0):
        """Create an UpdatePipe.

        Args:
            timeout: socket timeout in seconds for connect/recv operations (default 5.0).
            updater_port: TCP port for the updater daemon (default RC_CAR_UPDATER_PORT or 5000).
            web_port: caller/web-server port (sent in protocol payloads; default RC_CAR_WEB_PORT or 5000).
        """
        self.updater_port = UpdatePipe.UPDATER_PORT
        self.web_port = int(os.environ.get("RC_CAR_WEB_PORT", "5000"))
        logging.info("UpdatePipe initialized with updater_port=%d, web_port=%d", self.updater_port, self.web_port)
        super().__init__(host=UpdatePipe.HOST, port=self.updater_port, timeout=timeout)
        
        self.__socket = None
        self.timeout = float(timeout)


    def init_connection(self) -> bool:
        logging.log(logging.INFO, "Opening socket port")
        self.__connection_status = self.open(5) # Open the socket
        return self.__connection_status

    def command_main_app(self, state : bool) -> bool:
        if not self.__connection_status:
            return False
        # Command the main app to wind down the rc car since an update is in progress
        command : dict = {
            "state"   : state
        }

        hdr = ProxyIfaceHdr()
        hdr.source = ProxyIfaceHdrFields.WebAppRouteAddr.value
        hdr.dest   = ProxyIfaceHdrFields.MainAppRouteAddr.value
        hdr.len = len(json.dumps(command).encode('utf-8'))
        payload = bytes(hdr) + json.dumps(command).encode('utf-8')
        logging.log(logging.INFO, "Sending command to main app: %s", command)
        ret : bool = self.send(payload)
        if not ret:
            return False
        return True

    def start_update(self, file_path : str) -> bool:
        if not self.__connection_status:
            return False

        logging.log(logging.INFO,"Starting update with file at %s", file_path)        
        msg_out : dict = {
            "port"      : self.web_port,
            "command"   : UpdatePipe.commands.INIT_UPDATE.value,
            "file_path" : file_path
        }

        payload : bytes
        try:
            payload = json.dumps(msg_out).encode('utf-8')
        except Exception as e:
            logging.exception("Failed to serialize update message")
            return False

        hdr = ProxyIfaceHdr()
        hdr.source = ProxyIfaceHdrFields.WebAppRouteAddr.value
        hdr.dest   = ProxyIfaceHdrFields.UpdaterRouteAddr.value
        hdr.len = len(payload)
        payload = bytes(hdr) + payload

        ret : bool = self.send(payload)
        if not ret:
            return False

        data : bytes = self.read()
        if data == None:
            return False

        data = data[ctypes.sizeof(ProxyIfaceHdr):]
        reply : dict

        try:
            reply = json.loads(data.decode('utf-8'))
        except json.JSONDecodeError:
            logging.log(logging.ERROR, "Invalid reply")       
            return False 

        if not reply["status"]:
            return False
        
        return True
    

    def read_state(self) -> tuple:
        if not self.__connection_status:
            return None

        msg_out : dict = {
            "port"    : self.web_port,
            "command" : UpdatePipe.commands.READ_PROGRESS.value
        }

        payload : bytes
        
        hdr = ProxyIfaceHdr()
        hdr.source = ProxyIfaceHdrFields.WebAppRouteAddr.value
        hdr.dest = ProxyIfaceHdrFields.UpdaterRouteAddr.value
        try:
            payload = json.dumps(msg_out).encode('utf-8')
        except Exception as e:
            logging.exception("Failed to serialize update message")
            return None

        hdr.len = len(payload)
        payload = bytes(hdr) + payload

        ret : bool = self.send(payload)
        if not ret:
            return None

        data : bytes = self.read()
        if data is None:
            return None

        data = data[ctypes.sizeof(ProxyIfaceHdr):]

        try:
            reply = json.loads(data.decode('utf-8'))
        except json.JSONDecodeError:
            logging.log(logging.ERROR, "Invalid reply")
            return None
        
        if not reply["status"]:
            return None

        # Print reply 
        if reply["message"] != "":
            logging.log(logging.INFO, "%s", reply["message"])

        return reply["update_status"], reply["message"]
        
