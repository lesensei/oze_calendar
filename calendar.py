"""Oze Calendar"""
import copy
from datetime import datetime, timedelta
import json
import logging
import requests
from urllib.parse import urlparse, urlunparse, urljoin

from .const import DOMAIN

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.calendar import (
    ENTITY_ID_FORMAT,
    PLATFORM_SCHEMA,
    CalendarEventDevice,
    calculate_offset,
    get_date,
    is_offset_reached,
)
from homeassistant.const import (
    CONF_NAME,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.util import Throttle, dt

# from tests.util.test_location import session

PLATFORMS: list[str] = ["calendar"]

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Oze Calendar"
CONF_API_URL = "API URL"
CONF_CALENDARS = "calendars"
CONF_DAYS = "days"

OFFSET = "!!"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        # pylint: disable=no-value-for-parameter
        vol.Required(CONF_URL): vol.Url(),
        vol.Optional(CONF_API_URL): vol.Url(),
        vol.Required(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_CALENDARS, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Inclusive(CONF_USERNAME, "authentication"): cv.string,
        vol.Inclusive(CONF_PASSWORD, "authentication"): cv.string,
        vol.Optional(CONF_DAYS, default=1): cv.positive_int,
    }
)

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=15)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, add_entities
) -> bool:
    """Set up OzeCalendar from a config entry."""

    _LOGGER.debug("Entered async_setup_entry in calendar with config: '%s'", entry)

    parsed_url = urlparse(entry[CONF_URL])
    oze = OzeConnection(
        entry[CONF_URL],
        entry[CONF_USERNAME],
        entry[CONF_PASSWORD],
        api_url=entry[CONF_API_URL]
        or urlunparse(parsed_url._replace(netloc="api-" + parsed_url.netloc)),
    )

    hass.data[DOMAIN][entry.entry_id] = oze

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry, add_entities
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


def setup_platform(hass, config, add_entities, disc_info=None):
    """Set up the Oze Calendar platform."""

    _LOGGER.debug("Entered setup_platform in calendar with config: '%s'", config)

    days = config[CONF_DAYS]
    parsed_url = urlparse(config[CONF_URL])

    oze = OzeConnection(
        config[CONF_URL],
        config[CONF_USERNAME],
        config[CONF_PASSWORD],
        api_url=config.get(
            CONF_API_URL,
            urlunparse(parsed_url._replace(netloc="api-" + parsed_url.netloc)),
        ),
    )

    calendar_devices = []
    for cal in oze.get_calendars(config[CONF_CALENDARS]):
        entity_id = generate_entity_id(ENTITY_ID_FORMAT, cal["uid"], hass=hass)
        calendar_devices.append(
            OzeCalendarEventDevice(
                oze, cal["name"], cal["uid"], entity_id, days, cal["etab"]
            )
        )
        _LOGGER.debug("Added calendar for '%s' with id '%s'", cal["name"], cal["uid"])

    add_entities(calendar_devices, True)


class OzeConnection:
    """A class to manage the connection to an Oze account."""

    def __init__(self, url, username, password, api_url=""):
        """Create the connection."""
        self.url = url
        self.api_url = api_url
        self.username = username
        self.password = password
        self.sess = requests.session()
        self.profil = None
        _LOGGER.debug(
            "OzeConnection initialized for '%s' (and '%s') with user '%s'",
            self.url,
            self.api_url,
            self.username,
        )

    def is_connected(self) -> bool:
        """Check if the session is still valid"""
        if self.sess is None:
            self.sess = requests.session()
            return False
        res = self.sess.get(urljoin(self.api_url, "v1/users/me"))
        _LOGGER.debug(
            "GET on '%s' resulted in status code '%d': '%s'",
            res.request.url,
            res.status_code,
            res.reason,
        )
        if res.status_code == 200:
            return True
        self.sess = requests.session()
        return False

    # async def async_is_connected(self, hass: HomeAssistant) -> bool:
    #    return await hass.async_add_executor_job(
    #        self.is_connected()
    #    )

    def connect(self) -> bool:
        """Connect to Oze, if necessary"""
        if self.is_connected():
            return True
        # Get the necessary cookies (in my testing, both GETs seemed to be needed)
        res = self.sess.get(self.url)
        _LOGGER.debug(
            "GET on '%s' resulted in status code '%d': '%s'",
            res.request.url,
            res.status_code,
            res.reason,
        )
        res = self.sess.get(urljoin(self.url, "my.policy"))
        _LOGGER.debug(
            "GET on '%s' resulted in status code '%d': '%s'",
            res.request.url,
            res.status_code,
            res.reason,
        )
        # POST login information
        res = self.sess.post(
            urljoin(self.url, "my.policy"),
            data={
                "username": self.username,
                "password": self.password,
                "fakepassword": "fake",
                "private": "prive",
                "vhost": "standard",
                "SubmitCreds.x": "189",
                "SubmitCreds.y": "17",
            },
        )
        _LOGGER.debug(
            "POST on '%s' resulted in status code '%d': '%s'",
            res.request.url,
            res.status_code,
            res.reason,
        )
        return self.is_connected()

    def get_calendars(self, calendars):
        """Get all calendars (and filter them if a list has been provided in the configuration)"""
        if not self.is_connected():
            self.connect()
        res = self.sess.get(urljoin(self.api_url, "v1/users/me"))
        _LOGGER.debug(
            "GET on '%s' resulted in status code '%d': '%s'",
            res.request.url,
            res.status_code,
            res.reason,
        )
        if res.status_code == 200:
            _LOGGER.debug("Result was '%s'", res.text)
        info = res.json()
        _LOGGER.debug("JSON result: '%s'", info)
        calendars = []
        for relation in list(info["relations"]):
            if calendars and relation["user"]["prenom"] not in calendars:
                _LOGGER.debug("Ignoring calendar for '%s'", relation["user"]["prenom"])
                continue

            # entity_id = generate_entity_id(ENTITY_ID_FORMAT, device_id, hass=hass)
            self.profil = info["currentProfil"]["codeProfil"]
            calendars.append(
                {
                    "name": relation["user"]["nom"],
                    "uid": relation["user"]["id"],
                    "etab": relation["user"]["uai"],
                }
            )
            return calendars

    def get_oze_events(
        self,
        uid,
        etab,
        start_date: datetime = None,
        end_date: datetime = None,
        days: int = 1,
    ):
        """Get events for a given calendar (uid = student id)"""
        if not self.is_connected():
            self.connect()
        start = start_date or dt.start_of_local_day()
        end = end_date or (dt.start_of_local_day() + timedelta(days=days))
        params = {
            "ctx_profil": self.profil,
            "ctx_etab": etab,
            "aDateDebut": start.astimezone().replace(microsecond=0).isoformat()[:-6]
            + "Z",
            "aDateFin": end.astimezone().replace(microsecond=0).isoformat()[:-6] + "Z",
            "aPupilles": uid,
            "aWithCurrent": "false",
            "aUais": etab,
        }
        cours_url = urljoin(self.api_url, "/v1/cours/me")
        _LOGGER.debug("Getting '%s' with params '%s'", cours_url, json.dumps(params))
        res = self.sess.get(cours_url, params=params)
        if res.status_code != 200:
            _LOGGER.error("Error fetching Oze events for '%s': '%s'", uid, res.reason)
            return
        return res.json()


class OzeCalendarEventDevice(CalendarEventDevice):
    """A device for getting the next class from a Oze Calendar."""

    def __init__(self, oze: OzeConnection, name, uid, entity_id, days, etab):
        """Create the Oze Calendar Event Device."""
        self.data = OzeCalendarData(oze, days, uid, etab)
        self.entity_id = entity_id
        self._event = None
        self._attr_name = name

    @property
    def event(self):
        """Return the next upcoming class."""
        return self._event

    async def async_get_events(self, hass, start_date, end_date):
        """Get all classes in a specific time frame."""
        return await self.data.async_get_events(hass, start_date, end_date)

    def update(self):
        """Update calendar data."""
        self.data.update()
        event = copy.deepcopy(self.data.event)
        if event is None:
            self._event = event
            return
        event = calculate_offset(event, OFFSET)
        self._event = event
        self._attr_extra_state_attributes = {"offset_reached": is_offset_reached(event)}


class OzeCalendarData:
    """Class to utilize the Oze APIs to get next event."""

    def __init__(self, oze: OzeConnection, days, uid, etab):
        """Set up how we are going to request the Oze API."""
        self.oze = oze
        self.days = days
        self.uid = uid
        self.etab = etab
        self.event = None

    async def async_get_events(self, hass: HomeAssistant, start_date, end_date):
        """Get all classes in a specific time frame."""
        # Get classes list from the calendar for current user

        event_list_json = await hass.async_add_executor_job(
            self.oze.get_oze_events, self.uid, self.etab, start_date, end_date
        )

        event_list = []
        for event_json in event_list_json:
            _LOGGER.debug("Processing event '%s'...", event_json)
            data = {
                "uid": event_json["_id"],
                "summary": event_json["matieres"][0]["libelle"],
                "start": self.to_hass_date(event_json["dateDebut"]),
                "end": self.to_hass_date(event_json["dateFin"]),
                "location": event_json["classes"][0]["libelle"]
                if "classes" in event_json and event_json["classes"]
                else "",
                "description": (
                    event_json["profs"][0]["nom"]
                    + " "
                    + event_json["profs"][0]["prenom"]
                )
                if "profs" in event_json and event_json["profs"]
                else "",
            }

            data["start"] = get_date(data["start"]).isoformat()
            data["end"] = get_date(data["end"]).isoformat()

            event_list.append(data)

        return event_list

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Get the latest data."""
        start_of_today = dt.start_of_local_day()
        start_of_tomorrow = dt.start_of_local_day() + timedelta(days=self.days)

        results = self.oze.get_oze_events(
            self.uid,
            self.etab,
            start_date=start_of_today,
            end_date=start_of_tomorrow,
        )
        event_json = next(
            (event_json for event_json in results if (not self.is_over(event_json))),
            None,
        )

        # If no matching event could be found
        if event_json is None:
            _LOGGER.debug(
                "No event found today in the '%d' results for '%s'",
                len(results),
                self.uid,
            )
            self.event = None
            return

        # Populate the entity attributes with the event values
        self.event = {
            "uid": event_json["_id"],
            "summary": event_json["matieres"][0]["libelle"],
            "start": self.to_hass_date(event_json["dateDebut"]),
            "end": self.to_hass_date(event_json["dateFin"]),
            "location": event_json["classes"][0]["libelle"],
            "description": event_json["profs"][0]["nom"]
            + " "
            + event_json["profs"][0]["prenom"],
        }

    @staticmethod
    def is_over(event_json):
        """Return if the event is over."""
        return dt.now() >= OzeCalendarData.get_end_date(event_json)

    @staticmethod
    def to_hass_date(obj):
        """Return datetime in hass format."""
        if isinstance(obj, str):
            obj = datetime.fromisoformat(obj.replace("Z", "+00:00"))
        return {"dateTime": obj.isoformat()}

    @staticmethod
    def to_datetime(obj):
        """Return a datetime."""
        if isinstance(obj, datetime):
            if obj.tzinfo is None:
                # floating value, not bound to any time zone in particular
                # represent same time regardless of which time zone is currently being observed
                return obj.replace(tzinfo=dt.DEFAULT_TIME_ZONE)
            return obj
        return dt.dt.datetime.combine(obj, dt.dt.time.min).replace(
            tzinfo=dt.DEFAULT_TIME_ZONE
        )

    @staticmethod
    def get_end_date(obj):
        """Return the end datetime as determined by dateFin."""
        return OzeCalendarData.to_hass_date(obj.dateFin)
