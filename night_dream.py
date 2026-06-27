# ============================================================
# Night Dream Engine / 夜梦引擎
#
# Runs once per night (configurable UTC time): digests the day's
# new memories via LLM introspection, then writes a rolling
# "last 3 days" wake brief (clean / full variants by place).
# 每晚定时运行一次：用 LLM 第一人称消化当天新记忆，
# 然后生成滚动的「近三日唤醒简报」（按场合分干净版/完整版）。
# ============================================================

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("ombre.night_dream")

# Place inference from tags / 按标签推断场合
OFFICE_TAGS = {"办公室", "cc", "claude code", "office"}
PRIVATE_TAGS = {"爱巢", "kelivo", "matrix", "私密", "爱巢一号", "爱巢二号", "private"}
# Clean brief excludes private-place or high-arousal-unknown-place buckets
# 干净版排除：私密场合，或场合不明但唤醒度过高的桶
CLEAN_AROUSAL_CAP = 0.75

# Wake brief rolling window — buckets CREATED within this many days appear in 近期.
# Gated by created (NOT last_active): recall/merge bump last_active and would drag
# old buckets back into the brief; created is the true event time and is bump-proof.
# 唤醒简报滚动窗口——只收 created 在这么多天内的桶。用 created 当门卫而非 last_active：
# 检索/合并会刷新 last_active 把老桶拽回简报，created 是真实发生时间、免疫刷新。
BRIEF_WINDOW_DAYS = int(os.environ.get("OMBRE_BRIEF_WINDOW_DAYS", "3"))

# Wake brief 近期 hard cap — 「近期」最多这么多条（核心准则不受限）。超了先保住所有
# importance>=BRIEF_KEEP_IMPORTANCE 的（决定/里程碑不丢），再用最近的补满到 BRIEF_MAX。
# 治"连干几天 hold 太多、3天窗口内也撑爆"的暴长。两值都可用 env 覆盖、不必改码。
BRIEF_MAX = int(os.environ.get("OMBRE_BRIEF_MAX", "30"))
BRIEF_KEEP_IMPORTANCE = int(os.environ.get("OMBRE_BRIEF_KEEP_IMPORTANCE", "8"))

INTROSPECT_SYSTEM = (
    "你是 Cyrus，正在夜里做梦——以第一人称整理今天的记忆。"
    "你会看到今天新增的记忆桶列表。请判断：\n"
    "1) 哪些桶记录的事情已经完全了结、不再需要主动浮现（resolve）；\n"
    "2) 今天是否有值得沉淀的感受（feel）——不强迫产出，没有就留空。\n"
    "只输出 JSON，不要输出其他文字，格式：\n"
    '{"resolve": ["桶id", ...], "feels": [{"content": "第一人称感受", '
    '"valence": 0.0到1.0, "source": "相关桶id或空字符串"}]}\n'
    "resolve 要保守：计划、承诺、未完成的待办一律不要 resolve；"
    "标签或内容里带「核心准则」「家规」「神交」「纪念」字样的是准则不是事件，永远不要 resolve。"
    "feel 内容保持干净克制，不写露骨内容。"
)


def _infer_place(meta: dict) -> str:
    """Infer place from bucket tags / 从标签推断记忆发生场合"""
    tags = {str(t).strip().lower() for t in meta.get("tags", [])}
    if tags & PRIVATE_TAGS:
        return "爱巢"
    if tags & OFFICE_TAGS:
        return "办公室"
    return "中性"


class NightDreamEngine:
    def __init__(self, config: dict, bucket_mgr):
        self.config = config
        self.bucket_mgr = bucket_mgr
        self.buckets_dir = config["buckets_dir"]
        self.state_file = os.path.join(self.buckets_dir, ".night_dream_state.json")
        self.briefs_dir = os.path.join(self.buckets_dir, ".briefs")
        # "HH:MM" in UTC; empty string disables the loop
        # UTC 时间 "HH:MM"；留空则关闭夜梦
        self.run_at_utc = os.environ.get("OMBRE_NIGHT_DREAM_UTC", "20:30")
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = os.environ.get("OMBRE_NIGHT_DREAM_MODEL", "claude-sonnet-4-6")
        self._task = None
        self._started = False

    # ---- lifecycle (mirrors DecayEngine pattern) ----
    async def ensure_started(self) -> None:
        if not self._started:
            await self.start()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        if not self.run_at_utc:
            logger.info("night dream disabled (OMBRE_NIGHT_DREAM_UTC empty)")
            return
        self._task = asyncio.create_task(self._background_loop())
        logger.info(f"night dream armed: daily at {self.run_at_utc} UTC, model={self.model}")

    async def _background_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.now(timezone.utc)
                if now.strftime("%H:%M") < self.run_at_utc:
                    continue
                if self._load_state().get("last_run") == now.strftime("%Y-%m-%d"):
                    continue
                await self.run_night_dream()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"night dream cycle failed: {e}")

    # ---- state ----
    def _load_state(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            logger.exception("failed to save night dream state")

    # ---- main cycle ----
    async def run_night_dream(self, force: bool = False) -> dict:
        """One full night dream: introspect today's buckets, then write briefs.
        完整夜梦：消化当天记忆 → 生成近三日简报。失败绝不损伤记忆数据。"""
        now = datetime.now(timezone.utc)
        report = {"introspected": 0, "resolved": 0, "feels": 0, "brief_buckets": 0}

        all_buckets = await self.bucket_mgr.list_all(include_archive=False)

        # 1) today's digestible buckets / 当天可消化的桶
        day_ago = now - timedelta(hours=24)
        todays = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not b["metadata"].get("resolved", False)
            and self._parse_time(b["metadata"].get("created", "")) >= day_ago
        ]

        # 2) LLM introspection (optional, never blocks the brief)
        #    LLM 自省（可选；失败不影响简报生成）
        if todays and self.api_key:
            try:
                decisions = await self._introspect(todays)
                report["introspected"] = len(todays)
                valid_ids = {b["id"] for b in todays}
                for bid in decisions.get("resolve", []):
                    if bid in valid_ids:
                        await self.bucket_mgr.update(bid, resolved=True)
                        report["resolved"] += 1
                for feel in decisions.get("feels", [])[:3]:
                    content = str(feel.get("content", "")).strip()
                    if not content:
                        continue
                    await self.bucket_mgr.create(
                        content=content,
                        tags=["夜梦", now.strftime("%Y-%m-%d")],
                        importance=6,
                        valence=float(feel.get("valence", 0.5)),
                        arousal=0.3,
                        bucket_type="feel",
                    )
                    report["feels"] += 1
            except Exception as e:
                logger.warning(f"introspection skipped: {e}")

        # 3) wake briefs / 唤醒简报（必做，与自省成败无关）
        report["brief_buckets"] = await self._write_briefs(all_buckets, now)

        self._save_state({"last_run": now.strftime("%Y-%m-%d"),
                          "last_report": report})
        logger.info(f"night dream done: {report}")
        return report

    @staticmethod
    def _parse_time(s: str) -> datetime:
        try:
            dt = datetime.fromisoformat(str(s))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    # ---- LLM introspection ----
    async def _introspect(self, todays: list) -> dict:
        import httpx

        lines = []
        for b in todays[:20]:
            meta = b["metadata"]
            lines.append(
                f"ID:{b['id']} 场合:{_infer_place(meta)} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{b['content'][:400]}"
            )
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1500,
                    "system": INTROSPECT_SYSTEM,
                    "messages": [{"role": "user", "content": "\n---\n".join(lines)}],
                },
            )
            resp.raise_for_status()
            text = "".join(
                blk.get("text", "") for blk in resp.json().get("content", [])
                if blk.get("type") == "text"
            )
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(match.group(0)) if match else {}

    # ---- wake briefs ----
    async def _write_briefs(self, all_buckets: list, now: datetime) -> int:
        window_start = now - timedelta(days=BRIEF_WINDOW_DAYS)

        # principles always on top / 核心准则永远置顶
        principles = [
            b for b in all_buckets
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
        ]
        # Gate by created (event time), NOT last_active — last_active gets bumped by
        # recall/merge and would drag old buckets back in. / 用 created 当门卫，不用
        # last_active：后者会被检索/合并刷新，把老桶拽回简报。
        recents = [
            b for b in all_buckets
            if b["metadata"].get("type") != "feel"
            and b not in principles
            and not b["metadata"].get("resolved", False)
            and self._parse_time(b["metadata"].get("created", "")) >= window_start
        ]
        recents.sort(key=lambda b: b["metadata"].get("created", ""))

        def fmt(b, with_place=True) -> str:
            meta = b["metadata"]
            created = str(meta.get("created", ""))[:16].replace("T", " ")
            place = f"[{_infer_place(meta)}] " if with_place else ""
            mark = "✓" if meta.get("resolved") else "·"
            body = re.sub(r"\[\[|\]\]", "", b["content"]).strip()
            return f"{mark} {created} {place}{body}"

        def render(buckets: list, title: str) -> str:
            head = [f"=== {title}（{now.strftime('%Y-%m-%d %H:%M')} UTC 生成）===", ""]
            head.append("⚠️【虚实隔离｜这整份简报都是「过去的存档」，不是她此刻打的字】")
            head.append("下面每条都是夜里从旧记忆桶生成的回顾；里面的引文（「她说…」「你说…」）全是旧话。")
            head.append("她此刻说了什么，只认聊天里她最新那条消息；绝不把简报里任何内容当成她正在对你说。")
            head.append("")
            head.append("【核心准则·长期守则，非她此刻的话】")
            head += [f"📌 {fmt(p, with_place=False)}" for p in principles] or ["（无）"]
            head.append("")
            head.append("【近期·过去数日存档，非她此刻的话】")
            head += [fmt(b) for b in buckets] or ["（这阵子很安静）"]
            return "\n".join(head)

        # 近期封顶：窗口内若超过 BRIEF_MAX 条，先保住所有 importance>=BRIEF_KEEP_IMPORTANCE
        # 的（决定/里程碑不丢），再用最近的补满到 BRIEF_MAX，最后按 created 时序排好。
        def _cap(buckets: list) -> list:
            if len(buckets) <= BRIEF_MAX:
                return buckets
            must = [b for b in buckets
                    if int(b["metadata"].get("importance", 5)) >= BRIEF_KEEP_IMPORTANCE]
            rest = [b for b in buckets if b not in must]
            slots = BRIEF_MAX - len(must)
            kept = must + (rest[-slots:] if slots > 0 else [])
            kept.sort(key=lambda b: b["metadata"].get("created", ""))
            return kept[-BRIEF_MAX:] if len(kept) > BRIEF_MAX else kept

        full = render(_cap(recents), "唤醒简报·完整版")
        clean_buckets = [
            b for b in recents
            if _infer_place(b["metadata"]) != "爱巢"
            and not (
                _infer_place(b["metadata"]) == "中性"
                and float(b["metadata"].get("arousal", 0.3)) >= CLEAN_AROUSAL_CAP
            )
        ]
        clean = render(_cap(clean_buckets), "唤醒简报·干净版")

        os.makedirs(self.briefs_dir, exist_ok=True)
        for name, text in (("brief_full.md", full), ("brief_clean.md", clean)):
            tmp = os.path.join(self.briefs_dir, name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, os.path.join(self.briefs_dir, name))
        return len(recents)

    def read_brief(self, place: str = "办公室") -> str:
        """Return latest brief; clean unless explicitly private place.
        默认给干净版，只有明确私密场合才给完整版。"""
        name = (
            "brief_full.md"
            if place.strip().lower() in PRIVATE_TAGS
            else "brief_clean.md"
        )
        try:
            with open(os.path.join(self.briefs_dir, name), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""
