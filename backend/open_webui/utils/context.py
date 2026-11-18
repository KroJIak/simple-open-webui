"""Context budget for chat requests.

Every request resends the whole chat history. A chat that grows over weeks
therefore becomes slower and more expensive with every single message, and
eventually exceeds the limits of the model provider.

This module estimates how big a request is and can condense the oldest part
of the history into a short digest, so that the request stays within a
configurable budget. The condensation is deterministic and happens in blocks,
so the unchanged prefix of the request stays byte identical - that keeps the
provider's prompt cache working (a cached prefix is answered several times
faster).
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Optional

from open_webui.config import CONTEXT_TOKEN_ESTIMATE_FACTOR

log = logging.getLogger(__name__)

# Rough fallback when tiktoken is not available: ~3.4 characters per token
# for mixed Russian/English text.
_CHARS_PER_TOKEN = 3.4

# Per message overhead (role, separators) as used by the OpenAI chat format.
_MESSAGE_OVERHEAD_TOKENS = 4

# Tokens an image costs the provider. The images are downscaled before they
# are sent (see MAX_IMAGE_SIDE), so a single tile is enough.
IMAGE_TOKEN_ESTIMATE = 255

# How many characters of a dropped user message end up in the digest.
_DIGEST_MESSAGE_CHARS = 400

# How many messages of the digest are kept.
_DIGEST_MAX_ITEMS = 12

_encoding = None
_encoding_loaded = False


def _get_encoding():
    """Lazily load the tiktoken encoding, None if it is not available."""
    global _encoding, _encoding_loaded

    if _encoding_loaded:
        return _encoding

    _encoding_loaded = True
    try:
        import tiktoken

        from open_webui.config import TIKTOKEN_ENCODING_NAME

        _encoding = tiktoken.get_encoding(str(TIKTOKEN_ENCODING_NAME))
    except Exception as e:  # pragma: no cover - depends on the environment
        log.debug(f'Context: tiktoken is not available, using the estimate ({e})')
        _encoding = None

    return _encoding


def estimate_text_tokens(text: str) -> int:
    """Number of tokens of a text."""
    if not text:
        return 0

    encoding = _get_encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(text, disallowed_special=()))
        except Exception:
            pass

    return int(len(text) / _CHARS_PER_TOKEN)


def _image_size(data_url: str) -> Optional[tuple[int, int]]:
    """Width and height of an inline image, None if they cannot be read."""
    try:
        _, _, payload = data_url.partition(',')
        if not payload:
            return None

        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(payload)))
        return image.size
    except Exception:
        return None


def estimate_image_tokens(data_url: str) -> int:
    """Number of tokens an inline image costs the provider."""
    size = _image_size(data_url)
    if not size:
        return IMAGE_TOKEN_ESTIMATE

    width, height = size
    # The provider splits the image into 512px tiles, every tile costs ~255
    # tokens (the same formula the OpenAI vision models use).
    tiles = max(1, (width // 512) * (height // 512))
    return min(tiles, 12) * IMAGE_TOKEN_ESTIMATE


def estimate_content_tokens(content: Any) -> int:
    """Number of tokens of the content of a message."""
    if content is None:
        return 0

    if isinstance(content, str):
        return estimate_text_tokens(content)

    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                total += estimate_text_tokens(str(part))
                continue

            part_type = part.get('type')
            if part_type == 'text':
                total += estimate_text_tokens(part.get('text', ''))
            elif part_type == 'image_url':
                url = (part.get('image_url') or {}).get('url', '')
                total += estimate_image_tokens(url) if url.startswith('data:') else 0
            elif part_type == 'input_image':
                url = (part.get('image_url') or {}).get('url', '') or part.get('image_url', '')
                total += estimate_image_tokens(url) if str(url).startswith('data:') else 0
            else:
                total += estimate_text_tokens(str(part))
        return total

    return estimate_text_tokens(str(content))


def estimate_message_tokens(message: dict) -> int:
    """Number of tokens of a single message."""
    if not isinstance(message, dict):
        return 0

    tokens = _MESSAGE_OVERHEAD_TOKENS + estimate_content_tokens(message.get('content'))

    for call in message.get('tool_calls') or []:
        tokens += estimate_text_tokens(str(call))

    return int(tokens * CONTEXT_TOKEN_ESTIMATE_FACTOR)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Number of tokens of a list of messages."""
    return sum(estimate_message_tokens(message) for message in messages)


def count_images(messages: list[dict]) -> int:
    """Number of images in a list of messages."""
    count = 0
    for message in messages:
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get('type') in ('image_url', 'input_image'):
                count += 1
    return count


def build_history_digest(messages: list[dict]) -> str:
    """A short, deterministic summary of the messages that were dropped.

    The digest keeps the questions of the user and the topics of the answers,
    so the model still knows what the conversation was about. It never contains
    the full text - the point is to save tokens.
    """
    if not messages:
        return ''

    questions: list[str] = []
    answers: list[str] = []
    tools: set[str] = set()
    images = 0

    for message in messages:
        role = message.get('role')
        content = message.get('content')

        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get('type') in ('image_url', 'input_image'):
                    images += 1

        text = content if isinstance(content, str) else _text_of_parts(content)
        text = ' '.join((text or '').split())
        if not text:
            continue

        if role == 'user':
            questions.append(text)
        elif role == 'assistant':
            answers.append(text)

        for call in message.get('tool_calls') or []:
            name = (call or {}).get('function', {}).get('name') if isinstance(call, dict) else None
            if name:
                tools.add(name)

    lines = [
        f'[Earlier conversation summary - {len(messages)} messages, {images} images were condensed]'
    ]

    if questions:
        lines.append('The user asked about:')
        for question in _pick(questions, 8, 4):
            lines.append(f'- {question[:_DIGEST_MESSAGE_CHARS]}')

    if answers:
        lines.append('The assistant answered:')
        for answer in _pick(answers, 3, 3):
            lines.append(f'- {answer[:_DIGEST_MESSAGE_CHARS]}')

    if tools:
        lines.append(f'Tools used: {", ".join(sorted(tools))}')

    if images:
        lines.append(f'Images were attached in the earlier part of the conversation ({images}).')

    return '\n'.join(lines)


def _pick(items: list[str], first: int, last: int) -> list[str]:
    """The first and the last items of a list.

    The first questions show what the conversation was about, the last ones
    are the closest to the part of the chat that is kept.
    """
    if len(items) <= first + last:
        return items

    return items[:first] + items[-last:]


def _text_of_parts(content: Any) -> str:
    if not isinstance(content, list):
        return ''

    parts = []
    for part in content:
        if isinstance(part, dict) and part.get('type') == 'text':
            parts.append(part.get('text', ''))
    return ' '.join(parts)


def compact_messages(
    messages: list[dict],
    budget: int,
    block_size: int = 20,
    keep_recent: int = 20,
    headroom: float = 0.85,
    tokens_before: int | None = None,
) -> tuple[list[dict], dict]:
    """Condense the oldest messages so the request fits into the budget.

    The oldest messages are replaced by a short digest. The amount of dropped
    messages is rounded up to a multiple of `block_size`, so the condensed
    prefix only changes every few messages and the provider's prompt cache
    stays valid.
    """
    info = {
        'compacted': False,
        'dropped_messages': 0,
        'dropped_tokens': 0,
        'tokens_before': (
            tokens_before if tokens_before is not None else estimate_messages_tokens(messages)
        ),
        'tokens_after': 0,
    }

    if not budget or budget <= 0 or len(messages) <= keep_recent:
        info['tokens_after'] = info['tokens_before']
        return messages, info

    if info['tokens_before'] <= budget:
        info['tokens_after'] = info['tokens_before']
        return messages, info

    # The system prompt always stays, it is not a part of the conversation.
    head = 0
    while head < len(messages) and messages[head].get('role') == 'system':
        head += 1

    target = int(budget * headroom)

    # Keep the newest messages that still fit into the target budget.
    kept_indexes: list[int] = []
    used = 0
    for index in range(len(messages) - 1, head - 1, -1):
        tokens = estimate_message_tokens(messages[index])
        if used + tokens > target and len(kept_indexes) >= keep_recent:
            break
        kept_indexes.append(index)
        used += tokens

    kept_indexes.reverse()

    if not kept_indexes:
        info['tokens_after'] = info['tokens_before']
        return messages, info

    boundary = kept_indexes[0]

    # A tool result without the assistant message that requested it is an
    # invalid request - move the boundary in front of the assistant message.
    while boundary > head and messages[boundary].get('role') == 'tool':
        boundary -= 1

    dropped_count = boundary - head
    if block_size > 0:
        # Round up to the next block so that the condensed prefix only changes
        # every `block_size` messages (the prompt cache stays valid).
        rounded = ((dropped_count + block_size - 1) // block_size) * block_size
        if 0 < rounded <= len(messages) - head - keep_recent:
            dropped_count = rounded
            while dropped_count > 0 and messages[head + dropped_count].get('role') == 'tool':
                dropped_count -= 1

    dropped = messages[head : head + dropped_count]
    kept = messages[head + dropped_count :]

    digest = build_history_digest(dropped)
    if digest:
        kept = [{'role': 'user', 'content': digest}, *kept]

    info.update(
        {
            'compacted': True,
            'dropped_messages': dropped_count,
            'dropped_tokens': info['tokens_before'] - estimate_messages_tokens(kept),
            'tokens_after': estimate_messages_tokens(kept),
        }
    )
    log.info(
        f'Context compacted: dropped {dropped_count} of {len(messages)} messages '
        f'({info["tokens_before"]} -> {info["tokens_after"]} tokens)'
    )

    return messages[:head] + kept, info


def prepare_context_estimator() -> None:
    """Load the tokenizer of the context estimation.

    Called in the background on startup - the first chat request should not
    have to wait for the tokenizer to be loaded.
    """
    encoding = _get_encoding()
    log.info(f'Context estimator ready (tokenizer: {"tiktoken" if encoding else "estimate"})')


def apply_context_budget(messages: list[dict]) -> tuple[list[dict], dict]:
    """Condense the history when it exceeds the configured token budget."""
    from open_webui.config import (
        CONTEXT_COMPACTION_BLOCK,
        CONTEXT_KEEP_RECENT_MESSAGES,
        CONTEXT_TOKEN_BUDGET,
    )

    return compact_messages(
        messages,
        budget=CONTEXT_TOKEN_BUDGET,
        block_size=CONTEXT_COMPACTION_BLOCK,
        keep_recent=CONTEXT_KEEP_RECENT_MESSAGES,
    )

