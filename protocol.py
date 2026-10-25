
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any


PROPOSAL_MEDIA_TYPE = (
    "application/vnd.ga5.invoice-action-proposals+json"
)
RECEIPT_MEDIA_TYPE = (
    "application/vnd.ga5.invoice-action-receipts+json"
)
BATCH_MEDIA_TYPE = (
    "application/vnd.ga5.invoice-claim-batch+json"
)
RESULT_MEDIA_TYPE = (
    "application/vnd.ga5.invoice-action-results+json"
)


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id():
    return str(uuid.uuid4())


def compact_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_message_hash(message: dict) -> str:
    return hashlib.sha256(
        compact_json(message).encode("utf-8")
    ).hexdigest()


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        compact_json(value).encode("utf-8")
    ).hexdigest()


def make_artifact(name: str, media_type: str, data: dict) -> dict:
    return {
        "artifactId": new_id(),
        "name": name,
        "parts": [
            {
                "mediaType": media_type,
                "data": data,
            }
        ],
    }


def make_task(
    task_id: str,
    context_id: str,
    initial_message: dict,
    batch_id: str,
    proposals: dict,
) -> dict:
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": "TASK_STATE_INPUT_REQUIRED",
            "timestamp": now_iso(),
        },
        "history": [initial_message],
        "artifacts": [
            make_artifact(
                "invoice-action-proposals",
                PROPOSAL_MEDIA_TYPE,
                proposals,
            )
        ],
        "metadata": {
            "batchId": batch_id,
        },
    }


def append_history(task: dict, message: dict) -> dict:
    task.setdefault("history", []).append(message)
    return task


def set_task_state(task: dict, state: str) -> dict:
    task["status"] = {
        "state": state,
        "timestamp": now_iso(),
    }
    return task
