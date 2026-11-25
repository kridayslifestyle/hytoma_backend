
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


def decide_next_legacy(lead, user_text):
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


# ============================================================
# HYTOMA QUALIFICATION FLOW — VERSION 2
# Management-approved conversation flow
# ============================================================

PROJECT_TYPE_OPTIONS = [
    {"id": "project_2bhk", "title": "2BHK"},
    {"id": "project_3bhk", "title": "3BHK"},
    {"id": "project_villa", "title": "Villa"},
    {"id": "project_apartment", "title": "Apartment"},
]

AUTOMATION_TYPE_OPTIONS = [
    {"id": "automation_full", "title": "Full Home Automation"},
    {"id": "automation_semi", "title": "Semi Automation"},
    {"id": "automation_basic", "title": "Basic Automation"},
]

PROJECT_STATUS_OPTIONS_V2 = [
    {"id": "status_construction", "title": "Under Construction"},
    {"id": "status_interiors", "title": "Interior Work Going On"},
    {"id": "status_completed", "title": "Completed"},
]

TIMELINE_OPTIONS_V2 = [
    {"id": "timeline_1", "title": "Within 1 Month"},
    {"id": "timeline_2", "title": "1-2 Months"},
    {"id": "timeline_3", "title": "3 Months"},
]


PROJECT_TYPE_MAP = {
    "project_2bhk": "2BHK",
    "2bhk": "2BHK",
    "2 bhk": "2BHK",

    "project_3bhk": "3BHK",
    "3bhk": "3BHK",
    "3 bhk": "3BHK",

    "project_villa": "Villa",
    "villa": "Villa",

    "project_apartment": "Apartment",
    "apartment": "Apartment",
}

AUTOMATION_TYPE_MAP = {
    "automation_full": "Full Home Automation",
    "full home automation": "Full Home Automation",

    "automation_semi": "Semi Automation",
    "semi automation": "Semi Automation",

    "automation_basic": "Basic Automation",
    "basic automation": "Basic Automation",
}

PROJECT_STATUS_MAP_V2 = {
    "status_construction": "construction",
    "under construction": "construction",
    "construction": "construction",

    "status_interiors": "interiors",
    "interior work going on": "interiors",
    "interiors in progress": "interiors",
    "interiors": "interiors",

    "status_completed": "completed",
    "completed": "completed",
}

TIMELINE_MAP_V2 = {
    "timeline_1": "Within 1 Month",
    "within 1 month": "Within 1 Month",
    "1 month": "Within 1 Month",

    "timeline_2": "1-2 Months",
    "1-2 months": "1-2 Months",
    "1–2 months": "1-2 Months",

    "timeline_3": "3 Months",
    "3 months": "3 Months",
}


WELCOME_MESSAGE_V2 = (
    "Welcome to HYTOMA Automation! 👋\n\n"
    "Thank you for contacting us.\n"
    "We appreciate your interest.\n\n"
    "Smart people choose smart automation. 🏡"
)

SMART_CHOICE_MESSAGE = (
    "Great choice! 👏\n"
    "Full home automation is a smart decision."
)

FINAL_VISIT_MESSAGE_V2 = (
    "Thank you for sharing your details "
    "with HYTOMA Automation! ✅\n\n"
    "We have received your site visit request.\n"
    "One of our team members will get in touch "
    "with you to confirm the schedule.\n\n"
    "Thank you for choosing HYTOMA.\n"
    "Have a great day! 😊"
)


def sequence_reply(*responses):
    """
    Multiple outgoing WhatsApp messages, in order.

    IMPORTANT:
    db.py must be updated to support this response
    type before Version 2 is enabled in production.
    """
    return {
        "type": "sequence",
        "messages": list(responses),
    }


def project_type_question():
    return list_reply(
        "What type of project are you planning "
        "for home automation?\n\n"
        "Select one option below.",
        "Select Project",
        PROJECT_TYPE_OPTIONS,
    )


def automation_type_question():
    return list_reply(
        "What type of automation are you "
        "looking for?\n\n"
        "Select one option below.",
        "Select Automation",
        AUTOMATION_TYPE_OPTIONS,
    )


def project_status_question_v2():
    return list_reply(
        "What is the current status of your project?\n\n"
        "Select one option below.",
        "Select Status",
        PROJECT_STATUS_OPTIONS_V2,
    )


def completion_question_v2():
    return list_reply(
        "When do you expect your construction "
        "or interior work to be completed?\n\n"
        "Select one option below.",
        "Select Timeline",
        TIMELINE_OPTIONS_V2,
    )


def project_location_question_v2():
    return text_reply(
        "Could you please share your project location?\n\n"
        "Please mention your city and area."
    )


def site_visit_question_v2():
    return text_reply(
        "When would you prefer a site visit "
        "from our HYTOMA team?\n\n"
        "Please share your preferred date "
        "or day of the week.\n"
        "Our team will confirm availability."
    )


def deferred_message_v2(timeline):
    if timeline == "1-2 Months":
        duration = "another 1–2 months"
    else:
        duration = "around 3 months"

    return text_reply(
        "Thank you for sharing your details "
        "with HYTOMA Automation! 😊\n\n"
        f"We understand that your project may take "
        f"{duration} to complete.\n\n"
        "Once your project is ready, please let us know. "
        "Our team will be happy to arrange a site visit "
        "and discuss your automation requirements.\n\n"
        "Thank you for choosing HYTOMA!"
    )


def decide_next_v2(lead, user_text):
    """
    New management-approved qualification flow.

    Returns:
        updated_lead, response

    Responses may be:
        text
        list
        sequence
        None
    """

    # --------------------------------------------------------
    # New customer: separate welcome and first question
    # --------------------------------------------------------
    if lead is None:
        return {
            "flow_version": 2,
            "step": "v2_project_type",
            "status": "screening",
            "project_type": None,
            "automation_type": None,
            "project_status": None,
            "completion_timeline": None,
            "project_location": None,
            "preferred_site_visit": None,
        }, sequence_reply(
            text_reply(WELCOME_MESSAGE_V2),
            project_type_question(),
        )

    data = dict(lead)

    if data.get("status") != "screening":
        return data, None

    answer = (user_text or "").strip()
    normalized = answer.casefold()

    # Customer requests to stop messages.
    if normalized in {
        "stop",
        "unsubscribe",
        "opt out",
    }:
        data["step"] = "closed"
        data["status"] = "opted_out"
        return data, None

    # Customer requests a human representative.
    if normalized in {
        "human",
        "agent",
        "salesperson",
        "talk to person",
    }:
        data["step"] = "closed"
        data["status"] = "human"
        return data, text_reply(HUMAN_MESSAGE)

    step = data.get("step")

    # --------------------------------------------------------
    # Q1: Project Type
    # --------------------------------------------------------
    if step == "v2_project_type":

        value = PROJECT_TYPE_MAP.get(normalized)

        if not value:
            return data, project_type_question()

        data["project_type"] = value
        data["step"] = "v2_automation_type"

        return data, automation_type_question()

    # --------------------------------------------------------
    # Q2: Automation Requirement
    # --------------------------------------------------------
    if step == "v2_automation_type":

        value = AUTOMATION_TYPE_MAP.get(normalized)

        if not value:
            return data, automation_type_question()

        data["automation_type"] = value
        data["step"] = "v2_project_status"

        if value == "Full Home Automation":
            return data, sequence_reply(
                text_reply(SMART_CHOICE_MESSAGE),
                project_status_question_v2(),
            )

        return data, project_status_question_v2()

    # --------------------------------------------------------
    # Q3: Current Project Status
    # --------------------------------------------------------
    if step == "v2_project_status":

        value = PROJECT_STATUS_MAP_V2.get(normalized)

        if not value:
            return data, project_status_question_v2()

        data["project_status"] = value

        if value in {"construction", "interiors"}:
            data["step"] = "v2_timeline"
            return data, completion_question_v2()

        # Completed project: skip timeline.
        data["step"] = "v2_location"
        return data, project_location_question_v2()

    # --------------------------------------------------------
    # Q4: Expected Project Completion
    # --------------------------------------------------------
    if step == "v2_timeline":

        value = TIMELINE_MAP_V2.get(normalized)

        if not value:
            return data, completion_question_v2()

        data["completion_timeline"] = value

        if value in {"1-2 Months", "3 Months"}:
            data["step"] = "closed"
            data["status"] = "deferred"

            return data, deferred_message_v2(value)

        data["step"] = "v2_location"
        return data, project_location_question_v2()

    # --------------------------------------------------------
    # Q5: Project Location
    # --------------------------------------------------------
    if step == "v2_location":

        if not 3 <= len(answer) <= 200:
            return data, text_reply(
                "Please share your city and area.\n"
                "Example: KPHB, Hyderabad."
            )

        data["project_location"] = answer
        data["step"] = "v2_visit"

        return data, site_visit_question_v2()

    # --------------------------------------------------------
    # Q6: Preferred Site Visit
    # --------------------------------------------------------
    if step == "v2_visit":

        if not 2 <= len(answer) <= 200:
            return data, site_visit_question_v2()

        data["preferred_site_visit"] = answer
        data["step"] = "closed"
        data["status"] = "completed"

        return data, text_reply(FINAL_VISIT_MESSAGE_V2)

    return data, None


# ============================================================
# VERSION-AWARE ENTRY POINT
# Existing db.py can continue importing decide_next.
# ============================================================

def decide_next(lead, user_text):
    """
    Route each customer to the correct flow version.

    Existing leads: Version 1
    New leads: Version 2
    """

    if lead is None:
        return decide_next_v2(None, user_text)

    version = lead.get("flow_version", 1)

    if version is not None and int(version) >= 2:
        return decide_next_v2(lead, user_text)

    return decide_next_legacy(lead, user_text)
