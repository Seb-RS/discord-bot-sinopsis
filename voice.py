"""Narración por voz: si quien usa /pelicula está en un canal de voz, el bot
entra, lee el título y la sinopsis, y se va.

TTS con edge-tts (voces neuronales de Microsoft Edge, sin modelos locales).
El audio se genera en memoria y FFmpeg lo pasa directamente a Opus, así que
no hay archivos temporales ni codificación en Python.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from collections import defaultdict

import discord
import edge_tts

log = logging.getLogger("voice")

MAX_CHARS = 700       # sinopsis muy largas se cortan en la última frase completa
PLAY_TIMEOUT = 120    # s; red de seguridad por si la reproducción se cuelga
MAX_QUEUE = 3         # lecturas en cola por servidor; más se descartan (evita monopolizar la voz)


def narration_text(title: str, year: int | None, overview: str) -> str:
    text = f"{title}, de {year}." if year else f"{title}."
    if overview:
        if len(overview) > MAX_CHARS:
            cut = overview[:MAX_CHARS]
            # Cortar en el último punto; si no hay, en el último espacio.
            m = re.search(r"^.*[.!?…](?=\s|$)", cut, re.S)
            overview = m.group(0) if m else cut.rsplit(" ", 1)[0] + "…"
        text += " " + overview
    return text


class Narrator:
    def __init__(self, voice: str = "es-MX-DaliaNeural", ffmpeg: str = "ffmpeg"):
        self.voice, self.ffmpeg = voice, ffmpeg
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.pending: dict[int, int] = defaultdict(int)

    async def tts(self, text: str) -> bytes:
        buf = bytearray()
        async for chunk in edge_tts.Communicate(text, self.voice).stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
        return bytes(buf)

    @staticmethod
    def can_speak(channel: discord.VoiceChannel | discord.StageChannel) -> bool:
        me = channel.guild.me
        perms = channel.permissions_for(me) if me else None
        return bool(perms and perms.connect and perms.speak)

    async def speak(self, channel: discord.VoiceChannel | discord.StageChannel, text: str) -> None:
        guild = channel.guild
        if self.pending.get(guild.id, 0) >= MAX_QUEUE:
            log.info("Cola de voz llena en %s: se omite la lectura", guild.id)
            return
        self.pending[guild.id] += 1
        try:
            audio = await self.tts(text)      # antes de entrar: nada de silencios incómodos
        except BaseException:
            self.pending[guild.id] -= 1
            if not self.pending[guild.id]:
                del self.pending[guild.id]
            raise
        try:
            # Cola por servidor: si dos personas piden a la vez, se leen por turnos.
            async with self.locks[guild.id]:
                vc: discord.VoiceClient | None = guild.voice_client  # type: ignore[assignment]
                if vc and vc.channel != channel:
                    await vc.move_to(channel)
                elif not vc:
                    vc = await channel.connect(timeout=15, self_deaf=True)

                done = asyncio.Event()
                loop = asyncio.get_running_loop()
                source = discord.FFmpegOpusAudio(io.BytesIO(audio), pipe=True, executable=self.ffmpeg)
                vc.play(source, after=lambda err: loop.call_soon_threadsafe(done.set))
                try:
                    await asyncio.wait_for(done.wait(), PLAY_TIMEOUT)
                except asyncio.TimeoutError:
                    vc.stop()
        finally:
            self.pending[guild.id] -= 1
            # Solo se desconecta si no queda nadie en la cola.
            if not self.pending[guild.id]:
                del self.pending[guild.id]
                if guild.voice_client:
                    await guild.voice_client.disconnect(force=False)
