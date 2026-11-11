"""Explicit one-time network installation of pinned multilingual ASR models.

Never invoked automatically by a GUI or by the transcription worker.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from .voice import MODEL_ROOT, MODEL_SPECS


def install_model(name):
    from huggingface_hub import snapshot_download
    spec = MODEL_SPECS[name]
    destination = MODEL_ROOT / name
    destination.mkdir(parents=True, exist_ok=True)
    print(f"Загрузка {name}: {spec['size'] / 1e6:.0f} МБ", flush=True)
    started = time.perf_counter()
    snapshot_download(spec["repo"], revision=spec["revision"], local_dir=destination,
                      allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "README.md"],
                      max_workers=3)
    digest = hashlib.sha256()
    with (destination / "model.bin").open("rb") as model:
        for block in iter(lambda: model.read(1024 * 1024), b""):
            digest.update(block)
    if (destination / "model.bin").stat().st_size != spec["size"] or digest.hexdigest() != spec["sha256"]:
        raise ValueError(f"Контрольная сумма {name} не совпала. Установка не подтверждена.")
    # Small files have stable Git blob IDs at both pinned revisions.
    blobs = {"tokenizer.json": "7818adb6de9fa3064d3ff81226fdd675be1f6344",
             "vocabulary.txt": "c9074644d9d1205686f16d411564729461324b75",
             "config.json": "e5047537059bd8f182d9ca64c470201585015187" if name == "small" else
                            "242aa06a480a7b5509375c645097e87af5136774"}
    for filename, expected in blobs.items():
        data = (destination / filename).read_bytes()
        actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if actual != expected:
            raise ValueError(f"Контрольная сумма {name}/{filename} не совпала.")
    (destination / "installed.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    print(f"Готово: {name}, файлы проверены ({time.perf_counter() - started:.1f} с)", flush=True)
    return {"name": name, **spec, "elapsed_s": time.perf_counter() - started}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("balanced", "quality", "all"), default="all")
    args = parser.parse_args()
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    names = ["small", "medium"] if args.profile == "all" else ["small" if args.profile == "balanced" else "medium"]
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(MODEL_ROOT).free < sum(MODEL_SPECS[name]["size"] for name in names) * 2:
        raise ValueError("Для установки моделей нужно около 4 ГБ свободного места.")
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        results = list(pool.map(install_model, names))
    print(json.dumps({"installed": results}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
