"""Bot de Discord: /pelicula <título> → sinopsis, portada y valoración agregada."""
from __future__ import annotations

import asyncio
import logging
import os
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
        # Solo el intent `guilds` (lo exige discord.py para funcionar bien):
        # el bot responde a slash commands, no necesita miembros, mensajes ni presencias.
        super().__init__(
            intents=discord.Intents(guilds=True),
            member_cache_flags=discord.MemberCacheFlags.none(),
            max_messages=None,
            chunk_guilds_at_startup=False,
        )
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None
        self.service: MovieService | None = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12),
            connector=aiohttp.TCPConnector(limit=16, ttl_dns_cache=600),
            headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
        )
        self.service = MovieService(self.http_session, os.environ["TMDB_API_KEY"],
                                    os.getenv("OMDB_API_KEY", ""),
                                    filmaffinity=os.getenv("ENABLE_FILMAFFINITY") == "1",
                                    region=os.getenv("WATCH_REGION") or "AR")
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

    region = bot.service.tmdb.region
    watch = []
    if m.stream:
        watch.append("**Suscripción:** " + ", ".join(m.stream[:6]))
    if m.rent_buy:
        watch.append("**Alquiler/compra:** " + ", ".join(m.rent_buy[:6]))
    if m.watch_link and watch:
        watch.append(f"[Ver todas las opciones]({m.watch_link})")
    e.add_field(name=f"📺 Dónde verla ({region})",
                value="\n".join(watch) or "No está en plataformas de streaming en tu región.",
                inline=False)

    e.set_footer(text="Notas normalizadas a 0–100 · Crítica y público pesan 50/50 · "
                      "Datos y portada: TMDB · Plataformas: JustWatch")
    return e


# ───────────────────────── comandos ─────────────────────────

@bot.tree.command(name="pelicula", description="Sinopsis, portada y valoración media de una película")
@app_commands.describe(titulo="Título en español (LAT o España) o inglés. Puedes añadir el año: «Origen 2010»")
@app_commands.checks.cooldown(1, 8.0, key=lambda i: i.user.id)
async def pelicula(inter: discord.Interaction, titulo: app_commands.Range[str, 1, 100]):
    await inter.response.defer(thinking=True)
    try:
        res = await bot.service.lookup(titulo)
    except Exception:
        log.exception("Error buscando %r", titulo)
        await inter.followup.send("⚠️ Error consultando los datos. Prueba de nuevo en un rato.")
        return
    if not res:
        await inter.followup.send(f"🔍 No encontré ninguna película para **{titulo}**.")
        return
    await inter.followup.send(embed=build_embed(res))


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
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ Espera {error.retry_after:.0f} s antes de volver a usarlo."
    else:
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
