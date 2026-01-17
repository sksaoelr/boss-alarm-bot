import os
import json
import asyncio
import time
from typing import Dict, Any, Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import datetime
import pytz

import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

def fmt_rel(ts: int, now: Optional[int] = None) -> str:
    now = now if now is not None else now_ts()
    diff = ts - now  # +면 미래(후), -면 과거(전)
    ad = abs(diff)

    if ad < 30:
        return "지금"

    mins = ad // 60
    if mins < 60:
        return f"{mins}분 {'후' if diff > 0 else '전'}"

    hours = mins // 60
    if hours < 24:
        return f"{hours}시간 {'후' if diff > 0 else '전'}"

    days = hours // 24
    return f"{days}일 {'후' if diff > 0 else '전'}"


def fmt_kst_rel(ts: int) -> str:
    return f"{fmt_kst(ts)} | {fmt_rel(ts)}"

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
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_web, daemon=True).start()

KST = pytz.timezone("Asia/Seoul")

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
VOICE_CHAT_CHANNEL_ID = os.getenv("VOICE_CHAT_CHANNEL_ID", "").strip()
if not VOICE_CHAT_CHANNEL_ID.isdigit():
    raise SystemExit("VOICE_CHAT_CHANNEL_ID 가 올바르지 않습니다. Env에 VOICE_CHAT_CHANNEL_ID=숫자를 넣어주세요.")
VOICE_CHAT_CHANNEL_ID = int(VOICE_CHAT_CHANNEL_ID)
ALLOWED_CHANNEL_IDS = {CHANNEL_ID, VOICE_CHAT_CHANNEL_ID}
# 패널/버튼 허용 채널(관리채널 + 보이스채팅탭)
ALLOWED_CHANNEL_IDS = {CHANNEL_ID, VOICE_CHAT_CHANNEL_ID}

# 패널을 띄울 채널들 (키는 상태파일 저장용)
PANEL_CHANNELS = {
    "admin": CHANNEL_ID,
    # "voice": VOICE_CHAT_CHANNEL_ID,
}


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN 이 없습니다. Render Env에 DISCORD_TOKEN을 넣어주세요.")
if not CHANNEL_ID.isdigit():
    raise SystemExit("CHANNEL_ID 가 올바르지 않습니다. Render Env에 CHANNEL_ID=숫자를 넣어주세요.")

CHANNEL_ID = int(CHANNEL_ID)

STATE_FILE = "boss_state.json"

# 보스 리젠 규칙(시간)
BOSSES: Dict[str, int] = {
    "베지": 6,
    "멘지": 6,
    "부활": 6,
    "각성": 6,
    "악계": 12,
    "인과율": 12,
}

FIVE_MIN = 5 * 60


def now_ts() -> int:
    return int(time.time())


def load_state() -> Dict[str, Any]:
    # 파일이 아예 없을 때 (최초 실행)
    if not os.path.exists(STATE_FILE):
        return {
            "panel_message_ids": {"admin": None, "voice": None},
            "bosses": {
                name: {"next_spawn": None, "last_cut": None}
                for name in BOSSES.keys()
            },
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

    # ✅ panel_message_ids (구버전 panel_message_id 호환)
    panel_message_ids = data.get("panel_message_ids")
    legacy_panel_id = data.get("panel_message_id")  # ✅ 단수 키가 구버전

    if isinstance(panel_message_ids, int):
        panel_message_ids = {"admin": panel_message_ids, "voice": None}
    elif not isinstance(panel_message_ids, dict):
        panel_message_ids = {"admin": legacy_panel_id if isinstance(legacy_panel_id, int) else None, "voice": None}

    bosses_data = data.get("bosses", {})
    if not isinstance(bosses_data, dict):
        bosses_data = {}

    normalized = {
        "panel_message_ids": {
            "admin": panel_message_ids.get("admin"),
            "voice": panel_message_ids.get("voice"),
        },
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
    # panel_message_ids가 int로 들어오면 dict로 강제
    pm = state.get("panel_message_ids")
    if isinstance(pm, int):
        state["panel_message_ids"] = {"admin": pm, "voice": None}
    elif not isinstance(pm, dict):
        state["panel_message_ids"] = {"admin": None, "voice": None}

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def parse_time_to_ts(text: str) -> Optional[int]:
    """
    입력 지원:
    - HH:MM
    - HH:MM:SS
    - YYYY-MM-DD HH:MM
    - YYYY-MM-DD HH:MM:SS

    HH:MM 형태면 "오늘(KST)" 기준으로 잡고,
    만약 이미 지난 시간이면 "내일(KST)"로 넘김.
    """
    text = text.strip()

    # 1) YYYY-MM-DD HH:MM(:SS)
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

    # 2) HH:MM(:SS)
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


def render_panel_text_compact(state: Dict[str, Any]) -> str:
    lines = []
    lines.append("**현재 다음 젠 시간**")

    bosses_data = state["bosses"]
    for name, hours in BOSSES.items():
        ns = bosses_data[name].get("next_spawn")
        if isinstance(ns, int) and ns > 0:
            lines.append(f"- {name} ({hours}h): {fmt_kst(ns)}")
        else:
            lines.append(f"- {name} ({hours}h): 미등록")
    return "\n".join(lines)

def render_panel_text(state: Dict[str, Any]) -> str:
    # 패널 최초 생성용(설명 포함)
    lines = []
    lines.append("**보스 젠 관리 패널 (버튼: 컷 / 멍)**")
    lines.append("- 컷: 지금 잡힘(현재시간 기준으로 다음 젠 등록)")
    lines.append("- 멍: 미젠(기존 다음 젠 시간 기준으로 +리젠시간 연장)")
    lines.append("- 채팅 설정: `/설정 보스명 시간` (예: `/설정 베지 21:30` 또는 `/설정 베지 2026-01-20 09:10`)")
    lines.append("")
    lines.append(render_panel_text_compact(state))
    return "\n".join(lines)


class BossPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)  # persistent
        self.bot = bot

        # 디스코드 버튼 한 줄 최대 5개 제한 -> row 자동 배치
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
        # 지정 채널 제한
        if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"이 버튼은 지정 채널에서만 사용됩니다. (채널ID: {CHANNEL_ID})",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"이 버튼은 지정 채널에서만 사용됩니다. (채널ID: {CHANNEL_ID})",
                    ephemeral=True,
                )
            return

        # 3초 제한 때문에 먼저 ACK
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        state = self.bot.state_data  # type: ignore[attr-defined]
        bosses_data = state["bosses"]
        hours = BOSSES[self.boss_name]
        interval_sec = hours * 3600

        cur = bosses_data[self.boss_name]
        ns_before = cur.get("next_spawn")

        if self.action == "컷":
            n = now_ts()
            cur["last_cut"] = n
            cur["next_spawn"] = n + interval_sec
            save_state(state)

            await self.bot.reschedule_boss(self.boss_name)  # type: ignore[attr-defined]
            await self.bot.update_panel_message()           # type: ignore[attr-defined]

            ns_after = cur["next_spawn"]
            await interaction.followup.send(
                f"✅ **{self.boss_name} 컷 처리**\n"
                f"- 컷: <t:{cur['last_cut']}:F>\n"
                f"- 다음 젠: {fmt_kst(ns_after)}",
                ephemeral=True,
            )
            return

        # 멍: 기존 next_spawn 기준으로 연장
        if not isinstance(ns_before, int) or ns_before <= 0:
            await interaction.followup.send(
                f"⚠️ **{self.boss_name}** 는 아직 다음 젠이 미등록입니다.\n"
                f"먼저 **{self.boss_name} 컷** 또는 `/설정`으로 등록해주세요.",
                ephemeral=True,
            )
            return

        cur["next_spawn"] = ns_before + interval_sec
        save_state(state)

        await self.bot.reschedule_boss(self.boss_name)  # type: ignore[attr-defined]
        await self.bot.update_panel_message()           # type: ignore[attr-defined]

        ns_after = cur["next_spawn"]
        await interaction.followup.send(
            f"🟨 **{self.boss_name} 멍 처리** (기존 젠 기준 연장)\n"
            f"- 기존 젠: <t:{ns_before}:F>\n"
            f"- 변경 젠: <t:{ns_after}:F> | <t:{ns_after}:R>",
            ephemeral=True,
        )


class SpawnAlertView(discord.ui.View):
    """
    '젠타임입니다' 알림 메시지에 붙는 컷/멍 버튼.
    - 컷: 현재시간 기준으로 +리젠시간
    - 멍: 알림의 target_ts(원래 젠시간) 기준으로 +리젠시간
    클릭하면: 상태 저장 + 재스케줄 + 패널 갱신 + (해당 메시지) 버튼 제거(view=None)
    """
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
        # 채널 제한
        if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
            await interaction.response.send_message(
                f"이 버튼은 지정 채널에서만 사용됩니다. (채널ID: {CHANNEL_ID})",
                ephemeral=True,
            )
            return

        boss = self.boss_name
        hours = BOSSES[boss]
        interval_sec = hours * 3600

        state = self.bot.state_data  # type: ignore[attr-defined]
        cur = state["bosses"][boss]

        # ✅ 중복/동시 클릭 방지 (메시지 ID 기준 최초 1회만 처리)
        handled_alerts = state.setdefault("handled_alerts", {})
        msg_id = str(interaction.message.id)

        if handled_alerts.get(msg_id):
            # 이미 처리된 경우: 에페메랄로만 안내 (이건 필요)
            await interaction.response.send_message("⚠️ 이미 처리된 알림입니다.", ephemeral=True)
            return

        # 먼저 처리 표시를 남겨 동시 클릭도 막음
        handled_alerts[msg_id] = {
            "boss": boss,
            "action": action,
            "by": str(interaction.user.id),
            "at": now_ts(),
        }
        save_state(state)

        # 다음 젠 계산
        if action == "컷":
            base = now_ts()
            cur["last_cut"] = base
            next_spawn = base + interval_sec
            cur["next_spawn"] = next_spawn
        else:
            base = self.target_ts
            next_spawn = base + interval_sec
            cur["next_spawn"] = next_spawn

        save_state(state)

        # ✅ 이 Interaction 응답으로 "원 메시지"를 수정 + 버튼 제거 (에페메랄 없음)
        handled = "컷" if action == "컷" else "멍"
        await interaction.response.edit_message(
            content=(
                f"🔔 **{boss} 젠타임입니다!**\n"
                f"- 예정: {fmt_kst_rel(self.target_ts)}\n\n"
                f"✅ 처리: **{handled}** (by {interaction.user.mention})\n"
                f"➡️ 다음 젠: {fmt_kst_rel(next_spawn)}"
            ),
            view=None,
        )

        # 스케줄/패널 갱신 (응답 이후에 처리)
        await self.bot.reschedule_boss(boss)     # type: ignore[attr-defined]
        await self.bot.update_panel_message()    # type: ignore[attr-defined]


class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # 슬래시 커맨드 기반이라 message_content 불필요
        super().__init__(command_prefix="!", intents=intents)

        self.state_data: Dict[str, Any] = load_state()
        self.panel_view: Optional[BossPanelView] = None

        # 보스별 알림 task (각 보스당 1개)
        self.alarm_tasks: Dict[str, asyncio.Task] = {}

        GUILD_ID = 1461167609222529026  # 서버 ID
        
        async def setup_hook(self):
            self.panel_view = BossPanelView(self)
            self.add_view(self.panel_view)
        
            guild = discord.Object(id=GUILD_ID)
        
            # 기존 글로벌 명령 제거 (안전)
            self.tree.clear_commands(guild=None)
        
            # 길드에 즉시 동기화
            await self.tree.sync(guild=guild)

    async def on_ready(self):
        print(f"Logged in as: {self.user} (id: {self.user.id})")

        await self.ensure_panel_message()

        # 저장된 next_spawn 복구 스케줄
        for boss_name in BOSSES.keys():
            await self.reschedule_boss(boss_name)

        await self.update_panel_message()

    async def ensure_panel_message(self):
        # 두 채널에 패널이 모두 존재하도록 보장
        for key, cid in PANEL_CHANNELS.items():
            await self._ensure_panel_in_channel(key, cid)

    async def _ensure_panel_in_channel(self, key: str, channel_id: int):
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)

        if not hasattr(channel, "send"):
            raise SystemExit("CHANNEL_ID가 메시지를 보낼 수 있는 채널이 아닙니다. 텍스트 채널(#) ID를 넣어주세요.")

        msg_id = self.state_data.get("panel_message_ids")
        if isinstance(msg_id, int):
            try:
                msg = await channel.fetch_message(msg_id)  # type: ignore[attr-defined]
                return
            except Exception:
                pass

        content = render_panel_text(self.state_data)
        msg = await channel.send(content=content, view=self.panel_view)  # type: ignore[attr-defined]
        self.state_data["panel_message_ids"] = msg.id
        save_state(self.state_data)

    async def update_panel_message(self):
        msg_ids = self.state_data.get("panel_message_ids")
        if not isinstance(msg_ids, dict):
            return

        content = render_panel_text_compact(self.state_data)

        for key, cid in PANEL_CHANNELS.items():
            channel = self.get_channel(cid)
            if channel is None:
                try:
                    channel = await self.fetch_channel(cid)
                except Exception:
                    continue

            if not hasattr(channel, "send"):
                continue

            msg_id = msg_ids.get(key)
            if not isinstance(msg_id, int):
                # 없으면 생성 시도
                try:
                    await self._ensure_panel_in_channel(key, cid)
                except Exception:
                    pass
                continue

            try:
                msg = await channel.fetch_message(msg_id)  # type: ignore[attr-defined]
                await msg.edit(content=content, view=self.panel_view)
            except Exception:
                # 삭제/권한 문제 → ID 초기화 후 재생성
                self.state_data["panel_message_ids"][key] = None
                save_state(self.state_data)
                try:
                    await self._ensure_panel_in_channel(key, cid)
                except Exception:
                    pass

    async def reschedule_boss(self, boss_name: str):
        # 기존 task 취소
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
            channel = self.get_channel(VOICE_CHAT_CHANNEL_ID)
            if channel is None:
                channel = await self.fetch_channel(VOICE_CHAT_CHANNEL_ID)
            if not hasattr(channel, "send"):
                return
    
            five_before = target_ts - FIVE_MIN
    
            # 1) 5분 전 알림 (정확히 5분 전일 때만)
            wait1 = five_before - now_ts()
            if wait1 > 0:
                await asyncio.sleep(wait1)
            else:
                # 이미 5분 전이 지났으면 5분전 알림은 절대 안 함
                pass
    
            # 스케줄 변경 확인 (최신 next_spawn이 target_ts인지)
            latest = self.state_data["bosses"][boss_name].get("next_spawn")
            if latest != target_ts:
                return
    
            # 정확히 five_before 근처(±2초)일 때만 발송
            if wait1 > 0 and abs(now_ts() - five_before) <= 2:
                await channel.send(
                    f"⏰ **{boss_name} 젠 5분전입니다.**\n- 예정: {fmt_kst(target_ts)}"
                )
    
            # 2) 정시 알림
            wait2 = target_ts - now_ts()
            if wait2 > 0:
                await asyncio.sleep(wait2)
            else:
                # 이미 젠 시간이 지났으면 정시 알림도 생략
                return
    
            latest2 = self.state_data["bosses"][boss_name].get("next_spawn")
            if latest2 != target_ts:
                return
    
            await channel.send(
                content=f"🔔 **{boss_name} 젠타임입니다!**\n- {fmt_kst(target_ts)}",
                view=SpawnAlertView(self, boss_name, target_ts),
            )  # type: ignore[attr-defined]
    
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[ERROR] alarm task for {boss_name}: {e}")


bot = BossBot()


# -----------------------------
# 슬래시 커맨드: /설정, /젠타임
# -----------------------------
@bot.tree.command(name="설정", description="보스의 다음 젠 시간을 설정합니다. 예) /설정 베지 21:30 또는 /설정 베지 2026-01-20 09:10")
@app_commands.describe(보스="베지/멘지/부활/각성/악계/인과율", 시간="HH:MM 또는 YYYY-MM-DD HH:MM (초까지는 :SS)")
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
        await interaction.response.send_message("시간 형식이 올바르지 않습니다. 예: 21:30 / 21:30:10 / 2026-01-20 09:10", ephemeral=True)
        return

    bot.state_data["bosses"][보스]["next_spawn"] = ts
    save_state(bot.state_data)

    await bot.reschedule_boss(보스)
    await bot.update_panel_message()

    await interaction.response.send_message(
        f"✅ **{보스} 다음 젠 시간 설정 완료**\n- 다음 젠: <t:{ts}:F> | <t:{ts}:R>",
        ephemeral=True,
    )


@bot.tree.command(name="젠타임", description="전체 보스의 다음 젠 시간을 보여줍니다.")
async def show_next(interaction: discord.Interaction):
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message("이 명령어는 지정 채널에서만 사용해주세요.", ephemeral=True)
        return

    lines = ["**다음 젠 목록**"]
    for name, hours in BOSSES.items():
        ns = bot.state_data["bosses"][name].get("next_spawn")
        if isinstance(ns, int) and ns > 0:
            lines.append(f"- {name}({hours}h): {fmt_kst(ns)}")
        else:
            lines.append(f"- {name}({hours}h): 미등록")
    
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
