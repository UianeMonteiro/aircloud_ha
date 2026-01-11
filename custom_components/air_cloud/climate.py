import asyncio
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_MIDDLE,
    SWING_OFF,
    SWING_VERTICAL,
    SWING_HORIZONTAL,
    SWING_BOTH,
    HVACMode,
    ClimateEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from .const import DOMAIN, API, CONF_TEMP_ADJUST, CONF_TEMP_STEP

_LOGGER = logging.getLogger(__name__)

SUPPORT_FAN = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_MIDDLE, FAN_HIGH]
SUPPORT_SWING = [SWING_OFF, SWING_VERTICAL, SWING_HORIZONTAL, SWING_BOTH]

# Pelo seu relato:
# Resfriamento / Seco / Ventilador / Automático / OFF
SUPPORT_HVAC = [HVACMode.OFF, HVACMode.COOL, HVACMode.DRY, HVACMode.FAN_ONLY, HVACMode.AUTO]

NO_HUMIDITY_VALUE = 2147483647


async def _async_setup(hass, async_add):
    api = hass.data[DOMAIN][API]

    family_ids = await api.load_family_ids()
    _LOGGER.warning("AirCloud: setup_entry families=%s", family_ids)

    for family_id in family_ids:
        family_devices = await api.load_climate_data(family_id)
        _LOGGER.warning("AirCloud: setup_entry family_id=%s devices=%s", family_id, (len(family_devices) if family_devices else 0))

        for device in (family_devices or []):
            async_add([AirCloudClimateEntity(api, device, hass, family_id)], update_before_add=False)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    await _async_setup(hass, async_add_entities)


async def async_setup_entry(hass, config_entry, async_add_devices):
    api = hass.data[DOMAIN][API]
    entities = []
    family_ids = await api.load_family_ids()
    _LOGGER.warning("AirCloud: setup_entry families=%s", family_ids)

    for family_id in family_ids:
        family_devices = await api.load_climate_data(family_id)
        _LOGGER.warning("AirCloud: setup_entry family_id=%s devices=%s", family_id, (len(family_devices) if family_devices else 0))

        for device in (family_devices or []):
            entities.append(AirCloudClimateEntity(api, device, hass, family_id))

    if entities:
        async_add_devices(entities)


class AirCloudClimateEntity(ClimateEntity):
    _enable_turn_on_off_backwards_compatibility = False
    _attr_has_entity_name = True

    def __init__(self, api, device, hass, family_id):
        self._api = api
        self._hass = hass
        self._id = device["id"]
        self._name = device["name"]
        self._vendor_id = device["vendorThingId"]
        self._family_id = family_id

        self._update_lock = False

        # defaults
        self._target_temp = 24  # fallback seguro (API pede integer em alguns cenários)
        self._room_temp = None
        self._power = "OFF"
        self._mode = "AUTO"
        self._fan_speed = "AUTO"
        self._fan_swing = "OFF"
        self._humidity = None

        self.__update_data(device)

        _LOGGER.warning(
            "AirCloud: entity init name=%s id=%s vendor=%s family=%s power=%s mode=%s fan=%s swing=%s",
            self._name, self._id, self._vendor_id, self._family_id,
            self._power, self._mode, self._fan_speed, self._fan_swing
        )

    @property
    def unique_id(self):
        return self._vendor_id

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._vendor_id)},
            "name": self._name,
            "manufacturer": "Hitachi",
            "model": "AirCloud Climate",
        }

    @property
    def extra_state_attributes(self):
        return {"family_id": self._family_id, "air_cloud_id": self._id}

    @property
    def supported_features(self):
        # Temperatura só faz sentido expor para o usuário em COOLING.
        support_flags = (
            ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self._power == "ON" and self._mode == "COOLING":
            support_flags |= ClimateEntityFeature.TARGET_TEMPERATURE
        return support_flags

    @property
    def temperature_unit(self):
        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self):
        return self._room_temp

    @property
    def target_temperature(self):
        # Só expõe algo coerente quando em COOLING
        if self._mode != "COOLING":
            return None
        return self._target_temp

    @property
    def target_temperature_step(self):
        step_data = self._hass.data[DOMAIN].get(CONF_TEMP_STEP, {})
        step = step_data.get(self._id)
        return 0.5 if step is None else step

    @property
    def max_temp(self):
        return 32.0

    @property
    def min_temp(self):
        return 16.0

    @property
    def name(self):
        return self._name

    @property
    def hvac_mode(self):
        if self._power == "OFF":
            return HVACMode.OFF
        if self._mode == "COOLING":
            return HVACMode.COOL
        if self._mode == "FAN":
            return HVACMode.FAN_ONLY
        if self._mode == "DRY":
            return HVACMode.DRY
        if self._mode == "AUTO":
            return HVACMode.AUTO
        return HVACMode.OFF

    @property
    def hvac_modes(self):
        return SUPPORT_HVAC

    @property
    def fan_mode(self):
        if self._fan_speed == "AUTO":
            return FAN_AUTO
        if self._fan_speed == "LV1":
            return FAN_LOW
        if self._fan_speed == "LV2":
            return FAN_MEDIUM
        if self._fan_speed == "LV3":
            return FAN_MIDDLE
        if self._fan_speed == "LV4":
            return FAN_HIGH
        return FAN_AUTO

    @property
    def fan_modes(self):
        return SUPPORT_FAN

    @property
    def swing_mode(self):
        if self._fan_swing == "VERTICAL":
            return SWING_VERTICAL
        if self._fan_swing == "HORIZONTAL":
            return SWING_HORIZONTAL
        if self._fan_swing == "BOTH":
            return SWING_BOTH
        return SWING_OFF

    @property
    def swing_modes(self):
        return SUPPORT_SWING

    async def async_turn_on(self):
        self._update_lock = True
        self._power = "ON"
        _LOGGER.warning("AirCloud: async_turn_on called for id=%s name=%s", self._id, self._name)
        await self.__execute_command(origin="turn_on")

    async def async_turn_off(self):
        self._update_lock = True
        self._power = "OFF"
        _LOGGER.warning("AirCloud: async_turn_off called for id=%s name=%s", self._id, self._name)
        await self.__execute_command(origin="turn_off")

    async def async_set_hvac_mode(self, hvac_mode):
        self._update_lock = True
        _LOGGER.warning("AirCloud: async_set_hvac_mode=%s called for id=%s name=%s", hvac_mode, self._id, self._name)

        if hvac_mode == HVACMode.OFF:
            # CRÍTICO: NÃO setar mode="OFF".
            # A API exige mode válido + temperatura + adjustSwing mesmo ao desligar.
            self._power = "OFF"
            await self.__execute_command(origin="set_hvac_mode:OFF")
            return

        # qualquer outro modo = power ON
        self._power = "ON"

        if hvac_mode == HVACMode.COOL:
            self._mode = "COOLING"
        elif hvac_mode == HVACMode.DRY:
            self._mode = "DRY"
        elif hvac_mode == HVACMode.FAN_ONLY:
            self._mode = "FAN"
        elif hvac_mode == HVACMode.AUTO:
            self._mode = "AUTO"

        await self.__execute_command(origin=f"set_hvac_mode:{hvac_mode}")

    async def async_set_fan_mode(self, fan_mode):
        self._update_lock = True
        _LOGGER.warning("AirCloud: async_set_fan_mode=%s called for id=%s name=%s", fan_mode, self._id, self._name)

        if fan_mode == FAN_AUTO:
            self._fan_speed = "AUTO"
        elif fan_mode == FAN_LOW:
            self._fan_speed = "LV1"
        elif fan_mode == FAN_MEDIUM:
            self._fan_speed = "LV2"
        elif fan_mode == FAN_MIDDLE:
            self._fan_speed = "LV3"
        elif fan_mode == FAN_HIGH:
            self._fan_speed = "LV4"
        else:
            self._fan_speed = "AUTO"

        await self.__execute_command(origin="set_fan_mode")

    async def async_set_swing_mode(self, swing_mode):
        self._update_lock = True
        _LOGGER.warning("AirCloud: async_set_swing_mode=%s called for id=%s name=%s", swing_mode, self._id, self._name)

        if swing_mode == SWING_VERTICAL:
            self._fan_swing = "VERTICAL"
        elif swing_mode == SWING_HORIZONTAL:
            self._fan_swing = "HORIZONTAL"
        elif swing_mode == SWING_BOTH:
            self._fan_swing = "BOTH"
        else:
            self._fan_swing = "OFF"

        await self.__execute_command(origin="set_swing_mode")

    async def async_set_temperature(self, **kwargs):
        # Só permite ajuste de temperatura em COOLING.
        if self._mode != "COOLING" or self._power != "ON":
            return

        self._update_lock = True
        target_temp = kwargs.get(ATTR_TEMPERATURE)
        if target_temp is None:
            return

        self._target_temp = target_temp
        await self.__execute_command(origin="set_temperature")

    async def async_update(self):
        if self._update_lock:
            return

        try:
            devices = await asyncio.wait_for(
                self._api.load_climate_data(self._family_id),
                timeout=10
            )
            if not devices:
                return

            for device in devices:
                if self._id == device["id"]:
                    self.__update_data(device)
                    break
        except asyncio.TimeoutError:
            _LOGGER.warning("AirCloud: async_update timeout for family=%s", self._family_id)
            return

    async def __execute_command(self, origin="unknown"):
        """
        Regras que funcionam com a validação da API:
        - Ao DESLIGAR (power OFF): enviar mode atual (não OFF) + iduTemperature (int) + swing/fan.
        - Ao LIGAR/usar COOLING: enviar iduTemperature.
        - Em AUTO/DRY/FAN: geralmente omitimos iduTemperature para evitar erro,
          mas o OFF exige integer => o api.py vai forçar iduTemperature quando power OFF.
        """
        idu_temp_to_send = None
        if self._power == "ON" and self._mode == "COOLING":
            idu_temp_to_send = self._target_temp

        humidity_to_send = None

        _LOGGER.warning(
            "AirCloud: __execute_command origin=%s id=%s family=%s payload(power=%s mode=%s temp=%s fan=%s swing=%s humidity=%s)",
            origin, self._id, self._family_id,
            self._power, self._mode, idu_temp_to_send, self._fan_speed, self._fan_swing, humidity_to_send
        )

        await self._api.execute_command(
            self._id,
            self._family_id,
            self._power,
            idu_temp_to_send,
            self._mode,
            self._fan_speed,
            self._fan_swing,
            humidity_to_send,
        )

        # pequena espera para backend refletir estado
        await asyncio.sleep(2)
        self._update_lock = False
        await self.async_update()

    def __update_data(self, climate_data):
        self._power = climate_data.get("power", self._power)
        self._mode = climate_data.get("mode", self._mode)

        # target temp: manter fallback 24 se API não retornar
        idu_temp = climate_data.get("iduTemperature")
        if idu_temp is not None:
            self._target_temp = idu_temp

        # room temp + ajuste
        adjust_data = self._hass.data[DOMAIN].get(CONF_TEMP_ADJUST, {})
        temp_adjust = adjust_data.get(self._id, 0.0)

        self._room_temp = climate_data.get("roomTemperature")
        if self._room_temp is not None:
            self._room_temp = self._room_temp + temp_adjust

        self._fan_speed = climate_data.get("fanSpeed", self._fan_speed)
        self._fan_swing = climate_data.get("fanSwing", self._fan_swing)

        humidity = climate_data.get("humidity")
        if humidity is None or humidity == NO_HUMIDITY_VALUE:
            self._humidity = None
        else:
            self._humidity = humidity
