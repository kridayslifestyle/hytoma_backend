
"""HYTOMA WhatsApp lead qualification logic."""

INTRO = (
    "Welcome to HYTOMA Automation! 👋\n"
    "Please answer a few quick questions so our team can assist you.\n\n"
)

STATUS_QUESTION = (
    "1/4. What is your current project status?\n"
    "Reply with a number:\n"
    "1. Planning\n"
    "2. Under construction\n"
    "3. Interiors in progress\n"
    "4. Completed"
)

TIMELINE_QUESTION = (
    "2/4. When do you expect construction/interior work to finish?\n"
    "1. Within 1 month\n"
    "2. 1–3 months\n"
    "3. 3–6 months\n"
    "4. More than 6 months"
)

LOCATION_QUESTION = (
    "3/4. Where is your project located? "
    "Please reply with city and area."
)

VISIT_QUESTION = (
    "4/4. When would you prefer a site visit? "
    "Please type a preferred date/day. "
    "Our sales team will confirm availability."
)

FINAL_MESSAGE = (
    "Thank you! ✅ HYTOMA has received your project details. "
    "Our team will review them and contact you for the next steps. "
    "Your preferred site visit is not yet confirmed."
)

HUMAN_MESSAGE = (
    "Sure. I've forwarded this conversation "
    "for HYTOMA team review."
)

STATUS_VALUES = {
    "1": "planning",
    "planning": "planning",
    "plan": "planning",

    "2": "construction",
    "construction": "construction",
    "under construction": "construction",

    "3": "interiors",
    "interiors": "interiors",
    "interior": "interiors",
    "interiors in progress": "interiors",
    "interior work": "interiors",

    "4": "completed",
    "completed": "completed",
    "ready": "completed",
}

TIMELINE_VALUES = {
    "1": "Within 1 month",
    "within 1 month": "Within 1 month",

    "2": "1–3 months",
    "1-3 months": "1–3 months",
    "1–3 months": "1–3 months",

    "3": "3–6 months",
    "3-6 months": "3–6 months",
    "3–6 months": "3–6 months",

    "4": "More than 6 months",
    "more than 6 months": "More than 6 months",
}


def normalize_status(value: str):
    return STATUS_VALUES.get(value.strip().lower())


def normalize_timeline(value: str):
    return TIMELINE_VALUES.get(value.strip().lower())


def decide_next(
    lead: dict | None,
    user_text: str
) -> tuple[dict, str | None]:

    # First message starts qualification.
    if lead is None:
        return {
            "step": "status",
            "status": "screening",
            "project_status": None,
            "completion_timeline": None,
            "project_location": None,
            "preferred_site_visit": None,
        }, INTRO + STATUS_QUESTION

    data = dict(lead)

    # Do not restart completed conversations.
    if data["status"] != "screening":
        return data, None

    answer = (user_text or "").strip()

    if answer.casefold() in {
        "stop", "unsubscribe", "opt out"
    }:
        data.update(
            step="closed",
            status="opted_out"
        )
        return data, None

    if answer.casefold() in {
        "human",
        "agent",
        "salesperson",
        "talk to person"
    }:
        data.update(
            step="closed",
            status="human"
        )
        return data, HUMAN_MESSAGE

    step = data["step"]

    # Question 1 — Project status
    if step == "status":
        value = normalize_status(answer)

        if not value:
            return data, (
                "Please choose 1, 2, 3 or 4.\n\n"
                + STATUS_QUESTION
            )

        data["project_status"] = value

        if value in {"construction", "interiors"}:
            data["step"] = "timeline"
            return data, TIMELINE_QUESTION

        data["step"] = "location"
        return data, LOCATION_QUESTION

    # Question 2 — Completion timeline
    if step == "timeline":
        value = normalize_timeline(answer)

        if not value:
            return data, (
                "Please choose a timeline from 1 to 4.\n\n"
                + TIMELINE_QUESTION
            )

        data["completion_timeline"] = value
        data["step"] = "location"

        return data, LOCATION_QUESTION

    # Question 3 — Project location
    if step == "location":
        if len(answer) < 3 or len(answer) > 200:
            return data, (
                "Please enter your city and area "
                "(for example, Vijayawada, Benz Circle)."
            )

        data["project_location"] = answer
        data["step"] = "visit"

        return data, VISIT_QUESTION

    # Question 4 — Preferred site visit
    if step == "visit":
        if len(answer) < 2 or len(answer) > 200:
            return data, (
                "Please type your preferred "
                "site-visit date or day."
            )

        data["preferred_site_visit"] = answer

        data.update(
            step="closed",
            status="completed"
        )

        return data, FINAL_MESSAGE

    return data, None
