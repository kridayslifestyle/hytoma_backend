
"""HYTOMA WhatsApp qualification flow."""


def text_reply(message):
    return {
        "type": "text",
        "body": message
    }


def list_reply(message, button, options):
    return {
        "type": "list",
        "body": message,
        "button": button,
        "rows": options
    }


STATUS_OPTIONS = [
    {"id": "status_planning", "title": "Planning"},
    {"id": "status_construction", "title": "Under construction"},
    {"id": "status_interiors", "title": "Interiors in progress"},
    {"id": "status_completed", "title": "Completed"}
]

TIMELINE_OPTIONS = [
    {"id": "timeline_1", "title": "Within 1 month"},
    {"id": "timeline_2", "title": "1-3 months"},
    {"id": "timeline_3", "title": "3-6 months"},
    {"id": "timeline_4", "title": "More than 6 months"}
]


def status_question(first=False):
    intro = (
        "Welcome to HYTOMA Automation! 👋\n\n"
        "Thank you for contacting us.\n\n"
        if first else ""
    )

    return list_reply(
        intro + "What is the current status of your project?",
        "Select Status",
        STATUS_OPTIONS
    )


def timeline_question():
    return list_reply(
        "When do you expect your construction "
        "or interior work to be completed?",
        "Select Timeline",
        TIMELINE_OPTIONS
    )


def location_question():
    return text_reply(
        "Could you please share your project location?\n\n"
        "Please mention your city and area."
    )


def visit_question():
    return text_reply(
        "When would you prefer a site visit "
        "from our HYTOMA team?\n\n"
        "Please share your preferred date or day. "
        "Our team will confirm availability."
    )


RETURNING_CUSTOMER_MESSAGE = (
    "Welcome back to HYTOMA Automation! 👋\n\n"
    "Thank you for reaching out again. "
    "Our team will review your message "
    "and assist you shortly."
)



FINAL_MESSAGE = (
    "Thank you for sharing your details with "
    "HYTOMA Automation! ✅\n\n"
    "Our team will review your project information "
    "and contact you to discuss the next steps.\n\n"
    "Your site visit is not yet confirmed."
)

LATER_MESSAGE = (
    "Thank you for sharing your project details "
    "with HYTOMA Automation! 😊\n\n"
    "Since your project is still in progress, "
    "you can contact us when you're closer to "
    "completing the work.\n\n"
    "We'll be happy to assist you with your "
    "automation requirements."
)

HUMAN_MESSAGE = (
    "Sure! Our HYTOMA team will review "
    "your enquiry and assist you."
)


STATUS_MAP = {
    "status_planning": "planning",
    "1": "planning",
    "planning": "planning",

    "status_construction": "construction",
    "2": "construction",
    "under construction": "construction",
    "construction": "construction",

    "status_interiors": "interiors",
    "3": "interiors",
    "interiors": "interiors",
    "interiors in progress": "interiors",
    "interior work": "interiors",

    "status_completed": "completed",
    "4": "completed",
    "completed": "completed"
}

TIMELINE_MAP = {
    "timeline_1": "Within 1 month",
    "1": "Within 1 month",
    "within 1 month": "Within 1 month",

    "timeline_2": "1-3 months",
    "2": "1-3 months",
    "1-3 months": "1-3 months",
    "1–3 months": "1-3 months",

    "timeline_3": "3-6 months",
    "3": "3-6 months",
    "3-6 months": "3-6 months",
    "3–6 months": "3-6 months",

    "timeline_4": "More than 6 months",
    "4": "More than 6 months",
    "more than 6 months": "More than 6 months"
}


def decide_next(lead, user_text):
    """
    Returns:
        updated_lead, response

    response is a dict containing:
        type: text or list
        body: message content
        button/rows: list message options
    """

    # New conversation
    if lead is None:
        return {
            "step": "status",
            "status": "screening",
            "project_status": None,
            "completion_timeline": None,
            "project_location": None,
            "preferred_site_visit": None
        }, status_question(first=True)

    data = dict(lead)

    # Completed/deferred/human conversations
    # should not restart automatically.
    if data["status"] != "screening":
        return data, None

    answer = (user_text or "").strip()
    normalized = answer.casefold()

    # Customer opt-out
    if normalized in {
        "stop",
        "unsubscribe",
        "opt out"
    }:
        data["step"] = "closed"
        data["status"] = "opted_out"
        return data, None

    # Human requested
    if normalized in {
        "human",
        "agent",
        "salesperson",
        "talk to person"
    }:
        data["step"] = "closed"
        data["status"] = "human"
        return data, text_reply(HUMAN_MESSAGE)

    step = data["step"]

    # Project status
    if step == "status":

        value = STATUS_MAP.get(normalized)

        if not value:
            return data, status_question()

        data["project_status"] = value

        
        if value in {"construction", "interiors"}:
            data["step"] = "timeline"
            return data, timeline_question()

        if value == "planning":
            data["step"] = "planning_location"
            return data, location_question()

# Completed projects
        data["step"] = "location"
        return data, location_question()


    # Expected completion timeline
    if step == "timeline":

        value = TIMELINE_MAP.get(normalized)

        if not value:
            return data, timeline_question()

        data["completion_timeline"] = value

        # If work takes more than one month,
        # don't ask location or site-visit questions.
        if value != "Within 1 month":
            data["step"] = "closed"
            data["status"] = "deferred"

            return data, text_reply(LATER_MESSAGE)

        data["step"] = "location"
        return data, location_question()

    
# Planning project — collect location only
    if step == "planning_location":

        if not 3 <= len(answer) <= 200:
            return data, text_reply(
                "Please share your city and area.\n"
                "Example: KPHB, Hyderabad."
            )

        data["project_location"] = answer
        data["step"] = "closed"
        data["status"] = "deferred"

        return data, text_reply(
            "Thank you for sharing your project details "
            "with HYTOMA Automation! 😊\n\n"
            "Since your project is currently in the "
            "planning stage, our team will be happy "
            "to assist you when you're ready to "
            "proceed with automation.\n\n"
            "Feel free to contact us anytime!"
        )


    # Project location
    if step == "location":

        if not 3 <= len(answer) <= 200:
            return data, text_reply(
                "Please share your city and area.\n"
                "Example: KPHB, Hyderabad."
            )

        data["project_location"] = answer
        data["step"] = "visit"

        return data, visit_question()

    # Preferred site visit
    if step == "visit":

        if not 2 <= len(answer) <= 200:
            return data, visit_question()

        data["preferred_site_visit"] = answer

        data["step"] = "closed"
        data["status"] = "completed"

        return data, text_reply(FINAL_MESSAGE)

    return data, None
