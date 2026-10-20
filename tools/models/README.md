# Подготовка моделей

Модели готовит инженер Qoima до поставки; на объекте hot path запускает только ONNX
(ONNX Runtime или TensorRT EP, движок FP16 строится на месте). Артефакты кладутся в
`/var/lib/qoima/models/registry/<имя>/` — `model.onnx` + `manifest.yaml`
(контракт `ModelManifest` с разделом `runtime`). sha256 файла сверяется при загрузке.

| Модель | Скрипт / источник | Статус |
|---|---|---|
| `rfdetr-person` — детектор людей RF-DETR (Apache-2.0, COCO) | `prepare_rfdetr.py` | скрипт готов; запуск требует скачать веса Roboflow (~100 МБ) |
| `rtmpose-body` — поза RTMPose (Apache-2.0) | официальный ONNX OpenMMLab, манифест — `models/registry/rtmpose-body.yaml` | выбор модели позы — по бенчмарку на клипах объекта; лицензии датасетов обучения (Body7) — на ревью |
| `qoima-smoke-fire` — дым и огонь | веса Qoima | нет весов → сценарий не развёрнут; обучение — Фаза 7 |

Без артефактов vision-pipeline работает (Camera Health), но не объявляет возможности
`person_detection`/`fall`/`red_zone` — «Состояние системы» показывает, каких моделей нет.
