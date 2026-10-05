"""Orquestación: búsqueda → ficha → fuentes en paralelo → media."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from statistics import mean

import aiohttp

import sources
from sources import AUDIENCE, CRITICS, Rating
from tmdb import TMDB, Movie

log = logging.getLogger("service")

SOURCE_TIMEOUT = 10  # s por fuente; una web lenta no bloquea al resto


class TTLCache:
    """LRU con caducidad, acotada en tamaño (memoria predecible en el VPS)."""

    def __init__(self, ttl: float, maxsize: int):
        self.ttl, self.maxsize, self.data = ttl, maxsize, OrderedDict()

    def get(self, key):
        item = self.data.get(key)
        if not item or item[0] < time.monotonic():
            self.data.pop(key, None)
            return None
        self.data.move_to_end(key)
        return item[1]

    def set(self, key, value):
        self.data[key] = (time.monotonic() + self.ttl, value)
        self.data.move_to_end(key)
        while len(self.data) > self.maxsize:
            self.data.popitem(last=False)


@dataclass(slots=True)
class Result:
    movie: Movie
    ratings: list[Rating]
    critics: float | None
    audience: float | None
    overall: float | None


def aggregate(ratings: list[Rating]) -> tuple[float | None, float | None, float | None]:
    """Media de crítica y media de público por separado, y global = media de
    ambas (50/50). Así el resultado no depende de cuántas webs de público haya
    frente a las de crítica."""
    def avg(kind):
        xs = [r.score for r in ratings if r.kind == kind and r.reliable]
        return mean(xs) if xs else None

    c, a = avg(CRITICS), avg(AUDIENCE)
    parts = [x for x in (c, a) if x is not None]
    return c, a, (mean(parts) if parts else None)


YEAR_RE = re.compile(r"^(.*?)[\s(\[]+((?:18|19|20)\d{2})[)\]]?\s*$")


class MovieService:
    def __init__(self, http: aiohttp.ClientSession, tmdb_key: str, omdb_key: str = "",
                 filmaffinity: bool = False, region: str = "AR"):
        self.http, self.tmdb, self.omdb_key = http, TMDB(http, tmdb_key, region), omdb_key
        self.use_filmaffinity = filmaffinity
        self.results = TTLCache(ttl=12 * 3600, maxsize=200)
        self.searches = TTLCache(ttl=30 * 60, maxsize=500)
        self.inflight: dict[int, asyncio.Task] = {}

    async def suggest(self, query: str) -> list[dict]:
        key = query.strip().lower()
        if (hit := self.searches.get(key)) is not None:
            return hit
        title, year = self._split_year(query)
        res = await self.tmdb.search(title, year)
        if not res and year:
            # El "año" era parte del título (p. ej. "Blade Runner 2049").
            res = await self.tmdb.search(query.strip())
        self.searches.set(key, res)
        return res

    async def lookup(self, query: str) -> Result | None:
        if m := re.fullmatch(r"tmdb:(\d+)", query.strip()):
            return await self.get(int(m.group(1)))
        results = await self.suggest(query)
        return await self.get(results[0]["id"]) if results else None

    async def get(self, movie_id: int) -> Result:
        if hit := self.results.get(movie_id):
            return hit
        # Si dos usuarios piden la misma peli a la vez, se scrapea una sola vez.
        task = self.inflight.get(movie_id)
        if not task:
            task = self.inflight[movie_id] = asyncio.create_task(self._build(movie_id))
            task.add_done_callback(lambda _: self.inflight.pop(movie_id, None))
        return await task

    async def _build(self, movie_id: int) -> Result:
        movie = await self.tmdb.movie(movie_id)
        jobs = {
            "rt": sources.rotten_tomatoes(self.http, movie),
            "mc": sources.metacritic(self.http, movie),
            "lb": sources.letterboxd(self.http, movie),
            "omdb": sources.omdb(self.http, movie, self.omdb_key),
        }
        if self.use_filmaffinity:
            jobs["fa"] = sources.filmaffinity(self.http, movie)
        done = await asyncio.gather(
            *(asyncio.wait_for(j, SOURCE_TIMEOUT) for j in jobs.values()), return_exceptions=True)

        ratings: dict[str, Rating] = {}
        for name, res in zip(jobs, done):
            if isinstance(res, BaseException):
                log.warning("%s falló para %s: %r", name, movie.title_en, res)
                continue
            for r in res:
                ratings.setdefault(r.source, r)   # el scraping directo tiene prioridad sobre OMDb
        for r in sources.tmdb_rating(movie):
            ratings.setdefault(r.source, r)

        ordered = sorted(ratings.values(), key=lambda r: (r.kind != CRITICS, r.source))
        result = Result(movie, ordered, *aggregate(ordered))
        self.results.set(movie_id, result)
        return result

    @staticmethod
    def _split_year(q: str) -> tuple[str, int | None]:
        m = YEAR_RE.match(q.strip())
        if m and m.group(1).strip():
            return m.group(1).strip(), int(m.group(2))
        return q.strip(), None
