"""Local multilingual retrieval embeddings. Only prepare_model uses the network."""

from __future__ import annotations

import hashlib
import os
import pathlib
import tempfile

import numpy as np

MODEL_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
DIMENSIONS = 384
_FILES = {
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    "onnx/model_quantized.onnx": "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193",
}


def _digest(path: pathlib.Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def prepare_model(model_dir: pathlib.Path) -> None:
    import requests

    model_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = f"https://huggingface.co/Xenova/multilingual-e5-small/resolve/{MODEL_REVISION}"
    for relative, expected in _FILES.items():
        target = model_dir / pathlib.Path(relative).name
        if target.is_file() and _digest(target) == expected:
            continue
        with tempfile.NamedTemporaryFile(dir=model_dir, delete=False) as file:
            partial = pathlib.Path(file.name)
        try:
            with requests.get(f"{base}/{relative}", stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                with partial.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        file.write(chunk)
            if _digest(partial) != expected:
                raise ValueError(f"Checksum mismatch for {target.name}")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)


class E5Encoder:
    def __init__(self, model_dir: pathlib.Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_dir / "model_quantized.onnx"), sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {item.name for item in self.session.get_inputs()}

    def encode(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        prefix = "query: " if query else "passage: "
        batches = []
        for start in range(0, len(texts), 8):
            tokens = self.tokenizer.encode_batch([prefix + text for text in texts[start:start + 8]])
            mask = np.asarray([t.attention_mask for t in tokens], dtype=np.int64)
            values = {
                "input_ids": np.asarray([t.ids for t in tokens], dtype=np.int64),
                "attention_mask": mask,
                "token_type_ids": np.asarray([t.type_ids for t in tokens], dtype=np.int64),
            }
            hidden = self.session.run(["last_hidden_state"], {name: values[name] for name in self.input_names})[0]
            pooled = (hidden * mask[..., None].astype(np.float32)).sum(axis=1) / mask.sum(axis=1, keepdims=True)
            norm = np.linalg.norm(pooled, axis=1, keepdims=True)
            batches.append((pooled / np.maximum(norm, 1e-12)).astype("<f4"))
        return np.concatenate(batches, axis=0)
