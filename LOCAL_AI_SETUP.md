# Локальная ИИ в JARVIS — бесплатно, уровень бога

Гайд: как поставить **Ollama** (чат) и **локальную генерацию изображений** (топовое качество без платежей) и подключить к проекту.

---

## 1. Ollama — чат (Llama, Mistral, Phi и др.)

**Что это:** Локальный запуск больших языковых моделей на своём ПК. Никаких ключей и подписок.

### Установка (Windows)

1. **Скачать:** https://ollama.com/download — выбери **Windows**, скачай установщик.
2. **Установить:** запусти `OllamaSetup.exe`, пройди шаги. Ollama поставится как служба и будет доступен по адресу `http://localhost:11434`.
3. **Проверка:** открой терминал (PowerShell или cmd) и выполни:
   ```bash
   ollama --version
   ollama list
   ```

### Команды Ollama

| Команда | Описание |
|--------|----------|
| `ollama serve` | Запустить сервер (обычно уже запущен как служба) |
| `ollama list` | Список установленных моделей |
| `ollama run llama3.2` | Скачать (если нет) и запустить чат с Llama 3.2 |
| `ollama run mistral` | Модель Mistral 7B |
| `ollama run phi3` | Малая модель Phi-3 |
| `ollama run llama3.1:8b` | Llama 3.1 8B (быстрая) |
| `ollama run gemma2` | Gemma 2 |
| `ollama pull <имя>` | Только скачать модель, не запуская чат |

**Популярные модели для чата (бесплатно):**

- `llama3.2` — универсальная, хороший баланс
- `llama3.1:8b` — быстрая, мало памяти
- `mistral` — качественная 7B
- `phi3` — очень лёгкая
- `gemma2` — от Google
- `qwen2.5` — сильная по рассуждениям

Сначала выполни, например: `ollama pull llama3.2`, затем в JARVIS выбери модель **Ollama — Llama 3.2**.

### Huihui AI (Ollama) — DeepSeek R1 / Qwen3 / Gemma3 без цензуры

Модели из библиотеки [huihui_ai](https://ollama.com/huihui_ai/deepseek-r1-abliterated) нужно скачать отдельно. В JARVIS: **AI → Авто** → секция **Huihui AI (Ollama)**.

**Для слабого ПК — сначала попробуй лёгкие модели (мало RAM и места):**

```bash
# Самая лёгкая — Gemma3 270M (~543 MB)
ollama pull huihui_ai/gemma3-abliterated:270m

# Gemma3 1B (~806 MB)
ollama pull huihui_ai/gemma3-abliterated:1b

# Qwen3 0.6B
ollama pull huihui_ai/qwen3-abliterated:0.6b

# DeepSeek R1 1.5B (~1.1 GB)
ollama pull huihui_ai/deepseek-r1-abliterated:1.5b
```

**Средние и большие варианты:**

```bash
# DeepSeek R1 (размеры: 1.5b, 7b, 8b, 14b, 32b, 70b)
ollama pull huihui_ai/deepseek-r1-abliterated
ollama pull huihui_ai/deepseek-r1-abliterated:7b

# Qwen3 (0.6b, 1.7b, 4b, 8b, 14b, 32b...)
ollama pull huihui_ai/qwen3-abliterated
ollama pull huihui_ai/qwen3-abliterated:8b

# Gemma3 (270m, 1b, 4b, 12b, 27b)
ollama pull huihui_ai/gemma3-abliterated
ollama pull huihui_ai/gemma3-abliterated:4b
```

Если в чате видишь ошибку **model not found** — в сообщении будет подсказка вида `Скачать модель: ollama pull huihui_ai/...`. Выполни эту команду в PowerShell или cmd, дождись загрузки, затем снова выбери модель в JARVIS.

### Настройка в JARVIS (.env)

Добавь в `.env` (если хочешь другой адрес или модель по умолчанию):

```env
# Ollama — локальный чат (по умолчанию уже localhost:11434 и llama3.2)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
```

В интерфейсе JARVIS: вкладка **AI** → кнопка **Авто** → в списке моделей секция **Локально (Ollama)** → выбери нужную модель. Чат будет ходить в твой локальный Ollama.

---

## 2. Локальная генерация изображений — топовое качество бесплатно

**Рекомендация:** **Fooocus** — один из самых простых и сильных вариантов под Windows (на базе Stable Diffusion). Качество на уровне платных сервисов, полностью бесплатно.

### Вариант A: Fooocus (рекомендуется)

1. **Скачать:** https://github.com/lllyasviel/Fooocus/releases — архив для Windows (например `Fooocus_win64_2-1-xxx.7z`).
2. **Распаковать** в папку, например `C:\Fooocus`.
3. **Запуск:** открой в этой папке `run_nvidia_gpu.bat` (если есть NVIDIA) или `run_cpu.bat` (только CPU будет медленнее).
4. При первом запуске скачаются модели — дождись окончания. Откроется окно с полем ввода промпта и кнопкой генерации.
5. **API для JARVIS:** локальный `Fooocus` должен быть доступен по адресу `http://127.0.0.1:7865`. Укажи в `.env`:
   ```env
   LOCAL_IMAGE_API_URL=http://127.0.0.1:7865
   FOOOCUS_AUTOSTART=1
   ```
   Если папка `Fooocus-2.5.5` лежит рядом с проектом или явно указана через `FOOOCUS_PATH`, JARVIS будет пытаться запускать `Fooocus` сам и ждать его готовности.

**Альтернативы того же уровня:**

- **Stable Diffusion WebUI (A1111):** https://github.com/AUTOMATIC1111/stable-diffusion-webui — больше настроек, сложнее установка.
- **ComfyUI:** максимальная гибкость и качество, но нужна настройка нод.

Все они бесплатные и работают локально на твоём железе.

---

## 3. Советы

- **ОЗУ/VRAM:** для Ollama 8B-моделей хватает 8–16 GB RAM. Для Fooocus/SD желательно 6+ GB VRAM (NVIDIA) или терпение на CPU.
- **Первый запуск:** Ollama и Fooocus при первом запуске качают модели (несколько гигабайт). Убедись, что есть место на диске и стабильный интернет.
- **Брандмауэр:** если JARVIS и Ollama на разных машинах, открой порт 11434 или укажи в `OLLAMA_BASE_URL` IP и порт сервера.
- **Модели Ollama под задачи:** для кода/агента хорошо показывают себя `llama3.2`, `qwen2.5`, `mistral`; для быстрых ответов — `phi3`, `llama3.1:8b`.

---

## 4. Что уже готово в проекте

- **Ollama (чат):** в списке моделей есть секция «Локально (Ollama)». Выбор `ollama` или `ollama:имя_модели` переключает нейросеть на твой локальный Ollama. При `OLLAMA_AUTOSTART=1` JARVIS пытается поднять сервис сам и проверяет его готовность.
- **Fooocus (картинки):** в списке image-моделей есть `Fooocus (локально, без цензуры)`. Команда `/image <prompt>` генерирует картинку через локальный `Fooocus`, показывает статус генерации прямо в чате, а итоговое изображение отображается inline с действиями открыть/скачать/открыть папку/копировать путь.
- **Конфиг:** в `config.py` и `.env` используются переменные `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_AUTOSTART`, `LOCAL_IMAGE_API_URL`, `FOOOCUS_AUTOSTART`, `FOOOCUS_PATH`.

После установки Ollama и (по желанию) Fooocus достаточно выбрать в JARVIS нужную модель и при необходимости поправить `.env`.
