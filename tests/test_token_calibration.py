"""Uplift U31: token estimates learn each model's real chars-per-token from usage."""

from trendlab.context.manager import calibrate, estimate_tokens, message_tokens


def test_calibration_converges_to_the_observed_ratio():
    msgs = [{"role": "user", "content": "x" * 12000}]
    cpt = 4.0
    for _ in range(12):  # JSON-like content measured at ~3.15 chars/token on DeepSeek
        cpt = calibrate(cpt, msgs, 3810)
    assert 3.1 < cpt < 3.25
    est = estimate_tokens("y" * 12000, cpt)
    assert abs(est - 3810) / 3810 < 0.05  # len/4 was 21% low on the same text
    assert calibrate(4.0, msgs, 50) == 4.0  # tiny calls do not move it
    assert calibrate(4.0, msgs, 100000) >= 0.7 * 4.0 + 0.3 * 2.0  # clamped


def test_message_tokens_uses_the_ratio():
    m = {"role": "user", "content": "a" * 3000}
    assert message_tokens(m) == 754 and message_tokens(m, 3.0) == 1004
