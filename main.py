"""astrbot_plugin_deepseek_peak_switch - DeepSeek 高峰/低峰时段自动切换模型提供商。

功能：
- 按 DeepSeek 官方高峰/低峰时段，为不同 bot 自动切换模型提供商（Provider）；
- 自动模式：定时拉取官方定价文档解析时段（兼容新旧措辞与中英文），失败自动回退：
  备用页面 -> 本地缓存 -> 内置默认；
- 节假日支持：识别中国法定节假日（在线数据 + 手动清单），节假日全天按低峰（空闲时段）处理；
- 手动模式：可自由配置高峰/低峰星期与时段；
- 指令：/ds_switch_status（状态）、/ds_switch_refresh（刷新）、/ds_switch_force（强制切换）。

许可证：MIT
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time as time_mod
from datetime import datetime, timedelta, timezone
from html import unescape

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import Provider
from astrbot.api.star import Context, Star, StarTools

PLUGIN_NAME = "astrbot_plugin_deepseek_peak_switch"
_TAG = "[deepseek_peak_switch]"

DEFAULT_DOCS_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
DEFAULT_DOCS_URL_FALLBACK = "https://api-docs.deepseek.com/quick_start/pricing"
DEFAULT_HOLIDAY_URL = "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"
HTTP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 内置默认高峰时段（与 DeepSeek 官方文档一致）：周一至周五 09:00-12:00、14:00-18:00
BUILTIN_WEEKDAYS = frozenset({0, 1, 2, 3, 4})
BUILTIN_WINDOWS: tuple[tuple[int, int], ...] = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))

SOURCE_AUTO = "官方文档自动"
SOURCE_CACHE = "本地缓存（上次成功结果）"
SOURCE_BUILTIN = "内置默认"
SOURCE_MANUAL = "手动配置"

_WEEKDAY_CN = "一二三四五六日"
_CN_DAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_EN_WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# ---------- 通用解析工具 ----------

def _strip_html(raw: str) -> str:
    """去除 HTML 标签、脚本与样式，折叠空白，返回纯文本。"""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text)

def _split_sentences(text: str) -> list[str]:
    """按中英文句读/分号切分句子。"""
    return [p for p in re.split(r"[。；;!?！？]|\.\s", text) if p.strip()]

def _times_of(seg: str) -> tuple[tuple[int, int], ...] | None:
    """从文本片段解析时间窗口（分钟）。解析失败返回 None。"""
    mins: list[int] = []
    for h, m in re.findall(r"(\d{1,2})\s*[:：]\s*(\d{2})", seg):
        hh, mm = int(h), int(m)
        if hh > 23 or mm > 59:
            return None
        mins.append(hh * 60 + mm)
    if not (2 <= len(mins) <= 8) or len(mins) % 2 != 0:
        return None
    windows: list[tuple[int, int]] = []
    for i in range(0, len(mins), 2):
        s, e = mins[i], mins[i + 1]
        if s == e:
            return None
        windows.append((s, e))
    return tuple(sorted(set(windows)))

def _weekdays_of(seg: str) -> frozenset[int] | None:
    """从文本片段解析星期集合（周一=0）。解析不出返回 None。"""
    low = seg.lower()
    if "工作日" in seg or "weekday" in low:
        return frozenset({0, 1, 2, 3, 4})
    if "每天" in seg or "每日" in seg or "every day" in low:
        return frozenset(range(7))
    m = re.search(
        r"(?:周|星期)\s*([一二三四五六日天])\s*(?:至|到|~|～|—|–|-)\s*(?:周|星期)?\s*"
        r"([一二三四五六日天])",
        seg,
    )
    if m:
        a, b = _CN_DAY_MAP[m.group(1)], _CN_DAY_MAP[m.group(2)]
        if a <= b:
            return frozenset(range(a, b + 1))
        return frozenset(list(range(a, 7)) + list(range(0, b + 1)))
    m2 = re.search(
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*"
        r"(?:through|thru|to|~|—|–|-)\s*"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
        low,
    )
    if m2:
        a, b = _EN_WEEKDAY_MAP[m2.group(1)], _EN_WEEKDAY_MAP[m2.group(2)]
        if a <= b:
            return frozenset(range(a, b + 1))
        return frozenset(list(range(a, 7)) + list(range(0, b + 1)))
    return None

def parse_peak_schedule(
    html: str,
) -> tuple[frozenset[int] | None, tuple[tuple[int, int], ...], bool] | None:
    """解析官方文档 HTML。

    返回 (星期集合|None, 时间窗集合(北京时间), 是否排除法定节假日)；解析失败返回 None。

    兼容新旧措辞与中英文，例如：
    - 高峰时段为北京时间周一至周五 9:00 - 12:00、14:00 - 18:00（其余为空闲时段）      [旧版]
    - 北京时间周一至周五（不含中国法定节假日）9:00 - 12:00、14:00 - 18:00 为高峰时段    [新版]
    - Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday ...   [英文]
    """
    text = _strip_html(html)
    if not text:
        return None

    def _finish(weekdays, windows, seg):
        if "utc" in seg.lower():
            windows = tuple(
                sorted(
                    {
                        ((s + 8 * 60) % (24 * 60), (e + 8 * 60) % (24 * 60))
                        for s, e in windows
                    }
                )
            )
        excl = ("法定节假日" in seg) or ("public holiday" in seg.lower())
        return weekdays, windows, excl

    # 第一遍：句级扫描（兼容"关键词在前/在后"两种措辞）
    for seg in _split_sentences(text):
        low = seg.lower()
        if ("高峰" not in seg) and ("peak hour" not in low):
            continue
        windows = _times_of(seg)
        if not windows:
            continue
        return _finish(_weekdays_of(seg), windows, seg)

    # 第二遍：关键词前后窗口扫描（兜底）
    matches = list(re.finditer(r"高峰时段", text)) + list(
        re.finditer(r"[Pp]eak [Hh]ours?", text)
    )
    for m in matches:
        i = m.start()
        seg = text[max(0, i - 220):i + 160]
        windows = _times_of(seg)
        if not windows:
            continue
        return _finish(_weekdays_of(seg), windows, seg)
    return None

def _fmt_min(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"

def _fmt_weekdays(weekdays) -> str:
    wd = sorted(weekdays)
    if not wd:
        return "（未指定）"
    if len(wd) == 7:
        return "每天"
    if wd == [0, 1, 2, 3, 4]:
        return "周一至周五"
    return "、".join(f"周{_WEEKDAY_CN[d]}" for d in wd)

def _fmt_windows(windows) -> str:
    return "、".join(f"{_fmt_min(s)}-{_fmt_min(e)}" for s, e in windows)

# ---------- 手动时段解析 ----------

def _day_token(token: str) -> int | None:
    """把单个星期记号（1-7 / 一~日）转为 0-6（周一=0）。"""
    token = token.strip()
    if token.isdigit():
        v = int(token)
        return v - 1 if 1 <= v <= 7 else None
    return _CN_DAY_MAP.get(token)

def _parse_manual_weekdays(items) -> frozenset[int] | None:
    """解析手动星期配置，如 ["1-5"] / ["1,2,3"] / ["一-五"]。失败返回 None。"""
    if isinstance(items, str):
        items = re.split(r"[,\s，、]+", items)
    days: set[int] = set()
    for raw in items or []:
        for token in re.split(r"[,\s，、]+", str(raw)):
            if not token:
                continue
            m = re.fullmatch(
                r"([1-7一二三四五六日天])\s*[-~～—–至到]\s*([1-7一二三四五六日天])",
                token,
            )
            if m:
                a, b = _day_token(m.group(1)), _day_token(m.group(2))
                if a is None or b is None:
                    return None
                if a <= b:
                    days.update(range(a, b + 1))
                else:
                    days.update(list(range(a, 7)) + list(range(0, b + 1)))
                continue
            d = _day_token(token)
            if d is None:
                return None
            days.add(d)
    return frozenset(days) if days else None

def _parse_manual_windows(items) -> tuple[tuple[int, int], ...] | None:
    """解析手动时段窗口，如 ["09:00-12:00", "23:00~01:00"]。失败返回 None。"""
    if isinstance(items, str):
        items = [items]
    windows: list[tuple[int, int]] = []
    for raw in items or []:
        for token in re.split(r"[,\s，、;；]+", str(raw)):
            if not token:
                continue
            m = re.fullmatch(
                r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~～—–至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})",
                token,
            )
            if not m:
                return None
            h1, m1, h2, m2 = (int(x) for x in m.groups())
            if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
                return None
            s, e = h1 * 60 + m1, h2 * 60 + m2
            if s == e:
                return None
            windows.append((s, e))
    return tuple(sorted(set(windows))) if windows else None

# ---------- 节假日数据解析 ----------

def parse_holiday_json(text: str) -> dict[str, str]:
    """解析 holiday-cn 格式 JSON，返回 {日期: 名称}（仅法定休假日）。"""
    data = json.loads(text)
    days = data.get("days") or []
    result: dict[str, str] = {}
    for item in days:
        if not isinstance(item, dict) or not item.get("isOffDay"):
            continue
        date = str(item.get("date") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            result[date] = str(item.get("name") or "法定节假日")
    return result

class DeepSeekPeakSwitchPlugin(Star):
    """DeepSeek 高峰/低峰自动切换插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 自动模式时段
        self._schedule_weekdays: frozenset[int] = frozenset(BUILTIN_WEEKDAYS)
        self._schedule_windows: tuple[tuple[int, int], ...] = BUILTIN_WINDOWS
        self._schedule_source: str = SOURCE_BUILTIN
        self._schedule_updated: datetime | None = None
        self._schedule_excl_holidays: bool = False
        self._last_fetch_error: str | None = None

        # 节假日数据
        self._holiday_dates: dict[str, str] = {}
        self._holiday_source: str = ""
        self._holiday_updated: datetime | None = None
        self._holiday_error: str | None = None

        self._fetch_task: asyncio.Task | None = None
        # 强制模式：key = "{platform_id}:{self_id}"，value = "peak" / "offpeak"
        self._force: dict[str, str] = {}
        self._missing_warned: dict[str, float] = {}
        self._last_applied: dict[str, str] = {}

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        await self._load_cache()
        self._fetch_task = asyncio.create_task(self._fetch_loop())

    async def terminate(self) -> None:
        task = self._fetch_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self._fetch_task = None

    # ---------- 网络与更新 ----------

    async def _http_get(self, url: str) -> str:
        import aiohttp  # AstrBot 运行环境自带

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers={"User-Agent": HTTP_UA}, allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return await resp.text()

    def _cfg_interval_hours(self) -> float:
        try:
            hours = float(self.config.get("fetch_interval_hours", 24) or 24)
        except (TypeError, ValueError):
            hours = 24.0
        return max(0.25, min(hours, 24.0 * 30))

    def _is_auto_mode(self) -> bool:
        return (
            str(self.config.get("schedule_mode", "auto") or "auto").strip().lower()
            != "manual"
        )

    def _manual_schedule(self):
        """手动模式配置（有效时返回 (weekdays, windows, kind)，否则 None）。"""
        if self._is_auto_mode():
            return None
        weekdays = _parse_manual_weekdays(self.config.get("manual_weekdays") or [])
        windows = _parse_manual_windows(self.config.get("manual_windows") or [])
        if weekdays is None or windows is None:
            return None
        kind = str(self.config.get("manual_kind", "peak") or "peak").strip().lower()
        if kind not in ("peak", "offpeak"):
            kind = "peak"
        return weekdays, windows, kind

    async def _fetch_loop(self) -> None:
        await asyncio.sleep(3)  # 稍等插件加载完成
        while True:
            try:
                if bool(self.config.get("auto_fetch", True)) and self._is_auto_mode():
                    await self._fetch_schedule_once()
                if bool(self.config.get("respect_holidays", True)):
                    await self._fetch_holidays_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"{_TAG} 后台更新任务异常：{e}")
            await asyncio.sleep(self._cfg_interval_hours() * 3600)

    async def _fetch_schedule_once(self) -> bool:
        """拉取官方文档并解析高峰时段（主地址失败自动尝试备用地址）。"""
        primary = str(self.config.get("docs_url") or "").strip() or DEFAULT_DOCS_URL
        fallback = (
            str(self.config.get("docs_url_fallback") or "").strip()
            or DEFAULT_DOCS_URL_FALLBACK
        )
        urls = [primary]
        if fallback and fallback not in urls:
            urls.append(fallback)

        last_err: str | None = None
        for url in urls:
            try:
                html = await self._http_get(url)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                continue
            parsed = parse_peak_schedule(html)
            if not parsed:
                last_err = "文档已获取，但未能解析出高峰时段"
                continue
            weekdays, windows, excl = parsed
            self._schedule_weekdays = (
                weekdays if weekdays else frozenset(BUILTIN_WEEKDAYS)
            )
            self._schedule_windows = windows
            self._schedule_excl_holidays = bool(excl)
            self._schedule_source = SOURCE_AUTO
            self._schedule_updated = datetime.now(BEIJING_TZ)
            self._last_fetch_error = None
            await self._save_cache()
            logger.info(
                f"{_TAG} 已更新高峰时段：{_fmt_weekdays(self._schedule_weekdays)} "
                f"{_fmt_windows(self._schedule_windows)}"
                f"{'（不含法定节假日）' if excl else ''}（来源：官方文档）"
            )
            return True

        self._last_fetch_error = last_err or "未知错误"
        logger.warning(f"{_TAG} 拉取官方文档失败：{self._last_fetch_error}（{primary}）")
        return False

    async def _fetch_holidays_once(self) -> bool:
        """拉取中国法定节假日数据（holiday-cn 格式 JSON）。"""
        year = datetime.now(BEIJING_TZ).year
        tpl = str(self.config.get("holiday_url") or "").strip() or DEFAULT_HOLIDAY_URL
        url = tpl.replace("{year}", str(year))
        try:
            text = await self._http_get(url)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._holiday_error = f"{type(e).__name__}: {e}"
            logger.warning(f"{_TAG} 拉取节假日数据失败：{self._holiday_error}")
            return False
        try:
            dates = parse_holiday_json(text)
        except Exception as e:  # noqa: BLE001
            self._holiday_error = f"数据解析失败：{e}"
            logger.warning(f"{_TAG} 节假日数据解析失败：{e}")
            return False
        if not dates:
            self._holiday_error = "节假日数据为空"
            logger.warning(f"{_TAG} 节假日数据为空（{url}）")
            return False
        self._holiday_dates = dates
        self._holiday_source = f"在线（{year}年）"
        self._holiday_updated = datetime.now(BEIJING_TZ)
        self._holiday_error = None
        await self._save_holiday_cache(year)
        logger.info(f"{_TAG} 已更新节假日数据：{year}年共 {len(dates)} 天")
        return True

    # ---------- 本地缓存 ----------

    def _data_file(self, name: str) -> str | None:
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            os.makedirs(str(data_dir), exist_ok=True)
            return os.path.join(str(data_dir), name)
        except Exception:  # noqa: BLE001
            return None

    async def _load_cache(self) -> None:
        # 时段缓存
        path = self._data_file("schedule_cache.json")
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                weekdays = frozenset(
                    int(x) for x in (data.get("weekdays") or []) if 0 <= int(x) <= 6
                )
                windows = tuple(
                    (int(item[0]), int(item[1]))
                    for item in (data.get("windows") or [])
                    if isinstance(item, (list, tuple)) and len(item) == 2
                )
                if weekdays and windows:
                    self._schedule_weekdays = weekdays
                    self._schedule_windows = windows
                    self._schedule_source = SOURCE_CACHE
                    updated = data.get("updated_at")
                    if isinstance(updated, str):
                        try:
                            self._schedule_updated = datetime.fromisoformat(updated)
                        except ValueError:
                            self._schedule_updated = None
                    logger.info(
                        f"{_TAG} 已加载本机缓存时段：{_fmt_weekdays(weekdays)} "
                        f"{_fmt_windows(windows)}"
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{_TAG} 读取时段缓存失败：{e}")

        # 节假日缓存
        hpath = self._data_file("holidays_cache.json")
        if hpath and os.path.isfile(hpath):
            try:
                with open(hpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                year = int(data.get("year") or 0)
                if year == datetime.now(BEIJING_TZ).year:
                    dates = {
                        str(k): str(v) for k, v in (data.get("dates") or {}).items()
                    }
                    if dates:
                        self._holiday_dates = dates
                        self._holiday_source = f"本地缓存（{year}年）"
                        updated = data.get("updated_at")
                        if isinstance(updated, str):
                            try:
                                self._holiday_updated = datetime.fromisoformat(updated)
                            except ValueError:
                                self._holiday_updated = None
                        logger.info(f"{_TAG} 已加载节假日缓存：共 {len(dates)} 天")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{_TAG} 读取节假日缓存失败：{e}")

    async def _save_cache(self) -> None:
        path = self._data_file("schedule_cache.json")
        if not path:
            return
        try:
            data = {
                "weekdays": sorted(self._schedule_weekdays),
                "windows": [[s, e] for s, e in self._schedule_windows],
                "updated_at": (
                    self._schedule_updated or datetime.now(BEIJING_TZ)
                ).isoformat(),
                "source": self._schedule_source,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{_TAG} 写入时段缓存失败：{e}")

    async def _save_holiday_cache(self, year: int) -> None:
        path = self._data_file("holidays_cache.json")
        if not path:
            return
        try:
            data = {
                "year": year,
                "dates": self._holiday_dates,
                "updated_at": (
                    self._holiday_updated or datetime.now(BEIJING_TZ)
                ).isoformat(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{_TAG} 写入节假日缓存失败：{e}")

    # ---------- 时段与规则判定 ----------

    def _holiday_map(self) -> dict[str, str]:
        """合并在线节假日数据与手动清单（手动优先）。"""
        result = dict(self._holiday_dates)
        raw = self.config.get("holiday_dates") or []
        if isinstance(raw, str):
            raw = re.split(r"[,\s，、;；]+", raw)
        for item in raw:
            d = str(item).strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                result[d] = "手动添加"
        return result

    def _is_holiday(self, now: datetime) -> str | None:
        if not bool(self.config.get("respect_holidays", True)):
            return None
        return self._holiday_map().get(now.strftime("%Y-%m-%d"))

    def _in_windows(self, now: datetime, windows) -> bool:
        cur = now.hour * 60 + now.minute
        for s, e in windows:
            if s <= e:
                if s <= cur < e:
                    return True
            elif cur >= s or cur < e:  # 跨天窗口（如 23:00-01:00）
                return True
        return False

    def _is_peak(self, now: datetime | None = None) -> bool:
        """当前是否处于高峰时段（按北京时间；节假日全天视为低峰）。"""
        now = now or datetime.now(BEIJING_TZ)
        if self._is_holiday(now):
            return False

        manual = self._manual_schedule()
        if manual:
            weekdays, windows, kind = manual
            in_set = (now.weekday() in weekdays) and self._in_windows(now, windows)
            return in_set if kind == "peak" else (not in_set)

        if now.weekday() not in self._schedule_weekdays:
            return False
        return self._in_windows(now, self._schedule_windows)

    @staticmethod
    def _bot_key(event: AstrMessageEvent) -> str:
        return f"{event.get_platform_id() or '?'}:{event.get_self_id() or '?'}"

    @staticmethod
    def _bot_tokens(event: AstrMessageEvent) -> set[str]:
        tokens: set[str] = set()
        for value in (
            str(event.get_platform_id() or "").strip(),
            str(event.get_self_id() or "").strip(),
            str(event.unified_msg_origin or "").strip(),
        ):
            if value:
                tokens.add(value)
        return tokens

    def _match_rule(self, event: AstrMessageEvent) -> dict | None:
        """匹配 bot 规则；未命中时回退到默认规则；都没有返回 None。"""
        rules = self.config.get("bot_rules") or []
        if isinstance(rules, dict):
            rules = [rules]
        tokens = self._bot_tokens(event)
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            raw = rule.get("bots") or []
            if isinstance(raw, str):
                raw = re.split(r"[,\s，、;；]+", raw)
            for item in raw:
                item = str(item).strip()
                if item and item in tokens:
                    return rule
        default_peak = str(self.config.get("default_peak_provider") or "").strip()
        default_offpeak = str(self.config.get("default_offpeak_provider") or "").strip()
        if default_peak or default_offpeak:
            return {
                "name": "默认规则",
                "peak_provider": default_peak,
                "offpeak_provider": default_offpeak,
            }
        return None

    def _resolve_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        """返回 (目标提供商 ID, 模式)；无需切换时返回 ("", "")。"""
        rule = self._match_rule(event)
        if not rule:
            return "", ""
        mode = self._force.get(self._bot_key(event))
        if mode not in ("peak", "offpeak"):
            mode = "peak" if self._is_peak() else "offpeak"
        key = "peak_provider" if mode == "peak" else "offpeak_provider"
        provider_id = str(rule.get(key) or "").strip()
        if not provider_id:
            return "", ""
        return provider_id, mode

    def _provider_ok(self, provider_id: str) -> bool:
        try:
            manager = getattr(self.context, "provider_manager", None)
            inst_map = getattr(manager, "inst_map", None)
            if isinstance(inst_map, dict):
                return isinstance(inst_map.get(provider_id), Provider)
            return isinstance(self.context.get_provider_by_id(provider_id), Provider)
        except Exception:  # noqa: BLE001
            return False

    def _warn_missing(self, provider_id: str) -> None:
        now = time_mod.time()
        if now - self._missing_warned.get(provider_id, 0.0) < 600:
            return
        self._missing_warned[provider_id] = now
        logger.warning(
            f"{_TAG} 提供商 {provider_id} 不存在或类型不符，该消息不做切换。"
            "请在插件配置中确认选择的模型提供商。"
        )

    # ---------- 核心：消息阶段注入目标提供商 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_switch(self, event: AstrMessageEvent) -> None:
        """消息处理阶段：为命中的 bot 注入当前时段应使用的模型提供商。"""
        try:
            if not bool(self.config.get("enabled", True)):
                return
            provider_id, mode = self._resolve_target(event)
            if not provider_id:
                return
            if not self._provider_ok(provider_id):
                self._warn_missing(provider_id)
                return
            event.set_extra("selected_provider", provider_id)
            bot_key = self._bot_key(event)
            if self._last_applied.get(bot_key) != provider_id:
                self._last_applied[bot_key] = provider_id
                logger.info(
                    f"{_TAG} {bot_key} 已切换到"
                    f"{'高峰' if mode == 'peak' else '低峰'}模型：{provider_id}"
                )
        except Exception as e:  # noqa: BLE001
            logger.error(f"{_TAG} 消息处理出错：{e}")

    # ---------- 指令 ----------

    @filter.command("ds_switch_status", alias={"ds_switch"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 DeepSeek 高峰/低峰切换状态。"""
        now = datetime.now(BEIJING_TZ)
        is_peak = self._is_peak(now)
        auto_mode = self._is_auto_mode()
        manual = self._manual_schedule()
        bot_key = self._bot_key(event)
        rule = self._match_rule(event)
        forced = self._force.get(bot_key, "")
        target_id, target_mode = self._resolve_target(event)

        lines = [
            "DeepSeek 高峰/低峰自动切换 · 状态",
            f"启用：{'是' if bool(self.config.get('enabled', True)) else '否'}",
            f"当前北京时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（周{_WEEKDAY_CN[now.weekday()]}）",
            f"当前时段：{'高峰' if is_peak else '低峰'}",
            f"模式：{'自动（拉取官方文档）' if auto_mode else '手动（自定义时段）'}",
        ]

        if manual:
            wd, w, kind = manual
            lines.append(
                f"自定义时间段：{_fmt_weekdays(wd)} {_fmt_windows(w)}"
                f"（定义为{'高峰' if kind == 'peak' else '低峰'}）"
            )
            lines.append(f"时段来源：{SOURCE_MANUAL}")
        else:
            if not auto_mode:
                lines.append("⚠ 手动配置无效（星期/时段格式有误），已回退自动时段")
            lines.append(
                "时段来源："
                + self._schedule_source
                + (
                    f"（更新于 {self._schedule_updated.strftime('%m-%d %H:%M')}）"
                    if self._schedule_updated
                    else ""
                )
            )
            lines.append(
                f"高峰判定：{_fmt_weekdays(self._schedule_weekdays)} "
                f"{_fmt_windows(self._schedule_windows)}"
                + ("（不含法定节假日）" if self._schedule_excl_holidays else "")
            )

        # 节假日
        if not bool(self.config.get("respect_holidays", True)):
            lines.append("节假日处理：未启用")
        else:
            hmap = self._holiday_map()
            hsrc = self._holiday_source or ("手动清单" if hmap else "无数据")
            extra = (
                f"（更新于 {self._holiday_updated.strftime('%m-%d %H:%M')}）"
                if self._holiday_updated
                else ""
            )
            if self._holiday_error:
                extra += f"｜最近错误：{self._holiday_error}"
            lines.append(f"节假日数据：{hsrc} · {now.year}年共 {len(hmap)} 天{extra}")
            hname = self._is_holiday(now)
            lines.append(
                f"今日：法定节假日 - {hname}（全天按低峰处理）"
                if hname
                else "今日：正常日（非节假日）"
            )

        lines.append(
            f"当前 bot 标识：platform_id={event.get_platform_id() or '未知'} / "
            f"self_id={event.get_self_id() or '未知'}"
        )

        if rule:
            lines.append(f"命中规则：{str(rule.get('name') or '未命名规则')}")
            lines.append(f"低峰模型：{str(rule.get('offpeak_provider') or '未配置')}")
            lines.append(f"高峰模型：{str(rule.get('peak_provider') or '未配置')}")
            if target_id:
                lines.append(
                    f"当前将使用：{target_id}（{'高峰' if target_mode == 'peak' else '低峰'}模型）"
                )
            else:
                lines.append("当前将使用：不切换（当前时段对应模型未配置）")
            if forced:
                lines.append(
                    f"强制模式：{'高峰' if forced == 'peak' else '低峰'}"
                    "（发送 /ds_switch_force auto 可恢复自动）"
                )
        else:
            lines.append("未命中任何规则：当前 bot 不会被自动切换。")
            lines.append("提示：将上面的 bot 标识填入插件配置的「多 bot 切换规则」即可启用。")

        rules_count = len(self.config.get("bot_rules") or [])
        lines.append(f"已配置规则数：{rules_count}")
        if self._last_fetch_error and auto_mode:
            lines.append(f"最近一次文档拉取：失败（{self._last_fetch_error}）")
        yield event.plain_result("\n".join(lines))

    @filter.command("ds_switch_refresh", alias={"ds_refresh"})
    async def cmd_refresh(self, event: AstrMessageEvent):
        """立即刷新时段/节假日数据（仅管理员）。"""
        if not event.is_admin():
            yield event.plain_result("该指令仅限管理员使用。")
            return
        yield event.plain_result("正在刷新 DeepSeek 时段数据…")
        msgs: list[str] = []
        if self._is_auto_mode():
            ok = await self._fetch_schedule_once()
            if ok:
                msgs.append(
                    "时段已更新："
                    f"{_fmt_weekdays(self._schedule_weekdays)} "
                    f"{_fmt_windows(self._schedule_windows)}（北京时间）"
                )
            else:
                msgs.append(
                    f"时段更新失败：{self._last_fetch_error or '未知错误'}"
                    f"（继续使用：{self._schedule_source}）"
                )
        else:
            msgs.append("当前为手动时段模式，跳过文档时段拉取。")
        if bool(self.config.get("respect_holidays", True)):
            hok = await self._fetch_holidays_once()
            if hok:
                msgs.append(f"节假日数据已更新：共 {len(self._holiday_dates)} 天")
            else:
                msgs.append(f"节假日数据更新失败：{self._holiday_error or '未知错误'}")
        yield event.plain_result("\n".join(msgs))

    @filter.command("ds_switch_force", alias={"ds_force"})
    async def cmd_force(self, event: AstrMessageEvent, mode: str = ""):
        """强制切换高峰/低峰模型（仅管理员；auto 恢复自动）。"""
        if not event.is_admin():
            yield event.plain_result("该指令仅限管理员使用。")
            return
        raw = (mode or "").strip().lower()
        mapping = {
            "peak": "peak",
            "高峰": "peak",
            "高峰期": "peak",
            "1": "peak",
            "offpeak": "offpeak",
            "off": "offpeak",
            "低峰": "offpeak",
            "低峰期": "offpeak",
            "0": "offpeak",
            "auto": "auto",
            "自动": "auto",
            "取消": "auto",
        }
        target = mapping.get(raw)
        if not target:
            yield event.plain_result(
                "用法：/ds_switch_force peak|offpeak|auto\n"
                "（高峰 / 低峰 / 恢复自动；仅对当前 bot 生效）"
            )
            return
        bot_key = self._bot_key(event)
        if target == "auto":
            self._force.pop(bot_key, None)
            current = "高峰" if self._is_peak() else "低峰"
            msg = f"已恢复自动模式。当前时段：{current}。"
        else:
            self._force[bot_key] = target
            msg = (
                f"已强制为{'高峰' if target == 'peak' else '低峰'}模式"
                "（仅当前 bot，插件重载后恢复自动）。"
            )
            provider_id, _ = self._resolve_target(event)
            if provider_id:
                msg += f"\n当前将使用：{provider_id}"
            else:
                msg += "\n提示：当前 bot 未配置对应的模型提供商，实际不会切换。"
        yield event.plain_result(msg)