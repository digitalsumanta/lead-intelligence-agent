"""Modular discovery providers for the Business Research Engine."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any
import httpx

from app.config import settings

logger = logging.getLogger("lead_agent.business_research.providers")

PLACES_SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELD_MASK = ",".join((
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.googleMapsUri",
    "places.types",
    "places.primaryTypeDisplayName",
))


class MissingProviderConfigurationError(RuntimeError):
    """Raised when an external research provider is invoked without configured credentials."""


class BaseDiscoveryProvider(ABC):
    """Abstract interface for discovery providers."""

    name: str

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if provider credentials and endpoints are configured."""
        ...

    @abstractmethod
    async def discover(
        self,
        industry: str,
        location: str,
        sub_category: str | None = None,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        """Discover candidate businesses matching industry and location."""
        ...


class GooglePlacesDiscoveryProvider(BaseDiscoveryProvider):
    """Business discovery provider backed by Google Places API (New) Text Search."""

    name = "google_places"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 15.0,
        client_factory=httpx.AsyncClient,
    ) -> None:
        self._explicit_key = api_key
        self.timeout = timeout
        self.client_factory = client_factory

    @property
    def api_key(self) -> str:
        if self._explicit_key is not None:
            return self._explicit_key
        return settings.google_places_api_key.strip()

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def discover(
        self,
        industry: str,
        location: str,
        sub_category: str | None = None,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        if not self.is_configured():
            raise MissingProviderConfigurationError(
                "Google Places API key is not configured. Please configure GOOGLE_PLACES_API_KEY."
            )

        query_term = f"{sub_category or industry} in {location}"
        page_size = min(max(count, 1), 20)  # Google Places max page size is 20
        candidates: list[dict[str, Any]] = []

        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": PLACES_FIELD_MASK,
            "Content-Type": "application/json",
        }

        next_page_token: str | None = None

        async with self.client_factory(timeout=self.timeout) as client:
            while len(candidates) < count:
                payload: dict[str, Any] = {
                    "textQuery": query_term,
                    "pageSize": page_size,
                }
                if next_page_token:
                    payload["pageToken"] = next_page_token

                try:
                    response = await client.post(
                        PLACES_SEARCH_TEXT_URL,
                        headers=headers,
                        json=payload,
                    )
                    if response.status_code == 403 or response.status_code == 401:
                        raise MissingProviderConfigurationError(
                            f"Google Places API authentication failed (HTTP {response.status_code}). Verify your API key."
                        )
                    if response.status_code >= 400:
                        logger.error("Google Places search error: %s %s", response.status_code, response.text)
                        break

                    data = response.json()
                    places = data.get("places", [])
                    if not places:
                        break

                    for place in places:
                        display = place.get("displayName", {})
                        name = display.get("text") if isinstance(display, dict) else None
                        if not name:
                            continue

                        address = place.get("formattedAddress") or ""
                        phone = place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber")
                        website = place.get("websiteUri")
                        maps_url = place.get("googleMapsUri")
                        primary_type = (place.get("primaryTypeDisplayName") or {}).get("text") or industry

                        candidates.append({
                            "company_name": name,
                            "website": website,
                            "industry": industry,
                            "sub_category": sub_category or primary_type,
                            "address": address,
                            "phone": phone,
                            "source_url": maps_url or website,
                            "raw_place": place,
                        })

                        if len(candidates) >= count:
                            break

                    next_page_token = data.get("nextPageToken")
                    if not next_page_token or len(candidates) >= count:
                        break

                except httpx.HTTPError as exc:
                    logger.warning("Google Places request failed: %s", exc)
                    break

        return candidates


class MockDiscoveryProvider(BaseDiscoveryProvider):
    """Deterministic mock provider for unit tests, development, and offline validation."""

    name = "mock_discovery"

    def __init__(self, is_enabled: bool = True) -> None:
        self.enabled = is_enabled

    def is_configured(self) -> bool:
        return self.enabled

    async def discover(
        self,
        industry: str,
        location: str,
        sub_category: str | None = None,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        # Generates deterministic candidates for tests
        sub = sub_category or f"{industry} Services"
        results: list[dict[str, Any]] = []
        templates = [
            ("Apex Solutions", "https://apexsolutions.example", "Park Street", "+919830011221"),
            ("Zenith Enterprise", "https://zenithenterprise.example", "Salt Lake", "+919830022332"),
            ("Horizon Tech", "https://horizontech.example", "New Town", None),
            ("Bluebird Studio", "https://bluebirdstudio.example", "Ballygunge", "+919830044554"),
            ("Pinnacle Hub", "https://pinnaclehub.example", "Howrah Maidan", None),
            ("Redwood Associates", "https://redwoodassociates.example", "Alipore", "+919830066776"),
            ("Silverline Group", "https://silverlinegroup.example", "Gariahat", None),
            ("Nexus Digital", "https://nexusdigital.example", "Rajarhat", "+919830088998"),
        ]

        for i in range(min(count, len(templates))):
            name, site, loc_sub, phone = templates[i]
            results.append({
                "company_name": f"{name} {location}",
                "website": site,
                "industry": industry,
                "sub_category": sub,
                "city": location,
                "country": "India",
                "address": f"{loc_sub}, {location}",
                "phone": phone,
                "source_url": f"{site}/about",
                "raw_place": {"displayName": {"text": f"{name} {location}"}, "rating": 4.5},
            })

        return results


class ResearchProviderRegistry:
    """Registry managing available business discovery providers."""

    def __init__(self) -> None:
        self._providers: dict[str, BaseDiscoveryProvider] = {}
        # Register standard providers
        self.register(GooglePlacesDiscoveryProvider())

    def register(self, provider: BaseDiscoveryProvider) -> None:
        self._providers[provider.name] = provider

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)

    def get_provider(self, name: str) -> BaseDiscoveryProvider | None:
        return self._providers.get(name)

    def list_providers(self) -> list[dict[str, Any]]:
        result = []
        for name, provider in self._providers.items():
            result.append({
                "name": name,
                "is_configured": provider.is_configured(),
            })
        return result

    def get_active_provider(self) -> BaseDiscoveryProvider:
        """Return the active configured provider. Raises MissingProviderConfigurationError if none configured."""
        # Prefer mock provider if explicitly registered and enabled (e.g. in test mode)
        mock = self.get_provider("mock_discovery")
        if mock and mock.is_configured():
            return mock

        # Use google_places if configured
        places = self.get_provider("google_places")
        if places and places.is_configured():
            return places

        # No configured provider found
        raise MissingProviderConfigurationError(
            "No external business discovery provider is configured. "
            "Please configure GOOGLE_PLACES_API_KEY in environment or .env."
        )


# Global singleton registry
provider_registry = ResearchProviderRegistry()
