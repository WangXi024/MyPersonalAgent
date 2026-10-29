"""Bounded presentation proposals; business commands remain separate."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

ShortText = Annotated[str, Field(min_length=1, max_length=500)]


class ChecklistDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["checklist"]
    title: str = Field(min_length=1, max_length=160)
    items: list[ShortText] = Field(min_length=1, max_length=10)


class ResourceDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["resource_list"]
    title: str = Field(min_length=1, max_length=160)
    source_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=8)


DisplayProposal = Annotated[ChecklistDisplay | ResourceDisplay, Field(discriminator="type")]
DISPLAY_PROPOSALS = TypeAdapter(list[DisplayProposal])


def build_ui_components(proposals: list, sources: list[dict]) -> list[dict]:
    """Resolve source IDs only against this operation's actual research results."""
    from app.domain.common import DomainError

    try:
        validated = DISPLAY_PROPOSALS.validate_python(proposals)
    except ValidationError:
        raise DomainError("invalid_ui", "展示组件的数据格式无效", 422) from None
    if len(validated) > 6:
        raise DomainError("invalid_ui", "展示组件数量超出限制", 422)
    known = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        try:
            url = urlsplit(source.get("url", ""))
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                continue
        except (ValueError, TypeError):
            continue
        if source.get("id"):
            known[source["id"]] = source
    components = []
    for index, proposal in enumerate(validated):
        block = {"type": proposal.type, "id": f"display-{index + 1}", "title": proposal.title}
        if isinstance(proposal, ChecklistDisplay):
            block["items"] = proposal.items
        else:
            selected = list(dict.fromkeys(proposal.source_ids))
            if any(source_id not in known for source_id in selected):
                raise DomainError("invalid_ui_source", "资料组件引用了本次未检索到的来源", 422)
            block["items"] = [{
                "id": source_id,
                "title": str(known[source_id].get("title", "资料"))[:200],
                "url": known[source_id]["url"],
                "snippet": str(known[source_id].get("snippet", ""))[:2000],
                "retrieved_at": str(known[source_id].get("retrieved_at", "")),
            } for source_id in selected]
        components.append(block)
    return components
