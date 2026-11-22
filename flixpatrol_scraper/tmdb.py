from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from curl_cffi.requests import Response, Session
from curl_cffi.requests.exceptions import HTTPError, RequestException

from .http_client import format_response_diagnostics, get_with_retries
from .rate_limit import RateLimiter

TMDB_API_BASE_URL = "https://api.themoviedb.org/3"
TMDB_DEFAULT_LANGUAGE = "en-US"
TMDB_DEFAULT_TIMEOUT_SECONDS = 30
TMDB_DEFAULT_MAX_REQUESTS_PER_SECOND = 50

logger = logging.getLogger(__name__)


class TMDBResolverError(RuntimeError):
    """Raised when TMDB matching cannot be performed."""


class _AmbiguousMatchError(TMDBResolverError):
    """More than one work matches the available title information."""

    def __init__(self, matches: tuple[TMDBMatch, ...]) -> None:
        self.matches = matches
        super().__init__(
            ", ".join(
                f"{match.media_type}:{match.id} {match.matched_title!r} ({match.release_date}, "
                f"title-score={match.title_match_score:.2f}, "
                f"popularity={match.popularity:.2f}, votes={match.vote_count})"
                for match in sorted(matches, key=lambda item: item.popularity, reverse=True)
            )
        )


@dataclass(slots=True)
class _InFlightRequest:
    event: threading.Event
    result: Any = None
    exception: BaseException | None = None


@dataclass(frozen=True, slots=True)
class TMDBMatch:
    id: int
    media_type: str
    matched_title: str
    original_title: str | None
    release_date: str | None
    search_endpoint: str
    popularity: float = 0.0
    vote_count: int = 0
    title_match_score: float = 1.0
    vote_average: float = 0.0
    original_language: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "media_type": self.media_type,
            "release_date": self.release_date,
        }


_MatchResult = TMDBMatch | tuple[TMDBMatch, ...] | None


class TMDBResolver:
    """Resolve scraped titles to TMDB IDs using TMDB search endpoints."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        api_key: str | None = None,
        session: Session | None = None,
        timeout_seconds: int = TMDB_DEFAULT_TIMEOUT_SECONDS,
        language: str = TMDB_DEFAULT_LANGUAGE,
        include_adult: bool = False,
        max_requests_per_second: float = TMDB_DEFAULT_MAX_REQUESTS_PER_SECOND,
    ) -> None:
        self.access_token = access_token
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.language = language
        self.include_adult = include_adult
        self.max_requests_per_second = max(1, max_requests_per_second)
        self._shared_session = session
        self._thread_local = threading.local()
        self._search_cache: dict[tuple[str, str, str | None], list[dict[str, Any]]] = {}
        self._match_cache: dict[tuple[Any, ...], _MatchResult] = {}
        self._reported_unmatched: set[
            tuple[Any, ...]
        ] = set()
        self._provider_cache: dict[tuple[str, int, str], frozenset[str]] = {}
        self._country_codes: dict[str, str] | None = None
        self._country_codes_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._search_inflight: dict[tuple[str, str, str | None], _InFlightRequest] = {}
        self._match_inflight: dict[tuple[Any, ...], _InFlightRequest] = {}
        self._provider_inflight: dict[tuple[str, int, str], _InFlightRequest] = {}
        self._rate_limiter = RateLimiter(
            max_requests_per_second=self.max_requests_per_second
        )

        if not self.access_token and not self.api_key:
            raise TMDBResolverError(
                "TMDB credentials are required. Provide TMDB_ACCESS_TOKEN or TMDB_API_KEY."
            )

    @classmethod
    def from_env(
        cls,
        *,
        session: Session | None = None,
        timeout_seconds: int = TMDB_DEFAULT_TIMEOUT_SECONDS,
        language: str = TMDB_DEFAULT_LANGUAGE,
        include_adult: bool = False,
        max_requests_per_second: float = TMDB_DEFAULT_MAX_REQUESTS_PER_SECOND,
    ) -> TMDBResolver | None:
        access_token = os.getenv("TMDB_ACCESS_TOKEN")
        api_key = os.getenv("TMDB_API_KEY")
        if not access_token and not api_key:
            return None
        return cls(
            access_token=access_token,
            api_key=api_key,
            session=session,
            timeout_seconds=timeout_seconds,
            language=language,
            include_adult=include_adult,
            max_requests_per_second=max_requests_per_second,
        )

    @staticmethod
    def _build_session(*, access_token: str | None) -> Session:
        headers = {
            "Accept": "application/json",
            "User-Agent": "flixpatrol-scraper/0.1.0",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        return Session(
            default_encoding="utf-8",
            headers=headers,
        )

    def _get_session(self) -> Session:
        if self._shared_session is not None:
            return self._shared_session

        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._build_session(access_token=self.access_token)
            self._thread_local.session = session
        return session

    def _acquire_inflight(
        self,
        inflight_requests: dict[tuple[Any, ...], _InFlightRequest],
        cache_key: tuple[Any, ...],
    ) -> tuple[_InFlightRequest, bool]:
        with self._cache_lock:
            request = inflight_requests.get(cache_key)
            if request is not None:
                return request, False

            request = _InFlightRequest(event=threading.Event())
            inflight_requests[cache_key] = request
            return request, True

    def _complete_inflight(
        self,
        inflight_requests: dict[tuple[Any, ...], _InFlightRequest],
        cache_key: tuple[Any, ...],
        *,
        result: Any = None,
        exception: BaseException | None = None,
    ) -> None:
        with self._cache_lock:
            request = inflight_requests.pop(cache_key, None)
            if request is None:
                return
            request.result = result
            request.exception = exception
            request.event.set()

    def resolve(
        self,
        title: str,
        *,
        media_hint: str | None = None,
        release_year: str | None = None,
        original_provider: str | None = None,
        streaming_platform: str | None = None,
        region: str | None = None,
        chart_date: str | None = None,
        title_details: Callable[[], tuple[str, str] | None] | None = None,
        fallback: TMDBMatch | None = None,
    ) -> TMDBMatch | None:
        result = self.find_match(
            title,
            media_hint=media_hint,
            release_year=release_year,
            original_provider=original_provider,
            streaming_platform=streaming_platform,
            region=region,
            chart_date=chart_date,
        )
        return self.complete_match(
            title, result, media_hint=media_hint, release_year=release_year,
            original_provider=original_provider, streaming_platform=streaming_platform,
            region=region, chart_date=chart_date,
            title_details=title_details, fallback=fallback,
        )

    def complete_match(
        self,
        title: str,
        result: _MatchResult,
        *,
        media_hint: str | None = None,
        release_year: str | None = None,
        original_provider: str | None = None,
        streaming_platform: str | None = None,
        region: str | None = None,
        chart_date: str | None = None,
        title_details: Callable[[], tuple[str, str] | None] | None = None,
        fallback: TMDBMatch | None = None,
    ) -> TMDBMatch | None:
        if isinstance(result, TMDBMatch):
            return result
        if fallback is not None:
            return fallback
        # A missing match can be caused by FlixPatrol's displayed title differing
        # from TMDB's title. Use the source page's type/year as a second search hint
        # for both ambiguous and completely unmatched titles.
        details = title_details() if title_details is not None else None
        if details is not None:
            detail_media, detail_year = details
            if detail_media in {"movie", "tv"} and media_hint in {None, detail_media}:
                endpoint = f"/search/{detail_media}"
                try:
                    match = self._pick_best_match(
                        normalize_title(title),
                        self._search(endpoint, title, release_year=detail_year),
                        endpoint=endpoint,
                    )
                except (TMDBResolverError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    logger.debug("Could not resolve TMDB match for %r with title details: %s", title, exc)
                else:
                    if match is not None and match.title_match_score >= 0.90:
                        return match

        warning_key = (
            title, media_hint, release_year, original_provider,
            streaming_platform, region, chart_date, result, details,
        )
        with self._cache_lock:
            if warning_key in self._reported_unmatched:
                return None
            self._reported_unmatched.add(warning_key)
        context = (
            f"media={media_hint or 'any'}, year={release_year or 'any'}, "
            f"platform={streaming_platform or original_provider or 'unknown'}, "
            f"region={region or 'unknown'}, chart_date={chart_date or 'unknown'}"
        )
        if result is None:
            logger.warning(
                "Leaving TMDB null for %r (%s): no qualifying TMDB candidates",
                title,
                context,
            )
            return None
        logger.warning(
            "Leaving TMDB null for %r (%s): unresolved candidates: %s",
            title,
            context,
            _AmbiguousMatchError(result),
        )
        return None

    def find_match(
        self,
        title: str,
        *,
        media_hint: str | None = None,
        release_year: str | None = None,
        original_provider: str | None = None,
        streaming_platform: str | None = None,
        region: str | None = None,
        chart_date: str | None = None,
    ) -> _MatchResult:
        provider = normalize_title(original_provider or "")
        platform = _normalize_provider(streaming_platform or "")
        country = normalize_title(region or "")
        endpoint_order = self._build_search_order(media_hint)
        cache_key = (title, media_hint, release_year, provider, platform, country, chart_date)
        with self._cache_lock:
            if cache_key in self._match_cache:
                return self._match_cache[cache_key]

        inflight_request, is_owner = self._acquire_inflight(
            self._match_inflight,
            cache_key,
        )
        if not is_owner:
            inflight_request.event.wait()
            if inflight_request.exception is not None:
                raise inflight_request.exception
            return inflight_request.result

        with self._cache_lock:
            cached_match = self._match_cache.get(cache_key)
            has_cached_match = cache_key in self._match_cache
        if has_cached_match:
            self._complete_inflight(
                self._match_inflight,
                cache_key,
                result=cached_match,
            )
            return cached_match

        try:
            candidates: list[TMDBMatch] = []
            for search_title in _title_search_variants(title):
                normalized_search_title = normalize_title(search_title)
                for endpoint in endpoint_order:
                    results = self._search(endpoint, search_title)
                    try:
                        candidate = self._pick_best_match(
                            normalized_search_title,
                            results,
                            endpoint=endpoint,
                        )
                    except _AmbiguousMatchError as exc:
                        candidates.extend(exc.matches)
                    else:
                        if candidate is not None:
                            candidates.append(candidate)
                    # Search every applicable endpoint; ordering is not match evidence.

            unique_candidates: dict[tuple[str, int], TMDBMatch] = {}
            for candidate in candidates:
                identity = (candidate.media_type, candidate.id)
                current = unique_candidates.get(identity)
                if current is None or candidate.title_match_score > current.title_match_score:
                    unique_candidates[identity] = candidate
            candidates = list(unique_candidates.values())

            typed_candidates = [
                candidate for candidate in candidates if candidate.media_type == media_hint
            ]
            if typed_candidates:
                candidates = typed_candidates
            match = self._pick_ranked_match(tuple(candidates))
            with self._cache_lock:
                self._match_cache[cache_key] = match
            self._complete_inflight(
                self._match_inflight,
                cache_key,
                result=match,
            )
            return match
        except BaseException as exc:
            self._complete_inflight(
                self._match_inflight,
                cache_key,
                exception=exc,
            )
            raise

    def _search(
        self, endpoint: str, query: str, *, release_year: str | None = None
    ) -> list[dict[str, Any]]:
        if endpoint == "/search/multi":
            release_year = None
        cache_key = (endpoint, query, release_year)
        with self._cache_lock:
            if cache_key in self._search_cache:
                return self._search_cache[cache_key]

        inflight_request, is_owner = self._acquire_inflight(
            self._search_inflight,
            cache_key,
        )
        if not is_owner:
            inflight_request.event.wait()
            if inflight_request.exception is not None:
                raise inflight_request.exception
            return inflight_request.result

        with self._cache_lock:
            cached_results = self._search_cache.get(cache_key)
            has_cached_results = cache_key in self._search_cache
        if has_cached_results:
            self._complete_inflight(
                self._search_inflight,
                cache_key,
                result=cached_results,
            )
            return cached_results

        params: dict[str, Any] = {
            "query": query,
            "language": self.language,
            "include_adult": str(self.include_adult).lower(),
            "page": 1,
        }
        if release_year is not None:
            year_param = "first_air_date_year" if endpoint == "/search/tv" else "primary_release_year"
            params[year_param] = release_year
        try:
            payload = self._request_json(endpoint, query, params)
            results = payload.get("results", [])
            filtered_results = [
                result
                for result in results
                if endpoint != "/search/multi" or result.get("media_type") in {"movie", "tv"}
            ]
            with self._cache_lock:
                self._search_cache[cache_key] = filtered_results
            self._complete_inflight(
                self._search_inflight,
                cache_key,
                result=filtered_results,
            )
            return filtered_results
        except BaseException as exc:
            self._complete_inflight(
                self._search_inflight,
                cache_key,
                exception=exc,
            )
            raise

    def _request_json(self, endpoint: str, title: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.api_key:
            params["api_key"] = self.api_key
        try:
            self._rate_limiter.acquire()
            response = get_with_retries(
                self._get_session(),
                f"{TMDB_API_BASE_URL}{endpoint}",
                params=params,
                timeout_seconds=self.timeout_seconds,
            )
        except RequestException as exc:
            raise TMDBResolverError(
                f"TMDB request failed for endpoint {endpoint!r} and query {title!r}: "
                f"{type(exc).__name__}"
            ) from exc
        self._raise_for_bad_response(response, endpoint, title)
        return response.json()

    def _pick_provider_match(
        self,
        matches: tuple[TMDBMatch, ...],
        providers: tuple[str | None, str | None],
        region: str | None,
        title: str,
    ) -> TMDBMatch | None:
        country_code = self._country_code(region, title)
        if country_code is None:
            return None
        for provider in providers:
            expected = _normalize_provider(provider or "")
            if not expected:
                continue
            candidates = [
                match
                for match in matches
                if match.title_match_score >= 0.90
                if expected in self._subscription_providers(match, country_code, title)
            ]
            logger.debug(
                "TMDB availability check for %r: %s matches %d/%d candidates in %s",
                title,
                provider,
                len(candidates),
                len(matches),
                region,
            )
            if len(candidates) == 1:
                logger.debug(
                    "Selected TMDB %s:%s for %r because it is available on %s in %s "
                    "(title=%r, release=%s, title-score=%.2f, popularity=%.2f, votes=%d)",
                    candidates[0].media_type,
                    candidates[0].id,
                    title,
                    provider,
                    region,
                    candidates[0].matched_title,
                    candidates[0].release_date,
                    candidates[0].title_match_score,
                    candidates[0].popularity,
                    candidates[0].vote_count,
                )
                return candidates[0]
        return None

    @staticmethod
    def _pick_ranked_match(matches: tuple[TMDBMatch, ...]) -> TMDBMatch | None:
        """Choose the most popular candidate among safe fuzzy title matches."""
        if not matches:
            return None
        return max(
            matches,
            key=lambda match: (
                match.popularity,
                match.title_match_score,
                match.vote_count,
            ),
        )

    def _country_code(self, region: str | None, title: str) -> str | None:
        normalized_region = normalize_title(region or "")
        if not normalized_region or normalized_region in {"global", "the world"}:
            return None
        with self._country_codes_lock:
            if self._country_codes is None:
                countries = self._request_json("/configuration/countries", title, {})
                self._country_codes = {
                    normalize_title(str(name)): str(country["iso_3166_1"])
                    for country in countries
                    if isinstance(country, dict)
                    and country.get("iso_3166_1")
                    for name in (country.get("english_name"), country.get("native_name"))
                    if name
                }
            aliases = {
                "united states": "united states of america",
                "salvador": "el salvador",
                "swaziland": "eswatini",
            }
            lookup_region = aliases.get(normalized_region, normalized_region)
            country_code = self._country_codes.get(lookup_region)
            if country_code is None:
                logger.debug("TMDB provider lookup skipped: no country code for %r", region)
            return country_code

    def _subscription_providers(
        self, match: TMDBMatch, country_code: str, title: str
    ) -> frozenset[str]:
        cache_key = (match.media_type, match.id, country_code)
        with self._cache_lock:
            if cache_key in self._provider_cache:
                return self._provider_cache[cache_key]

        inflight_request, is_owner = self._acquire_inflight(
            self._provider_inflight,
            cache_key,
        )
        if not is_owner:
            inflight_request.event.wait()
            if inflight_request.exception is not None:
                raise inflight_request.exception
            return inflight_request.result

        with self._cache_lock:
            providers = self._provider_cache.get(cache_key)
        try:
            if providers is None:
                endpoint = f"/{match.media_type}/{match.id}/watch/providers"
                markets = self._request_json(endpoint, title, {})["results"]
                market = markets.get(country_code, {})
                providers = frozenset(
                    _normalize_provider(offer["provider_name"])
                    for offer in market.get("flatrate", [])
                    if offer.get("provider_name")
                )
                with self._cache_lock:
                    self._provider_cache[cache_key] = providers
        except BaseException as exc:
            self._complete_inflight(self._provider_inflight, cache_key, exception=exc)
            raise
        self._complete_inflight(self._provider_inflight, cache_key, result=providers)
        return providers

    @staticmethod
    def _raise_for_bad_response(response: Response, endpoint: str, query: str) -> None:
        try:
            response.raise_for_status()
        except HTTPError as exc:
            raise TMDBResolverError(
                f"TMDB request failed for endpoint {endpoint!r} and query {query!r}: "
                f"{format_response_diagnostics(response)}"
            ) from exc

    @staticmethod
    def _build_search_order(media_hint: str | None) -> list[str]:
        if media_hint == "movie":
            return ["/search/movie", "/search/multi"]
        if media_hint == "tv":
            return ["/search/tv", "/search/multi"]
        return ["/search/multi", "/search/movie", "/search/tv"]

    def _pick_best_match(
        self,
        normalized_search_title: str,
        results: list[dict[str, Any]],
        *,
        endpoint: str,
    ) -> TMDBMatch | None:
        matches: dict[tuple[str, int], TMDBMatch] = {}
        for result in results:
            media_type = self._extract_media_type(result, endpoint)
            candidate_titles = [
                self._extract_result_title(result, media_type),
                self._extract_result_original_title(result, media_type),
            ]
            scores = [
                (_title_similarity(normalized_search_title, normalize_title(candidate)), candidate)
                for candidate in candidate_titles
                if candidate
            ]
            safe_scores = [
                score
                for score, candidate in scores
                if score >= 0.72
                and _is_safe_fuzzy_title_match(
                    normalized_search_title, normalize_title(candidate)
                )
            ]
            if safe_scores:
                match = self._build_match(
                    result, endpoint=endpoint, title_match_score=max(safe_scores)
                )
                matches[(match.media_type, match.id)] = match

        if len(matches) > 1:
            raise _AmbiguousMatchError(tuple(matches.values()))
        return next(iter(matches.values()), None)

    def _build_match(
        self,
        result: dict[str, Any],
        *,
        endpoint: str,
        title_match_score: float = 1.0,
    ) -> TMDBMatch:
        media_type = self._extract_media_type(result, endpoint)
        matched_title = self._extract_result_title(result, media_type)
        original_title = self._extract_result_original_title(result, media_type)
        release_date = self._extract_result_release_date(result, media_type)

        return TMDBMatch(
            id=int(result["id"]),
            media_type=media_type,
            matched_title=matched_title,
            original_title=original_title,
            release_date=release_date,
            search_endpoint=endpoint,
            popularity=float(result.get("popularity") or 0.0),
            vote_count=int(result.get("vote_count") or 0),
            title_match_score=title_match_score,
            vote_average=float(result.get("vote_average") or 0.0),
            original_language=(
                str(result["original_language"])
                if result.get("original_language")
                else None
            ),
        )

    @staticmethod
    def _extract_media_type(result: dict[str, Any], endpoint: str) -> str:
        if endpoint == "/search/movie":
            return "movie"
        if endpoint == "/search/tv":
            return "tv"
        return str(result.get("media_type") or "")

    @staticmethod
    def _extract_result_title(result: dict[str, Any], media_type: str) -> str:
        if media_type == "movie":
            return str(result.get("title") or "")
        return str(result.get("name") or "")

    @staticmethod
    def _extract_result_original_title(result: dict[str, Any], media_type: str) -> str | None:
        if media_type == "movie":
            value = result.get("original_title")
        else:
            value = result.get("original_name")
        return str(value) if value else None

    @staticmethod
    def _extract_result_release_date(result: dict[str, Any], media_type: str) -> str | None:
        if media_type == "movie":
            value = result.get("release_date")
        else:
            value = result.get("first_air_date")
        return str(value) if value else None


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value.casefold())
    stripped = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )
    stripped = stripped.replace("&", " and ")
    stripped = re.sub(r"[^\w\s]", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _normalize_provider(value: str) -> str:
    provider = normalize_title(value)
    aliases = {
        "amazon prime": "amazon prime video",
        "prime video": "amazon prime video",
        "disney": "disney plus",
        "apple tv": "apple tv plus",
        "max": "hbo max",
        "paramount": "paramount plus",
    }
    return aliases.get(provider, provider)


def _title_search_variants(title: str) -> tuple[str, ...]:
    normalized = normalize_title(title)
    if re.fullmatch(r"\d{2,4}", normalized):
        digit_words = (
            "zero", "one", "two", "three", "four",
            "five", "six", "seven", "eight", "nine",
        )
        spelled = " ".join(digit_words[int(digit)] for digit in normalized)
        if spelled != normalized:
            return title, spelled
    return (title,)


def _is_safe_fuzzy_title_match(query: str, candidate: str) -> bool:
    if query == candidate:
        return True
    generic = {"a", "an", "the", "movie", "film", "tv", "show", "series"}
    query_words = set(query.split()) - generic
    candidate_words = set(candidate.split()) - generic
    if not query_words or not candidate_words:
        return False
    # A title that merely extends or shortens another title is often a sequel,
    # subtitle, or different work in the same franchise (e.g. Hunger Games: Ballad).
    # Permit fuzzy spelling/order differences, but reject token containment.
    return not (query_words < candidate_words or candidate_words < query_words)


def _title_similarity(query: str, candidate: str) -> float:
    """Score exact, shortened, and expanded titles without matching one shared word."""
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0

    stop_words = {"a", "an", "the", "movie", "film", "tv", "show", "series"}
    query_words = query.split()
    candidate_words = candidate.split()
    query_tokens = set(query.split()) - stop_words
    candidate_tokens = set(candidate.split()) - stop_words
    query_tokens = query_tokens or set(query.split())
    candidate_tokens = candidate_tokens or set(candidate.split())

    if query_tokens <= candidate_tokens:
        # Keep generic suffixes such as "the movie" as useful evidence. They
        # should qualify a shortened title, while an exact title still ranks
        # a little higher when candidates otherwise have similar evidence.
        extra_tokens = len(candidate_words) - len(query_words)
        return max(0.90, 0.97 - 0.025 * extra_tokens)
    if candidate_tokens < query_tokens:
        extra_tokens = len(query_tokens - candidate_tokens)
        return max(0.72, 0.92 - 0.10 * extra_tokens)

    overlap = len(query_tokens & candidate_tokens)
    coverage = overlap / len(query_tokens)
    if coverage >= 0.75:
        precision = overlap / len(candidate_tokens)
        return 0.72 + 0.20 * coverage * precision
    return 0.0
