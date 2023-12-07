# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import logging

import win32file
import pywintypes
import winerror
import time
import win32pipe
import json
from typing import Dict, Optional
from pywintypes import HANDLE


from openjd.adaptor_runtime._background.server_config import NAMED_PIPE_BUFFER_SIZE

_logger = logging.getLogger(__name__)


class PipeDisconnectedException(Exception):
    """
    Exception raised when a Named Pipe is either broken or not connected.

    Attributes:
        error (pywintypes.error): An error raised by pywin32.
    """

    def __init__(self, error: pywintypes.error):
        self.winerror = error.winerror  # The numerical error code
        self.funcname = error.funcname  # The name of the function that caused the error
        self.strerror = error.strerror  # The human-readable error message

        self.message = f"An error occurred: {error.strerror} (Error code: {error.winerror}) in function {error.funcname }"
        super().__init__(self.message)

    def __str__(self):
        return self.message


class NamedPipeTimeoutError(Exception):
    """A custom error raised on timeouts when waiting for another error."""

    def __init__(self, duration: float, error: Exception):
        """Initialize TimeoutError with original error.

        Args:
            duration (float): The duration waited in seconds.
            error (Exception): The original error that was raised.
        """
        self.error = error
        super().__init__(f"Timeout after {duration}s, original error: {error}")


class NamedPipeHelper:
    """
    Helper class for reading from and writing to Named Pipes in Windows.

    This class provides static methods to interact with Named Pipes,
    facilitating data transmission between the server and the client.
    """

    @staticmethod
    def _handle_pipe_exception(e: pywintypes.error) -> None:
        """
        Handles exceptions related to pipe operations.

        Args:
            e (pywintypes.error): The caught exception.

        Raises:
            PipeDisconnectedException: When the pipe is disconnected, broken, or invalid.
        """
        if e.winerror in [
            winerror.ERROR_BROKEN_PIPE,
            winerror.ERROR_PIPE_NOT_CONNECTED,
            winerror.ERROR_INVALID_HANDLE,
        ]:
            raise PipeDisconnectedException(e)
        else:
            raise

    @staticmethod
    def read_from_pipe(handle: HANDLE) -> str:  # type: ignore
        """
        Reads data from a Named Pipe.

        Args:
            handle (HANDLE): The handle to the Named Pipe.

        Returns:
            str: The data read from the Named Pipe.
        """
        data_parts = []
        while True:
            try:
                return_code, data = win32file.ReadFile(handle, NAMED_PIPE_BUFFER_SIZE)
                data_parts.append(data.decode("utf-8"))
                if return_code == winerror.ERROR_MORE_DATA:
                    continue
                elif return_code == winerror.NO_ERROR:
                    return "".join(data_parts)
                else:
                    raise IOError(
                        f"Got error when reading from the Named Pipe with error code: {return_code}"
                    )
            # Server maybe shutdown during reading.
            except pywintypes.error as e:
                NamedPipeHelper._handle_pipe_exception(e)

    @staticmethod
    def write_to_pipe(handle: HANDLE, message: str) -> None:  # type: ignore
        """
        Writes data to a Named Pipe.

        Args:
            handle (HANDLE): The handle to the Named Pipe.
            message (str): The message to write to the Named Pipe.

        """
        try:
            win32file.WriteFile(handle, message.encode("utf-8"))
        # Server maybe shutdown during writing.
        except pywintypes.error as e:
            NamedPipeHelper._handle_pipe_exception(e)

    @staticmethod
    def establish_named_pipe_connection(pipe_name: str, timeout_in_seconds: float) -> HANDLE:
        """
        Creates a client handle for connecting to a named pipe server.

        This function attempts to establish a connection to a named pipe server.
        It keeps trying until the connection is successful or the specified timeout is exceeded.
        If the server pipe is not available (either not found or busy), it waits and retries.
        Once connected, the pipe is set to message-read mode.

        Args:
            pipe_name (str): The name of the pipe to connect to.
            timeout_in_seconds (float): The maximum time in seconds to wait for the server pipe
                to become available before raising an error.

        Returns:
            HANDLE: A handle to the connected pipe.

        Raises:
            pywintypes.error: If the connection cannot be established within the timeout period
                or due to other errors.

        """
        start_time = time.time()
        # Wait for the server pipe to become available.
        handle = None
        while handle is None:
            try:
                handle = win32file.CreateFile(
                    pipe_name,  # pipe name
                    # Give the read / write permission
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,  # Disable the sharing Mode
                    None,  # TODO: Need to set the security descriptor. Right now, None means default security
                    win32file.OPEN_EXISTING,  # Open existing pipe
                    0,  # No Additional flags
                    None,  # A valid handle to a template file, This parameter is ignored when opening an existing pipe.
                )
            except pywintypes.error as e:
                # NamedPipe server may be not ready,
                # or no additional resource to create new instance and need to wait for previous connection release
                if e.winerror in [winerror.ERROR_FILE_NOT_FOUND, winerror.ERROR_PIPE_BUSY]:
                    duration = time.time() - start_time
                    time.sleep(0.1)
                    # Check timeout limit
                    if duration > timeout_in_seconds:
                        _logger.error(
                            f"NamedPipe Server readiness timeout. Duration: {duration} seconds, "
                            f"Timeout limit: {timeout_in_seconds} seconds."
                        )
                        raise NamedPipeTimeoutError(duration, e)
                    continue
                _logger.error(f"Could not open pipe: {e}")
                raise e

        # Switch to message-read mode for the pipe. This ensures that each write operation is treated as a
        # distinct message. For example, a single write operation like "Hello from client." will be read
        # entirely in one request, avoiding partial reads like "Hello fr".
        win32pipe.SetNamedPipeHandleState(
            handle,  # The handle to the named pipe.
            win32pipe.PIPE_READMODE_MESSAGE,  # Set the pipe to message mode
            # Maximum bytes collected before transmission to the server.
            # 'None' means the system's default value is used.
            None,
            # Maximum time to wait
            # 'None' means the system's default value is used.
            None,
        )

        return handle

    @staticmethod
    def send_named_pipe_request(
        pipe_name: str,
        timeout_in_seconds: float,
        method: str,
        path: str,
        *,
        params: Optional[Dict] = None,
        json_body: Optional[Dict] = None,
    ) -> Dict:
        """
        Sends a request to a named pipe server and receives the response.

        This method establishes a connection to a named pipe server, sends a JSON-formatted request,
        and waits for a response.

        Args:
            pipe_name (str): The name of the pipe to connect to.
            timeout_in_seconds (float): The maximum time in seconds to wait for the server pipe to become available
                before raising an error.
            method (str): The HTTP method type (e.g., 'GET', 'POST').
            path (str): The request path.
            params (dict, optional): Dictionary of URL parameters to append to the path.
            json_body (dict, optional): Dictionary representing the JSON body of the request.

        Returns:
            Dict: The parsed JSON response from the server.

        Raises:
            pywintypes.error: If there are issues in establishing a connection or sending the request.
            json.JSONDecodeError: If there is an error in parsing the server's response.
        """

        handle = NamedPipeHelper.establish_named_pipe_connection(pipe_name, timeout_in_seconds)
        try:
            message_dict = {
                "method": method,
                "path": path,
            }

            if json_body:
                message_dict["body"] = json.dumps(json_body)
            if params:
                message_dict["params"] = json.dumps(params)
            message = json.dumps(message_dict)
            NamedPipeHelper.write_to_pipe(handle, message)
            result = NamedPipeHelper.read_from_pipe(handle)
        finally:
            handle.close()
        return json.loads(result)
