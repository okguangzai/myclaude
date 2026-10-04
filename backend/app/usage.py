"""Token/cost accounting. Historical source prices are never presented as current bills."""

FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def pricing_for_model(model):
    normalized = model.lower()
    if "haiku" in normalized:
        rates = (1.0, 5.0, 1.25, 0.1)
    elif "sonnet" in normalized or "opus" in normalized:
        rates = (15.0, 75.0, 18.75, 1.5)
    else:
        return None
    return dict(zip(FIELDS, rates))


def estimate(usage, rates):
    costs = {field: usage.get(field, 0) * rates.get(field, 0) / 1_000_000 for field in FIELDS}
    return {"costs_usd": costs, "total_cost_usd": sum(costs.values())}


def summarize(messages, model=None, rates=None, source_compatibility=False):
    turns = [message["usage"] for message in messages if message.get("usage") is not None]
    cumulative = {field: sum(u.get(field, 0) for u in turns) for field in FIELDS}
    result = {
        "turn_count": len(turns),
        "last_turn": turns[-1] if turns else dict.fromkeys(FIELDS, 0),
        "cumulative": cumulative,
        "total_tokens": sum(cumulative.values()),
        "estimated_cost": None,
    }
    if rates is not None:
        result.update(estimated_cost=estimate(cumulative, rates), pricing_source="configured")
    elif source_compatibility:
        rates = pricing_for_model(model or "") or dict(zip(FIELDS, (15.0, 75.0, 18.75, 1.5)))
        result.update(
            estimated_cost=estimate(cumulative, rates),
            pricing_source="frozen-upstream-08106b0; not current pricing",
        )
    return result
