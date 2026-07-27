"""Kênh phát sự kiện realtime (Server-Sent Events) cho web app.

Mỗi client mở GET /api/stream sẽ có 1 asyncio.Queue. Khi có mã mới quét /
mã trùng bị chặn, ta đẩy sự kiện vào tất cả queue để web app cập nhật ngay.

Lưu ý: publish() có thể được gọi từ endpoint chạy trong threadpool (def thường),
nên phải đẩy vào queue qua loop.call_soon_threadsafe cho an toàn.
"""
import asyncio
import json
from typing import Any, Dict, Optional, Set

_subscribers: Set[asyncio.Queue] = set()
_loop: Optional[asyncio.AbstractEventLoop] = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Lưu event loop chính (gọi lúc app startup)."""
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def _deliver(payload: Dict[str, Any]) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # client chậm -> bỏ qua, không chặn


def publish(event: str, data: Dict[str, Any]) -> None:
    """Đẩy sự kiện tới mọi subscriber. An toàn khi gọi từ thread khác."""
    payload = {"event": event, "data": data}
    if _loop is not None:
        _loop.call_soon_threadsafe(_deliver, payload)
    else:
        _deliver(payload)


def format_sse(payload: Dict[str, Any]) -> str:
    return f"event: {payload['event']}\ndata: {json.dumps(payload['data'], ensure_ascii=False)}\n\n"
