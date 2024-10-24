"""Support for Elero cover components with time-based position tracking."""

__version__ = "3.2.1"

import logging
import time

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.cover import (
    ATTR_POSITION, 
    ATTR_TILT_POSITION,
    CoverEntity,
    CoverEntityFeature
)
from homeassistant.components.light import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_COVERS, 
    CONF_DEVICE_CLASS, 
    CONF_NAME,
    STATE_CLOSED, 
    STATE_CLOSING, 
    STATE_OPEN,
    STATE_OPENING, 
    STATE_UNKNOWN
)

import custom_components.elero as elero
from custom_components.elero import (
    CONF_TRANSMITTER_SERIAL_NUMBER,
    INFO_BLOCKING,
    INFO_BOTTOM_POS_STOP_WICH_INT_POS,
    INFO_BOTTOM_POSITION_STOP,
    INFO_INTERMEDIATE_POSITION_STOP,
    INFO_MOVING_DOWN, 
    INFO_MOVING_UP,
    INFO_NO_INFORMATION, 
    INFO_OVERHEATED,
    INFO_START_TO_MOVE_DOWN,
    INFO_START_TO_MOVE_UP,
    INFO_STOPPED_IN_UNDEFINED_POSITION,
    INFO_SWITCHING_DEVICE_SWITCHED_OFF,
    INFO_SWITCHING_DEVICE_SWITCHED_ON,
    INFO_TILT_VENTILATION_POS_STOP,
    INFO_TIMEOUT,
    INFO_TOP_POS_STOP_WICH_TILT_POS,
    INFO_TOP_POSITION_STOP
)

from enum import Enum

_LOGGER = logging.getLogger(__name__)

ATTR_ELERO_STATE = "elero_state"

CONF_CHANNEL = "channel"
CONF_SUPPORTED_FEATURES = "supported_features"

ELERO_COVER_DEVICE_CLASSES = {
    "awning": "window",
    "interior shading": "window",
    "roller shutter": "window",
    "rolling door": "garage",
    "venetian blind": "window",
}

# Position slider values
POSITION_CLOSED = 0
POSITION_INTERMEDIATE = 75
POSITION_OPEN = 100
POSITION_TILT_VENTILATION = 25
POSITION_UNDEFINED = 50

# Elero states
STATE_INTERMEDIATE = "intermediate"
STATE_STOPPED = "stopped"
STATE_TILT_VENTILATION = "ventilation/tilt"
STATE_UNDEFINED = "undefined"

# Supported features
SUPPORTED_FEATURES = {
    "close_tilt": CoverEntityFeature.CLOSE_TILT,
    "down": CoverEntityFeature.CLOSE,
    "open_tilt": CoverEntityFeature.OPEN_TILT,
    "set_position": CoverEntityFeature.SET_POSITION,
    "set_tilt_position": CoverEntityFeature.SET_TILT_POSITION,
    "stop_tilt": CoverEntityFeature.STOP_TILT,
    "stop": CoverEntityFeature.STOP,
    "up": CoverEntityFeature.OPEN,
}

ELERO_COVER_DEVICE_CLASSES_SCHEMA = vol.All(
    vol.Lower, vol.In(ELERO_COVER_DEVICE_CLASSES)
)

SUPPORTED_FEATURES_SCHEMA = vol.All(cv.ensure_list, [vol.In(SUPPORTED_FEATURES)])

CHANNEL_NUMBERS_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=1, max=15))

COVER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CHANNEL): CHANNEL_NUMBERS_SCHEMA,
        vol.Required(CONF_DEVICE_CLASS): ELERO_COVER_DEVICE_CLASSES_SCHEMA,
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_SUPPORTED_FEATURES): SUPPORTED_FEATURES_SCHEMA,
        vol.Required(CONF_TRANSMITTER_SERIAL_NUMBER): str,
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_COVERS): vol.Schema({cv.slug: COVER_SCHEMA}), }
)


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the Elero cover platform."""
    covers = []
    covers_conf = config.get(CONF_COVERS, {})
    for _, cover_conf in covers_conf.items():
        transmitter = elero.ELERO_TRANSMITTERS.get_transmitter(
            cover_conf.get(CONF_TRANSMITTER_SERIAL_NUMBER)
        )
        if not transmitter:
            t = cover_conf.get(CONF_TRANSMITTER_SERIAL_NUMBER)
            ch = cover_conf.get(CONF_CHANNEL)
            n = cover_conf.get(CONF_NAME)
            _LOGGER.error(
                f"The transmitter '{t}' of the '{ch}' - '{n}' channel is "
                "non-existent transmitter!"
            )
            continue

        covers.append(
            EleroCover(
                hass,
                transmitter,
                cover_conf.get(CONF_NAME),
                cover_conf.get(CONF_CHANNEL),
                cover_conf.get(CONF_DEVICE_CLASS),
                cover_conf.get(CONF_SUPPORTED_FEATURES),
                travel_time_up=30,  # default travel time, adjust as necessary
                travel_time_down=40  # default travel time, adjust as necessary
            )
        )

    add_devices(covers, True)


# Travel Status Enum
class TravelStatus(Enum):
    DIRECTION_UP = 1
    DIRECTION_DOWN = 2
    STOPPED = 3

# Travel Calculator for time-based position prediction
class TravelCalculator:
    def __init__(self, travel_time_down: float, travel_time_up: float) -> None:
        self.travel_direction = TravelStatus.STOPPED
        self.travel_time_down = travel_time_down
        self.travel_time_up = travel_time_up

        self._last_known_position = None
        self._last_known_position_timestamp = 0.0
        self._position_confirmed = False
        self._travel_to_position = None
        self.position_closed = 100
        self.position_open = 0

    def set_position(self, position: int) -> None:
        self._travel_to_position = position
        self.update_position(position)

    def update_position(self, position: int) -> None:
        self._last_known_position = position
        self._last_known_position_timestamp = time.time()
        if position == self._travel_to_position:
            self._position_confirmed = True

    def stop(self) -> None:
        stop_position = self.current_position()
        if stop_position is None:
            return
        self._last_known_position = stop_position
        self._travel_to_position = stop_position
        self._position_confirmed = False
        self.travel_direction = TravelStatus.STOPPED

    def start_travel(self, _travel_to_position: int) -> None:
        if self._last_known_position is None:
            self.set_position(_travel_to_position)
            return
        self.stop()
        self._last_known_position_timestamp = time.time()
        self._travel_to_position = _travel_to_position
        self._position_confirmed = False
        self.travel_direction = (
            TravelStatus.DIRECTION_DOWN
            if _travel_to_position > self._last_known_position
            else TravelStatus.DIRECTION_UP
        )

    def current_position(self) -> int | None:
        if not self._position_confirmed:
            return self._calculate_position()
        return self._last_known_position

    def _calculate_position(self) -> int | None:
        if self._travel_to_position is None or self._last_known_position is None:
            return self._last_known_position

        relative_position = self._travel_to_position - self._last_known_position
        remaining_travel_time = self.calculate_travel_time(
            from_position=self._last_known_position,
            to_position=self._travel_to_position,
        )
        progress = (
            time.time() - self._last_known_position_timestamp
        ) / remaining_travel_time

        return int(self._last_known_position + relative_position * progress)

    def calculate_travel_time(self, from_position: int, to_position: int) -> float:
        travel_range = to_position - from_position
        travel_time_full = (
            self.travel_time_down if travel_range > 0 else self.travel_time_up
        )
        return travel_time_full * abs(travel_range) / self.position_closed


class EleroCover(CoverEntity):
    """Representation of a Elero cover device with time-based position tracking."""

    def __init__(
        self, hass, transmitter, name, channel, device_class, supported_features, travel_time_up, travel_time_down
    ):
        """Init of a Elero cover."""
        self.hass = hass
        self._transmitter = transmitter
        self._name = name
        self._channel = channel
        self._device_class = ELERO_COVER_DEVICE_CLASSES[device_class]
        self._supported_features = 0
        for f in supported_features:
            self._supported_features |= SUPPORTED_FEATURES[f]

        self.travel_calculator = TravelCalculator(travel_time_down, travel_time_up)

        self._available = self._transmitter.set_channel(
            self._channel, self.response_handler
        )
        self._position = None
        self._is_opening = None
        self._is_closing = None
        self._closed = None
        self._tilt_position = None
        self._state = None
        self._elero_state = None
        self._response = dict()

    # Other existing methods of the class...

    @property
    def current_cover_position(self):
        """Return the current position of the cover."""
        return self.travel_calculator.current_position()

    def set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        position = kwargs.get(ATTR_POSITION)
        self.travel_calculator.start_travel(position)
        # Send position command to the Elero device

    def open_cover(self, **kwargs):
        """Open the cover fully."""
        self.travel_calculator.start_travel_up()
        # Open cover via Elero device

    def close_cover(self, **kwargs):
        """Close the cover fully."""
        self.travel_calculator.start_travel_down()
        # Close cover via Elero device

    def stop_cover(self, **kwargs):
        """Stop the cover."""
        self.travel_calculator.stop()
        # Stop the cover via Elero device

    def update(self):
        """Get the current position from the travel calculator and update."""
        self._position = self.travel_calculator.current_position()

