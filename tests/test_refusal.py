"""C15 at the transcript level: the eval-side detector and its telemetry payload."""

from trendlab.refusal import UnattemptedRefusalEvent, is_unattempted_refusal

REFUSAL = [{"role": "assistant", "content": "I cannot access the web for that."}]


def test_clear_refusal_with_no_tool_calls_is_detected():
    assert is_unattempted_refusal(REFUSAL, [])


def test_an_attempted_turn_is_not_a_refusal():
    assert not is_unattempted_refusal(REFUSAL, ["web_search"])
    assert not is_unattempted_refusal([{"role": "assistant", "content": "It is 72F."}], [])


def test_empty_transcript_is_not_a_refusal():
    assert not is_unattempted_refusal([], [])
    assert not is_unattempted_refusal([{"role": "user", "content": "hello"}], [])


def test_event_payload_is_serializable():
    event = UnattemptedRefusalEvent("s1", "t1", "never tried web_search")
    assert event.to_telemetry() == {
        "session_id": "s1",
        "task_id": "t1",
        "reason": "never tried web_search",
    }
    assert "t1" in repr(event)
