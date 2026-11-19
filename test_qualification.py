
from qualification import decide_next


def test_one_month_flow():

    lead, reply = decide_next(None, "Hi")

    assert lead["step"] == "status"
    assert reply["type"] == "list"
    assert "1/4" not in reply["body"]

    lead, reply = decide_next(
        lead,
        "status_construction"
    )

    assert lead["step"] == "timeline"
    assert reply["type"] == "list"

    lead, reply = decide_next(
        lead,
        "timeline_1"
    )

    assert lead["step"] == "location"

    lead, reply = decide_next(
        lead,
        "KPHB, Hyderabad"
    )

    assert lead["step"] == "visit"

    lead, reply = decide_next(
        lead,
        "This Saturday"
    )

    assert lead["status"] == "completed"
    assert reply["type"] == "text"


def test_deferred_flow():

    for option in [
        "timeline_2",
        "timeline_3",
        "timeline_4"
    ]:

        lead, _ = decide_next(None, "Hi")

        lead, _ = decide_next(
            lead,
            "status_interiors"
        )

        lead, reply = decide_next(
            lead,
            option
        )

        assert lead["status"] == "deferred"
        assert lead["step"] == "closed"
        assert lead["project_location"] is None
        assert lead["preferred_site_visit"] is None

        assert reply["type"] == "text"
        assert "Thank you" in reply["body"]

        # No automatic restart
        lead, reply = decide_next(
            lead,
            "Hi again"
        )

        assert reply is None


def test_completed_project():

    lead, _ = decide_next(None, "Hi")

    lead, reply = decide_next(
        lead,
        "status_completed"
    )

    assert lead["step"] == "location"
    assert reply["type"] == "text"


def test_invalid_option():

    lead, _ = decide_next(None, "Hi")

    lead, reply = decide_next(
        lead,
        "invalid"
    )

    assert lead["step"] == "status"
    assert reply["type"] == "list"


def test_human():

    lead, _ = decide_next(None, "Hi")

    lead, reply = decide_next(
        lead,
        "human"
    )

    assert lead["status"] == "human"
    assert reply["type"] == "text"

def test_planning_no_site_visit():

    lead, _ = decide_next(None, "Hi")

    lead, reply = decide_next(
        lead,
        "status_planning"
    )

    assert lead["step"] == "planning_location"
    assert reply["type"] == "text"

    lead, reply = decide_next(
        lead,
        "KPHB, Hyderabad"
    )

    assert lead["status"] == "deferred"
    assert lead["step"] == "closed"
    assert lead["project_location"] == "KPHB, Hyderabad"
    assert lead["preferred_site_visit"] is None

    assert reply["type"] == "text"
    assert "Thank you" in reply["body"]

    # Future messages should not restart screening.
    lead, reply = decide_next(lead, "Hi again")

    assert reply is None



if __name__ == "__main__":

    test_one_month_flow()
    test_deferred_flow()
    test_completed_project()
    test_invalid_option()
    test_human()
    test_planning_no_site_visit()

    print("All updated qualification tests passed")

