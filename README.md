# Bot de sinopsis y valoraciones

`/pelicula` sin nada abre un formulario de búsqueda (título + «leer en voz»). `/pelicula titulo:<nombre>` busca directamente, con autocompletado, y devuelve la sinopsis en español, la portada, los títulos (LAT / España / inglés / original), una **valoración media** a partir de varias webs de crítica y de público, y **hasta 3 películas parecidas**. Cada una tiene un botón que abre un formulario con el título y «leer en voz» ya rellenos, para ajustarlos antes de enviar.

**Un solo canal:** con `ALLOWED_CHANNEL_IDS` (separados por comas) el bot solo responde ahí; en otros canales avisa, solo a quien lo usó, de dónde usarlo. Para que el comando ni aparezca en otros canales: Ajustes del servidor → Integraciones → el bot → Canales.

**Voz:** si quien usa el comando está en un canal de voz, el bot entra, lee el título y la sinopsis (voz neuronal en español latino vía `edge-tts`) y se va. Si varias personas piden a la vez, las lee por turnos. Para pedir una película sin que entre a leer, usa la opción `leer: False` (los botones de recomendadas heredan esa elección). Se desactiva del todo con `ENABLE_VOICE=0`.

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

## Recomendaciones

Las recomendaciones de TMDB tal cual mezclan mal parecido y popularidad (p. ej. *Avengers: Endgame* para *Interstellar*), así que se calculan por **temática**, con TF-IDF sobre las palabras clave de TMDB:

1. Por cada palabra clave de la película se piden las películas más votadas que la tienen. El total de resultados indica lo rara que es: *agujero de gusano* (10 películas) pesa mucho más que *distopía* (267).
2. **Puntuación** = Σ IDF de las palabras clave compartidas × (0,5 + similitud de géneros) × nota bayesiana / 7.
3. **Reglas**: al menos un género en común, ≥ 300 votos y como mucho una película por saga.
4. Cada recomendación muestra los 3 temas en común más específicos, para que se vea **por qué** sale.

Ejemplos: *Interstellar* → *Pasajeros*, *Misión Rescate*, *2001*; *El Padrino* → *El Padrino II*, *El irlandés*, *Caracortada*; *Relatos salvajes* → *Un día de furia*, *Tiempos violentos*. Falla cuando las palabras clave de TMDB son pobres (*El origen* → *Matrix recargado*, *Misión Imposible 5*). Las palabras clave de TMDB solo existen en inglés.

Coste: unas 30 peticiones pequeñas a TMDB por película nueva (en paralelo, ~1 s), que se cachean 12 h.

## Ligereza

- Dependencias: `discord.py[voice]` (incluye `aiohttp`) y `edge-tts`, más FFmpeg en el sistema para la voz. Sin BeautifulSoup, sin Selenium ni Playwright: todo son regex sobre el HTML.
- Solo el intent `guilds` y sin caché de miembros ni mensajes: el bot solo atiende slash commands.
- Caché en memoria acotada: 200 fichas (12 h) y 500 búsquedas (30 min). Sin base de datos.
- `DEV_GUILD_ID` registra el comando solo en ese servidor (cambios al instante) y borra la versión global para no duplicarlo. Para un bot de un solo servidor, déjalo puesto también en el VPS.
- Las fuentes se consultan en paralelo con un timeout de 10 s cada una. Si dos usuarios piden la misma película a la vez, solo se scrapea una vez.
- Cooldown de 8 s por usuario.

## Requisitos

- **Python 3.10+** y **FFmpeg** (solo para la voz).
- Un bot de Discord: [Developer Portal](https://discord.com/developers/applications) → *New Application* → **Bot** → *Reset Token*. No necesita ningún *privileged intent*.
- Una API key de [TMDB](https://www.themoviedb.org/settings/api) (gratis).
- Opcional: una API key de [OMDb](https://www.omdbapi.com/apikey.aspx) para las notas de IMDb (gratis, 1.000 consultas/día).

**Invitar el bot:** Developer Portal → OAuth2 → URL Generator → scopes `bot` y `applications.commands`; permisos `Send Messages`, `Embed Links`, `Connect` y `Speak`.

## Probarlo en local

```bash
git clone https://github.com/Seb-RS/discord-bot-sinopsis.git
cd discord-bot-sinopsis
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
cp .env.example .env                          # y rellena las claves
.venv/bin/python bot.py                       # Windows: .venv\Scripts\python bot.py
```

Pon `DEV_GUILD_ID` con el ID de tu servidor para que el comando aparezca al instante.

## Instalación en un servidor (Debian/Ubuntu)

El código queda a nombre de tu usuario (para actualizar con `git pull` sin `sudo`) y el bot corre como un usuario de sistema sin privilegios ni login. No abre puertos y tiene límites de memoria y CPU, así que puede convivir con otros servicios.

```bash
sudo apt install -y git ffmpeg python3-venv
sudo useradd -r -s /usr/sbin/nologin sinopsis
sudo mkdir /opt/sinopsis-bot && sudo chown "$USER": /opt/sinopsis-bot
git clone https://github.com/Seb-RS/discord-bot-sinopsis.git /opt/sinopsis-bot
cd /opt/sinopsis-bot
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
cp .env.example .env && vi .env                # rellena las claves; FFMPEG_PATH vacío
sudo chown "$USER":sinopsis .env && chmod 640 .env
sudo cp sinopsis-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sinopsis-bot
```

Comprobar que funciona (debe salir `connected to Gateway`):

```bash
sudo journalctl -u sinopsis-bot -n 20 --no-pager
```

Actualizar:

```bash
git -C /opt/sinopsis-bot pull && sudo systemctl restart sinopsis-bot
```

Si cambia `requirements.txt`, antes: `/opt/sinopsis-bot/.venv/bin/pip install -q -r /opt/sinopsis-bot/requirements.txt`.

## Límites conocidos

- El scraping se rompe si las webs cambian su HTML. Cada fuente falla por separado y el resto sigue funcionando; revisa `journalctl` si una deja de aparecer.
- Desde IPs de datacenter, Cloudflare puede bloquear Letterboxd o Rotten Tomatoes igual que hace con FilmAffinity. Compruébalo con `LOG_LEVEL=INFO`: verás `HTTP 403` en el log.
- Metacritic no tiene una búsqueda en HTML estático, así que se prueba el *slug* del título en inglés (y `slug-año`). Si falla y hay `OMDB_API_KEY`, se usa el Metascore de OMDb.

## Aviso legal

Proyecto personal y educativo, sin ánimo de lucro.

- **No está afiliado, patrocinado ni respaldado** por Rotten Tomatoes, Metacritic, Letterboxd, IMDb, FilmAffinity, TMDB, OMDb, Microsoft ni Discord. Todas las marcas pertenecen a sus dueños.
- **Scraping:** el bot lee páginas públicas de Rotten Tomatoes, Metacritic y Letterboxd. Sus condiciones de uso pueden prohibir el acceso automatizado. Revisa esas condiciones antes de usarlo; **cada persona es responsable de cómo lo usa**. El bot hace pocas peticiones, guarda los resultados 12 h y no intenta saltarse protecciones anti-bots (por eso FilmAffinity va desactivado).
- **TMDB:** este producto usa la API de TMDB, pero TMDB no lo respalda ni lo certifica.
- **Voz:** `edge-tts` usa un servicio no oficial de Microsoft que puede dejar de funcionar en cualquier momento.

## Licencia

[MIT](LICENSE) © 2026 Sebdev
