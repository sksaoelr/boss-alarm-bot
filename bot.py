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
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN 이 없습니다. Render Env에 DISCORD_TOKEN을 넣어주세요.")
if not CHANNEL_ID_RAW.isdigit():
    raise SystemExit("CHANNEL_ID 가 올바르지 않습니다. Render Env에 CHANNEL_ID=숫자를 넣어주세요.")

CHANNEL_ID = int(CHANNEL_ID_RAW)

STATE_FILE = "boss_state.json"

# 보스 리젠 규칙(시간)
BOSSES: Dict[str, int] = {
    "베지": 6,
    "멘지": 6,
    "부활": 6,
    "각성": 6,
    "악계": 12,
    "인과": 12,
}

FIVE_MIN = 5 * 60


def now_ts() -> int:
    return int(time.time())


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {
            "panel_message_id": None,
            "bosses": {name: {"next_spawn": None, "last_cut": None} for name in BOSSES.keys()},
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    panel_message_id = data.get("panel_message_id")
    bosses_data = data.get("bosses", {})

    normalized = {"panel_message_id": panel_message_id, "bosses": {}}
    for name in BOSSES.keys():
        b = bosses_data.get(name, {})
        normalized["bosses"][name] = {
            "next_spawn": b.get("next_spawn"),
            "last_cut": b.get("last_cut"),
        }
    return normalized


def save_state(state: Dict[str, Any]) -> None:
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


def render_panel_text(state: Dict[str, Any]) -> str:
    lines = []
    lines.append("**보스 젠 관리 패널 (버튼: 컷 / 멍)**")
    lines.append("- 컷: 지금 잡힘(현재시간 기준으로 다음 젠 등록)")
    lines.append("- 멍: 미젠(기존 다음 젠 시간 기준으로 +리젠시간 연장)")
    lines.append("- 채팅 설정: `/설정 보스명 시간` (예: `/설정 베지 21:30` 또는 `/설정 베지 2026-01-20 09:10`)")
    lines.append("")
    lines.append("**현재 다음 젠 시간**")

    bosses_data = state["bosses"]
    for name, hours in BOSSES.items():
        ns = bosses_data[name].get("next_spawn")
        if isinstance(ns, int) and ns > 0:
            lines.append(f"- {name} ({hours}h): <t:{ns}:F>  |  <t:{ns}:R>")
        else:
            lines.append(f"- {name} ({hours}h): 미등록")

    lines.append("")
    lines.append("※ 알림: 5분 전 1회 + 정시 1회")
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
        if interaction.channel_id != CHANNEL_ID:
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
                f"- 다음 젠: <t:{ns_after}:F> | <t:{ns_after}:R>",
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
        super().__init__(timeout=60 * 60 * 24)  # 24시간 정도면 충분 (원하면 None도 가능)
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
        if interaction.channel_id != CHANNEL_ID:
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

        # 3초 제한 방지: 먼저 ACK
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        boss = self.boss_name
        hours = BOSSES[boss]
        interval_sec = hours * 3600

        state = self.bot.state_data  # type: ignore[attr-defined]
        cur = state["bosses"][boss]

        if action == "컷":
            base = now_ts()
            cur["last_cut"] = base
            next_spawn = base + interval_sec
            cur["next_spawn"] = next_spawn
        else:
            # 멍: 알림에 찍힌 "원래 젠 시간" 기준으로 +리젠
            base = self.target_ts
            next_spawn = base + interval_sec
            cur["next_spawn"] = next_spawn

        save_state(state)

        # 스케줄/패널 갱신
        await self.bot.reschedule_boss(boss)     # type: ignore[attr-defined]
        await self.bot.update_panel_message()    # type: ignore[attr-defined]

        # ✅ 버튼 제거 + 메시지 내용 업데이트
        try:
            handled = "컷" if action == "컷" else "멍"
            msg = interaction.message
            await msg.edit(
                content=(
                    f"🔔 **{boss} 젠타임입니다!**\n"
                    f"- 젠: <t:{self.target_ts}:F> | <t:{self.target_ts}:R>\n\n"
                    f"✅ 처리: **{handled}** (by {interaction.user.mention})\n"
                    f"➡️ 다음 젠: <t:{next_spawn}:F> | <t:{next_spawn}:R>"
                ),
                view=None,  # <-- 버튼 사라짐
            )
        except Exception as e:
            print(f"[WARN] failed to edit spawn alert message: {e}")

        # 사용자에게는 ephemeral 확인 메시지
        await interaction.followup.send(
            f"✅ **{boss} {action} 처리 완료**\n"
            f"- 다음 젠: <t:{next_spawn}:F> | <t:{next_spawn}:R>",
            ephemeral=True,
        )

class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # 슬래시 커맨드 기반이라 message_content 불필요
        super().__init__(command_prefix="!", intents=intents)

        self.state_data: Dict[str, Any] = load_state()
        self.panel_view: Optional[BossPanelView] = None

        # 보스별 알림 task (각 보스당 1개)
        self.alarm_tasks: Dict[str, asyncio.Task] = {}

    async def setup_hook(self):
        # 이벤트 루프 준비된 후 View 생성
        self.panel_view = BossPanelView(self)
        self.add_view(self.panel_view)

        # 슬래시 커맨드 sync
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as: {self.user} (id: {self.user.id})")

        await self.ensure_panel_message()

        # 저장된 next_spawn 복구 스케줄
        for boss_name in BOSSES.keys():
            await self.reschedule_boss(boss_name)

        await self.update_panel_message()

    async def ensure_panel_message(self):
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await self.fetch_channel(CHANNEL_ID)

        if not hasattr(channel, "send"):
            raise SystemExit("CHANNEL_ID가 메시지를 보낼 수 있는 채널이 아닙니다. 텍스트 채널(#) ID를 넣어주세요.")

        msg_id = self.state_data.get("panel_message_id")
        if isinstance(msg_id, int):
            try:
                msg = await channel.fetch_message(msg_id)  # type: ignore[attr-defined]
                return
            except Exception:
                pass

        content = render_panel_text(self.state_data)
        msg = await channel.send(content=content, view=self.panel_view)  # type: ignore[attr-defined]
        self.state_data["panel_message_id"] = msg.id
        save_state(self.state_data)

    async def update_panel_message(self):
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await self.fetch_channel(CHANNEL_ID)

        if not hasattr(channel, "send"):
            return

        msg_id = self.state_data.get("panel_message_id")
        if not isinstance(msg_id, int):
            return

        try:
            msg = await channel.fetch_message(msg_id)  # type: ignore[attr-defined]
            await msg.edit(content=render_panel_text(self.state_data), view=self.panel_view)
        except Exception:
            # 패널이 삭제되었거나 권한 문제면 재생성 시도
            self.state_data["panel_message_id"] = None
            save_state(self.state_data)
            try:
                await self.ensure_panel_message()
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
            channel = self.get_channel(CHANNEL_ID)
            if channel is None:
                channel = await self.fetch_channel(CHANNEL_ID)
            if not hasattr(channel, "send"):
                return

            # 5분 전 알림 시각
            five_before = target_ts - FIVE_MIN

            # 1) 5분 전 알림
            wait1 = five_before - now_ts()
            if wait1 > 0:
                await asyncio.sleep(wait1)

            # 스케줄이 바뀌었을 수도 있으니 최신값 확인
            latest = self.state_data["bosses"][boss_name].get("next_spawn")
            if latest != target_ts:
                return

            # five_before가 이미 지난 경우에도, target이 아직 남아있으면 5분전 알림 생략 가능
            if now_ts() < target_ts:
                # five_before 기준으로 늦게 깨어났더라도 target 이전이면 5분 전 알림 송출
                # (원치 않으면 아래 if를 now_ts() <= five_before + 2 같은 식으로 더 타이트하게 조정 가능)
                if now_ts() >= five_before:
                    await channel.send(f"⏰ **{boss_name} 5분 전입니다.**\n- 예정: <t:{target_ts}:F> | <t:{target_ts}:R>")  # type: ignore[attr-defined]

            # 2) 정시 알림
            wait2 = target_ts - now_ts()
            if wait2 > 0:
                await asyncio.sleep(wait2)

            latest2 = self.state_data["bosses"][boss_name].get("next_spawn")
            if latest2 != target_ts:
                return

            await channel.send(
                content=f"🔔 **{boss_name} 젠타임입니다!**\n- 젠: <t:{target_ts}:F> | <t:{target_ts}:R>",
                view=SpawnAlertView(self, boss_name, target_ts),
            )  # type: ignore[attr-defined]
            
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[ERROR] alarm task for {boss_name}: {e}")


bot = BossBot()


# -----------------------------
# 슬래시 커맨드: /설정, /다음젠
# -----------------------------
@bot.tree.command(name="설정", description="보스의 다음 젠 시간을 설정합니다. 예) /설정 베지 21:30 또는 /설정 베지 2026-01-20 09:10")
@app_commands.describe(보스="베지/멘지/부활/각성/악계/인과", 시간="HH:MM 또는 YYYY-MM-DD HH:MM (초까지는 :SS)")
async def set_boss_time(interaction: discord.Interaction, 보스: str, 시간: str):
    if interaction.channel_id != CHANNEL_ID:
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


@bot.tree.command(name="다음젠", description="전체 보스의 다음 젠 시간을 보여줍니다.")
async def show_next(interaction: discord.Interaction):
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("이 명령어는 지정 채널에서만 사용해주세요.", ephemeral=True)
        return

    lines = ["**다음 젠 목록**"]
    for name, hours in BOSSES.items():
        ns = bot.state_data["bosses"][name].get("next_spawn")
        if isinstance(ns, int) and ns > 0:
            lines.append(f"- {name}({hours}h): <t:{ns}:F> | <t:{ns}:R>")
        else:
            lines.append(f"- {name}({hours}h): 미등록")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


def main():
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
