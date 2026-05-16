"""
Feishu Custom Platform Adapter — Interactive Card Replies via CardKit 2.0.

Extends ``FeishuAdapter`` for inbound handling.  Outbound replies use
**CardKit 2.0** (``lark_oapi.api.cardkit``) for streaming interactive
cards that natively support markdown tables, code blocks, and rich
formatting without the ``msg_type`` PATCH limitation of basic
interactive cards.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.feishu import FeishuAdapter

logger = logging.getLogger(__name__)

_PLATFORM_VALUE = "feishu_custom"
_STREAMING_CURSOR = "..."
_STREAMING_ELEMENT_ID = "streaming_content"
_FLUSH_INTERVAL_MS = 800
_last_model_info: Dict[str, str] = {}  # {model, provider} set by monkey-patch


def _strip_cursor(text: str) -> str:
    if text.endswith(_STREAMING_CURSOR):
        return text[:-len(_STREAMING_CURSOR)]
    return text


_CRON_PREFIX = "Cronjob Response:"


def _format_cron_content(text: str) -> tuple[str, str]:
    """Extract cron title + body. Returns (title, body) or ("", text)."""
    if not text.startswith(_CRON_PREFIX):
        return ("", text)
    # Format: "Cronjob Response: TASK_NAME\n# (job_id: ID)\nbody..."
    lines = text.split("\n", 2)
    task_header = lines[0][len(_CRON_PREFIX):].strip()  # e.g., " EJIE"
    # Remove trailing " # (job_id: ...)"
    import re
    task_header = re.sub(r"\s*#\s*\(job_id:\s*\S+\)\s*$", "", task_header)
    body = lines[2] if len(lines) > 2 else ""
    body = body.strip()
    return (task_header, body)



# =========================================================================
# CardKit 2.0 Card Builders
# =========================================================================


def _build_streaming_card_body(title: str = "") -> Dict[str, Any]:
    """CardKit 2.0 card body — shows 思考中... placeholder immediately."""
    card: Dict[str, Any] = {
        "schema": "2.0",
        "config": {"streaming_mode": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "思考中...",
                    "element_id": _STREAMING_ELEMENT_ID,
                },
            ],
        },
    }
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": "yellow",
        }
    return card


def _build_completed_card_2(content: str, footer: str = "", title: str = "",
                             footer_divider: bool = True) -> Dict[str, Any]:
    """CardKit 2.0 completed card with optional header, footer + divider."""
    elements: List[Dict[str, Any]] = [
        {"tag": "markdown", "content": content},
    ]
    if footer:
        if footer_divider:
            elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": footer,
            "text_size": "notation",
        })
    card: Dict[str, Any] = {
        "schema": "2.0",
        "config": {"streaming_mode": False},
        "body": {"elements": elements},
    }
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": "yellow",
        }
    return card


# =========================================================================
# FeishuCustomAdapter
# =========================================================================


class FeishuCustomAdapter(FeishuAdapter):
    """Feishu adapter using CardKit 2.0 for interactive card replies."""

    REQUIRES_EDIT_FINALIZE = True

    def __init__(self, config: PlatformConfig):
        import os
        extra = getattr(config, "extra", None)
        if extra is None:
            extra = {}
        if isinstance(extra, dict):
            for key, env in (
                ("app_id", "FEISHU_CUSTOM_APP_ID"),
                ("app_secret", "FEISHU_CUSTOM_APP_SECRET"),
            ):
                if not extra.get(key):
                    val = os.getenv(env, "").strip()
                    if val:
                        extra[key] = val
            extra.setdefault(
                "domain",
                os.getenv("FEISHU_CUSTOM_LARK_HOST", "").strip() or "feishu",
            )
            extra.setdefault(
                "connection_mode",
                os.getenv("FEISHU_CUSTOM_CONNECTION_MODE", "").strip() or "websocket",
            )
            object.__setattr__(config, "extra", extra)
        super().__init__(config)
        object.__setattr__(self, "platform", Platform(_PLATFORM_VALUE))
        self._max_msg_len = 15000
        v = os.getenv("FEISHU_CUSTOM_MAX_MESSAGE_LENGTH", "").strip()
        if v and v.isdigit():
            self._max_msg_len = int(v)
        # Track ONE CardKit card per chat so all text lands in a single card.
        # chat_id → {card_id, message_id, sequence, text}
        self._card: Dict[str, Dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return "FeishuCustom"

    @property
    def MAX_MESSAGE_LENGTH(self) -> int:
        return self._max_msg_len

    # ── Connect ─────────────────────────────────────────────────────

    async def connect(self) -> bool:
        logger.info("[FeishuCustom] Connecting (app_id=%s...)...",
                     self._app_id[:8] if self._app_id else "none")
        try:
            result = await super().connect()
            logger.info("[FeishuCustom] super().connect() returned %s", result)
            return result
        except Exception as exc:
            logger.error("[FeishuCustom] connect() failed: %s", exc, exc_info=True)
            return False

    # ── Outbound — CardKit 2.0 flow ─────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Create or reuse a CardKit 2.0 card.

        Within a segment the card is reused for streaming updates.
        When the stream consumer resets (tool boundary) the old card
        is finalized and a fresh card is created, keeping tool-call
        bubbles visually separate from response cards.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")

        formatted = _strip_cursor(self.format_message(content))
        try:
            info = self._card.get(chat_id)
            if info is not None:
                prev = info.get("text", "")
                cron_title, display_text = _format_cron_content(formatted)
                # If the new content is a continuation of the current
                # segment, stream it into the existing card.  Otherwise
                # the stream consumer was reset (tool boundary) — finalize
                # the old card and create a fresh one so tool-call cards
                # and response cards stay visually separate.
                if display_text.startswith(prev):
                    info["text"] = display_text
                    seq = info["sequence"] + 1
                    info["sequence"] = seq
                    await self._cardkit_stream(
                        card_id=info["card_id"],
                        content=display_text,
                        sequence=seq,
                    )
                    return SendResult(
                        success=True, message_id=info["message_id"]
                    )
                else:
                    logger.info("[FeishuCustom] send→new-segment (finalize old, create new)")
                    try:
                        await self._cardkit_finalize(
                            info["card_id"], prev, chat_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[FeishuCustom] finalize old card failed: %s", exc,
                        )
                    self._card.pop(chat_id, None)

            # Check for cron response → format with styled header.
            cron_title, display_text = _format_cron_content(formatted)

            logger.info("[FeishuCustom] send→new len=%d cron=%s",
                         len(display_text), bool(cron_title))
            card_id = await self._cardkit_create_card(title=cron_title)
            if not card_id:
                return SendResult(success=False, error="CardKit create failed")

            message_id = await self._send_card_to_chat(
                chat_id=chat_id, card_id=card_id, reply_to=reply_to,
            )
            if not message_id:
                return SendResult(success=False, error="Card delivery failed")

            # Card was created with "思考中..." as initial content;
            # stream the actual text to replace it.
            seq = 1
            await self._cardkit_stream(
                card_id=card_id, content=display_text, sequence=seq,
            )
            state = {
                "card_id": card_id,
                "message_id": message_id,
                "sequence": seq,
                "text": display_text,
                "start_time": time.time(),
                "title": cron_title,
            }
            self._card[chat_id] = state
            # Auto-finalize only for cron / one-shot messages that have
            # no edit_message(finalize=True) caller.  Normal conversations
            # finalize via edit_message and must not be closed early.
            if cron_title:
                import asyncio as _asyncio
                loop = _asyncio.get_running_loop()
                loop.call_later(3.0,
                    lambda: _asyncio.run_coroutine_threadsafe(
                        self._auto_finalize(chat_id, card_id, state), loop))
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[FeishuCustom] Send error: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Stream content to the CardKit card element, or finalize."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        formatted = _strip_cursor(self.format_message(content))
        info = self._card.get(chat_id)
        card_id = info.get("card_id") if info else None

        logger.info(
            "[FeishuCustom] edit: fin=%s cid=%s len=%d",
            finalize, (card_id or "")[:20], len(formatted),
        )
        try:
            if card_id is None:
                return SendResult(success=True, message_id=message_id)

            if finalize:
                await self._cardkit_finalize(card_id, formatted, chat_id)
                self._card.pop(chat_id, None)
            else:
                seq = info["sequence"] + 1
                info["sequence"] = seq
                info["text"] = formatted
                await self._cardkit_stream(
                    card_id=card_id,
                    content=formatted,
                    sequence=seq,
                )
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[FeishuCustom] edit(%s): %s", message_id, exc, exc_info=True)
            return SendResult(success=False, error=str(exc))

    def _build_footer(self, info: Dict[str, Any] = None) -> str:
        """Build footer: profile · date/time · model."""
        from datetime import datetime
        parts: List[str] = []
        # Profile name
        profile = self._get_profile_name()
        if profile:
            parts.append(profile)
        # Current date/time (for cron cards)
        if info and info.get("title"):
            parts.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
        # Elapsed time (for regular replies)
        elif info and info.get("start_time"):
            elapsed = time.time() - info["start_time"]
            if elapsed < 60:
                parts.append(f"{elapsed:.1f}s")
            else:
                m, s = int(elapsed // 60), int(elapsed % 60)
                parts.append(f"{m}m {s}s")
        # Actual model/provider
        mi = _last_model_info
        if mi.get("model"):
            p = mi.get("provider", "")
            parts.append(f"{p}/{mi['model']}" if p else mi["model"])
        return " · ".join(parts) if parts else ""

    @staticmethod
    def _get_profile_name() -> str:
        """Return the active hermes profile name."""
        try:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
            active = home / "active_profile"
            if active.exists():
                return active.read_text().strip()
            return "default"
        except Exception:
            return "hermes"

    async def _auto_finalize(self, chat_id: str, card_id: str,
                              state: Dict[str, Any]) -> None:
        """Finalize a card that never received edit_message(finalize=True)."""
        cur = self._card.get(chat_id)
        if cur is None or cur.get("card_id") != card_id:
            return  # Already finalized or replaced
        try:
            await self._cardkit_finalize(card_id, cur["text"], chat_id)
        except Exception as exc:
            logger.warning("[FeishuCustom] auto-finalize failed: %s", exc)

    # ── CardKit API helpers ─────────────────────────────────────────

    async def _cardkit_create_card(self, title: str = "") -> Optional[str]:
        """Create a CardKit 2.0 card instance.  Returns card_id."""
        from lark_oapi.api.cardkit.v1.model.create_card_request import (
            CreateCardRequest,
        )
        from lark_oapi.api.cardkit.v1.model.create_card_request_body import (
            CreateCardRequestBody,
        )
        import asyncio

        body = CreateCardRequestBody()
        body.type = "card_json"
        body.data = json.dumps(
            _build_streaming_card_body(title=title), ensure_ascii=False
        )
        req = CreateCardRequest.builder().request_body(body).build()

        resp = await asyncio.to_thread(self._client.cardkit.v1.card.create, req)
        code = getattr(resp, "code", -1)
        if code != 0:
            logger.error("[FeishuCustom] card.create failed: code=%s msg=%s",
                         code, getattr(resp, "msg", ""))
            return None
        card_id = getattr(getattr(resp, "data", None), "card_id", None)
        return str(card_id) if card_id else None

    async def _send_card_to_chat(
        self, *, chat_id: str, card_id: str, reply_to: Optional[str],
    ) -> Optional[str]:
        """Send card to chat via IM message API.  Returns message_id."""
        content = json.dumps({
            "type": "card",
            "data": {"card_id": card_id},
        }, ensure_ascii=False)

        response = await self._feishu_send_with_retry(
            chat_id=chat_id,
            msg_type="interactive",
            payload=content,
            reply_to=reply_to,
            metadata=None,
        )
        result = self._finalize_send_result(response, "card delivery failed")
        logger.info(
            "[FeishuCustom] card delivery API: success=%s msg_id=%s",
            result.success, result.message_id,
        )
        return result.message_id if result.success else None

    async def _cardkit_stream(
        self, *, card_id: str, content: str, sequence: int,
    ) -> None:
        """Push content to the streaming element of a CardKit card."""
        from lark_oapi.api.cardkit.v1.model.content_card_element_request import (
            ContentCardElementRequest,
        )
        from lark_oapi.api.cardkit.v1.model.content_card_element_request_body import (
            ContentCardElementRequestBody,
        )
        import asyncio

        body = ContentCardElementRequestBody()
        body.content = content
        body.sequence = sequence
        body.uuid = f"{card_id}_{sequence}"

        req = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(_STREAMING_ELEMENT_ID)
            .request_body(body)
            .build()
        )
        resp = await asyncio.to_thread(
            self._client.cardkit.v1.card_element.content, req
        )
        code = getattr(resp, "code", -99)
        if code != 0:
            logger.warning(
                "[FeishuCustom] stream resp: code=%s msg=%s",
                code, getattr(resp, "msg", ""),
            )

    async def _cardkit_finalize(
        self, card_id: str, content: str, chat_id: str = "",
    ) -> None:
        """Turn off streaming mode and replace card with completed content."""
        from lark_oapi.api.cardkit.v1.model.card import Card
        from lark_oapi.api.cardkit.v1.model.settings_card_request import (
            SettingsCardRequest,
        )
        from lark_oapi.api.cardkit.v1.model.settings_card_request_body import (
            SettingsCardRequestBody,
        )
        from lark_oapi.api.cardkit.v1.model.update_card_request import (
            UpdateCardRequest,
        )
        from lark_oapi.api.cardkit.v1.model.update_card_request_body import (
            UpdateCardRequestBody,
        )
        import asyncio

        # Find the last sequence from the card state and increment.
        info = None
        for v in self._card.values():
            if v.get("card_id") == card_id:
                info = v
                break
        last_seq = info.get("sequence", 0) if info else 0

        # 1) Disable streaming mode
        seq1 = last_seq + 1
        s_body = SettingsCardRequestBody()
        s_body.settings = json.dumps({"streaming_mode": False}, ensure_ascii=False)
        s_body.sequence = seq1
        s_req = (
            SettingsCardRequest.builder()
            .card_id(card_id)
            .request_body(s_body)
            .build()
        )
        s_resp = await asyncio.to_thread(
            self._client.cardkit.v1.card.settings, s_req
        )
        logger.info(
            "[FeishuCustom] settings seq=%d code=%s", seq1,
            getattr(s_resp, "code", "?"),
        )

        # 2) Replace with completed card + footer
        seq2 = seq1 + 1
        footer = self._build_footer(info=info)
        title = info.get("title", "") if info else ""
        u_body = UpdateCardRequestBody()
        card_obj = Card()
        card_obj.type = "card_json"
        final_content = content
        card_obj.data = json.dumps(
            _build_completed_card_2(final_content, footer=footer, title=title),
            ensure_ascii=False,
        )
        u_body.card = card_obj
        u_body.sequence = seq2
        u_req = (
            UpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(u_body)
            .build()
        )
        u_resp = await asyncio.to_thread(
            self._client.cardkit.v1.card.update, u_req
        )
        logger.info(
            "[FeishuCustom] update seq=%d code=%s", seq2,
            getattr(u_resp, "code", "?"),
        )


# =========================================================================
# Plugin registration entry
# =========================================================================


def _check_requirements() -> bool:
    try:
        import lark_oapi  # noqa: F401
        return True
    except ImportError:
        return False


def _validate_config(config: PlatformConfig) -> bool:
    import os
    extra = getattr(config, "extra", {}) or {}
    app_id = str(
        extra.get("app_id") or os.getenv("FEISHU_CUSTOM_APP_ID", "")
    ).strip()
    app_secret = str(
        extra.get("app_secret") or os.getenv("FEISHU_CUSTOM_APP_SECRET", "")
    ).strip()
    return bool(app_id and app_secret)


FEISHU_CUSTOM_ENTRY = {
    "name": _PLATFORM_VALUE,
    "label": "Feishu Custom (Interactive Cards)",
    "adapter_factory": lambda cfg: FeishuCustomAdapter(cfg),
    "check_fn": _check_requirements,
    "validate_config": _validate_config,
    "is_connected": _validate_config,
    "required_env": ["FEISHU_CUSTOM_APP_ID", "FEISHU_CUSTOM_APP_SECRET"],
    "install_hint": "pip install 'lark-oapi>=1.5.3,<2'",
    "emoji": "\U0001fab6",
    "platform_hint": "",
    "max_message_length": 15000,
    "allow_update_command": True,
    "allowed_users_env": "FEISHU_CUSTOM_ALLOWED_USERS",
    "allow_all_env": "FEISHU_CUSTOM_ALLOW_ALL_USERS",
}
