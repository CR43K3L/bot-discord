"""
Bot Discord — Musique + Logs + Logs vocaux + TikTok Live (opt) + Embed Builder 100% custom (fix)
discord.py 2.4.0 — Python 3.10+

Installer dans le venv :
  python -m pip install "discord.py[voice]==2.4.0" python-dotenv==1.0.1 yt-dlp TikTokLive

Exemple .env :
DISCORD_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
# GUILD_IDS=123,...   (facultatif, pour sync rapide sur des guildes spécifiques)
# FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe  (si ffmpeg n'est pas dans le PATH)
# LOG_LEVEL=INFO
# LOG_FILE=logs/bot.log
# LOG_CHANNEL_ID=123456789012345678     (si tu veux envoyer des logs dans un salon)
# LIVE_CHANNEL_ID=123456789012345678    (pour renommer un salon en Live ON/OFF)
# LIVE_NAME_ON=🟢・Live ON
# LIVE_NAME_OFF=🔴・Live OFF
# TIKTOK_USERNAME=monusername
# TIKTOK_POLL_SECONDS=10
"""

from __future__ import annotations
import os
import re
import sys
import shutil
import asyncio
import logging
import textwrap
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, List

import discord
from discord import app_commands, FFmpegPCMAudio
from discord.ext import commands
from dotenv import load_dotenv

# ---- yt-dlp (musique)
try:
    import yt_dlp  # type: ignore
except Exception:
    yt_dlp = None

# ---- TikTok (optionnel)
try:
    from TikTokLive import TikTokLiveClient  # type: ignore
except Exception:
    TikTokLiveClient = None

# ---------- .env ----------
ENV_PATH = Path(__file__).with_name(".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)

# ================== LOGGING (console + fichier + filtre httpx) ==================
def setup_logging():
    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = os.getenv("LOG_FILE") or "logs/bot.log"
    max_bytes = int(os.getenv("LOG_MAX_BYTES") or 1_048_576)
    backups = int(os.getenv("LOG_BACKUPS") or 5)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    console_fmt = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
    file_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(filename)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(console_fmt)
    root.addHandler(ch)

    fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(file_fmt)
    root.addHandler(fh)

    # calmer le bruit des libs
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.ERROR)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    # couper les gros logs httpx/TikTokLive
    for name in ["httpx", "httpcore", "aiohttp.client", "websockets", "TikTokLive", "tiktoklive"]:
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.propagate = False

    logging.getLogger("bot").info("Logging initialisé (niveau=%s, fichier=%s)", level_name, log_file)

setup_logging()
log = logging.getLogger("bot")

# ================== CONFIG ==================
TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_IDS_ENV = os.getenv("GUILD_IDS") or os.getenv("GUILD_ID")
GUILD_IDS: list[int] = [int(x) for x in re.split(r"[,;\s]+", GUILD_IDS_ENV.strip()) if x] if GUILD_IDS_ENV else []

FFMPEG_EXE = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
log.info("[FFmpeg] using: %s", FFMPEG_EXE)

# TikTok (optionnel)
LIVE_CHANNEL_ID = int(os.getenv("LIVE_CHANNEL_ID", "0")) or None
LIVE_NAME_ON = os.getenv("LIVE_NAME_ON", "🟢・Live ON")
LIVE_NAME_OFF = os.getenv("LIVE_NAME_OFF", "🔴・Live OFF")
TIKTOK_USERNAME = (os.getenv("TIKTOK_USERNAME") or "").strip()
TIKTOK_POLL_SECONDS = int(os.getenv("TIKTOK_POLL_SECONDS", "10"))
_current_live_state: bool | None = None

# Logs → salon Discord (optionnel)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0")) or None
LOG_DISCORD_LEVEL = (os.getenv("LOG_DISCORD_LEVEL") or "ERROR").upper()

# Musique
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "noplaylist": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"

# ================== Intents & Bot ==================
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================== LOGS → SALON DISCORD ==================
class DiscordChannelHandler(logging.Handler):
    def __init__(self, bot: commands.Bot, channel_id: int, level=logging.ERROR):
        super().__init__(level)
        self.bot = bot
        self.channel_id = channel_id

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
        except Exception:
            return
        asyncio.create_task(self._send(msg))

    async def _send(self, msg: str):
        try:
            if not self.bot.is_ready():
                return
            ch = self.bot.get_channel(self.channel_id) or await self.bot.fetch_channel(self.channel_id)
            for chunk in textwrap.wrap(msg, width=1900):
                await ch.send(f"```{chunk}```")
        except Exception:
            pass

def attach_discord_log_handler():
    if not LOG_CHANNEL_ID:
        return
    level = getattr(logging, LOG_DISCORD_LEVEL, logging.ERROR)
    h = DiscordChannelHandler(bot, LOG_CHANNEL_ID, level=level)
    h.setFormatter(logging.Formatter("%H:%M:%S | %(levelname)s | %(name)s | %(message)s"))
    logging.getLogger().addHandler(h)

attach_discord_log_handler()

# ================== HELPERS généraux ==================
def _user_tag(u: discord.abc.User) -> str:
    try:
        return f"{u.name}#{u.discriminator}({u.id})"
    except Exception:
        return str(getattr(u, "id", "unknown"))

def _place(i: discord.Interaction) -> str:
    g = getattr(i.guild, "name", "DM")
    c = getattr(i.channel, "name", str(getattr(i.channel, "id", "DM")))
    return f"{g} / {c}"

def log_cmd_start(interaction: discord.Interaction, cmd_name: str):
    log.info("▶️ /%s par %s @ %s", cmd_name, _user_tag(interaction.user), _place(interaction))

def log_cmd_ok(interaction: discord.Interaction, cmd_name: str):
    log.info("✅ /%s OK pour %s @ %s", cmd_name, _user_tag(interaction.user), _place(interaction))

def log_cmd_err(interaction: discord.Interaction, cmd_name: str, err: Exception):
    log.error("❌ /%s ERROR pour %s @ %s → %s", cmd_name, _user_tag(interaction.user), _place(interaction), err, exc_info=err)

async def safe_reply(interaction: discord.Interaction, content: str = "", *, embed: discord.Embed | None = None, ephemeral: bool = True):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content or None, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content or None, embed=embed, ephemeral=ephemeral)
    except Exception as e:
        log.warning("[safe_reply] send failed: %s", e)

# ================== VOICE HELPERS ==================
_vc_connect_lock = asyncio.Lock()

def build_ffmpeg_source(stream_url: str) -> FFmpegPCMAudio:
    return FFmpegPCMAudio(
        stream_url,
        executable=FFMPEG_EXE,
        before_options=FFMPEG_BEFORE,
        options=FFMPEG_OPTS,
    )

async def ensure_connected_to_user_vc(interaction: discord.Interaction) -> discord.VoiceClient | None:
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=False, thinking=True)
        except Exception:
            pass

    if not interaction.user or not isinstance(interaction.user, discord.Member):
        await safe_reply(interaction, "Impossible de trouver ton salon vocal.")
        return None

    voice_state = interaction.user.voice
    if not voice_state or not voice_state.channel:
        await safe_reply(interaction, "Rejoins un **salon vocal** d'abord 😉")
        return None

    async with _vc_connect_lock:
        vc: discord.VoiceClient | None = interaction.guild.voice_client
        try:
            if vc and vc.is_connected():
                if vc.channel.id != voice_state.channel.id:
                    await vc.move_to(voice_state.channel, timeout=12)
            else:
                vc = await voice_state.channel.connect(self_deaf=True, reconnect=False, timeout=12)
            return vc
        except asyncio.TimeoutError:
            await safe_reply(interaction, "⏳ Connexion vocal **timeout**. Vérifie pare-feu/VPN et réessaie.")
        except discord.Forbidden:
            await safe_reply(interaction, "Pas la permission de rejoindre ce salon vocal.")
        except Exception as e:
            await safe_reply(interaction, f"Connexion vocal impossible: `{e}`")
            log.exception("Voice connect error")
        return None

# ================== TIKTOK (optionnel) ==================
async def set_live_channel_name(is_live: bool):
    global _current_live_state
    if LIVE_CHANNEL_ID is None:
        return
    if _current_live_state is is_live:
        return
    channel = bot.get_channel(LIVE_CHANNEL_ID) or await bot.fetch_channel(LIVE_CHANNEL_ID)
    new_name = LIVE_NAME_ON if is_live else LIVE_NAME_OFF
    try:
        await channel.edit(name=new_name, reason="TikTok LIVE status")
        _current_live_state = is_live
        log.info("🔁 Salon renommé en: %s", new_name)
    except discord.Forbidden:
        log.error("Permission manquante: Gérer les salons.")
    except Exception as e:
        log.exception("Erreur lors du renommage: %s", e)

async def tiktok_watch_loop():
    if not TIKTOK_USERNAME:
        log.info("TIKTOK_USERNAME non défini → TikTok désactivé.")
        return
    if TikTokLiveClient is None:
        log.warning("TikTokLive non installé. Fais: pip install TikTokLive")
        return
    client = TikTokLiveClient(unique_id=TIKTOK_USERNAME)
    while True:
        try:
            is_live = await client.is_live()
            await set_live_channel_name(bool(is_live))
        except Exception as e:
            log.warning("TikTok watch error: %s", e)
        await asyncio.sleep(TIKTOK_POLL_SECONDS)

# ================== EVENTS ==================
@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="vos commandes /"))
    try:
        if GUILD_IDS:
            for gid in GUILD_IDS:
                guild = discord.Object(id=int(gid))
                bot.tree.copy_global_to(guild=guild)
                synced_guild = await bot.tree.sync(guild=guild)
                log.info("Sync guilde %s: %s", gid, [c.name for c in synced_guild])
            bot.tree.clear_commands(guild=None)
            synced_global = await bot.tree.sync()
            log.info("Global commands maintenant: %d", len(synced_global))
        else:
            synced = await bot.tree.sync()
            log.info("Slash commands globales synchronisées: %s", [c.name for c in synced])
    except Exception as e:
        log.exception("Erreur de sync des commandes: %s", e)

    asyncio.create_task(tiktok_watch_loop())
    log.info("Bot prêt: %s (ID: %s)", bot.user, bot.user.id)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    if before.channel is None and after.channel is not None:
        log.info("🔊 %s a rejoint %s", member.display_name, after.channel.name)
    elif before.channel is not None and after.channel is None:
        log.info("🔇 %s a quitté %s", member.display_name, before.channel.name)
    elif before.channel and after.channel and before.channel.id != after.channel.id:
        log.info("🔁 %s est passé de %s → %s", member.display_name, before.channel.name, after.channel.name)
    if before.self_mute != after.self_mute:
        log.info("🤐 %s %s (self-mute)", member.display_name, "s'est **muté**" if after.self_mute else "s'est **démuté**")
    if before.self_deaf != after.self_deaf:
        log.info("🙉 %s %s (self-deaf)", member.display_name, "s'est **deaf**" if after.self_deaf else "n'est plus **deaf**")
    if before.mute != after.mute:
        log.warning("⛔ %s %s", member.display_name, "a été **server-mute**" if after.mute else "n'est plus **server-mute**")
    if before.deaf != after.deaf:
        log.warning("⛔ %s %s", member.display_name, "a été **server-deaf**" if after.deaf else "n'est plus **server-deaf**")
    if before.self_stream != after.self_stream:
        log.info("📡 %s %s le **stream**", member.display_name, "a démarré" if after.self_stream else "a coupé")
    if before.self_video != after.self_video:
        log.info("🎥 %s a %s sa caméra", member.display_name, "allumé" if after.self_video else "éteint")

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.communication_disabled_until != after.communication_disabled_until:
        a = after.communication_disabled_until
        if a:
            log.warning("🕒 %s a été **timeout** jusqu'au %s", after.display_name, a.isoformat())
        else:
            log.info("🕒 %s n'est plus **timeout**", after.display_name)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    log_cmd_err(interaction, interaction.command.name if interaction.command else "unknown", error)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Une erreur est survenue pendant la commande.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Une erreur est survenue pendant la commande.", ephemeral=True)
    except Exception:
        pass

# ================== EMBED BUILDER — Helpers Couleur ==================
COLOR_NAMES = {
    "red": 0xED4245, "green": 0x57F287, "blue": 0x5865F2, "blurple": 0x5865F2,
    "yellow": 0xFEE75C, "orange": 0xF39C12, "purple": 0x9B59B6, "pink": 0xE91E63,
    "black": 0x000000, "white": 0xFFFFFF, "grey": 0x95A5A6, "gray": 0x95A5A6,
}
def parse_color(value: str | None) -> discord.Color | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in COLOR_NAMES:
        return discord.Color(COLOR_NAMES[v])
    v = v.replace("#", "")
    if v.startswith("0x"):
        v = v[2:]
    try:
        return discord.Color(int(v, 16))
    except Exception:
        return None

# ================== EMBED BUILDER — Core ==================
class EmbedDraft:
    """État de l'embed en cours d'édition."""
    def __init__(self):
        self.title: str | None = None
        self.description: str | None = None
        self.url: str | None = None
        self.color: Optional[discord.Color] = discord.Color.blurple()
        self.timestamp: bool = False

        self.author_name: Optional[str] = None
        self.author_icon: Optional[str] = None
        self.author_url: Optional[str] = None

        self.footer_text: Optional[str] = None
        self.footer_icon: Optional[str] = None

        self.image_url: Optional[str] = None
        self.thumb_url: Optional[str] = None

        self.fields: List[tuple[str, str, bool]] = []  # (name, value, inline)

    def to_embed(self) -> discord.Embed:
        emb = discord.Embed(
            title=self.title or discord.Embed.Empty,
            description=self.description or discord.Embed.Empty,
            url=self.url or None,
            color=self.color or discord.Color.blurple(),
        )
        if self.timestamp:
            emb.timestamp = discord.utils.utcnow()
        if self.author_name:
            emb.set_author(
                name=self.author_name,
                url=self.author_url or discord.Embed.Empty,
                icon_url=self.author_icon or discord.Embed.Empty
            )
        if self.footer_text:
            emb.set_footer(text=self.footer_text, icon_url=self.footer_icon or discord.Embed.Empty)
        if self.image_url:
            emb.set_image(url=self.image_url)
        if self.thumb_url:
            emb.set_thumbnail(url=self.thumb_url)
        for n, v, inline in self.fields:
            emb.add_field(name=n or "\u200b", value=v or "\u200b", inline=inline)
        return emb

# --------- utilitaire d’aperçu (FIX STABLE) ---------
async def update_preview(itx: discord.Interaction, draft: "EmbedDraft", view: discord.ui.View, edit_only: bool = False):
    emb = draft.to_embed()
    try:
        if itx.response.is_done():
            await itx.edit_original_response(content="**Aperçu** — modifie via les boutons :", embed=emb, view=view)
        else:
            if edit_only:
                await itx.response.defer()
                await itx.edit_original_response(content="**Aperçu** — modifie via les boutons :", embed=emb, view=view)
            else:
                await itx.response.edit_message(content="**Aperçu** — modifie via les boutons :", embed=emb, view=view)
    except Exception:
        try:
            await itx.followup.send(content="**Aperçu mis à jour** :", embed=emb, ephemeral=True)
        except Exception:
            pass

# --------- Modals ---------
class TitleDescModal(discord.ui.Modal, title="Titre & Description"):
    titre = discord.ui.TextInput(label="Titre", required=False, max_length=256)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False, max_length=4000)
    url = discord.ui.TextInput(label="URL du Titre (optionnel)", required=False, max_length=512)
    def __init__(self, draft: EmbedDraft, view_ref: discord.ui.View):
        super().__init__()
        self.draft = draft
        self.view_ref = view_ref
        self.titre.default = draft.title or ""
        self.description.default = draft.description or ""
        self.url.default = draft.url or ""
    async def on_submit(self, itx: discord.Interaction):
        self.draft.title = str(self.titre) or None
        self.draft.description = str(self.description) or None
        self.draft.url = str(self.url) or None
        await update_preview(itx, self.draft, self.view_ref)

class ColorModal(discord.ui.Modal, title="Couleur"):
    couleur = discord.ui.TextInput(label="Couleur", placeholder="blue | #5865F2 | ff0044", required=False, max_length=16)
    def __init__(self, draft: EmbedDraft, view_ref: discord.ui.View):
        super().__init__()
        self.draft = draft
        self.view_ref = view_ref
        if draft.color:
            self.couleur.default = f"#{draft.color.value:06x}"
    async def on_submit(self, itx: discord.Interaction):
        col = parse_color(str(self.couleur))
        self.draft.color = col or discord.Color.blurple()
        await update_preview(itx, self.draft, self.view_ref)

class ImagesModal(discord.ui.Modal, title="Images"):
    image_url = discord.ui.TextInput(label="Image URL", required=False, max_length=512)
    thumb_url = discord.ui.TextInput(label="Thumbnail URL", required=False, max_length=512)
    def __init__(self, draft: EmbedDraft, view_ref: discord.ui.View):
        super().__init__()
        self.draft = draft
        self.view_ref = view_ref
        self.image_url.default = draft.image_url or ""
        self.thumb_url.default = draft.thumb_url or ""
    async def on_submit(self, itx: discord.Interaction):
        self.draft.image_url = str(self.image_url) or None
        self.draft.thumb_url = str(self.thumb_url) or None
        await update_preview(itx, self.draft, self.view_ref)

class AuthorModal(discord.ui.Modal, title="Author (en-tête)"):
    name = discord.ui.TextInput(label="Nom", required=False, max_length=256)
    icon = discord.ui.TextInput(label="Icon URL (optionnel)", required=False, max_length=512)
    url = discord.ui.TextInput(label="URL (optionnel)", required=False, max_length=512)
    def __init__(self, draft: EmbedDraft, view_ref: discord.ui.View):
        super().__init__()
        self.draft = draft
        self.view_ref = view_ref
        self.name.default = draft.author_name or ""
        self.icon.default = draft.author_icon or ""
        self.url.default = draft.author_url or ""
    async def on_submit(self, itx: discord.Interaction):
        self.draft.author_name = str(self.name) or None
        self.draft.author_icon = str(self.icon) or None
        self.draft.author_url = str(self.url) or None
        await update_preview(itx, self.draft, self.view_ref)

class FooterModal(discord.ui.Modal, title="Footer (pied de page)"):
    text = discord.ui.TextInput(label="Texte", required=False, max_length=2048)
    icon = discord.ui.TextInput(label="Icon URL (optionnel)", required=False, max_length=512)
    def __init__(self, draft: EmbedDraft, view_ref: discord.ui.View):
        super().__init__()
        self.draft = draft
        self.view_ref = view_ref
        self.text.default = draft.footer_text or ""
        self.icon.default = draft.footer_icon or ""
    async def on_submit(self, itx: discord.Interaction):
        self.draft.footer_text = str(self.text) or None
        self.draft.footer_icon = str(self.icon) or None
        await update_preview(itx, self.draft, self.view_ref)

class FieldModal(discord.ui.Modal, title="Ajouter/Éditer un Field"):
    name = discord.ui.TextInput(label="Nom", required=False, max_length=256)
    value = discord.ui.TextInput(label="Valeur", style=discord.TextStyle.paragraph, required=False, max_length=1024)
    inline = discord.ui.TextInput(label="Inline ?", placeholder="oui/non (défaut: oui)", required=False, max_length=8)
    def __init__(self, draft: EmbedDraft, view_ref: discord.ui.View, index: Optional[int] = None):
        super().__init__()
        self.draft = draft
        self.view_ref = view_ref
        self.index = index
        if index is not None and 0 <= index < len(draft.fields):
            n, v, i = draft.fields[index]
            self.name.default = n
            self.value.default = v
            self.inline.default = "oui" if i else "non"
    async def on_submit(self, itx: discord.Interaction):
        n = str(self.name) or "\u200b"
        v = str(self.value) or "\u200b"
        i = (str(self.inline) or "oui").strip().lower() in {"oui", "yes", "true", "1", "y", "o"}
        if self.index is None:
            self.draft.fields.append((n, v, i))
        else:
            if 0 <= self.index < len(self.draft.fields):
                self.draft.fields[self.index] = (n, v, i)
        await update_preview(itx, self.draft, self.view_ref)

class EmbedBuilderView(discord.ui.View):
    def __init__(self, author_id: int, initial_channel: discord.abc.GuildChannel):
        super().__init__(timeout=600)
        self.author_id = author_id
        self.draft = EmbedDraft()
        self.target_channel_id = initial_channel.id

        # ✅ ChannelSelect (classe) — compatible discord.py 2.4.0
        chan_select = discord.ui.ChannelSelect(
            placeholder="Choisir le salon de destination",
            min_values=1, max_values=1,
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.voice,
                discord.ChannelType.forum,
            ],
            row=0
        )

        async def _on_select(itx: discord.Interaction):
            ch = chan_select.values[0]  # Channel choisi
            if isinstance(ch, (discord.TextChannel, discord.Thread, discord.ForumChannel, discord.VoiceChannel)):
                self.target_channel_id = ch.id
                await update_preview(itx, self.draft, self, edit_only=True)
            else:
                await itx.response.send_message("Salon invalide pour l’envoi.", ephemeral=True)

        chan_select.callback = _on_select  # on attache le callback
        self.add_item(chan_select)

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.author_id:
            await itx.response.send_message("Seul l’auteur peut modifier cet embed.", ephemeral=True)
            return False
        return True

    # --- Ligne 1 : contenu de base
    @discord.ui.button(label="Titre/Desc/URL", style=discord.ButtonStyle.primary, row=1)
    async def btn_title(self, itx: discord.Interaction, _btn: discord.ui.Button):
        await itx.response.send_modal(TitleDescModal(self.draft, self))

    @discord.ui.button(label="Couleur", style=discord.ButtonStyle.secondary, row=1)
    async def btn_color(self, itx: discord.Interaction, _btn: discord.ui.Button):
        await itx.response.send_modal(ColorModal(self.draft, self))

    @discord.ui.button(label="Timestamp ON/OFF", style=discord.ButtonStyle.secondary, row=1)
    async def btn_ts(self, itx: discord.Interaction, _btn: discord.ui.Button):
        self.draft.timestamp = not self.draft.timestamp
        await update_preview(itx, self.draft, self)

    # --- Ligne 2 : médias & méta
    @discord.ui.button(label="Images", style=discord.ButtonStyle.secondary, row=2)
    async def btn_images(self, itx: discord.Interaction, _btn: discord.ui.Button):
        await itx.response.send_modal(ImagesModal(self.draft, self))

    @discord.ui.button(label="Author", style=discord.ButtonStyle.secondary, row=2)
    async def btn_author(self, itx: discord.Interaction, _btn: discord.ui.Button):
        await itx.response.send_modal(AuthorModal(self.draft, self))

    @discord.ui.button(label="Footer", style=discord.ButtonStyle.secondary, row=2)
    async def btn_footer(self, itx: discord.Interaction, _btn: discord.ui.Button):
        await itx.response.send_modal(FooterModal(self.draft, self))

    # --- Ligne 3 : fields
    @discord.ui.button(label="Ajouter Field", style=discord.ButtonStyle.success, row=3)
    async def btn_add_field(self, itx: discord.Interaction, _btn: discord.ui.Button):
        await itx.response.send_modal(FieldModal(self.draft, self, index=None))

        @discord.ui.button(label="Éditer/Supprimer Field", style=discord.ButtonStyle.secondary, row=3)
        async def btn_edit_field(self, itx: discord.Interaction, _btn: discord.ui.Button):
            if not self.draft.fields:
                await itx.response.send_message("Aucun field à éditer.", ephemeral=True)
                return
            view = FieldManageView(self)
            await itx.response.send_message("Choisis un field à éditer/supprimer :", view=view, ephemeral=True)
    
    # --------- FieldManageView (ajouté pour gérer les fields) ---------
    class FieldManageView(discord.ui.View):
        def __init__(self, builder_view: EmbedBuilderView):
            super().__init__(timeout=120)
            self.builder_view = builder_view
            self.draft = builder_view.draft
    
            for idx, (name, value, inline) in enumerate(self.draft.fields):
                btn = discord.ui.Button(
                    label=f"{idx+1}: {name[:20]}",
                    style=discord.ButtonStyle.primary,
                    row=0
                )
                async def make_callback(index):
                    async def callback(itx: discord.Interaction):
                        await itx.response.send_modal(FieldModal(self.draft, self.builder_view, index=index))
                    return callback
                btn.callback = asyncio.coroutine(make_callback(idx))
                self.add_item(btn)
    
            # Bouton supprimer
            btn_del = discord.ui.Button(label="Supprimer un field", style=discord.ButtonStyle.danger, row=1)
            async def del_callback(itx: discord.Interaction):
                options = [
                    discord.SelectOption(label=f"{i+1}: {n[:20]}", value=str(i))
                    for i, (n, _, _) in enumerate(self.draft.fields)
                ]
                select = discord.ui.Select(placeholder="Choisis le field à supprimer", options=options)
                async def select_callback(sel_itx: discord.Interaction):
                    idx = int(select.values[0])
                    if 0 <= idx < len(self.draft.fields):
                        self.draft.fields.pop(idx)
                        await update_preview(sel_itx, self.draft, self.builder_view, edit_only=True)
                        await sel_itx.response.edit_message(content="Field supprimé.", view=None)
                select.callback = select_callback
                v = discord.ui.View()
                v.add_item(select)
                await itx.response.send_message("Sélectionne le field à supprimer :", view=v, ephemeral=True)
            btn_del.callback = del_callback
            self.add_item(btn_del)
    
        async def interaction_check(self, itx: discord.Interaction) -> bool:
            return await self.builder_view.interaction_check(itx)

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.danger, row=3)
    async def btn_reset(self, itx: discord.Interaction, _btn: discord.ui.Button):
        self.draft = EmbedDraft()
        await update_preview(itx, self.draft, self)

    # --- Ligne 4 : envoyer/annuler
    @discord.ui.button(label="📤 Envoyer", style=discord.ButtonStyle.success, row=4)
    async def btn_send(self, itx: discord.Interaction, _btn: discord.ui.Button):
        try:
            ch = itx.client.get_channel(self.target_channel_id) or await itx.client.fetch_channel(self.target_channel_id)
            await ch.send(embed=self.draft.to_embed())
            await itx.response.edit_message(content="✅ Embed envoyé.", embed=None, view=None)
        except discord.Forbidden:
            await itx.response.send_message("❌ Permission insuffisante dans ce salon.", ephemeral=True)
        except Exception as e:
            await itx.response.send_message(f"❌ Erreur à l’envoi : {e}", ephemeral=True)

    @discord.ui.button(label="🗑️ Annuler", style=discord.ButtonStyle.secondary, row=4)
    async def btn_cancel(self, itx: discord.Interaction, _btn: discord.ui.Button):
        await itx.response.edit_message(content="❎ Annulé.", embed=None, view=None)


# ================== COMMANDES ==================
@bot.tree.command(name="ping", description="Renvoie la latence du bot")
async def ping(interaction: discord.Interaction):
    log_cmd_start(interaction, "ping")
    latency_ms = round(bot.latency * 1000)
    embed = discord.Embed(title="Pong!", description=f"Latence: **{latency_ms} ms**", color=discord.Color.blurple())
    await safe_reply(interaction, embed=embed, ephemeral=True)
    log_cmd_ok(interaction, "ping")

@bot.tree.command(name="hello", description="Dis bonjour (optionnellement à quelqu'un)")
@app_commands.describe(nom="Nom ou pseudo à saluer (facultatif)")
async def hello(interaction: discord.Interaction, nom: str | None = None):
    log_cmd_start(interaction, "hello")
    cible = nom or interaction.user.display_name
    await safe_reply(interaction, f"Salut, **{cible}**! 👋", ephemeral=False)
    log_cmd_ok(interaction, "hello")

@bot.tree.command(name="invite", description="Affiche le lien d'invitation du bot")
async def invite(interaction: discord.Interaction):
    log_cmd_start(interaction, "invite")
    app_info = await bot.application_info()
    permissions = discord.Permissions(permissions=0)
    permissions.update(view_channel=True, send_messages=True, embed_links=True, read_message_history=True, use_application_commands=True)
    invite_url = discord.utils.oauth_url(app_info.id, permissions=permissions, scopes=("bot", "applications.commands"))
    await safe_reply(interaction, f"Voici mon lien d'invitation : {invite_url}")
    log_cmd_ok(interaction, "invite")

@bot.tree.command(name="vc_test", description="Test de connexion vocale (diagnostic)")
async def vc_test(interaction: discord.Interaction):
    log_cmd_start(interaction, "vc_test")
    vc = await ensure_connected_to_user_vc(interaction)
    if vc:
        await safe_reply(interaction, f"✅ Connecté au vocal **{vc.channel.name}**", ephemeral=True)
        log_cmd_ok(interaction, "vc_test")

@bot.tree.command(name="join", description="Fait venir le bot dans ton salon vocal")
async def join(interaction: discord.Interaction):
    log_cmd_start(interaction, "join")
    vc = await ensure_connected_to_user_vc(interaction)
    if vc:
        await safe_reply(interaction, f"Connecté à **{vc.channel.name}** ✅", ephemeral=True)
        log_cmd_ok(interaction, "join")

@bot.tree.command(name="leave", description="Fait quitter le salon vocal au bot")
async def leave(interaction: discord.Interaction):
    log_cmd_start(interaction, "leave")
    vc: discord.VoiceClient | None = interaction.guild.voice_client
    if vc and vc.is_connected():
        await vc.disconnect()
        await safe_reply(interaction, "J'ai quitté le salon vocal 👋", ephemeral=True)
        log_cmd_ok(interaction, "leave")
    else:
        await safe_reply(interaction, "Je ne suis pas dans un salon vocal.", ephemeral=True)

@bot.tree.command(name="pause", description="Met la musique en pause")
async def pause(interaction: discord.Interaction):
    log_cmd_start(interaction, "pause")
    vc: discord.VoiceClient | None = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await safe_reply(interaction, "⏸️ Pause.", ephemeral=True)
        log_cmd_ok(interaction, "pause")
    else:
        await safe_reply(interaction, "Rien n'est en cours de lecture.", ephemeral=True)

@bot.tree.command(name="resume", description="Relance la musique")
async def resume(interaction: discord.Interaction):
    log_cmd_start(interaction, "resume")
    vc: discord.VoiceClient | None = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await safe_reply(interaction, "▶️ Reprise.", ephemeral=True)
        log_cmd_ok(interaction, "resume")
    else:
        await safe_reply(interaction, "Rien n'est en pause.", ephemeral=True)

@bot.tree.command(name="stop", description="Arrête la musique")
async def stop(interaction: discord.Interaction):
    log_cmd_start(interaction, "stop")
    vc: discord.VoiceClient | None = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await safe_reply(interaction, "⏹️ Musique arrêtée.", ephemeral=True)
        log_cmd_ok(interaction, "stop")
    else:
        await safe_reply(interaction, "Rien n'est en cours de lecture.", ephemeral=True)

@bot.tree.command(name="play", description="Lire une musique depuis un lien ou une recherche (YouTube, etc.)")
@app_commands.describe(query="Lien (YouTube/… ) ou recherche (ex: 'artist - title')")
async def play(interaction: discord.Interaction, query: str):
    log_cmd_start(interaction, "play")
    vc = await ensure_connected_to_user_vc(interaction)
    if not vc:
        return

    if yt_dlp is None:
        await safe_reply(interaction, "yt-dlp n'est pas installé. Fais: `pip install yt-dlp`")
        return

    if vc.is_playing() or vc.is_paused():
        vc.stop()

    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = next((e for e in info["entries"] if e), None)
            if not info:
                await safe_reply(interaction, "Aucun résultat trouvé.")
                return

            title = info.get("title", "Inconnu")
            url = info.get("webpage_url", query)
            stream_url = info.get("url")

        if not stream_url:
            await safe_reply(interaction, "Impossible d'obtenir le flux audio.")
            return

        source = build_ffmpeg_source(stream_url)
        vc.play(source, after=lambda e: log.info("[PLAY] terminé: %s", e) if e else log.info("[PLAY] terminé."))
        embed = discord.Embed(title="Lecture en cours 🎵", description=f"**{title}**", color=discord.Color.green())
        embed.add_field(name="Source", value=url, inline=False)
        await safe_reply(interaction, embed=embed, ephemeral=False)
        log_cmd_ok(interaction, "play")

    except Exception as e:
        log_cmd_err(interaction, "play", e)
        await safe_reply(interaction, f"Erreur lecture: {e}")

# ---- EMBED BUILDER command ----
@bot.tree.command(name="embed", description="Constructeur d'embed 100% custom (preview + envoi)")
@app_commands.describe(channel="Salon de destination (défaut: ici)")
async def embed_cmd(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    log_cmd_start(interaction, "embed")
    target = channel or interaction.channel
    perms = target.permissions_for(interaction.guild.me if interaction.guild else interaction.user)
    if not perms.send_messages or not perms.embed_links:
        await interaction.response.send_message("❌ Je n’ai pas la permission d’envoyer des **embeds** dans ce salon.", ephemeral=True)
        return
    view = EmbedBuilderView(author_id=interaction.user.id, initial_channel=target)
    emb = view.draft.to_embed()
    await interaction.response.send_message(content="**Aperçu** — configure via les boutons ci-dessous :", embed=emb, view=view, ephemeral=True)
    log_cmd_ok(interaction, "embed")

@bot.tree.command(name="live", description="Force l'état du live ON/OFF (TikTok)")
@app_commands.describe(state="Choisis ON ou OFF")
@app_commands.choices(state=[
    app_commands.Choice(name="ON", value="on"),
    app_commands.Choice(name="OFF", value="off"),
])
async def live(interaction: discord.Interaction, state: app_commands.Choice[str]):
    log_cmd_start(interaction, "live")
    await safe_reply(interaction, f"Bascule LIVE → **{state.name}**", ephemeral=True)
    await set_live_channel_name(state.value == "on")
    log_cmd_ok(interaction, "live")

# ================== LANCEMENT ==================
def main():
    if not TOKEN:
        raise RuntimeError("La variable d'environnement DISCORD_TOKEN est manquante. Créez un fichier .env avec DISCORD_TOKEN=...")
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
