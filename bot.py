"""Bot de Discord: /pelicula <título> → sinopsis, portada y valoración agregada."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import aiohttp
import discord
from discord import app_commands

from service import MovieService, Result

log = logging.getLogger("bot")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    """Lector de .env de 5 líneas para no depender de python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Bot(discord.Client):
    def __init__(self):
        # Intents mínimos: `guilds` (lo exige discord.py) y `voice_states` (para saber
        # si quien usa el comando está en un canal de voz). Ni miembros, ni mensajes,
        # ni presencias: los estados de voz se guardan aparte de la caché de miembros.
        super().__init__(
            intents=discord.Intents(guilds=True, voice_states=True),
            member_cache_flags=discord.MemberCacheFlags.none(),
            max_messages=None,
            chunk_guilds_at_startup=False,
        )
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None
        self.service: MovieService | None = None
        self.narrator = None  # voice.Narrator si la voz está activada

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12),
            connector=aiohttp.TCPConnector(limit=16, ttl_dns_cache=600),
            headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
        )
        self.service = MovieService(self.http_session, os.environ["TMDB_API_KEY"],
                                    os.getenv("OMDB_API_KEY", ""),
                                    filmaffinity=os.getenv("ENABLE_FILMAFFINITY") == "1")
        self.add_dynamic_items(RecButton)  # botones de recomendadas
        if os.getenv("ENABLE_VOICE", "1") == "1":
            try:
                from voice import Narrator
                self.narrator = Narrator(os.getenv("TTS_VOICE") or "es-MX-DaliaNeural",
                                         os.getenv("FFMPEG_PATH") or "ffmpeg")
            except ImportError as e:  # falta edge-tts o los extras de voz
                log.warning("Voz desactivada: %s", e)
        if gid := os.getenv("DEV_GUILD_ID"):
            # En desarrollo: sincroniza al instante en un servidor concreto.
            guild = discord.Object(int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()  # global: puede tardar hasta 1 h en aparecer

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = Bot()


# ───────────────────────── presentación ─────────────────────────

def es_num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def bar(score: float, width: int = 10) -> str:
    filled = round(score / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def color(score: float | None) -> discord.Color:
    if score is None:
        return discord.Color.light_grey()
    if score >= 70:
        return discord.Color.green()
    if score >= 50:
        return discord.Color.gold()
    return discord.Color.red()


def build_embed(res: Result) -> discord.Embed:
    m = res.movie
    title = f"{m.title_lat} ({m.year})" if m.year else m.title_lat
    overview = m.overview or "*Sin sinopsis disponible.*"
    if len(overview) > 1500:
        overview = overview[:1500].rsplit(" ", 1)[0] + "…"

    e = discord.Embed(title=title[:256], url=m.url, description=overview, color=color(res.overall))
    if m.poster:
        e.set_thumbnail(url=m.poster)

    # Títulos en cada variante, solo si difieren entre sí.
    seen, lines = set(), []
    for flag, t in (("🇲🇽 LAT", m.title_lat), ("🇪🇸 ES", m.title_es),
                    ("🇺🇸 EN", m.title_en), ("🌐 Original", m.original_title)):
        if t and t.lower() not in seen:
            seen.add(t.lower())
            lines.append(f"{flag}: {t}")
    if len(lines) > 1:
        e.add_field(name="Títulos", value="\n".join(lines), inline=True)

    info = []
    if m.genres:
        info.append(", ".join(m.genres[:3]))
    if m.runtime:
        info.append(f"{m.runtime // 60} h {m.runtime % 60:02d} min")
    if info:
        e.add_field(name="Ficha", value="\n".join(info), inline=True)

    if res.overall is not None:
        parts = []
        if res.critics is not None:
            parts.append(f"Crítica **{res.critics:.0f}**")
        if res.audience is not None:
            parts.append(f"Público **{res.audience:.0f}**")
        e.add_field(
            name="⭐ Valoración global",
            value=f"# {res.overall:.0f}/100\n{bar(res.overall)}\n" + " · ".join(parts),
            inline=False,
        )

    if res.ratings:
        rows = []
        for r in res.ratings:
            count = f" · {es_num(r.count)} {'reseñas' if r.kind == 'crítica' else 'votos'}" if r.count else ""
            row = f"[{r.source}]({r.url}) ({r.kind}): **{r.display}** → {r.score:.0f}{count}"
            rows.append(row if r.reliable else f"~~{row}~~ *(muestra insuficiente)*")
        e.add_field(name="Fuentes", value="\n".join(rows)[:1024], inline=False)
    else:
        e.add_field(name="Fuentes", value="No se pudo obtener ninguna valoración.", inline=False)

    if m.recommendations:
        recs = [f"**{r['title']}**" + (f" ({r['year']})" if r["year"] else "")
                + f" · ⭐ {r['vote']:.1f}"
                + (f"\n-# En común: {', '.join(r['themes'])}" if r.get("themes") else "")
                for r in m.recommendations]
        e.add_field(name="Peliculas parecidas:", value="\n".join(recs), inline=False)

    e.set_footer(text="Notas normalizadas a 0–100 · Crítica y público pesan 50/50 · "
                      "Datos y portada: TMDB")
    return e


class MovieModal(discord.ui.Modal):
    """Formulario que abre un botón de recomendada: título y «leer en voz»
    ya rellenos, para ajustarlos antes de enviar (Discord no deja a un bot
    escribir un comando en la barra de mensajes del usuario)."""

    def __init__(self, movie_id: int, label: str, read: bool):
        super().__init__(title=f"🎬 {label}"[:45], timeout=600)
        self.movie_id, self.label = movie_id, label
        self.titulo = discord.ui.TextInput(default=label, max_length=100)
        self.leer = discord.ui.Checkbox(default=read)
        self.add_item(discord.ui.Label(
            text="Título", description="Puedes cambiarlo o añadir el año", component=self.titulo))
        self.add_item(discord.ui.Label(
            text="Leer en voz", description="Si estás en un canal de voz, el bot entra a leer",
            component=self.leer))

    async def on_submit(self, inter: discord.Interaction):
        if retry := cooldown_left(inter.user.id):
            await inter.response.send_message(f"⏳ Espera {retry:.0f} s antes de volver a usarlo.",
                                              ephemeral=True)
            return
        titulo = self.titulo.value.strip()
        # Sin cambios → se usa el ID exacto; si lo editó, se busca lo que escribió.
        query = f"tmdb:{self.movie_id}" if titulo == self.label else titulo
        await send_movie(inter, query, self.leer.value)


class RecButton(discord.ui.DynamicItem[discord.ui.Button], template=r"peli:(?P<id>\d+)(?::(?P<read>[01]))?"):
    """Botón de película recomendada. Es «dinámico»: el ID y si leer en voz van
    en el custom_id, así que sigue funcionando tras reiniciar el bot y no ocupa memoria."""

    def __init__(self, movie_id: int, read: bool = True, label: str = "Ver"):
        super().__init__(discord.ui.Button(
            label=label, emoji="🎬", style=discord.ButtonStyle.secondary,
            custom_id=f"peli:{movie_id}:{int(read)}"))
        self.movie_id, self.read = movie_id, read

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        # Los botones antiguos no llevan «:0/1» → se leen (comportamiento anterior).
        return cls(int(match["id"]), match["read"] != "0", item.label or "Ver")

    async def callback(self, inter: discord.Interaction):
        # Abre el formulario; el cooldown se comprueba al enviarlo, no al abrirlo.
        await inter.response.send_modal(MovieModal(self.movie_id, self.item.label or "", self.read))


def recs_view(res: Result, read: bool) -> discord.ui.View | None:
    if not res.movie.recommendations:
        return None
    view = discord.ui.View(timeout=1)  # sin estado: los clics los atiende RecButton
    for r in res.movie.recommendations:
        label = f"{r['title']} ({r['year']})" if r["year"] else r["title"]
        view.add_item(RecButton(r["id"], read, label[:80]))
    return view


async def send_movie(inter: discord.Interaction, query: str, read: bool = True):
    await inter.response.defer(thinking=True)
    try:
        res = await bot.service.lookup(query)
    except Exception:
        log.exception("Error buscando %r", query)
        await inter.followup.send("⚠️ Error consultando los datos. Prueba de nuevo en un rato.")
        return
    if not res:
        await inter.followup.send(f"🔍 No encontré ninguna película para **{query}**.")
        return
    kwargs = {"embed": build_embed(res)}
    if view := recs_view(res, read):
        kwargs["view"] = view
    await inter.followup.send(**kwargs)
    if read:
        narrate(inter, res)


def narrate(inter: discord.Interaction, res: Result) -> None:
    """Si quien pidió la película está en un canal de voz, el bot la lee allí."""
    voice = getattr(inter.user, "voice", None)  # solo Member (en servidores) tiene .voice
    if not (bot.narrator and voice and voice.channel):
        return
    if not bot.narrator.can_speak(voice.channel):
        log.info("Sin permiso de Conectar/Hablar en #%s", voice.channel)
        return
    from voice import narration_text
    m = res.movie
    task = asyncio.create_task(bot.narrator.speak(voice.channel, narration_text(m.title_lat, m.year, m.overview)))
    _voice_tasks.add(task)  # referencia fuerte: si no, el GC podría cancelar la tarea

    def done(t: asyncio.Task):
        _voice_tasks.discard(t)
        if not t.cancelled() and (err := t.exception()):
            log.warning("Fallo leyendo «%s» en voz: %r", m.title_lat, err)
    task.add_done_callback(done)


_voice_tasks: set[asyncio.Task] = set()


# ───────────────────────── comandos ─────────────────────────

COOLDOWN = 8.0
_last_use: dict[int, float] = {}


def cooldown_left(user_id: int) -> float:
    """Segundos que le faltan al usuario (0 si puede usarlo). Compartido por el
    comando y los botones."""
    now = time.monotonic()
    if (left := _last_use.get(user_id, 0) + COOLDOWN - now) > 0:
        return left
    _last_use[user_id] = now
    if len(_last_use) > 1000:  # limpieza para que el dict no crezca sin fin
        for uid in [u for u, t in _last_use.items() if now - t > COOLDOWN]:
            del _last_use[uid]
    return 0


@bot.tree.command(name="pelicula", description="Sinopsis, portada y valoración media de una película")
@app_commands.describe(
    titulo="Título en español (LAT o España) o inglés. Puedes añadir el año: «Origen 2010»",
    leer="Si estás en un canal de voz, el bot entra y lee título y sinopsis (por defecto: sí)",
)
async def pelicula(inter: discord.Interaction, titulo: app_commands.Range[str, 1, 100],
                   leer: bool = True):
    if retry := cooldown_left(inter.user.id):
        await inter.response.send_message(f"⏳ Espera {retry:.0f} s antes de volver a usarlo.",
                                          ephemeral=True)
        return
    await send_movie(inter, titulo, leer)


@pelicula.autocomplete("titulo")
async def titulo_autocomplete(inter: discord.Interaction, current: str):
    if len(current.strip()) < 2:
        return []
    try:
        # Discord da 3 s para responder al autocompletado.
        results = await asyncio.wait_for(bot.service.suggest(current), 2.5)
    except Exception:
        return []
    choices = []
    for r in results[:10]:
        year = (r.get("release_date") or "")[:4]
        name = r.get("title") or r.get("original_title") or "?"
        if r.get("original_title") and r["original_title"].lower() != name.lower():
            name += f" / {r['original_title']}"
        label = f"{name} ({year})" if year else name
        choices.append(app_commands.Choice(name=label[:100], value=f"tmdb:{r['id']}"))
    return choices


@bot.tree.error
async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Error en comando", exc_info=error)
    msg = "⚠️ Algo salió mal."
    if inter.response.is_done():
        await inter.followup.send(msg, ephemeral=True)
    else:
        await inter.response.send_message(msg, ephemeral=True)


def main():
    load_env()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if token.count(".") != 2:
        raise SystemExit(
            "DISCORD_TOKEN no tiene formato de token de bot (debe ser xxxx.yyyy.zzzz).\n"
            "Ojo: la «Public Key» y el «Client Secret» NO sirven. Sácalo en "
            "Developer Portal → tu app → Bot → Reset Token.")
    if not os.getenv("TMDB_API_KEY"):
        raise SystemExit("Falta TMDB_API_KEY en el .env.")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
