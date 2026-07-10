import logging
from typing import Optional

import aiohttp

log = logging.getLogger("ntfy")


async def send_push(
    topic_url: str,
    title: str,
    message: str,
    priority: str = "default",
    tags: Optional[list] = None,
) -> bool:
    """POST a push notification to an ntfy topic URL.

    topic_url examples:
      https://ntfy.sh/my-topic
      https://ntfy.sh/bybit-scanner
    """
    if not topic_url:
        return False
    headers = {
        "Title":    title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                topic_url,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                ok = r.status in (200, 201, 202)
                if not ok:
                    body = await r.text()
                    log.warning(f"ntfy {r.status}: {body[:120]}")
                return ok
    except Exception as e:
        log.error(f"ntfy push failed: {e}")
        return False
