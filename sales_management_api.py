
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

from auth import require_admin
from db import db_connect


router = APIRouter(
    prefix="/admin",
    tags=["Sales Team and Lead Editing"],
    dependencies=[Depends(require_admin)],
)


class SalespersonCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class SalespersonUpdate(BaseModel):
    name: Optional[str] = Field(
        default=None, min_length=1, max_length=100
    )
    is_active: Optional[bool] = None


class AssignSalesperson(BaseModel):
    salesperson_id: int = Field(gt=0)


class LeadDetailsUpdate(BaseModel):
    project_status: Optional[str] = Field(
        default=None, max_length=100
    )
    completion_timeline: Optional[str] = Field(
        default=None, max_length=100
    )
    project_location: Optional[str] = Field(
        default=None, max_length=255
    )
    preferred_site_visit: Optional[str] = Field(
        default=None, max_length=100
    )


@router.get("/salespeople")
def list_salespeople():
    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT id, name, is_active, created_at
                FROM salespeople
                ORDER BY is_active DESC, name ASC
            """)

            return {"salespeople": cur.fetchall()}


@router.post("/salespeople", status_code=201)
def create_salesperson(payload: SalespersonCreate):
    name = payload.name.strip()

    if not name:
        raise HTTPException(
            status_code=422,
            detail="Salesperson name cannot be empty"
        )

    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                INSERT INTO salespeople (name)
                VALUES (%s)
                ON CONFLICT DO NOTHING
                RETURNING id, name, is_active
            """, (name,))

            result = cur.fetchone()

            if result is None:
                raise HTTPException(
                    status_code=409,
                    detail="Salesperson already exists"
                )

            return result


@router.patch("/salespeople/{salesperson_id}")
def update_salesperson(
    salesperson_id: int,
    payload: SalespersonUpdate,
):
    changes = payload.model_dump(exclude_unset=True)

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="No changes provided"
        )

    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT id, name, is_active
                FROM salespeople
                WHERE id = %s
                FOR UPDATE
            """, (salesperson_id,))

            current = cur.fetchone()

            if not current:
                raise HTTPException(
                    status_code=404,
                    detail="Salesperson not found"
                )

            new_name = current["name"]

            if "name" in changes:
                if changes["name"] is None:
                    raise HTTPException(
                        status_code=422,
                        detail="Name cannot be null"
                    )

                new_name = changes["name"].strip()

                if not new_name:
                    raise HTTPException(
                        status_code=422,
                        detail="Name cannot be empty"
                    )

                cur.execute("""
                    SELECT id
                    FROM salespeople
                    WHERE LOWER(name) = LOWER(%s)
                      AND id <> %s
                """, (new_name, salesperson_id))

                if cur.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail="Salesperson name already exists"
                    )

            active = changes.get(
                "is_active", current["is_active"]
            )

            if active is None:
                raise HTTPException(
                    status_code=422,
                    detail="is_active cannot be null"
                )

            cur.execute("""
                UPDATE salespeople
                SET name = %s,
                    is_active = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id, name, is_active
            """, (new_name, active, salesperson_id))

            result = cur.fetchone()

            # Existing lead assignments currently store
            # the salesperson's name, so keep them in sync.
            if new_name != current["name"]:
                cur.execute("""
                    UPDATE leads
                    SET assigned_salesperson = %s
                    WHERE assigned_salesperson = %s
                """, (new_name, current["name"]))

            return result


@router.patch("/leads/{phone}/salesperson")
def assign_lead_salesperson(
    phone: str,
    payload: AssignSalesperson,
):
    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT name
                FROM salespeople
                WHERE id = %s AND is_active = TRUE
            """, (payload.salesperson_id,))

            person = cur.fetchone()

            if person is None:
                raise HTTPException(
                    status_code=422,
                    detail="Select an active salesperson"
                )

            cur.execute("""
                UPDATE leads
                SET assigned_salesperson = %s,
                    sales_status = 'assigned'
                WHERE phone = %s
                RETURNING phone, assigned_salesperson,
                          sales_status
            """, (person["name"], phone))

            result = cur.fetchone()

            if result is None:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found"
                )

            return {
                "status": "updated",
                "lead": result,
            }


@router.patch("/leads/{phone}/details")
def edit_lead_details(
    phone: str,
    payload: LeadDetailsUpdate,
):
    changes = payload.model_dump(exclude_unset=True)

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="No fields provided"
        )

    allowed = {
        "project_status",
        "completion_timeline",
        "project_location",
        "preferred_site_visit",
    }

    # Column names come only from our allowlist,
    # not from user-provided arbitrary strings.
    fields = [
        key for key in changes
        if key in allowed
    ]

    if not fields:
        raise HTTPException(
            status_code=422,
            detail="No editable fields provided"
        )

    values = []

    for field in fields:
        value = changes[field]

        if isinstance(value, str):
            value = value.strip() or None

        values.append(value)

    assignments = ", ".join(
        f"{field} = %s" for field in fields
    )

    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                UPDATE leads
                SET {assignments}
                WHERE phone = %s
                RETURNING
                    phone,
                    project_status,
                    completion_timeline,
                    project_location,
                    preferred_site_visit,
                    status
                """,
                (*values, phone),
            )

            result = cur.fetchone()

            if result is None:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found"
                )

            return {
                "status": "updated",
                "lead": result,
            }
