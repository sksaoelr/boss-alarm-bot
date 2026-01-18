import os
import json
import asyncio
import time
import datetime
from typing import Dict, Any, Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import pytz

# --------------------
# 기본 설정
# --------------------
load_dotenv()
KST = pytz.timezone("Asia/Seoul")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()            # 패널 + 조작
VOICE_CHAT_CHANNEL_ID_RAW = os.getenv("VOICE_CHAT_CHANNEL_ID", "").strip()  # 알림 + 조작

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN 없음")
if not CHANNEL_ID_RAW.isdigit():
    raise SystemExit("CHANNEL_ID 오류")
if not VOICE_CHAT_CHANNEL_ID_RAW.isdigit():
    raise SystemExit("VOICE_CHAT_CHANNEL_ID 오류")

CHANNEL_ID = int(CHANNEL_ID_RAW)
VOICE_CHAT_CHANNEL_ID = int(VOICE_CHAT_CHANNEL_ID_RAW)

# 👉 이 두 채널에서만 전부 허용
ALLOWED_CHANNEL_IDS = {CHANNEL_ID, VOICE_CHAT_CHANNEL_ID}

STATE_FILE = "boss_state.json"

BOSSES: Dict[str, int] = {
    "베지": 6,
    "멘지": 6,
    "부활": 6,
    "각성": 6,
    "악계": 12,
    "인과율": 12,
}

FIVE_MIN = 5 * 60


# --------------------
# 유틸
# --------------------
def now_ts() -> int:
    return int(time.time())


def fmt_kst(ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts, KST)
    return dt.strftime("%m-%d %H:%M")


def fmt_rel(ts: int) -> str:
    diff = ts - now_ts()
    mins = abs(diff) // 60

    if abs(diff) < 30:
        return "지금"
    if diff < 0:
        return f"{mins}분 전"
    if mins < 60:
        return f"{mins}분 후"
    return f"{mins // 60}시간 후"


def fmt_kst_rel(ts: int) -> str:
    return f"{fmt_kst(ts)} | {fmt_rel(ts)}"


# --------------------
# 상태 저장
# --------------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {
            "panel_message_id": None,
            "bosses": {k: {"next_spawn": None} for k in BOSSES},
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --------------------
# Bot
# --------------------
class BossBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.state = load_state()
        self.alarm_tasks: Dict[str, asyncio.Task] = {}

    async def setup_hook(self):
        guild_id_raw = os.getenv("GUILD_ID", "").strip()

        if guild_id_raw.isdigit():
            guild = discord.Object(id=int(guild_id_raw))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"[SYNC] guild sync ok: {guild.id}")
        else:
            await self.tree.sync()
            print("[SYNC] global sync ok")

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")
        await self.ensure_panel()
        for boss in BOSSES:
            await self.reschedule(boss)

    # ----------------
    # 패널
    # ----------------
    async def ensure_panel(self):
        channel = await self.fetch_channel(CHANNEL_ID)
        msg_id = self.state.get("panel_message_id")

        content = self.render_panel()

        if isinstance(msg_id, int):
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(content=content)
                return
            except Exception:
                pass

        msg = await channel.send(content)
        self.state["panel_message_id"] = msg.id
        save_state(self.state)

    def render_panel(self) -> str:
        lines = ["**현재 다음 젠 시간**"]
        for name, h in BOSSES.items():
            ns = self.state["bosses"][name]["next_spawn"]
            if isinstance(ns, int):
                lines.append(f"- {name}({h}h): {fmt_kst_rel(ns)}")
            else:
                lines.append(f"- {name}({h}h): 미등록")
        return "\n".join(lines)

    async def update_panel(self):
        self.state = load_state()
        await self.ensure_panel()

    # ----------------
    # 알림 스케줄
    # ----------------
    async def reschedule(self, boss: str):
        t = self.alarm_tasks.get(boss)
        if t:
            t.cancel()

        ts = self.state["bosses"][boss]["next_spawn"]
        if not isinstance(ts, int):
            return

        self.alarm_tasks[boss] = asyncio.create_task(self.alarm_task(boss, ts))

    async def alarm_task(self, boss: str, target: int):
        try:
            five = target - FIVE_MIN
            await asyncio.sleep(max(0, five - now_ts()))

            if self.state["bosses"][boss]["next_spawn"] != target:
                return

            ch = await self.fetch_channel(VOICE_CHAT_CHANNEL_ID)
            await ch.send(f"⏰ **{boss} 젠 5분전** ({fmt_kst_rel(target)})")

            await asyncio.sleep(max(0, target - now_ts()))
            if self.state["bosses"][boss]["next_spawn"] != target:
                return

            await ch.send(f"🔔 **{boss} 젠타임입니다!**")
        except asyncio.CancelledError:
            return


bot = BossBot()

# --------------------
# Slash Commands
# --------------------
@bot.tree.command(name="설정")
@app_commands.describe(보스="보스명", 시간="HH:MM 또는 YYYY-MM-DD HH:MM")
async def set_time(interaction: discord.Interaction, 보스: str, 시간: str):
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return

    if 보스 not in BOSSES:
        await interaction.response.send_message("보스명 오류", ephemeral=True)
        return

    try:
        if "-" in 시간:
            dt = datetime.datetime.strptime(시간, "%Y-%m-%d %H:%M")
        else:
            now = datetime.datetime.now(KST)
            h, m = map(int, 시간.split(":"))
            dt = now.replace(hour=h, minute=m, second=0)
        ts = int(KST.localize(dt).timestamp())
    except Exception:
        await interaction.response.send_message("시간 형식 오류", ephemeral=True)
        return

    bot.state["bosses"][보스]["next_spawn"] = ts
    save_state(bot.state)
    await bot.reschedule(보스)
    await bot.update_panel()

    await interaction.response.send_message(
        f"✅ **{보스} 설정 완료**\n- {fmt_kst_rel(ts)}",
        ephemeral=False,
    )


@bot.tree.command(name="젠타임")
async def show_times(interaction: discord.Interaction):
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return

    lines = ["**젠타임 목록**"]
    for name, h in BOSSES.items():
        ns = bot.state["bosses"][name]["next_spawn"]
        if isinstance(ns, int):
            lines.append(f"- {name}({h}h): {fmt_kst_rel(ns)}")
        else:
            lines.append(f"- {name}({h}h): 미등록")

    await interaction.response.send_message("\n".join(lines), ephemeral=False)


# --------------------
# 실행
# --------------------
bot.run(TOKEN)
