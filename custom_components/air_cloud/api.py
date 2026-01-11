import aiohttp
import asyncio
import json
import logging
import uuid
from datetime import datetime
from aiohttp import WSMsgType

from .const import HOST_API, URN_AUTH, URN_WHO, URN_WSS, URN_CONTROL, URN_REFRESH_TOKEN

_LOGGER = logging.getLogger(__name__)


def _adjust_swing_from_fan_swing(fan_swing: str) -> int:
    """
    A API retorna 'Adjust Swing must not be null' e espera um INTEGER.
    Mapeamento conservador:
      OFF -> 0
      VERTICAL -> 1
      HORIZONTAL -> 2
      BOTH -> 3
    """
    if not fan_swing:
        return 0
    s = str(fan_swing).upper()
    if s == "VERTICAL":
        return 1
    if s == "HORIZONTAL":
        return 2
    if s == "BOTH":
        return 3
    return 0


class AirCloudApi:
    def __init__(self, login, password):
        self._login = login
        self._password = password
        self._last_token_update = datetime.now()
        self._token = None
        self._ref_token = None
        self._session = aiohttp.ClientSession()

    async def validate_credentials(self):
        try:
            await self.__authenticate()
            return True
        except Exception as e:
            _LOGGER.error("Failed to validate credentials: %s", str(e))
            return False

    async def __authenticate(self):
        authorization = {"email": self._login, "password": self._password}
        async with self._session.post(HOST_API + URN_AUTH, json=authorization) as response:
            await self.__update_token_data(await response.json())
        self._last_token_update = datetime.now()

    async def __refresh_token(self, forced=False):
        now_datetime = datetime.now()
        td = now_datetime - self._last_token_update
        td_minutes = divmod(td.total_seconds(), 60)

        if self._token is None or forced:
            await self.__authenticate()
            return

        # refresh roughly every 9 minutes
        if td_minutes[0] >= 9:
            refresh_body = {"refreshToken": self._ref_token}
            async with self._session.post(HOST_API + URN_REFRESH_TOKEN, json=refresh_body) as response:
                await self.__update_token_data(await response.json())
            self._last_token_update = datetime.now()

    async def __update_token_data(self, response):
        self._token = response["token"]
        self._ref_token = response["refreshToken"]

    def __create_headers(self):
        # IMPORTANTE: este método precisa existir dentro da classe
        return {"Authorization": f"Bearer {self._token}"}

    async def load_family_ids(self):
        await self.__refresh_token()
        async with self._session.get(
            HOST_API + URN_WHO,
            headers=self.__create_headers()
        ) as response:
            response_data = await response.json()
            return [item["familyId"] for item in response_data]

    async def load_climate_data(self, family_id):
        if self._session.closed:
            return []
        await self.__refresh_token()

        async with self._session.ws_connect(URN_WSS, timeout=60) as ws:
            connection_string = (
                "CONNECT\naccept-version:1.1,1.2\nheart-beat:10000,10000\n"
                "Authorization:Bearer {}\n\n\0\n"
                "SUBSCRIBE\nid:{}\ndestination:/notification/{}/{}\nack:auto\n\n\0"
            ).format(
                self._token,
                str(uuid.uuid4()),
                str(family_id),
                str(family_id),
            )
            await ws.send_str(connection_string)

            try:
                attempt = 0
                max_attempts = 10
                response = None

                while attempt < max_attempts:
                    attempt += 1
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)

                    if msg.type == WSMsgType.TEXT:
                        # algumas instalações derrubam o WS e exigem reauth
                        if msg.data.startswith("CONNECTED") and "user-name:" not in msg.data:
                            _LOGGER.warning("Websocket connection failed. Re-authenticating.")
                            await ws.close()
                            await self.__refresh_token(forced=True)
                            return await self.load_climate_data(family_id)

                        if msg.data.startswith("MESSAGE") and "{" in msg.data:
                            response = msg.data
                            break

                    elif msg.type == WSMsgType.CLOSED:
                        _LOGGER.warning("WebSocket connection is closed.")
                        return None

                if not response:
                    _LOGGER.warning("No valid response received from WebSocket.")
                    return None

            except asyncio.TimeoutError:
                _LOGGER.warning("WebSocket connection timed out while receiving data")
                await ws.close()
                return None

        _LOGGER.debug("AirCloud climate data: %s", response)
        message = "{" + response.partition("{")[2].replace("\0", "")
        struct = json.loads(message)
        return struct["data"]

    async def execute_command(self, id, family_id, power, idu_temperature, mode, fan_speed, fan_swing, humidity):
        """
        PATCH OFF:
        - A API exige adjustSwing (Integer) não nulo.
        - Em vários ambientes, OFF também exige iduTemperature (Integer) não nulo.
        """
        if self._session.closed:
            return

        await self.__refresh_token()

        is_off = str(power).upper() == "OFF"

        # OFF: forçar integer para satisfazer "Integer must not be null"
        if is_off and idu_temperature is None:
            idu_temperature = 24  # fallback seguro (int)

        command = {
            "power": power,
            "mode": mode,
            "fanSpeed": fan_speed,
            "fanSwing": fan_swing,
            "adjustSwing": _adjust_swing_from_fan_swing(fan_swing),
        }

        # Para OFF sempre vai entrar (forçado); para COOLING quando setado também entra
        if idu_temperature is not None:
            command["iduTemperature"] = int(idu_temperature)

        if humidity is not None:
            command["humidity"] = humidity

        url = f"{HOST_API}{URN_CONTROL}/{id}?familyId={family_id}"
        _LOGGER.warning("AirCloud CMD -> PUT %s payload=%s", url, command)

        async with self._session.put(
            url,
            headers=self.__create_headers(),
            json=command
        ) as response:
            body = await response.text()
            _LOGGER.warning("AirCloud CMD <- HTTP %s body=%s", response.status, body)

    async def close_session(self):
        await self._session.close()
