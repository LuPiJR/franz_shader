"""Support for Elero cover components with time-based position tracking."""

__version__ = "3.2.1"

import logging
import time
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.cover import (ATTR_POSITION, ATTR_TILT_POSITION,
                                                CoverEntity,
                                                CoverEntityFeature)
from homeassistant.components.light import PLATFORM_SCHEMA
from homeassistant.const import (CONF_COVERS, CONF_DEVICE_CLASS, CONF_NAME, 
                                 STATE_CLOSED, STATE_CLOSING, STATE_OPEN,
                                   STATE_OPENING, STATE_UNKNOWN)

import custom_components.elero as elero
from custom_components.elero import (
                                        CONF_TRANSMITTER_SERIAL_NUMBER,
                                        INFO_BLOCKING,
                                        INFO_BOTTOM_POS_STOP_WICH_INT_POS,
                                        INFO_BOTTOM_POSITION_STOP,
                                        INFO_INTERMEDIATE_POSITION_STOP,
                                        INFO_MOVING_DOWN, INFO_MOVING_UP,
                                        INFO_NO_INFORMATION, INFO_OVERHEATED,
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

REQUIREMENTS = []

# Other HASS components that should be setup before the platform is loaded.
DEPENDENCIES = ["elero"]

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
        vol.Optional('travel_time_up', default=5): vol.Coerce(int),  # Add this line for travel_time_up
        vol.Optional('travel_time_down', default=5): vol.Coerce(int),  # Add this line for travel_time_down
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
        
        # Retrieve travel times from the configuration
        travel_time_up = cover_conf.get('travel_time_up', 15)
        travel_time_down = cover_conf.get('travel_time_down', 15)
        _LOGGER.debug(f"Travel times - Up: {travel_time_up}s, Down: {travel_time_down}s")
        
        covers.append(
            EleroCover(
                hass,
                transmitter,
                cover_conf.get(CONF_NAME),
                cover_conf.get(CONF_CHANNEL),
                cover_conf.get(CONF_DEVICE_CLASS),
                cover_conf.get(CONF_SUPPORTED_FEATURES),
                travel_time_up=travel_time_up,
                travel_time_down=travel_time_down
            )
        )

    add_devices(covers, True)



# Travel Status Enum
class TravelStatus(Enum):
    DIRECTION_UP = 1
    DIRECTION_DOWN = 2
    STOPPED = 3

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
        _LOGGER.warning(f"logger warning from position: {self._last_known_position} to position: {self._travel_to_position} and remaining time is {remaining_travel_time}")

        if remaining_travel_time == 0:
            _LOGGER.warning("Remaining travel time is zero, returning last known position")
            return self._last_known_position
        _LOGGER.warning(f"Last known position timestamp {self._last_known_position_timestamp}")
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
    """Representation of a Elero cover device with time-based position tracking and error handling."""

    def __init__(
        self, hass, transmitter, name, channel, device_class, supported_features, travel_time_up, travel_time_down
    ):
        """Initialize a Elero cover."""
        self.hass = hass
        self._transmitter = transmitter
        self._name = name
        self._channel = channel
        self._device_class = ELERO_COVER_DEVICE_CLASSES[device_class]
        self._supported_features = 0
        for feature in supported_features:
            self._supported_features |= SUPPORTED_FEATURES[feature]

        # Initialize TravelCalculator for time-based position tracking
        self.travel_calculator = TravelCalculator(travel_time_down, travel_time_up)

        self._position = POSITION_CLOSED  # Assuming the cover starts closed
        self.travel_calculator.set_position(self._position)  # Set initial position in calculator

        self._available = self._transmitter.set_channel(
            self._channel, self.response_handler
        )

        # States
        self._is_opening = None  # Boolean indicating if it's opening
        self._is_closing = None  # Boolean indicating if it's closing
        self._closed = None  # Boolean for closed state
        self._tilt_position = None  # Current tilt position
        self._state = None  # General state (opening, closing, stopped)
        self._elero_state = None  # State from the transmitter (errors, etc.)
        self._response = dict()  # Holds responses from the transmitter


    @property
    def unique_id(self):
        """
        Gets the unique ID of the cover.
        """
        ser_num = self._transmitter.get_serial_number()
        ch = self._channel
        return f"{ser_num}_{ch}"
        
    @property
    def name(self):
        """Return the name of the cover."""
        return self._name
    
    @property
    def device_class(self):
        """Return the class of this device."""
        return self._device_class
    
    @property
    def supported_features(self):
        """Return the supported features of the cover."""
        return self._supported_features
    
    @property
    def should_poll(self):
        """Return True if entity has to be polled for state.

        Because of you can use other remote control (like MultiTel2)
        next to the HA in your system and the status of the Elero devices
        may change therefore it is necessary to monitor their statuses.
        """
        return True
    
    @property
    def available(self):
        """Return if the entity is available."""
        return self._available
    
    @property
    def current_cover_position(self):
        """Return the current position of the cover."""
        return self.travel_calculator.current_position()

    @property
    def is_closed(self):
        """Return if the cover is closed or not."""
        if self._position is None:
            return None
        return self._position == POSITION_CLOSED

    @property
    def is_opening(self):
        """Return if the cover is opening."""
        return self._is_opening

    @property
    def is_closing(self):
        """Return if the cover is closing."""
        return self._is_closing

    def set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        position = kwargs.get(ATTR_POSITION)
        self.travel_calculator.start_travel(position)
        # Send position command to the Elero device

    def open_cover(self, **kwargs):
        """Open the cover fully."""
        _LOGGER.warning(f"Opening cover: {self._name} on channel {self._channel}")
        self.travel_calculator.start_travel(self.travel_calculator.position_open)
        self._is_opening = True
        self._is_closing = False
        # Open the cover via Elero device

    def close_cover(self, **kwargs):
        """Close the cover fully."""
        _LOGGER.warning(f"Closeing cover: {self._name} on channel {self._channel}")
        self.travel_calculator.start_travel(self.travel_calculator.position_closed)
        self._is_closing = True
        self._is_opening = False
        # Close the cover via Elero device

    def stop_cover(self, **kwargs):
        """Stop the cover."""
        _LOGGER.warning(f"Stopping cover: {self._name} on channel {self._channel}")
        self.travel_calculator.stop()
        self._is_opening = False
        self._is_closing = False
        # Stop the cover via Elero device

    def update(self):
        """Update the current position from the travel calculator."""
        self._position = self.travel_calculator.current_position()

    def response_handler(self, response):
        """Handle callback to the response from the Transmitter."""
        _LOGGER.debug(f"Response received from transmitter: {response}")
        self._response = response
        self.set_states()

    def set_states(self):
        """Set the state of the cover based on the response from the transmitter, focusing on errors."""
        self._elero_state = self._response.get("status")

        if self._elero_state == INFO_NO_INFORMATION:
            self._closed = None
            self._is_closing = None
            self._is_opening = None
            self._state = STATE_UNKNOWN
            self._position = None
            self._tilt_position = None
        elif self._elero_state == INFO_TOP_POSITION_STOP:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_OPEN
            self._position = POSITION_OPEN
            self._tilt_position = POSITION_UNDEFINED
        elif self._elero_state == INFO_BOTTOM_POSITION_STOP:
            self._closed = True
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_CLOSED
            self._position = POSITION_CLOSED
            self._tilt_position = POSITION_UNDEFINED
        elif self._elero_state == INFO_INTERMEDIATE_POSITION_STOP:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_INTERMEDIATE
            self._position = POSITION_INTERMEDIATE
            self._tilt_position = POSITION_INTERMEDIATE
        elif self._elero_state == INFO_TILT_VENTILATION_POS_STOP:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_TILT_VENTILATION
            self._position = POSITION_TILT_VENTILATION
            self._tilt_position = POSITION_TILT_VENTILATION
        elif self._elero_state == INFO_START_TO_MOVE_UP:
            self._closed = False
            self._is_closing = False
            self._is_opening = True
            self._state = STATE_OPENING
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._elero_state == INFO_START_TO_MOVE_DOWN:
            self._closed = False
            self._is_closing = True
            self._is_opening = False
            self._state = STATE_CLOSING
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._elero_state == INFO_MOVING_UP:
            self._closed = False
            self._is_closing = False
            self._is_opening = True
            self._state = STATE_OPENING
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._elero_state == INFO_MOVING_DOWN:
            self._closed = False
            self._is_closing = True
            self._is_opening = False
            self._state = STATE_CLOSING
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._elero_state == INFO_STOPPED_IN_UNDEFINED_POSITION:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_UNDEFINED
            self._position = POSITION_UNDEFINED
            self._tilt_position = POSITION_UNDEFINED
        elif self._elero_state == INFO_TOP_POS_STOP_WICH_TILT_POS:
            self._closed = False
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_TILT_VENTILATION
            self._position = POSITION_TILT_VENTILATION
            self._tilt_position = POSITION_TILT_VENTILATION
        elif self._elero_state == INFO_BOTTOM_POS_STOP_WICH_INT_POS:
            self._closed = True
            self._is_closing = False
            self._is_opening = False
            self._state = STATE_INTERMEDIATE
            self._position = POSITION_INTERMEDIATE
            self._tilt_position = POSITION_INTERMEDIATE
        elif self._elero_state in (INFO_BLOCKING, INFO_OVERHEATED, INFO_TIMEOUT):
            self._closed = None
            self._is_closing = None
            self._is_opening = None
            self._state = STATE_UNKNOWN
            self._position = None
            self._tilt_position = None
            t = self._transmitter.get_serial_number()
            r = self._response["status"]
            _LOGGER.error(
                f"Transmitter: '{t}' ch: '{self._channel}'  error response: '{r}'."
            )
        elif self._elero_state in (INFO_SWITCHING_DEVICE_SWITCHED_ON, INFO_SWITCHING_DEVICE_SWITCHED_OFF):
            self._closed = None
            self._is_closing = None
            self._is_opening = None
            self._state = STATE_UNKNOWN
            self._position = None
            self._tilt_position = None
        else:
            self._closed = None
            self._is_closing = None
            self._is_opening = None
            self._state = STATE_UNKNOWN
            self._position = None
            self._tilt_position = None
            t = self._transmitter.get_serial_number()
            r = self._response["status"]
            _LOGGER.error(
                f"Transmitter: '{t}' ch: '{self._channel}' "
                f"unhandled response: '{r}'."
            )
