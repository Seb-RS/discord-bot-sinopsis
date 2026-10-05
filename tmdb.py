"""Cliente mínimo de TMDB: búsqueda multilingüe + ficha de la película."""
from __future__ import annotations

from dataclasses import dataclass, field

import asyncio

import aiohttp

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
MIN_VOTES = 500  # una recomendación con menos votos es una apuesta, no una recomendación


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
    certification: str | None = None                   # clasificación por edad, p. ej. "+13 (AR)"
    recommendations: list[dict] = field(default_factory=list)
    # Semilla para calcular recomendaciones (no se muestra)
    _genre_ids: set[int] = field(default_factory=set, repr=False)
    _keyword_ids: list[int] = field(default_factory=list, repr=False)
    _tmdb_recs: list[dict] = field(default_factory=list, repr=False)
    _collection_id: int | None = field(default=None, repr=False)

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
            append_to_response="translations,external_ids,watch/providers,release_dates,recommendations,keywords",
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
            certification=self._certification(d),
            _genre_ids={g["id"] for g in d.get("genres", [])},
            _keyword_ids=[k["id"] for k in d.get("keywords", {}).get("keywords", [])],
            _tmdb_recs=d.get("recommendations", {}).get("results", []),
            _collection_id=(d.get("belongs_to_collection") or {}).get("id"),
        )

    def _certification(self, d: dict) -> str | None:
        by_country = {r["iso_3166_1"]: r.get("release_dates", [])
                      for r in d.get("release_dates", {}).get("results", [])}
        for country in dict.fromkeys((self.region, "US")):
            # El estreno en cines (tipo 3) primero; si no, cualquiera con clasificación.
            dates = sorted(by_country.get(country, []), key=lambda x: x.get("type") != 3)
            for rd in dates:
                if cert := (rd.get("certification") or "").strip():
                    return f"{cert} ({country})"
        return None

    async def recommend(self, movie: Movie, n: int = 3) -> list[dict]:
        """Hasta `n` películas parecidas.

        Las recomendaciones crudas de TMDB mezclan mal parecido y popularidad
        (p. ej. «Avengers: Endgame» para «Interstellar»), así que:
        1. Candidatas = recomendaciones de TMDB + películas de la misma saga +
           películas que comparten palabras clave temáticas (discover), con
           ≥ MIN_VOTES votos y algún género en común.
        2. Se piden las palabras clave de las mejores candidatas y se puntúa:
           (4·similitud de palabras clave + similitud de géneros) × nota bayesiana.
        3. Se exige ≥ 1 palabra clave en común (si no, solo entra de relleno) y
           como mucho 1 película de la misma saga, para no recomendar solo secuelas.
        """
        kw0, g0 = set(movie._keyword_ids), movie._genre_ids
        reqs = []
        if kw0:
            with_kw = "|".join(map(str, movie._keyword_ids[:20]))
            reqs += [self._get("/discover/movie", language="es-MX", with_keywords=with_kw,
                               sort_by="vote_count.desc", page=p, **{"vote_count.gte": MIN_VOTES})
                     for p in (1, 2)]
        if movie._collection_id:
            reqs.append(self._get(f"/collection/{movie._collection_id}", language="es-MX"))
        pages = await asyncio.gather(*reqs, return_exceptions=True)
        saga = {r["id"] for pg in pages if isinstance(pg, dict) for r in pg.get("parts", [])}
        pool: dict[int, dict] = {}
        for r in movie._tmdb_recs + [x for pg in pages if isinstance(pg, dict)
                                     for x in pg.get("results", []) + pg.get("parts", [])]:
            if (r["id"] != movie.tmdb_id and not r.get("adult")
                    and (r.get("vote_count") or 0) >= MIN_VOTES and g0 & set(r.get("genre_ids", []))):
                pool.setdefault(r["id"], r)

        def jaccard(a: set, b: set) -> float:
            return len(a & b) / len(a | b) if a | b else 0.0

        def quality(r: dict) -> float:
            # Media bayesiana: con pocos votos la nota tiende a 6,5.
            v, c = r.get("vote_average") or 0, r.get("vote_count") or 0
            return (v * c + 6.5 * 1000) / (c + 1000)

        # La saga siempre entra a puntuarse; el resto, las 15 mejores por calidad × géneros.
        rest = sorted((r for r in pool.values() if r["id"] not in saga),
                      key=lambda r: -quality(r) * jaccard(g0, set(r["genre_ids"])))
        top = [r for r in pool.values() if r["id"] in saga] + rest[:15]
        kws = await asyncio.gather(*(self._get(f"/movie/{r['id']}/keywords") for r in top),
                                   return_exceptions=True)
        scored = []
        for r, k in zip(top, kws):
            kw = {x["id"] for x in k.get("keywords", [])} if isinstance(k, dict) else set()
            score = (4 * jaccard(kw0, kw) + jaccard(g0, set(r["genre_ids"]))) * quality(r)
            scored.append((bool(kw0 & kw), score, r))
        # Primero las que comparten temática; dentro de cada grupo, por puntuación.
        scored.sort(key=lambda x: (not x[0], -x[1]))
        out, saga_used = [], False
        for _, _, r in scored:
            if r["id"] in saga:
                if saga_used:
                    continue
                saga_used = True
            out.append({"id": r["id"], "title": r.get("title") or r.get("original_title"),
                        "year": (r.get("release_date") or "")[:4] or None,
                        "vote": r.get("vote_average") or 0.0})
            if len(out) == n:
                break
        return out
