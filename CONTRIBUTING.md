# Как помочь проекту

Спасибо за желание улучшить StreamDock. Проект рассчитан на локальную работу в Windows, поэтому изменения должны сохранять приватность данных и не требовать облачных API.

## Перед началом

1. Проверьте существующие issues, чтобы не создавать дубликат.
2. Для заметного изменения сначала создайте issue и коротко опишите пользовательскую проблему и предлагаемое решение.
3. Уязвимости не публикуйте в обычных issues. Используйте порядок из [SECURITY.md](SECURITY.md).

## Локальная установка для разработки

Понадобятся Windows, Python 3.11 или новее, FFmpeg и Node.js 22 или новее.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Правила изменений

- Соблюдайте архитектуру и проверки из [AGENTS.md](AGENTS.md).
- Изменения интерфейса должны соответствовать [DESIGN.md](DESIGN.md).
- Не добавляйте внешние API, телеметрию, обход DRM или передачу пользовательских данных без отдельного обсуждения.
- Не включайте в коммит видео, аудио, модели, логи, cookies, токены, локальные пути и другие личные данные.
- Делайте один pull request на одну понятную задачу. Не смешивайте исправление ошибки с несвязанным рефакторингом.
- Добавляйте или обновляйте тесты, если меняется поведение приложения.

## Проверка перед pull request

```powershell
.venv\Scripts\python.exe -m compileall -q app tests
.venv\Scripts\python.exe -m pytest
node --check app\static\app.js
Get-ChildItem browser-extension -Filter *.js | ForEach-Object { node --check $_.FullName }
node --check tests\extension_background_harness.js
```

В описании pull request укажите, что изменилось, как это проверить вручную и какие проверки вы запустили. Для интерфейсных изменений приложите скриншоты на ширине 1366, 1024 и 390 пикселей.
