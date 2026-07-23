import json
import time

from src.model import SystemEvent
from src.schema.system.activity import SystemEventEnvelope


class SystemEventService:
    """系统事件持久化与 SSE 增量读取。"""

    @staticmethod
    def publish(
        *,
        event_type: str,
        payload: dict,
        resource_type: str | None = None,
        resource_id: int | None = None,
    ) -> SystemEvent:
        return SystemEvent.create(
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
        )

    @staticmethod
    def list_after(event_id: int, limit: int = 100) -> list[SystemEventEnvelope]:
        query = (
            SystemEvent.select()
            .where(SystemEvent.id > max(int(event_id), 0))
            .order_by(SystemEvent.id.asc())
            .limit(max(1, limit))
        )
        return [
            SystemEventEnvelope(
                event_id=event.id,
                event=event.event_type,
                data=event.payload or {},
            )
            for event in query
        ]

    @classmethod
    def stream(
        cls,
        *,
        after_event_id: int = 0,
        poll_interval_seconds: float = 1.0,
        heartbeat_interval_seconds: float = 15.0,
    ):
        last_event_id = max(int(after_event_id), 0)
        last_heartbeat_at = time.time()
        while True:
            events = cls.list_after(last_event_id)
            if events:
                for event in events:
                    last_event_id = event.event_id
                    yield (
                        f"id: {event.event_id}\n"
                        f"event: {event.event}\n"
                        f"data: {json.dumps(event.data, ensure_ascii=False)}\n\n"
                    )
                last_heartbeat_at = time.time()
                continue

            now = time.time()
            if now - last_heartbeat_at >= heartbeat_interval_seconds:
                yield "event: heartbeat\ndata: {}\n\n"
                last_heartbeat_at = now
            time.sleep(max(poll_interval_seconds, 0.1))
