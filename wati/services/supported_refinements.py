"""Supported OS/version and Samsung model catalog for retrieval refinement validation."""

import re

SUPPORTED_APPLE_IOS = frozenset({"17", "18", "26"})
SUPPORTED_APPLE_IPADOS = frozenset({"17", "18", "26"})
SUPPORTED_OPPO_COLOROS = frozenset(
    {"3.1", "3.2", "5", "5.1", "5.2", "6", "6.1", "7.1", "11.1", "12", "12.1", "13", "14", "15"}
)
SUPPORTED_XIAOMI_HYPEROS = frozenset({"2", "3"})
SUPPORTED_PIXEL_ANDROID = frozenset({"16"})
SUPPORTED_SAMSUNG_MODELS = frozenset(
    {
        "a05",
        "a05s",
        "a06",
        "a06_5g",
        "a07_4g",
        "a07_5g",
        "a15_5g",
        "a16_5g",
        "a17_5g",
        "a26_5g",
        "a35_5g",
        "a36_5g",
        "a55_5g",
        "a56_5g",
        "f05",
        "f06_5g",
        "f07",
        "f15_5g",
        "f16_5g",
        "f17_5g",
        "f36_5g",
        "f55_5g",
        "f56_5g",
        "f70e_5g",
        "m05",
        "m06_5g",
        "m07",
        "m15_5g",
        "m17_5g",
        "m35_5g",
        "m55_5g",
        "m55s_5g",
        "m56_5g",
        "s23",
        "s23_fe",
        "s23_ultra",
        "s24",
        "s24_fe",
        "s24_plus",
        "s24_ultra",
        "s25",
        "s25_fe",
        "s25_plus",
        "s25_ultra",
        "s26",
        "s26_ultra",
        "z_flip5",
        "z_flip6",
        "z_flip7",
        "z_flip7_fe",
        "z_fold5",
        "z_fold6",
        "z_fold7",
    }
)


def _normalize_samsung_model_regex(text: str) -> str:
    lowered = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not lowered:
        return ""

    lowered = re.sub(r"^galaxy\s+", "", lowered)
    lowered = re.sub(r"\bgalaxy\s+", " ", lowered).strip()

    m = re.search(r"\bz_(flip|fold)(\d+)(_fe)?\b", lowered)
    if m:
        fe = m.group(3) or ""
        return f"z_{m.group(1)}{m.group(2)}{fe}"

    m = re.search(
        r"\b(?:z\s*)?(?:galaxy\s*)?(flip|fold)\s*(\d+)\s*(fe)?\b",
        lowered,
        flags=re.IGNORECASE,
    )
    if m:
        fe = "_fe" if m.group(3) else ""
        return f"z_{m.group(1).lower()}{m.group(2)}{fe}"

    m = re.search(
        r"\b([amsfz])(\d{1,2}[a-z]?)\s*(?:_|\s|-)?\s*(5g|ultra|plus|fe)?\b",
        lowered,
        flags=re.IGNORECASE,
    )
    if m:
        prefix = m.group(1).lower()
        num = m.group(2).lower()
        suffix = (m.group(3) or "").lower()
        base = f"{prefix}{num}"
        if suffix == "5g":
            return f"{base}_5g"
        if suffix == "ultra":
            return f"{base}_ultra"
        if suffix == "plus":
            return f"{base}_plus"
        if suffix == "fe":
            return f"{base}_fe"
        return base

    compact = lowered.replace(" ", "_").replace("-", "_")
    if compact in SUPPORTED_SAMSUNG_MODELS:
        return compact

    for model in SUPPORTED_SAMSUNG_MODELS:
        spaced = model.replace("_", " ")
        if spaced in lowered or model in lowered:
            return model
    return ""


def _regex_extract_apple_version(text: str) -> tuple[str, str]:
    lowered = (text or "").lower()
    m = re.search(r"\bipados\s*(\d+(?:\.\d+)?)\b", lowered)
    if m:
        return "ipados", m.group(1).split(".")[0]
    m = re.search(r"\bios\s*(\d+(?:\.\d+)?)\b", lowered)
    if m:
        return "ios", m.group(1).split(".")[0]
    return "", ""


def _regex_extract_coloros_version(text: str) -> str:
    m = re.search(r"\bcolor\s*os\s*(\d+(?:\.\d+)?)\b", (text or "").lower())
    if m:
        return m.group(1)
    m = re.search(r"\bcoloros\s*(\d+(?:\.\d+)?)\b", (text or "").lower())
    if m:
        return m.group(1)
    return ""


def _regex_extract_hyperos_version(text: str) -> str:
    m = re.search(r"\bhyper\s*os\s*(\d+(?:\.\d+)?)\b", (text or "").lower())
    if m:
        return m.group(1).split(".")[0]
    m = re.search(r"\bhyperos\s*(\d+(?:\.\d+)?)\b", (text or "").lower())
    if m:
        return m.group(1).split(".")[0]
    return ""


def _regex_extract_android_version(text: str) -> str:
    m = re.search(r"\bandroid\s*(\d+(?:\.\d+)?)\b", (text or "").lower())
    if m:
        return m.group(1).split(".")[0]
    return ""


def validate_platform_refinement(
    platform: str,
    *,
    os_family: str = "",
    os_version: str = "",
    device_model: str = "",
) -> dict:
    p = (platform or "").strip().lower()
    if p not in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
        return {"status": "none", "label": "", "refinement_type": ""}

    family = (os_family or "").strip().lower()
    version = (os_version or "").strip()
    model = (device_model or "").strip().lower()

    if p == "apple":
        if not version:
            return {"status": "none", "label": "", "refinement_type": ""}
        if family not in {"ios", "ipados"}:
            family = "ipados" if "ipad" in family else "ios"
        allowed = SUPPORTED_APPLE_IPADOS if family == "ipados" else SUPPORTED_APPLE_IOS
        display = f"iPadOS {version}" if family == "ipados" else f"iOS {version}"
        if version in allowed:
            return {"status": "supported", "label": display, "refinement_type": "version"}
        return {"status": "unsupported", "label": display, "refinement_type": "version"}

    if p == "samsung":
        if not model:
            return {"status": "none", "label": "", "refinement_type": ""}
        display = model.replace("_", " ").upper()
        if model.startswith(("s", "a", "m", "f", "z")):
            display = f"Galaxy {display}"
        if model in SUPPORTED_SAMSUNG_MODELS:
            return {"status": "supported", "label": display, "refinement_type": "model"}
        return {"status": "unsupported", "label": display, "refinement_type": "model"}

    if p == "oppo":
        if not version:
            return {"status": "none", "label": "", "refinement_type": ""}
        display = f"ColorOS {version}"
        if version in SUPPORTED_OPPO_COLOROS:
            return {"status": "supported", "label": display, "refinement_type": "version"}
        return {"status": "unsupported", "label": display, "refinement_type": "version"}

    if p == "xiaomi":
        if not version:
            return {"status": "none", "label": "", "refinement_type": ""}
        display = f"HyperOS {version}"
        if version in SUPPORTED_XIAOMI_HYPEROS:
            return {"status": "supported", "label": display, "refinement_type": "version"}
        return {"status": "unsupported", "label": display, "refinement_type": "version"}

    if p == "pixel":
        if not version:
            return {"status": "none", "label": "", "refinement_type": ""}
        display = f"Android {version}"
        if version in SUPPORTED_PIXEL_ANDROID:
            return {"status": "supported", "label": display, "refinement_type": "version"}
        return {"status": "unsupported", "label": display, "refinement_type": "version"}

    return {"status": "none", "label": "", "refinement_type": ""}


def check_platform_refinement_from_extraction(
    extraction: dict | None,
    platform: str,
    *,
    text: str = "",
) -> dict:
    """LLM for OS version; regex for Samsung device model."""
    p = (platform or "").strip().lower()
    if p == "samsung":
        return check_platform_refinement(text, platform)

    ext = extraction or {}
    confidence = str(ext.get("confidence") or "").strip().lower()
    os_family = str(ext.get("os_family") or "").strip().lower()
    os_version = str(ext.get("os_version") or "").strip()

    if os_version and confidence != "low":
        return validate_platform_refinement(
            platform,
            os_family=os_family,
            os_version=os_version,
            device_model="",
        )
    return check_platform_refinement(text, platform)


def check_platform_refinement(text: str, platform: str) -> dict:
    p = (platform or "").strip().lower()
    if p not in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
        return {"status": "none", "label": "", "refinement_type": ""}

    if p == "apple":
        family, version = _regex_extract_apple_version(text)
        return validate_platform_refinement(
            p, os_family=family, os_version=version, device_model=""
        )

    if p == "samsung":
        model = _normalize_samsung_model_regex(text)
        return validate_platform_refinement(
            p, os_family="", os_version="", device_model=model
        )

    if p == "oppo":
        version = _regex_extract_coloros_version(text)
        return validate_platform_refinement(
            p, os_family="coloros", os_version=version, device_model=""
        )

    if p == "xiaomi":
        version = _regex_extract_hyperos_version(text)
        return validate_platform_refinement(
            p, os_family="hyperos", os_version=version, device_model=""
        )

    if p == "pixel":
        version = _regex_extract_android_version(text)
        return validate_platform_refinement(
            p, os_family="android", os_version=version, device_model=""
        )

    return {"status": "none", "label": "", "refinement_type": ""}
