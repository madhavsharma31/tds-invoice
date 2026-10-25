
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ActionType = Literal[
    "settle_invoice",
    "request_approval",
    "hold_invoice",
    "reject_duplicate",
    "open_exception",
]


class InvoicePackage(BaseModel):
    model_config = ConfigDict(extra="allow")

    packageId: str = Field(min_length=1)
    documents: Any = None


class InvoiceBatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    batchId: str = Field(min_length=1)
    policyRevision: str = Field(min_length=1)
    packages: list[dict[str, Any]] = Field(min_length=1)


class InvoiceFacts(BaseModel):
    vendorName: str = Field(min_length=1)
    invoiceNumber: str = Field(min_length=1)
    amountMinor: int = Field(ge=0, strict=True)
    currency: str = Field(min_length=3, max_length=3)


class InvoiceProposal(BaseModel):
    packageId: str = Field(min_length=1)
    actionId: str = Field(min_length=12)
    action: ActionType
    facts: InvoiceFacts
    evidenceRefs: list[str] = Field(min_length=3, max_length=3)
    rationale: str = Field(min_length=60, max_length=1500)

    @model_validator(mode="after")
    def validate_evidence(self):
        if any(not ref.strip() for ref in self.evidenceRefs):
            raise ValueError("Evidence references cannot be empty")

        if len(set(self.evidenceRefs)) != 3:
            raise ValueError("Evidence references must be distinct")

        if not any(
            ref in self.rationale for ref in self.evidenceRefs
        ):
            raise ValueError(
                "Rationale must cite decisive evidence references"
            )

        return self


class ProposalBatch(BaseModel):
    batchId: str
    proposals: list[InvoiceProposal]
