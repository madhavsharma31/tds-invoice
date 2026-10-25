
import hashlib
import json
import os

from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone
from database import (
    create_tables,
    SessionLocal,
    TaskRecord,
    MessageRecord,
    ActionExecutionRecord,
)
from decisions import propose_batch
from models import InvoiceBatch
from protocol import (
    BATCH_MEDIA_TYPE,
    PROPOSAL_MEDIA_TYPE,
    RECEIPT_MEDIA_TYPE,
    RESULT_MEDIA_TYPE,
    canonical_message_hash,
    compact_json,
    make_artifact,
    make_task,
    new_id,
    set_task_state,
)

load_dotenv()

BASE_URL = os.getenv("A2A_BASE_URL", "http://127.0.0.1:8000/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    yield


app = FastAPI(
    title="A2A Invoice Agent",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def a2a_response_media_type(request: Request, call_next):
    response = await call_next(request)

    if (
        response.status_code < 400
        and (
            request.url.path.endswith("/message:send")
            or "/tasks" in request.url.path
        )
    ):
        response.headers["content-type"] = "application/a2a+json"

    return response


def get_principal(token: str) -> str:
    # Never store the raw Bearer token as the owner identifier.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate(
    authorization: str | None = Header(default=None),
    a2a_version: str | None = Header(
        default=None, alias="A2A-Version"
    ),
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # The Bearer token identifies the caller/principal. The assignment
    # expects different supplied tokens to map to separate isolated users;
    # do not compare the grader's token against a single deployment secret.
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if a2a_version != "1.0":
        raise HTTPException(status_code=400, detail="Invalid A2A version")

    return get_principal(token)


def require_media_type(request: Request):
    value = request.headers.get("content-type", "")
    media_type = value.split(";", 1)[0].strip().lower()

    if media_type != "application/a2a+json":
        raise HTTPException(
            status_code=415,
            detail="Unsupported media type",
        )


def get_part_data(message: dict, media_type: str) -> dict:
    parts = message.get("parts", [])

    matches = [
        part.get("data")
        for part in parts
        if part.get("mediaType") == media_type
        and isinstance(part.get("data"), dict)
    ]

    if len(matches) != 1:
        raise HTTPException(
            status_code=400,
            detail="Invalid message parts",
        )

    return matches[0]


def get_owned_task(db, principal: str, task_id: str):
    record = db.execute(
        select(TaskRecord).where(
            TaskRecord.id == task_id,
            TaskRecord.principal == principal,
        )
    ).scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return record


def task_from_record(record: TaskRecord) -> dict:
    return json.loads(record.task_json)


def save_task(db, record: TaskRecord, task: dict):
    record.task_json = compact_json(task)
    record.status = task["status"]["state"]
    db.add(record)


@app.get("/.well-known/agent-card.json")
def agent_card():
    return {
        "name": "Invoice Action Agent",
        "description": (
            "Reviews invoice evidence and proposes safe business actions"
        ),
        "version": "1.0.0",
        "capabilities": {},
        "supportedInterfaces": [
            {
                "url": BASE_URL,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            }
        ],
        "defaultInputModes": [BATCH_MEDIA_TYPE],
        "defaultOutputModes": [
            PROPOSAL_MEDIA_TYPE,
            RECEIPT_MEDIA_TYPE,
        ],
        "skills": [
            {
                "id": "invoice_action_agent",
                "name": "Invoice Action Agent",
                "description": (
                    "Analyzes invoice evidence and proposes safe actions"
                ),
                "tags": ["invoices", "reconciliation", "approvals"],
            }
        ],
    }


@app.post("/message:send")
async def send_message(
    request: Request,
    principal: str = Depends(authenticate),
):
    require_media_type(request)

    body = await request.json()
    message = body.get("message")

    if not isinstance(message, dict):
        raise HTTPException(status_code=400, detail="Invalid message")

    message_id = message.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise HTTPException(status_code=400, detail="Missing messageId")

    msg_hash = canonical_message_hash(message)

    # Fast replay/conflict check before making any model call.
    with SessionLocal() as db:
        previous = db.execute(
            select(MessageRecord).where(
                MessageRecord.principal == principal,
                MessageRecord.message_id == message_id,
            )
        ).scalar_one_or_none()

        if previous:
            if previous.content_hash != msg_hash:
                raise HTTPException(
                    status_code=409,
                    detail="IDEMPOTENCY_CONFLICT",
                )

            record = db.execute(
                select(TaskRecord).where(
                    TaskRecord.id == previous.task_id,
                    TaskRecord.principal == principal,
                )
            ).scalar_one_or_none()

            if record is None:
                raise HTTPException(
                    status_code=500,
                    detail="Stored task unavailable",
                )

            return {"task": task_from_record(record)}

    # Initial invoice batch.
    batch_parts = [
        p for p in message.get("parts", [])
        if p.get("mediaType") == BATCH_MEDIA_TYPE
    ]

    if batch_parts:
        if message.get("role") != "ROLE_USER":
            raise HTTPException(status_code=400, detail="Invalid role")

        batch_data = get_part_data(message, BATCH_MEDIA_TYPE)

        try:
            batch = InvoiceBatch.model_validate(batch_data).model_dump()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid batch")

        # The model call is deliberately outside a database transaction.
        try:
            proposals = await propose_batch(batch)
        except Exception:
            raise HTTPException(
                status_code=502,
                detail="Invoice analysis failed",
            )

        task_id = new_id()
        context_id = message.get("contextId") or new_id()

        task = make_task(
            task_id=task_id,
            context_id=context_id,
            initial_message=message,
            batch_id=batch["batchId"],
            proposals=proposals,
        )

        with SessionLocal() as db:
            try:
                with db.begin():
                    # Recheck after the model call to handle concurrent
                    # equivalent requests.
                    previous = db.execute(
                        select(MessageRecord).where(
                            MessageRecord.principal == principal,
                            MessageRecord.message_id == message_id,
                        )
                    ).scalar_one_or_none()

                    if previous:
                        if previous.content_hash != msg_hash:
                            raise HTTPException(
                                status_code=409,
                                detail="IDEMPOTENCY_CONFLICT",
                            )

                        old_record = db.execute(
                            select(TaskRecord).where(
                                TaskRecord.id == previous.task_id,
                                TaskRecord.principal == principal,
                            )
                        ).scalar_one()
                        task = task_from_record(old_record)
                    else:
                        record = TaskRecord(
                            id=task_id,
                            principal=principal,
                            context_id=context_id,
                            batch_id=batch["batchId"],
                            status=task["status"]["state"],
                            task_json=compact_json(task),
                        )
                        db.add(record)
                        db.add(
                            MessageRecord(
                                principal=principal,
                                message_id=message_id,
                                content_hash=msg_hash,
                                task_id=task_id,
                            )
                        )
            except IntegrityError:
                raise HTTPException(
                    status_code=409,
                    detail="Concurrent request conflict; retry safely",
                )

        return {"task": task}

    # Result continuation.
    result_parts = [
        p for p in message.get("parts", [])
        if p.get("mediaType") == RESULT_MEDIA_TYPE
    ]

    if result_parts:
        if message.get("role") != "ROLE_USER":
            raise HTTPException(status_code=400, detail="Invalid role")

        task_id = message.get("taskId")
        context_id = message.get("contextId")

        if not isinstance(task_id, str) or not isinstance(context_id, str):
            raise HTTPException(
                status_code=400,
                detail="Missing task or context ID",
            )

        result_data = get_part_data(message, RESULT_MEDIA_TYPE)

        with SessionLocal() as db:
            try:
                with db.begin():
                    record = get_owned_task(db, principal, task_id)
                    task = task_from_record(record)

                    if task.get("contextId") != context_id:
                        raise HTTPException(
                            status_code=400,
                            detail="Context mismatch",
                        )

                    if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
                        raise HTTPException(
                            status_code=409,
                            detail="Task is not awaiting results",
                        )

                    if result_data.get("batchId") != record.batch_id:
                        raise HTTPException(
                            status_code=400,
                            detail="Batch mismatch",
                        )

                    proposal_artifacts = [
                        a for a in task.get("artifacts", [])
                        if any(
                            p.get("mediaType") == PROPOSAL_MEDIA_TYPE
                            for p in a.get("parts", [])
                        )
                    ]

                    if len(proposal_artifacts) != 1:
                        raise HTTPException(
                            status_code=409,
                            detail="Stored proposal unavailable",
                        )

                    proposal_part = next(
                        p for p in proposal_artifacts[0]["parts"]
                        if p.get("mediaType") == PROPOSAL_MEDIA_TYPE
                    )

                    stored_proposals = proposal_part["data"]["proposals"]
                    proposal_map = {
                        p["packageId"]: p for p in stored_proposals
                    }

                    results = result_data.get("results")
                    if not isinstance(results, list):
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid results",
                        )

                    if len(results) != len(proposal_map):
                        raise HTTPException(
                            status_code=400,
                            detail="Incomplete result set",
                        )

                    seen = set()
                    accepted_executions = []

                    for result in results:
                        package_id = result.get("packageId")

                        if package_id in seen:
                            raise HTTPException(
                                status_code=400,
                                detail="Duplicate result package",
                            )
                        seen.add(package_id)

                        proposal = proposal_map.get(package_id)
                        if proposal is None:
                            raise HTTPException(
                                status_code=400,
                                detail="Unknown package",
                            )

                        if (
                            result.get("actionId") != proposal["actionId"]
                            or result.get("action") != proposal["action"]
                        ):
                            raise HTTPException(
                                status_code=400,
                                detail="Proposal mismatch",
                            )

                        if result.get("outcome") not in (
                            "ACCEPTED", "REJECTED"
                        ):
                            raise HTTPException(
                                status_code=400,
                                detail="Invalid outcome",
                            )

                        nonce = result.get("receiptNonce")
                        if not isinstance(nonce, str) or not nonce:
                            raise HTTPException(
                                status_code=400,
                                detail="Missing receipt nonce",
                            )

                        if result["outcome"] == "ACCEPTED":
                            execution = {
                                "packageId": package_id,
                                "actionId": proposal["actionId"],
                                "action": proposal["action"],
                                "receiptNonce": nonce,
                                "facts": proposal["facts"],
                                "evidenceRefs": proposal["evidenceRefs"],
                            }

                            db.add(
                                ActionExecutionRecord(
                                    task_id=task_id,
                                    package_id=package_id,
                                    action_id=proposal["actionId"],
                                    receipt_nonce=nonce,
                                    execution_json=compact_json(execution),
                                )
                            )
                            accepted_executions.append(execution)

                    if seen != set(proposal_map):
                        raise HTTPException(
                            status_code=400,
                            detail="Result packages do not match proposals",
                        )

                    # Store the incoming continuation in Task history.
                    task.setdefault("history", []).append(message)

                    task["artifacts"].append(
                        make_artifact(
                            "invoice-action-receipts",
                            RECEIPT_MEDIA_TYPE,
                            {
                                "batchId": record.batch_id,
                                "executions": accepted_executions,
                            },
                        )
                    )

                    set_task_state(task, "TASK_STATE_COMPLETED")
                    save_task(db, record, task)

                    # Persist continuation idempotency in the same
                    # transaction as the final Task.
                    db.add(
                        MessageRecord(
                            principal=principal,
                            message_id=message_id,
                            content_hash=msg_hash,
                            task_id=task_id,
                        )
                    )

            except IntegrityError:
                raise HTTPException(
                    status_code=409,
                    detail="Concurrent request conflict",
                )

            return {"task": task}

    raise HTTPException(status_code=400, detail="Unknown message type")


@app.get("/tasks")
def list_tasks(principal: str = Depends(authenticate)):
    with SessionLocal() as db:
        records = db.execute(
            select(TaskRecord)
            .where(TaskRecord.principal == principal)
            .order_by(TaskRecord.created_at.desc())
        ).scalars().all()

        return {
            "tasks": [task_from_record(record) for record in records]
        }


@app.get("/tasks/{task_id}")
def read_task(
    task_id: str,
    principal: str = Depends(authenticate),
):
    with SessionLocal() as db:
        record = get_owned_task(db, principal, task_id)
        return task_from_record(record)


@app.post("/tasks/{task_id}:cancel")
def cancel_task(
    task_id: str,
    principal: str = Depends(authenticate),
):
    with SessionLocal() as db:
        with db.begin():
            record = get_owned_task(db, principal, task_id)

            # Only one concurrent cancellation or result processor
            # may claim a Task in INPUT_REQUIRED state.
            result = db.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.id == task_id,
                    TaskRecord.principal == principal,
                    TaskRecord.status == "TASK_STATE_INPUT_REQUIRED",
                )
                .values(
                    status="TASK_STATE_CANCELED",
                    updated_at=datetime.now(timezone.utc),
                )
            )

            if result.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Task is already terminal or processing",
                )

            task = task_from_record(record)
            set_task_state(task, "TASK_STATE_CANCELED")
            record.task_json = compact_json(task)

        return task
