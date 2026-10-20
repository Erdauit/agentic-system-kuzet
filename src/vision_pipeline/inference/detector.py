"""Детектор людей RF-DETR (Apache-2.0, COCO).

Разбор выходов повторяет rfdetr/export/_runtime/decode.py (RF-DETR 1.11): `dets` — боксы
(B, Q, 4) в нормализованных cxcywh; `labels` — логиты (B, Q, C), независимые сигмоиды по классам.
Нужен один класс (person), поэтому вместо глобального top-k по всем парам запрос/класс берутся
запросы, у которых вероятность класса person выше порога, — для одного класса это то же самое.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from vision_pipeline.inference.preprocess import RGB, to_tensor
from vision_pipeline.inference.runner import OnnxModel
from vision_pipeline.tracking.tracker import Detection


def _sigmoid(x: NDArray[np.float32]) -> NDArray[np.float32]:
    clipped = np.clip(x, -88, 88).astype(np.float32)
    return (np.float32(1.0) / (np.float32(1.0) + np.exp(-clipped))).astype(np.float32)


class PersonDetector:
    def __init__(self, model: OnnxModel, person_class: str, min_score: float) -> None:
        rt = model.artifact.runtime
        if rt.decoder != "rfdetr":
            raise ValueError(f"детектор людей ожидает декодер rfdetr, в манифесте {rt.decoder}")
        if person_class not in rt.class_ids:
            raise ValueError(f"в манифесте нет класса {person_class}")
        self.model = model
        self.class_index = rt.class_ids[person_class]
        self.min_score = min_score
        self.boxes_name, self.logits_name = rt.outputs[0], rt.outputs[1]

    @property
    def version(self) -> str:
        return self.model.artifact.version

    def detect(self, frames: list[RGB]) -> list[list[Detection]]:
        if not frames:
            return []
        batch = np.stack([to_tensor(f, self.model.artifact.runtime) for f in frames])
        out = self.model.run(batch)
        return [
            self.decode(out[self.boxes_name][i], out[self.logits_name][i])
            for i in range(len(frames))
        ]

    def decode(
        self, boxes_cwh: NDArray[np.float32], logits: NDArray[np.float32]
    ) -> list[Detection]:
        scores = _sigmoid(logits[:, self.class_index])
        keep = np.nonzero(scores > self.min_score)[0]
        result = []
        for q in keep[np.argsort(-scores[keep])]:
            cx, cy, w, h = (float(v) for v in boxes_cwh[q])
            box = (
                max(0.0, cx - w / 2),
                max(0.0, cy - h / 2),
                min(1.0, cx + w / 2),
                min(1.0, cy + h / 2),
            )
            if box[2] > box[0] and box[3] > box[1]:
                result.append(Detection(box=box, score=float(scores[q])))
        return result
