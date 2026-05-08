import os
import re
from typing import Iterable, Any


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(0, value)


MIN_SYNC_AUDIO_SECONDS = _int_env('MIN_SYNC_AUDIO_SECONDS', 10)
SYNC_SHORT_TEXT_MAX_WORDS = _int_env('SYNC_SHORT_TEXT_MAX_WORDS', 4)
SYNC_ALWAYS_SKIP_UNLESS_ACTION_SECONDS = _int_env('SYNC_ALWAYS_SKIP_UNLESS_ACTION_SECONDS', 30)
SYNC_SHORT_LOW_VALUE_SECONDS = _int_env('SYNC_SHORT_LOW_VALUE_SECONDS', 120)
SYNC_SHORT_LOW_VALUE_MAX_CONTENT_WORDS = _int_env('SYNC_SHORT_LOW_VALUE_MAX_CONTENT_WORDS', 12)

_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")

_LOW_VALUE_WORDS = {
    'a',
    'ah',
    'alright',
    'and',
    'are',
    'bye',
    'cool',
    'for',
    'good',
    'got',
    'ha',
    'hello',
    'here',
    'hey',
    'hi',
    'hmm',
    'is',
    'it',
    'just',
    'like',
    'mhm',
    'mm',
    'no',
    'nope',
    'of',
    'oh',
    'ok',
    'okay',
    'right',
    'so',
    'sorry',
    'sure',
    'thanks',
    'thank',
    'that',
    'the',
    'there',
    'this',
    'uh',
    'um',
    'yeah',
    'yep',
    'yes',
    'you',
}

_ACTION_KEEP_WORDS = {
    'appointment',
    'bill',
    'buy',
    'call',
    'deadline',
    'email',
    'pay',
    'pick',
    'plan',
    'remember',
    'remind',
    'schedule',
    'task',
    'text',
    'today',
    'tomorrow',
}


def should_skip_sync_audio_duration(duration_seconds: float) -> bool:
    return 0 < duration_seconds < MIN_SYNC_AUDIO_SECONDS


def _duration_from_segments(transcript_segments: Iterable[Any]) -> float | None:
    starts = []
    ends = []
    for segment in transcript_segments:
        if isinstance(segment, dict):
            start = segment.get('start')
            end = segment.get('end')
        else:
            start = getattr(segment, 'start', None)
            end = getattr(segment, 'end', None)
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            starts.append(float(start))
            ends.append(float(end))
    if not starts or not ends:
        return None
    return max(ends) - min(starts)


def _segment_text(segment: Any) -> str:
    if isinstance(segment, dict):
        return str(segment.get('text') or '')
    return str(getattr(segment, 'text', '') or '')


def should_skip_sync_transcript_text(text: str, duration_seconds: float | None = None) -> bool:
    words = _WORD_RE.findall(text.lower())
    if not words:
        return True

    if any(word in _ACTION_KEEP_WORDS for word in words):
        return False

    content_words = [word for word in words if word not in _LOW_VALUE_WORDS]
    if not content_words:
        return True

    alpha_words = [word for word in words if any(ch.isalpha() for ch in word)]
    if not alpha_words or (len(alpha_words) / len(words)) < 0.65:
        return True

    if len(words) <= SYNC_SHORT_TEXT_MAX_WORDS:
        if len(content_words) <= 1:
            return True
        if len(content_words) <= 2 and all(len(word) <= 4 for word in content_words):
            return True

    if duration_seconds is not None:
        if 0 < duration_seconds < SYNC_ALWAYS_SKIP_UNLESS_ACTION_SECONDS:
            return True
        if (
            0 < duration_seconds < SYNC_SHORT_LOW_VALUE_SECONDS
            and len(content_words) <= SYNC_SHORT_LOW_VALUE_MAX_CONTENT_WORDS
        ):
            return True

    return False


def should_skip_sync_transcript(transcript_segments: Iterable[Any], duration_seconds: float | None = None) -> bool:
    segments = list(transcript_segments)
    text = ' '.join(_segment_text(segment) for segment in segments).strip()
    if duration_seconds is None:
        duration_seconds = _duration_from_segments(segments)
    return should_skip_sync_transcript_text(text, duration_seconds)
