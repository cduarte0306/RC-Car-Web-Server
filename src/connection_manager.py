from threading import Thread, Lock
import socket
import time
import ctypes
import logging

from enum import Enum, auto
import json

logger = logging.getLogger(__name__)


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

        try:
            self.__socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.__socket.connect((self.__host, self.__port))
            self.__socket.settimeout(self.__timeout)
        except Exception as e:
            print(e)
            return False
        
        return True


    def close(self) -> None:
        """
        Closes socket
        """
        if self.__socket == None:
            return
        
        print("Closing socket")

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
            print("ERROR: Invalid data type")
            return False
        
        self.__socket.sendall(data)
        return True
    

    def read(self) -> bytes:
        data : bytes
        try:
            data = self.__socket.recv(1024)
        except socket.timeout:
            print("ERROR: Socket timeout")
            return None
        
        return data
        

class UpdatePipe(TcpClient):
    
    WEBSERVER_PORT = 5000
    HOST = '127.0.0.1' 

    class commands(Enum):
        INIT_UPDATE   = 0
        READ_PROGRESS = auto()
        END_PROGRESS  = auto()

    def __init__(self, timeout: float = 5.0):
        """Create an UpdatePipe.

        Args:
            timeout: socket timeout in seconds for connect/recv operations (default 5.0).
        """
        super().__init__(host=UpdatePipe.HOST, port=UpdatePipe.WEBSERVER_PORT, timeout=timeout)        
        
        self.__socket = None
        self.timeout = float(timeout)


    def init_connection(self) -> bool:
        print("Opening socket port")
        self.__connection_status = self.open(5) # Open the socket
        return self.__connection_status

    
    def start_update(self) -> bool:
        if not self.__connection_status:
            return False
        
        msg_out : dict = {
            "port"    : UpdatePipe.WEBSERVER_PORT,
            "command" : UpdatePipe.commands.INIT_UPDATE.value
        }

        print("Starting update...")

        payload : bytes

        try:
            payload = json.dumps(msg_out).encode('utf-8')
        except Exception as e:
            logger.exception("Failed to serialize update message")
            return False
        
        print(payload)
        ret : bool = self.send(payload)
        if not ret:
            return False
        
        data : bytes = self.read()
        if data == None:
            return False
        reply : dict

        try:
            reply = json.loads(data.decode('utf-8'))
        except json.JSONDecodeError:
            print("ERROR: Invalid reply")
        
        if not reply["status"]:
            return False
        
        print(reply)

        return True
    

    def read_state(self) -> float:
        if not self.__connection_status:
            return False
        
        msg_out : dict = {
            "port"    : UpdatePipe.WEBSERVER_PORT,
            "command" : UpdatePipe.commands.READ_PROGRESS.value
        }

        payload : bytes

        try:
            payload = json.dumps(msg_out).encode('utf-8')
        except Exception as e:
            logger.exception("Failed to serialize update message")
            return False
        
        ret : bool = self.send(payload)
        if not ret:
            return False
        
        data : bytes = self.read()

        try:
            reply = json.loads(data.decode('utf-8'))
        except json.JSONDecodeError:
            print("ERROR: Invalid reply")
        
        if not reply["status"]:
            return False
        
        return reply["progress"]
        