"""Shared FeatureCollection validation on import."""


def parse_feature_collection(fc) -> tuple[str, list]:
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        return "invalid", []
    feats = fc.get("features")
    if not isinstance(feats, list):
        return "invalid", []
    if not feats:
        return "empty", []
    return "ok", feats


def empty_import_result(total_key: str, total: int) -> dict:
    return {
        "imported": 0,
        "merged": 0,
        "skipped_existing": 0,
        "invalid": 0,
        total_key: total,
        "empty": True,
        "message": "rien à importer",
    }
