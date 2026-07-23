from src.api.exception.errors import ApiError


def validate_page(page: int, page_size: int) -> None:
    if page <= 0 or page_size <= 0 or page_size > 100:
        raise ApiError(
            422,
            "invalid_pagination",
            "page and page_size must be valid positive integers",
            {"page": page, "page_size": page_size},
        )


def normalize_string_filter(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def normalize_allowed_filter(
    value: str | None,
    *,
    field_name: str,
    allowed_values: set[str],
) -> str | None:
    normalized = normalize_string_filter(value)
    if normalized is None:
        return None
    normalized = normalized.lower()
    if normalized not in allowed_values:
        raise ApiError(
            422,
            "invalid_activity_filter",
            f"{field_name} is invalid",
            {"field_name": field_name, "value": value, "allowed_values": sorted(allowed_values)},
        )
    return normalized
