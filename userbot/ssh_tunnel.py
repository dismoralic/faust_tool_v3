import asyncssh
from loguru import logger

class SshTunnel:
    def __init__(self, ssh_config):
        self.ssh_config = ssh_config
        self._connection = None
        self._closed = False

    async def start(self):
        if self._connection:
            return
        try:
            connect_kwargs = {
                "host": self.ssh_config["host"],
                "port": self.ssh_config.get("port", 22),
                "username": self.ssh_config["username"],
            }
            if "password" in self.ssh_config:
                connect_kwargs["password"] = self.ssh_config["password"]
            elif "key_path" in self.ssh_config:
                connect_kwargs["client_keys"] = [self.ssh_config["key_path"]]

            self._connection = await asyncssh.connect(**connect_kwargs)

            await self._connection.forward_local_port(
                listen_host="127.0.0.1",
                listen_port=self.ssh_config["local_port"],
                dest_host="127.0.0.1",
                dest_port=self.ssh_config["remote_port"],
            )
            logger.info(
                f"SSH туннель запущен: 127.0.0.1:{self.ssh_config['local_port']} "
                f"-> {self.ssh_config['host']}:{self.ssh_config['remote_port']}"
            )
        except Exception as e:
            logger.error(f"Не удалось поднять SSH-туннель: {e}")
            raise

    async def stop(self):
        if self._connection and not self._closed:
            self._connection.close()
            await self._connection.wait_closed()
            self._closed = True
            logger.info("SSH туннель закрыт")
