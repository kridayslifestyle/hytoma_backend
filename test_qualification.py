
from qualification import decide_next


def test_construction():
    lead, reply = decide_next(None, "Hi")

    assert lead["step"] == "status"
    assert "project status" in reply

    lead, reply = decide_next(lead, "2")
    assert lead["step"] == "timeline"

    lead, reply = decide_next(lead, "2")
    assert lead["step"] == "location"

    lead, reply = decide_next(
        lead,
        "Bhimavaram, AP"
    )
    assert lead["step"] == "visit"

    lead, reply = decide_next(
        lead,
        "Saturday afternoon"
    )

    assert lead["status"] == "completed"
    assert "not yet confirmed" in reply

    updated, reply = decide_next(
        lead,
        "New lock available?"
    )

    assert updated == lead
    assert reply is None


def test_skip_timeline():
    lead, _ = decide_next(None, "Hi")

    lead, reply = decide_next(lead, "4")

    assert lead["step"] == "location"
    assert lead["completion_timeline"] is None


def test_invalid_option():
    lead, _ = decide_next(None, "Hi")

    lead, reply = decide_next(
        lead,
        "unknown"
    )

    assert lead["step"] == "status"
    assert "Please choose" in reply


def test_human():
    lead, _ = decide_next(None, "Hi")

    lead, reply = decide_next(
        lead,
        "human"
    )

    assert lead["status"] == "human"
    assert "HYTOMA" in reply


if __name__ == "__main__":
    test_construction()
    test_skip_timeline()
    test_invalid_option()
    test_human()

    print("All qualification tests passed")
