"""Fetch the attributed UCI source once; retain a verifiable download receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://archive.ics.uci.edu/static/public/352/online+retail.zip"
SOURCE_PAGE = "https://archive.ics.uci.edu/dataset/352/online+retail"
ATTRIBUTION = "Chen, D. (2015). Online Retail [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5BW33."


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "online-retail.zip"
    workbook = destination / "Online Retail.xlsx"
    receipt_path = destination / "source_receipt.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        for path, field in ((archive, "archive_sha256"), (workbook, "workbook_sha256")):
            if not path.exists() or sha256(path) != receipt[field]:
                raise ValueError(f"Source receipt verification failed: {path.name}")
        return receipt
    if archive.exists() or workbook.exists():
        raise ValueError("Unreceipted source files exist; inspect them before fetching again.")
    temporary = destination / "online-retail.zip.partial"
    with requests.get(SOURCE_URL, stream=True, timeout=(20, 180)) as response:
        response.raise_for_status()
        with temporary.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                handle.write(chunk)
    with zipfile.ZipFile(temporary) as bundle:
        names = [name for name in bundle.namelist() if name.endswith("Online Retail.xlsx")]
        if len(names) != 1:
            raise ValueError("Expected exactly one Online Retail workbook")
        workbook.write_bytes(bundle.read(names[0]))
    temporary.replace(archive)
    receipt = {
        "source_url": SOURCE_URL,
        "source_page": SOURCE_PAGE,
        "attribution": ATTRIBUTION,
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "downloaded_at": datetime.now(UTC).isoformat(),
        "archive_sha256": sha256(archive),
        "workbook_sha256": sha256(workbook),
        "archive_bytes": archive.stat().st_size,
        "workbook_bytes": workbook.stat().st_size,
    }
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "data" / "private")
    args = parser.parse_args()
    print(json.dumps(fetch(args.destination), indent=2))
