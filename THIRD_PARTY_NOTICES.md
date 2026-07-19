# Сторонние компоненты

Исходный код StreamDock распространяется по лицензии [MIT](LICENSE). Зависимости и модели устанавливаются или загружаются отдельно и сохраняют собственные лицензии и условия использования.

Основные сторонние проекты:

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - загрузка медиаданных;
- [FFmpeg](https://ffmpeg.org/) - объединение и преобразование аудио и видео;
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) и модели [Whisper](https://github.com/openai/whisper) - распознавание речи;
- [onnx-asr](https://github.com/istupakov/onnx-asr) и [GigaAM](https://huggingface.co/istupakov/gigaam-v3-onnx) - распознавание русской речи;
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) и используемые им модели - локальное разделение спикеров;
- [FastAPI](https://github.com/fastapi/fastapi) и [Uvicorn](https://github.com/encode/uvicorn) - локальный веб-сервер.

StreamDock не включает файлы моделей в Git-репозиторий и не меняет их лицензии. Перед распространением сборки с заранее загруженными моделями необходимо отдельно проверить условия каждой модели и сохранить её уведомления об авторских правах.

Этот файл служит краткой навигацией и не заменяет полные тексты лицензий сторонних проектов.
