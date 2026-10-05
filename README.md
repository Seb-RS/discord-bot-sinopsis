# Bot de sinopsis y valoraciones

`/pelicula titulo:<nombre>` devuelve la sinopsis en español, la portada, los títulos (LAT / España / inglés / original) y una **valoración media** a partir de varias webs de crítica y de público.

Acepta títulos en español latino («El origen»), de España («Origen») o en inglés («Inception»). El autocompletado muestra las coincidencias con su año para desambiguar remakes. También puedes escribir el año: `Origen 2010`.

## Fuentes

| Fuente | Tipo | Cómo se obtiene | Escala |
|---|---|---|---|
| Rotten Tomatoes (nota media de críticos) | crítica | scraping (JSON embebido) | 0–10 |
| Metacritic (Metascore) | crítica | scraping (JSON-LD), con OMDb de respaldo | 0–100 |
| RT Popcornmeter (nota media) | público | scraping | 0,5–5 |
| Letterboxd | público | scraping (JSON-LD, por ID de TMDB) | 0,5–5 |
| IMDb | público | API de OMDb (IMDb bloquea el scraping) | 1–10 |
| TMDB | público | API | 0,5–10 |
| FilmAffinity | público | scraping, **desactivado por defecto** (Cloudflare) | 1–10 |

## Metodología

1. **Normalización min-max a 0–100** según la escala real de cada web. Un 4,2/5 de Letterboxd (mínimo 0,5) da 83, no 84.
2. **De Rotten Tomatoes se usa la nota media**, no el Tomatometer. El % de reseñas positivas mide consenso, no calidad: una película que a todos les parece «correcta» (6/10) puede sacar un 95 %.
3. **Muestra mínima**: si una fuente tiene muy pocos votos o reseñas, se muestra tachada y no cuenta en la media (umbrales en `MIN_COUNT`, en `sources.py`).
4. **Crítica y público se promedian por separado** y la nota global es la media de ambas (50/50). Así el resultado no se inclina hacia el público solo porque haya más webs de público.
5. **No se adivina**: una ficha scrapeada solo se acepta si el año coincide (±1) y, cuando hay ambigüedad, también el título. Si no hay coincidencia fiable, la fuente se omite.

## Ligereza

- Una sola dependencia: `discord.py` (incluye `aiohttp`). Sin BeautifulSoup, sin Selenium ni Playwright: todo son regex sobre el HTML.
- Sin intents ni cachés de Discord: el bot solo atiende slash commands.
- Caché en memoria acotada: 200 fichas (12 h) y 500 búsquedas (30 min). Sin base de datos.
- Las fuentes se consultan en paralelo con un timeout de 10 s cada una. Si dos usuarios piden la misma película a la vez, solo se scrapea una vez.
- Cooldown de 8 s por usuario.

## Instalación en el VPS (Debian/Ubuntu)

```bash
sudo useradd -r -s /usr/sbin/nologin sinopsis
sudo mkdir -p /opt/sinopsis-bot && sudo cp *.py requirements.txt .env.example sinopsis-bot.service /opt/sinopsis-bot/
cd /opt/sinopsis-bot
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env      # rellena DISCORD_TOKEN y TMDB_API_KEY
sudo chown -R sinopsis: /opt/sinopsis-bot && sudo chmod 600 .env
sudo cp sinopsis-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sinopsis-bot
journalctl -u sinopsis-bot -f
```

**Invitar el bot:** en el Developer Portal → OAuth2 → URL Generator, marca los scopes `bot` y `applications.commands`. No hace falta ningún permiso extra ni ningún *privileged intent*.

## Límites conocidos

- El scraping se rompe si las webs cambian su HTML. Cada fuente falla por separado y el resto sigue funcionando; revisa `journalctl` si una deja de aparecer.
- Desde IPs de datacenter, Cloudflare puede bloquear Letterboxd o Rotten Tomatoes igual que hace con FilmAffinity. Compruébalo con `LOG_LEVEL=INFO`: verás `HTTP 403` en el log.
- Metacritic no tiene una búsqueda en HTML estático, así que se prueba el *slug* del título en inglés (y `slug-año`). Si falla y hay `OMDB_API_KEY`, se usa el Metascore de OMDb.
