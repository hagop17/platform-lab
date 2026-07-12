from fastapi import APIRouter
from pydantic import BaseModel

from llm_providers import complete
from rag.tpr_rag import answer_repair_question

router = APIRouter()


class RepairQuestion(BaseModel):
    description: str


@router.post("/api/v1/repair-tax-impact")
def repair_tax_impact(payload: RepairQuestion):
    return answer_repair_question(payload.description)


@router.post("/api/v1/repair-tax-impact-no-rag")
def repair_tax_impact_no_rag(payload: RepairQuestion):
    """Same question, sent straight to the LLM with no retrieved context — for comparison."""
    return {"answer": complete(payload.description), "sources": []}
