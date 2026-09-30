from __future__ import annotations

from typing import Literal

DemoIntent = Literal["create_csv", "optional_phone", "add_tsv", "unsupported"]


def classify_demo_intent(text: str) -> DemoIntent:
    normalized = text.strip().casefold()
    if not normalized:
        return "unsupported"
    if "tsv" in normalized or "制表符" in normalized:
        return "add_tsv"
    if (
        "closure-sample-v1" in normalized
        or "csv" in normalized
        or "批量导入" in normalized
        or "导入" in normalized
    ):
        return "create_csv"
    if "手机号" in normalized or "phone" in normalized:
        return "optional_phone"
    return "unsupported"
