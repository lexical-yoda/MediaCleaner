from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

CACHE_PATH = Path(__file__).parent / "cache.json"
TMDB_BASE = "https://api.themoviedb.org/3"
OMDB_BASE = "http://www.omdbapi.com"

_TMDB_SEMAPHORE: asyncio.Semaphore | None = None
_OMDB_SEMAPHORE: asyncio.Semaphore | None = None


def _get_tmdb_semaphore() -> asyncio.Semaphore:
    global _TMDB_SEMAPHORE
    if _TMDB_SEMAPHORE is None:
        _TMDB_SEMAPHORE = asyncio.Semaphore(8)
    return _TMDB_SEMAPHORE


def _get_omdb_semaphore() -> asyncio.Semaphore:
    global _OMDB_SEMAPHORE
    if _OMDB_SEMAPHORE is None:
        _OMDB_SEMAPHORE = asyncio.Semaphore(5)
    return _OMDB_SEMAPHORE


def _load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass


def _is_fresh(entry: dict[str, Any], ttl_days: int) -> bool:
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
        return datetime.now() - fetched < timedelta(days=ttl_days)
    except (KeyError, ValueError):
        return False


class RatingsClient:
    def __init__(self, tmdb_api_key: str, omdb_api_key: str, cache_ttl_days: int = 7) -> None:
        self.tmdb_key = tmdb_api_key
        self.omdb_key = omdb_api_key
        self.ttl_days = cache_ttl_days
        self._cache: dict[str, Any] = _load_cache()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "RatingsClient":
        self._client = httpx.AsyncClient(timeout=12.0)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
        _save_cache(self._cache)

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        entry = self._cache.get(key)
        if entry and _is_fresh(entry, self.ttl_days):
            return entry
        return None

    def _cache_set(self, key: str, data: dict[str, Any]) -> None:
        self._cache[key] = {**data, "fetched_at": datetime.now().isoformat()}

    async def _tmdb_get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        assert self._client is not None
        params = {**params, "api_key": self.tmdb_key}
        async with _get_tmdb_semaphore():
            try:
                await asyncio.sleep(0.12)
                r = await self._client.get(f"{TMDB_BASE}{path}", params=params)
                if r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", "10"))
                    await asyncio.sleep(retry_after + 1)
                    r = await self._client.get(f"{TMDB_BASE}{path}", params=params)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, httpx.TimeoutException):
                return None

    async def _omdb_get(self, params: dict[str, Any]) -> dict[str, Any] | None:
        assert self._client is not None
        params = {**params, "apikey": self.omdb_key}
        async with _get_omdb_semaphore():
            try:
                await asyncio.sleep(0.2)
                r = await self._client.get(OMDB_BASE, params=params)
                r.raise_for_status()
                data = r.json()
                return data if data.get("Response") == "True" else None
            except (httpx.HTTPError, httpx.TimeoutException):
                return None

    async def fetch_ratings(
        self, title: str, year: int | None, media_type: str
    ) -> tuple[str | None, str | None, float | None, int | None]:
        """Returns (tmdb_title, imdb_id, imdb_rating, rt_score)."""
        cache_key = f"v2:{media_type}:{title.lower()}:{year}"
        cached = self._cache_get(cache_key)
        if cached:
            return (
                cached.get("tmdb_title"),
                cached.get("imdb_id"),
                cached.get("imdb_rating"),
                cached.get("rt_score"),
            )

        imdb_id, tmdb_title = await self._get_imdb_id(title, year, media_type)
        imdb_rating, rt_score = None, None

        if imdb_id:
            imdb_rating, rt_score = await self._get_omdb_ratings(imdb_id)

        self._cache_set(cache_key, {
            "tmdb_title": tmdb_title,
            "imdb_id": imdb_id,
            "imdb_rating": imdb_rating,
            "rt_score": rt_score,
        })
        return tmdb_title, imdb_id, imdb_rating, rt_score

    async def _get_imdb_id(
        self, title: str, year: int | None, media_type: str
    ) -> tuple[str | None, str | None]:
        if media_type == "movie":
            return await self._movie_imdb_id(title, year)
        else:
            return await self._tv_imdb_id(title)

    async def _movie_imdb_id(
        self, title: str, year: int | None
    ) -> tuple[str | None, str | None]:
        params: dict[str, Any] = {"query": title, "language": "en-US", "page": 1}
        if year:
            params["primary_release_year"] = year

        data = await self._tmdb_get("/search/movie", params)
        results = (data or {}).get("results", [])

        if not results and year:
            params.pop("primary_release_year")
            data = await self._tmdb_get("/search/movie", params)
            results = (data or {}).get("results", [])

        tmdb_id = self._pick_movie_result(results, year)
        if not tmdb_id:
            return None, None

        detail = await self._tmdb_get(f"/movie/{tmdb_id}", {"language": "en-US"})
        if not detail:
            return None, None

        return detail.get("imdb_id"), detail.get("title")

    def _pick_movie_result(self, results: list[dict], year: int | None) -> int | None:
        for r in results[:5]:
            if r.get("vote_count", 0) < 5:
                continue
            if year:
                rd = r.get("release_date", "")
                ry = int(rd[:4]) if len(rd) >= 4 else None
                if ry and abs(ry - year) <= 1:
                    return r["id"]
            else:
                return r["id"]
        return results[0]["id"] if results else None

    async def _tv_imdb_id(self, title: str) -> tuple[str | None, str | None]:
        data = await self._tmdb_get("/search/tv", {"query": title, "language": "en-US", "page": 1})
        results = (data or {}).get("results", [])
        if not results:
            return None, None

        tmdb_id = results[0]["id"]
        ext = await self._tmdb_get(f"/tv/{tmdb_id}/external_ids", {})
        tmdb_title = results[0].get("name")
        if not ext:
            return None, tmdb_title

        return ext.get("imdb_id"), tmdb_title

    async def _get_omdb_ratings(self, imdb_id: str) -> tuple[float | None, int | None]:
        data = await self._omdb_get({"i": imdb_id, "tomatoes": "true"})
        if not data:
            return None, None

        imdb_rating: float | None = None
        rt_score: int | None = None

        raw_imdb = data.get("imdbRating", "N/A")
        if raw_imdb not in ("N/A", "", None):
            try:
                imdb_rating = float(raw_imdb)
            except ValueError:
                pass

        for rating in data.get("Ratings", []):
            if rating.get("Source") == "Rotten Tomatoes":
                val = rating.get("Value", "")
                try:
                    rt_score = int(val.rstrip("%"))
                except ValueError:
                    pass
                break

        return imdb_rating, rt_score
