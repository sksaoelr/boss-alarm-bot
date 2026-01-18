import os
import json
import asyncio
import time
from typing import Dict, Any, Optional, Set
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import datetime
import pytz

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler


# -----------------------------
# 기본 설정 / 유틸
# -----------------------------
KST = pytz.timezone("Asia/Seoul")


def now_ts() -> int:
    return int(time.time())


def fmt_kst(ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts, KST)
    return dt.strftime("%m-%d %H:%M")


def fmt_rel(ts: int, now: Optional[int] = None) -> str:
    now = now if now is not None else now_ts()
    diff = ts - now
    ad = abs(diff)

    if ad < 30:
        return "지금"

    mins = ad // 60

    # 지난 경우(미처리 젠)
    if diff < 0:
        if mins < 60:
            return f"• {mins}분 전"
        hours = mins // 60
        if hours < 24:
            return f"• {hours}시간 전"
        days = hours // 24
        return f"• {days}일 전"

    # 미래
    if mins < 60:
        return f"{mins}분 후"

    hours = mins // 60
    if hours < 24:
        return f"{hours}시간 후"

    days = hours // 24
    return f"{days}일 후"


def fmt_kst_rel(ts: int) -> str:
    return f"{fmt_kst(ts)} | {fmt_rel(ts)}"


# -----------------------------
# 간단 웹 헬스체크 (Render/OCI용)
# -----------------------------
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def run_web():
    port = int(os.environ.get("PORT", 3000))
    from http.server import HTTPServer
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


threading.Thread(target=run_web, daemon=True).start()


# -----------------------------
# ENV / 채널 설정
# -----------------------------
load_dotenv()


def parse_id_set(value: str) -> Set[int]:
    if not value:
        return set()
    out: Set[int] = set()
    for x in value.split(","):
        x = x.strip()
        if x.isdigit():
            out.add(int(x))
    return out


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()
VOICE_CHAT_CHANNEL_ID_RAW = os.getenv("VOICE_CHAT_CHANNEL_ID", "").strip()

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN 이 없습니다.")
if not CHANNEL_ID_RAW.isdigit():
    raise SystemExit("CHANNEL_ID 가 올바르지 않습니다. 숫자 ID를 넣어주세요.")
if not VOICE_CHAT_CHANNEL_ID_RAW.isdigit():
    raise SystemExit("VOICE_CHAT_CHANNEL_ID 가 올바르지 않습니다. 숫자 ID를 넣어주세요.")

CHANNEL_ID = int(CHANNEL_ID_RAW)
VOICE_CHAT_CHANNEL_ID = int(VOICE_CHAT_CHANNEL_ID_RAW)

# 여러 채널 허용: 없으면 기본 2개
ALLOWED_CHANNEL_IDS_ENV = parse_id_set(os.getenv("ALLOWED_CHANNEL_IDS", "").strip())
ALLOWED_CHANNEL_IDS = ALLOWED_CHANNEL_IDS_ENV or {CHANNEL_ID, VOICE_CHAT_CHANNEL_ID}

# 알림 채널(복수): 없으면 기본 1개
ALERT_CHANNEL_IDS = parse_id_set(os.getenv("ALERT_CHANNEL_IDS", "").strip()) or {VOICE_CHAT_CHANNEL_ID}

# 패널을 띄울 채널들(원하면 여기도 복수로 확장 가능)
PANEL_CHANNELS = {
    "admin": CHANNEL_ID,
}

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


# -----------------------------
# 상태 저장/로드
# -----------------------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {
            "panel_message_ids": {k: None for k in PANEL_CHANNELS.keys()},
            "bosses": {name: {"next_spawn": None, "last_cut": None} for name in BOSSES.keys()},
            "handled_alerts": {},
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    handled_alerts = data.get("handled_alerts", {})
    if not isinstance(handled_alerts, dict):
        handled_alerts = {}

    panel_message_ids = data.get("panel_message_ids", {})
    if isinstance(panel_message_ids, int):
        # 구버전 단일 ID 호환
        panel_message_ids = {"admin": panel_message_ids}
    if not isinstance(panel_message_ids, dict):
        panel_message_ids = {}

    bosses_data = data.get("bosses", {})
    if not isinstance(bosses_data, dict):
        bosses_data = {}

    normalized = {
        "panel_message_ids": {k: panel_message_ids.get(k) for k in PANEL_CHANNELS.keys()},
        "bosses": {},
        "handled_alerts": handled_alerts,
    }

    for name in BOSSES.keys():
        b = bosses_data.get(name, {})
        if not isinstance(b, dict):
            b = {}
        normalized["bosses"][name] = {
            "next_spawn": b.get("next_spawn"),
            "last_cut": b.get("last_cut"),
        }

    return normalized


def save_state(state: Dict[str, Any]) -> None:
    pm = state.get("panel_message_ids")
    if not isinstance(pm, dict):
        state["panel_message_ids"] = {k: None for k in PANEL_CHANNELS.keys()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_time_to_ts(text: str) -> Optional[int]:
    text = text.strip()

    # YYYY-MM-DD HH:MM(:SS)
    try:
        if " " in text and "-" in text:
            date_part, time_part = text.split(" ", 1)
            y, m, d = map(int, date_part.split("-"))
            tparts = list(map(int, time_part.split(":")))
            if len(tparts) == 2:
                hh, mm = tparts
                ss = 0
            elif len(tparts) == 3:
                hh, mm, ss = tparts
            else:
                return None
            dt = KST.localize(datetime.datetime(y, m, d, hh, mm, ss))
            return int(dt.timestamp())
    except Exception:
        pass

    # HH:MM(:SS) - 오늘 기준, 이미 지났으면 내일
    try:
        if ":" in text and "-" not in text:
            tparts = list(map(int, text.split(":")))
            if len(tparts) == 2:
                hh, mm = tparts
                ss = 0
            elif len(tparts) == 3:
                hh, mm, ss = tparts
            else:
                return None

            now = datetime.datetime.now(KST)
            dt = KST.localize(datetime.datetime(now.year, now.month, now.day, hh, mm, ss))
            ts = int(dt.timestamp())
            if ts <= int(now.timestamp()):
                dt = dt + datetime.timedelta(days=1)
                ts = int(dt.timestamp())
            return ts
    except Exception:
        pass

    return None


# -----------------------------
# 패널 렌더링
# -----------------------------
def render_panel_text_compact(state: Dict[str, Any]) -> str:
    lines = ["**현재 다음 젠 시간**"]
    bosses_data = state["bosses"]

    for name, hours in BOSSES.items():
        ns = bosses_data[name].get("next_spawn")
        if isinstance(ns, int) and ns > 0:
            lines.append(f"- {name} ({hours}h): {fmt_kst_rel(ns)}")
        else:
            lines.append(f"- {name} ({hours}h): 미등록")
    return "\n".join(lines)


def render_panel_text(state: Dict[str, Any]) -> str:
    lines = []
    lines.append("**보스 젠 관리 패널 (버튼: 컷 / 멍)**")
    lines.append("- 컷: 지금 잡힘(현재시간 기준으로 다음 젠 등록)")
    lines.append("- 멍: 미젠(기존 다음 젠 시간 기준으로 +리젠시간 연장)")
    lines.append("- 채팅 설정: `/설정 보스명 시간` (예: `/설정 베지 21:30` 또는 `/설정 베지 2026-01-20 09:10`)")
    lines.append("")
    lines.append(render_panel_text_compact(state))
    return "\n".join(lines)


# -----------------------------
# UI: 패널 버튼
# -----------------------------
class BossPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

        row = 0
        col = 0

        def next_row():
            nonlocal row, col
            row += 1
            col = 0

        for boss_name in BOSSES.keys():
            self.add_item(BossButton(bot, boss_name, action="컷", row=row))
            col += 1
            if col >= 5:
                next_row()

            self.add_item(BossButton(bot, boss_name, action="멍", row=row))
            col += 1
            if col >= 5:
                next_row()


class BossButton(discord.ui.Button):
    def __init__(self, bot: commands.Bot, boss_name: str, action: str, row: int):
        self.bot = bot
        self.boss_name = boss_name
        self.action = action

        label = f"{boss_name} {action}"
        style = discord.ButtonStyle.success if action == "컷" else discord.ButtonStyle.secondary
        custom_id = f"boss:{boss_name}:{action}"
        super().__init__(label=label, style=style, custom_id=custom_id, row=row)

    async def callback(self, interaction: discord.Interaction):
        if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
            await interaction.response.send_message("이 버튼은 지정 채널에서만 사용됩니다.", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        state = self.bot.state_data  # type: ignore[attr-defined]
        cur = state["bosses"][self.boss_name]
        interval_sec = BOSSES[self.boss_name] * 3600

        ns_before = cur.get("next_spawn")

        if self.action == "컷":
            base = now_ts()
            cur["last_cut"] = base
            cur["next_spawn"] = base + interval_sec
            save_state(state)

            await self.bot.reschedule_boss(self.boss_name)  # type: ignore[attr-defined]
            await self.bot.update_panel_message()           # type: ignore[attr-defined]

            ns_after = cur["next_spawn"]
            await interaction.followup.send(
                f"✅ **{self.boss_name}** 다음 젠: {fmt_kst_rel(ns_after)}",
                ephemeral=False,
            )
            return

        # 멍
        if not isinstance(ns_before, int) or ns_before <= 0:
            await interaction.followup.send(
                f"⚠️ **{self.boss_name}** 는 아직 다음 젠이 미등록입니다.\n먼저 **{self.boss_name} 컷** 또는 `/설정`으로 등록해주세요.",
                ephemeral=True,
            )
            return

        cur["next_spawn"] = ns_before + interval_sec
        save_state(state)

        await self.bot.reschedule_boss(self.boss_name)  # type: ignore[attr-defined]
        await self.bot.update_panel_message()           # type: ignore[attr-defined]

        ns_after = cur["next_spawn"]
        await interaction.followup.send(
            f"🟨 **{self.boss_name}** 변경 젠: {fmt_kst_rel(ns_after)}",
            ephemeral=False,
        )


# -----------------------------
# UI: 알림 메시지 컷/멍 버튼
# -----------------------------
class SpawnAlertView(discord.ui.View):
    def __init__(self, bot: commands.Bot, boss_name: str, target_ts: int):
        super().__init__(timeout=60 * 60 * 24)
        self.bot = bot
        self.boss_name = boss_name
        self.target_ts = target_ts

    @discord.ui.button(label="컷", style=discord.ButtonStyle.success)
    async def cut_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, action="컷")

    @discord.ui.button(label="멍", style=discord.ButtonStyle.secondary)
    async def miss_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, action="멍")

    async def _handle(self, interaction: discord.Interaction, action: str):
        if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
            await interaction.response.send_message("이 버튼은 지정 채널에서만 사용됩니다.", ephemeral=True)
            return

        boss = self.boss_name
        interval_sec = BOSSES[boss] * 3600

        state = self.bot.state_data  # type: ignore[attr-defined]
        cur = state["bosses"][boss]

        handled_alerts = state.setdefault("handled_alerts", {})
        msg_id = str(interaction.message.id)

        if handled_alerts.get(msg_id):
            await interaction.response.send_message("⚠️ 이미 처리된 알림입니다.", ephemeral=True)
            return

        handled_alerts[msg_id] = {
            "boss": boss,
            "action": action,
            "by": str(interaction.user.id),
            "at": now_ts(),
        }
        save_state(state)

        if action == "컷":
            base = now_ts()
            cur["last_cut"] = base
            next_spawn = base + interval_sec
        else:
            base = self.target_ts
            next_spawn = base + interval_sec

        cur["next_spawn"] = next_spawn
        save_state(state)

        handled = "컷" if action == "컷" else "멍"
        await interaction.response.edit_message(
            content=(
                f"🔔 **{boss} 젠타임입니다!**\n"
                f"- 예정: {fmt_kst_rel(self.target_ts)}\n\n"
                f"✅ **{handled}** (by {interaction.user.mention})\n"
                f"➡️ 다음 젠: {fmt_kst_rel(next_spawn)}"
            ),
            view=None,
        )

        await self.bot.reschedule_boss(boss)     # type: ignore[attr-defined]
        await self.bot.update_panel_message()    # type: ignore[attr-defined]


# -----------------------------
# Bot
# -----------------------------
class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.state_data: Dict[str, Any] = load_state()
        self.panel_view: Optional[BossPanelView] = None
        self.alarm_tasks: Dict[str, asyncio.Task] = {}

    async def setup_hook(self):
        # persistent view 등록
        self.panel_view = BossPanelView(self)
        self.add_view(self.panel_view)

        # 여러 서버 배포용: 글로벌 sync
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as: {self.user} (id: {self.user.id})")
        print(f"Guilds count: {len(self.guilds)}")
        for g in self.guilds:
            print(f"- guild: {g.name} ({g.id})")

        await self.ensure_panel_message()

        for boss_name in BOSSES.keys():
            await self.reschedule_boss(boss_name)

        await self.update_panel_message()

    async def on_guild_join(self, guild: discord.Guild):
        print(f"[JOINED] {guild.name} ({guild.id})")

    async def _get_text_channel(self, cid: int):
        ch = self.get_channel(cid)
        if ch is None:
            try:
                ch = await self.fetch_channel(cid)
            except Exception:
                return None
        if hasattr(ch, "send"):
            return ch
        return None

    async def ensure_panel_message(self):
        for key, cid in PANEL_CHANNELS.items():
            await self._ensure_panel_in_channel(key, cid)

    async def _ensure_panel_in_channel(self, key: str, channel_id: int):
        channel = await self._get_text_channel(channel_id)
        if channel is None:
            raise SystemExit("패널 채널 ID가 올바르지 않거나 메시지 권한이 없습니다.")

        pm_ids = self.state_data.get("panel_message_ids")
        if not isinstance(pm_ids, dict):
            pm_ids = {k: None for k in PANEL_CHANNELS.keys()}
            self.state_data["panel_message_ids"] = pm_ids

        msg_id = pm_ids.get(key)

        if isinstance(msg_id, int):
            try:
                await channel.fetch_message(msg_id)  # type: ignore[attr-defined]
                return
            except Exception:
                pm_ids[key] = None
                save_state(self.state_data)

        content = render_panel_text(self.state_data)
        msg = await channel.send(content=content, view=self.panel_view)  # type: ignore[attr-defined]
        pm_ids[key] = msg.id
        save_state(self.state_data)

    async def update_panel_message(self):
        pm_ids = self.state_data.get("panel_message_ids")
        if not isinstance(pm_ids, dict):
            return

        content = render_panel_text_compact(self.state_data)

        for key, cid in PANEL_CHANNELS.items():
            channel = await self._get_text_channel(cid)
            if channel is None:
                continue

            msg_id = pm_ids.get(key)
            if not isinstance(msg_id, int):
                try:
                    await self._ensure_panel_in_channel(key, cid)
                except Exception:
                    pass
                continue

            try:
                msg = await channel.fetch_message(msg_id)  # type: ignore[attr-defined]
                await msg.edit(content=content, view=self.panel_view)
            except Exception:
                pm_ids[key] = None
                save_state(self.state_data)
                try:
                    await self._ensure_panel_in_channel(key, cid)
                except Exception:
                    pass

    async def reschedule_boss(self, boss_name: str):
        t = self.alarm_tasks.get(boss_name)
        if t and not t.done():
            t.cancel()

        ns = self.state_data["bosses"][boss_name].get("next_spawn")
        if not isinstance(ns, int) or ns <= 0:
            self.alarm_tasks.pop(boss_name, None)
            return

        self.alarm_tasks[boss_name] = asyncio.create_task(self._alarm_task(boss_name, ns))

    async def _alarm_task(self, boss_name: str, target_ts: int):
        try:
            five_before = target_ts - FIVE_MIN

            # 1) 5분 전 알림 (정확히 5분 전 근처에만)
            wait1 = five_before - now_ts()
            if wait1 > 0:
                await asyncio.sleep(wait1)

            latest = self.state_data["bosses"][boss_name].get("next_spawn")
            if latest != target_ts:
                return

            if wait1 > 0 and abs(now_ts() - five_before) <= 2:
                for cid in ALERT_CHANNEL_IDS:
                    ch = await self._get_text_channel(cid)
                    if ch:
                        await ch.send(
                            f"⏰ **{boss_name} 젠 5분전입니다.**\n- 예정: {fmt_kst_rel(target_ts)}"
                        )

            # 2) 정시 알림
            wait2 = target_ts - now_ts()
            if wait2 > 0:
                await asyncio.sleep(wait2)
            else:
                return

            latest2 = self.state_data["bosses"][boss_name].get("next_spawn")
            if latest2 != target_ts:
                return

            for cid in ALERT_CHANNEL_IDS:
                ch = await self._get_text_channel(cid)
                if ch:
                    await ch.send(
                        content=f"🔔 **{boss_name} 젠타임입니다!**",
                        view=SpawnAlertView(self, boss_name, target_ts),
                    )  # type: ignore[attr-defined]

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[ERROR] alarm task for {boss_name}: {e}")


bot = BossBot()


# -----------------------------
# Slash Commands
# -----------------------------
@bot.tree.command(name="설정", description="보스의 다음 젠 시간을 설정합니다.")
@app_commands.describe(보스="베지/멘지/부활/각성/악계/인과율", 시간="HH:MM 또는 YYYY-MM-DD HH:MM (초는 :SS)")
async def set_boss_time(interaction: discord.Interaction, 보스: str, 시간: str):
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("이 명령어는 지정 채널에서만 사용해주세요.", ephemeral=True)
        return

    보스 = 보스.strip()
    if 보스 not in BOSSES:
        await interaction.response.send_message(f"보스명이 올바르지 않습니다. 사용 가능: {', '.join(BOSSES.keys())}", ephemeral=True)
        return

    ts = parse_time_to_ts(시간)
    if ts is None:
        await interaction.response.send_message("시간 형식이 올바르지 않습니다. 예: 21:30 / 2026-01-20 09:10", ephemeral=True)
        return

    bot.state_data["bosses"][보스]["next_spawn"] = ts
    save_state(bot.state_data)

    await bot.reschedule_boss(보스)
    await bot.update_panel_message()

    await interaction.response.send_message(
        f"✅ **{보스} 다음 젠 시간 설정 완료**\n- 다음 젠: {fmt_kst_rel(ts)}",
        ephemeral=False,
    )


@bot.tree.command(name="젠타임", description="전체 보스의 다음 젠 시간을 보여줍니다.")
async def show_next(interaction: discord.Interaction):
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("이 명령어는 지정 채널에서만 사용해주세요.", ephemeral=True)
        return

    lines = ["**목록**"]
    for name, hours in BOSSES.items():
        ns = bot.state_data["bosses"][name].get("next_spawn")
        if isinstance(ns, int) and ns > 0:
            lines.append(f"- {name} ({hours}h): {fmt_kst_rel(ns)}")
        else:
            lines.append(f"- {name} ({hours}h): 미등록")

    await interaction.response.send_message("\n".join(lines), ephemeral=False)


def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
