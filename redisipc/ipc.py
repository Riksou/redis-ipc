"""
Copyright (C) 2021-present  AXVin

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""


import os
import json
import logging
from typing import Any, Coroutine, Dict, Callable, List, Optional, TypedDict, Union
import asyncio

from asyncio.events import AbstractEventLoop
from redis.asyncio import Redis

logger = logging.getLogger('redis-ipc')

__all__ = (
    'JSON',
    'IPC',
    'IPCRouter',
    'Handler',
)


JSON = Optional[Union[str, float, bool, List['JSON'], Dict[str, 'JSON']]]
Handler = Callable[[Optional[JSON]], Coroutine[Any, Any, JSON]]


class _BaseIPCMessage(TypedDict, total=False):
    op: str
    data: JSON
    nonce: str
    required_identity: str


class IPCMessage(_BaseIPCMessage):
    sender: str


class IPCRouter:
    async def router_load(self) -> None:
        pass

    async def router_unload(self) -> None:
        pass


def random_hex(_bytes: int = 16) -> str:
    return os.urandom(_bytes).hex()


class IPC:
    def __init__(
        self,
        pool: Redis,
        loop: Optional[AbstractEventLoop] = None,
        channel: str = "ipc:1",
        identity: Optional[str] = None,
        error_handler: Optional[Callable[[Exception, JSON], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        self.redis = pool
        self.channel_address = channel
        self.identity = identity or random_hex()
        self.error_handler = error_handler
        self.loop = loop or asyncio.get_running_loop()
        self.channel = None
        self.routers = []
        self.handlers: Dict[str, Handler] = {
            method.replace("handle_", ""): getattr(self, method)
            for method in dir(self)
            if method.startswith("handle_")
        }
        self.nonces: Dict[str, asyncio.Future[JSON]] = {}
        logger.info(
            f"Created an IPC instance with identity: {self.identity!r} and {len(self.handlers)} handlers"
        )

    def add_router(self, router: IPCRouter) -> None:
        self.routers.append(router)

        for method in dir(router):
            if method.startswith("handle_"):
                self.handlers[method.replace("handle_", "")] = getattr(router, method)

    def add_handler(self, name: str, func: Handler) -> None:
        self.handlers[name] = func
        logger.info(f"Added logger named {name!r}")

    def remove_handler(self, name: str) -> None:
        del self.handlers[name]
        logger.debug(f"Removed logger named {name!r}")

    async def publish(
        self, op: str, *, required_identity: Optional[str] = None, nonce: Optional[str] = None, **data: JSON
    ) -> None:
        """
        A normal publish to the current channel
        with no expectations of any returns
        """
        message: IPCMessage = {
            "op": op,
            "data": data,
            "sender": self.identity,
            # "nonce": nonce,
        }
        if nonce:
            message["nonce"] = nonce
        if required_identity:
            message["required_identity"] = required_identity
        logger.debug(f"Published {message}")
        await self.redis.publish(self.channel_address, json.dumps(message))

    async def get(
        self, op: str, *, timeout: int = 5, required_identity: Optional[str] = None, **data: JSON
    ) -> JSON:
        """
        An IPC call to get a response back

        Parameters:
        -----------
        op: str
            The operation to call on the other processes
        timeout: int
            How long to wait for a response
            default 5 seconds
        required_identity: str
            The identity of the sender that should send the response
            set it to None to use the first response received from any identity
        data: kwargs
            The data to be sent

        Returns:
        --------
        dict:
            The data sent by the first response

        Raises:
        -------
        asyncio.errors.TimeoutError:
            when timeout runs out
        RuntimeError:
            when the ipc class has not been started beforehand
        """
        if self.channel is None:
            raise RuntimeError(f"Must run {self.__class__.__name__}.start to use this method!")
        nonce = random_hex()
        future: asyncio.Future[JSON] = self.loop.create_future()
        self.nonces[nonce] = future

        try:
            await self.publish(op, nonce=nonce, required_identity=required_identity, **data)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            del self.nonces[nonce]

    async def _run_handler(self, handler: Handler, nonce: Optional[str], message: JSON = None) -> None:
        try:
            if message:
                resp = await handler(message)
            else:
                resp = await handler()  # type: ignore

            if resp and nonce:
                data: IPCMessage = {
                    'nonce': nonce,
                    'sender': self.identity,
                    'data': resp,
                }
                resp = json.dumps(data)
                await self.redis.publish(self.channel_address, resp)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.error_handler is not None:
                await self.error_handler(e, message)
            else:
                raise e from None

    async def ensure_channel(self) -> None:
        if self.channel is None:
            pubsub = self.channel = self.redis.pubsub()
            await pubsub.subscribe(self.channel_address)

    async def listen_ipc(self) -> None:
        try:
            await self.ensure_channel()
            if self.channel is None:
                raise Exception("Could not subscribe to redis channel")
            async for msg in self.channel.listen():
                if msg.get("type") != "message":
                    continue
                message: IPCMessage = json.loads(msg.get('data'))
                logging.debug(f"Received message: {message}")
                op = message.get("op")
                nonce = message.get("nonce")
                sender = message.get("sender")
                data = message.get('data')
                required_identity = message.get('required_identity')
                if op is None and sender != self.identity and nonce is not None and nonce in self.nonces:
                    future = self.nonces.get(nonce)
                    if future and future.done() is False:
                        future.set_result(data)
                    continue

                handler = self.handlers.get(op)  # type: ignore
                if handler:
                    if required_identity and self.identity != required_identity:
                        continue
                    wrapped = self._run_handler(handler, message=data, nonce=nonce)
                    asyncio.create_task(wrapped, name=f"redis-ipc: {op}")
        except asyncio.CancelledError:
            if self.channel:
                await self.channel.unsubscribe(self.channel_address)

    async def start(self) -> None:
        """
        Starts the IPC server
        """
        await self.listen_ipc()

    async def close(self) -> None:
        """
        Close the IPC reciever
        """
        if self.channel:
            await self.channel.unsubscribe(self.channel_address)
