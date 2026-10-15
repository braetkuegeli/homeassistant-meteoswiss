"""Robust download of the MeteoSwiss app forecast.

Since 2026-10-14 the forecast endpoint answers the very same request at
random with XML instead of JSON, whatever the Accept header says (measured:
with ``Accept: application/json`` still about one answer in three is XML).
hamsclientfork calls ``.json()`` on the answer unconditionally and fails with
``JSONDecodeError: Expecting value: line 1 column 1``, see
https://github.com/Rudd-O/homeassistant-meteoswiss/issues/93.

We ask for JSON, look at the Content-Type of each answer and retry a few
times when it is not JSON. If every attempt came back as XML, the last XML
answer is translated into the shape of the JSON answer, so the rest of the
integration never sees the difference.
"""

import datetime
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests
from hamsclientfork import meteoSwissClient
from hamsclientfork.client import JSON_FORECAST_URL

_LOGGER = logging.getLogger(__name__)

ATTEMPTS = 3
RETRY_PAUSE = 0.5  # seconds between attempts
REQUEST_TIMEOUT = 5  # seconds per attempt

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML"
    ", like Gecko) Chrome/1337 Safari/537.36",
}

# Fields that are lists in the JSON answer. In XML an empty list and a null
# value look the same (an empty element), so these need to be named.
_LIST_FIELDS = frozenset(
    {
        "regionForecast",
        "warningsOverview",
        "precipitation10m",
        "precipitationMax10m",
        "precipitationMin10m",
        "precipitationMax1h",
        "precipitationMean1h",
        "precipitationMin1h",
        "temperatureMax1h",
        "temperatureMean1h",
        "temperatureMin1h",
    }
)

_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?")
# The XML carries instants as ISO text, the JSON as epoch milliseconds.
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")


def _leaf(text: str | None) -> Any:
    """Convert the text of an XML leaf to the JSON value it stands for."""
    if text is None or not text.strip():
        return None
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    if text in ("true", "false"):
        return text == "true"
    if _INSTANT.fullmatch(text):
        instant = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(instant.timestamp() * 1000)
    return text


def _node(element: ET.Element) -> Any:
    """Convert an XML element to the JSON value it stands for.

    A JSON list is written as an element whose children all repeat its own
    tag: ``<warningsOverview><warningsOverview>…</warningsOverview>…``.
    """
    children = list(element)
    if children and all(child.tag == element.tag for child in children):
        return [_list_item(child) for child in children]
    if children:
        return {child.tag: _node(child) for child in children}
    if element.tag in _LIST_FIELDS and not (element.text or "").strip():
        return []
    return _leaf(element.text)


def _list_item(element: ET.Element) -> Any:
    children = list(element)
    if children:
        return {child.tag: _node(child) for child in children}
    return _leaf(element.text)


def forecast_from_xml(text: str) -> dict[str, Any]:
    """Translate the XML forecast answer into the JSON forecast answer."""
    root = ET.fromstring(text)
    return {child.tag: _node(child) for child in root}


def _is_json(response: requests.Response) -> bool:
    return "json" in response.headers.get("Content-Type", "")


def _is_xml(response: requests.Response) -> bool:
    return "xml" in response.headers.get("Content-Type", "")


def fetch_forecast(postcode: int | str) -> dict[str, Any]:
    """Download the forecast for a post code, whichever format comes back."""
    url = JSON_FORECAST_URL.format(postcode)
    xml_answer: str | None = None
    with requests.Session() as session:
        session.headers.update(_HEADERS)
        for attempt in range(1, ATTEMPTS + 1):
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if _is_json(response):
                return response.json()
            if not _is_xml(response):
                raise ValueError(
                    f"MeteoSwiss forecast came back as {content_type!r}, "
                    "neither JSON nor XML"
                )
            xml_answer = response.text
            _LOGGER.debug("Forecast attempt %s/%s came back as XML", attempt, ATTEMPTS)
            if attempt < ATTEMPTS:
                time.sleep(RETRY_PAUSE)

    _LOGGER.debug("Forecast only came back as XML, translating it")
    return forecast_from_xml(xml_answer)


class RobustMeteoSwissClient(meteoSwissClient):  # type:ignore[misc]
    """meteoSwissClient whose forecast download copes with XML answers."""

    def get_forecast(self) -> None:
        self._forecast = fetch_forecast(self._postCode)
