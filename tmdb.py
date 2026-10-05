"""Cliente mínimo de TMDB: búsqueda multilingüe + ficha de la película."""
from __future__ import annotations

from dataclasses import dataclass, field

import asyncio
import math
from collections import defaultdict

import aiohttp

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
MIN_VOTES = 300      # una recomendación con menos votos es una apuesta, no una recomendación
MAX_KEYWORDS = 30    # nº máximo de palabras clave consultadas (1 petición pequeña cada una)
CORPUS = 20_000      # orden de magnitud de películas con ≥ MIN_VOTES votos en TMDB (para el IDF)


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
    recommendations: list[dict] = field(default_factory=list)
    # Semilla para calcular recomendaciones (no se muestra)
    _genre_ids: set[int] = field(default_factory=set, repr=False)
    _keywords: dict[int, str] = field(default_factory=dict, repr=False)  # id → nombre
    _collection_id: int | None = field(default=None, repr=False)

    @property
    def url(self) -> str:
        return f"https://www.themoviedb.org/movie/{self.tmdb_id}"

    def all_titles(self) -> set[str]:
        return {t for t in (self.original_title, self.title_lat, self.title_es, self.title_en) if t}


class TMDBError(Exception):
    """Error de TMDB sin datos sensibles (ni URL con la clave ni cabeceras)."""


class TMDB:
    def __init__(self, http: aiohttp.ClientSession, key: str):
        self.http = http
        # Acepta tanto la API key v3 como el "Read Access Token" v4 (JWT).
        if key.startswith("eyJ"):
            self.headers, self.auth = {"Authorization": f"Bearer {key}"}, {}
        else:
            self.headers, self.auth = {}, {"api_key": key}

    async def _get(self, path: str, **params) -> dict:
        async with self.http.get(API + path, params={**self.auth, **params}, headers=self.headers) as r:
            if r.status != 200:
                # No se usa raise_for_status(): su excepción incluye la URL (con la
                # api_key) y las cabeceras (con el token), y acabaría en los logs.
                raise TMDBError(f"TMDB respondió HTTP {r.status} en {path}")
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
            append_to_response="translations,external_ids,keywords",
        )
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
            _genre_ids={g["id"] for g in d.get("genres", [])},
            _keywords={k["id"]: k["name"] for k in d.get("keywords", {}).get("keywords", [])},
            _collection_id=(d.get("belongs_to_collection") or {}).get("id"),
        )

    async def recommend(self, movie: Movie, n: int = 3) -> list[dict]:
        """Hasta `n` películas parecidas por TEMÁTICA, no por popularidad.

        Las recomendaciones de TMDB tal cual mezclan mal parecido y popularidad
        (p. ej. «Avengers: Endgame» para «Interstellar»). Aquí se usa TF-IDF
        sobre las palabras clave de TMDB:
        1. Por cada palabra clave de la película se piden las películas más
           votadas que la tienen. `total_results` dice lo rara que es:
           «agujero de gusano» (10 películas) pesa mucho más que «distopía» (267).
        2. Puntuación de cada candidata = Σ IDF de las palabras clave que comparte
           × (0,5 + similitud de géneros) × nota bayesiana / 7.
        3. Se exige algún género en común y entra como mucho 1 película de la saga.
        """
        kws = list(movie._keywords.items())[:MAX_KEYWORDS]
        if not kws:
            return []
        g0 = movie._genre_ids
        reqs = [self._get("/discover/movie", language="es-MX", with_keywords=kid,
                          sort_by="vote_count.desc", **{"vote_count.gte": MIN_VOTES})
                for kid, _ in kws]
        if movie._collection_id:
            reqs.append(self._get(f"/collection/{movie._collection_id}"))
        pages = await asyncio.gather(*reqs, return_exceptions=True)
        saga = set()
        if movie._collection_id and isinstance(pages[-1], dict):
            saga = {p["id"] for p in pages.pop().get("parts", [])}

        idf_sum: dict[int, float] = defaultdict(float)
        shared: dict[int, list[tuple[float, str]]] = defaultdict(list)
        info: dict[int, dict] = {}
        for (_, name), page in zip(kws, pages):
            if not isinstance(page, dict):
                continue
            idf = math.log(CORPUS / max(page.get("total_results") or 1, 1))
            for r in page.get("results", []):
                if r["id"] == movie.tmdb_id or r.get("adult"):
                    continue
                idf_sum[r["id"]] += idf
                shared[r["id"]].append((idf, name))
                info[r["id"]] = r

        def quality(r: dict) -> float:
            # Media bayesiana: con pocos votos la nota tiende a 6,5.
            v, c = r.get("vote_average") or 0, r.get("vote_count") or 0
            return (v * c + 6.5 * 1000) / (c + 1000)

        scored = []
        for mid, s in idf_sum.items():
            r, g = info[mid], set(info[mid].get("genre_ids", []))
            if not g0 & g:
                continue
            scored.append((s * (0.5 + len(g0 & g) / len(g0 | g)) * quality(r) / 7, r))
        scored.sort(key=lambda x: -x[0])

        out, saga_used, franchises = [], False, set()
        for _, r in scored:
            if r["id"] in saga:
                if saga_used:
                    continue
                saga_used = True
            # Tampoco dos de otra misma saga («Matrix 2» y «Matrix 3»). Las
            # candidatas no traen su colección, así que se aproxima con la
            # primera palabra significativa del título original.
            if (fr := _franchise(r.get("original_title") or r.get("title") or "")) in franchises:
                continue
            franchises.add(fr)
            # Los 3 temas compartidos más específicos, para explicar el porqué.
            themes = [name for _, name in sorted(shared[r["id"]], reverse=True)[:3]]
            out.append({"id": r["id"], "title": r.get("title") or r.get("original_title"),
                        "year": (r.get("release_date") or "")[:4] or None,
                        "vote": r.get("vote_average") or 0.0, "themes": themes})
            if len(out) == n:
                break
        return out


_ARTICLES = {"the", "a", "an", "el", "la", "los", "las", "un", "una", "le", "les", "der", "die", "das"}


def _franchise(title: str) -> str:
    words = [w for w in "".join(c if c.isalnum() else " " for c in title.lower()).split()
             if w not in _ARTICLES]
    return words[0] if words else title
