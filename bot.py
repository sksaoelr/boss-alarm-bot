"""
Discord 보스젠 알림봇 (버튼: 컷 / 멍)
- 보스 6개: 베지, 멘지, 부활, 각성 (6시간) / 악계, 인과 (12시간)
- 특정 채널에 "보스 젠 관리 패널" 1개를 올리고, 버튼으로 시간 갱신
- 컷: 누른 시각(초까지) 기준으로 next_spawn = now + interval
- 멍: 누른 시각이 아니라 "기존 next_spawn" 기준으로 next_spawn = next_spawn + interval
- 봇 재시작해도 state.json 저장값으로 복구 + 버튼 지속(persistent view)

실행 준비:
1) pip install -U discord.py python-dotenv
2) 같은 폴더에 .env 파일 생성 후 아래 입력:
   DISCORD_TOKEN=너의봇토큰
   CHANNEL_ID=알림채널ID(숫자)

3) 봇에 권한: Send Messages, Read Message History, Use Application Commands(선택), Use External Emojis(선택)
4) python bot.py 로 실행

주의:
- 버튼 지속(persistent view)은 봇 재시작 시에도 살아있지만, 코드를 수정/재배포 후에도 항상 on_ready에서 add_view가 호출되어야 합니다.
"""

import os
import json
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional, Any

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN 이 없습니다. .env에 DISCORD_TOKEN=... 넣어주세요.")
if not CHANNEL_ID_RAW.isdigit():
    raise SystemExit("CHANNEL_ID 가 올바르지 않습니다. .env에 CHANNEL_ID=숫자 넣어주세요.")

CHANNEL_ID = int(CHANNEL_ID_RAW)

STATE_FILE = "boss_state.json"

BOSSES: Dict[str, int] = {
    "베지": 6,
    "멘지": 6,
    "부활": 6,
    "각성": 6,
    "악계": 12,
    "인과": 12,
}


@dataclass
class BossState:
    next_spawn: Optional[int] = None  # unix seconds
    last_cut: Optional[int] = None    # unix seconds


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
        # 파일이 깨졌을 때 최소 복구
        data = {}

    panel_message_id = data.get("panel_message_id")
    bosses_data = data.get("bosses", {})

    normalized = {
        "panel_message_id": panel_message_id,
        "bosses": {},
    }
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


def render_panel_text(state: Dict[str, Any]) -> str:
    lines = []
    lines.append("**보스 젠 관리 패널 (버튼: 컷 / 멍)**")
    lines.append("- 컷: 지금 시간 기준으로 다음 젠 자동 등록")
    lines.append("- 멍: (안뜸) 기존 다음 젠 시간 기준으로 +리젠시간 만큼 밀기")
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
    lines.append("※ 버튼 눌렀는데 반응이 없다면, 봇이 해당 채널에서 메시지/상호작용 권한이 있는지 확인해주세요.")
    return "\n".join(lines)


class BossPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)  # persistent
        self.bot = bot

        # 버튼 12개: 보스6 * (컷/멍)
        # 한 줄에 5개 제한 -> row 0~2로 배치
        row = 0
        col = 0

        def next_row():
            nonlocal row, col
            row += 1
            col = 0

        for boss_name in BOSSES.keys():
            # 컷 버튼
            self.add_item(BossButton(bot, boss_name, action="컷", row=row))
            col += 1
            if col >= 5:
                next_row()

            # 멍 버튼
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

        # custom_id는 persistent view에서 중요(고유해야 함)
        custom_id = f"boss:{boss_name}:{action}"

        super().__init__(label=label, style=style, custom_id=custom_id, row=row)

async def callback(self, interaction: discord.Interaction):
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

    # 3초 제한 때문에 먼저 ACK(응답 예약)
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

    # 멍 처리
    if not isinstance(ns_before, int) or ns_before <= 0:
        await interaction.followup.send(
            f"⚠️ **{self.boss_name}** 는 아직 다음 젠이 미등록입니다.\n"
            f"먼저 **{self.boss_name} 컷**을 눌러 등록해주세요.",
            ephemeral=True,
        )
        return

    cur["next_spawn"] = ns_before + interval_sec
    save_state(state)

    await self.bot.reschedule_boss(self.boss_name)  # type: ignore[attr-defined]
    await self.bot.update_panel_message()           # type: ignore[attr-defined]

    ns_after = cur["next_spawn"]
    await interaction.followup.send(
        f"🟨 **{self.boss_name} 멍 처리** (기존 젠 기준으로 연장)\n"
        f"- 기존 젠: <t:{ns_before}:F>\n"
        f"- 변경 젠: <t:{ns_after}:F> | <t:{ns_after}:R>",
        ephemeral=True,
    )

class BossBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # 버튼 기반이라 message_content 필요 없음
        super().__init__(command_prefix="!", intents=intents)

        self.state_data: Dict[str, Any] = load_state()
        self.panel_view = None

        # 보스별 예약 task 핸들
        self.spawn_tasks: Dict[str, asyncio.Task] = {}

    async def setup_hook(self):
        # 이벤트 루프가 준비된 뒤 View 생성 (no running event loop 방지)
        self.panel_view = BossPanelView(self)
        self.add_view(self.panel_view)

    async def on_ready(self):
        print(f"Logged in as: {self.user} (id: {self.user.id})")

        # 패널 메시지 보장
        await self.ensure_panel_message()

        # 저장된 next_spawn로 스케줄 복구
        for boss_name in BOSSES.keys():
            await self.reschedule_boss(boss_name)

        # 패널 텍스트 최신화
        await self.update_panel_message()

    async def ensure_panel_message(self):
        channel = self.get_channel(CHANNEL_ID)
        if channel is None or not isinstance(channel, discord.TextChannel):
            # 캐시에 없으면 fetch
            channel = await self.fetch_channel(CHANNEL_ID)  # type: ignore[assignment]
        assert isinstance(channel, discord.TextChannel)

        msg_id = self.state_data.get("panel_message_id")

        if isinstance(msg_id, int):
            try:
                msg = await channel.fetch_message(msg_id)
                # 메시지가 존재하면 OK
                return
            except discord.NotFound:
                pass
            except discord.Forbidden:
                raise SystemExit("봇이 채널 메시지 읽기 권한(Read Message History)이 없습니다.")
            except Exception:
                pass

        # 없으면 새로 생성
        content = render_panel_text(self.state_data)
        msg = await channel.send(content=content, view=self.panel_view)
        self.state_data["panel_message_id"] = msg.id
        save_state(self.state_data)

    async def update_panel_message(self):
        channel = self.get_channel(CHANNEL_ID)
        if channel is None or not isinstance(channel, discord.TextChannel):
            channel = await self.fetch_channel(CHANNEL_ID)  # type: ignore[assignment]
        assert isinstance(channel, discord.TextChannel)

        msg_id = self.state_data.get("panel_message_id")
        if not isinstance(msg_id, int):
            return

        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(content=render_panel_text(self.state_data), view=self.panel_view)
        except discord.NotFound:
            # 패널이 삭제됐으면 재생성
            self.state_data["panel_message_id"] = None
            save_state(self.state_data)
            await self.ensure_panel_message()
        except discord.Forbidden:
            # 편집 권한이 없을 때
            pass

    async def reschedule_boss(self, boss_name: str):
        # 기존 task 취소
        t = self.spawn_tasks.get(boss_name)
        if t and not t.done():
            t.cancel()

        ns = self.state_data["bosses"][boss_name].get("next_spawn")
        if not isinstance(ns, int) or ns <= 0:
            self.spawn_tasks.pop(boss_name, None)
            return

        self.spawn_tasks[boss_name] = asyncio.create_task(self._spawn_alarm_task(boss_name, ns))

    async def _spawn_alarm_task(self, boss_name: str, target_ts: int):
        try:
            # target_ts까지 sleep
            wait = max(0, target_ts - now_ts())
            if wait > 0:
                await asyncio.sleep(wait)

            # 알림 전송
            channel = self.get_channel(CHANNEL_ID)
            if channel is None or not isinstance(channel, discord.TextChannel):
                channel = await self.fetch_channel(CHANNEL_ID)  # type: ignore[assignment]
            assert isinstance(channel, discord.TextChannel)

            hours = BOSSES[boss_name]
            await channel.send(
                f"🔔 **{boss_name} 젠 시간입니다!** ({hours}h)\n"
                f"- 예정 젠: <t:{target_ts}:F> | <t:{target_ts}:R>\n"
                f"※ 실제로 잡았으면 패널에서 **{boss_name} 컷**을 눌러 다음 젠을 갱신하세요.\n"
                f"※ 안 떴으면 **{boss_name} 멍**으로 기존 젠 기준 연장하세요."
            )

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[ERROR] alarm task for {boss_name}: {e}")


def main():
    bot = BossBot()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
