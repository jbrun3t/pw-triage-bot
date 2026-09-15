"""Read and update a Patchwork instance over its REST API.

See https://patchwork.readthedocs.io/en/stable/api/rest/
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from importlib.metadata import version
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from . import Error
from .config import Patchwork

RETRY = Retry(connect=3, backoff_factor=0.5)
USER_AGENT = f"pw-triage-bot/{version('pw-triage-bot')}"
DEV_NULL = "/dev/null"
DEFAULT_PER_PAGE = 100
DEFAULT_TIMEOUT = 30


class DiffError(Error):
    """A diff could not be parsed."""


class PwError(Error):
    """The patchwork instance could not be reached, or answered badly."""


class Client:
    """A session against one patchwork instance."""

    def __init__(
        self,
        config: Patchwork,
        *,
        dry_run: bool = False,
    ) -> None:
        self._dry_run = dry_run
        self._config = config
        self._list_params = {"per_page": DEFAULT_PER_PAGE}

        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._session.mount("https://", HTTPAdapter(max_retries=RETRY))

        if config.token:
            self._session.headers["Authorization"] = f"Token {config.token}"

    @staticmethod
    def _decode(response: requests.Response, url: str) -> dict | list:
        try:
            return response.json()
        except ValueError as e:
            raise PwError(f"{url}: response is not JSON: {e}") from e

    @staticmethod
    def _value(value: object) -> object:
        """Spell one query parameter as patchwork expects it: bools lowercase, datetimes as naive UTC."""
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, datetime.datetime):
            if value.tzinfo is not None:
                value = value.astimezone(datetime.UTC).replace(tzinfo=None)
            return value.isoformat()
        return value

    def _get(self, url: str) -> requests.Response:
        try:
            response = self._session.get(url, timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as e:
            raise PwError(f"{url}: {e}") from e
        return response

    def _list(self, path: str, **params: object) -> Iterator[dict]:
        """Yield every entry of a list endpoint, a page at a time."""
        query = self._list_params | {k: self._value(v) for k, v in params.items() if v is not None}
        url = f"{self._config.url}/{path}/?{urlencode(query)}"
        while url:
            response = self._get(url)
            yield from self._decode(response, url)
            url = response.links.get("next", {}).get("url", "")

    def patch_list(self, **params: object) -> Iterator[dict]:
        return self._list("patches", project=self._config.project, **params)

    def event_list(self, **params: object) -> Iterator[dict]:
        return self._list("events", project=self._config.project, **params)

    def _document(self, path: str) -> dict:
        """Fetch one document, refusing one that belongs to another project."""
        url = f"{self._config.url}/{path}/"
        data = self._decode(self._get(url), url)

        found = (data.get("project") or {}).get("link_name")
        if found != self._config.project:
            raise PwError(f"{url}: belongs to project {found!r}, not {self._config.project!r}")
        return data

    def patch_data(self, patch_id: int) -> dict:
        return self._document(f"patches/{patch_id}")

    def series_data(self, series_id: int) -> dict:
        return self._document(f"series/{series_id}")

    def update(
        self,
        patch_id: int,
        *,
        state: str | None = None,
        archived: bool | None = None,
    ) -> None:
        """Write the settings given on a patch, skipping whatever is None. A dry run writes nothing."""
        body = {
            key: value
            for key, value in (("state", state), ("archived", archived))
            if value is not None
        }
        if not body:
            return
        if self._dry_run:
            return

        url = f"{self._config.url}/patches/{patch_id}/"
        try:
            response = self._session.patch(url, json=body, timeout=DEFAULT_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as e:
            raise PwError(f"{url}: cannot update {body}: {e}") from e
