"""
Delivery channel for job wake-ups. The DATABASE decides what is runnable (jobs.status,
available_at, leases); this queue only tells workers *which job id to look at next*. If a
message is lost (Redis restart/failover) the reaper republishes from the database, so a lost
message delays a job, it never loses one.

  RedisQueue  - ZSET ordered by (priority, enqueue time). Pro jobs sort first. Delayed retries
                wait in a second ZSET until due.
  NullQueue   - no Redis configured: workers poll the database directly (slower, still correct).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from utils import redis_client

logger = logging.getLogger("omixa.queue")

PENDING = "omx:q:pending"
DELAYED = "omx:q:delayed"
DEAD = "omx:q:dead"
REAPER_LOCK = "omx:q:reaper-lock"
_PRIORITY_SPAN = 1e10  # score = priority * span + unix time, so priority dominates, FIFO within


class RedisQueue:
    name = "redis"

    def __init__(self, client):
        self.r = client

    def publish(self, job_id: str, priority: int = 1, delay: float = 0) -> None:
        if delay > 0:
            self.r.zadd(DELAYED, {f"{priority}:{job_id}": time.time() + delay})
        else:
            # nx: republishing an already-queued id (reaper) must not move it to the back
            self.r.zadd(PENDING, {job_id: priority * _PRIORITY_SPAN + time.time()}, nx=True)

    def pop(self) -> Optional[str]:
        got = self.r.zpopmin(PENDING, 1)
        return got[0][0] if got else None

    def promote_due(self) -> int:
        due = self.r.zrangebyscore(DELAYED, "-inf", time.time(), start=0, num=500)
        moved = 0
        for member in due:
            if self.r.zrem(DELAYED, member):  # only one promoter wins per member
                prio, _, job_id = member.partition(":")
                self.publish(job_id, int(prio or 1))
                moved += 1
        return moved

    def depth(self) -> int:
        return int(self.r.zcard(PENDING)) + int(self.r.zcard(DELAYED))

    def position(self, job_id: str) -> Optional[int]:
        rank = self.r.zrank(PENDING, job_id)
        return None if rank is None else int(rank) + 1

    def dead_letter(self, job_id: str, reason: str) -> None:
        self.r.lpush(DEAD, f"{int(time.time())}:{job_id}:{reason}"[:200])
        self.r.ltrim(DEAD, 0, 999)

    def try_lock(self, name: str, seconds: int) -> bool:
        return bool(self.r.set(name, "1", nx=True, ex=seconds))


class NullQueue:
    name = "db-poll"

    def publish(self, job_id, priority=1, delay=0): return None
    def pop(self): return None
    def promote_due(self): return 0
    def depth(self): return None
    def position(self, job_id): return None
    def dead_letter(self, job_id, reason): return None
    def try_lock(self, name, seconds): return True


def get_queue():
    """Redis queue when Redis is configured, else the DB-polling fallback. Resolved per call
    (cheap) so tests and a Redis recovery take effect immediately."""
    r = redis_client.get_redis()
    return RedisQueue(r) if r is not None else NullQueue()


def safe_publish(job_id: str, priority: int = 1, delay: float = 0) -> bool:
    """Publishing is best-effort: the job row is already committed, and the reaper republishes
    queued rows whose message never arrived."""
    try:
        get_queue().publish(job_id, priority, delay)
        return True
    except Exception as exc:
        redis_client.note_failure("queue.publish", exc)
        return False
