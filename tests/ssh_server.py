"""A loopback ssh server answering exec requests from a script and logging every command."""

from __future__ import annotations

import socket
import threading

import paramiko


class _Session(paramiko.ServerInterface):
    def __init__(self, server: ScriptedSshServer) -> None:
        self._server = server

    def get_allowed_auths(self, username: str) -> str:
        return "publickey"

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_request(self, kind: str, chanid: int) -> int:
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel: paramiko.Channel, command: bytes) -> bool:
        threading.Thread(target=self._server.answer, args=(channel, command.decode())).start()
        return True


class ScriptedSshServer:
    """Accepts any public key, answers each command from `script` (stdout, exit), logs it."""

    def __init__(self, script: dict[str, tuple[str, int]]) -> None:
        self.script = script
        self.commands: list[str] = []
        self.host_key = paramiko.RSAKey.generate(2048)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(client)
            transport.add_server_key(self.host_key)
            transport.start_server(server=_Session(self))

    def answer(self, channel: paramiko.Channel, command: str) -> None:
        self.commands.append(command)
        stdout, status = self.script.get(command, ("", 127))
        channel.sendall(stdout.encode())
        channel.send_exit_status(status)
        channel.shutdown_write()

    def close(self) -> None:
        self._listener.close()
