"""Daily forecast from MeteoSwiss Open Government Data (OGD).

The app forecast endpoint is undocumented and changes without notice (see
forecast_fetch).  The daily forecast comes instead from the official local
forecast collection, which holds the same forecast as the app, see
https://opendatadocs.meteoswiss.ch/e-forecast-data/e4-local-forecast-data

A new run is published every hour as one CSV per parameter covering all
points.  The daily files are small (about 200 KB each), so we download the
four we need once per run and keep only the rows of our post code.
"""

import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo

import requests

_LOGGER = logging.getLogger(__name__)

RUN_URL = (
    "https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/"
    "{run:%Y%m%d}-ch/vnut12.lssw.{run:%Y%m%d%H%M}.{parameter}.csv"
)
# Point ids are only unique within a point type; post codes are type 2.
POINT_TYPE_POSTCODE = "2"
# OGD parameter -> field of a regionForecast entry of the app forecast.
PARAMETERS = {
    "jp2000d0": "iconDay",
    "tre200px": "temperatureMax",
    "tre200pn": "temperatureMin",
    "rka150p0": "precipitation",
}
# The newest run appears a few minutes past the hour; older runs stay online.
RUNS_TO_TRY = 3
REQUEST_TIMEOUT = 10  # seconds per file
SWISS_TIME = ZoneInfo("Europe/Zurich")


class RunNotPublished(Exception):
    """The files of a run are not (all) online yet."""


class OgdDailyForecast:
    """Daily forecast for one post code, downloaded once per OGD run."""

    def __init__(self, postcode: int | str) -> None:
        # A post code has point ids <post code>01, <post code>02, ... and
        # mostly <post code>00, the one the app uses.  We take the lowest.
        self._postcode = str(int(postcode))
        self._run: datetime.datetime | None = None
        self._days: list[dict[str, Any]] = []

    def get(self) -> list[dict[str, Any]]:
        """Return the daily forecast from today on, in the app's shape."""
        newest = datetime.datetime.now(datetime.timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        with requests.Session() as session:
            for hours_back in range(RUNS_TO_TRY):
                run = newest - datetime.timedelta(hours=hours_back)
                if run == self._run:
                    break  # the newest published run is already loaded
                try:
                    days = self._download(session, run)
                except RunNotPublished:
                    _LOGGER.debug("OGD run %s not published yet", run)
                    continue
                self._run, self._days = run, days
                _LOGGER.debug("Loaded OGD daily forecast of run %s", run)
                break

        if self._run is None:
            raise ValueError(
                f"No OGD daily forecast published in the last {RUNS_TO_TRY} hours"
            )
        if not self._days:
            raise ValueError(f"OGD forecast has no post code {self._postcode}")
        today = datetime.datetime.now(SWISS_TIME).date().isoformat()
        return [day for day in self._days if day["dayDate"] >= today]

    def _download(
        self, session: requests.Session, run: datetime.datetime
    ) -> list[dict[str, Any]]:
        by_point: dict[str, dict[str, dict[str, str]]] = {}
        for parameter, field in PARAMETERS.items():
            url = RUN_URL.format(run=run, parameter=parameter)
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            # The bucket answers 403, not 404, for files that do not exist.
            if response.status_code in (403, 404):
                raise RunNotPublished(url)
            response.raise_for_status()
            response.encoding = "iso_8859_1"
            for line in response.text.splitlines():
                if not line.startswith(self._postcode):
                    continue
                point_id, point_type, date, value = line.split(";")
                if point_type == POINT_TYPE_POSTCODE and (
                    point_id[:-2] == self._postcode
                ):
                    by_date = by_point.setdefault(point_id, {})
                    by_date.setdefault(date[:8], {})[field] = value

        if not by_point:
            return []
        point_id = min(by_point)
        days = []
        for date, values in sorted(by_point[point_id].items()):
            if len(values) < len(PARAMETERS) or not all(values.values()):
                continue
            icon = int(values["iconDay"])
            days.append(
                {
                    "dayDate": f"{date[:4]}-{date[4:6]}-{date[6:]}",
                    "iconDay": icon,
                    "iconDayV2": icon,
                    "temperatureMax": float(values["temperatureMax"]),
                    "temperatureMin": float(values["temperatureMin"]),
                    "precipitation": float(values["precipitation"]),
                }
            )
        return days
