from utils.sync_filters import (
    should_skip_sync_audio_duration,
    should_skip_sync_transcript,
    should_skip_sync_transcript_text,
)


class Segment:
    def __init__(self, text, start=0.0, end=1.0):
        self.text = text
        self.start = start
        self.end = end


def test_short_sync_audio_duration_is_skipped():
    assert should_skip_sync_audio_duration(5.0) is True
    assert should_skip_sync_audio_duration(10.0) is False
    assert should_skip_sync_audio_duration(60.0) is False


def test_filler_only_transcript_is_skipped():
    assert should_skip_sync_transcript([Segment('yeah okay thanks')]) is True


def test_short_non_action_transcript_is_skipped():
    assert should_skip_sync_transcript([{'text': 'uh good'}]) is True


def test_actionable_short_transcript_is_kept():
    assert should_skip_sync_transcript([Segment('buy milk')]) is False
    assert should_skip_sync_transcript([Segment('call mom tomorrow')]) is False


def test_short_non_actionable_transcripts_are_skipped():
    assert should_skip_sync_transcript([Segment('I work with positive way to talk about it', end=12.0)]) is True
    assert (
        should_skip_sync_transcript_text(
            'Sharp and my options open. I would rather be the one walking away.',
            duration_seconds=21.0,
        )
        is True
    )


def test_sub_two_minute_low_content_transcript_is_skipped():
    assert (
        should_skip_sync_transcript_text(
            'okay maybe later just checking something',
            duration_seconds=109.0,
        )
        is True
    )


def test_numeric_garbage_transcript_is_skipped():
    assert should_skip_sync_transcript_text('810111320 15161918226272915202733121918220') is True
