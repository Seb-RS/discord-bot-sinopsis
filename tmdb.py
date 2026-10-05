"""Cliente mínimo de TMDB: búsqueda multilingüe + ficha de la película."""
from __future__ import annotations

from dataclasses import dataclass, field

import aiohttp

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"


@dataclass(slots=True)
class Movie:
    tmdb_id: int
    imdb_id: str | None
    year: int | None
    original_title: str
    title_lat: str          # es-MX
    title_es: str           # es-ES
    title_en: str           # en-US (para RT / Metacritic)
    overview: str
    poster: str | None
    runtime: int | None
    genres: list[str] = field(default_factory=list)
    vote_average: float = 0.0
    vote_count: int = 0
    # Plataformas legales en la región configurada (datos de JustWatch vía TMDB)
    watch_link: str | None = None
    stream: list[str] = field(default_factory=list)   # suscripción / gratis
    rent_buy: list[str] = field(default_factory=list)  # alquiler o compra

    @property
    def url(self) -> str:
        return f"https://www.themoviedb.org/movie/{self.tmdb_id}"

    def all_titles(self) -> set[str]:
        return {t for t in (self.original_title, self.title_lat, self.title_es, self.title_en) if t}


class TMDB:
    def __init__(self, http: aiohttp.ClientSession, key: str, region: str = "AR"):
        self.http, self.region = http, region.upper()
        # Acepta tanto la API key v3 como el "Read Access Token" v4 (JWT).
        if key.startswith("eyJ"):
            self.headers, self.auth = {"Authorization": f"Bearer {key}"}, {}
        else:
            self.headers, self.auth = {}, {"api_key": key}

    async def _get(self, path: str, **params) -> dict:
        async with self.http.get(API + path, params={**self.auth, **params}, headers=self.headers) as r:
            r.raise_for_status()
            return await r.json()

    async def search(self, query: str, year: int | None = None) -> list[dict]:
        # TMDB busca también en títulos traducidos y alternativos, así que
        # "El origen", "Origen" e "Inception" devuelven la misma película.
        params = {"query": query, "language": "es-MX", "include_adult": "false"}
        if year:
            params["year"] = year
        return (await self._get("/search/movie", **params)).get("results", [])

    async def movie(self, movie_id: int) -> Movie:
        d = await self._get(
            f"/movie/{movie_id}", language="es-MX",
            append_to_response="translations,external_ids,watch/providers",
        )
        wp = d.get("watch/providers", {}).get("results", {}).get(self.region, {})

        def providers(*kinds: str) -> list[str]:
            names = [p["provider_name"] for k in kinds for p in wp.get(k, [])]
            return list(dict.fromkeys(names))  # sin duplicados, en orden
        tr = {
            f"{t['iso_639_1']}-{t['iso_3166_1']}": t.get("data", {})
            for t in d.get("translations", {}).get("translations", [])
        }

        def pick(key: str, *langs: str) -> str:
            for lang in langs:
                if v := (tr.get(lang) or {}).get(key):
                    return v
            return ""

        original = d.get("original_title") or d.get("title") or ""
        en = pick("title", "en-US", "en-GB") or (original if d.get("original_language") == "en" else "")
        date = d.get("release_date") or ""
        return Movie(
            tmdb_id=d["id"],
            imdb_id=d.get("imdb_id") or d.get("external_ids", {}).get("imdb_id"),
            year=int(date[:4]) if date[:4].isdigit() else None,
            original_title=original,
            title_lat=pick("title", "es-MX") or d.get("title") or original,
            title_es=pick("title", "es-ES") or pick("title", "es-MX") or original,
            title_en=en or original,
            overview=pick("overview", "es-MX", "es-ES") or d.get("overview") or pick("overview", "en-US"),
            poster=IMG + d["poster_path"] if d.get("poster_path") else None,
            runtime=d.get("runtime") or None,
            genres=[g["name"] for g in d.get("genres", [])],
            vote_average=d.get("vote_average") or 0.0,
            vote_count=d.get("vote_count") or 0,
            watch_link=wp.get("link"),
            stream=providers("flatrate", "free", "ads"),
            rent_buy=providers("rent", "buy"),
        )
