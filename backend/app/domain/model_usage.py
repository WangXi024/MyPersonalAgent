"""Public usage is an estimate from recorded responses, never a provider bill."""
from decimal import Decimal


def public_usage(op):
    details = op.usage_details or {}
    reports = details.get("reported_requests")
    complete = op.model_requests == 0 or (reports is not None and len(reports) == op.model_requests)
    pricing = details.get("pricing")
    cost = None
    if op.model_requests == 0:
        cost = "0.000000"
    elif pricing and reports:
        cost = str(((Decimal(pricing["input_cny_per_million"]) * op.input_tokens
                     + Decimal(pricing["output_cny_per_million"]) * op.output_tokens)
                    / 1000000).quantize(Decimal("0.000001")))
    return {
        "model_name": details.get("model_name"),
        "model_id": details.get("model_id") or op.versions.get("model_alias"),
        "provider": details.get("provider"),
        "requests": op.model_requests,
        "input_tokens": op.input_tokens,
        "output_tokens": op.output_tokens,
        "total_tokens": op.input_tokens + op.output_tokens,
        "usage_complete": complete,
        "estimated_cost_cny": cost,
        "cost_complete": complete and cost is not None,
        "pricing_known": pricing is not None,
    }


def summarize_usage(operations):
    items = [public_usage(op) for op in operations]
    attempted = [item for item in items if item["requests"] > 0]
    known = [Decimal(item["estimated_cost_cny"]) for item in attempted if item["estimated_cost_cny"] is not None]
    return {
        **{key: sum(item[key] for item in items)
           for key in ("requests", "input_tokens", "output_tokens", "total_tokens")},
        "usage_complete": all(item["usage_complete"] for item in items),
        "cost_complete": all(item["cost_complete"] for item in items),
        "pricing_known": all(item["pricing_known"] or item["requests"] == 0 for item in items),
        "estimated_cost_cny": str(sum(known, Decimal("0")).quantize(Decimal("0.000001")))
        if known or not attempted else None,
    }
