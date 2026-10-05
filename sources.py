"""Fuentes de valoración.

Cada fuente devuelve una lista de `Rating` (vacía si falla o no hay match
fiable). Nunca se "adivina": si el año no coincide (±1) se descarta.
Todo se parsea con regex sobre JSON embebido (JSON-LD / props), sin
BeautifulSoup ni navegadores headless.
"""
from __future__ import annotations

import html
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

import aiohttp

from tmdb import Movie

log = logging.getLogger("sources")

CRITICS, AUDIENCE = "crítica", "público"

# Muestra mínima para que una puntuación cuente en la media. Por debajo se
# muestra pero se excluye (una media de 3 votos no es una valoración).
MIN_COUNT = {
    "Rotten Tomatoes": 20,
    "Metacritic": 7,
    "RT Popcornmeter": 100,
    "IMDb": 500,
    "Letterboxd": 500,
    "FilmAffinity": 200,
    "TMDB": 100,
}


@dataclass(slots=True)
class Rating:
    source: str
    kind: str            # CRITICS | AUDIENCE
    value: float         # en su escala original
    lo: float            # mínimo de la escala
    hi: float            # máximo de la escala
    count: int | None
    url: str

    @property
    def score(self) -> float:
        """Normalización min-max a 0–100 (respeta escalas que no empiezan en 0)."""
        return max(0.0, min(100.0, (self.value - self.lo) / (self.hi - self.lo) * 100))

    @property
    def reliable(self) -> bool:
        return self.count is None or self.count >= MIN_COUNT.get(self.source, 0)

    @property
    def display(self) -> str:
        v = f"{self.value:.0f}" if self.hi == 100 else f"{self.value:.1f}".rstrip("0").rstrip(".")
        return f"{v}/{self.hi:g}"


# ───────────────────────── utilidades ─────────────────────────

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", html.unescape(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def year_ok(movie: Movie, year: int | str | None) -> bool:
    if not movie.year or not year:
        return False
    try:
        return abs(int(str(year)[:4]) - movie.year) <= 1
    except ValueError:
        return False


def best_match(movie: Movie, candidates: list[tuple[int | None, str, str]]) -> str | None:
    """candidates: (año, título, url). Exige año ±1; prioriza título idéntico."""
    titles = {norm(t) for t in movie.all_titles()}
    by_year = [c for c in candidates if year_ok(movie, c[0])]
    for c in by_year:
        if norm(c[1]) in titles:
            return c[2]
    return by_year[0][2] if by_year else None


async def fetch(http: aiohttp.ClientSession, url: str) -> tuple[str, str] | None:
    # Un reintento ante cortes de conexión a mitad de descarga (pasa con
    # páginas grandes como las de Metacritic, ~700 KB).
    for attempt in (1, 2):
        try:
            async with http.get(url) as r:
                if r.status != 200:
                    log.info("%s -> HTTP %s", url, r.status)
                    return None
                return str(r.url), await r.text(errors="replace")
        except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError):
            if attempt == 2:
                raise
            log.info("%s -> conexión cortada, reintentando", url)


def int_or_none(x) -> int | None:
    try:
        return int(str(x).replace(",", "").replace(".", ""))
    except (TypeError, ValueError):
        return None


# ───────────────────────── fuentes ─────────────────────────

RT_ROW = re.compile(r"<search-page-media-row(.*?)</search-page-media-row>", re.S)


async def rotten_tomatoes(http, movie: Movie) -> list[Rating]:
    res = await fetch(http, "https://www.rottentomatoes.com/search?search=" + quote_plus(movie.title_en))
    if not res:
        return []
    cands = []
    for row in RT_ROW.findall(res[1]):
        href = re.search(r'href="(https://www\.rottentomatoes\.com/m/[^"]+)"', row)
        if not href:
            continue
        year = re.search(r'release-year="(\d{4})"', row)
        title = re.search(r'slot="title"[^>]*>\s*([^<]+?)\s*<', row)
        cands.append((year and year.group(1), title.group(1) if title else "", href.group(1)))
    url = best_match(movie, cands)
    if not url or not (page := await fetch(http, url)):
        return []

    out = []
    for key, name, kind, hi in (("criticsScore", "Rotten Tomatoes", CRITICS, 10),
                                ("audienceScore", "RT Popcornmeter", AUDIENCE, 5)):
        m = re.search(rf'"{key}":(\{{[^{{}}]*\}})', page[1])
        if not m:
            continue
        d = json.loads(m.group(1))
        # Se usa la NOTA MEDIA, no el % de reseñas positivas (Tomatometer),
        # porque el % mide consenso, no calidad, y no es comparable con el resto.
        avg = d.get("averageRating")
        if not avg:
            continue
        count = d.get("ratingCount") if kind == CRITICS else (
            (d.get("likedCount") or 0) + (d.get("notLikedCount") or 0))
        out.append(Rating(name, kind, float(avg), 0 if kind == CRITICS else 0.5, hi, int_or_none(count), url))
    return out


def mc_slug(title: str) -> str:
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    t = t.replace("&", "and").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


async def metacritic(http, movie: Movie) -> list[Rating]:
    slug = mc_slug(movie.title_en)
    if not slug:
        return []
    # Metacritic no tiene búsqueda en HTML estático: se prueba el slug y,
    # si choca con otra película del mismo nombre, el slug con año.
    for s in (slug, f"{slug}-{movie.year}"):
        res = await fetch(http, f"https://www.metacritic.com/movie/{s}/")
        if not res:
            continue
        for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', res[1], re.S):
            try:
                d = json.loads(block)
            except json.JSONDecodeError:
                continue
            if d.get("@type") != "Movie":
                continue
            # MC redirige slugs ambiguos a otras películas: se exige título Y año.
            if norm(d.get("name", "")) not in {norm(t) for t in movie.all_titles()} \
                    or not year_ok(movie, d.get("datePublished")):
                break
            agg = d.get("aggregateRating") or {}
            if agg.get("ratingValue") is None:
                return []
            return [Rating("Metacritic", CRITICS, float(agg["ratingValue"]), 0, 100,
                           int_or_none(agg.get("reviewCount")), res[0])]
    return []


async def letterboxd(http, movie: Movie) -> list[Rating]:
    # Letterboxd resuelve por ID de TMDB: match exacto, sin ambigüedad.
    res = await fetch(http, f"https://letterboxd.com/tmdb/{movie.tmdb_id}/")
    if not res:
        return []
    m = re.search(r'"aggregateRating":(\{[^{}]*\})', res[1])
    if not m:
        return []
    d = json.loads(m.group(1))
    return [Rating("Letterboxd", AUDIENCE, float(d["ratingValue"]), 0.5, 5,
                   int_or_none(d.get("ratingCount")), res[0])]


FA_ITEM = re.compile(
    r'ye-w">(\d{4})|data-movie-id="(\d+)"[\s\S]{0,2000}?mc-title">\s*<a[^>]*>([^<]+)')


async def filmaffinity(http, movie: Movie) -> list[Rating]:
    url = None
    for q in dict.fromkeys((movie.title_es, movie.original_title, movie.title_lat)):
        res = await fetch(http, "https://www.filmaffinity.com/es/search.php?stype=title&stext=" + quote(q))
        if not res:
            continue
        if "/film" in res[0]:          # resultado único → redirige a la ficha
            url, page = res
            break
        cands, year = [], None
        for y, mid, title in FA_ITEM.findall(res[1]):
            if y:
                year = int(y)       # FA agrupa los resultados bajo un encabezado de año
            else:
                cands.append((year, title, f"https://www.filmaffinity.com/es/film{mid}.html"))
        if url := best_match(movie, cands):
            page = None
            break
    if not url:
        return []
    if page is None:
        if not (res := await fetch(http, url)):
            return []
        page = res[1]
    y = re.search(r'itemprop="datePublished">(\d{4})', page)
    if y and not year_ok(movie, y.group(1)):
        return []
    v = re.search(r'itemprop="ratingValue" content="([\d.,]+)"', page)
    c = re.search(r'itemprop="ratingCount" content="(\d+)"', page)
    if not v:
        return []
    return [Rating("FilmAffinity", AUDIENCE, float(v.group(1).replace(",", ".")), 1, 10,
                   int_or_none(c and c.group(1)), url)]


async def omdb(http, movie: Movie, key: str) -> list[Rating]:
    """IMDb bloquea el scraping (AWS WAF), así que se usa OMDb. Aporta además
    el Metascore como respaldo si el scraping de Metacritic falla."""
    if not key or not movie.imdb_id:
        return []
    try:
        async with http.get("https://www.omdbapi.com/", params={"i": movie.imdb_id, "apikey": key}) as r:
            if r.status != 200:
                return []
            d = await r.json(content_type=None)
    except aiohttp.ClientResponseError as e:
        # Su repr incluye la URL con la apikey: se relanza sin ella.
        raise RuntimeError(f"OMDb falló: HTTP {e.status}") from None
    if d.get("Response") != "True":
        return []
    out = []
    imdb_url = f"https://www.imdb.com/title/{movie.imdb_id}/"
    if (v := d.get("imdbRating")) not in (None, "N/A"):
        out.append(Rating("IMDb", AUDIENCE, float(v), 1, 10, int_or_none(d.get("imdbVotes")), imdb_url))
    if (v := d.get("Metascore")) not in (None, "N/A"):
        # Sin nº de reseñas en OMDb → count=None (se considera fiable: MC solo
        # publica Metascore a partir de 4 reseñas).
        out.append(Rating("Metacritic", CRITICS, float(v), 0, 100, None,
                          f"https://www.metacritic.com/search/{quote(movie.title_en)}/"))
    return out


def tmdb_rating(movie: Movie) -> list[Rating]:
    if not movie.vote_count:
        return []
    return [Rating("TMDB", AUDIENCE, movie.vote_average, 0.5, 10, movie.vote_count, movie.url)]
