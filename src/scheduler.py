"""APScheduler 백그라운드 잡.

- 매주 수 14:00  → 자료 마감 리마인더 (내일 발표자 DM)
- 매주 목 10:00  → 오늘 발표 채널 공지
- 매주 목 16:00  → 지난 일정 자동 완료 마킹 + 발표자 stats 갱신 (catch-up 포함)
- 매일 12:00     → 연기 마감 임박 (8일 전) 발표자 DM
- 매주 토 09:00  → 다가올 일정 < 2주면 다음 사이클 자동 추첨 + 채널 공지
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from slack_sdk import WebClient

from .config import DB_PATH, TIMEZONE
from .db import session
from .services import cycle_service, member_service, notification_service, submission_service

log = logging.getLogger(__name__)


def _job_wednesday_reminder(client: WebClient) -> None:
    today = date.today()
    if today.weekday() != 2:
        return
    target_thu = today + timedelta(days=1)
    with session(DB_PATH) as conn:
        notification_service.send_wednesday_reminder(client, conn, target_thu)


def _job_thursday_announce(client: WebClient) -> None:
    today = date.today()
    if today.weekday() != 3:
        return
    with session(DB_PATH) as conn:
        notification_service.send_thursday_announce(client, conn, today)


def _job_thursday_complete(client: WebClient) -> None:
    today = date.today()
    with session(DB_PATH) as conn:
        completed = cycle_service.mark_past_seminars_completed(conn, today)
    if completed:
        log.info("auto-completed %d schedule(s): %s", len(completed), [s.date.isoformat() for s in completed])


def _job_defer_deadline(client: WebClient) -> None:
    today = date.today()
    with session(DB_PATH) as conn:
        notification_service.send_defer_deadline_reminders(client, conn, today)


def _job_cycle_check(client: WebClient) -> None:
    today = date.today()
    with session(DB_PATH) as conn:
        if not cycle_service.needs_new_cycle(conn, today, threshold_weeks=2):
            log.info("cycle_check: 충분히 남음, skip")
            return
        log.info("cycle_check: 잔여 일정 부족, 사이클 직전 sync + 새 사이클 추첨")
        member_service.sync_from_channel(client, conn)
        cycle_id, schedules = cycle_service.generate_next_cycle(conn, today)
        notification_service.ask_for_topics(client, conn, schedules)
    notification_service.announce_new_cycle(client, schedules, cycle_id)
    log.info("새 사이클 cycle_id=%d 공지 완료", cycle_id)


def _job_member_sync(client: WebClient) -> None:
    with session(DB_PATH) as conn:
        active, errors = member_service.sync_from_channel(client, conn)
    log.info("member_sync: active=%d errors=%s", len(active), errors)


def _job_monday_preview(client: WebClient) -> None:
    today = date.today()
    if today.weekday() != 0:
        return  # 월요일만
    with session(DB_PATH) as conn:
        notification_service.send_monday_preview(client, conn, today)


def _job_topic_reminder(client: WebClient) -> None:
    today = date.today()
    with session(DB_PATH) as conn:
        notification_service.send_topic_reminders(client, conn, today)


def _job_wednesday_distribute(client: WebClient) -> None:
    """수요일 14:00 — 내일(목) 발표 자료 중 아직 배포 안 된 ingested 자료 자동 게시."""
    from .slack import flows
    today = date.today()
    if today.weekday() != 2:
        return
    target_thu = today + timedelta(days=1)
    with session(DB_PATH) as conn:
        subs = submission_service.get_for_seminar(conn, target_thu)
        for sub in subs:
            if sub.announce_ts:
                log.info("distribute skip (already announced): submission %d", sub.id)
                continue
            ok, msg = flows.distribute_submission(client, conn, sub.id)
            log.info("distribute submission %d: ok=%s msg=%s", sub.id, ok, msg)


def start_scheduler(client: WebClient) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=TIMEZONE)

    scheduler.add_job(
        _job_wednesday_reminder, args=(client,),
        # 퇴근 전 (17:00) — 자료 미제출 발표자에게 /제출 안내
        trigger=CronTrigger(day_of_week="wed", hour=17, minute=0, timezone=TIMEZONE),
        id="wednesday_reminder", replace_existing=True,
    )
    scheduler.add_job(
        _job_thursday_announce, args=(client,),
        trigger=CronTrigger(day_of_week="thu", hour=10, minute=0, timezone=TIMEZONE),
        id="thursday_announce", replace_existing=True,
    )
    scheduler.add_job(
        _job_wednesday_distribute, args=(client,),
        # 수 14:00 (발표 전날 점심) — 내일(목) 발표자 ingested 자료 자동 배포
        trigger=CronTrigger(day_of_week="wed", hour=14, minute=0, timezone=TIMEZONE),
        id="wednesday_distribute", replace_existing=True,
    )
    scheduler.add_job(
        _job_thursday_complete, args=(client,),
        trigger=CronTrigger(day_of_week="thu", hour=16, minute=0, timezone=TIMEZONE),
        id="thursday_complete", replace_existing=True,
    )
    scheduler.add_job(
        _job_defer_deadline, args=(client,),
        trigger=CronTrigger(hour=12, minute=0, timezone=TIMEZONE),  # 매일
        id="defer_deadline", replace_existing=True,
    )
    scheduler.add_job(
        _job_cycle_check, args=(client,),
        trigger=CronTrigger(day_of_week="sat", hour=9, minute=0, timezone=TIMEZONE),
        id="cycle_check", replace_existing=True,
    )
    scheduler.add_job(
        _job_member_sync, args=(client,),
        trigger=CronTrigger(hour=9, minute=0, timezone=TIMEZONE),  # 매일 09:00
        id="member_sync", replace_existing=True,
    )
    scheduler.add_job(
        _job_monday_preview, args=(client,),
        trigger=CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=TIMEZONE),
        id="monday_preview", replace_existing=True,
    )
    scheduler.add_job(
        _job_topic_reminder, args=(client,),
        trigger=CronTrigger(hour=10, minute=0, timezone=TIMEZONE),  # 매일 10:00
        id="topic_reminder", replace_existing=True,
    )

    scheduler.start()
    log.info("scheduler started (TZ=%s) jobs: %s", TIMEZONE, [j.id for j in scheduler.get_jobs()])
    return scheduler
