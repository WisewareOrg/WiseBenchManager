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

    def check_auth_none(self, username: str) -> int:
        return self._server.attempt("none")

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        return self._server.attempt("publickey")

    def check_channel_request(self, kind: str, chanid: int) -> int:
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel: paramiko.Channel, command: bytes) -> bool:
        threading.Thread(target=self._server.answer, args=(channel, command.decode())).start()
        return True


class ScriptedSshServer:
    """Answers each command from `script` (stdout, exit) and logs it.

    `accepts` names the auth methods that succeed: `publickey` (any key) and `none`. Every
    attempt is logged in `attempts` as `(method, succeeded)`.
    """

    def __init__(
        self, script: dict[str, tuple[str, int]], accepts: frozenset[str] = frozenset({"publickey"})
    ) -> None:
        self.script = script
        self.accepts = accepts
        self.commands: list[str] = []
        self.attempts: list[tuple[str, bool]] = []
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

    def attempt(self, method: str) -> int:
        succeeded = method in self.accepts
        self.attempts.append((method, succeeded))
        return paramiko.AUTH_SUCCESSFUL if succeeded else paramiko.AUTH_FAILED

    def answer(self, channel: paramiko.Channel, command: str) -> None:
        self.commands.append(command)
        stdout, status = self.script.get(command, ("", 127))
        channel.sendall(stdout.encode())
        channel.send_exit_status(status)
        channel.shutdown_write()

    def close(self) -> None:
        self._listener.close()
