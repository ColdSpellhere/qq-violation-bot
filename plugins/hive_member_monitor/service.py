from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from nonebot.adapters.onebot.v11 import MessageSegment

from .exporter import export_member_list
from .store import (
    DepartureEvent,
    MemberSnapshot,
    MemberSnapshotStore,
    departure_event_key,
    normalize_members,
)


_DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
_SUB_TYPE_LABELS = {
    "leave": "主动退群",
    "kick": "被管理员移出",
    "reconcile": "名单差异复核确认退群",
}
_MAX_RECONCILE_MISSING_MEMBERS = 20
_MAX_RECONCILE_MISSING_RATIO = 0.02
_MASS_DIFFERENCE_CONFIRMATIONS = 3
_MAX_DEPARTURE_DELIVERIES_PER_RUN = 20
_DEPARTURE_LEASE_SECONDS = 600
_MEMBER_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class HiveMemberMonitorService:
    """OneBot-only member monitor; this module has no chat or LLM dependency."""

    def __init__(
        self,
        *,
        config: Any,
        store: MemberSnapshotStore,
        output_dir: Path,
        clock: Callable[[], datetime] | None = None,
        runtime_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.output_dir = Path(output_dir)
        self.clock = clock or datetime.now
        self.runtime_enabled = runtime_enabled
        self._sync_lock = asyncio.Lock()

    @property
    def monitor_group_id(self) -> int:
        return int(self.config.hive_member_monitor_group_id)

    @property
    def report_group_id(self) -> int:
        return int(self.config.hive_member_report_group_id)

    def _enabled(self) -> bool:
        return bool(
            getattr(self.config, "hive_member_monitor_enabled", False)
            and (
                self.runtime_enabled is None
                or self.runtime_enabled()
            )
        )

    @staticmethod
    def _event_is_older_than_member_watermark(
        event_time: int,
        episode_started_at: str | None,
    ) -> bool:
        if not episode_started_at:
            return False
        try:
            episode_started = datetime.strptime(
                episode_started_at,
                _MEMBER_TIMESTAMP_FORMAT,
            ).replace(tzinfo=_DISPLAY_TIMEZONE)
        except ValueError:
            return False
        return int(event_time) < int(episode_started.timestamp())

    async def sync_once(self, bot: Any) -> int:
        """Fetch one full list, persist it, deliver the first workbook, and reconcile."""

        if not self._enabled():
            return 0
        async with self._sync_lock:
            await self.deliver_pending_departures(bot)
            payload = await bot.call_api(
                "get_group_member_list",
                group_id=str(self.monitor_group_id),
                no_cache=True,
            )
            if not isinstance(payload, list):
                self.store.clear_mass_difference_candidate(self.monitor_group_id)
                raise TypeError("get_group_member_list must return a list")
            members = normalize_members(payload)
            if not members:
                self.store.clear_mass_difference_candidate(self.monitor_group_id)
                raise ValueError("get_group_member_list returned no valid members")
            if any(member.group_id != self.monitor_group_id for member in members):
                self.store.clear_mass_difference_candidate(self.monitor_group_id)
                raise ValueError("get_group_member_list returned the wrong group")

            group_info = await bot.call_api(
                "get_group_info",
                group_id=str(self.monitor_group_id),
                no_cache=True,
            )
            if not isinstance(group_info, dict):
                self.store.clear_mass_difference_candidate(self.monitor_group_id)
                raise TypeError("get_group_info must return an object")
            reported_group_id = group_info.get("group_id", self.monitor_group_id)
            try:
                reported_group_id = int(reported_group_id)
                reported_member_count = int(group_info["member_count"])
            except (KeyError, TypeError, ValueError) as exc:
                self.store.clear_mass_difference_candidate(self.monitor_group_id)
                raise ValueError("get_group_info member count is invalid") from exc
            if reported_group_id != self.monitor_group_id:
                self.store.clear_mass_difference_candidate(self.monitor_group_id)
                raise ValueError("get_group_info returned the wrong group")
            if reported_member_count <= 0 or reported_member_count != len(members):
                self.store.clear_mass_difference_candidate(self.monitor_group_id)
                raise ValueError(
                    "get_group_info member count does not match the full member list"
                )
            if not self._enabled():
                return 0

            previous_count = self.store.member_count(self.monitor_group_id)
            if previous_count == 0:
                delta = self.store.replace_snapshot(
                    self.monitor_group_id, members, now=self.clock()
                )
                self.store.clear_mass_difference_candidate(self.monitor_group_id)
            else:
                previous_ids = {
                    member.user_id
                    for member in self.store.list_members(self.monitor_group_id)
                }
                current_ids = {member.user_id for member in members}
                missing_count = len(previous_ids - current_ids)
                ratio_limit = max(
                    1,
                    int(previous_count * _MAX_RECONCILE_MISSING_RATIO),
                )
                maximum_safe_missing = min(
                    _MAX_RECONCILE_MISSING_MEMBERS,
                    ratio_limit,
                )
                missing_threshold = 2
                if missing_count > maximum_safe_missing:
                    confirmations = self.store.observe_mass_difference_candidate(
                        self.monitor_group_id,
                        baseline_user_ids=previous_ids,
                        candidate_user_ids=current_ids,
                        now=self.clock(),
                    )
                    if confirmations < _MASS_DIFFERENCE_CONFIRMATIONS:
                        raise ValueError(
                            "member departure fuse is awaiting a stable complete "
                            "large-difference candidate"
                        )
                    missing_threshold = 1
                else:
                    self.store.clear_mass_difference_candidate(
                        self.monitor_group_id
                    )
                detected_at = int(self.clock().timestamp())
                delta = self.store.reconcile_snapshot_with_departures(
                    self.monitor_group_id,
                    members,
                    now=self.clock(),
                    missing_threshold=missing_threshold,
                    event_time=detected_at,
                    sub_type="reconcile",
                    operator_id=None,
                    source="OneBot V11 get_group_member_list reconciliation",
                )
                if missing_count > maximum_safe_missing:
                    self.store.clear_mass_difference_candidate(
                        self.monitor_group_id
                    )

            if not self.store.initial_export_delivered(
                self.monitor_group_id,
                self.report_group_id,
            ):
                await self._deliver_initial_export(bot)

            if delta.departed:
                await self.deliver_pending_departures(bot)
            return len(members)

    async def _deliver_initial_export(self, bot: Any) -> Path:
        if not self._enabled():
            raise RuntimeError("hive member monitor was disabled before export")
        members = self.store.list_members(self.monitor_group_id)
        path = export_member_list(
            members,
            output_dir=self.output_dir,
            now=self.clock(),
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            if not self._enabled():
                raise RuntimeError("hive member monitor was disabled before upload")
            await bot.call_api(
                "upload_group_file",
                group_id=str(self.report_group_id),
                file=str(path.resolve()),
                name=path.name,
            )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self.store.mark_initial_export_delivered(
            self.monitor_group_id,
            self.report_group_id,
            file_name=path.name,
            sha256=digest,
            now=self.clock(),
        )
        return path

    async def handle_group_decrease(
        self,
        bot: Any,
        *,
        group_id: int,
        user_id: int | str,
        sub_type: str,
        event_time: int,
        operator_id: int | str | None = None,
    ) -> bool:
        if not self._enabled() or int(group_id) != self.monitor_group_id:
            return False
        if str(sub_type).strip() == "kick_me":
            return False
        event_source = "OneBot V11 group_decrease"
        key = departure_event_key(
            self.monitor_group_id,
            user_id,
            str(sub_type),
            int(event_time),
            source=event_source,
        )
        async with self._sync_lock:
            if not self._enabled():
                return False
            existing = self.store.get_departure_event(key)
            if existing is not None:
                if existing.status == "delivered":
                    return False
                await self.deliver_pending_departures(bot, propagate_errors=True)
                return self.store.departure_delivered(key)

            member = self.store.get_member(self.monitor_group_id, user_id)
            member_is_active = member is not None and self.store.member_active(
                self.monitor_group_id, user_id
            )
            episode_started_at = (
                self.store.member_episode_started_at(
                    self.monitor_group_id,
                    user_id,
                )
                if member_is_active
                else None
            )
            if member_is_active and self._event_is_older_than_member_watermark(
                int(event_time),
                episode_started_at,
            ):
                return False
            if member is not None and not member_is_active:
                current_episode = self.store.latest_departure_for_member(
                    self.monitor_group_id, user_id
                )
                if current_episode is not None:
                    if current_episode.status == "delivered":
                        return False
                    await self.deliver_pending_departures(
                        bot,
                        propagate_errors=True,
                    )
                    return self.store.departure_delivered(
                        current_episode.event_key
                    )
            if member is None:
                member = MemberSnapshot(user_id=str(user_id), qq_name=str(user_id))
            departure = self.store.mark_left_and_ensure_departure(
                event_key=key,
                group_id=self.monitor_group_id,
                user_id=member.user_id,
                qq_name=member.qq_name,
                sub_type=str(sub_type),
                event_time=int(event_time),
                operator_id=operator_id,
                source=event_source,
                now=self.clock(),
                expected_episode_started_at=episode_started_at,
            )
            if departure is None:
                return False
            await self.deliver_pending_departures(bot, propagate_errors=True)
            return self.store.departure_delivered(departure.event_key)

    async def handle_group_increase(
        self,
        bot: Any,
        *,
        group_id: int,
        user_id: int | str,
    ) -> bool:
        if not self._enabled() or int(group_id) != self.monitor_group_id:
            return False
        async with self._sync_lock:
            if not self._enabled():
                return False
            payload = await bot.call_api(
                "get_group_member_info",
                group_id=str(self.monitor_group_id),
                user_id=str(user_id),
                no_cache=True,
            )
            if not isinstance(payload, dict):
                raise TypeError("get_group_member_info must return an object")
            if not self._enabled():
                return False
            self.store.upsert_member(
                self.monitor_group_id, payload, now=self.clock()
            )
            return True

    async def deliver_pending_departures(
        self,
        bot: Any,
        *,
        propagate_errors: bool = False,
    ) -> int:
        if not self._enabled():
            return 0
        delivered = 0
        for _ in range(_MAX_DEPARTURE_DELIVERIES_PER_RUN):
            lease_token = uuid4().hex
            departures = self.store.claim_pending_departures(
                self.monitor_group_id,
                lease_token,
                now=self.clock(),
                lease_seconds=_DEPARTURE_LEASE_SECONDS,
                limit=1,
            )
            if not departures:
                break
            departure = departures[0]
            if not self._enabled():
                self.store.release_departure_claim(
                    departure.event_key,
                    lease_token,
                    now=self.clock(),
                )
                break
            try:
                sent = await self._deliver_departure(
                    bot,
                    departure,
                    lease_token=lease_token,
                )
            except Exception:
                if propagate_errors:
                    raise
                break
            delivered += int(sent)
        return delivered

    async def _deliver_departure(
        self,
        bot: Any,
        departure: DepartureEvent,
        *,
        lease_token: str,
    ) -> bool:
        if not self._enabled():
            self.store.release_departure_claim(
                departure.event_key,
                lease_token,
                now=self.clock(),
            )
            return False
        try:
            await bot.send_group_msg(
                group_id=self.report_group_id,
                message=MessageSegment.text(self._departure_message(departure)),
            )
        except Exception as exc:
            self.store.mark_departure_failed(
                departure.event_key,
                exc,
                lease_token,
                now=self.clock(),
            )
            raise
        return self.store.mark_departure_delivered(
            departure.event_key,
            lease_token,
            now=self.clock(),
        )

    def _departure_message(self, departure: DepartureEvent) -> str:
        event_time = datetime.fromtimestamp(
            departure.event_time, tz=_DISPLAY_TIMEZONE
        ).strftime("%Y-%m-%d %H:%M:%S %Z")
        operator = departure.operator_id or "无事件数据"
        event_type = _SUB_TYPE_LABELS.get(departure.sub_type, departure.sub_type)
        return "\n".join(
            (
                "【蜂巢群员退群日志】",
                f"监控群：{departure.group_id}",
                f"成员QQ：{departure.user_id}",
                f"QQ名字：{departure.qq_name}",
                f"退群类型：{event_type}",
                f"操作人QQ：{operator}",
                f"事件时间：{event_time}",
                f"检测来源：{departure.source}",
                f"事件编号：{departure.event_key[:16]}",
                f"当前成员数：{self.store.member_count(departure.group_id)}",
            )
        )
