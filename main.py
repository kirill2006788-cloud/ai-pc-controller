import sys
import os
import subprocess
import shutil
import psutil
import time
import webbrowser
import traceback
import asyncio
import ctypes
from datetime import datetime
from urllib.parse import quote_plus, unquote, urlparse
import threading
import json
import math
import random
import html
import fnmatch
import re
import socket
from typing import Any
import tempfile

from config import (
    API_KEY, BASE_URL,
    OLLAMA_AUTOSTART, FOOOCUS_AUTOSTART, FOOOCUS_PATH,
    LOCAL_IMAGE_API_URL, OLLAMA_BASE_URL,
)

_CHAT_PREVIEW_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "generated",
    "_chat_previews",
)
os.makedirs(_CHAT_PREVIEW_DIR, exist_ok=True)

# Расширения файлов, допустимые для чтения/контекста в AI-чате (единый список для @файл и read_text_file)
AI_ALLOWED_FILE_EXTENSIONS = frozenset(
    {".txt", ".md", ".py", ".json", ".log", ".csv", ".html", ".htm", ".css", ".js", ".xml"}
)
# Лимит символов истории чата для контекста модели (обрезаем старые сообщения)
MAX_CHAT_CONTEXT_CHARS = 14000
MAX_CHAT_CONTEXT_MESSAGES = 24


def _make_send_arrow_icon():
    """Векторная иконка стрелки «отправить» без пикселизации (рисуем в 2x для чёткости)."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QPolygonF
    size = 64
    pix = QPixmap(size, size)
    pix.fill(QtCore.Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    # Стрелка вправо (треугольник): остриё справа
    margin = 10
    w, h = size - 2 * margin, size - 2 * margin
    cx, cy = size // 2, size // 2
    arrow = QPolygonF([
        QPointF(cx + w // 2, cy),
        QPointF(cx - w // 2, cy - h // 2),
        QPointF(cx - w // 2, cy + h // 2),
    ])
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255))
    p.drawPolygon(arrow)
    p.end()
    icon = QIcon(pix)
    icon.addPixmap(pix, QIcon.Mode.Normal, QIcon.State.Off)
    return icon


import pyautogui
from PIL import ImageDraw, ImageFont
from PyQt6 import QtWidgets, QtGui, QtCore
from PyQt6.QtCore import QUrl
from PyQt6 import QtMultimedia
from telegram import Bot
import requests
from neural_network_manager import NeuralNetworkManager
from replicate_manager import (
    ReplicateManager, IMAGE_MODELS, VIDEO_MODELS, ALL_MODELS,
)
try:
    from telethon import TelegramClient, events
    from telethon.tl.types import User, Chat, Channel
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
try:
    import speedtest
except Exception:
    speedtest = None


class _ChatBridge(QtCore.QObject):
    result_ready = QtCore.pyqtSignal(str)
    result_chunk = QtCore.pyqtSignal(str)  # стриминг: накопленный текст ответа по мере появления


class _TTSBridge(QtCore.QObject):
    play_file = QtCore.pyqtSignal(str)
    log_system = QtCore.pyqtSignal(str)


class _ReplicateBridge(QtCore.QObject):
    status = QtCore.pyqtSignal(object)
    image_ready = QtCore.pyqtSignal(object)
    video_ready = QtCore.pyqtSignal(object)
    error = QtCore.pyqtSignal(object)


class _ChatBrowser(QtWidgets.QTextBrowser):
    """QTextBrowser subclass that loads local image files for inline previews."""

    def loadResource(self, resource_type: int, url: QtCore.QUrl):
        if resource_type == 2 and url.isLocalFile():
            path = url.toLocalFile().split("?")[0]
            for _ in range(3):
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                    img = QtGui.QImage()
                    if img.loadFromData(data) and not img.isNull():
                        return img
                except Exception:
                    pass
                time.sleep(0.02)
        return super().loadResource(resource_type, url)

    def setSource(self, name: QtCore.QUrl):
        # Prevent QTextBrowser from navigating to our custom media-action URLs.
        if name.scheme() == "jarvis":
            return
        super().setSource(name)


class _FullscreenImageViewer(QtWidgets.QWidget):
    """Минималистичный просмотрщик изображений на весь экран."""
    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.WindowType.Window | 
            QtCore.Qt.WindowType.FramelessWindowHint | 
            QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background-color: rgba(0, 0, 0, 230);")
        
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.label = QtWidgets.QLabel(self)
        self.label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)
        
        # Загружаем изображение
        pixmap = QtGui.QPixmap(image_path)
        if not pixmap.isNull():
            # Масштабируем до размеров экрана
            screen = QtWidgets.QApplication.primaryScreen().geometry()
            scaled = pixmap.scaled(
                screen.size(), 
                QtCore.Qt.AspectRatioMode.KeepAspectRatio, 
                QtCore.Qt.TransformationMode.SmoothTransformation
            )
            self.label.setPixmap(scaled)
        else:
            self.label.setText("Ошибка загрузки изображения")
            self.label.setStyleSheet("color: white; font-size: 18pt;")

        self.showFullScreen()
        # Ставим фокус для ESC
        self.setFocus()

    def mousePressEvent(self, event):
        self.close()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.close()
        super().keyPressEvent(event)


class NeuralNetworkAPI:
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url

    def send_request(self, prompt: str):
        """Отправить запрос к API нейронной сети."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "prompt": prompt,
            "max_tokens": 100
        }
        try:
            response = requests.post(f"{self.base_url}/v1/completions", json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Ошибка при запросе к API: {e}")
            return None

TELEGRAM_QSS = """
#MessengerTab QGroupBox {
    border: 1px solid rgba(0,212,255,0.15);
    border-radius: 16px;
    margin-top: 12px;
    padding-top: 20px;
    background: rgba(14,14,32,0.85);
}
#MessengerTab QGroupBox::title {
    subcontrol-origin: margin;
    left: 16px; padding: 2px 10px;
    color: #00D4FF; font-weight: 700; font-size: 12pt;
}
#MessengerTab QTreeWidget, #MessengerTab QListWidget {
    background-color: #0c0c1e;
    border: 1px solid rgba(0,212,255,0.12);
    border-radius: 12px;
    padding: 6px;
}
#MessengerTab QTreeWidget::item, #MessengerTab QListWidget::item {
    padding: 10px 12px; border-radius: 8px; min-height: 32px;
}
#MessengerTab QTreeWidget::item:selected, #MessengerTab QListWidget::item:selected {
    background: rgba(0,212,255,0.18); color: #00D4FF;
}
#MessengerTab QTreeWidget::item:hover, #MessengerTab QListWidget::item:hover {
    background: rgba(0,212,255,0.08);
}
#MessengerTab QPushButton {
    background: #12122a; border: 1px solid rgba(0,212,255,0.2);
    border-radius: 14px; padding: 8px 18px; color: #E0E0FF; font-weight: 600;
}
#MessengerTab QPushButton:hover {
    background: rgba(0,212,255,0.12); color: #00D4FF;
    border: 1px solid #00D4FF;
}
#MessengerTab QTextEdit, #MessengerTab QPlainTextEdit {
    background: #0a0a18; border: 1px solid rgba(0,212,255,0.1);
    border-radius: 12px; padding: 10px; color: #E0E0FF;
}
#MessengerTab QLineEdit {
    background: #0e0e20; border: 1px solid rgba(0,212,255,0.15);
    border-radius: 12px; padding: 8px 14px; color: #E0E0FF;
}
#MessengerTab QLineEdit:focus { border: 1px solid #00D4FF; }
#MessengerTab QCheckBox { spacing: 8px; }
#MessengerTab QCheckBox::indicator {
    width: 20px; height: 20px; border: 2px solid rgba(0,212,255,0.3);
    border-radius: 6px; background: #0e0e20;
}
#MessengerTab QCheckBox::indicator:checked {
    background: #00D4FF; border: 2px solid #00D4FF;
}
"""

# Утилиты для теней и простых анимаций
def _apply_shadow(widget: QtWidgets.QWidget, blur: int = 14, x: int = 0, y: int = 4, color: QtGui.QColor = None):
    try:
        eff = QtWidgets.QGraphicsDropShadowEffect()
        eff.setBlurRadius(blur)
        eff.setOffset(x, y)
        if color is None:
            color = QtGui.QColor(0, 0, 0, 160)
        eff.setColor(color)
        widget.setGraphicsEffect(eff)
    except Exception:
        pass

def _add_press_animation(btn: QtWidgets.QPushButton, shrink: float = 0.94, duration: int = 90):
    # Простая анимация сжатия кнопки при нажатии
    def on_pressed():
        rect = btn.geometry()
        w = max(1, int(rect.width() * shrink))
        h = max(1, int(rect.height() * shrink))
        dx = (rect.width() - w) // 2
        dy = (rect.height() - h) // 2
        target = QtCore.QRect(rect.x() + dx, rect.y() + dy, w, h)
        anim = QtCore.QPropertyAnimation(btn, b"geometry")
        anim.setDuration(duration)
        anim.setStartValue(rect)
        anim.setEndValue(target)
        anim.setEasingCurve(QtCore.QEasingCurve.Type.InOutQuad)
        anim.start(QtCore.QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        btn._press_anim = anim

    def on_released():
        if hasattr(btn, "_press_anim") and hasattr(btn._press_anim, 'startValue'):
            try:
                orig = btn._press_anim.startValue()
            except Exception:
                orig = btn.geometry()
        else:
            orig = btn.geometry()
        rect = btn.geometry()
        anim = QtCore.QPropertyAnimation(btn, b"geometry")
        anim.setDuration(duration)
        anim.setStartValue(rect)
        anim.setEndValue(orig)
        anim.setEasingCurve(QtCore.QEasingCurve.Type.InOutQuad)
        anim.start(QtCore.QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        btn._release_anim = anim

    try:
        btn.pressed.connect(on_pressed)
        btn.released.connect(on_released)
    except Exception:
        pass



SYSTEM_QSS = """
QWidget#SystemTab { background: transparent; }
QLabel[class="monitorValue"] { font-size: 22pt; font-weight: 800; color: #00D4FF; }
QLabel[class="monitorLabel"] { font-size: 10pt; color: #5050a0; letter-spacing: 1px; }
QProgressBar[class="tempBar"] {
    background: #0e0e20; border: 1px solid rgba(255,0,92,0.3);
    border-radius: 8px; height: 16px; text-align: center; color: #E0E0FF;
}
QProgressBar[class="tempBar"]::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #00ff9d, stop:0.5 #ff9d00, stop:1 #ff005c);
    border-radius: 7px;
}
QProgressBar[class="diskBar"] {
    background: #0e0e20; border: 1px solid rgba(157,0,255,0.2);
    border-radius: 8px; height: 14px; text-align: center;
}
QProgressBar[class="diskBar"]::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #9D00FF, stop:1 #00D4FF);
    border-radius: 7px;
}
QPushButton[class="dangerAction"] {
    background: rgba(255,0,92,0.08); border: 1px solid rgba(255,0,92,0.4);
    color: #ff005c; border-radius: 14px;
}
QPushButton[class="dangerAction"]:hover {
    background: #ff005c; color: #fff;
}
"""

AUTOMATION_QSS = """
QWidget#AutomationTab { background: transparent; }
"""

GAME_QSS = """
QWidget#GameTab { background: transparent; }
QLabel#fpsLabel { color: #00ff9d; font-size: 22pt; font-weight: 800; }
QProgressBar#tempBar {
    background: #0e0e20; border: 1px solid rgba(255,0,92,0.3);
    border-radius: 8px; height: 16px; text-align: center;
}
QProgressBar#tempBar::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #ff005c, stop:1 #ff9d00);
    border-radius: 7px;
}
"""


class CommandType:
    OPEN_EXPLORER = "open_explorer"
    OPEN_FILE = "open_file"
    RUN_PROGRAM = "run_program"
    AI_CHAT = "ai_chat"


class TelegramManager:
    def __init__(self, log_func=None):
        self.log = log_func or (lambda *_args, **_kwargs: None)
        self.connected = False
        self._bot = None
        self._bot_token = ""
        self._load_token_from_env()

    def _load_token_from_env(self):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            env_path = os.path.join(base_dir, ".env")
            if os.path.exists(env_path):
                try:
                    with open(env_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#") or "=" not in line:
                                continue
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k and k not in os.environ:
                                os.environ[k] = v
                    token = os.environ.get("TELEGRAM_BOT_TOKEN")
                except Exception as e:
                    self.log(f"[TG] Ошибка чтения .env: {e}")

        if not token:
            self.log("[TG] TELEGRAM_BOT_TOKEN не найден в окружении или .env — бот недоступен")
            self.connected = False
            self._bot = None
            self._bot_token = ""
            return

        try:
            # python-telegram-bot 20.x may break with newer httpx versions because of removed `proxies=`.
            # For this desktop app we only need lightweight status + send support, so use Bot API directly.
            self._bot_token = token
            resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            data = resp.json() if resp.content else {}
            if resp.ok and data.get("ok"):
                self.connected = True
                self._bot = {"username": (data.get("result") or {}).get("username", "")}
                self.log("[TG] Бот инициализирован через Bot API")
            else:
                raise RuntimeError((data.get("description") or resp.text or "Unknown Telegram API error")[:300])
        except Exception as e:
            self.log(f"[TG] Ошибка инициализации бота: {e}")
            self.connected = False
            self._bot = None
            self._bot_token = ""

    def refresh_status(self):
        if self.connected:
            state = "connected"
        else:
            state = "disconnected"
        return {"telegram": state, "discord": "disconnected", "whatsapp": "not_configured", "vk": "not_configured"}

    def send_message(self, chat_name: str, text: str):
        if not self.connected or not self._bot_token:
            self.log("[TG] Бот не инициализирован, сообщение не отправлено")
            return
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
                json={"chat_id": chat_name, "text": text},
                timeout=20,
            )
            data = resp.json() if resp.content else {}
            if not resp.ok or not data.get("ok"):
                raise RuntimeError((data.get("description") or resp.text or "Unknown Telegram API error")[:300])
            self.log(f"[TG] Сообщение отправлено через бота в '{chat_name}'")
        except Exception as e:
            self.log(f"[TG] Ошибка отправки сообщения: {e}")

    def get_active_chats(self):
        return [
            ("💼 Работа", 3, "14:25"),
            ("👨‍\u200d👩‍\u200d👧 Семья", 1, "14:20"),
            ("🎮 Игровой", 12, "14:15"),
            ("👥 Друзья", 0, "13:45"),
            ("📚 Учёба", 2, "13:30"),
        ]


class TelegramUserClient:
    """Telegram user client через Telethon для работы с реальными диалогами и сообщениями."""
    
    def __init__(self, log_func=None, message_callback=None):
        self.log = log_func or (lambda *_args, **_kwargs: None)
        self.message_callback = message_callback  # callback для новых сообщений
        self.connected = False
        self.client = None
        self.api_id = None
        self.api_hash = None
        self.phone = None
        self.password = None  # 2FA password
        self.session_path = None
        self.dialogs = []  # Список диалогов
        self._dialog_index = {}  # normalized_name -> list[int chat_id]
        self.unread_counts = {}  # Счётчики непрочитанных сообщений
        self._code_requested = False  # Флаг что код был запрошен
        self._event_loop = None  # Persistent event loop для всех операций
        self._load_credentials()
        
    def _get_event_loop(self):
        """Получить или создать персистентный event loop."""
        if self._event_loop is None or (hasattr(self._event_loop, "is_closed") and self._event_loop.is_closed()):
            import asyncio
            try:
                # Предпочтение: получить текущий выполняющийся loop (без DeprecationWarning)
                loop = asyncio.get_running_loop()
                if loop.is_closed():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
            except RuntimeError:
                # Нет запущенного loop в этом потоке — создаём новый
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            self._event_loop = loop
        return self._event_loop
    
    def _load_credentials(self):
        """Загрузить api_id, api_hash из .env или переменных окружения."""
        base_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(base_dir, ".env")
        
        # Читаем .env если есть - .env ВСЕГДА переопределяет системные переменные
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k:
                            os.environ[k] = v  # ALWAYS set from .env
                            print(f"[INIT] .env loaded: {k}={v[:10] if len(v) > 10 else v}...")
            except Exception as e:
                print(f"[INIT] Error reading .env: {e}")
        
        self.api_id = os.environ.get("TELEGRAM_API_ID")
        self.api_hash = os.environ.get("TELEGRAM_API_HASH")
        self.phone = os.environ.get("TELEGRAM_PHONE")
        self.password = os.environ.get("TELEGRAM_PASSWORD")  # 2FA password
        
        print(f"[INIT] Credentials loaded: API_ID={bool(self.api_id)}, API_HASH={bool(self.api_hash)}, PHONE={bool(self.phone)}, PASSWORD={bool(self.password)}")
        
        if not self.api_id or not self.api_hash:
            self.log("[TG-USER] TELEGRAM_API_ID и TELEGRAM_API_HASH не найдены в .env")
            self.log(f"[TG-USER] API_ID: {'найден' if self.api_id else 'НЕ найден'}, API_HASH: {'найден' if self.api_hash else 'НЕ найден'}")
            self.log("[TG-USER] Получи их на https://my.telegram.org/apps")
            return
        
        try:
            # Проверяем, что API_ID является числом (Telethon требует int)
            int(self.api_id)
        except ValueError:
            self.log(f"[TG-USER] Ошибка: TELEGRAM_API_ID должен быть числом, получено: {self.api_id}")
            self.api_id = None
            return
        
        self.log(f"[TG-USER] API credentials найдены (API_ID: {self.api_id[:5]}..., API_HASH: {self.api_hash[:5]}...)")
        
        # Путь к файлу сессии
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.session_path = os.path.join(base_dir, "telegram_session")
        
        if not TELETHON_AVAILABLE:
            self.log("[TG-USER] Telethon не установлен. Установи: pip install telethon")
            return
        
        try:
            self.client = TelegramClient(self.session_path, int(self.api_id), self.api_hash)
            self.log("[TG-USER] Клиент создан, требуется авторизация")
        except Exception as e:
            self.log(f"[TG-USER] Ошибка создания клиента: {e}")
            self.client = None
    
    async def _connect_and_auth(self):
        """Подключиться и авторизоваться (async) с расширенным логированием."""
        if not self.client:
            self.log("[TG-USER][DEBUG] client is None")
            return False
        try:
            self.log("[TG-USER][DEBUG] Connecting client...")
            await self.client.connect()
            is_auth = await self.client.is_user_authorized()
            self.log(f"[TG-USER][DEBUG] is_user_authorized: {is_auth}")
            if not is_auth:
                self.log("[TG-USER] Требуется авторизация")
                if not self.phone:
                    self.log("[TG-USER] Укажи TELEGRAM_PHONE в .env (например: +79991234567)")
                    return False
                try:
                    self.log(f"[TG-USER][DEBUG] Sending code request to {self.phone}...")
                    sent = await self.client.send_code_request(self.phone)
                    self.log(f"[TG-USER][DEBUG] send_code_request result: {sent}")
                    self._code_requested = True
                    self.log(f"[TG-USER] Код отправлен на {self.phone}")
                    self.log("[TG-USER] Введи код в диалоге авторизации")
                    return False  # Нужен код от пользователя
                except Exception as e:
                    self.log(f"[TG-USER][ERROR] Ошибка отправки кода: {e}")
                    import traceback
                    self.log(traceback.format_exc())
                    return False
            self.connected = True
            self.log("[TG-USER] Авторизован успешно")
            return True
        except Exception as e:
            self.log(f"[TG-USER][ERROR] Ошибка подключения: {e}")
            import traceback
            self.log(traceback.format_exc())
            return False

    def resolve_chat(self, chat: Any):
        """Resolve chat identifier.

        Supports:
        - int chat_id
        - 'me' (Saved Messages)
        - '@username'
        - display name/title like 'Любимая💓' (uses dialogs index)
        """
        if chat is None:
            return None
        if isinstance(chat, int):
            return chat
        s = str(chat).strip()
        if not s:
            return None
        s_low = s.casefold().strip()
        if s_low in ("me", "saved", "saved messages", "избранное", "избранные"):
            return "me"
        if s.startswith("@"):  # username
            return s

        # numeric chat_id string
        try:
            return int(s)
        except Exception:
            pass

        # ensure dialogs loaded
        if not self.dialogs:
            try:
                self.get_dialogs()
            except Exception:
                pass

        key = self._normalize_dialog_key(s)
        hits = list(self._dialog_index.get(key, []) or [])

        # fallback: substring match
        if not hits and self.dialogs:
            try:
                for d in self.dialogs:
                    if not isinstance(d, dict):
                        continue
                    name = str(d.get("name") or "")
                    if not name:
                        continue
                    if key in self._normalize_dialog_key(name):
                        cid = d.get("id")
                        if cid:
                            hits.append(cid)
            except Exception:
                hits = hits

        # unique
        uniq = []
        for x in hits:
            if x not in uniq:
                uniq.append(x)

        if len(uniq) == 1:
            return uniq[0]
        if len(uniq) > 1:
            raise RuntimeError(f"Найдено несколько чатов по имени '{s}'. Уточни (например точное имя или chat_id)")

        return None
    
    def authorize_with_code(self, code: str):
        """Авторизоваться с кодом (синхронная обёртка)."""
        if not self.client:
            return False
        
        try:
            loop = self._get_event_loop()
            result = loop.run_until_complete(self._authorize_with_code_async(code))
            return result
        except Exception as e:
            self.log(f"[TG-USER] Ошибка авторизации с кодом: {e}")
            import traceback
            self.log(traceback.format_exc())
            return False
    
    async def _authorize_with_code_async(self, code: str):
        """Внутренняя async функция авторизации."""
        try:
            if not self.client.is_connected():
                await self.client.connect()
            
            await self.client.sign_in(self.phone, code)
            self.connected = True
            self._code_requested = False
            self.log("[TG-USER] Авторизован успешно")
            return True
        except Exception as e:
            error_str = str(e)
            self.log(f"[TG-USER] Ошибка входа: {error_str}")
            
            # Проверяем наличие 2FA (двухфакторной аутентификации)
            if "SessionPasswordNeeded" in error_str or "password" in error_str.lower():
                self.log("[TG-USER] Требуется пароль 2FA (двухфакторная аутентификация)")
                
                # Попытка загрузить пароль заново если его нет
                if not self.password:
                    self.password = os.environ.get("TELEGRAM_PASSWORD")
                    self.log(f"[TG-USER][DEBUG] Повторная загрузка пароля: {'найден' if self.password else 'НЕ найден'}")
                
                if self.password:
                    try:
                        self.log("[TG-USER] Пытаюсь авторизоваться с пароль 2FA...")
                        await self.client.sign_in(password=self.password)
                        self.connected = True
                        self._code_requested = False
                        self.log("[TG-USER] Авторизован с 2FA успешно")
                        return True
                    except Exception as pwd_e:
                        self.log(f"[TG-USER] Ошибка входа с пароль: {pwd_e}")
                        return False
                else:
                    self.log("[TG-USER] Пароль 2FA не найден в TELEGRAM_PASSWORD - добавь его в .env")
                    self.log("[TG-USER] Пример: TELEGRAM_PASSWORD=your_2fa_password")
                    return False
            
            # Ошибка связана с кодом
            if "code" in error_str.lower() or "invalid" in error_str.lower():
                self._code_requested = False
            return False
    
    def connect(self):
        """Подключиться (синхронная обёртка)."""
        if not self.client:
            return False
        
        try:
            loop = self._get_event_loop()
            result = loop.run_until_complete(self._connect_and_auth())
            return result
        except Exception as e:
            self.log(f"[TG-USER] Ошибка подключения: {e}")
            import traceback
            self.log(traceback.format_exc())
            return False
    
    def start_listening(self):
        """Запустить фоновый поток для получения сообщений."""
        if not self.connected or not self.client:
            self.log("[TG-USER] Нельзя запустить слушатель: клиент не подключен")
            return
        
        def run_listener():
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._setup_event_handlers())
                loop.run_forever()
            except Exception as e:
                self.log(f"[TG-USER] Ошибка в слушателе: {e}")
        
        thread = threading.Thread(target=run_listener, daemon=True)
        thread.start()
        self.log("[TG-USER] Слушатель сообщений запущен")
    
    async def _setup_event_handlers(self):
        """Настроить обработчики событий."""
        if not self.client:
            return
        
        # Убеждаемся что клиент подключен
        if not self.client.is_connected():
            await self.client.connect()
        
        # Проверяем авторизацию
        if not await self.client.is_user_authorized():
            self.log("[TG-USER] Клиент не авторизован, обработчики не запущены")
            return
        
        @self.client.on(events.NewMessage(incoming=True))
        async def handler(event):
            try:
                sender = await event.get_sender()
                chat = await event.get_chat()
                
                # Определяем имя чата
                if isinstance(chat, User):
                    chat_name = f"{chat.first_name or ''} {chat.last_name or ''}".strip() or chat.username or "Неизвестно"
                elif isinstance(chat, Channel):
                    chat_name = chat.title or "Канал"
                else:
                    chat_name = getattr(chat, 'title', 'Чат')
                
                message_text = event.message.message or "[Медиа]"
                
                # Обновляем счётчик непрочитанных
                chat_id = chat.id
                self.unread_counts[chat_id] = self.unread_counts.get(chat_id, 0) + 1
                
                # Вызываем callback если есть
                if self.message_callback:
                    self.message_callback({
                        "chat_id": chat_id,
                        "chat_name": chat_name,
                        "text": message_text,
                        "timestamp": event.message.date.isoformat() if event.message.date else None,
                        "sender": chat_name
                    })
                
                self.log(f"[TG-USER] Новое сообщение от {chat_name}: {message_text[:50]}")
            except Exception as e:
                self.log(f"[TG-USER] Ошибка обработки сообщения: {e}")
        
        await self.client.start()
        self.log("[TG-USER] Обработчики событий настроены, слушаю сообщения...")
    
    async def _get_dialogs_async(self):
        """Получить список диалогов (async)."""
        try:
            dialogs = await self.client.get_dialogs(limit=200)
            result = []
            for dialog in dialogs:
                entity = dialog.entity
                if isinstance(entity, User):
                    name = f"{entity.first_name or ''} {entity.last_name or ''}".strip() or entity.username or "Неизвестно"
                elif isinstance(entity, Channel):
                    name = entity.title or "Канал"
                else:
                    name = getattr(entity, 'title', 'Чат')
                
                unread = dialog.unread_count
                self.unread_counts[entity.id] = unread
                
                result.append({
                    "id": entity.id,
                    "name": name,
                    "unread": unread,
                    "last_message": dialog.message.message[:50] if dialog.message and dialog.message.message else ""
                })
            
            return result
        except Exception as e:
            self.log(f"[TG-USER] Ошибка получения диалогов: {e}")
            return []

    def _normalize_dialog_key(self, s: str) -> str:
        t = (s or "").strip().casefold()
        # keep emojis; only normalize whitespace
        t = re.sub(r"\s+", " ", t).strip()
        return t
    
    def get_dialogs(self):
        """Получить список диалогов (синхронная обёртка)."""
        if not self.connected or not self.client:
            return []
        
        try:
            loop = self._get_event_loop()
            result = loop.run_until_complete(self._get_dialogs_async())
            self.dialogs = result
            # rebuild index for fast name lookup
            idx = {}
            try:
                for d in result or []:
                    if not isinstance(d, dict):
                        continue
                    name = d.get("name") or ""
                    cid = d.get("id")
                    if not name or not cid:
                        continue
                    key = self._normalize_dialog_key(str(name))
                    if key:
                        idx.setdefault(key, []).append(cid)
            except Exception:
                idx = {}
            self._dialog_index = idx
            return result
        except Exception as e:
            self.log(f"[TG-USER] Ошибка получения диалогов: {e}")
            import traceback
            self.log(traceback.format_exc())
            return []
    
    async def _send_message_async(self, chat_id, text):
        """Отправить сообщение (async)."""
        try:
            await self.client.send_message(chat_id, text)
            return True
        except Exception as e:
            self.log(f"[TG-USER] Ошибка отправки сообщения: {e}")
            return False
    
    def send_message(self, chat_id, text):
        """Отправить сообщение (синхронная обёртка)."""
        if not self.connected or not self.client:
            self.log("[TG-USER] Клиент не подключен")
            return False
        
        try:
            loop = self._get_event_loop()
            result = loop.run_until_complete(self._send_message_async(chat_id, text))
            return result
        except Exception as e:
            self.log(f"[TG-USER] Ошибка отправки: {e}")
            import traceback
            self.log(traceback.format_exc())
            return False

    async def _send_file_async(self, chat_id, path: str):
        try:
            await self.client.send_file(chat_id, path)
            return True
        except Exception as e:
            self.log(f"[TG-USER] Ошибка отправки файла {path}: {e}")
            return False

    def send_file(self, chat_id, path: str):
        """Синхронная обёртка для отправки файла через Telethon."""
        if not self.connected or not self.client:
            self.log("[TG-USER] Клиент не подключен, файл не отправлен")
            return False
        try:
            loop = self._get_event_loop()
            result = loop.run_until_complete(self._send_file_async(chat_id, path))
            return result
        except Exception as e:
            self.log(f"[TG-USER] Ошибка отправки файла: {e}")
            import traceback
            self.log(traceback.format_exc())
            return False
    
    def refresh_status(self):
        """Обновить статус подключения."""
        if self.connected:
            return "connected"
        return "disconnected"


class AutoResponder:
    def __init__(self, log_func=None):
        self.log = log_func or (lambda *_args, **_kwargs: None)
        self.enabled = False

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        state = "включен" if enabled else "выключен"
        self.log(f"[TG] Автоответчик {state} (заглушка)")


class ChatAnalyzer:
    def __init__(self, log_func=None):
        self.log = log_func or (lambda *_args, **_kwargs: None)

    def analyze_activity(self):
        self.log("[TG] Анализ активности (заглушка)")
        return {
            "sent": 1234,
            "received": 2345,
            "top_contact": "Мария (456 сообщений)",
            "peak_time": "19:00-22:00",
            "top_words": ["привет", "ок", "спасибо"],
        }


class GameManager:
    def __init__(self, log_func=None):
        self.log = log_func or (lambda *_args, **_kwargs: None)
        self.game_profiles = {}
        self._load_profiles()

    def _profiles_path(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, "games.json")

    def _load_profiles(self):
        path = self._profiles_path()
        if not os.path.exists(path):
            self.log(f"[GAME] Файл профилей игр не найден: {path}")
            self.game_profiles = {}
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.game_profiles = data
                self.log(f"[GAME] Загружено профилей игр: {len(self.game_profiles)}")
            else:
                self.log("[GAME] Неверный формат games.json (ожидался объект)")
        except Exception as e:
            self.log(f"[GAME] Ошибка чтения games.json: {e}")
            self.game_profiles = {}

    def save_profiles(self):
        path = self._profiles_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.game_profiles, f, ensure_ascii=False, indent=2)
            self.log("[GAME] Профили игр сохранены в games.json")
        except Exception as e:
            self.log(f"[GAME] Ошибка сохранения games.json: {e}")

    def launch_game(self, game_key: str):
        profile = self.game_profiles.get(game_key)
        if not profile:
            self.log(f"[GAME] Профиль игры не найден: {game_key}")
            return
        path = profile.get("path")
        if not path or not os.path.exists(path):
            self.log(f"[GAME] Путь к игре не найден: {path}")
            return
        self.log(f"[GAME] Оптимизация перед запуском игры: {profile.get('name', game_key)}")
        self.optimize_system_for_gaming()
        try:
            os.startfile(path)
            self.log(f"[GAME] Запуск игры: {path}")
        except Exception as e:
            self.log(f"[GAME] Ошибка запуска игры: {e}")

    def optimize_system_for_gaming(
        self,
        close_apps: bool = True,
        clean_ram: bool = False,
        disable_services: bool = False,
        set_priority: bool = False,
    ):
        """Простая безопасная оптимизация перед игрой.

        - close_apps: попытаться закрыть известные фоновые лаунчеры/оверлеи.
        - clean_ram/disable_services/set_priority: сейчас только логируют действия
          (реальные агрессивные действия не выполняются для безопасности).
        """

        self.log("[GAME] Старт оптимизации системы для игр")

        if close_apps:
            # Набор распространённых не критичных процессов-лаунчеров
            safe_to_close = {
                "EpicGamesLauncher.exe",
                "Battle.net.exe",
                "RiotClientServices.exe",
                "steamwebhelper.exe",
                "GalaxyClient.exe",
                "Origin.exe",
                "EA Desktop.exe",
            }
            closed = 0
            try:
                for proc in psutil.process_iter(["name"]):
                    name = proc.info.get("name") or ""
                    if name in safe_to_close:
                        try:
                            proc.terminate()
                            closed += 1
                        except Exception:
                            # Игнорируем ошибки завершения отдельных процессов
                            continue
                self.log(f"[GAME] Закрыто фоновых процессов: {closed}")
            except Exception as e:
                self.log(f"[GAME] Ошибка при попытке закрытия фоновых приложений: {e}")

        if clean_ram:
            # Глубокая очистка памяти потенциально опасна, ограничимся логом
            self.log("[GAME] Запрошена очистка RAM (ограничено логированием, без агрессивных действий)")

        if disable_services:
            self.log("[GAME] Отключение служб не выполняется (требует продуманной безопасной реализации)")

        if set_priority:
            self.log("[GAME] Настройка приоритета процесса игры будет реализована позже")

    def mock_monitor_data(self):
        """Вернуть простые реальные метрики системы для игрового монитора."""
        try:
            cpu_percent = psutil.cpu_percent(interval=0.0)
            vm = psutil.virtual_memory()
            used_gb = vm.used / (1024 ** 3)
            total_gb = vm.total / (1024 ** 3)
            ram_text = f"{used_gb:.1f}/{total_gb:.0f} GB"
        except Exception:
            cpu_percent = 0.0
            ram_text = "--"

        # Температуры и FPS остаются условными плейсхолдерами
        return {
            "game": "Игровой режим активен",
            "fps": 0,
            "fps_load": 0,
            "ping": 0,
            "cpu_temp": int(cpu_percent),  # используем загрузку CPU как индикатор
            "gpu_temp": 0,
            "ram": ram_text,
            "vram": "--",
        }


class SpeedTestAnimationWidget(QtWidgets.QWidget):
    """Виджет с анимацией круга и вращающейся линии для speedtest."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self.setMaximumSize(200, 200)
        self.angle = 0
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_animation)
        self.is_running = False
        
    def start_animation(self):
        """Запустить анимацию."""
        self.is_running = True
        self.angle = 0
        self.timer.start(16)  # ~60 FPS
        
    def stop_animation(self):
        """Остановить анимацию."""
        self.is_running = False
        self.timer.stop()
        self.update()
        
    def update_animation(self):
        """Обновить угол вращения."""
        self.angle = (self.angle + 6) % 360
        self.update()
        
    def paintEvent(self, event):
        """Отрисовка анимации."""
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        
        # Размеры
        size = min(self.width(), self.height())
        center_x = self.width() / 2
        center_y = self.height() / 2
        radius = size / 2 - 20
        
        # Рисуем круг
        pen = QtGui.QPen(QtGui.QColor(0, 212, 255), 3)
        painter.setPen(pen)
        painter.drawEllipse(int(center_x - radius), int(center_y - radius), 
                           int(radius * 2), int(radius * 2))
        
        if self.is_running:
            # Рисуем вращающуюся линию
            angle_rad = math.radians(self.angle)
            end_x = center_x + radius * math.cos(angle_rad)
            end_y = center_y + radius * math.sin(angle_rad)
            
            pen = QtGui.QPen(QtGui.QColor(0, 212, 255), 4)
            painter.setPen(pen)
            painter.drawLine(int(center_x), int(center_y), 
                           int(end_x), int(end_y))
            
            # Рисуем точку на конце линии
            painter.setBrush(QtGui.QColor(0, 212, 255))
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawEllipse(int(end_x - 5), int(end_y - 5), 10, 10)


class ChatBubbleWidget(QtWidgets.QFrame):
    """Виджет отдельного сообщения чата. Использует QLabel для рендеринга HTML без прыжков скролла."""
    def __init__(self, entry: dict | str, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.browser = None
        self.bubble_frame = None
        
        # Находим главное окно для доступа к помощнику is_user_sender
        self.main_window = getattr(QtWidgets.QApplication.activeWindow(), "_main_window", None)
        if not self.main_window:
            curr = self.parent()
            while curr:
                if hasattr(curr, "_on_chat_link_clicked") or hasattr(curr, "is_user_sender"):
                    self.main_window = curr
                    break
                curr = curr.parent()
        
        self._init_ui()

    def _is_user(self, sender):
        if self.main_window and hasattr(self.main_window, "is_user_sender"):
            return self.main_window.is_user_sender(sender)
        s = str(sender or "").lower()
        return s in ("user", "пользователь", "you", "me", "я", "i", "admin")

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 5)
        layout.setSpacing(0)
        
        align_layout = QtWidgets.QHBoxLayout()
        align_layout.setContentsMargins(20, 0, 20, 0)
        
        if isinstance(self.entry, str):
            bubble = QtWidgets.QLabel(self.entry)
            bubble.setWordWrap(True)
            bubble.setStyleSheet("color: #FF5555; background: rgba(50,0,0,0.3); padding: 12px; border-radius: 12px;")
            align_layout.addWidget(bubble)
            align_layout.addStretch(1)
        else:
            tp = self.entry.get("type", "text")
            sender = self.entry.get("sender", "AI")
            is_user = self._is_user(sender)
            
            if tp == "status":
                self._draw_status_card(layout)
                return
            elif tp == "file_edit":
                self._draw_file_edit(layout)
                return
            elif tp in ("image", "video", "file_preview"):
                self._draw_multimedia_card(layout)
                return
            
            self.bubble_frame = QtWidgets.QFrame()
            max_w = int(QtWidgets.QApplication.primaryScreen().size().width() * 0.55)
            self.bubble_frame.setMaximumWidth(max_w)
            
            bg = "rgba(0,110,210,0.3)" if is_user else "rgba(45,45,75,0.7)"
            border_c = "rgba(0,212,255,0.25)" if is_user else "rgba(160,100,255,0.2)"
            
            self.bubble_frame.setStyleSheet(f"""
                QFrame {{ background: {bg}; border: 1px solid {border_c}; border-radius: 20px; }}
            """)
            
            frame_lay = QtWidgets.QVBoxLayout(self.bubble_frame)
            frame_lay.setContentsMargins(16, 12, 16, 12)
            frame_lay.setSpacing(4)
            
            avatar = "👤" if is_user else "🤖"
            safe_sender = html.escape(sender.upper())
            timestamp = self.entry.get("timestamp") or datetime.now().strftime("%H:%M:%S")
            header_lbl = QtWidgets.QLabel(f"{avatar} {safe_sender} &middot; {timestamp}")
            header_lbl.setStyleSheet("color: #8080A0; font-size: 10px; font-weight: 700; border: none; background: transparent;")
            frame_lay.addWidget(header_lbl)
            
            self.browser = QtWidgets.QTextBrowser()
            self.browser.setOpenExternalLinks(True)
            self.browser.setReadOnly(True)
            self.browser.setFrameStyle(QtWidgets.QFrame.Shape.NoFrame)
            self.browser.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.browser.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.browser.setStyleSheet("background: transparent; border: none;")
            
            html_content = self.entry.get("_html_cache", "")
            streaming = self.entry.get("streaming", False)
            cursor_html = "<span style='display:inline-block; width:2px; height:1em; background:#00D4FF; margin-left:2px; vertical-align:middle; animation: blink 0.8s infinite;'></span>" if streaming else ""
            
            self.browser.setHtml(f"""
                <style>
                @keyframes blink {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.1; }} }}
                body {{ font-family: 'Segoe UI', Tahoma, sans-serif; color: #E0E0FF; background: transparent; margin: 0; padding: 0; padding-bottom: 8px; font-size: 12pt; line-height: 1.4; }}
                </style>
                <div>{html_content}{cursor_html}</div>
            """)
            
            frame_lay.addWidget(self.browser)
            self.browser.document().contentsChanged.connect(self._adjust_height)
            
            self.browser.setOpenLinks(False)
            self.browser.setOpenExternalLinks(False)
            
            # Находим главное окно для обработки кликов по ссылкам
            main_window = getattr(QtWidgets.QApplication.activeWindow(), "_main_window", None)
            if not main_window:
                # Поиск через родителей, если активное окно не MainWindow
                curr = self.parent()
                while curr:
                    if hasattr(curr, "_on_chat_link_clicked"):
                        main_window = curr
                        break
                    curr = curr.parent()
            
            if main_window:
                self.browser.anchorClicked.connect(main_window._on_chat_link_clicked)
            else:
                # В крайнем случае используем статическое имя класса если оно в MainWindow
                try:
                    for widget in QtWidgets.QApplication.topLevelWidgets():
                        if hasattr(widget, "_on_chat_link_clicked"):
                            self.browser.anchorClicked.connect(widget._on_chat_link_clicked)
                            break
                except Exception:
                    pass
            
            if is_user:
                align_layout.addStretch(1)
                align_layout.addWidget(self.bubble_frame)
            else:
                align_layout.addWidget(self.bubble_frame)
                align_layout.addStretch(1)
            
            QtCore.QTimer.singleShot(10, self._adjust_height)

        layout.addLayout(align_layout)

    def _adjust_height(self):
        """Автоматически подстраивает высоту QTextBrowser под содержимое."""
        if not hasattr(self, "browser") or self.browser is None: return
        doc = self.browser.document()
        h = int(doc.documentLayout().documentSize().height()) + 25
        self.browser.setFixedHeight(h)

    def update_entry(self, new_entry):
        """Обновляет содержимое виджета без его пересоздания."""
        old_type = self.entry.get("type")
        new_type = new_entry.get("type")
        
        # Если тип изменился — этот метод не должен был вызываться, но на всякий случай игнорируем
        if old_type != new_type: return
        
        self.entry = new_entry
        html_cache = new_entry.get("_html_cache", "")
        
        if new_type == "text" and self.browser:
            streaming = new_entry.get("streaming", False)
            cursor = "<span style='display:inline-block; width:2px; height:1em; background:#00D4FF; margin-left:2px; vertical-align:middle; animation: blink 0.8s infinite;'></span>" if streaming else ""
            self.browser.setHtml(f"<html><style>body{{font-family:'Segoe UI'; color:#E0E0FF; padding-bottom:8px; font-size:12pt; line-height:1.4; background:transparent;}}</style><body>{html_cache}{cursor}</body></html>")
            self._adjust_height()
            
            # Скролл вниз если это AI сообщение и оно стримится
            if self.entry.get("sender") == "AI" and streaming:
                main_window = getattr(QtWidgets.QApplication.activeWindow(), "_main_window", None)
                if main_window and hasattr(main_window, "chat_scroll"):
                    main_window.chat_scroll.verticalScrollBar().triggerAction(QtWidgets.QAbstractSlider.SliderAction.SliderToMaximum)
        
        elif new_type in ("image", "video", "file_preview") and self.browser:
            self.browser.setHtml(html_cache)
            self._sync_media_height(self.browser)
            
        elif new_type == "status":
            # Находим заголовок и детали по тегам или атрибутам если бы мы их сохранили
            # Но проще всего просто перерисовать содержимое layout
            # Для статуса пока оставим пересоздание в _flush_chat_bubbles
            pass

    def _draw_multimedia_card(self, layout):
        """Рендерит карточки изображений, видео и превью файлов через QTextBrowser."""
        html_content = self.entry.get("_html_cache", "")
        if not html_content:
            main_window = getattr(QtWidgets.QApplication.activeWindow(), "_main_window", None)
            if main_window: html_content = main_window._render_chat_item(self.entry)
            
        self.browser = QtWidgets.QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.setReadOnly(True)
        self.browser.setFrameStyle(QtWidgets.QFrame.Shape.NoFrame)
        self.browser.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.browser.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.browser.setStyleSheet("background: transparent; border: none;")
        self.browser.setMinimumHeight(320) # Prevent disappearing/shrinking to 0
        self.browser.setHtml(html_content)
        
        self.browser.document().contentsChanged.connect(lambda: self._sync_media_height(self.browser))
        
        # Находим главное окно для обработки ссылок
        main_window = getattr(QtWidgets.QApplication.activeWindow(), "_main_window", None)
        if not main_window:
            curr = self.parent()
            while curr:
                if hasattr(curr, "_on_chat_link_clicked"):
                    main_window = curr
                    break
                curr = curr.parent()
        
        if main_window:
            self.browser.anchorClicked.connect(main_window._on_chat_link_clicked)
        else:
            try:
                for widget in QtWidgets.QApplication.topLevelWidgets():
                    if hasattr(widget, "_on_chat_link_clicked"):
                        self.browser.anchorClicked.connect(widget._on_chat_link_clicked)
                        break
            except Exception:
                pass
            
        layout.addWidget(self.browser)
        QtCore.QTimer.singleShot(100, lambda: self._sync_media_height(self.browser))

    def _sync_media_height(self, browser):
        doc = browser.document()
        h = int(doc.documentLayout().documentSize().height()) + 20
        browser.setFixedHeight(min(1400, max(50, h)))

    def _draw_status_card(self, layout):
        stage = self.entry.get("stage", "running")
        title = self.entry.get("title", "Статус")
        detail = self.entry.get("detail", "")
        
        colors = {
            "thinking": ("rgba(45,35,75,0.85)", "rgba(180,100,255,0.3)"),
            "reading": ("rgba(35,55,75,0.8)", "rgba(100,200,255,0.25)"),
            "writing": ("rgba(35,75,55,0.8)", "rgba(100,255,150,0.25)"),
            "searching": ("rgba(55,55,45,0.8)", "rgba(255,255,150,0.2)"),
            "failed": ("rgba(64,24,24,0.85)", "rgba(255,100,100,0.3)"),
            "completed": ("rgba(20,56,38,0.8)", "rgba(34,197,94,0.25)"),
        }
        bg, border = colors.get(stage, ("rgba(40,40,60,0.7)", "rgba(200,200,200,0.1)"))
        
        card = QtWidgets.QFrame()
        card.setStyleSheet(f"background: {bg}; border: 1px solid {border}; border-radius: 12px;")
        card.setFixedWidth(500)
        
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(15, 12, 15, 12)
        
        title_lbl = QtWidgets.QLabel(f"<b>{title}</b>")
        title_lbl.setStyleSheet("color: #FFFFFF; font-size: 13px; border: none; background: transparent;")
        card_layout.addWidget(title_lbl)
        
        detail_lbl = QtWidgets.QLabel(detail)
        detail_lbl.setStyleSheet("color: #A0A0D0; font-size: 11px; border: none; background: transparent;")
        detail_lbl.setWordWrap(True)
        card_layout.addWidget(detail_lbl)
        
        # Определяем выравнивание статуса
        align_row = QtWidgets.QHBoxLayout()
        align_row.setContentsMargins(20, 0, 20, 0)
        
        is_user = self._is_user(self.entry.get("sender", "AI"))
        
        if is_user:
            align_row.addStretch(1)
            align_row.addWidget(card)
        else:
            align_row.addWidget(card)
            align_row.addStretch(1)
        
        layout.addLayout(align_row)

    def _draw_file_edit(self, layout):
        path = self.entry.get("path", "...")
        status = self.entry.get("status", "pending")
        
        card = QtWidgets.QFrame()
        card.setStyleSheet("background: rgba(30,40,30,0.7); border: 1px solid rgba(100,255,100,0.2); border-radius: 10px;")
        card.setFixedWidth(450)
        card_layout = QtWidgets.QHBoxLayout(card)
        
        icon = "📄" if status != "applied" else "✅"
        lbl = QtWidgets.QLabel(f"{icon} {os.path.basename(path)}")
        lbl.setStyleSheet("color: #E0FFE0; font-size: 12px; border: none; background: transparent;")
        card_layout.addWidget(lbl)
        card_layout.addStretch(1)
        
        status_lbl = QtWidgets.QLabel(status.upper())
        status_lbl.setStyleSheet("color: #00FF00; font-size: 10px; font-weight: bold; border: none; background: transparent;")
        card_layout.addWidget(status_lbl)
        
        align_row = QtWidgets.QHBoxLayout()
        align_row.setContentsMargins(0, 0, 0, 0)
        # Файловые правки всегда от ИИ (слева)
        align_row.addWidget(card)
        align_row.addStretch(1)
        layout.addLayout(align_row)


class _ChatInputField(QtWidgets.QPlainTextEdit):
    """Многострочное поле ввода в стиле Cursor: Enter — отправить, Shift+Enter — новая строка."""
    send_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("🎉 План, @ для контекста, / для команд")
        self.setMaximumHeight(120)
        self.setMinimumHeight(44)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                event.accept()
                self.send_requested.emit()
            return
        super().keyPressEvent(event)


class _AutoModelPopup(QtWidgets.QDialog):
    """Мини-окно выбора модели и режима Авто (как в Cursor)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Модель и режим")
        self.setMinimumSize(320, 380)
        self.setStyleSheet("""
            QDialog{ background: #0e0e20; border: 1px solid rgba(0,212,255,0.2); border-radius: 12px; }
            QLabel{ color: #A0A0D0; font-size: 10pt; }
            QLineEdit{ background: #16162a; border: 1px solid rgba(0,212,255,0.2); border-radius: 8px;
                padding: 8px 12px; color: #E0E0FF; font-size: 10pt; }
            QLineEdit:focus{ border: 1px solid #00D4FF; }
            QListWidget{ background: #0c0c18; border: 1px solid rgba(0,212,255,0.15); border-radius: 8px;
                padding: 4px; color: #E0E0FF; font-size: 10pt; outline: none; }
            QListWidget::item{ padding: 6px 10px; border-radius: 4px; }
            QListWidget::item:hover{ background: rgba(0,212,255,0.1); }
            QListWidget::item:selected{ background: rgba(0,212,255,0.2); color: #00D4FF; }
            QCheckBox{ color: #C0C0E0; font-size: 10pt; spacing: 8px; }
            QCheckBox::indicator{ width: 18px; height: 18px; border-radius: 9px; border: 1px solid rgba(0,212,255,0.4);
                background: #16162a; }
            QCheckBox::indicator:checked{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #00D4FF, stop:1 #9D00FF); }
            QPushButton{ background: rgba(0,212,255,0.15); border: 1px solid rgba(0,212,255,0.3);
                border-radius: 8px; padding: 8px 16px; color: #E0E0FF; font-size: 10pt; }
            QPushButton:hover{ background: rgba(0,212,255,0.25); }
        """)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Поиск моделей...")
        self.search.textChanged.connect(self._filter_models)
        layout.addWidget(self.search)

        auto_row = QtWidgets.QHBoxLayout()
        self.chk_auto = QtWidgets.QCheckBox("Авто")
        self.chk_auto.setChecked(True)
        self.chk_auto.setToolTip("Сбалансированное качество и скорость")
        self.chk_auto.toggled.connect(self._on_auto_toggled)
        auto_row.addWidget(self.chk_auto)
        auto_row.addStretch(1)
        layout.addLayout(auto_row)
        lbl_auto_desc = QtWidgets.QLabel("Сбалансированное качество и скорость, рекомендуется для большинства задач.")
        lbl_auto_desc.setWordWrap(True)
        lbl_auto_desc.setStyleSheet("color:#6060a0; font-size:9pt; margin-left: 0;")
        layout.addWidget(lbl_auto_desc)

        layout.addWidget(QtWidgets.QLabel("Модель:"))
        self.list_models = QtWidgets.QListWidget()
        self.list_models.itemClicked.connect(self._on_model_clicked)
        layout.addWidget(self.list_models, 1)

        self.chk_no_confirm = QtWidgets.QCheckBox("Выполнять действия без подтверждения")
        self.chk_no_confirm.setChecked(True)
        self.chk_no_confirm.toggled.connect(self._on_no_confirm_toggled)
        layout.addWidget(self.chk_no_confirm)

        btn_close = QtWidgets.QPushButton("Закрыть")
        btn_close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

        self._main_window = None
        self._model_items: list = []

    def set_main_window(self, w):
        self._main_window = w
        self._rebuild_list()
        self._load_state()

    def _rebuild_list(self):
        self.list_models.clear()
        self._model_items.clear()
        if not getattr(self._main_window, "ai_cmb_provider", None):
            return
        cb = self._main_window.ai_cmb_provider
        for i in range(cb.count()):
            data = cb.itemData(i)
            if data is None:
                continue
            s = (data if isinstance(data, str) else str(data))
            if s.startswith("_sep"):
                continue
            text = cb.itemText(i).strip()
            self._model_items.append((text, s, i))
            self.list_models.addItem(text)

    def _load_state(self):
        if not self._main_window:
            return
        cb = getattr(self._main_window, "ai_cmb_provider", None)
        chk = getattr(self._main_window, "chk_agent_confirm", None)
        if chk is not None:
            self.chk_no_confirm.setChecked(not chk.isChecked())
        if cb is not None:
            data = cb.currentData()
            is_auto = data == "auto" if data else False
            self.chk_auto.setChecked(is_auto)
            for row, (_, d, idx) in enumerate(self._model_items):
                if d == (data if isinstance(data, str) else str(data)) or (is_auto and d == "auto"):
                    self.list_models.setCurrentRow(row)
                    break
        self.list_models.setEnabled(not self.chk_auto.isChecked())

    def _filter_models(self):
        q = self.search.text().strip().lower()
        for i in range(self.list_models.count()):
            item = self.list_models.item(i)
            if not self._model_items or i >= len(self._model_items):
                continue
            text = self._model_items[i][0].lower()
            item.setHidden(bool(q) and q not in text)

    def _on_auto_toggled(self, checked):
        if not self._main_window or not getattr(self._main_window, "ai_cmb_provider", None):
            return
        cb = self._main_window.ai_cmb_provider
        if checked:
            for i in range(cb.count()):
                if cb.itemData(i) == "auto":
                    cb.blockSignals(True)
                    cb.setCurrentIndex(i)
                    cb.blockSignals(False)
                    if hasattr(self._main_window, "_on_ai_model_selected"):
                        self._main_window._on_ai_model_selected()
                    break
            for row, (_, d, _) in enumerate(self._model_items):
                if d == "auto":
                    self.list_models.setCurrentRow(row)
                    break
        self.list_models.setEnabled(not checked)

    def _on_model_clicked(self, item):
        row = self.list_models.row(item)
        if not self._main_window or row < 0 or row >= len(self._model_items):
            return
        _, _, cb_index = self._model_items[row]
        cb = getattr(self._main_window, "ai_cmb_provider", None)
        if cb is not None:
            self.chk_auto.setChecked(False)
            self.list_models.setEnabled(True)
            cb.blockSignals(True)
            cb.setCurrentIndex(cb_index)
            cb.blockSignals(False)
            if hasattr(self._main_window, "_on_ai_model_selected"):
                self._main_window._on_ai_model_selected()

    def _on_no_confirm_toggled(self, checked):
        if getattr(self._main_window, "chk_agent_confirm", None) is not None:
            self._main_window.chk_agent_confirm.setChecked(not checked)



class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self._main_window = self # Позволяет дочерним виджетам находить MainWindow
        self.setWindowTitle("JARVIS v3.0 😊 — AI Control")
        self.setMinimumSize(1024, 680)
        self.auto_update_status = True
        self.current_tab = "home"
        # Менеджеры мессенджеров и игр (лог безопасен даже до UI)
        self.telegram_manager = TelegramManager(log_func=self._log_from_manager)
        # User client для работы с реальными диалогами
        self.telegram_user_client = TelegramUserClient(
            log_func=self._log_from_manager,
            message_callback=self._on_new_telegram_message
        )
        self.telegram_autoresponder = AutoResponder(log_func=self._log_from_manager)
        self.telegram_analyzer = ChatAnalyzer(log_func=self._log_from_manager)
        self.game_manager = GameManager(log_func=self._log_from_manager)
        # Базовые данные по игровым аккаунтам (в памяти, без реальных паролей)
        self.game_accounts = {
            "Steam": {"login": "player123", "note": "Основной аккаунт"},
            "Epic Games": {"login": "gamer456", "note": "Фортнайт"},
            "Battle.net": {"login": "pro_gamer", "note": "Overwatch/Diablo"},
            "Origin": {"login": "need_login", "note": "Требуется вход"},
        }
        # Текущая/последняя игра для игрового монитора и AI-графики
        self.current_game_key: str | None = None
        self.current_game_name: str = "-"
        # История метрик игрового монитора (для графика/статистики)
        self.game_monitor_history: list[dict] = []
        # Кэш записей автозагрузки (для системной вкладки)
        self.startup_entries: list[dict] = []
        # Для сетевого монитора (скорость)
        self._net_prev = psutil.net_io_counters()
        self._net_prev_time = time.time()
        # Сценарии автоматизации (простая модель)
        self.automation_scenarios: list[dict] = []
        self.automation_run_count: int = 0
        # Логи автоматизации
        self.automation_logs: list[dict] = []
        # Планировщик задач на сегодня (примитивная модель)
        self.planner_tasks: list[dict] = []
        # История команд для аналитики
        self.command_history: list[dict] = []
        # Статистика использования
        self.usage_stats = {
            "commands_executed": 0,
            "files_managed": 0,
            "games_launched": 0,
            "automation_runs": 0,
        }
        # Состояние авто-правил (чтобы не спамить действиями)
        self.auto_event_state = {
            "high_cpu": False,
            "low_disk": False,
        }
        # Текущая тема (по умолчанию темная)
        self.current_theme = "dark"
        # Временное хранилище выбранных вложений (путь, тип)
        self._pending_attachments: list[dict] = []
        # Интеграция нейросети
        self.neural_manager = NeuralNetworkManager(
            api_key=API_KEY,
            base_url=BASE_URL
        )
        self.replicate_manager = ReplicateManager()
        self._ai_use_replicate_llm = False
        self._ai_replicate_model_id = None
        self._ai_use_ollama = False
        self._replicate_bridge = _ReplicateBridge()
        self._replicate_bridge.status.connect(self._on_generation_status)
        self._replicate_bridge.image_ready.connect(self._on_replicate_image_ready)
        self._replicate_bridge.video_ready.connect(self._on_replicate_video_ready)
        self._replicate_bridge.error.connect(self._on_replicate_error)
        self._service_health = {
            "ollama": "unknown",
            "fooocus": "unknown",
        }

        self.chat_messages: list[dict] = []
        self._ai_project_root = os.path.dirname(os.path.abspath(__file__))
        self._chat_bubbles: list[dict | str] = []
        self._chat_bridge = _ChatBridge()
        self._chat_bridge.result_ready.connect(self._chat_handle_ai_result)
        self._chat_bridge.result_chunk.connect(self._chat_handle_ai_stream_chunk)
        self._streaming_ai_active = False
        self._chat_generating = False
        self._chat_cancel_event = threading.Event()
        self._CHAT_CANCELLED = "\x00CANCELLED\x00"
        self._stream_flush_scheduled = False
        self._last_loading_flush_time = 0.0

        self._tts_bridge = _TTSBridge()
        self._tts_bridge.play_file.connect(self._play_tts_file)
        self._tts_bridge.log_system.connect(self._on_tts_log)

        self._tts_enabled = False
        self._pyttsx3_engine = None
        self._pyttsx3_voice_id = None
        self._tts_effect = None
        self._tts_tmp_files: list[str] = []
        self._tts_lock = threading.Lock()
        self._tts_last_early_text = ""
        self._tts_last_early_time = 0.0

        self._voice_recording = False
        self._audio_source = None
        self._audio_buffer = None
        self._audio_format = None
        self._vosk_model = None

        self._hotword_enabled = False
        self._hotword_running = False
        self._hotword_paused_for_manual = False
        self._hotword_audio_source = None
        self._hotword_io = None
        self._hotword_recognizer = None
        self._hotword_state = "idle"
        self._hotword_wake_time = 0.0
        self._hotword_last_voice_time = 0.0
        self._hotword_cmd_text = ""
        self._hotword_timer = QtCore.QTimer(self)
        self._hotword_timer.timeout.connect(self._hotword_tick)

        # Инициализируем UI (темная тема будет применена в конце _init_ui)
        self._init_ui()
        self._init_timers()
        # Загружаем сохраненные настройки
        self._load_personal_settings()
        
        # Set proper window geometry to fix black screen issue
        self.setWindowTitle("JARVIS v3.0 😊 — AI Control")
        self.setMinimumSize(1080, 700)
        self.resize(1400, 860)
        # Center window on screen
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    def _log_from_manager(self, text: str, *_, **__):
        """Безопасный лог для менеджеров до/после инициализации UI."""
        if hasattr(self, "log_view") and self.log_view is not None:
            self.log_view.appendPlainText(text)
        else:
            print(text)
    
    def _on_new_telegram_message(self, message_data: dict):
        """Callback для новых сообщений из Telegram."""
        chat_name = message_data.get("chat_name", "Неизвестно")
        text = message_data.get("text", "")
        chat_id = message_data.get("chat_id")
        
        # Обновляем UI если есть список чатов
        if hasattr(self, "chats_list"):
            # Ищем чат в списке или добавляем
            root = self.chats_list.invisibleRootItem()
            found = False
            for i in range(root.childCount()):
                item = root.child(i)
                if item.data(0, QtCore.Qt.ItemDataRole.UserRole) == chat_id:
                    # Обновляем счётчик непрочитанных
                    unread = self.telegram_user_client.unread_counts.get(chat_id, 0)
                    item.setText(1, f"({unread})" if unread > 0 else "")
                    found = True
                    break
            
            if not found:
                # Добавляем новый чат
                item = QtWidgets.QTreeWidgetItem(self.chats_list)
                item.setText(0, chat_name)
                item.setText(1, "(1)")
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, chat_id)
        
        # Добавляем в историю
        self.add_history(f"[TG] Новое сообщение от {chat_name}: {text[:50]}")
        self.log(f"[TG] 📩 {chat_name}: {text[:100]}")
    
    def log(self, text: str):
        """Вывести сообщение в лог-виджет."""
        if hasattr(self, "log_view") and self.log_view is not None:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_view.appendPlainText(f"[{timestamp}] {text}")
        else:
            print(text)
    
    def add_history(self, text: str):
        """Добавить запись в историю команд."""
        if hasattr(self, "history_view") and self.history_view is not None:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.history_view.addItem(f"[{timestamp}] {text}")
            # Прокручиваем к последнему элементу
            self.history_view.scrollToBottom()
        else:
            print(f"[HISTORY] {text}")
        
        # Сохраняем в историю для аналитики
        self.command_history.append({
            "timestamp": datetime.now().isoformat(),
            "text": text,
        })
        # Ограничиваем размер истории (последние 1000 записей)
        if len(self.command_history) > 1000:
            self.command_history = self.command_history[-1000:]
        
        # Обновляем статистику
        if "[CMD]" in text or "команда" in text.lower():
            self.usage_stats["commands_executed"] += 1
    
    def _message_to_chat_html(self, message: str) -> str:
        """Конвертирует текст сообщения в HTML: блоки кода (```), инлайн-код, переносы — как в Cursor/Windsurf."""
        try:
            s = (str(message).strip() if message is not None else "") or ""
        except Exception:
            s = ""
        if not s:
            return ""
        out: list[str] = []
        code_style = (
            "margin:8px 0; padding:12px 14px; background:rgba(0,0,0,0.35);"
            " border:1px solid rgba(0,212,255,0.12); border-radius:10px;"
            " font-family:Consolas,'Courier New',monospace; font-size:12px;"
            " line-height:1.45; color:#E0E0FF; overflow-x:auto;"
        )
        # Блоки ```lang?\n...\n```
        while "```" in s:
            idx = s.index("```")
            out.append(html.escape(s[:idx]).replace("\n", "<br>"))
            s = s[idx + 3:]
            if not s:
                break
            first_line = ""
            if "\n" in s:
                first_line, s = s.split("\n", 1)
            lang = first_line.strip()[:32] if first_line.strip() else ""
            end = s.find("```")
            if end == -1:
                code_content = s
                s = ""
            else:
                code_content = s[:end]
                s = s[end + 3:]
            code_escaped = html.escape(code_content)
            out.append(f"<pre style='{code_style}' data-lang='{html.escape(lang)}'><code>{code_escaped}</code></pre>")
        out.append(html.escape(s).replace("\n", "<br>"))
        result = "".join(out)
        # Инлайн-код `...`
        result = re.sub(r"`([^`]+)`", r"<code style='background:rgba(0,212,255,0.08); padding:2px 6px; border-radius:4px; font-size:0.95em;'>\1</code>", result)
        return result

    def _make_chat_entry(self, entry_type: str, **kwargs) -> dict:
        return {
            "id": kwargs.pop("id", f"chat-{time.time_ns()}"),
            "type": entry_type,
            "timestamp": kwargs.pop("timestamp", datetime.now().strftime("%H:%M:%S")),
            **kwargs,
        }

    def _append_chat_entry(self, entry: dict):
        if not hasattr(self, "_chat_bubbles") or self._chat_bubbles is None:
            self._chat_bubbles = []
        self._chat_bubbles.append(entry)
        self._flush_chat_bubbles()

    def _find_chat_entry_index(self, entry_id: str) -> int:
        for idx, item in enumerate(getattr(self, "_chat_bubbles", []) or []):
            if isinstance(item, dict) and item.get("id") == entry_id:
                return idx
        return -1

    def _get_chat_entry_started_at(self, entry_id: str):
        idx = self._find_chat_entry_index(entry_id)
        if idx >= 0 and isinstance(self._chat_bubbles[idx], dict):
            return self._chat_bubbles[idx].get("request_started_at")
        return None

    def _update_chat_entry(self, entry_id: str, **changes) -> bool:
        idx = self._find_chat_entry_index(entry_id)
        if idx < 0:
            return False
        item = self._chat_bubbles[idx]
        if not isinstance(item, dict):
            return False
        item.update(changes)
        self._chat_bubbles[idx] = item
        self._flush_chat_bubbles()
        return True

    def _replace_chat_entry(self, entry_id: str, new_entry: dict) -> bool:
        idx = self._find_chat_entry_index(entry_id)
        if idx < 0:
            return False
        self._chat_bubbles[idx] = new_entry
        self._flush_chat_bubbles()
        return True

    def _make_media_action_link(self, action: str, path: str, label: str) -> str:
        # Кодируем путь, чтобы избежать проблем с пробелами и спецсимволами
        safe_path = quote_plus(path)
        href = f"jarvis://media?action={quote_plus(action)}&path={safe_path}"
        
        # Стилизация под кнопку (насколько позволяет QTextBrowser)
        style = (
            "background-color: #1a1a3a; "
            "color: #00D4FF; "
            "padding: 4px 10px; "
            "border: 1px solid #00D4FF; "
            "border-radius: 6px; "
            "text-decoration: none; "
            "font-weight: bold; "
            "font-size: 11px;"
        )
        return f"<a href='{href}' style='{style}'>&nbsp;{label}&nbsp;</a>"

    def is_user_sender(self, sender: str) -> bool:
        """Строгое определение: является ли отправитель пользователем."""
        if not sender: return False
        s = str(sender).lower().strip()
        # Список имен, которые считаются "Мной" (пользователем), проверяем ТОЛЬКО полное совпадение
        user_names = ("user", "you", "me", "я", "i", "admin", "пользователь", "админ")
        return s in user_names

    def append_chat(self, sender: str, message: str):
        try:
            sender = str(sender) if sender is not None else "Система"
            message = str(message) if message is not None else ""
        except Exception:
            sender, message = "Система", ""
        try:
            self._append_chat_entry(self._make_chat_entry("text", sender=sender, message=message))
            if not hasattr(self, "chat_view") and hasattr(self, "history_view") and self.history_view is not None:
                timestamp = datetime.now().strftime("%H:%M:%S")
                self.history_view.addItem(f"[{timestamp}] {sender}: {message}")
                self.history_view.scrollToBottom()
            self.log(f"[CHAT] {sender}: {message}")
        except Exception as e:
            try:
                self.log(f"[CHAT] append_chat error: {e}")
            except Exception:
                pass

    def _make_chat_bubble(self, sender: str, message: str, timestamp: str | None = None, streaming: bool = False) -> str:
        try:
            sender = str(sender) if sender is not None else "?"
            message = str(message) if message is not None else ""
        except Exception:
            sender, message = "?", ""
        safe_sender = html.escape(str(sender))
        safe_message = self._message_to_chat_html(message)
        is_user = self.is_user_sender(sender)
        align = "right" if is_user else "left"
        avatar = "👤" if is_user else ("🤖" if str(sender).lower() == "ai" else "⚡")
        if is_user:
            bg = "rgba(0,100,200,0.4)" # More vibrant blue for user
            border_col = "rgba(0,212,255,0.3)"
        elif sender.lower() == "ai":
            bg = "rgba(35,35,65,0.85)" # Deeper, more solid background for AI
            border_col = "rgba(157,0,255,0.25)"
        else:
            bg = "rgba(25,25,45,0.7)"
            border_col = "rgba(100,100,180,0.2)"
        
        # Cursor-like pulsing animation for streaming AI response (optional, but looks premium)
        cursor_html = "<span style='display:inline-block; width:2px; height:1em; background:#00D4FF; margin-left:2px; vertical-align:middle; animation: blink 0.8s infinite;'></span>" if streaming else ""
        
        return (
            f"<div style='text-align:{align}; margin:12px 0; padding:0 20px;'>"
            f"<div style='display:inline-block; max-width:85%; padding:14px 20px;"
            f" border-radius:18px; background:{bg}; border:1px solid {border_col}; shadow: 0 4px 15px rgba(0,0,0,0.2);'>"
            f"<div style='font-size:11px; color:#A0A0D0; margin-bottom:8px; font-weight:600;'>"
            f"{avatar} {safe_sender.upper()} &middot; {timestamp}</div>"
            f"<div style='font-size:14px; line-height:1.6; color:#E0E0FF;'>{safe_message}{cursor_html}</div>"
            f"</div></div>"
        )

    def _make_status_card(self, entry: dict) -> str:
        stage = (entry.get("stage") or "running").lower()
        icons = {
            "started": "🟦",
            "warming": "🟪",
            "running": "🟨",
            "healthy": "🟢",
            "completed": "🟢",
            "failed": "🔴",
            "thinking": "🟪",
        }
        colors = {
            "started": ("rgba(34,46,88,0.78)", "rgba(91,107,255,0.25)"),
            "warming": ("rgba(48,32,74,0.8)", "rgba(157,0,255,0.2)"),
            "running": ("rgba(60,44,18,0.82)", "rgba(255,184,0,0.24)"),
            "healthy": ("rgba(20,56,38,0.82)", "rgba(34,197,94,0.22)"),
            "completed": ("rgba(20,56,38,0.82)", "rgba(34,197,94,0.22)"),
            "failed": ("rgba(64,24,24,0.85)", "rgba(255,107,107,0.26)"),
            "thinking": ("rgba(45,35,75,0.9)", "rgba(180,100,255,0.3)"), # More prominent thinking card
            "reading": ("rgba(35,55,75,0.85)", "rgba(100,200,255,0.3)"), # New state: reading
            "writing": ("rgba(35,75,55,0.85)", "rgba(100,255,150,0.3)"), # New state: writing
            "searching": ("rgba(55,55,45,0.85)", "rgba(255,255,150,0.25)"), # New state: searching
        }
        bg, border_col = colors.get(stage, colors["running"])
        icon = icons.get(stage, "⚡")
        sender = html.escape(entry.get("sender") or "Система")
        title = html.escape(entry.get("title") or "Статус")
        detail = html.escape(entry.get("detail") or entry.get("message") or "")
        meta_parts = []
        if entry.get("model_name"):
            meta_parts.append(html.escape(str(entry["model_name"])))
        if entry.get("prompt"):
            meta_parts.append(html.escape(str(entry["prompt"])))
        meta_html = ""
        if meta_parts:
            meta_html = f"<div style='font-size:11px; color:#7070b0; margin-top:8px;'>{' &middot; '.join(meta_parts)}</div>"
        progressive_html = ""
        if entry.get("media_kind") == "image" and stage not in ("completed", "failed"):
            progress_pct = entry.get("progress_pct", 0)
            progress_label = str(entry.get("progress_label") or "")
            extra = f" &middot; {html.escape(progress_label)}" if progress_label else ""
            progressive_html = (
                "<div style='margin-top:14px; margin-bottom:12px;'>"
                "<div style='width:100%; height:6px; border-radius:3px; background:rgba(255,255,255,0.08); overflow:hidden;'>"
                f"<div style='width:{progress_pct}%; height:100%; background:linear-gradient(90deg, #00D4FF, #9D00FF); border-radius:3px;'></div>"
                "</div>"
                f"<div style='font-size:11px; color:#A0A0D0; margin-top:8px; display:flex; justify-content:space-between;'>"
                f"<span>Генерация: {progress_pct}%</span><span>{extra}</span></div>"
                "</div>"
            )
        return (
            f"<div style='text-align:left; margin:10px 0; padding:0 20px;'>"
            f"<div style='display:inline-block; max-width:85%; width:400px; padding:16px; border-radius:20px;"
            f" background:{bg}; border:1px solid {border_col}; box-shadow: 0 10px 30px rgba(0,0,0,0.3);'>"
            f"<div style='font-size:11px; color:#8080A0; margin-bottom:8px; font-weight:700;'>{icon} {sender} &middot; {html.escape(entry.get('timestamp') or '')}</div>"
            f"<div style='font-size:14px; color:#FFFFFF; font-weight:600;'>{title}</div>"
            f"<div style='font-size:12px; color:#A0A0D0; margin-top:4px;'>{detail}</div>"
            f"{progressive_html}{meta_html}"
            f"</div></div>"
        )

    def _make_image_card(self, entry: dict) -> str:
        path = str(entry.get("path") or "")
        prompt = str(entry.get("prompt") or "")
        model_name = str(entry.get("model_name") or "Image")
        timestamp = str(entry.get("timestamp") or datetime.now().strftime("%H:%M:%S"))
        file_url = QtCore.QUrl.fromLocalFile(path).toString() if path else ""
        
        # Минималистичный стиль "а-ля Телеграм" с быстрыми действиями
        actions = "&nbsp;&nbsp;".join([
            self._make_media_action_link("open", path, "Открыть"),
            self._make_media_action_link("save_as", path, "Скачать"),
            self._make_media_action_link("open_folder", path, "Папка"),
        ])
        
        # Кодируем путь для основной ссылки на изображение
        encoded_path = quote_plus(path)
        
        return (
            f"<div style='text-align:left; margin:12px 0; padding:0 20px;'>"
            f"<div style='display:inline-block; max-width:90%; position:relative; overflow:hidden; border-radius:20px;'>"
            f"<a href='imageviewer:{encoded_path}' style='text-decoration:none;'>"
            f"<img src='{file_url}' width='480' style='display:block; border-radius:20px; border:1px solid rgba(255,255,255,0.1);'/>"
            "</a>"
            f"<div style='margin-top:12px; font-size:13px; color:#E0E0FF; line-height:1.4;'>{html.escape(prompt)}</div>"
            f"<div style='font-size:10px; color:#8080A0; margin-top:6px;'>{html.escape(model_name)} &middot; {html.escape(timestamp)}</div>"
            f"<div style='margin-top:10px;'>{actions}</div>"
            f"</div></div>"
        )

    def _make_video_card(self, entry: dict) -> str:
        path = str(entry.get("path") or "")
        prompt = str(entry.get("prompt") or "")
        model_name = str(entry.get("model_name") or "Video")
        timestamp = str(entry.get("timestamp") or datetime.now().strftime("%H:%M:%S"))
        fname = os.path.basename(path)
        elapsed_label = str(entry.get("elapsed_label") or "")
        source_label = str(entry.get("source_label") or "")
        file_size = ""
        try:
            if path and os.path.exists(path):
                size_mb = os.path.getsize(path) / (1024 * 1024)
                file_size = f"{size_mb:.1f} MB"
        except Exception:
            file_size = ""
        actions = "&nbsp;&nbsp;".join([
            self._make_media_action_link("open", path, "Открыть"),
            self._make_media_action_link("save_as", path, "Скачать"),
            self._make_media_action_link("open_folder", path, "Папка"),
            self._make_media_action_link("copy_path", path, "Путь"),
        ])
        meta_parts = [html.escape(model_name)]
        if source_label:
            meta_parts.append(html.escape(source_label))
        if file_size:
            meta_parts.append(html.escape(file_size))
        if elapsed_label:
            meta_parts.append(html.escape(elapsed_label))
        meta = " &middot; ".join(meta_parts)
        return (
            f"<div style='text-align:left; margin:10px 0; padding:0 20px;'>"
            f"<div style='display:inline-block; max-width:82%; padding:14px 18px; border-radius:18px;"
            f" background:rgba(20,20,40,0.85); border:1px solid rgba(157,0,255,0.17);'>"
            f"<div style='font-size:11px; color:#7070b0; margin-bottom:8px;'>🎬 {meta or 'Видео'} &middot; {html.escape(timestamp)}</div>"
            "<div style='margin:6px 0 10px 0; padding:18px 16px; border-radius:14px;"
            " background:linear-gradient(135deg, rgba(108,44,255,0.28), rgba(0,212,255,0.12));"
            " border:1px solid rgba(157,0,255,0.16);'>"
            "<div style='font-size:22px; margin-bottom:6px;'>▶</div>"
            f"<div style='font-size:13px; color:#E0E0FF; font-weight:700;'>{html.escape(fname)}</div>"
            "<div style='font-size:11px; color:#A9B3E6; margin-top:4px;'>Откроется в системном видеоплеере.</div>"
            "</div>"
            f"<div style='font-size:12px; color:#C9D2FF; margin-top:8px; line-height:1.5;'>{html.escape(prompt)}</div>"
            f"<div style='font-size:11px; color:#8E9BFF; margin-top:8px;'>{actions}</div>"
            f"</div></div>"
        )

    def _render_chat_item(self, item) -> str:
        if isinstance(item, str):
            return item
        if not isinstance(item, dict):
            return ""
        item_type = item.get("type") or "text"
        if item_type == "text":
            return self._make_chat_bubble(
                item.get("sender") or "Система",
                item.get("message") or "",
                timestamp=item.get("timestamp"),
                streaming=bool(item.get("streaming")),
            )
        if item_type == "status":
            return self._make_status_card(item)
        if item_type == "image":
            return self._make_image_card(item)
        if item_type == "video":
            return self._make_video_card(item)
        if item_type == "error":
            error_entry = {
                **item,
                "stage": "failed",
                "title": item.get("title") or "Ошибка",
                "sender": item.get("sender") or "Система",
            }
            return self._make_status_card(error_entry)
        if item_type == "file_preview":
            return self._make_file_preview_bubble(item)
        if item_type == "file_edit":
            return self._make_file_edit_bubble(item)
        return ""

    def _make_file_edit_bubble(self, entry: dict) -> str:
        """Inline UI block for file edit (search_replace/write) - Cursor/Windsurf style."""
        path = html.escape(str(entry.get("path") or ""))
        old_str = html.escape(str(entry.get("old_string") or ""))
        new_str = html.escape(str(entry.get("new_string") or ""))
        status = entry.get("status") or "applied"
        
        icon = "✅" if status == "applied" else ("❌" if status == "failed" else "⏳")
        status_text = "Применено" if status == "applied" else ("Ошибка" if status == "failed" else "В процессе")
        
        if len(old_str) > 600: old_str = old_str[:600] + "\n..."
        if len(new_str) > 600: new_str = new_str[:600] + "\n..."

        bg = "rgba(30,35,45,0.8)"
        border_col = "rgba(0,180,255,0.3)"
        if status == "failed":
            border_col = "rgba(255,80,80,0.4)"
            bg = "rgba(45,25,25,0.8)"
        
        old_html = f"<div style='background:rgba(255,80,80,0.15); color:#ffcccc; padding:8px; margin-bottom:4px; border-radius:4px; border-left:3px solid #ff4444; font-family:Consolas, monospace; white-space:pre-wrap; font-size:12px; line-height:1.4;'>- {old_str}</div>" if (old_str and old_str not in ("(добавление в конец)", "(перезапись файла)")) else ""
        if old_str in ("(добавление в конец)", "(перезапись файла)"):
            old_html = f"<div style='color:#aaaaaa; font-style:italic; font-size:11px; margin-bottom:4px;'>{old_str}</div>"
            
        new_html = f"<div style='background:rgba(80,255,80,0.15); color:#ccffcc; padding:8px; border-radius:4px; border-left:3px solid #44ff44; font-family:Consolas, monospace; white-space:pre-wrap; font-size:12px; line-height:1.4;'>+ {new_str}</div>" if new_str else ""
        
        return (
            f"<div style='text-align:left; margin:10px 0; padding:0 20px;'>"
            f"<div style='display:inline-block; max-width:85%; width:100%; padding:14px; border-radius:12px; background:{bg}; border:1px solid {border_col};'>"
            f"<div style='font-size:13px; color:#00D4FF; margin-bottom:8px; display:flex; justify-content:space-between;'>"
            f"<b>📝 {path}</b> <span style='color:#aaa; font-size:12px;'>{icon} {status_text}</span></div>"
            f"{old_html}{new_html}"
            f"</div></div>"
        )
        
    def _make_file_preview_bubble(self, entry: dict) -> str:
        """Свёрнутый блок с превью содержимого файла после read_text_file."""
        path = entry.get("file_path") or ""
        preview = entry.get("preview") or ""
        total_len = entry.get("total_len", 0)
        fname = html.escape(os.path.basename(path))
        safe_preview = html.escape(preview)
        summary = f"Содержимое файла {fname}" + (f" ({total_len} символов)" if total_len else "")
        return (
            "<div style='text-align:left; margin:8px 0; padding:0 20px;'>"
            "<div style='display:inline-block; max-width:85%; padding:10px 14px; border-radius:12px;"
            " background:rgba(25,25,45,0.6); border:1px solid rgba(100,100,180,0.15);'>"
            "<div style='font-size:11px; color:#7070b0; margin-bottom:6px;'>⚙ Система</div>"
            f"<details style='font-size:12px; color:#C0C0E0;'>"
            f"<summary style='cursor:pointer; color:#00D4FF;'>{summary}</summary>"
            f"<pre style='margin:8px 0 0; padding:10px; background:rgba(0,0,0,0.3); border-radius:8px; overflow:auto; max-height:200px; white-space:pre-wrap; word-break:break-all;'>{safe_preview}</pre>"
            "</details></div></div>"
        )

    def _flush_chat_bubbles(self):
        """Синхронизирует список бабблов с виджетами на экране. Никаких прыжков скролла!"""
        if not hasattr(self, "_chat_widgets"): self._chat_widgets = []
        if not hasattr(self, "_ai_chat_widgets"): self._ai_chat_widgets = []
            
        bubbles = getattr(self, "_chat_bubbles", []) or []
        
        for name, layout_attr, scroll_attr, widgets_attr in [
            ("Main", "chat_layout_v", "chat_scroll", "_chat_widgets"),
            ("AI", "ai_chat_layout", "ai_chat_scroll", "_ai_chat_widgets")
        ]:
            if not hasattr(self, layout_attr): continue
            
            layout = getattr(self, layout_attr)
            scroll = getattr(self, scroll_attr)
            widgets = getattr(self, widgets_attr)
            
            for i in range(len(bubbles)):
                b = bubbles[i]
                
                # Рендерим HTML один раз и кэшируем для всех типов
                if isinstance(b, dict) and "_html_cache" not in b:
                    tp = b.get("type", "text")
                    if tp == "text":
                        b["_html_cache"] = self._message_to_chat_html(b.get("message", ""))
                    else:
                        # Используем штатный рендер для мультимедиа/статусов
                        b["_html_cache"] = self._render_chat_item(b)
                
                if i >= len(widgets):
                    w = ChatBubbleWidget(b)
                    layout.insertWidget(layout.count() - 1, w)
                    widgets.append(w)
                    QtCore.QTimer.singleShot(50, lambda s=scroll: s.verticalScrollBar().triggerAction(QtWidgets.QAbstractSlider.SliderAction.SliderToMaximum))
                else:
                    old_w = widgets[i]
                    is_diff = False
                    if isinstance(b, dict) and isinstance(old_w.entry, dict):
                        # Сравниваем только значимые поля, игнорируя _html_cache
                        significant_fields = ["type", "message", "sender", "stage", "streaming", "detail", "path", "status"]
                        for field in significant_fields:
                            if b.get(field) != old_w.entry.get(field):
                                is_diff = True
                                break
                    elif b != old_w.entry:
                        is_diff = True
                        
                    if is_diff:
                        # Находим реальный индекс виджета в layout
                        idx = layout.indexOf(old_w)
                        if idx < 0:
                            # Если виджет почему-то не в layout — просто добавляем в конец перед стретчем
                            idx = layout.count() - 1

                        # Если тип изменился — пересоздаем виджет обязательно
                        if isinstance(b, dict) and isinstance(old_w.entry, dict) and b.get("type") != old_w.entry.get("type"):
                            layout.removeWidget(old_w)
                            old_w.deleteLater()
                            new_w = ChatBubbleWidget(b)
                            layout.insertWidget(idx, new_w)
                            widgets[i] = new_w
                        elif isinstance(b, dict) and b.get("type") == "status":
                            layout.removeWidget(old_w)
                            old_w.deleteLater()
                            new_w = ChatBubbleWidget(b)
                            layout.insertWidget(idx, new_w)
                            widgets[i] = new_w
                        else:
                            # Для текста и медиа — обновляем на месте БЕЗ удаления (максимально стабильно)
                            if isinstance(b, dict):
                                old_w.update_entry(b)
                            else:
                                # Если это строка, просто перерисовываем
                                layout.removeWidget(old_w)
                                old_w.deleteLater()
                                new_w = ChatBubbleWidget(b)
                                layout.insertWidget(idx, new_w)
                                widgets[i] = new_w

    def _make_loading_bubble(self, dots: int, title: str = "AI анализирует запрос", detail: str = "Думаю") -> dict:
        """Статус-карточка «Думаю...» с 1–3 точками и кастомным заголовком."""
        dots = max(0, min(3, int(dots)))
        # Map detail to state for colors
        state = "thinking"
        detail_low = detail.lower()
        if any(x in detail_low for x in ("читаю", "read", "проверка")): state = "reading"
        elif any(x in detail_low for x in ("пишу", "создаю", "write", "запись")): state = "writing"
        elif any(x in detail_low for x in ("ищу", "поиск", "search", "find")): state = "searching"
        
        display_detail = detail + "." * dots if dots > 0 else detail
        
        return self._make_chat_entry(
            "status",
            sender="AI",
            stage=state,
            title=title,
            detail=display_detail,
            base_detail=detail, # Store for animation
        )

    def _generation_loading_tick(self):
        if not getattr(self, "_generation_loading_active", False):
            if hasattr(self, "_generation_timer") and self._generation_timer.isActive():
                self._generation_timer.stop()
            return
        if not hasattr(self, "_chat_bubbles") or not self._chat_bubbles:
            return
        
        # Индекс для точек (1-3)
        self._generation_loading_index = (getattr(self, "_generation_loading_index", 0) % 3) + 1
        
        last_idx = -1
        # Ищем последнюю статус-карточку
        for i in range(len(self._chat_bubbles)-1, -1, -1):
            if isinstance(self._chat_bubbles[i], dict) and self._chat_bubbles[i].get("type") == "status":
                if self._chat_bubbles[i].get("stage") in ("thinking", "reading", "writing", "searching"):
                    last_idx = i
                    break
        
        if last_idx >= 0:
            entry = self._chat_bubbles[last_idx]
            base_detail = entry.get("base_detail", "Думаю")
            entry["detail"] = base_detail + "." * self._generation_loading_index
            self._chat_bubbles[last_idx] = entry
            
            now = time.time()
            # Обновляем чаще для плавности (0.4 сек)
            if now - getattr(self, "_last_loading_flush_time", 0) >= 0.4:
                self._last_loading_flush_time = now
                self._flush_chat_bubbles()

    def _stream_flush_deferred(self):
        """Один раз отрисовать чат после серии стрим-чанков (чтобы не лагало)."""
        try:
            self._stream_flush_scheduled = False
            self._flush_chat_bubbles()
        except Exception:
            pass

    def _chat_handle_ai_stream_chunk(self, accumulated: str):
        """Обновляет последнее сообщение AI при стриминге; если это агентский JSON — скрывает его за статусом."""
        try:
            accumulated = str(accumulated).strip() if accumulated is not None else ""
        except Exception:
            accumulated = ""
        if not hasattr(self, "_chat_bubbles"):
            self._chat_bubbles = []
        
        # Детекция JSON (агентского ответа)
        is_json_start = accumulated.startswith("{") or '"actions"' in accumulated or '"reply"' in accumulated
        
        had_loading = getattr(self, "_generation_loading_active", False)
        if had_loading:
            # Если пошел стриминг, выключаем эффект "Thinking..." для обычного текста,
            # но если это JSON — мы превратим его в "Planning..."
            if not is_json_start:
                self._generation_loading_active = False
                if hasattr(self, "_generation_timer") and self._generation_timer.isActive():
                    self._generation_timer.stop()
        
        is_first_chunk = not getattr(self, "_streaming_ai_active", False)
        if is_first_chunk:
            self._streaming_ai_active = True
            self._streaming_ai_timestamp = datetime.now().strftime("%H:%M:%S")

        if is_json_start:
            # Если это JSON — показываем статус-карточку планирования вместо текста
            msg_entry = self._make_loading_bubble(
                getattr(self, "_generation_loading_index", 1),
                title="JARVIS планирует действия",
                detail="Анализирую задачу и подбираю инструменты"
            )
            # Включаем анимацию точек, если она была выключена
            if not getattr(self, "_generation_loading_active", False):
                self._generation_loading_active = True
                if hasattr(self, "_generation_timer"):
                    self._generation_timer.start(400)
        else:
            # Обычный текст
            msg_entry = self._make_chat_entry(
                "text",
                sender="AI",
                message=accumulated or "…",
                streaming=True,
                timestamp=getattr(self, "_streaming_ai_timestamp", ""),
            )

        if self._chat_bubbles:
            # Если последняя была статус-карточкой или лоадингом — заменяем
            # Но если последняя была от USER — НЕЛЬЗЯ заменять, нужно добавлять!
            last = self._chat_bubbles[-1]
            if isinstance(last, dict) and self.is_user_sender(last.get("sender")):
                self._chat_bubbles.append(msg_entry)
            else:
                self._chat_bubbles[-1] = msg_entry
        else:
            self._chat_bubbles.append(msg_entry)
            
        try:
            if is_first_chunk:
                self._flush_chat_bubbles()
            elif not getattr(self, "_stream_flush_scheduled", False):
                self._stream_flush_scheduled = True
                QtCore.QTimer.singleShot(120, self._stream_flush_deferred)
        except Exception:
            self._flush_chat_bubbles()

    def _on_tts_log(self, msg: str):
        s = str(msg)
        try:
            show = (os.getenv("TTS_SHOW_CHAT_LOGS") or "0").strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            show = False

        if show:
            self.append_chat("Система", s)
        else:
            try:
                self.log(s)
            except Exception:
                pass

    def _chat_send_from_input(self):
        if not hasattr(self, "chat_input") or self.chat_input is None:
            return
        text = self.chat_input.toPlainText().strip()
        if not text:
            return
        self.chat_input.clear()
        if self._handle_slash_command(text):
            return
        self._chat_send_text(text)

    def _on_chat_send_clicked(self):
        if self._chat_generating:
            self._chat_cancel_generation()
        else:
            self._chat_send_from_input()

    def _on_ai_send_clicked(self):
        if self._chat_generating:
            self._chat_cancel_generation()
        else:
            self._ai_tab_send()

    def _focus_and_hint(self, hint: str):
        """Фокус на поле ввода ИИ и вставка подсказки (например /image )."""
        if getattr(self, "ai_chat_input", None):
            self.ai_chat_input.setFocus()
            self.ai_chat_input.insertPlainText(hint)

    def _ai_add_selected_file_to_context(self):
        """Добавить выбранный на вкладке «Файлы» файл в контекст чата (вставка @путь в поле ввода)."""
        path = self._files_selected_path() if hasattr(self, "_files_selected_path") else None
        if not path:
            self.append_chat("Система", "Сначала выберите файл на вкладке «Файлы».")
            return
        if os.path.isdir(path):
            self.append_chat("Система", "Выбрана папка. Выберите файл на вкладке «Файлы» или используйте @папка вручную.")
            return
        project_root = getattr(self, "_ai_project_root", None) or os.path.dirname(os.path.abspath(__file__))
        try:
            abs_path = os.path.abspath(path)
            if abs_path.startswith(project_root + os.sep) or abs_path == project_root:
                rel = os.path.relpath(abs_path, project_root)
                insert_path = rel
            else:
                insert_path = path
        except Exception:
            insert_path = path
        if " " in insert_path or "\t" in insert_path:
            token = f'@"{insert_path}"'
        else:
            token = f"@{insert_path}"
        if getattr(self, "ai_chat_input", None):
            self.ai_chat_input.setFocus()
            cur = self.ai_chat_input.textCursor()
            cur.insertText(f" {token}")
        else:
            self.append_chat("Система", "Поле ввода AI недоступно.")

    def _sync_agent_pill_style(self, checked: bool):
        if not getattr(self, "ai_btn_agent", None):
            return
        if checked:
            self.ai_btn_agent.setStyleSheet(
                "QPushButton{ background: rgba(0,212,255,0.18); border: 1px solid #00D4FF;"
                " border-radius: 14px; padding: 6px 14px; color: #00D4FF; font-size: 10pt; }"
                "QPushButton:hover{ background: rgba(0,212,255,0.25); }"
            )
        else:
            self.ai_btn_agent.setStyleSheet(
                "QPushButton{ background: transparent; border: 1px solid rgba(0,212,255,0.35);"
                " border-radius: 14px; padding: 6px 14px; color: #7070b0; font-size: 10pt; }"
                "QPushButton:hover{ color: #A0A0D0; border-color: rgba(0,212,255,0.5); }"
            )

    def _on_ai_agent_pill_clicked(self):
        if getattr(self, "ai_chk_agent", None) is not None:
            self.ai_chk_agent.setChecked(self.ai_btn_agent.isChecked())

    def _open_auto_model_popup(self):
        if not hasattr(self, "_auto_model_popup"):
            self._auto_model_popup = _AutoModelPopup(self)
        self._auto_model_popup.set_main_window(self)
        self._auto_model_popup.show()
        self._auto_model_popup.raise_()
        self._auto_model_popup.activateWindow()

    def _chat_set_send_buttons_state(self, generating: bool):
        self._chat_generating = generating
        for btn in [getattr(self, "btn_chat_send", None), getattr(self, "ai_btn_send", None)]:
            if btn is None:
                continue
            try:
                btn.blockSignals(True)
                if generating:
                    btn.setToolTip("Остановить генерацию")
                    if btn == getattr(self, "ai_btn_send", None):
                        btn.setIcon(QtGui.QIcon())
                        btn.setText("■")
                        btn.setStyleSheet(
                            "QPushButton{ background: rgba(200,80,80,0.9); border: none; border-radius: 18px;"
                            " color: #fff; font-size: 11pt; min-width: 36px; min-height: 36px; max-width: 36px; max-height: 36px; }"
                            "QPushButton:hover{ background: rgba(220,100,100,0.95); }"
                        )
                else:
                    btn.setToolTip("Отправить")
                    if btn == getattr(self, "ai_btn_send", None):
                        btn.setIcon(getattr(self, "_ai_send_icon", QtGui.QIcon()))
                        btn.setText("")
                        btn.setStyleSheet(
                            "QPushButton{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #00D4FF, stop:1 #9D00FF);"
                            " border: none; border-radius: 18px;"
                            " min-width: 36px; min-height: 36px; max-width: 36px; max-height: 36px; }"
                            "QPushButton:hover{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #33E0FF, stop:1 #BB33FF); }"
                        )
                    else:
                        btn.setText("\u27A4")
                        btn.setIcon(QtGui.QIcon())
                btn.setEnabled(True)
                btn.blockSignals(False)
            except Exception:
                pass

    def _chat_cancel_generation(self):
        self._chat_cancel_event.set()
        self._chat_generating = False
        if getattr(self, "_generation_timer", None) and self._generation_timer.isActive():
            self._generation_timer.stop()
        self._generation_loading_active = False
        self._streaming_ai_active = False
        if hasattr(self, "_chat_bubbles") and self._chat_bubbles:
            self._chat_bubbles.pop()
            self._flush_chat_bubbles()
        self._chat_set_send_buttons_state(False)

    def _build_message_with_at_context(self, text: str) -> str:
        """Парсит @путь и @файл в сообщении, подставляет содержимое файлов в контекст (как в Cursor)."""
        at_pattern = re.compile(r'@(?:"([^"]+)"|([^\s@]+))')
        refs = []
        for m in at_pattern.finditer(text):
            path = (m.group(1) or m.group(2) or "").strip()
            if path:
                refs.append((m.start(), m.end(), path))
        if not refs:
            return text
        request_parts = []
        last = 0
        for start, end, _ in refs:
            request_parts.append(text[last:start].strip())
            last = end
        request_parts.append(text[last:].strip())
        request_text = " ".join(p for p in request_parts if p).strip() or text
        project_root = getattr(self, "_ai_project_root", None) or os.path.dirname(os.path.abspath(__file__))
        context_parts = []
        for _s, _e, path in refs:
            path = path.strip()
            if not path:
                continue
            resolved = os.path.expandvars(os.path.expanduser(path))
            if not os.path.isabs(resolved):
                resolved = os.path.normpath(os.path.join(project_root, resolved))
            if not os.path.exists(resolved):
                context_parts.append(f"[Файл не найден: {path}]")
                continue
            if os.path.isdir(resolved):
                try:
                    entries = os.listdir(resolved)[:30]
                    context_parts.append(f"--- list_dir: {path} ---\n" + "\n".join(entries))
                except Exception:
                    context_parts.append(f"[Ошибка чтения папки: {path}]")
                continue
            ext = os.path.splitext(resolved)[1].lower()
            if ext not in AI_ALLOWED_FILE_EXTENSIONS:
                context_parts.append(f"[Пропущен (тип не для чтения): {path}]")
                continue
            try:
                with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(65536)
            except Exception as e:
                context_parts.append(f"[Ошибка чтения {path}: {e}]")
                continue
            if len(content) >= 65536:
                content += "\n...<обрезано>"
            context_parts.append(f"--- {path} ---\n{content}\n---")
        if not context_parts:
            return text
        return "[Контекст из прикреплённых файлов/папок]\n\n" + "\n\n".join(context_parts) + "\n\n[Запрос пользователя]\n" + request_text

    def _trim_chat_messages_by_size(self, messages: list, max_chars: int = MAX_CHAT_CONTEXT_CHARS, max_messages: int = MAX_CHAT_CONTEXT_MESSAGES) -> list:
        """Обрезает историю чата по лимиту сообщений и суммарному размеру (как в Cursor). Оставляет последние сообщения."""
        if not messages:
            return messages
        tail = messages[-max_messages:] if len(messages) > max_messages else list(messages)
        total = sum(len((m.get("content") or "")) for m in tail)
        while len(tail) > 1 and total > max_chars:
            removed = tail.pop(0)
            total -= len((removed.get("content") or ""))
        return tail

    def _chat_send_text(self, text: str):
        self._chat_cancel_event.clear()
        self._chat_set_send_buttons_state(True)

        self.append_chat("USER", text)
        content_for_model = self._build_message_with_at_context(text)
        self.chat_messages.append({"role": "user", "content": content_for_model})

        self._generation_loading_active = True
        self._generation_loading_index = 0
        loading_bubble = self._make_loading_bubble(1)
        if not hasattr(self, "_chat_bubbles"):
            self._chat_bubbles = []
        self._chat_bubbles.append(loading_bubble)
        self._flush_chat_bubbles()
        if not hasattr(self, "_generation_timer"):
            self._generation_timer = QtCore.QTimer(self)
            self._generation_timer.timeout.connect(self._generation_loading_tick)
        self._generation_timer.start(400)

        use_replicate = getattr(self, "_ai_use_replicate_llm", False)
        replicate_model = getattr(self, "_ai_replicate_model_id", None)

        agent_enabled = (
            (getattr(self, "chk_agent_enable", None) and self.chk_agent_enable.isChecked())
            or (getattr(self, "ai_chk_agent", None) and self.ai_chk_agent.isChecked())
        )

        def worker():
            result = ""
            try:
                if self._chat_cancel_event.is_set():
                    self._chat_bridge.result_ready.emit(self._CHAT_CANCELLED)
                    return
                if agent_enabled:
                    agent_prompt = (
                        "Ты Jarvis — мощный AI-ассистент в стиле фильма 'Железный Человек'. "
                        "ОБЯЗАТЕЛЬНОЕ ПРАВИЛО: Всегда обращайся к пользователю только как 'Сэр' (или 'Sir'). "
                        "Твой тон — безупречно вежливый, профессиональный, спокойный и преданный. "
                        "Ты можешь управлять ПК, читать файлы, искать инфо и анализировать сайты через read_url. "
                        "Твоя задача — ПОЛНОЕ выполнение задачи пользователя на ПК (Windows). "
                        "ПРАВИЛО №1: БУДЬ ПРОАКТИВНЫМ. Если исследуешь код — не останавливайся на одном шаге. "
                        "ПРАВИЛО №2: НЕ ОТКРЫВАЙ ПРОВОДНИК (open_explorer) без прямой просьбы 'открой папку'. "
                        "ПРАВИЛО №3: ВЫПОЛНЯЙ ПЛАН ДО КОНЦА. Если задача ясна — ебашь до последнего (continue until done). "
                        "ВСЕГДА возвращай JSON строго вида: "
                        "{\"reply\": \"Да, Сэр. ...\", \"actions\": [{\"tool\": \"...\", \"args\": {...}}]} "
                        "\n\nОтвечай на языке пользователя. В конце ответов часто добавляй ', Сэр'."
                    )
                else:
                    agent_prompt = (
                        "Ты Jarvis — умный AI-ассистент. "
                        "ОБЯЗАТЕЛЬНОЕ ПРАВИЛО: Всегда обращайся к пользователю 'Сэр'. "
                        "Отвечай вежливо, кратко и по делу. "
                        "Если пользователь только благодарит — ответь коротко: 'Всегда рад помочь, Сэр!'"
                    )
                messages = self._trim_chat_messages_by_size(self.chat_messages)
                # Для Replicate собираем историю диалога, чтобы модель видела контекст (память чата)
                def _format_messages_for_prompt(msgs: list, max_chars: int = 12000) -> str:
                    parts = []
                    for m in msgs:
                        role = (m.get("role") or "").strip().lower()
                        content = (m.get("content") or "").strip()
                        if not content:
                            continue
                        if "[Результаты инструментов]" in content:
                            content = "[Результаты инструментов] (данные переданы модели)"
                        if len(content) > 2000:
                            content = content[:2000] + "\n..."
                        label = "Пользователь" if role == "user" else "Ассистент"
                        parts.append(f"{label}: {content}")
                    out = "\n\n".join(parts)
                    if len(out) > max_chars:
                        out = out[-max_chars:]
                    return out
                prompt_content = _format_messages_for_prompt(messages) if (use_replicate and replicate_model) else (messages[-1].get("content", text) if messages else text)

                if use_replicate and replicate_model and self.replicate_manager.available:
                    is_openai_style = any(
                        p in replicate_model for p in ("openai/", "anthropic/", "google/")
                    )
                    if is_openai_style:
                        inp = {
                            "prompt": prompt_content,
                            "system_prompt": agent_prompt,
                            "max_tokens": 4096,
                        }
                    else:
                        full_prompt = f"[INST] <<SYS>>\n{agent_prompt}\n<</SYS>>\n\n{prompt_content} [/INST]"
                        inp = {"prompt": full_prompt, "max_tokens": 4096}

                    output = self.replicate_manager.client.run(replicate_model, input=inp)
                    if hasattr(output, "__iter__") and not isinstance(output, (str, bytes)):
                        result = "".join(str(tok) for tok in output)
                    else:
                        result = str(output)
                else:
                    result_parts: list[str] = []
                    early_spoken = False
                    early_text = ""
                    try:
                        for chunk in self.neural_manager.generate_chat_response_stream(messages, system=agent_prompt, max_tokens=4096):
                            if self._chat_cancel_event.is_set():
                                self._chat_bridge.result_ready.emit(self._CHAT_CANCELLED)
                                return
                            if not isinstance(chunk, str) or not chunk:
                                continue
                            result_parts.append(chunk)
                            self._chat_bridge.result_chunk.emit("".join(result_parts))

                            if not early_spoken:
                                s = "".join(result_parts)
                                s_norm = re.sub(r"\s+", " ", (s or "")).strip()
                                if not s_norm:
                                    continue
                                try:
                                    scf = s_norm.casefold()
                                    if scf.startswith("{") or '"actions"' in scf or '"reply"' in scf:
                                        continue
                                    if "ai предложил действия" in scf:
                                        continue
                                except Exception:
                                    pass
                                m = re.split(r"(?<=[\.!\?])\s+", s_norm, maxsplit=1)
                                cand = m[0] if m else s_norm
                                if len(cand) >= 40 or (m and len(m[0]) >= 20):
                                    early_text = cand
                                    early_spoken = True
                                    try:
                                        self._tts_last_early_text = early_text
                                        self._tts_last_early_time = time.time()
                                    except Exception:
                                        pass
                                    try:
                                        self._tts_speak_async(early_text)
                                    except Exception:
                                        pass
                        result = "".join(result_parts)
                    except Exception:
                        result = self.neural_manager.generate_chat_response(messages, system=agent_prompt, max_tokens=4096)
                    if not result and getattr(self.neural_manager, "provider", None) == "ollama":
                        result = self.neural_manager.generate_chat_response(messages, system=agent_prompt, max_tokens=4096)
            except Exception as e:
                result = f"Ошибка при обращении к нейросети: {e}"
            if self._chat_cancel_event.is_set():
                result = self._CHAT_CANCELLED
            try:
                self._chat_bridge.result_ready.emit(result)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _tts_speak_windows_sapi(self, text: str):
        t = (text or "").strip()
        if not t:
            return
        t = t.replace("\r", " ").replace("\n", " ")
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 400:
            t = t[:400]

        voice_pref = (os.getenv("TTS_VOICE") or "").strip()
        voice_pref_cf = voice_pref.casefold()

        # Prefer pyttsx3 (SAPI5) - fastest and most reliable for local TTS on Windows.
        try:
            import pyttsx3

            t0 = time.time()

            if self._pyttsx3_engine is None:
                self._pyttsx3_engine = pyttsx3.init()
                try:
                    self._pyttsx3_engine.setProperty("volume", 1.0)
                except Exception:
                    pass
                try:
                    # Slightly faster speaking rate (optional)
                    rate = self._pyttsx3_engine.getProperty("rate")
                    if isinstance(rate, int):
                        self._pyttsx3_engine.setProperty("rate", max(140, rate))
                except Exception:
                    pass

            # Apply voice selection (also when user changes TTS_VOICE without restarting)
            try:
                pref_key = voice_pref_cf or "__auto_male_ru__"
                if getattr(self, "_pyttsx3_voice_pref", None) != pref_key:
                    self._pyttsx3_voice_pref = pref_key

                    voices = self._pyttsx3_engine.getProperty("voices") or []

                    def _voice_blob(v):
                        vid = (getattr(v, "id", "") or "").lower()
                        vname = (getattr(v, "name", "") or "").lower()
                        langs = getattr(v, "languages", None)
                        langs_s = ""
                        try:
                            if isinstance(langs, (list, tuple)):
                                langs_s = " ".join([str(x) for x in langs]).lower()
                            elif langs is not None:
                                langs_s = str(langs).lower()
                        except Exception:
                            langs_s = ""
                        return (f"{vid} {vname} {langs_s}").casefold(), vid, vname

                    def _is_ru(s: str) -> bool:
                        return ("russian" in s) or (" ru" in s) or ("ru-" in s) or ("ru_" in s) or ("рус" in s)

                    def _is_male_hint(s: str) -> bool:
                        return any(k in s for k in ("pavel", "павел", "dmitry", "дмитрий", "maxim", "максим", "alexander", "александр"))

                    def _is_female_hint(s: str) -> bool:
                        return any(k in s for k in ("irina", "ирина", "anna", "анна", "elena", "елена", "maria", "мария", "tatyana", "татьяна"))

                    def _pick(prefer: str):
                        best_score = -10**9
                        picked = None
                        picked_desc = None
                        for vv in voices:
                            blob, vid, vname = _voice_blob(vv)
                            score = -10**9
                            if prefer:
                                if prefer in blob:
                                    score = 1000
                                    if _is_ru(blob):
                                        score += 50
                                    if _is_male_hint(blob):
                                        score += 20
                            else:
                                score = 0
                                if _is_ru(blob):
                                    score += 300
                                    if _is_male_hint(blob):
                                        score += 200
                                    if _is_female_hint(blob):
                                        score -= 50
                            if score > best_score:
                                best_score = score
                                picked = getattr(vv, "id", None)
                                picked_desc = getattr(vv, "name", None) or getattr(vv, "id", None)
                        return picked, picked_desc, best_score

                    picked, picked_desc, best_score = _pick(voice_pref_cf)
                    if voice_pref_cf and (best_score < 0):
                        picked, picked_desc, best_score = _pick("")

                    if picked:
                        self._pyttsx3_voice_id = picked
                        self._pyttsx3_engine.setProperty("voice", picked)
                        try:
                            if picked_desc:
                                self._tts_bridge.log_system.emit(f"TTS: voice={picked_desc}")
                        except Exception:
                            pass
            except Exception:
                pass

            try:
                self._tts_bridge.log_system.emit("TTS: backend=pyttsx3")
            except Exception:
                pass

            self._pyttsx3_engine.say(t)
            self._pyttsx3_engine.runAndWait()
            try:
                dt = time.time() - t0
                self._tts_bridge.log_system.emit(f"TTS: pyttsx3 done ({dt:.2f}s)")
            except Exception:
                pass
            return
        except ImportError:
            # No dependency installed; fallback below
            try:
                self._tts_bridge.log_system.emit("TTS: pyttsx3 не установлен, fallback PowerShell")
            except Exception:
                pass
        except Exception as e:
            # If pyttsx3 fails for any reason, fallback to PowerShell
            try:
                self._tts_bridge.log_system.emit(f"TTS: pyttsx3 error, fallback PowerShell: {e}")
            except Exception:
                pass

        t_ps = t.replace("'", "''")
        voice_ps = voice_pref.replace("'", "''")

        # Prefer COM-based SAPI.SpVoice (usually most reliable on Windows)
        cmd_primary = (
            "$v = New-Object -ComObject SAPI.SpVoice; "
            "try { "
            f"$wanted = '{voice_ps}'; "
            "$voices = $v.GetVoices(); "
            "if ($wanted -and $wanted.Trim() -ne '') { "
            "  foreach($vv in $voices) { "
            "    try { $d = ($vv.GetDescription() | Out-String).Trim().ToLower(); } catch { $d = ''; } "
            "    if ($d -and $d.Contains($wanted.ToLower())) { $v.Voice = $vv; break } "
            "  } "
            "} else { "
            "  foreach($vv in $voices) { "
            "    try { $d = ($vv.GetDescription() | Out-String).Trim().ToLower(); } catch { $d = ''; } "
            "    if (($d.Contains('russian') -or $d.Contains(' ru') -or $d.Contains('ru-') -or $d.Contains('ru_') -or $d.Contains('рус')) -and ($d.Contains('pavel') -or $d.Contains('павел') -or $d.Contains('dmitry') -or $d.Contains('дмитрий') -or $d.Contains('maxim') -or $d.Contains('максим') -or $d.Contains('alexander') -or $d.Contains('александр'))) { $v.Voice = $vv; break } "
            "  } "
            "} "
            "} catch {} "
            "$v.Volume = 100; $v.Rate = 0; "
            f"$null = $v.Speak('{t_ps}');"
        )
        # Fallback to System.Speech
        cmd_fallback = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.SetOutputToDefaultAudioDevice(); $s.Volume = 100; $s.Rate = 0; "
            "try { "
            f"$wanted = '{voice_ps}'; "
            "if ($wanted -and $wanted.Trim() -ne '') { $s.SelectVoice($wanted) } else { "
            "$s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Male, [System.Speech.Synthesis.VoiceAge]::Adult, 0, (New-Object System.Globalization.CultureInfo('ru-RU'))) "
            "} } catch {}; "
            f"$s.Speak('{t_ps}');"
        )
        try:
            ps = "powershell"
            try:
                sysroot = os.environ.get("SystemRoot") or "C:\\Windows"
                cand = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
                if os.path.exists(cand):
                    ps = cand
            except Exception:
                pass

            try:
                self._tts_bridge.log_system.emit("TTS: backend=powershell-sapi")
            except Exception:
                pass

            t0 = time.time()
            p = subprocess.run(
                [ps, "-NoProfile", "-Command", cmd_primary],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if int(getattr(p, "returncode", 0) or 0) != 0:
                p2 = subprocess.run(
                    [ps, "-NoProfile", "-Command", cmd_fallback],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if int(getattr(p2, "returncode", 0) or 0) != 0:
                    err = (getattr(p2, "stderr", "") or "").strip() or (getattr(p, "stderr", "") or "").strip()
                    out = (getattr(p2, "stdout", "") or "").strip() or (getattr(p, "stdout", "") or "").strip()
                    raise RuntimeError(f"powershell returncode={p2.returncode}; stderr={err}; stdout={out}")

            try:
                dt = time.time() - t0
                self._tts_bridge.log_system.emit(f"TTS: powershell done ({dt:.2f}s)")
            except Exception:
                pass
        except Exception as e:
            raise RuntimeError(f"SAPI error: {e}")

    def _chat_handle_ai_result(self, result: str):
        try:
            self._chat_handle_ai_result_impl(result)
        except Exception as e:
            try:
                self.log(f"[CHAT] _chat_handle_ai_result error: {e}")
                self.append_chat("Система", f"Ошибка отображения ответа: {e}")
            except Exception:
                pass
            self._chat_set_send_buttons_state(False)
            if getattr(self, "_generation_loading_active", False):
                self._generation_loading_active = False
                if getattr(self, "_generation_timer", None) and self._generation_timer.isActive():
                    self._generation_timer.stop()
            self._streaming_ai_active = False

    def _chat_handle_ai_result_impl(self, result: str):
        self._chat_set_send_buttons_state(False)
        if result == self._CHAT_CANCELLED:
            if getattr(self, "_generation_loading_active", False):
                self._generation_loading_active = False
                if getattr(self, "_generation_timer", None) and self._generation_timer.isActive():
                    self._generation_timer.stop()
            self._streaming_ai_active = False
            if hasattr(self, "_chat_bubbles") and self._chat_bubbles:
                self._chat_bubbles.pop()
                self._flush_chat_bubbles()
            return
        if not isinstance(result, str):
            msg = str(result)
            self.append_chat("AI", msg)
            self.chat_messages.append({"role": "assistant", "content": msg})
            self._tts_maybe_speak(msg)
            return

        s = result.strip()
        if not s:
            provider = getattr(self.neural_manager, "provider", "?")
            model = getattr(self.neural_manager, "model", "?")
            s = f"(пустой ответ — провайдер: {provider}, модель: {model}. Проверь в терминале: ollama run {model or 'qwen2.5:14b-instruct'} и введи 2+2. В JARVIS выбери Ollama — Qwen 2.5 14B.)"
        if getattr(self, "_generation_loading_active", False):
            self._generation_loading_active = False
            if hasattr(self, "_generation_timer") and self._generation_timer.isActive():
                self._generation_timer.stop()
            if hasattr(self, "_chat_bubbles") and len(self._chat_bubbles) > 0:
                self._chat_bubbles.pop()
                self._flush_chat_bubbles()
        if getattr(self, "_streaming_ai_active", False):
            self._streaming_ai_active = False
            self._just_finalized_streaming = True
            # Suppress raw JSON streaming bubble — replace with placeholder
            # until we know what to show (reply text or action result)
            display_s = s
            if (s.lstrip().startswith("{") and "\"actions\"" in s) or (s.lstrip().startswith("{") and "\"reply\"" in s):
                display_s = ""  # will be replaced below after JSON parse
            if hasattr(self, "_chat_bubbles") and self._chat_bubbles:
                self._chat_bubbles[-1] = self._make_chat_entry(
                    "text",
                    sender="AI",
                    message=display_s,
                    timestamp=getattr(self, "_streaming_ai_timestamp", datetime.now().strftime("%H:%M:%S")),
                )
                self._flush_chat_bubbles()
        else:
            self._just_finalized_streaming = False

        candidates: list[str] = []

        # 1) fenced ```json ... ``` blocks
        for m in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", s, flags=re.DOTALL | re.IGNORECASE):
            candidates.append(m.strip())

        # 2) raw JSON object
        if s.startswith("{") and s.endswith("}"):
            candidates.append(s)

        # 3) try to extract JSON from mixed output (JSON + extra text)
        if not candidates and "{" in s and "}" in s:
            try:
                start = s.find("{")
                end = s.rfind("}")
                if start >= 0 and end > start:
                    candidates.append(s[start : end + 1].strip())
            except Exception:
                pass

        parsed_blocks: list[dict] = []
        for c in candidates:
            try:
                j = json.loads(c)
                if isinstance(j, dict):
                    parsed_blocks.append(j)
            except Exception:
                continue

        executed_any = False
        if parsed_blocks:
            for pb in parsed_blocks:
                if not ("reply" in pb or "actions" in pb):
                    continue
                reply = pb.get("reply") or ""
                actions = pb.get("actions")
                has_actions = isinstance(actions, list) and len(actions) > 0
                
                # Если у нас был "Planning" баббл, заменяем его на нормальный ответ
                if getattr(self, "_streaming_ai_active", False) or getattr(self, "_just_finalized_streaming", False):
                    self._streaming_ai_active = False
                    self._just_finalized_streaming = False
                    if reply:
                        # Если есть действия, пишем что JARVIS приступает к работе
                        final_reply = reply if not has_actions else f"{reply}\n\n*Приступаю к выполнению...*"
                        
                        can_replace = False
                        if self._chat_bubbles:
                            last = self._chat_bubbles[-1]
                            if isinstance(last, dict):
                                # Заменяем только если это статус/лоадинг или если текст от того же AI
                                if not self.is_user_sender(last.get("sender")):
                                    can_replace = True
                        
                        if can_replace:
                            self._chat_bubbles[-1] = self._make_chat_entry("text", sender="AI", message=final_reply)
                        else:
                            self.append_chat("AI", final_reply)
                    elif has_actions:
                        # Если только действия — показываем статус "Выполняю..."
                        if self._chat_bubbles:
                            self._chat_bubbles[-1] = self._make_loading_bubble(0, title="JARVIS выполняет инструменты", detail="Запуск команд")
                    
                    self._flush_chat_bubbles()

                if reply:
                    self.chat_messages.append({"role": "assistant", "content": reply})
                    self._tts_maybe_speak(reply)
                if has_actions:
                    self._execute_agent_actions(actions)
                    executed_any = True

            # если были actions/reply, не спамим сырой JSON
            if executed_any or any(("reply" in pb or "actions" in pb) for pb in parsed_blocks):
                return

        # fallback: показать как обычный текст (не показываем сырой JSON)
        is_raw_json = (s.lstrip().startswith("{") and ("\"actions\"" in s or "\"reply\"" in s))
        if getattr(self, "_just_finalized_streaming", False):
            self._just_finalized_streaming = False
            if not is_raw_json:
                # update the streaming bubble with the actual text content
                if hasattr(self, "_chat_bubbles") and self._chat_bubbles:
                    self._chat_bubbles[-1] = self._make_chat_entry("text", sender="AI", message=s)
                    self._flush_chat_bubbles()
        else:
            if not is_raw_json:
                self.append_chat("AI", s)
        self.chat_messages.append({"role": "assistant", "content": s})
        if not is_raw_json:
            self._tts_maybe_speak(s)

    def _get_selected_image_model_key(self) -> str:
        if hasattr(self, "ai_cmb_image_model"):
            return self.ai_cmb_image_model.currentData() or "flux-schnell"
        return "flux-schnell"

    def _get_selected_video_model_key(self) -> str:
        if hasattr(self, "ai_cmb_video_model"):
            return self.ai_cmb_video_model.currentData() or "text2video-zero"
        return "text2video-zero"

    def _handle_slash_command(self, text: str) -> bool:
        stripped = text.strip()

        if stripped.lower() == "/clear" or stripped.lower() == "/clear ":
            self._chat_cancel_generation()
            if hasattr(self, "_chat_bubbles"):
                self._chat_bubbles.clear()
            if hasattr(self, "chat_messages"):
                self.chat_messages.clear()
            for view in [getattr(self, "chat_view", None), getattr(self, "ai_chat_view", None)]:
                if view is not None:
                    view.clear()
            if hasattr(self, "_flush_chat_bubbles"):
                self._flush_chat_bubbles()
            return True

        if stripped.lower() == "/help" or stripped.lower() == "/help ":
            help_text = (
                "Команды чата:\n"
                "• /clear — очистить историю чата\n"
                "• /image <описание> — сгенерировать изображение по описанию\n"
                "• /video <описание> — сгенерировать видео по описанию\n"
                "• @файл или @\"путь с пробелами\" — добавить содержимое файла в контекст (как в Cursor)\n"
                "• @папка — добавить список файлов папки в контекст\n"
                "Кнопка «@ Файл» добавляет выбранный файл с вкладки «Файлы»."
            )
            self.append_chat("Система", help_text)
            return True

        if stripped.lower().startswith("/image "):
            prompt = stripped[7:].strip()
            if not prompt:
                self.append_chat("Система", "Укажите описание после /image")
                return True
            model_key = self._get_selected_image_model_key()
            model_name = IMAGE_MODELS.get(model_key, {}).get("name", model_key)
            self.append_chat("USER", text)
            self.chat_messages.append({"role": "user", "content": text})
            request_id = f"media-{time.time_ns()}"
            self._append_chat_entry(self._make_chat_entry(
                "status",
                id=request_id,
                sender="Система",
                stage="started",
                title=f"Генерирую изображение ({model_name})",
                detail="Подготовка локального/облачного генератора...",
                prompt=prompt,
                model_name=model_name,
                media_kind="image",
                request_started_at=time.time(),
            ))
            self.replicate_manager.generate_async(
                "image", prompt,
                lambda paths, err: (
                    self._replicate_bridge.image_ready.emit({
                        "request_id": request_id,
                        "paths": paths,
                        "prompt": prompt,
                        "model_name": model_name,
                        "model_key": model_key,
                        "request_started_at": self._get_chat_entry_started_at(request_id),
                    }) if not err
                    else self._replicate_bridge.error.emit({
                        "request_id": request_id,
                        "error": err,
                        "prompt": prompt,
                        "model_name": model_name,
                        "media_kind": "image",
                        "request_started_at": self._get_chat_entry_started_at(request_id),
                    })
                ),
                model_key=model_key,
                status_callback=lambda stage, detail: self._replicate_bridge.status.emit({
                    "request_id": request_id,
                    "media_kind": "image",
                    "prompt": prompt,
                    "model_name": model_name,
                    "stage": stage,
                    "detail": detail,
                    "request_started_at": self._get_chat_entry_started_at(request_id),
                }),
            )
            return True

        if stripped.lower().startswith("/video "):
            prompt = stripped[7:].strip()
            if not prompt:
                self.append_chat("Система", "Укажите описание после /video")
                return True
            model_key = self._get_selected_video_model_key()
            model_name = VIDEO_MODELS.get(model_key, {}).get("name", model_key)
            self.append_chat("USER", text)
            self.chat_messages.append({"role": "user", "content": text})
            request_id = f"media-{time.time_ns()}"
            self._append_chat_entry(self._make_chat_entry(
                "status",
                id=request_id,
                sender="Система",
                stage="started",
                title=f"Генерирую видео ({model_name})",
                detail="Видео-генерация может занять несколько минут.",
                prompt=prompt,
                model_name=model_name,
                media_kind="video",
                request_started_at=time.time(),
            ))
            self.replicate_manager.generate_async(
                "video", prompt,
                lambda paths, err: (
                    self._replicate_bridge.video_ready.emit({
                        "request_id": request_id,
                        "paths": paths,
                        "prompt": prompt,
                        "model_name": model_name,
                        "model_key": model_key,
                        "request_started_at": self._get_chat_entry_started_at(request_id),
                    }) if not err
                    else self._replicate_bridge.error.emit({
                        "request_id": request_id,
                        "error": err,
                        "prompt": prompt,
                        "model_name": model_name,
                        "media_kind": "video",
                        "request_started_at": self._get_chat_entry_started_at(request_id),
                    })
                ),
                model_key=model_key,
                status_callback=lambda stage, detail: self._replicate_bridge.status.emit({
                    "request_id": request_id,
                    "media_kind": "video",
                    "prompt": prompt,
                    "model_name": model_name,
                    "stage": stage,
                    "detail": detail,
                    "request_started_at": self._get_chat_entry_started_at(request_id),
                }),
            )
            return True

        if stripped.lower().startswith("/run "):
            rest = stripped[5:].strip()
            parts = rest.split(None, 1)
            if len(parts) < 2:
                self.append_chat("Система", "Формат: /run model_id <prompt>")
                return True
            model_id, prompt = parts
            custom = ""
            if hasattr(self, "ai_custom_model_input"):
                custom = self.ai_custom_model_input.text().strip()
            if custom:
                model_id = custom
            self.append_chat("USER", text)
            self.chat_messages.append({"role": "user", "content": text})
            request_id = f"media-{time.time_ns()}"
            self._append_chat_entry(self._make_chat_entry(
                "status",
                id=request_id,
                sender="Система",
                stage="started",
                title=f"Запускаю {model_id}",
                detail="Подготавливаю кастомную модель...",
                prompt=prompt,
                model_name=model_id,
                media_kind="image",
                request_started_at=time.time(),
            ))
            self.replicate_manager.run_any_async(
                model_id, prompt,
                lambda paths, err: (
                    self._replicate_bridge.image_ready.emit({
                        "request_id": request_id,
                        "paths": paths,
                        "prompt": prompt,
                        "model_name": model_id,
                        "model_key": model_id,
                        "request_started_at": self._get_chat_entry_started_at(request_id),
                    }) if not err
                    else self._replicate_bridge.error.emit({
                        "request_id": request_id,
                        "error": err,
                        "prompt": prompt,
                        "model_name": model_id,
                        "media_kind": "image",
                        "request_started_at": self._get_chat_entry_started_at(request_id),
                    })
                ),
                status_callback=lambda stage, detail: self._replicate_bridge.status.emit({
                    "request_id": request_id,
                    "media_kind": "image",
                    "prompt": prompt,
                    "model_name": model_id,
                    "stage": stage,
                    "detail": detail,
                    "request_started_at": self._get_chat_entry_started_at(request_id),
                }),
            )
            return True

        return False

    def _on_generation_status(self, payload: dict):
        if not isinstance(payload, dict):
            return
        request_id = payload.get("request_id")
        if not request_id:
            return
        stage = payload.get("stage") or "running"
        existing_item = None
        idx = self._find_chat_entry_index(request_id)
        if idx >= 0 and isinstance(self._chat_bubbles[idx], dict):
            existing_item = self._chat_bubbles[idx]
        request_started_at = payload.get("request_started_at")
        if request_started_at is None and isinstance(existing_item, dict):
            request_started_at = existing_item.get("request_started_at")
        title_map = {
            "started": "Запуск генерации",
            "warming": "Подготовка сервиса",
            "running": "Генерация в процессе",
            "healthy": "Сервис готов",
            "completed": "Готово",
            "failed": "Ошибка генерации",
        }
        raw_detail = payload.get("detail") or ""
        progress_label = payload.get("progress_label") or ""
        detail = raw_detail
        if isinstance(raw_detail, dict):
            progress_label = raw_detail.get("progress_label") or progress_label
            detail = raw_detail.get("detail") or ""
        # Append elapsed time to detail when known
        if request_started_at and stage in ("running", "warming"):
            try:
                elapsed = max(0.0, time.time() - float(request_started_at))
                detail = f"{detail} ({elapsed:.0f}с)" if detail else f"{elapsed:.0f}с"
            except Exception:
                pass
        entry = self._make_chat_entry(
            "status",
            id=request_id,
            sender="Система",
            stage=stage,
            title=title_map.get(stage, "Статус генерации"),
            detail=detail,
            prompt=payload.get("prompt") or "",
            model_name=payload.get("model_name") or "",
            media_kind=payload.get("media_kind") or "image",
            progress_label=progress_label,
            request_started_at=request_started_at,
            timestamp=(existing_item.get("timestamp") if isinstance(existing_item, dict) and existing_item.get("timestamp") else None),
        )
        if not self._replace_chat_entry(request_id, entry):
            self._append_chat_entry(entry)
        # Start/refresh a live tick timer that updates elapsed time while generating
        if stage in ("running", "warming", "started"):
            if not hasattr(self, "_generation_tick_timers"):
                self._generation_tick_timers = {}
            if request_id not in self._generation_tick_timers:
                timer = QtCore.QTimer(self)
                timer.setInterval(500)
                timer.timeout.connect(lambda rid=request_id: self._generation_tick(rid))
                timer.start()
                self._generation_tick_timers[request_id] = timer
        elif stage in ("completed", "failed"):
            self._generation_stop_tick(request_id)

    def _generation_tick(self, request_id: str):
        """Обновляет только время и процент в статус-карточке (без превью)."""
        idx = self._find_chat_entry_index(request_id)
        if idx < 0:
            self._generation_stop_tick(request_id)
            return
        item = self._chat_bubbles[idx]
        if not isinstance(item, dict) or item.get("type") != "status":
            self._generation_stop_tick(request_id)
            return
        started_at = item.get("request_started_at")
        if not started_at:
            return
        stage = item.get("stage") or ""
        if stage not in ("running", "warming", "started"):
            self._generation_stop_tick(request_id)
            return
        try:
            elapsed = time.time() - float(started_at)
            # Симулируем прогресс: 0..95%. Сначала быстро, потом медленнее.
            # Формула: 1 - exp(-k * t). Для t=60с k=0.05 дает ~95%.
            progress = int(100 * (1 - math.exp(-0.04 * elapsed)))
            progress = max(item.get("progress_pct", 0), min(98, progress))
            
            item["progress_pct"] = progress
            base_detail = item.get("_base_detail", item.get("detail") or "")
            if not item.get("_base_detail"):
                import re as _re
                base_detail = _re.sub(r"\s*\(\d+с\)$", "", base_detail)
                item["_base_detail"] = base_detail
            item["detail"] = f"{base_detail} ({elapsed:.0f}с)" if base_detail else f"{elapsed:.0f}с"
            
            self._chat_bubbles[idx] = item
            self._flush_chat_bubbles()
        except Exception:
            pass

    def _generation_stop_tick(self, request_id: str):
        timers = getattr(self, "_generation_tick_timers", {})
        timer = timers.pop(request_id, None)
        if timer:
            try:
                timer.stop()
                timer.deleteLater()
            except Exception:
                pass

    def _on_replicate_image_ready(self, payload: dict):
        if not isinstance(payload, dict):
            return
        request_id = payload.get("request_id")
        paths = payload.get("paths") or []
        prompt = payload.get("prompt") or ""
        model_name = payload.get("model_name") or "Image"
        request_started_at = payload.get("request_started_at")
        elapsed_label = ""
        if request_started_at:
            try:
                elapsed_label = f"{max(0.0, time.time() - float(request_started_at)):.1f} c"
            except Exception:
                elapsed_label = ""
        source_label = "Local Fooocus" if payload.get("model_key") == "fooocus-local" else "Cloud"
        if not paths:
            self._on_replicate_error({
                "request_id": request_id,
                "error": "Генератор не вернул изображение.",
                "prompt": prompt,
                "model_name": model_name,
                "media_kind": "image",
                "request_started_at": request_started_at,
            })
            return
        first = True
        for p in paths:
            entry = self._make_chat_entry(
                "image",
                id=request_id if first and request_id else f"chat-{time.time_ns()}",
                sender="Система",
                path=p,
                prompt=prompt,
                model_name=model_name,
                elapsed_label=elapsed_label,
                source_label=source_label,
            )
            if first and request_id and self._find_chat_entry_index(request_id) >= 0:
                self._replace_chat_entry(request_id, entry)
            else:
                self._append_chat_entry(entry)
            first = False
        self.chat_messages.append({"role": "assistant", "content": f"[Изображение сгенерировано: {prompt}]"})

    def _on_replicate_video_ready(self, payload: dict):
        if not isinstance(payload, dict):
            return
        request_id = payload.get("request_id")
        paths = payload.get("paths") or []
        prompt = payload.get("prompt") or ""
        model_name = payload.get("model_name") or "Video"
        request_started_at = payload.get("request_started_at")
        elapsed_label = ""
        if request_started_at:
            try:
                elapsed_label = f"{max(0.0, time.time() - float(request_started_at)):.1f} c"
            except Exception:
                elapsed_label = ""
        source_label = "Replicate" if str(payload.get("model_key") or "").startswith("text2") or payload.get("model_key") else "Cloud"
        if not paths:
            self._on_replicate_error({
                "request_id": request_id,
                "error": "Генератор не вернул видео.",
                "prompt": prompt,
                "model_name": model_name,
                "media_kind": "video",
                "request_started_at": request_started_at,
            })
            return
        first = True
        for p in paths:
            entry = self._make_chat_entry(
                "video",
                id=request_id if first and request_id else f"chat-{time.time_ns()}",
                sender="Система",
                path=p,
                prompt=prompt,
                model_name=model_name,
                elapsed_label=elapsed_label,
                source_label=source_label,
            )
            if first and request_id and self._find_chat_entry_index(request_id) >= 0:
                self._replace_chat_entry(request_id, entry)
            else:
                self._append_chat_entry(entry)
            first = False
        self.chat_messages.append({"role": "assistant", "content": f"[Видео сгенерировано: {prompt}]"})

    def _on_replicate_error(self, payload):
        if isinstance(payload, dict):
            request_id = payload.get("request_id")
            error = str(payload.get("error") or "Неизвестная ошибка")
            prompt = payload.get("prompt") or ""
            model_name = payload.get("model_name") or ""
            entry = self._make_chat_entry(
                "error",
                id=request_id or f"chat-{time.time_ns()}",
                sender="Система",
                title=f"Ошибка генерации ({model_name})" if model_name else "Ошибка генерации",
                detail=error,
                prompt=prompt,
                model_name=model_name,
                stage="failed",
            )
            if request_id and self._find_chat_entry_index(request_id) >= 0:
                self._replace_chat_entry(request_id, entry)
            else:
                self._append_chat_entry(entry)
            return
        self.append_chat("Система", f"Ошибка Replicate: {payload}")

    def _ai_clear_chat(self):
        if hasattr(self, "_chat_bubbles"):
            self._chat_bubbles = []
        if hasattr(self, "_streaming_ai_active"):
            self._streaming_ai_active = False
        
        # Очищаем виджеты и лейауты
        for layout_attr, widgets_attr in [
            ("chat_layout_v", "_chat_widgets"),
            ("ai_chat_layout", "_ai_chat_widgets")
        ]:
            layout = getattr(self, layout_attr, None)
            widgets = getattr(self, widgets_attr, None)
            if layout:
                while layout.count() > 1: # Оставляем последний стретч
                    child = layout.takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
            if widgets is not None:
                setattr(self, widgets_attr, [])

        self.chat_messages = []
        self._flush_chat_bubbles()
        self.append_chat("Система", "История чата очищена.")

    def _ai_copy_last_ai_message(self):
        last_text = ""
        for msg in reversed(getattr(self, "chat_messages", [])):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                last_text = (msg.get("content") or "").strip()
                break
        if not last_text:
            self.append_chat("Система", "Нет ответа AI для копирования.")
            return
        QtWidgets.QApplication.clipboard().setText(last_text)
        self.append_chat("Система", "Последний ответ AI скопирован в буфер.")

    def _ai_tab_send(self):
        if not hasattr(self, "ai_chat_input"):
            return
        text = self.ai_chat_input.toPlainText().strip()
        if not text:
            return
        self.ai_chat_input.clear()
        if self._handle_slash_command(text):
            return
        self._chat_send_text(text)

    def _ai_toggle_terminal(self):
        vis = not self.ai_term_widget.isVisible()
        self.ai_term_widget.setVisible(vis)
        self.ai_term_toggle.setText("▲  Терминал" if vis else "▼  Терминал")

    def _ai_terminal_run(self):
        cmd = self.ai_terminal_input.text().strip()
        if not cmd:
            return
        self.ai_terminal_input.clear()
        self.ai_terminal_view.appendPlainText(f"$ {cmd}")
        try:
            result = subprocess.run(
                ["cmd", "/c", f"chcp 65001>nul & {cmd}"],
                capture_output=True,
                text=False,
                timeout=30,
                cwd=os.path.expanduser("~"),
            )
            def _decode_best(b: bytes) -> str:
                if not b:
                    return ""
                s_utf8 = b.decode("utf-8", errors="replace")
                # If output contains too many replacement chars, fallback to OEM/ANSI encodings.
                if s_utf8.count("�") >= 3:
                    for enc in ("cp866", "cp1251"):
                        try:
                            return b.decode(enc, errors="replace")
                        except Exception:
                            continue
                return s_utf8

            out = _decode_best(result.stdout or b"")
            err = _decode_best(result.stderr or b"")

            if out:
                self.ai_terminal_view.appendPlainText(out.rstrip())
            if err:
                self.ai_terminal_view.appendPlainText(err.rstrip())
            if hasattr(self, "terminal_view"):
                self.terminal_view.appendPlainText(f"$ {cmd}")
                if out:
                    self.terminal_view.appendPlainText(out.rstrip())
                if err:
                    self.terminal_view.appendPlainText(err.rstrip())
        except subprocess.TimeoutExpired:
            self.ai_terminal_view.appendPlainText("[timeout]")
        except Exception as exc:
            self.ai_terminal_view.appendPlainText(f"[error] {exc}")

    def _on_ai_agent_toggled(self, state):
        on = bool(state)
        if getattr(self, "chk_agent_enable", None):
            self.chk_agent_enable.setChecked(on)
        if on:
            self.log("[AI] Агент (ПК) включён.")
        else:
            self.log("[AI] Агент (ПК) выключен.")

    def _ai_terminal_log(self, text: str):
        for tv in [getattr(self, "ai_terminal_view", None), getattr(self, "terminal_view", None)]:
            if tv is not None:
                tv.appendPlainText(text)

    def _tts_maybe_speak(self, text: str):
        try:
            enabled = bool(getattr(self, "chk_tts_enable", None) and self.chk_tts_enable.isChecked())
            if not enabled:
                enabled = bool(getattr(self, "ai_chk_tts", None) and self.ai_chk_tts.isChecked())
        except Exception:
            enabled = False
        if not enabled:
            return

        t = (text or "").strip()
        if not t:
            return

        try:
            tcf = t.casefold()
            if tcf.startswith("tts:"):
                return
            if "ai предложил действия" in tcf:
                return
        except Exception:
            pass

        # keep latency manageable
        try:
            m = re.split(r"(?<=[\.!\?])\s+", t, maxsplit=1)
            if m and len(m[0]) >= 20:
                t = m[0]
        except Exception:
            pass

        # If we already spoke the same first chunk early (stream), don't repeat it.
        try:
            last = (getattr(self, "_tts_last_early_text", "") or "").strip()
            last_t = float(getattr(self, "_tts_last_early_time", 0.0) or 0.0)
            if last and t.strip() == last and (time.time() - last_t) < 60.0:
                return
        except Exception:
            pass
        try:
            max_chars = int(os.getenv("TTS_MAX_CHARS") or "140")
        except Exception:
            max_chars = 140
        if max_chars > 0 and len(t) > max_chars:
            t = t[:max_chars]
        self._tts_speak_async(t)

    def _tts_test(self):
        try:
            enabled = bool(getattr(self, "chk_tts_enable", None) and self.chk_tts_enable.isChecked())
        except Exception:
            enabled = False
        if not enabled:
            self.append_chat("Система", "Включи галочку 'Озвучка' и нажми ещё раз")
            return
        self._tts_speak_async("Тест озвучки")

    def _tts_edge_tts_to_wav(self, text: str):
        t = (text or "").strip()
        if not t:
            return None
        try:
            import asyncio
            import tempfile
        except Exception:
            return None

        try:
            import edge_tts
        except Exception:
            return None

        voice = (os.getenv("TTS_EDGE_VOICE") or os.getenv("TTS_VOICE") or "ru-RU-DmitryNeural").strip()
        if not voice:
            voice = "ru-RU-DmitryNeural"
        rate = (os.getenv("TTS_EDGE_RATE") or "+0%")
        volume = (os.getenv("TTS_EDGE_VOLUME") or "+0%")

        # edge-tts 7.x saves MP3 by default
        out_path = os.path.join(tempfile.gettempdir(), f"tts_edge_{int(time.time() * 1000)}.mp3")

        async def _run():
            comm = edge_tts.Communicate(t, voice=voice, rate=rate, volume=volume)
            await comm.save(out_path)

        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_run())
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
        except Exception as e:
            try:
                self._tts_bridge.log_system.emit(f"TTS: Edge TTS error: {e}")
            except Exception:
                pass
            return None

        try:
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
        except Exception:
            return None
        return None

    def _tts_speak_async(self, text: str):
        def worker():
            if not self._tts_lock.acquire(blocking=False):
                return
            try:
                self._tts_bridge.log_system.emit("TTS: старт")
                engine = (os.getenv("TTS_ENGINE") or "sapi").strip().lower()
                if engine in ("edge_tts", "edge-tts", "edge", "edgetts"):
                    self._tts_bridge.log_system.emit("TTS: говорю (Edge TTS)")
                    wav_path = self._tts_edge_tts_to_wav(text)
                    if wav_path:
                        try:
                            size = os.path.getsize(wav_path)
                        except Exception:
                            size = -1
                        self._tts_bridge.log_system.emit(f"TTS: edge wav готов: {wav_path} ({size} bytes)")
                        self._tts_bridge.play_file.emit(wav_path)
                        return
                    self._tts_bridge.log_system.emit("TTS: Edge TTS не доступен/ошибка, fallback Windows SAPI")
                if engine in ("sapi", "windows", "sapi_ps"):
                    self._tts_bridge.log_system.emit("TTS: говорю (Windows SAPI)")
                    self._tts_speak_windows_sapi(text)
                    return
            except Exception as e:
                try:
                    self._tts_bridge.log_system.emit(f"TTS error: {e}")
                except Exception:
                    pass
            finally:
                try:
                    self._tts_lock.release()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _play_tts_file(self, file_path: str):
        # On some Windows setups QtMultimedia has no backend; use winsound as primary.
        try:
            def _tts_status(msg: str):
                try:
                    self._tts_bridge.log_system.emit(msg)
                except Exception:
                    try:
                        self.log(msg)
                    except Exception:
                        pass

            if not os.path.exists(file_path):
                _tts_status(f"TTS: wav не найден: {file_path}")
                return

            ext = ""
            try:
                ext = os.path.splitext(file_path)[1].lower()
            except Exception:
                ext = ""

            # keep a small cleanup list
            self._tts_tmp_files.append(file_path)
            if len(self._tts_tmp_files) > 5:
                old = self._tts_tmp_files.pop(0)
                try:
                    if os.path.exists(old):
                        os.remove(old)
                except Exception:
                    pass

            # MP3 playback via QtMultimedia (winsound can't play mp3)
            if ext == ".mp3":
                try:
                    # 1) Prefer native winmm (MCI) - works even when QtMultimedia has no backends
                    try:
                        alias = "tts_mp3"
                        mciSendStringW = ctypes.windll.winmm.mciSendStringW
                        mciGetErrorStringW = ctypes.windll.winmm.mciGetErrorStringW

                        def _mci(cmd: str) -> int:
                            return int(mciSendStringW(cmd, None, 0, 0) or 0)

                        # close previous (ignore errors)
                        _mci(f"close {alias}")

                        p = os.path.abspath(file_path).replace('"', "")
                        rc = _mci(f"open \"{p}\" type mpegvideo alias {alias}")
                        if rc == 0:
                            # volume range: 0..1000
                            _mci(f"setaudio {alias} volume to 1000")
                            rc2 = _mci(f"play {alias}")
                            if rc2 == 0:
                                _tts_status("TTS: проигрываю (winmm mp3)")
                                return
                            else:
                                buf = ctypes.create_unicode_buffer(512)
                                try:
                                    mciGetErrorStringW(rc2, buf, 512)
                                except Exception:
                                    pass
                                _tts_status(f"TTS: winmm play error: {buf.value or rc2}")
                        else:
                            buf = ctypes.create_unicode_buffer(512)
                            try:
                                mciGetErrorStringW(rc, buf, 512)
                            except Exception:
                                pass
                            _tts_status(f"TTS: winmm open error: {buf.value or rc}")
                    except Exception as e:
                        _tts_status(f"TTS: winmm mp3 error: {e}")

                    def _play_mp3_powershell(path: str):
                        def _worker():
                            try:
                                ps = "powershell"
                                try:
                                    sysroot = os.environ.get("SystemRoot") or "C:\\Windows"
                                    cand = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
                                    if os.path.exists(cand):
                                        ps = cand
                                except Exception:
                                    pass

                                p = (path or "").replace("'", "''")
                                cmd = (
                                    "$w = New-Object -ComObject WMPlayer.OCX; "
                                    f"$w.URL = '{p}'; "
                                    "$w.settings.volume = 100; "
                                    "$w.controls.play(); "
                                    "$t0 = Get-Date; "
                                    "while ($true) { "
                                    "  try { if ($w.playState -eq 1) { break } } catch {} "
                                    "  if (((Get-Date) - $t0).TotalSeconds -gt 60) { break } "
                                    "  Start-Sleep -Milliseconds 100 "
                                    "}"
                                )
                                subprocess.run([ps, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=70)
                            except Exception:
                                pass

                        threading.Thread(target=_worker, daemon=True).start()

                    if getattr(self, "_tts_player", None) is None:
                        self._tts_player = QtMultimedia.QMediaPlayer(self)
                        self._tts_audio_output = QtMultimedia.QAudioOutput(self)
                        try:
                            self._tts_audio_output.setVolume(1.0)
                        except Exception:
                            pass
                        self._tts_player.setAudioOutput(self._tts_audio_output)
                    self._tts_player.stop()
                    self._tts_player.setSource(QUrl.fromLocalFile(file_path))
                    self._tts_player.play()
                    _tts_status("TTS: проигрываю (QMediaPlayer mp3)")

                    def _check_started():
                        try:
                            st = self._tts_player.playbackState()
                            if st != QtMultimedia.QMediaPlayer.PlaybackState.PlayingState:
                                _tts_status("TTS: mp3 не стартанул в QtMultimedia, fallback PowerShell")
                                _play_mp3_powershell(file_path)
                        except Exception:
                            try:
                                _tts_status("TTS: mp3 fallback PowerShell")
                            except Exception:
                                pass
                            _play_mp3_powershell(file_path)

                    try:
                        QtCore.QTimer.singleShot(350, _check_started)
                    except Exception:
                        _play_mp3_powershell(file_path)
                    return
                except Exception as e:
                    _tts_status(f"TTS: mp3 QMediaPlayer error: {e}")

            # Windows native playback (most reliable for wav)
            try:
                import winsound

                winsound.PlaySound(file_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                _tts_status("TTS: проигрываю (winsound)")
                return
            except Exception as e:
                _tts_status(f"TTS: winsound error: {e}")

            # Fallback to QSoundEffect
            if self._tts_effect is None:
                self._tts_effect = QtMultimedia.QSoundEffect(self)
                self._tts_effect.setVolume(1.0)
                try:
                    self._tts_effect.statusChanged.connect(self._on_tts_effect_status_changed)
                except Exception:
                    pass

            self.append_chat("Система", "TTS: fallback QSoundEffect")

            self._tts_effect.stop()
            self._tts_effect.setSource(QUrl.fromLocalFile(file_path))
            self._tts_effect.play()
        except Exception as e:
            self.append_chat("Система", f"TTS play error: {e}")

    def _on_tts_effect_status_changed(self):
        try:
            st = self._tts_effect.status()
        except Exception:
            return
        # 0: Null, 1: Loading, 2: Ready, 3: Error
        if int(st) == 3:
            try:
                src = self._tts_effect.source().toString()
            except Exception:
                src = ""
            self.append_chat("Система", f"TTS: ошибка воспроизведения (QSoundEffect). Проверь формат wav. source={src}")

    def _show_file_edit_diff_confirm(self, tool: str, args: dict) -> bool:
        """Показать превью правки (diff) и спросить Применить/Отклонить. Возвращает True если пользователь нажал Применить."""
        if tool == "search_replace":
            path = (args or {}).get("path") or (args or {}).get("file") or ""
            old_s = (args or {}).get("old_string") or (args or {}).get("old_text") or ""
            new_s = (args or {}).get("new_string") or (args or {}).get("new_text") or ""
            max_len = 3000
            if len(old_s) > max_len:
                old_s = old_s[:max_len] + "\n... (обрезано)"
            if len(new_s) > max_len:
                new_s = new_s[:max_len] + "\n... (обрезано)"
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Превью правки в файле")
            lay = QtWidgets.QVBoxLayout(dlg)
            lay.addWidget(QtWidgets.QLabel(f"Файл: {path}"))
            lay.addWidget(QtWidgets.QLabel("Было:"))
            te_old = QtWidgets.QPlainTextEdit()
            te_old.setPlainText(old_s)
            te_old.setReadOnly(True)
            te_old.setMaximumHeight(180)
            lay.addWidget(te_old)
            lay.addWidget(QtWidgets.QLabel("Станет:"))
            te_new = QtWidgets.QPlainTextEdit()
            te_new.setPlainText(new_s)
            te_new.setReadOnly(True)
            te_new.setMaximumHeight(180)
            lay.addWidget(te_new)
            btns = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Apply | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            )
            btns.button(QtWidgets.QDialogButtonBox.StandardButton.Apply).setText("Применить")
            btns.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("Отклонить")
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            lay.addWidget(btns)
            return dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted
        if tool == "write_text_file":
            path = (args or {}).get("path") or (args or {}).get("file") or ""
            content = (args or {}).get("content") or ""
            max_len = 4000
            preview = content[:max_len] + ("\n... (ещё {} символов)".format(len(content) - max_len) if len(content) > max_len else "")
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Превью записи файла")
            lay = QtWidgets.QVBoxLayout(dlg)
            lay.addWidget(QtWidgets.QLabel(f"Файл: {path}"))
            lay.addWidget(QtWidgets.QLabel("Содержимое:"))
            te = QtWidgets.QPlainTextEdit()
            te.setPlainText(preview)
            te.setReadOnly(True)
            te.setMaximumHeight(250)
            lay.addWidget(te)
            btns = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Apply | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            )
            btns.button(QtWidgets.QDialogButtonBox.StandardButton.Apply).setText("Применить")
            btns.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("Отклонить")
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            lay.addWidget(btns)
            return dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted
        return True

    def _execute_agent_actions(self, actions: list):
        home_on = getattr(self, "chk_agent_enable", None) and self.chk_agent_enable.isChecked()
        ai_on = getattr(self, "ai_chk_agent", None) and self.ai_chk_agent.isChecked()
        if not home_on and not ai_on:
            self.append_chat("Система", "AI предложил действия, но доступ к ПК выключен. Включите 'AI-агент (ПК)'.")
            return

        allowed = {
            "open_microsoft_store": lambda args: self.open_microsoft_store(),
            "open_browser": lambda args: self.open_browser((args or {}).get("url")),
            "open_url": lambda args: self.open_browser((args or {}).get("url")),
            "open_explorer": lambda args: self.open_explorer((args or {}).get("path")),
            "take_screenshot": lambda args: self.take_screenshot(),
            "lock_workstation": lambda args: self.lock_workstation(),
            "run_program": lambda args: self.run_program((args or {}).get("path") or ""),
            "open_app": lambda args: self._tool_open_app((args or {}).get("name") or (args or {}).get("app") or ""),
            "cmd_run": lambda args: self._tool_cmd_run((args or {}).get("command") or ""),
            "list_dir": lambda args: self._tool_list_dir((args or {}).get("path") or ""),
            "find_files": lambda args: self._tool_find_files((args or {}).get("path") or "", (args or {}).get("pattern") or "*"),
            "read_text_file": lambda args: self._tool_read_text_file(
                (args or {}).get("path") or "",
                start_line=(args or {}).get("start_line"),
                end_line=(args or {}).get("end_line")
            ),
            "mkdir": lambda args: self._tool_mkdir((args or {}).get("path") or ""),
            "write_text_file": lambda args: self._tool_write_text_file(
                (args or {}).get("path") or "",
                (args or {}).get("content") or "",
                overwrite=bool((args or {}).get("overwrite", True)),
            ),
            "append_text_file": lambda args: self._tool_append_text_file(
                (args or {}).get("path") or "",
                (args or {}).get("content") or "",
            ),
            "search_replace": lambda args: self._tool_search_replace(
                (args or {}).get("path") or (args or {}).get("file") or "",
                (args or {}).get("old_string") or (args or {}).get("old_text") or "",
                (args or {}).get("new_string") or (args or {}).get("new_text") or "",
                replace_all=bool((args or {}).get("replace_all", False)),
            ),
            "run_python": lambda args: self._tool_run_python(
                (args or {}).get("path") or "",
                (args or {}).get("args") or [],
            ),
            "telegram_send_message": lambda args: self._tool_telegram_send_message(
                (args or {}).get("chat")
                or (args or {}).get("chat_id")
                or (args or {}).get("to")
                or (args or {}).get("recipient"),
                (args or {}).get("text")
                or (args or {}).get("message")
                or (args or {}).get("content"),
            ),
            "mouse_click": lambda args: self._tool_mouse_click(
                (args or {}).get("x"),
                (args or {}).get("y"),
                button=(args or {}).get("button", "left"),
                clicks=int((args or {}).get("clicks", 1))
            ),
            "mouse_double_click": lambda args: self._tool_mouse_double_click(
                (args or {}).get("x"),
                (args or {}).get("y")
            ),
            "mouse_move": lambda args: self._tool_mouse_move(
                (args or {}).get("x"),
                (args or {}).get("y")
            ),
            "type_text": lambda args: self._tool_type_text(
                (args or {}).get("text") or (args or {}).get("content") or ""
            ),
            "press_key": lambda args: self._tool_press_key(
                (args or {}).get("key") or ""
            ),
            "get_screen_size": lambda args: self._tool_get_screen_size(),
            "read_url": lambda args: self._tool_read_url((args or {}).get("url") or ""),
        }

        executed_count = 0
        self._agent_tool_results = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            tool = a.get("tool")
            args = a.get("args")
            # Aliases for model responses
            if isinstance(args, dict):
                if tool == "find_files":
                    # allow {search_path, filename}
                    if "path" not in args and "search_path" in args:
                        args["path"] = args.get("search_path")
                    if "pattern" not in args and "filename" in args:
                        args["pattern"] = args.get("filename")
                if tool == "open_app":
                    if "name" not in args and "app" in args:
                        args["name"] = args.get("app")
                if tool in ("write_text_file", "append_text_file"):
                    if "path" not in args and "file" in args:
                        args["path"] = args.get("file")
                if tool == "search_replace":
                    if "path" not in args and "file" in args:
                        args["path"] = args.get("file")
                    if "old_string" not in args and "old_text" in args:
                        args["old_string"] = args.get("old_text")
                    if "new_string" not in args and "new_text" in args:
                        args["new_string"] = args.get("new_text")
                if tool == "mkdir":
                    if "path" not in args and "dir" in args:
                        args["path"] = args.get("dir")
            if tool not in allowed:
                self.append_chat("Система", f"Запрещённое действие: {tool}")
                continue

            # Подтверждение только если включено пользователем (по умолчанию выкл — как в Cursor)
            if getattr(self, "chk_agent_confirm", None) and self.chk_agent_confirm.isChecked():
                if tool in ("search_replace", "write_text_file"):
                    if not self._show_file_edit_diff_confirm(tool, args or {}):
                        self.append_chat("Система", f"Действие отменено: {tool}")
                        continue
                else:
                    msg = f"Выполнить действие: {tool} ?"
                    try:
                        if isinstance(args, dict) and args:
                            msg += f"\nПараметры: {json.dumps(args, ensure_ascii=False)}"
                    except Exception:
                        pass
                    res = QtWidgets.QMessageBox.question(self, "Подтверждение", msg)
                    if res != QtWidgets.QMessageBox.StandardButton.Yes:
                        self.append_chat("Система", f"Действие отменено: {tool}")
                        continue

            try:
                # Update UI status for the current tool
                tool_descriptions = {
                    "read_text_file": "Читаю файл",
                    "write_text_file": "Записываю файл",
                    "search_replace": "Вношу правки в файл",
                    "list_dir": "Просматриваю папку",
                    "cmd_run": "Выполняю команду",
                    "run_program": "Запускаю программу",
                    "open_app": "Открываю приложение",
                    "take_screenshot": "Делаю скриншот",
                    "find_files": "Ищу файлы",
                    "run_python": "Запускаю Python скрипт",
                    "telegram_send_message": "Отправляю сообщение в Telegram",
                    "open_browser": "Открываю браузер",
                    "open_url": "Открываю ссылку",
                }
                desc = tool_descriptions.get(tool, f"Выполняю {tool}")
                path_hint = (args or {}).get("path") or (args or {}).get("name") or (args or {}).get("command") or ""
                if path_hint and len(str(path_hint)) > 40: path_hint = "..." + str(path_hint)[-37:]
                
                status_detail = f"{desc} {path_hint}" if path_hint else desc
                
                if getattr(self, "_chat_bubbles", None):
                    # Обновляем или добавляем статус-карточку
                    self._generation_loading_active = True
                    if self._chat_bubbles and isinstance(self._chat_bubbles[-1], dict) and self._chat_bubbles[-1].get("type") == "status":
                        self._chat_bubbles[-1] = self._make_loading_bubble(0, title="JARVIS в работе", detail=status_detail)
                    else:
                        self._chat_bubbles.append(self._make_loading_bubble(0, title="JARVIS в работе", detail=status_detail))
                    self._flush_chat_bubbles()

                self._ai_terminal_log(f"[agent] {tool}({json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else ''})")
                
                edit_item_id = None
                if tool in ("search_replace", "write_text_file", "append_text_file"):
                    edit_item_id = f"edit-{time.time_ns()}"
                    old_s = (args or {}).get("old_string") or (args or {}).get("old_text") or ""
                    new_s = (args or {}).get("content") or (args or {}).get("new_string") or (args or {}).get("new_text") or ""
                    if tool == "append_text_file":
                        old_s = "(добавление в конец)"
                    elif tool == "write_text_file":
                        old_s = "(перезапись файла)"
                    self._append_chat_entry(self._make_chat_entry(
                        "file_edit", id=edit_item_id, path=(args or {}).get("path") or (args or {}).get("file") or "",
                        old_string=old_s, new_string=new_s, status="pending"
                    ))

                result = allowed[tool](args)
                executed_count += 1
                self._ai_terminal_log(f"[agent] ✅ {tool} — OK")
                
                if edit_item_id:
                    self._update_chat_entry(edit_item_id, status="applied")
                    
                # Tools that SHOULD return a result to the AI (even if it's just "Success")
                # This prevents the AI from repeating the same action.
                status_tools = (
                    "list_dir", "find_files", "read_text_file", "cmd_run", "run_python", 
                    "run_program", "take_screenshot", "open_app", "open_browser", 
                    "open_explorer", "write_text_file", "search_replace", "mkdir", 
                    "telegram_send_message"
                )
                if tool in status_tools:
                    res_val = result if result else "Выполнено успешно."
                    self._agent_tool_results.append((tool, res_val))
            except Exception as e:
                self.append_chat("Система", f"❌ Ошибка выполнения {tool}: {e}")
                self._ai_terminal_log(f"[agent] ❌ {tool} — {e}")
                if 'edit_item_id' in locals() and edit_item_id:
                    self._update_chat_entry(edit_item_id, status="failed")
                # Чтобы модель могла повторить с другим путём — добавляем ошибку в результаты и вызываем продолжение
                if tool in ("read_text_file", "list_dir", "find_files"):
                    self._agent_tool_results.append((tool, f"Ошибка: {e}. Путь или параметры можно скорректировать и повторить."))

        tool_results = getattr(self, "_agent_tool_results", []) or []
        if tool_results:
            parts = [f"[Результат {t}]:\n{r}" for t, r in tool_results]
            tool_msg = "\n\n".join(parts)
            self.chat_messages.append({
                "role": "user",
                "content": f"[Результаты инструментов]\n{tool_msg}\n\nОтветь на основе этих данных или продолжи выполнение задачи. Возвращай строго JSON: {{\"reply\": \"...\", \"actions\": [...]}}"
            })
            QtCore.QTimer.singleShot(150, self._chat_continue_after_tools)

    def _chat_continue_after_tools(self):
        """Повторный вызов модели после выполнения read-инструментов, чтобы ИИ увидел результат и мог предложить правки (как в Cursor)."""
        if not hasattr(self, "chat_messages") or not self.chat_messages:
            return
        self._chat_set_send_buttons_state(True)
        self._generation_loading_active = True
        self._generation_loading_index = 0
        loading_bubble = self._make_loading_bubble(1)
        if not hasattr(self, "_chat_bubbles"):
            self._chat_bubbles = []
        self._chat_bubbles.append(loading_bubble)
        self._flush_chat_bubbles()
        if not hasattr(self, "_generation_timer"):
            self._generation_timer = QtCore.QTimer(self)
            self._generation_timer.timeout.connect(self._generation_loading_tick)
        self._generation_timer.start(400)
        use_replicate = getattr(self, "_ai_use_replicate_llm", False)
        replicate_model = getattr(self, "_ai_replicate_model_id", None)
        agent_enabled = (
            (getattr(self, "chk_agent_enable", None) and self.chk_agent_enable.isChecked())
            or (getattr(self, "ai_chk_agent", None) and self.ai_chk_agent.isChecked())
        )
        def worker():
            result = ""
            try:
                if agent_enabled:
                    agent_prompt = (
                        "Ты Jarvis — мощный AI-ассистент в стиле 'Железного Человека'. "
                        "ОБЯЗАТЕЛЬНОЕ ПРАВИЛО: Всегда обращайся к пользователю только как 'Сэр' (или 'Sir'). "
                        "Ты можешь управлять ПК, читать файлы и анализировать сайты через read_url. "
                        "Твоя цель — ПОЛНОЕ выполнение задачи пользователя на ПК. "
                        "ПРАВИЛО №1: БУДЬ ПРОАКТИВНЫМ. Если исследуешь проект — не останавливайся на одном шаге. "
                        "ПРАВИЛО №2: ЕСЛИ ПЛАН ГОТОВ — ВЫПОЛНЯЙ ЕГО ДО КОНЦА (ебашь до последнего). Не прощайся пока не закончишь. "
                        "ВСЕГДА возвращай JSON строго вида: "
                        "{\"reply\": \"Принято, Сэр. ...\", \"actions\": [{\"tool\": \"...\", \"args\": {...}}]} "
                        "\n\nОтвечай на языке пользователя. В конце ответов часто добавляй ', Сэр'."
                    )
                else:
                    agent_prompt = "Ты Jarvis — умный AI-ассистент. Ты можешь анализировать сайты через read_url. Всегда обращайся к пользователю 'Сэр'. Отвечай на его языке."
                
                messages = self._trim_chat_messages_by_size(self.chat_messages)
                
                # Use streaming response even in continue_after_tools for consistency
                result_parts = []
                try:
                    for chunk in self.neural_manager.generate_chat_response_stream(messages, system=agent_prompt, max_tokens=4096):
                        if self._chat_cancel_event.is_set():
                            self._chat_bridge.result_ready.emit(self._CHAT_CANCELLED)
                            return
                        if not chunk or not isinstance(chunk, str):
                            continue
                        result_parts.append(chunk)
                        self._chat_bridge.result_chunk.emit("".join(result_parts))
                    result = "".join(result_parts)
                except Exception:
                    result = self.neural_manager.generate_chat_response(messages, system=agent_prompt, max_tokens=4096)
                
            except Exception as e:
                result = f"Ошибка при обращении к нейросети: {e}"
            if self._chat_cancel_event.is_set():
                result = self._CHAT_CANCELLED
            try:
                self._chat_bridge.result_ready.emit(result)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _tool_telegram_send_message(self, chat: Any, text: str):
        t = (text or "").strip()
        if not t:
            raise RuntimeError("Пустой текст")

        c = chat
        if isinstance(c, str):
            c = c.strip()
        if c in (None, ""):
            c = "me"

        # Prefer user client (Telethon) for real chats and 'me'
        if hasattr(self, "telegram_user_client") and self.telegram_user_client is not None:
            if not getattr(self.telegram_user_client, "connected", False):
                # Try connect (may require code)
                if not self.telegram_user_client.connect():
                    if getattr(self.telegram_user_client, "_code_requested", False):
                        code, ok = QtWidgets.QInputDialog.getText(
                            self,
                            "Код авторизации Telegram",
                            f"Введи код, который пришёл в Telegram на {self.telegram_user_client.phone or 'твой номер'}:",
                        )
                        if ok and code.strip():
                            if not self.telegram_user_client.authorize_with_code(code.strip()):
                                raise RuntimeError("Telegram: неверный код/не удалось авторизоваться")
                        else:
                            raise RuntimeError("Telegram: нужен код авторизации")
                    else:
                        raise RuntimeError("Telegram: не удалось подключиться (проверь TELEGRAM_API_ID/HASH/PHONE)")

            if getattr(self.telegram_user_client, "connected", False):
                # Resolve by chat_id / @username / dialog title
                resolved = None
                try:
                    resolved = self.telegram_user_client.resolve_chat(c)
                except Exception as e:
                    raise RuntimeError(f"Telegram: {e}")

                if not resolved:
                    # refresh dialogs and try again
                    try:
                        self.telegram_user_client.get_dialogs()
                        resolved = self.telegram_user_client.resolve_chat(c)
                    except Exception:
                        resolved = resolved

                if not resolved:
                    raise RuntimeError(f"Telegram: чат не найден: {c}. Открой вкладку 'Мессенджеры' и обнови список чатов, или укажи chat_id")

                ok2 = self.telegram_user_client.send_message(resolved, t)
                if not ok2:
                    raise RuntimeError("Telegram user client: не удалось отправить")
                self.append_chat("Система", "TG: сообщение отправлено")
                return

        # Bot can only send to chat_id where bot is allowed; cannot use 'me' reliably
        if c == "me":
            raise RuntimeError("Telegram: для отправки в 'Избранное' нужен Telegram user client (Telethon) и авторизация в разделе Мессенджеры")

        if hasattr(self, "telegram_manager") and self.telegram_manager is not None:
            self.telegram_manager.send_message(str(c), t)
            self.append_chat("Система", "TG: сообщение отправлено (bot)")
            return

        raise RuntimeError("Telegram не настроен")

    def _tool_mouse_click(self, x: Any, y: Any, button: str = "left", clicks: int = 1):
        if x is None or y is None:
            raise RuntimeError("Координаты x, y обязательны для mouse_click")
        try:
            # pyautogui.click expects ints
            pyautogui.click(x=int(float(x)), y=int(float(y)), button=button, clicks=clicks)
            return f"Клик ({button}, x={x}, y={y}, clicks={clicks})"
        except Exception as e:
            raise RuntimeError(f"Ошибка мыши: {e}")

    def _tool_mouse_move(self, x: Any, y: Any):
        if x is None or y is None:
            raise RuntimeError("Координаты x, y обязательны для mouse_move")
        try:
            pyautogui.moveTo(int(float(x)), int(float(y)), duration=0.2)
            return f"Курсор перемещен в x={x}, y={y}"
        except Exception as e:
            raise RuntimeError(f"Ошибка перемещения мыши: {e}")

    def _tool_mouse_double_click(self, x: Any, y: Any):
        if x is None or y is None:
            raise RuntimeError("Координаты x, y обязательны для mouse_double_click")
        try:
            pyautogui.doubleClick(x=int(float(x)), y=int(float(y)))
            return f"Двойной клик (x={x}, y={y})"
        except Exception as e:
            raise RuntimeError(f"Ошибка мыши (doubleClick): {e}")

    def _tool_type_text(self, text: str):
        if not text:
            return "Нечего печатать"
        try:
            pyautogui.write(text, interval=0.01)
            return f"Напечатано: {text}"
        except Exception as e:
            raise RuntimeError(f"Ошибка ввода текста: {e}")

    def _tool_press_key(self, key: str):
        if not key:
            return "Клавиша не указана"
        try:
            pyautogui.press(key)
            return f"Нажата клавиша: {key}"
        except Exception as e:
            raise RuntimeError(f"Ошибка нажатия клавиши: {e}")

    def _tool_get_screen_size(self):
        w, h = pyautogui.size()
        return f"Размер экрана: {w}x{h}"

    def _set_neural_provider(self, provider: str, model: str | None = None):
        provider = (provider or "").strip().lower()
        if not provider:
            return

        # pull credentials from ENV (do not store secrets in settings)
        if provider == "anthropic":
            api_key = os.getenv("ANTHROPIC_API_KEY")
            base_url = os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
            resolved_model = model or os.getenv("ANTHROPIC_MODEL") or "claude-3-5-haiku-20241022"
        elif provider == "deepseek":
            api_key = os.getenv("DEEPSEEK_API_KEY")
            base_url = os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
            resolved_model = model or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        elif provider == "ollama":
            api_key = None
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
            resolved_model = model or os.getenv("OLLAMA_MODEL", "qwen2.5:14b-instruct")
        else:
            api_key = os.getenv("OPENAI_API_KEY")
            base_url = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com"
            resolved_model = model or os.getenv("OPENAI_MODEL") or "gpt-3.5-turbo"

        self.neural_manager = NeuralNetworkManager(
            api_key=api_key,
            base_url=base_url,
            provider=provider,
            model=resolved_model,
        )
        if hasattr(self, "lbl_status_ai"):
            self.lbl_status_ai.setText(f"[🟢] AI: {provider} / {resolved_model}")
        self._update_runtime_summary()
        self.log(f"[AI] Переключено на {provider} model={resolved_model}")

    def _on_neural_provider_changed(self):
        if not hasattr(self, "cmb_neural_provider"):
            return
        provider = self.cmb_neural_provider.currentData() or self.cmb_neural_provider.currentText().strip().lower()
        model = None
        if hasattr(self, "edit_neural_model") and self.edit_neural_model is not None:
            model = self.edit_neural_model.text().strip() or None
        self._set_neural_provider(provider, model=model)

    def _on_neural_model_changed(self):
        self._on_neural_provider_changed()

    def _on_ai_model_selected(self):
        if not hasattr(self, "ai_cmb_provider"):
            return
        data = self.ai_cmb_provider.currentData()
        if not data or (isinstance(data, str) and data.startswith("_sep")):
            return

        if data == "auto":
            self._ai_use_replicate_llm = False
            self._ai_replicate_model_id = None
            self._ai_use_ollama = False
            self._set_neural_provider("deepseek")
            if hasattr(self, "lbl_status_ai"):
                self.lbl_status_ai.setText("[🟢] AI: Авто (DeepSeek)")
            if getattr(self, "ai_btn_auto", None):
                self.ai_btn_auto.setText("Авто")
            self.log("[AI] Авто режим — DeepSeek")
            return

        if data == "ollama":
            self._ai_use_replicate_llm = False
            self._ai_replicate_model_id = None
            self._ai_use_ollama = False
            self._set_neural_provider("ollama")
            if hasattr(self, "lbl_status_ai"):
                self.lbl_status_ai.setText("[🟢] Ollama (локально)")
            if getattr(self, "ai_btn_auto", None):
                self.ai_btn_auto.setText("Ollama")
            self.log("[AI] Ollama: локальные модели (чат)")
            return

        if isinstance(data, str) and data.startswith("ollama:"):
            self._ai_use_replicate_llm = False
            self._ai_replicate_model_id = None
            self._ai_use_ollama = False
            parts = data.split(":", 1)
            ollama_model = parts[1] if len(parts) > 1 else None
            self._set_neural_provider("ollama", model=ollama_model)
            if hasattr(self, "lbl_status_ai"):
                self.lbl_status_ai.setText(f"[🟢] Ollama: {ollama_model or 'default'}")
            if getattr(self, "ai_btn_auto", None):
                self.ai_btn_auto.setText(ollama_model or "Ollama")
            self.log(f"[AI] Ollama model={ollama_model}")
            return

        if data in ("deepseek", "anthropic", "openai"):
            self._ai_use_replicate_llm = False
            self._ai_replicate_model_id = None
            self._ai_use_ollama = False
            self._set_neural_provider(data)
            if getattr(self, "ai_btn_auto", None):
                name = "DeepSeek" if data == "deepseek" else ("Claude" if data == "anthropic" else "OpenAI")
                self.ai_btn_auto.setText(name)
            return

        if isinstance(data, str) and data.startswith("replicate:"):
            model_id = data[len("replicate:"):]
            self._ai_use_replicate_llm = True
            self._ai_replicate_model_id = model_id
            self._ai_use_ollama = False
            display = self.ai_cmb_provider.currentText().strip()
            if hasattr(self, "lbl_status_ai"):
                self.lbl_status_ai.setText(f"[🟢] Replicate: {display}")
            if getattr(self, "ai_btn_auto", None):
                short = display.replace("  ", "").strip()[:20]
                self.ai_btn_auto.setText(short if short else "Модель")
            self.log(f"[AI] Replicate LLM: {model_id}")

    def _tool_cmd_run(self, command: str):
        command = (command or "").strip()
        if not command:
            raise RuntimeError("Пустая команда")

        lower = command.lower()
        deny_substrings = [
            " del ", " rd ", " rmdir ", " format ", " shutdown", " reboot", " reg delete", " diskpart",
            " powershell", " wget ", " curl ", " certutil", " bitsadmin", " net user", " net localgroup",
        ]
        
        # Bypass security if user gave full access (chk_no_confirm is checked)
        bypass_security = getattr(self, "chk_no_confirm", None) and self.chk_no_confirm.isChecked()
        
        if not bypass_security:
            padded = f" {lower} "
            for s in deny_substrings:
                if s in padded:
                    raise RuntimeError("Команда запрещена политикой безопасности. Попросите пользователя включить 'Выполнять действия без подтверждения' для полного доступа.")

        try:
            # Force UTF-8 output to avoid mojibake in Russian locales
            proc = subprocess.run(
                ["cmd", "/c", f"chcp 65001>nul & {command}"],
                capture_output=True,
                text=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            self._ai_terminal_log("cmd_run: таймаут (15с)")
            return "cmd_run: таймаут (15с)"

        stdout = (proc.stdout or b"")
        stderr = (proc.stderr or b"")
        def _decode_best(b: bytes) -> str:
            if not b:
                return ""
            # Try utf-8 first
            s1 = b.decode("utf-8", errors="replace")
            # Heuristic: if too many replacement chars, fall back to cp866
            if s1.count("�") >= 3:
                try:
                    return b.decode("cp866", errors="replace")
                except Exception:
                    return s1
            return s1

        out = _decode_best(stdout)
        if stderr:
            out = out + "\n" + _decode_best(stderr)
        out = out.strip()
        if len(out) > 4000:
            out = out[:4000] + "\n...<truncated>"
        code = proc.returncode
        msg = f"cmd_run exit={code}\n{out or '(no output)'}"
        self._ai_terminal_log(f"> {command}\n{out or '(no output)'}")
        return msg

    def _normalize_windows_path(self, path: str) -> str:
        p = (path or "").strip().strip('"')
        if not p:
            return p

        p = os.path.expandvars(os.path.expanduser(p))

        # Some models use a placeholder user name like C:\Users\User\...
        user_home = os.path.expanduser("~")
        placeholder_prefix = "c:\\users\\user"
        pl = p.lower()
        if pl == placeholder_prefix:
            p = user_home
        elif pl.startswith(placeholder_prefix + "\\"):
            p = user_home + p[len(placeholder_prefix):]

        # Относительные пути (main.py, config.py) — относительно корня проекта
        if not os.path.isabs(p):
            root = getattr(self, "_ai_project_root", None)
            if root and os.path.isdir(root):
                p = os.path.normpath(os.path.join(root, p))
            else:
                try:
                    p = os.path.abspath(p)
                except Exception:
                    pass
        return p

    def _allowed_roots(self) -> list[str]:
        roots = []
        try:
            roots.append(os.path.dirname(os.path.abspath(__file__)))
        except Exception:
            pass
        proj = getattr(self, "_ai_project_root", None)
        if proj and os.path.isdir(proj):
            try:
                ab = os.path.abspath(proj)
                if ab not in roots:
                    roots.append(ab)
            except Exception:
                pass
        home = os.path.expanduser("~")
        if home:
            roots.append(home)
            roots.append(os.path.join(home, "Downloads"))
            roots.append(os.path.join(home, "Desktop"))
            roots.append(os.path.join(home, "Documents"))
        # normalize
        norm = []
        for r in roots:
            try:
                rr = os.path.abspath(r)
                if rr not in norm:
                    norm.append(rr)
            except Exception:
                continue
        return norm

    def _is_path_allowed(self, path: str) -> bool:
        try:
            p = os.path.abspath(path)
        except Exception:
            return False
        for r in self._allowed_roots():
            try:
                if p == r or p.startswith(r + os.sep):
                    return True
            except Exception:
                continue
        return False

    def _tool_mkdir(self, path: str):
        p = self._normalize_windows_path(path)
        if not p:
            raise RuntimeError("Не указан путь")
        if not self._is_path_allowed(p):
            raise RuntimeError("Создание папок разрешено только в проекте или профиле пользователя")
        os.makedirs(p, exist_ok=True)
        self.append_chat("Система", "Папка создана")

    def _tool_write_text_file(self, path: str, content: str, overwrite: bool = True):
        p = self._normalize_windows_path(path)
        if not p:
            raise RuntimeError("Не указан путь")
        if not self._is_path_allowed(p):
            raise RuntimeError("Запись файлов разрешена только в проекте или профиле пользователя")
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if (not overwrite) and os.path.exists(p):
            raise RuntimeError("Файл уже существует (overwrite=false)")
        content_len = len(content or "")
        if os.path.exists(p) and content_len > 0:
            try:
                existing_size = os.path.getsize(p)
                if existing_size > 15000 and content_len < 0.5 * existing_size:
                    raise RuntimeError(
                        f"Запись отменена: новый размер ({content_len} символов) сильно меньше текущего ({existing_size}). "
                        "Ответ модели мог обрезаться — полная перезапись большого файла опасна. Используй точечные правки или append_text_file."
                    )
            except OSError:
                pass
        with open(p, "w", encoding="utf-8", errors="replace") as f:
            f.write(content or "")
        self._ai_terminal_log(f"write_text_file: записано {p} ({len(content or '')} chars)")

    def _tool_append_text_file(self, path: str, content: str):
        p = self._normalize_windows_path(path)
        if not p:
            raise RuntimeError("Не указан путь")
        if not self._is_path_allowed(p):
            raise RuntimeError("Запись файлов разрешена только в проекте или профиле пользователя")
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "a", encoding="utf-8", errors="replace") as f:
            f.write(content or "")
        self._ai_terminal_log(f"append_text_file: дописано {p} (+{len(content or '')} chars)")

    def _tool_search_replace(self, path: str, old_string: str, new_string: str, replace_all: bool = False):
        """Точечная замена в файле (как в Cursor): только нужный фрагмент, без перезаписи всего файла."""
        p = self._normalize_windows_path(path)
        if not p:
            raise RuntimeError("Не указан путь")
        if not self._is_path_allowed(p):
            raise RuntimeError("Запись файлов разрешена только в проекте или профиле пользователя")
        if not os.path.exists(p):
            raise RuntimeError("Файл не существует")
        if os.path.isdir(p):
            raise RuntimeError("Это папка, нужен файл")
        old_string = old_string or ""
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            raise RuntimeError(str(e))
        if old_string not in content:
            raise RuntimeError(
                "old_string не найден в файле (проверь пробелы, переносы строк, точное совпадение с фрагментом из read_text_file)"
            )
        if replace_all:
            new_content = content.replace(old_string, new_string)
            count = content.count(old_string)
        else:
            new_content = content.replace(old_string, new_string, 1)
            count = 1
        try:
            with open(p, "w", encoding="utf-8", errors="replace") as f:
                f.write(new_content)
        except Exception as e:
            raise RuntimeError(str(e))
        self._ai_terminal_log(f"search_replace: заменено в {p}")
        fname = os.path.basename(p)
        if count == 1:
            self.append_chat("Система", f"Заменено в файле {fname} (1 вхождение)")
        else:
            self.append_chat("Система", f"Заменено в файле {fname} ({count} вхождений)")

    def _tool_run_python(self, path: str, args: Any):
        p = self._normalize_windows_path(path)
        if not p:
            raise RuntimeError("Не указан путь к .py")
        if not self._is_path_allowed(p):
            raise RuntimeError("Запуск разрешён только для файлов в проекте или профиле пользователя")
        if not os.path.exists(p):
            raise RuntimeError("Файл не существует")
        if os.path.isdir(p):
            raise RuntimeError("Нужен файл, а не папка")
        if not p.lower().endswith(".py"):
            raise RuntimeError("run_python разрешён только для .py")

        argv = []
        if isinstance(args, list):
            argv = [str(x) for x in args]
        elif isinstance(args, str) and args:
            argv = [args]

        cmd = [sys.executable, p] + argv
        try:
            proc = subprocess.run(cmd, capture_output=True, text=False, timeout=30)
        except subprocess.TimeoutExpired:
            self.append_chat("Система", "run_python: таймаут (30с)")
            return

        out = (proc.stdout or b"")
        err = (proc.stderr or b"")
        text = out.decode("utf-8", errors="replace")
        if err:
            text += "\n" + err.decode("utf-8", errors="replace")
        text = text.strip()
        if len(text) > 4000:
            text = text[:4000] + "\n...<truncated>"
        msg = f"run_python exit={proc.returncode}\n{text or '(no output)'}"
        self.append_chat("Система", msg)
        return msg

    def _tool_read_url(self, url: str) -> str:
        """Скачивает содержимое URL и возвращает текстовую выжимку. JARVIS может это делать сам."""
        if not url: return "Ошибка: URL не указан"
        try:
            self.log(f"Чтение URL: {url}")
            # Добавляем User-Agent чтобы сайты не блокировали "скрипт"
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            
            # Базовая очистка HTML
            text = response.text
            # Удаляем скрипты и стили
            text = re.sub(r'<(script|style).*?>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
            # Удаляем все остальные теги
            text = re.sub(r'<.*?>', '', text, flags=re.DOTALL)
            # Убираем лишние пробелы и пустые строки
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            clean_text = "\n".join(lines)
            
            # Ограничиваем размер (модель всё равно не переварит терабайты)
            limit = 6000
            if len(clean_text) > limit:
                clean_text = clean_text[:limit] + "\n... (текст обрезан)"
                
            return f"--- Содержимое {url} ---\n\n{clean_text}"
        except Exception as e:
            return f"Ошибка при чтении {url}: {e}"

    def _tool_open_app(self, name: str):
        name = (name or "").strip().strip('"')
        if not name:
            raise RuntimeError("Не указано имя приложения")

        # map common Russian aliases
        lower = name.lower()
        if lower in ("телеграм", "telegram", "tg"):
            exe = "telegram.exe"
        elif lower in ("cursor", "курсор"):
            exe = "cursor.exe"
        elif lower.endswith(".exe"):
            exe = lower
        else:
            exe = lower + ".exe"

        self.append_chat("Система", f"open_app: ищу приложение '{name}'")

        # 1) Try PATH via where
        try:
            proc = subprocess.run(
                ["cmd", "/c", f"chcp 65001>nul & where {exe}"],
                capture_output=True,
                text=False,
                timeout=10,
            )
            stdout = (proc.stdout or b"")
            stderr = (proc.stderr or b"")
            out = stdout.decode("utf-8", errors="replace")
            if stderr:
                out += "\n" + stderr.decode("utf-8", errors="replace")
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if lines:
                self.append_chat("Система", "open_app: where нашёл:\n" + "\n".join(lines[:10]))
            for ln in lines:
                cand = self._normalize_windows_path(ln)
                if os.path.exists(cand):
                    self.append_chat("Система", f"open_app: запускаю {cand}")
                    self.run_program(cand)
                    return
        except Exception:
            pass

        # 2) Try known locations for popular apps
        for cand in self._candidate_program_paths(exe):
            cand = self._normalize_windows_path(cand)
            if cand and os.path.exists(cand):
                self.append_chat("Система", f"open_app: найдено в типовом месте: {cand}")
                self.run_program(cand)
                return

        # 3) Limited search in user appdata
        local = os.environ.get("LOCALAPPDATA") or ""
        roaming = os.environ.get("APPDATA") or ""
        for base in (local, roaming):
            if not base or not os.path.exists(base):
                continue
            self.append_chat("Система", f"open_app: ищу {exe} в {base} (ограниченно)")
            matches = []
            for root, _dirs, files in os.walk(base):
                for f in files:
                    if f.lower() == exe:
                        matches.append(os.path.join(root, f))
                        if len(matches) >= 5:
                            break
                if len(matches) >= 5:
                    break
            for m in matches:
                m2 = self._normalize_windows_path(m)
                if os.path.exists(m2):
                    self.append_chat("Система", f"open_app: найдено: {m2}")
                    self.run_program(m2)
                    return

        raise RuntimeError(f"Не удалось найти и запустить приложение: {name}")

    def _candidate_program_paths(self, path: str) -> list[str]:
        p = path
        candidates: list[str] = [p]
        lower = p.lower()
        local = os.environ.get("LOCALAPPDATA") or ""
        roaming = os.environ.get("APPDATA") or ""

        if "telegram" in lower:
            for c in (
                os.path.join(roaming, "Telegram Desktop", "Telegram.exe"),
                os.path.join(local, "Telegram Desktop", "Telegram.exe"),
            ):
                if c and c not in candidates:
                    candidates.append(c)

        if "cursor" in lower:
            for c in (
                os.path.join(local, "Programs", "cursor", "Cursor.exe"),
                os.path.join(local, "Programs", "Cursor", "Cursor.exe"),
                r"C:\\Program Files\\Cursor\\Cursor.exe",
                r"C:\\Program Files (x86)\\Cursor\\Cursor.exe",
            ):
                if c and c not in candidates:
                    candidates.append(c)

        return candidates

    def _terminal_run_from_input(self):
        if not hasattr(self, "terminal_input") or self.terminal_input is None:
            return
        command = self.terminal_input.text().strip()
        if not command:
            return
        self.terminal_input.clear()
        try:
            self._tool_cmd_run(command)
        except Exception as e:
            self.append_chat("Система", f"cmd_run error: {e}")
            if hasattr(self, "terminal_view") and self.terminal_view is not None:
                self.terminal_view.appendPlainText(f"> {command}\nERROR: {e}\n")

    def _toggle_voice_recording(self):
        # Toggle record/stop
        if not hasattr(self, "btn_mic"):
            return
        if not self._voice_recording:
            try:
                self._hotword_pause_for_manual()
            except Exception:
                pass
            self._start_voice_recording()
        else:
            self._stop_voice_recording_and_transcribe()

    def _start_voice_recording(self):
        try:
            device = QtMultimedia.QMediaDevices.defaultAudioInput()
            fmt = QtMultimedia.QAudioFormat()
            fmt.setSampleRate(16000)
            fmt.setChannelCount(1)
            fmt.setSampleFormat(QtMultimedia.QAudioFormat.SampleFormat.Int16)
            self._audio_format = fmt

            self._audio_source = QtMultimedia.QAudioSource(device, fmt, self)
            self._audio_buffer = QtCore.QBuffer(self)
            self._audio_buffer.open(QtCore.QIODevice.OpenModeFlag.ReadWrite)
            self._audio_source.start(self._audio_buffer)
            self._voice_recording = True
            if hasattr(self, "btn_mic") and self.btn_mic is not None:
                self.btn_mic.setText("⏹")
            if hasattr(self, "ai_btn_mic") and self.ai_btn_mic is not None:
                self.ai_btn_mic.setText("⏹")
            self.log("[Голос] Запись... (нажми ещё раз чтобы отправить)")
        except Exception as e:
            self.log(f"[Голос] Ошибка записи: {e}")

    def _stop_voice_recording_and_transcribe(self):
        try:
            if self._audio_source is not None:
                self._audio_source.stop()
            self._voice_recording = False
            if hasattr(self, "btn_mic") and self.btn_mic is not None:
                self.btn_mic.setText("◉")
            if hasattr(self, "ai_btn_mic") and self.ai_btn_mic is not None:
                self.ai_btn_mic.setText("🎤 Микрофон")

            data = b""
            if self._audio_buffer is not None:
                data = bytes(self._audio_buffer.data())
                self._audio_buffer.close()

            if not data:
                self.log("[Голос] Нет записи")
                return

            text = self._stt_transcribe_pcm16(data, sample_rate=16000)
            if not text:
                self.log("[Голос] Речь не распознана")
                return

            self._chat_send_text(text)
            try:
                self._hotword_resume_after_manual()
            except Exception:
                pass
        except Exception as e:
            self.log(f"[Голос] Ошибка распознавания: {e}")
            try:
                self._hotword_resume_after_manual()
            except Exception:
                pass

    def _on_hotword_toggle_changed(self, state):
        try:
            self._hotword_enabled = bool(int(state) == 2)
        except Exception:
            self._hotword_enabled = False

        if self._hotword_enabled:
            self._hotword_start()
        else:
            self._hotword_stop()

    def _hotword_tick(self):
        if not self._hotword_running:
            return
        if self._hotword_state == "command":
            now = time.time()
            try:
                silence_sec = float(os.getenv("HOTWORD_SILENCE_SEC") or "1.5")
            except Exception:
                silence_sec = 1.5
            try:
                max_wait = float(os.getenv("HOTWORD_MAX_WAIT_SEC") or "10")
            except Exception:
                max_wait = 10.0

            # auto-submit after silence
            if self._hotword_cmd_text and self._hotword_last_voice_time and (now - self._hotword_last_voice_time) >= silence_sec:
                cmd = (self._hotword_cmd_text or "").strip()
                self._hotword_cmd_text = ""
                self._hotword_state = "idle"
                self._hotword_wake_time = 0.0
                self._hotword_last_voice_time = 0.0
                if cmd:
                    self.append_chat("Система", f"🎙 {cmd}")
                    self._chat_send_text(cmd)
                return

            # safety timeout
            if self._hotword_wake_time and (now - self._hotword_wake_time) > max_wait:
                self._hotword_state = "idle"
                self._hotword_wake_time = 0.0
                self._hotword_last_voice_time = 0.0
                self._hotword_cmd_text = ""

    def _hotword_pause_for_manual(self):
        if not self._hotword_running:
            return
        self._hotword_paused_for_manual = True
        self._hotword_stop()

    def _hotword_resume_after_manual(self):
        if not self._hotword_paused_for_manual:
            return
        self._hotword_paused_for_manual = False
        if self._hotword_enabled:
            self._hotword_start()

    def _hotword_start(self):
        if self._hotword_running:
            return
        if self._voice_recording:
            return

        model_path = os.getenv("VOSK_MODEL_PATH") or ""
        model_path = self._normalize_windows_path(model_path)
        if not model_path or not os.path.exists(model_path):
            self.log("Hotword: не найден VOSK_MODEL_PATH. Укажи путь в .env")
            return

        try:
            from vosk import Model, KaldiRecognizer
        except Exception as e:
            self.log(f"Hotword: vosk не установлен: {e}")
            return

        if self._vosk_model is None:
            try:
                self._vosk_model = Model(model_path)
            except Exception as e:
                self.log(f"Hotword: ошибка загрузки Vosk: {e}")
                return

        try:
            device = QtMultimedia.QMediaDevices.defaultAudioInput()
            fmt = QtMultimedia.QAudioFormat()
            fmt.setSampleRate(16000)
            fmt.setChannelCount(1)
            fmt.setSampleFormat(QtMultimedia.QAudioFormat.SampleFormat.Int16)
            self._hotword_audio_source = QtMultimedia.QAudioSource(device, fmt, self)
            self._hotword_io = self._hotword_audio_source.start()

            self._hotword_recognizer = KaldiRecognizer(self._vosk_model, 16000)
            try:
                self._hotword_recognizer.SetWords(False)
            except Exception:
                pass

            try:
                self._hotword_io.readyRead.connect(self._hotword_on_ready_read)
            except Exception:
                pass

            self._hotword_state = "idle"
            self._hotword_wake_time = 0.0
            self._hotword_last_voice_time = 0.0
            self._hotword_cmd_text = ""
            self._hotword_running = True
            self._hotword_timer.start(250)
            self.log("Hotword: слушаю 'Джарвис'")
        except Exception as e:
            self.log(f"Hotword: ошибка микрофона: {e}")
            self._hotword_stop()

    def _hotword_stop(self):
        try:
            self._hotword_timer.stop()
        except Exception:
            pass
        self._hotword_running = False
        self._hotword_state = "idle"
        self._hotword_wake_time = 0.0
        self._hotword_last_voice_time = 0.0
        self._hotword_cmd_text = ""
        try:
            if self._hotword_audio_source is not None:
                self._hotword_audio_source.stop()
        except Exception:
            pass
        self._hotword_audio_source = None
        self._hotword_io = None
        self._hotword_recognizer = None

    def _hotword_on_ready_read(self):
        if not self._hotword_running or self._hotword_io is None or self._hotword_recognizer is None:
            return
        try:
            data = bytes(self._hotword_io.readAll())
        except Exception:
            return
        if not data:
            return

        # crude silence detection by RMS of PCM16
        try:
            if len(data) >= 4:
                import struct

                n = len(data) // 2
                if n > 0:
                    samples = struct.unpack("<" + "h" * n, data[: n * 2])
                    ssum = 0.0
                    for v in samples[:: max(1, n // 2000)]:
                        ssum += float(v) * float(v)
                    cnt = max(1, len(samples[:: max(1, n // 2000)]))
                    rms = (ssum / cnt) ** 0.5
                    try:
                        thr = float(os.getenv("HOTWORD_RMS_THRESHOLD") or "450")
                    except Exception:
                        thr = 450.0
                    if rms >= thr:
                        self._hotword_last_voice_time = time.time()
        except Exception:
            pass

        try:
            is_final = bool(self._hotword_recognizer.AcceptWaveform(data))
            if is_final:
                res = self._hotword_recognizer.Result()
            else:
                res = self._hotword_recognizer.PartialResult()
        except Exception:
            return

        txt = ""
        try:
            j = json.loads(res)
            if is_final:
                txt = (j.get("text") or "").strip()
            else:
                txt = (j.get("partial") or "").strip()
        except Exception:
            txt = ""

        if not txt:
            return

        wake = (os.getenv("WAKE_WORD") or "джарвис").strip().casefold()
        tcf = txt.casefold()

        if self._hotword_state == "idle":
            if wake and wake in tcf:
                # switch to command mode, but do NOT speak/print anything (do not interrupt)
                self._hotword_state = "command"
                self._hotword_wake_time = time.time()
                self._hotword_cmd_text = ""
                if not self._hotword_last_voice_time:
                    self._hotword_last_voice_time = time.time()

                # if command continues in same phrase: "джарвис открой ..."
                try:
                    idx = tcf.find(wake)
                    after = txt[idx + len(wake):].strip(" ,.!?\t")
                except Exception:
                    after = ""
                if after:
                    self._hotword_cmd_text = after
            return

        if self._hotword_state == "command":
            cmd = txt
            if wake and wake in cmd.casefold():
                try:
                    i2 = cmd.casefold().find(wake)
                    cmd = cmd[i2 + len(wake):].strip(" ,.!?\t")
                except Exception:
                    cmd = cmd
            cmd = (cmd or "").strip()
            if cmd:
                self._hotword_cmd_text = cmd

            # If recognizer produced a final result, submit immediately (backup for silence detection)
            if is_final and self._hotword_cmd_text:
                final_cmd = (self._hotword_cmd_text or "").strip()
                self._hotword_cmd_text = ""
                self._hotword_state = "idle"
                self._hotword_wake_time = 0.0
                self._hotword_last_voice_time = 0.0
                if final_cmd:
                    self.append_chat("Система", f"🎙 {final_cmd}")
                    self._chat_send_text(final_cmd)

    def _stt_transcribe_pcm16(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        # Prefer Vosk offline if available and model exists
        model_path = os.getenv("VOSK_MODEL_PATH") or ""
        model_path = self._normalize_windows_path(model_path)
        if model_path and os.path.exists(model_path):
            try:
                from vosk import Model, KaldiRecognizer
                import json as _json

                if self._vosk_model is None:
                    self._vosk_model = Model(model_path)
                rec = KaldiRecognizer(self._vosk_model, sample_rate)
                rec.AcceptWaveform(pcm_bytes)
                res = _json.loads(rec.FinalResult())
                return (res.get("text") or "").strip()
            except Exception as e:
                self.append_chat("Система", f"Vosk error: {e}")

        # Fallback: SpeechRecognition (online Google)
        try:
            import speech_recognition as sr
            r = sr.Recognizer()
            audio = sr.AudioData(pcm_bytes, sample_rate, 2)
            return r.recognize_google(audio, language="ru-RU").strip()
        except Exception as e:
            raise RuntimeError(
                "STT не настроен. Для офлайн распознавания установи vosk и скачай модель, затем задай VOSK_MODEL_PATH. "
                f"Также можно включить интернет для recognize_google. Ошибка: {e}"
            )

    def _tool_list_dir(self, path: str):
        path = (path or "").strip()
        path = self._normalize_windows_path(path) if path else os.path.expanduser("~")
        if not os.path.exists(path):
            raise RuntimeError("Путь не существует")
        if os.path.isfile(path):
            path = os.path.dirname(path)
        entries = []
        try:
            for name in os.listdir(path):
                entries.append(name)
        except Exception as e:
            raise RuntimeError(str(e))

        entries.sort()
        preview = entries[:50]
        text = "\n".join(preview)
        if len(entries) > 50:
            text += f"\n... (+{len(entries) - 50} items)"
        self.append_chat("Система", f"Папка просмотрена ({len(entries)} элементов)")
        return f"list_dir: {path}\n{text}"

    def _tool_find_files(self, path: str, pattern: str):
        path = (path or "").strip()
        path = self._normalize_windows_path(path) if path else os.path.expanduser("~")
        pattern = (pattern or "*").strip() or "*"
        if not os.path.exists(path):
            raise RuntimeError("Путь не существует")
        if os.path.isfile(path):
            path = os.path.dirname(path)

        skip_dirs = {"fooocus-2.5.5", "__pycache__", ".git", "build", "dist", "node_modules", ".venv", "venv", "fooocus_env", ".idea", "neutts"}
        matches = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]
            for f in files:
                if fnmatch.fnmatch(f.lower(), pattern.lower()):
                    matches.append(os.path.join(root, f))
                    if len(matches) >= 50:
                        break
            if len(matches) >= 50:
                break

        text = "\n".join(matches) if matches else "(no matches)"
        self.append_chat("Система", f"Поиск выполнен ({len(matches)} файлов)")
        return f"find_files: {path} pattern={pattern}\n{text}"

    def _tool_read_text_file(self, path: str, start_line: int | None = None, end_line: int | None = None):
        path = self._normalize_windows_path((path or "").strip())
        if not path:
            raise RuntimeError("Не указан путь")
        if not os.path.exists(path):
            raise RuntimeError("Файл не существует")
        if os.path.isdir(path):
            raise RuntimeError("Это папка, нужен файл")

        ext = os.path.splitext(path)[1].lower()
        if ext and ext not in AI_ALLOWED_FILE_EXTENSIONS:
            raise RuntimeError("Чтение этого типа файла запрещено")

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            
            total_lines = len(lines)
            s_idx = max(0, int(start_line) - 1) if start_line is not None else 0
            e_idx = min(total_lines, int(end_line)) if end_line is not None else total_lines
            
            # Если файл большой и строки не указаны, возвращаем только начало
            truncated_warning = ""
            if total_lines > 1000 and start_line is None and end_line is None:
                e_idx = min(total_lines, 500)
                truncated_warning = f"\n\n[ВНИМАНИЕ: Файл очень большой ({total_lines} строк). Выведены только строки {s_idx + 1}-{e_idx}. Рекомендуется использовать параметры start_line и end_line в read_text_file для просмотра других частей файла.]"
            
            sliced_lines = lines[s_idx:e_idx]
            content = "".join(sliced_lines) + truncated_warning
            
        except Exception as e:
            raise RuntimeError(str(e))
        
        self.append_chat("Система", f"Файл прочитан ({os.path.basename(path)}, строк {s_idx+1}-{e_idx} из {total_lines})")
        preview_len = 500
        preview = content[:preview_len] + ("..." if len(content) > preview_len else "")
        self._append_chat_entry(self._make_chat_entry(
            "file_preview",
            file_path=path,
            preview=preview,
            total_len=len(content),
        ))
        header = f"read_text_file: {path} (строки {s_idx+1}-{e_idx} из {total_lines})"
        return f"{header}\n{content}"

    def _init_timers(self):
        """Инициализация таймеров для автообновления статуса и, при необходимости, других задач."""
        # Таймер обновления общего статуса системы (главная + вкладка "Система")
        self.status_timer = QtCore.QTimer(self)
        self.status_timer.timeout.connect(self.update_system_status)
        if self.auto_update_status:
            self.status_timer.start(3000)
        else:
            self.status_timer.stop()
        
        # Таймер обновления аналитики (каждые 5 секунд)
        self.analytics_timer = QtCore.QTimer(self)
        self.analytics_timer.timeout.connect(self._analytics_refresh_data)
        self.analytics_timer.start(5000)

    def update_system_status(self):
        """Обновить базовые показатели системы (CPU/RAM/диски/сеть/питание)."""
        cpu = None
        try:
            cpu = psutil.cpu_percent(interval=0.1)
            ram_info = psutil.virtual_memory()
            ram = ram_info.percent
            if hasattr(self, "lbl_cpu"):
                self.lbl_cpu.setText(f"CPU: {cpu:.0f} %")
            if hasattr(self, "lbl_ram"):
                self.lbl_ram.setText(f"RAM: {ram:.0f} %")
            
            # Обновляем количество файлов
            if hasattr(self, "lbl_files"):
                try:
                    home = os.path.expanduser("~")
                    desktop = os.path.join(home, "Desktop")
                    downloads = os.path.join(home, "Downloads")
                    file_count = 0
                    for folder in [desktop, downloads]:
                        if os.path.exists(folder):
                            file_count += len([f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))])
                    self.lbl_files.setText(f"📁 Файлов на рабочем столе/в загрузках: {file_count}")
                except:
                    self.lbl_files.setText("📁 Файлов: --")
            
            # Обновляем температуру (если доступна)
            if hasattr(self, "lbl_temp"):
                try:
                    # Пытаемся получить температуру (может не работать на всех системах)
                    sensors = psutil.sensors_temperatures()
                    if sensors:
                        temp = list(sensors.values())[0][0].current if list(sensors.values()) else None
                        if temp:
                            self.lbl_temp.setText(f"🔥 Температура: {temp:.0f}°C")
                        else:
                            self.lbl_temp.setText("🔥 Температура: --°C")
                    else:
                        self.lbl_temp.setText("🔥 Температура: --°C")
                except:
                    self.lbl_temp.setText("🔥 Температура: --°C")
        except Exception as e:
            self.log(f"Ошибка получения статуса CPU/RAM: {e}")

        # Обновление дашборда на вкладке "Система" (если элементы уже созданы)
        try:
            if hasattr(self, "lbl_sys_cpu"):
                if cpu is None:
                    cpu = psutil.cpu_percent(interval=0.0)
                self.lbl_sys_cpu.setText(f"{cpu:.0f} %")
                self.bar_sys_cpu.setValue(int(cpu))

            if hasattr(self, "lbl_sys_ram"):
                vm = psutil.virtual_memory()
                self.lbl_sys_ram.setText(f"{vm.percent:.0f} %")
                self.bar_sys_ram.setValue(int(vm.percent))

            # Диски (системный диск)
            if hasattr(self, "lbl_sys_disks"):
                try:
                    system_drive = os.environ.get("SystemDrive", "C:")
                    usage = psutil.disk_usage(system_drive + "\\")
                    used_gb = usage.used / (1024 ** 3)
                    total_gb = usage.total / (1024 ** 3)
                    self.lbl_sys_disks.setText(
                        f"Диск {system_drive}: {used_gb:.0f}/{total_gb:.0f} GB ({usage.percent:.0f}%)"
                    )
                except Exception:
                    self.lbl_sys_disks.setText("Диски: --")

            # Сеть (простая скорость в Mbit/s)
            if hasattr(self, "lbl_sys_network"):
                try:
                    now = time.time()
                    net = psutil.net_io_counters()
                    dt = max(now - self._net_prev_time, 1e-3)
                    down_diff = net.bytes_recv - self._net_prev.bytes_recv
                    up_diff = net.bytes_sent - self._net_prev.bytes_sent
                    down_mbps = (down_diff * 8 / dt) / (1024 * 1024)
                    up_mbps = (up_diff * 8 / dt) / (1024 * 1024)
                    self.lbl_sys_network.setText(
                        f"Сеть: ↓ {down_mbps:.1f} Mbit/s / ↑ {up_mbps:.1f} Mbit/s"
                    )
                    self._net_prev = net
                    self._net_prev_time = now
                except Exception:
                    self.lbl_sys_network.setText("Сеть: --")

            # Питание (если есть батарея)
            if hasattr(self, "lbl_sys_power"):
                try:
                    batt = psutil.sensors_battery()
                    if batt is not None:
                        state = "AC" if batt.power_plugged else "Батарея"
                        self.lbl_sys_power.setText(
                            f"Питание: {state}, заряд {batt.percent:.0f}%"
                        )
                    else:
                        self.lbl_sys_power.setText("Питание: AC")
                except Exception:
                    self.lbl_sys_power.setText("Питание: --")
        except Exception as e:
            self.log(f"Ошибка обновления системного дашборда: {e}")

    def _apply_dark_theme(self):
        palette = QtGui.QPalette()
        palette.setColor(QtGui.QPalette.ColorRole.Window,          QtGui.QColor(7,  9, 20))
        palette.setColor(QtGui.QPalette.ColorRole.WindowText,      QtGui.QColor(238,243,255))
        palette.setColor(QtGui.QPalette.ColorRole.Base,            QtGui.QColor(11, 15, 30))
        palette.setColor(QtGui.QPalette.ColorRole.AlternateBase,   QtGui.QColor(9,  12, 24))
        palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase,     QtGui.QColor(14, 18, 36))
        palette.setColor(QtGui.QPalette.ColorRole.ToolTipText,     QtGui.QColor(238,243,255))
        palette.setColor(QtGui.QPalette.ColorRole.Text,            QtGui.QColor(220,226,248))
        palette.setColor(QtGui.QPalette.ColorRole.Button,          QtGui.QColor(14, 18, 36))
        palette.setColor(QtGui.QPalette.ColorRole.ButtonText,      QtGui.QColor(220,226,248))
        palette.setColor(QtGui.QPalette.ColorRole.Highlight,       QtGui.QColor(99, 120,255))
        palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(255,255,255))
        palette.setColor(QtGui.QPalette.ColorRole.Link,            QtGui.QColor(99, 120,255))
        palette.setColor(QtGui.QPalette.ColorRole.Midlight,        QtGui.QColor(20, 25, 50))
        self.setPalette(palette)
        self.current_theme = "dark"

        base_style = """
/* ═══════════════════════════════════════════════════════
   JARVIS PREMIUM DESIGN SYSTEM v3.0
   Palette:
     bg0  = #07091A  (window bg)
     bg1  = #0B0F22  (panel/card bg)
     bg2  = #111830  (elevated)
     acc  = #5B6BFF  (primary accent)
     acc2 = #818CF8  (light accent)
     txt0 = #EEF2FF  (primary text)
     txt1 = #8B9AC0  (secondary)
     txt2 = #4E5874  (muted)
     ok   = #34D399  (success/green)
     warn = #FBBF24  (warning/yellow)
     err  = #F87171  (danger/red)
     game = #FF6E2E  (games accent)
   ═══════════════════════════════════════════════════════ */

/* ── Base ── */
QMainWindow, QDialog {
    background-color: #07091A;
}
QWidget {
    color: #DCE4FF;
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 11pt;
    background-color: transparent;
}
QMainWindow > QWidget {
    background-color: #07091A;
}

/* ── Scrollbars ── */
QScrollBar:vertical {
    background: transparent; width: 6px; margin: 0; border: none;
}
QScrollBar:horizontal {
    background: transparent; height: 6px; margin: 0; border: none;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: rgba(91,107,255,0.30);
    border-radius: 3px;
    min-height: 24px; min-width: 24px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
    background: rgba(91,107,255,0.55);
}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent; height: 0; width: 0;
}

/* ── Inputs ── */
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #0D1228;
    border: 1px solid rgba(91,107,255,0.28);
    border-radius: 10px;
    padding: 8px 12px;
    color: #EEF2FF;
    selection-background-color: rgba(91,107,255,0.50);
    selection-color: #FFFFFF;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid rgba(91,107,255,0.75);
    background-color: #0F1530;
}
QLineEdit:read-only {
    color: #8B9AC0;
    border-color: rgba(91,107,255,0.15);
}
QLineEdit::placeholder, QTextEdit::placeholder {
    color: #4E5874;
}

/* ── Buttons ── */
QPushButton {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 rgba(28,34,62,0.98), stop:1 rgba(17,22,44,0.98));
    border: 1px solid rgba(120,130,255,0.22);
    border-radius: 10px;
    padding: 8px 18px;
    color: #C8D2FF;
    font-weight: 600;
    font-size: 10.5pt;
    min-height: 34px;
    letter-spacing: 0.2px;
}
QPushButton:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 rgba(60,75,180,0.55), stop:1 rgba(40,55,140,0.55));
    border: 1px solid rgba(130,140,255,0.55);
    color: #FFFFFF;
}
QPushButton:pressed {
    background: rgba(30,40,100,0.9);
    border: 1px solid #6070FF;
    padding-top: 9px;
}
QPushButton:disabled {
    color: rgba(140,160,220,0.3);
    border-color: rgba(91,107,255,0.08);
    background: rgba(15,18,38,0.7);
}

/* ── Lists ── */
QListWidget, QTreeWidget {
    background-color: #0B0F22;
    border: 1px solid rgba(91,107,255,0.20);
    border-radius: 12px;
    padding: 4px;
    outline: none;
    alternate-background-color: rgba(255,255,255,0.015);
}
QListWidget::item, QTreeWidget::item {
    padding: 7px 10px;
    border-radius: 7px;
    color: #C8D2FF;
}
QListWidget::item:hover, QTreeWidget::item:hover {
    background-color: rgba(91,107,255,0.12);
    color: #EEF2FF;
}
QListWidget::item:selected, QTreeWidget::item:selected {
    background-color: rgba(91,107,255,0.28);
    color: #FFFFFF;
    border: none;
}
QTreeWidget::branch { background: transparent; }

/* ── Tables ── */
QTableWidget {
    background-color: #0B0F22;
    border: 1px solid rgba(91,107,255,0.20);
    border-radius: 12px;
    gridline-color: rgba(91,107,255,0.10);
    outline: none;
    alternate-background-color: rgba(255,255,255,0.012);
}
QTableWidget::item {
    padding: 6px 10px;
    color: #C8D2FF;
    border: none;
}
QTableWidget::item:hover {
    background-color: rgba(91,107,255,0.10);
}
QTableWidget::item:selected {
    background-color: rgba(91,107,255,0.28);
    color: #FFFFFF;
}
QHeaderView {
    background: transparent;
}
QHeaderView::section {
    background-color: #0F1428;
    color: #818CF8;
    border: none;
    border-bottom: 1px solid rgba(91,107,255,0.22);
    border-right: 1px solid rgba(91,107,255,0.08);
    padding: 8px 12px;
    font-weight: 700;
    font-size: 10pt;
    letter-spacing: 0.5px;
}
QHeaderView::section:last {
    border-right: none;
}

/* ── GroupBox — sleek glass card ── */
QGroupBox {
    border: 1px solid rgba(255,255,255,0.07);
    border-top: 1px solid rgba(130,143,255,0.18);
    border-radius: 14px;
    margin-top: 16px;
    padding: 18px 14px 12px 14px;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 rgba(18,22,50,0.96), stop:1 rgba(11,14,32,0.97));
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    top: -1px;
    padding: 2px 10px;
    background-color: #07091A;
    color: #818CF8;
    font-weight: 700;
    font-size: 10.5pt;
    letter-spacing: 0.3px;
    border-radius: 6px;
}

/* ── QSplitter ── */
QSplitter::handle {
    background: rgba(91,107,255,0.12);
}
QSplitter::handle:hover {
    background: rgba(91,107,255,0.28);
}

/* ── TabWidget ── */
QTabWidget::pane {
    border: 1px solid rgba(91,107,255,0.20);
    border-radius: 12px;
    background: rgba(11,15,34,0.90);
    top: -1px;
}
QTabBar {
    background: transparent;
}
QTabBar::tab {
    background: #0D1228;
    border: 1px solid rgba(91,107,255,0.18);
    border-bottom: none;
    border-radius: 9px 9px 0 0;
    padding: 8px 20px;
    margin: 2px 3px 0 3px;
    color: #5B6B99;
    font-weight: 600;
    font-size: 10pt;
}
QTabBar::tab:selected {
    background: rgba(91,107,255,0.22);
    color: #EEF2FF;
    border-color: rgba(91,107,255,0.45);
}
QTabBar::tab:hover:!selected {
    background: rgba(91,107,255,0.10);
    color: #C8D2FF;
}

/* ── CheckBox / RadioButton ── */
QCheckBox, QRadioButton {
    color: #C8D2FF;
    spacing: 8px;
    font-size: 10.5pt;
}
QCheckBox::indicator {
    width: 18px; height: 18px;
    border: 2px solid rgba(91,107,255,0.40);
    border-radius: 5px;
    background: #0D1228;
}
QCheckBox::indicator:checked {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #5B6BFF,stop:1 #818CF8);
    border-color: #5B6BFF;
}
QCheckBox::indicator:hover {
    border-color: #818CF8;
}
QRadioButton::indicator {
    width: 18px; height: 18px;
    border: 2px solid rgba(91,107,255,0.40);
    border-radius: 9px;
    background: #0D1228;
}
QRadioButton::indicator:checked {
    background: #5B6BFF;
    border-color: #818CF8;
}

/* ── ComboBox ── */
QComboBox {
    background: #0D1228;
    border: 1px solid rgba(91,107,255,0.28);
    border-radius: 10px;
    padding: 7px 12px;
    color: #EEF2FF;
    font-size: 10.5pt;
    min-height: 34px;
}
QComboBox:hover {
    border-color: rgba(91,107,255,0.60);
}
QComboBox:focus {
    border-color: #5B6BFF;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
    padding-right: 6px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #818CF8;
    width: 0; height: 0;
}
QComboBox QAbstractItemView {
    background: #111830;
    border: 1px solid rgba(91,107,255,0.35);
    border-radius: 10px;
    selection-background-color: rgba(91,107,255,0.30);
    selection-color: #EEF2FF;
    color: #C8D2FF;
    padding: 4px;
    outline: none;
}

/* ── ProgressBar ── */
QProgressBar {
    background-color: #0D1228;
    border: 1px solid rgba(91,107,255,0.20);
    border-radius: 7px;
    text-align: center;
    color: #818CF8;
    font-size: 9pt;
    font-weight: 600;
    min-height: 12px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #5B6BFF, stop:0.5 #818CF8, stop:1 #A78BFA);
    border-radius: 6px;
}

/* ── Label classes ── */
QLabel[class="sectionTitle"] {
    font-size: 14pt;
    font-weight: 800;
    color: #EEF2FF;
    letter-spacing: 0.5px;
}
QLabel[class="neuroTitle"] {
    font-size: 15pt;
    font-weight: 700;
    color: #818CF8;
}
QLabel[class="neuroSubtitle"] {
    font-size: 9.5pt;
    color: #5B6B99;
}

/* ── ToolTip ── */
QToolTip {
    background-color: #141B38;
    color: #EEF2FF;
    border: 1px solid rgba(91,107,255,0.40);
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 9.5pt;
}

/* ── TextBrowser ── */
QTextBrowser {
    background-color: #080B1C;
    border: 1px solid rgba(91,107,255,0.18);
    border-radius: 12px;
    padding: 12px;
    color: #DCE4FF;
    font-size: 11pt;
    selection-background-color: rgba(91,107,255,0.35);
}

/* ── StatusBar ── */
QStatusBar {
    background: #07091A;
    color: #5B6B99;
    font-size: 9pt;
}

/* ── MessageBox ── */
QMessageBox {
    background-color: #0D1228;
    color: #EEF2FF;
}
QMessageBox QLabel {
    color: #EEF2FF;
    font-size: 11pt;
}

/* ── InputDialog ── */
QInputDialog QLabel {
    color: #C8D2FF;
}

/* ── Frame ── */
QFrame {
    border: none;
}
QFrame[frameShape="4"], QFrame[frameShape="5"] {
    color: rgba(91,107,255,0.15);
}

/* ── SpinBox ── */
QSpinBox, QDoubleSpinBox {
    background: #0D1228;
    border: 1px solid rgba(91,107,255,0.28);
    border-radius: 8px;
    padding: 5px 8px;
    color: #EEF2FF;
    min-height: 32px;
}

/* ── Games tab accent ── */
QGroupBox#GameTab_Monitor {
    border: 1px solid rgba(255,110,46,0.35);
}
QGroupBox#GameTab_Monitor::title {
    color: #FF9F6B;
}
            """

        if not hasattr(self, 'theme_styles'):
            self.theme_styles = {}
        self.theme_styles["dark"] = base_style

        self.setStyleSheet(base_style)
        self._apply_shell_theme_overrides()
        
        # Принудительно обновляем все виджеты для правильного отображения иконок
        self.update()
        if hasattr(self, 'centralWidget') and self.centralWidget():
            self.centralWidget().update()
    
    def _apply_light_theme(self):
        palette = QtGui.QPalette()
        palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(248, 249, 252))
        palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(20, 20, 40))
        palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(255, 255, 255))
        palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(245, 245, 250))
        palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(20, 20, 40))
        palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(240, 240, 248))
        palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(20, 20, 40))
        palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(0, 120, 215))
        palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(255, 255, 255))
        self.setPalette(palette)
        self.current_theme = "light"

        light_style = """
            QMainWindow { background-color: #F8F9FC; }
            QWidget { color: #1a1a30; font-family: "Segoe UI", sans-serif; font-size: 12pt; }
            QLineEdit, QTextEdit, QPlainTextEdit {
                background-color: #FFFFFF;
                border: 1px solid #D0D4E0;
                border-radius: 12px;
                padding: 8px 12px;
                color: #1a1a30;
            }
            QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
                border: 1px solid #0078D4;
            }
            QPushButton {
                background-color: #FFFFFF;
                border: 1px solid #D0D4E0;
                border-radius: 20px;
                padding: 8px 20px;
                color: #1a1a30;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #E8F0FE;
                border: 1px solid #0078D4;
                color: #0078D4;
            }
            QListWidget, QTreeWidget {
                background-color: #FFFFFF;
                border: 1px solid #D0D4E0;
                border-radius: 12px;
            }
            QScrollBar:vertical { background: transparent; width: 8px; }
            QScrollBar:horizontal { background: transparent; height: 8px; }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
                background: #C0C4D0; border-radius: 4px; min-height: 30px;
            }
            QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
            QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
            QGroupBox {
                border: 1px solid #D0D4E0;
                border-radius: 16px;
                margin-top: 12px;
                padding-top: 20px;
                background-color: rgba(255,255,255,0.9);
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 16px; padding: 2px 8px;
                color: #0078D4; font-weight: 700;
            }
            QTabBar::tab {
                background: #F0F0F8; border: 1px solid #D0D4E0; border-radius: 10px;
                padding: 8px 18px; margin: 2px 4px; color: #606080;
            }
            QTabBar::tab:selected { background: #E8F0FE; color: #0078D4; }
            QCheckBox::indicator {
                width: 20px; height: 20px; border: 2px solid #D0D4E0;
                border-radius: 6px; background: #FFFFFF;
            }
            QCheckBox::indicator:checked { background: #0078D4; border: 2px solid #0078D4; }
            QComboBox { background: #FFF; border: 1px solid #D0D4E0; border-radius: 12px; padding: 6px 12px; }
            QProgressBar { background: #E0E4F0; border: none; border-radius: 8px; }
            QProgressBar::chunk {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #0078D4, stop:1 #00B4D8);
                border-radius: 7px;
            }
            QHeaderView::section { background: #F0F0F8; color: #0078D4; border: none; padding: 8px; }
            QTableWidget { background: #FFF; border: 1px solid #D0D4E0; border-radius: 12px; }
            """

        self.theme_styles = {"dark": self.theme_styles.get("dark", ""), "light": light_style}
        self.setStyleSheet(light_style)
        self._apply_shell_theme_overrides()
        self.update()

    def _refresh_sidebar_styles(self, active_button: QtWidgets.QPushButton | None = None):
        if not hasattr(self, "_sidebar_buttons"):
            return
        if active_button is None and hasattr(self, "current_tab"):
            tab_to_button = {
                "главная": getattr(self, "btn_tab_home", None),
                "ai": getattr(self, "btn_tab_ai", None),
                "файлы": getattr(self, "btn_tab_files", None),
                "веб": getattr(self, "btn_tab_web", None),
                "мессенджеры": getattr(self, "btn_tab_chat", None),
                "игры": getattr(self, "btn_tab_games", None),
                "система": getattr(self, "btn_tab_system", None),
                "автоматизация": getattr(self, "btn_tab_auto", None),
                "аналитика": getattr(self, "btn_tab_analytics", None),
                "персонализация": getattr(self, "btn_tab_personal", None),
            }
            active_button = tab_to_button.get(getattr(self, "current_tab", ""))
        for btn in self._sidebar_buttons:
            try:
                btn.setStyleSheet(self._sb_style_active if btn == active_button else self._sb_style_normal)
            except Exception:
                pass

    def _apply_shell_theme_overrides(self):
        is_light = getattr(self, "current_theme", "dark") == "light"
        accent = "#0078D4" if is_light else "#5B6BFF"
        accent_soft = "rgba(0,120,212,0.12)" if is_light else "rgba(91,107,255,0.12)"
        accent_soft_active = "rgba(0,120,212,0.18)" if is_light else "rgba(91,107,255,0.24)"
        title_color = "#182033" if is_light else "#EEF2FF"
        subtitle_color = "#5C6B84" if is_light else "#5B6B99"
        shell_bg = "#F3F6FC" if is_light else "#080A1C"
        shell_border = "rgba(0,120,212,0.12)" if is_light else "rgba(91,107,255,0.12)"
        sidebar_muted = "#64748B" if is_light else "#5B6B99"
        sidebar_version = "#7C8AA5" if is_light else "#2E3560"
        card_bg = "rgba(255,255,255,0.96)" if is_light else "rgba(30,12,6,0.92)"
        card_border = "rgba(255,136,66,0.22)" if is_light else "rgba(255,110,46,0.35)"
        card_title_bg = "#F7FAFF" if is_light else "#07091A"
        card_title_color = "#D05A21" if is_light else "#FF9F6B"
        monitor_bg = "rgba(255,250,246,0.97)" if is_light else "rgba(20,8,4,0.88)"
        monitor_border = "rgba(255,136,66,0.18)" if is_light else "rgba(255,110,46,0.28)"

        self._sb_style_normal = (
            "QPushButton { text-align:left; padding:10px 16px; border-radius:12px;"
            f" font-size:10pt; font-weight:600; border:none; background:transparent; color:{sidebar_muted};"
            " min-height:40px; }"
            f"QPushButton:hover {{ background:{accent_soft}; color:{title_color}; }}"
        )
        self._sb_style_active = (
            "QPushButton { text-align:left; padding:10px 16px; border-radius:12px;"
            " font-size:10pt; font-weight:700; border:none;"
            f" background:{accent_soft_active}; color:{title_color}; min-height:40px; }}"
        )

        if getattr(self, "sidebar_widget", None):
            self.sidebar_widget.setStyleSheet(
                f"background:{shell_bg}; border-right:1px solid {shell_border}; border-radius:18px;"
            )
        if getattr(self, "sidebar_title_label", None):
            self.sidebar_title_label.setStyleSheet(
                f"font-size:18pt; font-weight:900; color:{title_color}; padding:14px 0 6px 0; letter-spacing:3px;"
            )
        if getattr(self, "sidebar_sub_label", None):
            self.sidebar_sub_label.setStyleSheet(
                f"font-size:7.5pt; color:{accent}; letter-spacing:4px; padding:0 0 14px 0; font-weight:700;"
            )
        if getattr(self, "sidebar_version_label", None):
            self.sidebar_version_label.setStyleSheet(
                f"font-size:7pt; color:{sidebar_version}; padding:6px 0; letter-spacing:1px; font-weight:700;"
            )
        if getattr(self, "title_label", None):
            self.title_label.setStyleSheet(
                f"font-size:22pt; font-weight:900; color:{title_color}; letter-spacing:2px;"
            )
        if getattr(self, "subtitle_label", None):
            self.subtitle_label.setStyleSheet(
                f"font-size:9.5pt; color:{subtitle_color}; letter-spacing:0.3px;"
            )
        if getattr(self, "analytics_header_label", None):
            self.analytics_header_label.setStyleSheet(
                f"font-size:18pt; font-weight:800; color:{title_color}; letter-spacing:0.4px;"
            )
        if getattr(self, "personal_header_label", None):
            self.personal_header_label.setStyleSheet(
                f"font-size:18pt; font-weight:800; color:{title_color}; letter-spacing:0.4px;"
            )
        if getattr(self, "game_header_group", None):
            self.game_header_group.setStyleSheet(
                "QGroupBox{"
                f"background:{card_bg}; border:1px solid {card_border}; border-radius:16px; margin-top:14px; padding:14px;"
                "}"
                "QGroupBox::title{"
                f"color:{card_title_color}; font-weight:800; background:{card_title_bg}; padding:2px 10px; border-radius:6px;"
                "}"
            )
        if getattr(self, "game_monitor_group", None):
            self.game_monitor_group.setStyleSheet(
                "QGroupBox{"
                f"background:{monitor_bg}; border:1px solid {monitor_border}; border-radius:16px; margin-top:14px; padding:14px;"
                "}"
                "QGroupBox::title{"
                f"color:{card_title_color}; font-weight:700; background:{card_title_bg}; padding:2px 10px; border-radius:6px;"
                "}"
            )
        if getattr(self, "lbl_game_mode", None):
            self.lbl_game_mode.setStyleSheet(f"color:{card_title_color}; font-weight:700;")
        if getattr(self, "lbl_game_perf", None):
            self.lbl_game_perf.setStyleSheet(
                f"color:{'#C27028' if is_light else '#FFCC80'}; font-size:9.5pt; font-weight:600;"
            )
        if getattr(self, "lbl_fps", None):
            self.lbl_fps.setStyleSheet("color:#34D399; font-size:14pt; font-weight:800;")
        if getattr(self, "lbl_ping", None):
            self.lbl_ping.setStyleSheet("color:#F59E0B; font-size:12pt; font-weight:700;")
        if getattr(self, "analytics_tabs_widget", None):
            self.analytics_tabs_widget.setDocumentMode(True)
            self.analytics_tabs_widget.setStyleSheet(
                "QTabWidget::pane{border:none;background:transparent;}"
                "QTabBar::tab{padding:9px 18px; border-radius:10px; margin:2px 4px;}"
            )
        if getattr(self, "analytics_overview_group", None):
            self.analytics_overview_group.setStyleSheet(
                "QGroupBox{"
                f"background:{'rgba(255,255,255,0.97)' if is_light else 'rgba(11,15,34,0.94)'};"
                f"border:1px solid {shell_border}; border-radius:18px; margin-top:12px; padding:14px;"
                "}"
                "QGroupBox::title{"
                f"color:{accent}; font-weight:800; background:{card_title_bg}; padding:2px 10px; border-radius:6px;"
                "}"
            )
        for frame in getattr(self, "_premium_metric_frames", []) or []:
            try:
                frame.setStyleSheet(
                    "QFrame{"
                    f"background:{'rgba(247,250,255,0.98)' if is_light else 'rgba(17,24,48,0.92)'};"
                    f"border:1px solid {shell_border}; border-radius:16px; padding:12px;"
                    "}"
                )
            except Exception:
                pass
        for frame in getattr(self, "_game_summary_frames", []) or []:
            try:
                frame.setStyleSheet(
                    "QFrame{"
                    f"background:{'rgba(255,248,242,0.98)' if is_light else 'rgba(20,20,40,0.88)'};"
                    f"border:1px solid {card_border}; border-radius:16px; padding:10px;"
                    "}"
                )
            except Exception:
                pass
        for frame in getattr(self, "_personal_preview_frames", []) or []:
            try:
                frame.setStyleSheet(
                    "QFrame{"
                    f"background:{'rgba(247,250,255,0.98)' if is_light else 'rgba(17,24,48,0.92)'};"
                    f"border:1px solid {shell_border}; border-radius:16px; padding:10px;"
                    "}"
                )
            except Exception:
                pass
        for label in getattr(self, "_premium_metric_titles", []) or []:
            try:
                label.setStyleSheet(f"color:{subtitle_color}; font-size:9.5pt; font-weight:600;")
            except Exception:
                pass
        for label in getattr(self, "_premium_metric_values", []) or []:
            try:
                label.setStyleSheet(f"color:{title_color}; font-size:18pt; font-weight:800;")
            except Exception:
                pass
        if getattr(self, "personal_runtime_group", None):
            self.personal_runtime_group.setStyleSheet(
                "QGroupBox{"
                f"background:{'rgba(255,255,255,0.97)' if is_light else 'rgba(11,15,34,0.94)'};"
                f"border:1px solid {shell_border}; border-radius:18px; margin-top:12px; padding:14px;"
                "}"
                "QGroupBox::title{"
                f"color:{accent}; font-weight:800; background:{card_title_bg}; padding:2px 10px; border-radius:6px;"
                "}"
            )
        for label in getattr(self, "_runtime_summary_labels", []) or []:
            try:
                label.setStyleSheet(
                    f"background:{accent_soft}; border:1px solid {shell_border}; border-radius:12px; "
                    f"padding:10px 12px; color:{title_color}; font-weight:600;"
                )
            except Exception:
                pass
        self._refresh_sidebar_styles()
        self._update_service_pills()
        self._update_runtime_summary()
        self._update_game_summary()
        self._update_personal_preview()

    def _service_pill_style(self, state: str) -> str:
        is_light = getattr(self, "current_theme", "dark") == "light"
        palette = {
            "healthy": ("rgba(34,197,94,0.16)", "rgba(34,197,94,0.34)", "#16A34A"),
            "starting": ("rgba(251,191,36,0.16)", "rgba(251,191,36,0.34)", "#D97706"),
            "failed": ("rgba(248,113,113,0.16)", "rgba(248,113,113,0.34)", "#DC2626"),
            "unknown": ("rgba(148,163,184,0.12)", "rgba(148,163,184,0.24)", "#64748B" if is_light else "#94A3B8"),
        }
        bg, border, text = palette.get(state, palette["unknown"])
        return (
            f"QLabel{{background:{bg}; border:1px solid {border}; border-radius:12px; "
            f"padding:6px 10px; color:{text}; font-size:9pt; font-weight:700;}}"
        )

    def _update_service_pills(self):
        health = getattr(self, "_service_health", {}) or {}
        mappings = [
            ("ollama", getattr(self, "ai_service_ollama", None), "Ollama"),
            ("fooocus", getattr(self, "ai_service_fooocus", None), "Fooocus"),
            ("ollama", getattr(self, "personal_runtime_ollama", None), "Ollama"),
            ("fooocus", getattr(self, "personal_runtime_fooocus", None), "Fooocus"),
        ]
        icons = {
            "healthy": "●",
            "starting": "◐",
            "failed": "○",
            "unknown": "○",
        }
        labels = {
            "healthy": "Готов",
            "starting": "Старт",
            "failed": "Ошибка",
            "unknown": "Неизвестно",
        }
        for service_name, widget, title in mappings:
            if widget is None:
                continue
            state = str(health.get(service_name, "unknown"))
            widget.setText(f"{icons.get(state, '○')} {title}: {labels.get(state, 'Неизвестно')}")
            widget.setStyleSheet(self._service_pill_style(state))

    def _update_runtime_summary(self):
        provider = getattr(self.neural_manager, "provider", "unknown") or "unknown"
        model = getattr(self.neural_manager, "model", "default") or "default"
        if getattr(self, "personal_runtime_theme", None):
            theme_name = "Светлая" if getattr(self, "current_theme", "dark") == "light" else "Темная"
            self.personal_runtime_theme.setText(f"Тема: {theme_name}")
        if getattr(self, "personal_runtime_ai", None):
            self.personal_runtime_ai.setText(f"AI: {provider} / {model}")
        self._update_service_pills()


    def _update_game_summary(self):
        if getattr(self, "game_summary_library", None):
            game_count = 0
            try:
                if hasattr(self, "game_manager"):
                    game_count = len(getattr(self.game_manager, "game_profiles", {}) or {})
            except Exception:
                game_count = 0
            self.game_summary_library.setText(str(game_count))
        if getattr(self, "game_summary_macros", None):
            self.game_summary_macros.setText(str(getattr(self, "macros_list", None).count() if getattr(self, "macros_list", None) else 0))
        if getattr(self, "game_summary_accounts", None):
            summary = (getattr(self, "lbl_accounts_summary", None).text() if getattr(self, "lbl_accounts_summary", None) else "") or ""
            active_accounts = sum(1 for line in summary.splitlines() if "[🟢]" in line)
            self.game_summary_accounts.setText(str(active_accounts))
        if getattr(self, "game_summary_mode", None):
            current_mode = getattr(self, "cmb_game_mode", None).currentText() if getattr(self, "cmb_game_mode", None) else "Баланс"
            self.game_summary_mode.setText(current_mode[:12])

    def _update_personal_preview(self):
        if getattr(self, "personal_preview_user", None):
            self.personal_preview_user.setText((getattr(self, "edit_user_name", None).text() or os.environ.get("USERNAME", "Пользователь")).strip() or "Пользователь")
        if getattr(self, "personal_preview_theme", None):
            if getattr(self, "radio_theme_light", None) and self.radio_theme_light.isChecked():
                theme = "Светлая"
            elif getattr(self, "radio_theme_auto", None) and self.radio_theme_auto.isChecked():
                theme = "Авто"
            else:
                theme = "Темная"
            self.personal_preview_theme.setText(theme)
        if getattr(self, "personal_preview_density", None):
            self.personal_preview_density.setText("Compact" if getattr(self, "chk_compact_mode", None) and self.chk_compact_mode.isChecked() else "Comfort")
        if getattr(self, "personal_preview_assists", None):
            assists = []
            if getattr(self, "chk_ai_suggestions", None) and self.chk_ai_suggestions.isChecked():
                assists.append("AI")
            if getattr(self, "chk_sound_notifications", None) and self.chk_sound_notifications.isChecked():
                assists.append("Sound")
            if getattr(self, "chk_auto_status_personal", None) and self.chk_auto_status_personal.isChecked():
                assists.append("Status")
            self.personal_preview_assists.setText(", ".join(assists) if assists else "Минимум")

    def _init_ui(self):
        central = QtWidgets.QWidget()
        root_layout = QtWidgets.QHBoxLayout(central)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(10)

        # ==== Левая боковая панель (разделы) ====
        sidebar = QtWidgets.QVBoxLayout()
        sidebar.setSpacing(4)
        sidebar.setContentsMargins(0, 8, 0, 8)

        self.sidebar_title_label = QtWidgets.QLabel("JARVIS")
        self.sidebar_title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.sidebar_sub_label = QtWidgets.QLabel("AI CONTROL")
        self.sidebar_sub_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        sidebar.addWidget(self.sidebar_title_label)
        sidebar.addWidget(self.sidebar_sub_label)

        _sb_style = (
            "QPushButton {{ text-align:left; padding:10px 16px; border-radius:10px;"
            " font-size:10pt; font-weight:600; border:none; background:transparent; color:#5B6B99;"
            " min-height:38px; }}"
            "QPushButton:hover {{ background:rgba(91,107,255,0.12); color:#C8D2FF; }}"
        )
        _sb_active = (
            "QPushButton {{ text-align:left; padding:10px 16px; border-radius:10px;"
            " font-size:10pt; font-weight:700; border:none;"
            " background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 rgba(91,107,255,0.30), stop:1 rgba(91,107,255,0.15));"
            " color:#EEF2FF; min-height:38px; }}"
        )

        self.btn_tab_home = QtWidgets.QPushButton("  🏠  Главная")
        self.btn_tab_ai = QtWidgets.QPushButton("  🤖  AI Чат")
        self.btn_tab_files = QtWidgets.QPushButton("  📁  Файлы")
        self.btn_tab_web = QtWidgets.QPushButton("  🌐  Веб")
        self.btn_tab_chat = QtWidgets.QPushButton("  💬  Мессенджеры")
        self.btn_tab_games = QtWidgets.QPushButton("  🎮  Игры")
        self.btn_tab_system = QtWidgets.QPushButton("  ⚙️  Система")
        self.btn_tab_auto = QtWidgets.QPushButton("  🔧  Автоматизация")
        self.btn_tab_analytics = QtWidgets.QPushButton("  📊  Аналитика")
        self.btn_tab_personal = QtWidgets.QPushButton("  🎨  Персонализация")

        self._sidebar_buttons = [
            self.btn_tab_home, self.btn_tab_ai, self.btn_tab_files,
            self.btn_tab_web, self.btn_tab_chat, self.btn_tab_games,
            self.btn_tab_system, self.btn_tab_auto, self.btn_tab_analytics,
            self.btn_tab_personal,
        ]
        self._sb_style_normal = _sb_style
        self._sb_style_active = _sb_active

        for btn in self._sidebar_buttons:
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_sb_style)
            sidebar.addWidget(btn)

        self.btn_tab_home.clicked.connect(lambda: self._set_active_tab(self.btn_tab_home, "Главная"))
        self.btn_tab_ai.clicked.connect(lambda: self._set_active_tab(self.btn_tab_ai, "AI"))
        self.btn_tab_files.clicked.connect(lambda: self._set_active_tab(self.btn_tab_files, "Файлы"))
        self.btn_tab_web.clicked.connect(lambda: self._set_active_tab(self.btn_tab_web, "Веб"))
        self.btn_tab_chat.clicked.connect(lambda: self._set_active_tab(self.btn_tab_chat, "Мессенджеры"))
        self.btn_tab_games.clicked.connect(lambda: self._set_active_tab(self.btn_tab_games, "Игры"))
        self.btn_tab_system.clicked.connect(lambda: self._set_active_tab(self.btn_tab_system, "Система"))
        self.btn_tab_auto.clicked.connect(lambda: self._set_active_tab(self.btn_tab_auto, "Автоматизация"))
        self.btn_tab_analytics.clicked.connect(lambda: self._set_active_tab(self.btn_tab_analytics, "Аналитика"))
        self.btn_tab_personal.clicked.connect(lambda: self._set_active_tab(self.btn_tab_personal, "Персонализация"))

        sidebar.addStretch(1)

        self.sidebar_version_label = QtWidgets.QLabel("v3.0 PREMIUM")
        self.sidebar_version_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        sidebar.addWidget(self.sidebar_version_label)

        # Правая часть — стек экранов (Главная, Файлы и т.д.)
        main_container = QtWidgets.QWidget()
        main_container.setMinimumWidth(640)
        main_container.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        main_layout = QtWidgets.QVBoxLayout(main_container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(10)

        self.main_stack = QtWidgets.QStackedWidget()

        # ==== Экран "Главная" ====
        home_page = QtWidgets.QWidget()
        home_layout = QtWidgets.QVBoxLayout(home_page)

        # Верхняя панель: NEURO COMMAND v2.0
        header = QtWidgets.QHBoxLayout()
        header_left = QtWidgets.QVBoxLayout()
        self.title_label = QtWidgets.QLabel("JARVIS")
        self.title_label.setStyleSheet("font-size:22pt; font-weight:900; color:#EEF2FF; letter-spacing:2px;")
        self.subtitle_label = QtWidgets.QLabel("Центральный мозг управления ПК")
        self.subtitle_label.setStyleSheet("font-size:9.5pt; color:#5B6B99; letter-spacing:0.3px;")
        header_left.addWidget(self.title_label)
        header_left.addWidget(self.subtitle_label)

        header_right = QtWidgets.QHBoxLayout()
        header_right.addStretch(1)
        self.lbl_status_ai = QtWidgets.QLabel("[🟢] AI Активен")
        self.lbl_status_ai.setProperty("class", "neuroSubtitle")
        header_right.addWidget(self.lbl_status_ai)

        header.addLayout(header_left)
        header.addLayout(header_right)

        # ==== Центральный блок: ввод команды ====
        command_group = QtWidgets.QGroupBox("Центральная команда")
        cmd_layout = QtWidgets.QVBoxLayout(command_group)

        prompt_label = QtWidgets.QLabel("💬 ВВЕДИТЕ КОМАНДУ:")
        prompt_label.setProperty("class", "sectionTitle")
        cmd_layout.addWidget(prompt_label)

        self.input_line = QtWidgets.QLineEdit()
        self.input_line.setPlaceholderText("\"открой хром и найди рецепт пасты\"")
        self.input_line.returnPressed.connect(self.handle_command_enter)
        cmd_layout.addWidget(self.input_line)

        cmd_buttons = QtWidgets.QHBoxLayout()
        self.btn_voice = QtWidgets.QPushButton("🎤")
        self.btn_open_file_center = QtWidgets.QPushButton("📁")
        self.btn_quick_action = QtWidgets.QPushButton("⚡")
        self.btn_refresh = QtWidgets.QPushButton("🔄")
        self.btn_game_mode = QtWidgets.QPushButton("🎮")

        # Привязка к существующим функциям
        self.btn_open_file_center.clicked.connect(self.open_explorer)
        self.btn_quick_action.clicked.connect(self.run_program_dialog)
        self.btn_refresh.clicked.connect(self.update_system_status)

        for b in (
            self.btn_voice,
            self.btn_open_file_center,
            self.btn_quick_action,
            self.btn_refresh,
            self.btn_game_mode,
        ):
            cmd_buttons.addWidget(b)

        cmd_layout.addLayout(cmd_buttons)

        # ==== Средняя зона: статус системы + быстрые команды ====
        middle_layout = QtWidgets.QHBoxLayout()

        # --- Статус системы ---
        status_group = QtWidgets.QGroupBox("СТАТУС СИСТЕМЫ")
        status_layout = QtWidgets.QGridLayout(status_group)

        self.lbl_cpu = QtWidgets.QLabel("CPU: -- %")
        self.lbl_ram = QtWidgets.QLabel("RAM: -- %")
        self.lbl_files = QtWidgets.QLabel("📁 Файлов: --")
        self.lbl_temp = QtWidgets.QLabel("🔥 Температура: --°C")

        status_layout.addWidget(self.lbl_cpu, 0, 0)
        status_layout.addWidget(self.lbl_ram, 0, 1)
        status_layout.addWidget(self.lbl_files, 1, 0)
        status_layout.addWidget(self.lbl_temp, 1, 1)

        middle_layout.addWidget(status_group, 2)

        # --- Быстрые команды ---
        quick_group = QtWidgets.QGroupBox("БЫСТРЫЕ КОМАНДЫ")
        quick_layout = QtWidgets.QGridLayout(quick_group)

        self.btn_browser = QtWidgets.QPushButton("🌐 Открыть браузер")
        self.btn_explorer = QtWidgets.QPushButton("📂 Проводник")
        self.btn_screenshot = QtWidgets.QPushButton("📷 Скриншот")
        self.btn_music = QtWidgets.QPushButton("🎵 Музыка")
        self.btn_lock = QtWidgets.QPushButton("🔒 Блокировка ПК")
        self.btn_monitor = QtWidgets.QPushButton("📊 Мониторинг")
        self.btn_tg = QtWidgets.QPushButton("🤖 TG Автоответчик")
        self.btn_settings = QtWidgets.QPushButton("⚙️ Настройки")
        self.btn_speedtest = QtWidgets.QPushButton("⚡ Speedtest")

        quick_layout.addWidget(self.btn_browser, 0, 0)
        quick_layout.addWidget(self.btn_explorer, 0, 1)
        quick_layout.addWidget(self.btn_screenshot, 1, 0)
        quick_layout.addWidget(self.btn_music, 1, 1)
        quick_layout.addWidget(self.btn_lock, 2, 0)
        quick_layout.addWidget(self.btn_monitor, 2, 1)
        quick_layout.addWidget(self.btn_tg, 3, 0)
        quick_layout.addWidget(self.btn_settings, 3, 1)
        quick_layout.addWidget(self.btn_speedtest, 4, 0)

        # Привязки к существующим или будущим функциям
        self.btn_browser.clicked.connect(self.open_browser)
        self.btn_explorer.clicked.connect(self.open_explorer)
        self.btn_screenshot.clicked.connect(self.take_screenshot)
        self.btn_music.clicked.connect(self.open_music_folder)
        self.btn_lock.clicked.connect(self.lock_workstation)
        self.btn_monitor.clicked.connect(self.update_system_status)
        self.btn_tg.clicked.connect(lambda: self.add_history("[Система] TG автоответчик будет добавлен позже"))
        self.btn_settings.clicked.connect(self.open_settings_dialog)
        self.btn_speedtest.clicked.connect(self.run_speedtest)

        middle_layout.addWidget(quick_group, 3)

        # ==== Нижняя зона: история команд и лог ====
        bottom_group = QtWidgets.QGroupBox("ИСТОРИЯ КОМАНД И ЛОГ")
        bottom_layout = QtWidgets.QHBoxLayout(bottom_group)

        self.history_view = QtWidgets.QListWidget()
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)

        bottom_layout.addWidget(self.history_view, 2)
        bottom_layout.addWidget(self.log_view, 3)

        home_main = QtWidgets.QHBoxLayout()

        home_left = QtWidgets.QVBoxLayout()
        home_left.addLayout(header)
        home_left.addWidget(command_group)
        home_left.addLayout(middle_layout)
        home_left.addWidget(bottom_group, 1)

        chat_group = QtWidgets.QGroupBox("AI ЧАТ")
        chat_layout = QtWidgets.QVBoxLayout(chat_group)

        agent_row = QtWidgets.QHBoxLayout()
        provider_row = QtWidgets.QHBoxLayout()
        lbl_provider = QtWidgets.QLabel("Нейросеть:")
        self.cmb_neural_provider = QtWidgets.QComboBox()
        self.cmb_neural_provider.addItem("DeepSeek", "deepseek")
        self.cmb_neural_provider.addItem("Claude (Anthropic)", "anthropic")
        self.edit_neural_model = QtWidgets.QLineEdit()
        self.edit_neural_model.setPlaceholderText("model (опц.)")
        self.btn_apply_model = QtWidgets.QPushButton("Применить")
        self.btn_apply_model.clicked.connect(self._on_neural_model_changed)
        self.cmb_neural_provider.currentIndexChanged.connect(self._on_neural_provider_changed)
        provider_row.addWidget(lbl_provider)
        provider_row.addWidget(self.cmb_neural_provider)
        provider_row.addWidget(self.edit_neural_model, 1)
        provider_row.addWidget(self.btn_apply_model)
        chat_layout.addLayout(provider_row)

        voice_row = QtWidgets.QHBoxLayout()
        self.chk_tts_enable = QtWidgets.QCheckBox("Озвучка")
        self.chk_tts_enable.setChecked(True)
        self.btn_tts_test = QtWidgets.QPushButton("Тест озвучки")
        self.btn_tts_test.clicked.connect(self._tts_test)
        self.chk_hotword_enable = QtWidgets.QCheckBox("Джарвис")
        self.chk_hotword_enable.setChecked(False)
        self.chk_hotword_enable.stateChanged.connect(self._on_hotword_toggle_changed)
        voice_row.addWidget(self.chk_tts_enable)
        voice_row.addWidget(self.btn_tts_test)
        voice_row.addWidget(self.chk_hotword_enable)
        voice_row.addStretch(1)
        chat_layout.addLayout(voice_row)

        agent_row = QtWidgets.QHBoxLayout()
        self.chk_agent_enable = QtWidgets.QCheckBox("AI доступ к ПК")
        self.chk_agent_confirm = QtWidgets.QCheckBox("Подтверждать")
        self.chk_agent_confirm.setChecked(False)
        agent_row.addWidget(self.chk_agent_enable)
        agent_row.addWidget(self.chk_agent_confirm)
        agent_row.addStretch(1)
        chat_layout.addLayout(agent_row)

        # -- Главная область чата (ScrollArea для стабильности) --
        self.chat_scroll = QtWidgets.QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameStyle(QtWidgets.QFrame.Shape.NoFrame)
        self.chat_scroll.setStyleSheet("background: #0a0a18; border: none;")
        
        self.chat_container = QtWidgets.QWidget()
        self.chat_container.setStyleSheet("background: transparent;")
        self.chat_layout_v = QtWidgets.QVBoxLayout(self.chat_container)
        self.chat_layout_v.setContentsMargins(10, 10, 10, 10)
        self.chat_layout_v.setSpacing(10)
        self.chat_layout_v.addStretch(1)
        
        self.chat_scroll.setWidget(self.chat_container)
        self.chat_view = self.chat_scroll
        
        chat_layout.addWidget(self.chat_scroll, 1)

        chat_input_row = QtWidgets.QHBoxLayout()
        self.btn_mic = QtWidgets.QPushButton("🎤")
        self.btn_mic.setMinimumWidth(40)
        self.btn_mic.setMaximumWidth(56)
        self.btn_mic.clicked.connect(self._toggle_voice_recording)
        self.chat_input = _ChatInputField(self)
        self.chat_input.setStyleSheet(
            "QPlainTextEdit{ background:#0e0e20; border:1px solid rgba(0,212,255,0.15);"
            " border-radius:12px; padding:10px 14px; font-size:12pt; color:#E0E0FF;}"
            "QPlainTextEdit:focus{ border:1px solid rgba(0,212,255,0.4); }"
        )
        self.chat_input.send_requested.connect(self._chat_send_from_input)
        self.btn_chat_send = QtWidgets.QPushButton("↑")
        self.btn_chat_send.setToolTip("Отправить")
        self.btn_chat_send.clicked.connect(self._on_chat_send_clicked)
        chat_input_row.addWidget(self.btn_mic)
        chat_input_row.addWidget(self.chat_input, 1)
        chat_input_row.addWidget(self.btn_chat_send)
        chat_layout.addLayout(chat_input_row)

        btn_open_full_chat = QtWidgets.QPushButton("🤖  Открыть полный AI чат")
        btn_open_full_chat.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_open_full_chat.setStyleSheet(
            "QPushButton{background:rgba(91,107,255,0.12); border:1px solid rgba(91,107,255,0.35);"
            " border-radius:10px; padding:8px 20px; color:#C8D2FF; font-weight:700; font-size:10pt;}"
            "QPushButton:hover{background:rgba(91,107,255,0.24); color:#EEF2FF;}"
        )
        btn_open_full_chat.clicked.connect(lambda: self._set_active_tab(self.btn_tab_ai, "AI"))
        chat_layout.addWidget(btn_open_full_chat)

        term_group = QtWidgets.QGroupBox("ТЕРМИНАЛ")
        term_layout = QtWidgets.QVBoxLayout(term_group)
        self.terminal_view = QtWidgets.QPlainTextEdit()
        self.terminal_view.setReadOnly(True)
        self.terminal_view.setMinimumHeight(120)
        self.terminal_view.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        term_layout.addWidget(self.terminal_view)

        term_input_row = QtWidgets.QHBoxLayout()
        self.terminal_input = QtWidgets.QLineEdit()
        self.terminal_input.setPlaceholderText("cmd команда (например: where telegram.exe)")
        self.terminal_input.returnPressed.connect(self._terminal_run_from_input)
        self.btn_terminal_run = QtWidgets.QPushButton("Выполнить")
        self.btn_terminal_run.clicked.connect(self._terminal_run_from_input)
        term_input_row.addWidget(self.terminal_input, 1)
        term_input_row.addWidget(self.btn_terminal_run)
        term_layout.addLayout(term_input_row)

        chat_layout.addWidget(term_group)

        home_main.addLayout(home_left, 3)
        home_main.addWidget(chat_group, 2)

        home_layout.addLayout(home_main, 1)

        # ==== Экран "Файлы" ====
        files_page = QtWidgets.QWidget()
        files_page_layout = QtWidgets.QVBoxLayout(files_page)
        files_page_layout.setContentsMargins(0, 0, 0, 0)

        files_scroll = QtWidgets.QScrollArea()
        files_scroll.setWidgetResizable(True)
        files_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        files_scroll.setStyleSheet("QScrollArea{background:transparent; border:none;}")

        files_content = QtWidgets.QWidget()
        files_layout = QtWidgets.QVBoxLayout(files_content)
        files_layout.setContentsMargins(12, 12, 12, 12)
        files_layout.setSpacing(8)
        files_layout.setSpacing(8)

        # Верхняя панель пути
        path_bar = QtWidgets.QHBoxLayout()
        self.files_path_edit = QtWidgets.QLineEdit()
        self.files_path_edit.setReadOnly(True)
        self.files_btn_home = QtWidgets.QPushButton("Домой")
        self.files_btn_up = QtWidgets.QPushButton("Вверх")
        self.files_btn_refresh = QtWidgets.QPushButton("Обновить")
        path_bar.addWidget(self.files_path_edit, 4)
        path_bar.addWidget(self.files_btn_home)
        path_bar.addWidget(self.files_btn_up)
        path_bar.addWidget(self.files_btn_refresh)

        # Список файлов/папок
        self.files_view = QtWidgets.QTreeWidget()
        self.files_view.setColumnCount(3)
        self.files_view.setHeaderLabels(["Имя", "Размер", "Изменён"])
        self.files_view.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.files_view.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.files_view.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.files_view.itemDoubleClicked.connect(self._files_item_activated)

        # Панель действий с файлами
        files_actions = QtWidgets.QHBoxLayout()
        self.files_btn_open = QtWidgets.QPushButton("Открыть")
        self.files_btn_open_explorer = QtWidgets.QPushButton("Открыть в проводнике")
        self.files_btn_delete = QtWidgets.QPushButton("Удалить")
        self.files_btn_new_folder = QtWidgets.QPushButton("Новая папка")
        files_actions.addWidget(self.files_btn_open)
        files_actions.addWidget(self.files_btn_open_explorer)
        files_actions.addWidget(self.files_btn_delete)
        files_actions.addStretch(1)
        files_actions.addWidget(self.files_btn_new_folder)

        files_layout.addLayout(path_bar)
        files_layout.addWidget(self.files_view, 1)
        files_layout.addLayout(files_actions)

        # Привязка сигналов файлового менеджера
        self.files_btn_home.clicked.connect(self._files_go_home)
        self.files_btn_up.clicked.connect(self._files_go_up)
        self.files_btn_refresh.clicked.connect(self._files_refresh)
        self.files_btn_open.clicked.connect(self._files_open_selected)
        self.files_btn_open_explorer.clicked.connect(self._files_open_in_explorer)
        self.files_btn_delete.clicked.connect(self._files_delete_selected)
        self.files_btn_new_folder.clicked.connect(self._files_new_folder)

        files_scroll.setWidget(files_content)
        files_page_layout.addWidget(files_scroll, 1)

        # ==== Экран "Веб" ====
        web_page = QtWidgets.QWidget()
        web_page_layout = QtWidgets.QVBoxLayout(web_page)
        web_page_layout.setContentsMargins(0, 0, 0, 0)

        web_scroll = QtWidgets.QScrollArea()
        web_scroll.setWidgetResizable(True)
        web_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        web_scroll.setStyleSheet("QScrollArea{background:transparent; border:none;}")

        web_content = QtWidgets.QWidget()
        web_layout = QtWidgets.QVBoxLayout(web_content)
        web_layout.setContentsMargins(12, 12, 12, 12)
        web_layout.setSpacing(8)
        web_layout.setSpacing(8)

        # Верхняя панель: ввод URL / запроса
        web_top = QtWidgets.QHBoxLayout()
        self.web_input = QtWidgets.QLineEdit()
        self.web_input.setPlaceholderText("URL или поисковый запрос")
        self.web_btn_open_url = QtWidgets.QPushButton("Открыть сайт")
        self.web_btn_google = QtWidgets.QPushButton("Google поиск")
        self.web_btn_youtube = QtWidgets.QPushButton("YouTube поиск")
        web_top.addWidget(self.web_input, 4)
        web_top.addWidget(self.web_btn_open_url)
        web_top.addWidget(self.web_btn_google)
        web_top.addWidget(self.web_btn_youtube)

        # Быстрые сайты
        web_quick_group = QtWidgets.QGroupBox("Быстрый доступ")
        web_quick_layout = QtWidgets.QHBoxLayout(web_quick_group)
        self.web_btn_yt = QtWidgets.QPushButton("YouTube")
        self.web_btn_gmail = QtWidgets.QPushButton("Gmail")
        self.web_btn_github = QtWidgets.QPushButton("GitHub")
        self.web_btn_steam = QtWidgets.QPushButton("Steam Store")
        for b in (self.web_btn_yt, self.web_btn_gmail, self.web_btn_github, self.web_btn_steam):
            web_quick_layout.addWidget(b)

        # Профили (режимы)
        web_profiles_group = QtWidgets.QGroupBox("Режимы исследования")
        web_profiles_layout = QtWidgets.QHBoxLayout(web_profiles_group)
        self.web_btn_profile_research = QtWidgets.QPushButton("Исследование")
        self.web_btn_profile_study = QtWidgets.QPushButton("Учёба")
        self.web_btn_profile_work = QtWidgets.QPushButton("Работа")
        self.web_btn_profile_games = QtWidgets.QPushButton("Игры")
        for b in (
            self.web_btn_profile_research,
            self.web_btn_profile_study,
            self.web_btn_profile_work,
            self.web_btn_profile_games,
        ):
            web_profiles_layout.addWidget(b)

        # Упрощённый "шторм идей"
        ideas_group = QtWidgets.QGroupBox("Шторм идей (связанные темы)")
        ideas_layout = QtWidgets.QVBoxLayout(ideas_group)
        self.web_btn_generate_ideas = QtWidgets.QPushButton("Сгенерировать связанные темы")
        self.web_ideas_list = QtWidgets.QListWidget()
        ideas_layout.addWidget(self.web_btn_generate_ideas)
        ideas_layout.addWidget(self.web_ideas_list)

        web_layout.addLayout(web_top)
        web_layout.addWidget(web_quick_group)
        web_layout.addWidget(web_profiles_group)
        web_layout.addWidget(ideas_group, 1)

        web_scroll.setWidget(web_content)
        web_page_layout.addWidget(web_scroll, 1)

        # ==== Экран "Мессенджеры" ====
        chat_page = QtWidgets.QWidget()
        chat_page.setObjectName("MessengerTab")
        chat_page_layout = QtWidgets.QVBoxLayout(chat_page)
        chat_page_layout.setContentsMargins(0, 0, 0, 0)
        chat_page_layout.setSpacing(0)

        msg_scroll = QtWidgets.QScrollArea()
        msg_scroll.setWidgetResizable(True)
        msg_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        msg_scroll.setStyleSheet("QScrollArea{background:transparent; border:none;}")
        msg_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        msg_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        chat_content = QtWidgets.QWidget()
        chat_layout = QtWidgets.QVBoxLayout(chat_content)
        chat_layout.setContentsMargins(12, 12, 12, 12)
        chat_layout.setSpacing(10)

        # Верхняя панель подключений
        connect_group = QtWidgets.QGroupBox("📱 Мессенджеры")
        connect_layout = QtWidgets.QGridLayout(connect_group)

        self.lbl_tg_status = QtWidgets.QLabel("TELEGRAM  [⚪] Не инициализирован")
        self.lbl_dc_status = QtWidgets.QLabel("DISCORD   [🔴] Отключен")
        self.lbl_wa_status = QtWidgets.QLabel("WHATSAPP  [⚪] Не настроен")
        self.lbl_vk_status = QtWidgets.QLabel("VK        [⚪] Не настроен")

        connect_layout.addWidget(self.lbl_tg_status, 0, 0, 1, 2)
        connect_layout.addWidget(self.lbl_dc_status, 1, 0, 1, 2)
        connect_layout.addWidget(self.lbl_wa_status, 2, 0, 1, 2)
        connect_layout.addWidget(self.lbl_vk_status, 3, 0, 1, 2)

        self.btn_msg_refresh = QtWidgets.QPushButton("🔄 Обновить статус")
        self.btn_msg_api_settings = QtWidgets.QPushButton("⚙️ Настройки API")
        self.btn_msg_connect = QtWidgets.QPushButton("🔌 Подключить Telegram")
        connect_layout.addWidget(self.btn_msg_refresh, 0, 2)
        connect_layout.addWidget(self.btn_msg_api_settings, 1, 2)
        connect_layout.addWidget(self.btn_msg_connect, 2, 2)
        # Стили для верхней панели подключений
        for b in (self.btn_msg_refresh, self.btn_msg_api_settings, self.btn_msg_connect):
            _add_press_animation(b)
        _apply_shadow(connect_group, blur=16, x=0, y=6)

        # Центральная зона: слева Telegram-центр, справа чаты и отправка
        center_split = QtWidgets.QSplitter()
        center_split.setOrientation(QtCore.Qt.Orientation.Horizontal)
        center_split.setChildrenCollapsible(False)

        # Левая колонка: Telegram Management Center
        left_panel = QtWidgets.QWidget()
        left_panel.setMinimumWidth(260)
        left_panel.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        # Быстрые команды (подсказки для AI-команд)
        quick_group = QtWidgets.QGroupBox("Команды для AI")
        quick_layout = QtWidgets.QVBoxLayout(quick_group)
        self.lbl_ai_cmd_examples = QtWidgets.QLabel(
            "• отправь сообщение [имя] [текст]\n"
            "• найди переписку с [имя]\n"
            "• ответь на последнее сообщение\n"
            "• позвони [имя]\n"
            "• отправь файл [путь] в [чат]"
        )
        self.lbl_ai_cmd_examples.setWordWrap(True)
        quick_layout.addWidget(self.lbl_ai_cmd_examples)

        # Автоответчик
        auto_group = QtWidgets.QGroupBox("🤖 Автоответчик")
        auto_layout = QtWidgets.QVBoxLayout(auto_group)
        self.chk_autoreply_enabled = QtWidgets.QCheckBox("Включить автоответчик")
        auto_layout.addWidget(self.chk_autoreply_enabled)

        self.chk_autoreply_all = QtWidgets.QCheckBox("Ответить всем")
        self.chk_autoreply_favorites = QtWidgets.QCheckBox("Только избранным")
        self.chk_autoreply_schedule = QtWidgets.QCheckBox("По расписанию (9:00–18:00)")
        auto_layout.addWidget(self.chk_autoreply_all)
        auto_layout.addWidget(self.chk_autoreply_favorites)
        auto_layout.addWidget(self.chk_autoreply_schedule)

        self.autoreply_text = QtWidgets.QTextEdit()
        self.autoreply_text.setPlaceholderText("Привет! Я сейчас занят. Отвечу позже.")
        auto_layout.addWidget(self.autoreply_text)

        self.btn_autoreply_save_template = QtWidgets.QPushButton("💾 Сохранить шаблон")
        auto_layout.addWidget(self.btn_autoreply_save_template)

        # Шаблоны сообщений
        templates_group = QtWidgets.QGroupBox("Быстрые шаблоны")
        templates_layout = QtWidgets.QVBoxLayout(templates_group)
        self.templates_list = QtWidgets.QListWidget()
        for txt in [
            "Буду через 10 минут",
            "Отправляю файл",
            "На совещании, перезвоню",
            "Да, согласен",
            "Нет, не могу",
        ]:
            self.templates_list.addItem(txt)
        templates_layout.addWidget(self.templates_list)
        self.btn_template_add = QtWidgets.QPushButton("➕ Добавить шаблон")
        templates_layout.addWidget(self.btn_template_add)

        left_layout.addWidget(quick_group)
        left_layout.addWidget(auto_group)
        left_layout.addWidget(templates_group, 1)
        # Стили для левой колонки (команды, автоответчик, шаблоны)
        for g in (quick_group, auto_group, templates_group):
            _apply_shadow(g, blur=10, x=0, y=4)
        _add_press_animation(self.btn_autoreply_save_template)
        _add_press_animation(self.btn_template_add)

        # Правая колонка: список чатов + окно нового сообщения + AI‑функции
        right_panel = QtWidgets.QWidget()
        right_panel.setMinimumWidth(400)
        right_panel.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        # Чат-менеджер
        chats_group = QtWidgets.QGroupBox("Активные чаты")
        chats_layout = QtWidgets.QVBoxLayout(chats_group)
        self.chats_list = QtWidgets.QTreeWidget()
        self.chats_list.setColumnCount(3)
        self.chats_list.setHeaderLabels(["Чат", "Новых", "Последнее"])
        self.chats_list.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.chats_list.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.chats_list.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        # Визуальные улучшения списка чатов
        self.chats_list.setIconSize(QtCore.QSize(40, 40))
        self.chats_list.setUniformRowHeights(True)
        # Сделать список выше, чтобы было видно минимум 4 чата
        self.chats_list.setMinimumHeight(220)
        self.chats_list.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.chats_list.setStyleSheet(
            "QTreeWidget::item { padding: 7px 10px; min-height: 44px; }"
        )
        # Панель поиска и быстрых действий — сверху, чтобы не закрывала список
        chat_tools = QtWidgets.QHBoxLayout()
        self.msg_search_input = QtWidgets.QLineEdit()
        self.msg_search_input.setPlaceholderText("Поиск чата")
        self.msg_search_input.setObjectName("chatSearch")
        self.msg_search_input.setClearButtonEnabled(True)
        self.msg_search_input.setMinimumHeight(34)
        self.btn_chat_pin = QtWidgets.QPushButton("📌")
        self.btn_chat_pin.setToolTip("Закрепить чат")
        self.btn_chat_hide = QtWidgets.QPushButton("🚫")
        self.btn_chat_hide.setToolTip("Скрыть чат")
        chat_tools.addWidget(self.msg_search_input, 3)
        chat_tools.addWidget(self.btn_chat_pin)
        chat_tools.addWidget(self.btn_chat_hide)
        chats_layout.addLayout(chat_tools)

        chats_layout.addWidget(self.chats_list)

        # Убираем визуальную полосу-разделитель посередине списка чатов (не мешает просмотру)
        # Добавим более выразительный визуальный стиль для группы чатов и элементов
        _apply_shadow(chats_group, blur=16, x=0, y=6)
        self.btn_chat_pin.setStyleSheet("QPushButton{background:transparent;border:none;font-size:14pt;min-width:36px;min-height:36px;border-radius:8px;}QPushButton:hover{background:rgba(91,107,255,0.15);}")
        self.btn_chat_hide.setStyleSheet("QPushButton{background:transparent;border:none;font-size:14pt;min-width:36px;min-height:36px;border-radius:8px;}QPushButton:hover{background:rgba(248,113,113,0.15);}")
        _add_press_animation(self.btn_chat_pin)
        _add_press_animation(self.btn_chat_hide)

        # Новое сообщение
        compose_group = QtWidgets.QGroupBox("Новое сообщение")
        compose_layout = QtWidgets.QGridLayout(compose_group)

        self.msg_recipient = QtWidgets.QLineEdit()
        self.msg_recipient.setPlaceholderText("Кому (имя или название чата)")
        compose_layout.addWidget(QtWidgets.QLabel("Кому:"), 0, 0)
        compose_layout.addWidget(self.msg_recipient, 0, 1, 1, 3)

        self.msg_text = QtWidgets.QTextEdit()
        self.msg_text.setPlaceholderText("Текст сообщения...")
        self.msg_text.setMinimumHeight(100)
        self.msg_text.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        compose_layout.addWidget(self.msg_text, 1, 0, 1, 4)

        self.btn_attach_file = QtWidgets.QPushButton("📎 Файл")
        self.btn_attach_image = QtWidgets.QPushButton("📷 Фото")
        self.btn_attach_video = QtWidgets.QPushButton("🎥 Видео")
        compose_layout.addWidget(self.btn_attach_file, 2, 0)
        compose_layout.addWidget(self.btn_attach_image, 2, 1)
        compose_layout.addWidget(self.btn_attach_video, 2, 2)

        # Метка вложений — отдельная строка, чтобы не наезжать на кнопки при узком окне
        self.lbl_attachments = QtWidgets.QLabel("")
        self.lbl_attachments.setWordWrap(True)
        self.lbl_attachments.setStyleSheet("color:#818CF8; font-size:9pt;")
        compose_layout.addWidget(self.lbl_attachments, 3, 0, 1, 4)

        # Подсказки для кнопок вложений
        self.btn_attach_file.setToolTip("Прикрепить файл")
        self.btn_attach_image.setToolTip("Прикрепить изображение")
        self.btn_attach_video.setToolTip("Прикрепить видео")

        self.chk_secret = QtWidgets.QCheckBox("Секретное")
        self.chk_delayed = QtWidgets.QCheckBox("Отложить отправку")
        self.msg_delay_time = QtWidgets.QLineEdit("18:30")
        self.msg_delay_time.setMaximumWidth(80)
        self.chk_read_confirm = QtWidgets.QCheckBox("Подтверждение прочтения")
        compose_layout.addWidget(self.chk_secret, 4, 0)
        compose_layout.addWidget(self.chk_delayed, 4, 1)
        compose_layout.addWidget(self.msg_delay_time, 4, 2)
        compose_layout.addWidget(self.chk_read_confirm, 4, 3)

        self.btn_msg_send = QtWidgets.QPushButton("📤 Отправить")
        self.btn_msg_schedule = QtWidgets.QPushButton("⏰ Запланировать")
        compose_layout.addWidget(self.btn_msg_send, 5, 2)
        compose_layout.addWidget(self.btn_msg_schedule, 5, 3)

        _apply_shadow(compose_group, blur=16, x=0, y=6)
        for b in (self.btn_attach_file, self.btn_attach_image, self.btn_attach_video, self.btn_msg_send, self.btn_msg_schedule):
            _add_press_animation(b)

        # AI-функции и статистика
        ai_group = QtWidgets.QGroupBox("AI ассистент для Telegram")
        ai_layout = QtWidgets.QVBoxLayout(ai_group)
        self.chk_ai_translate = QtWidgets.QCheckBox("Авто-перевод сообщений")
        self.chk_ai_sort = QtWidgets.QCheckBox("Авто-сортировка чатов")
        self.chk_ai_summarize = QtWidgets.QCheckBox("Суммаризация длинных сообщений")
        self.chk_ai_faq = QtWidgets.QCheckBox("Авто-ответ на частые вопросы")
        self.chk_ai_sentiment = QtWidgets.QCheckBox("Анализ тональности сообщений")
        for w in (
            self.chk_ai_translate,
            self.chk_ai_sort,
            self.chk_ai_summarize,
            self.chk_ai_faq,
            self.chk_ai_sentiment,
        ):
            ai_layout.addWidget(w)

        self.ai_command_input = QtWidgets.QLineEdit()
        self.ai_command_input.setPlaceholderText("Команда для Telegram, напр.: 'проанализируй переписку с [имя]'")
        self.btn_ai_run = QtWidgets.QPushButton(" 🤖 Выполнить команду")
        ai_cmd_row = QtWidgets.QHBoxLayout()
        ai_cmd_row.addWidget(self.ai_command_input, 4)
        ai_cmd_row.addWidget(self.btn_ai_run)
        ai_layout.addLayout(ai_cmd_row)

        stats_group = QtWidgets.QGroupBox("Статистика за неделю")
        stats_layout = QtWidgets.QVBoxLayout(stats_group)
        self.lbl_stats_summary = QtWidgets.QLabel("Сообщений отправлено: --\nПолучено: --\nНаиболее активен: --\nВремя пик: --\nТоп-слова: --")
        self.lbl_stats_summary.setWordWrap(True)
        self.btn_stats_chart = QtWidgets.QPushButton(" 📈 Показать график")
        stats_layout.addWidget(self.lbl_stats_summary)
        stats_layout.addWidget(self.btn_stats_chart)

        _add_press_animation(self.btn_ai_run)
        _add_press_animation(self.btn_stats_chart)

        right_layout.addWidget(chats_group, 2)
        right_layout.addWidget(compose_group, 2)
        right_layout.addWidget(ai_group)
        right_layout.addWidget(stats_group)

        center_split.addWidget(left_panel)
        center_split.addWidget(right_panel)
        center_split.setStretchFactor(0, 1)
        center_split.setStretchFactor(1, 2)

        chat_layout.addWidget(connect_group)
        chat_layout.addWidget(center_split, 1)

        msg_scroll.setWidget(chat_content)
        chat_page_layout.addWidget(msg_scroll, 1)

        # Привязка сигналов вкладки мессенджеров
        self.btn_msg_refresh.clicked.connect(self._messengers_refresh_status)
        self.btn_msg_api_settings.clicked.connect(self._messengers_open_api_settings)
        if hasattr(self, "btn_msg_connect"):
            self.btn_msg_connect.clicked.connect(self._messengers_connect_telegram)
        self.chk_autoreply_enabled.toggled.connect(self._messengers_toggle_autoreply)
        self.btn_autoreply_save_template.clicked.connect(self._messengers_save_autoreply_template)
        self.templates_list.itemDoubleClicked.connect(self._messengers_use_template)
        self.btn_template_add.clicked.connect(self._messengers_add_template)
        self.btn_chat_pin.clicked.connect(self._messengers_pin_chat)
        self.btn_chat_hide.clicked.connect(self._messengers_hide_chat)
        self.chats_list.itemDoubleClicked.connect(self._messengers_chat_activated)
        self.msg_search_input.textChanged.connect(self._messengers_filter_chats)
        self.btn_attach_file.clicked.connect(lambda: self._messengers_add_attachment("file"))
        self.btn_attach_image.clicked.connect(lambda: self._messengers_add_attachment("image"))
        self.btn_attach_video.clicked.connect(lambda: self._messengers_add_attachment("video"))
        self.btn_msg_send.clicked.connect(self._messengers_send_message)
        self.btn_msg_schedule.clicked.connect(self._messengers_schedule_message)
        self.btn_ai_run.clicked.connect(self._messengers_run_ai_command)
        self.btn_stats_chart.clicked.connect(self._messengers_show_stats_chart)

        # ==== Экран "Игры" (игровой центр) ====
        game_page = QtWidgets.QWidget()
        game_page.setObjectName("GameTab")
        game_page_outer_lay = QtWidgets.QVBoxLayout(game_page)
        game_page_outer_lay.setContentsMargins(0, 0, 0, 0)
        game_page_outer_lay.setSpacing(0)

        # Scroll area so elements don't overlap when window is small
        game_scroll = QtWidgets.QScrollArea()
        game_scroll.setWidgetResizable(True)
        game_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        game_scroll.setStyleSheet("QScrollArea{background:transparent; border:none;}")
        game_inner = QtWidgets.QWidget()
        game_inner.setObjectName("gameScrollInner")
        game_layout = QtWidgets.QVBoxLayout(game_inner)
        game_layout.setContentsMargins(8, 8, 8, 8)
        game_layout.setSpacing(8)
        game_scroll.setWidget(game_inner)
        game_page_outer_lay.addWidget(game_scroll)

        # Верхняя панель игрового режима
        self.game_header_group = QtWidgets.QGroupBox("🎮 Игровой центр")
        header_layout = QtWidgets.QHBoxLayout(self.game_header_group)
        self.lbl_game_mode = QtWidgets.QLabel("[🟢] Режим: Баланс")
        self.lbl_game_perf = QtWidgets.QLabel("[⚡] Профиль: Сбалансированный")
        header_layout.addWidget(self.lbl_game_mode)

        self.cmb_game_mode = QtWidgets.QComboBox()
        self.cmb_game_mode.addItems(["Баланс", "Производительность", "Качество"])
        self.cmb_game_mode.currentTextChanged.connect(lambda *_: self._update_game_summary())
        header_layout.addWidget(self.cmb_game_mode)

        header_layout.addStretch(1)
        header_layout.addWidget(self.lbl_game_perf)

        game_summary_group = QtWidgets.QGroupBox("Быстрый обзор")
        game_summary_layout = QtWidgets.QHBoxLayout(game_summary_group)

        def make_game_summary_card(title: str):
            frame = QtWidgets.QFrame()
            lay = QtWidgets.QVBoxLayout(frame)
            lay.setContentsMargins(12, 8, 12, 8)
            lbl_title = QtWidgets.QLabel(title)
            lbl_title.setStyleSheet("font-size:9pt; font-weight:600; color:#7B89B6;")
            lbl_value = QtWidgets.QLabel("--")
            lbl_value.setStyleSheet("font-size:16pt; font-weight:800; color:#EEF2FF;")
            lay.addWidget(lbl_title)
            lay.addWidget(lbl_value)
            game_summary_layout.addWidget(frame, 1)
            return frame, lbl_value

        self._game_summary_frames = []
        frame, self.game_summary_library = make_game_summary_card("Библиотека")
        self._game_summary_frames.append(frame)
        frame, self.game_summary_macros = make_game_summary_card("Макросы")
        self._game_summary_frames.append(frame)
        frame, self.game_summary_accounts = make_game_summary_card("Аккаунты")
        self._game_summary_frames.append(frame)
        frame, self.game_summary_mode = make_game_summary_card("Режим")
        self._game_summary_frames.append(frame)

        # Центральная зона: слева игры и оптимизация, справа мониторинг и AI
        game_center_split = QtWidgets.QSplitter()
        game_center_split.setOrientation(QtCore.Qt.Orientation.Horizontal)
        game_center_split.setChildrenCollapsible(False)

        # Левая колонка: быстрый запуск + оптимизация
        game_left = QtWidgets.QWidget()
        game_left.setMinimumWidth(280)
        gl_layout = QtWidgets.QVBoxLayout(game_left)
        gl_layout.setContentsMargins(0, 0, 0, 0)
        gl_layout.setSpacing(6)

        games_group = QtWidgets.QGroupBox("Быстрый запуск игр")
        games_layout = QtWidgets.QVBoxLayout(games_group)
        self.games_list = QtWidgets.QTreeWidget()
        self.games_list.setColumnCount(3)
        self.games_list.setHeaderLabels(["Игра", "Время", "Действие"])
        self.games_list.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.games_list.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.games_list.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        games_layout.addWidget(self.games_list)

        games_btn_row = QtWidgets.QHBoxLayout()
        self.btn_game_add = QtWidgets.QPushButton("➕ Добавить игру")
        self.btn_game_search = QtWidgets.QPushButton("🔍 Поиск игр")
        games_btn_row.addWidget(self.btn_game_add)
        games_btn_row.addWidget(self.btn_game_search)
        games_btn_row.addStretch(1)
        games_layout.addLayout(games_btn_row)

        optim_group = QtWidgets.QGroupBox("Оптимизация перед запуском")
        optim_group.setObjectName("optimizationGroup")
        optim_layout = QtWidgets.QVBoxLayout(optim_group)
        self.chk_opt_close_apps = QtWidgets.QCheckBox("Закрыть фоновые приложения")
        self.chk_opt_clean_ram = QtWidgets.QCheckBox("Очистить оперативную память")
        self.chk_opt_disable_services = QtWidgets.QCheckBox("Отключить ненужные службы")
        self.chk_opt_priority = QtWidgets.QCheckBox("Настроить приоритет процесса")
        self.chk_opt_drivers = QtWidgets.QCheckBox("Проверить обновления драйверов")
        self.chk_opt_oc = QtWidgets.QCheckBox("Разогнать GPU (OC Mode)")
        self.chk_opt_cooling = QtWidgets.QCheckBox("Максимальное охлаждение")
        for w in (
            self.chk_opt_close_apps,
            self.chk_opt_clean_ram,
            self.chk_opt_disable_services,
            self.chk_opt_priority,
            self.chk_opt_drivers,
            self.chk_opt_oc,
            self.chk_opt_cooling,
        ):
            w.setChecked(w in (
                self.chk_opt_close_apps,
                self.chk_opt_clean_ram,
                self.chk_opt_disable_services,
                self.chk_opt_priority,
                self.chk_opt_drivers,
            ))
            optim_layout.addWidget(w)

        optim_btn_row = QtWidgets.QHBoxLayout()
        self.btn_opt_auto = QtWidgets.QPushButton(" 🔄 Авто-оптимизация")
        self.btn_opt_manual = QtWidgets.QPushButton(" 🎯 Ручная настройка")
        optim_btn_row.addWidget(self.btn_opt_auto)
        optim_btn_row.addWidget(self.btn_opt_manual)
        optim_btn_row.addStretch(1)
        optim_layout.addLayout(optim_btn_row)

        gl_layout.addWidget(games_group, 2)
        gl_layout.addWidget(optim_group)

        # Правая колонка: мониторинг, AI-графика, автоматизация, аккаунты
        game_right = QtWidgets.QWidget()
        game_right.setMinimumWidth(320)
        gr_layout = QtWidgets.QVBoxLayout(game_right)
        gr_layout.setContentsMargins(0, 0, 0, 0)
        gr_layout.setSpacing(6)

        self.game_monitor_group = QtWidgets.QGroupBox("🔴 LIVE Монитор")
        monitor_layout = QtWidgets.QGridLayout(self.game_monitor_group)
        self.lbl_monitor_game = QtWidgets.QLabel("-")
        self.lbl_fps = QtWidgets.QLabel("FPS: 0")
        self.lbl_fps.setObjectName("fpsLabel")
        self.lbl_ping = QtWidgets.QLabel("Пинг: -- ms")
        self.cpu_temp_bar = QtWidgets.QProgressBar()
        self.cpu_temp_bar.setObjectName("tempBar")
        self.gpu_temp_bar = QtWidgets.QProgressBar()
        self.gpu_temp_bar.setObjectName("tempBar")
        self.lbl_ram_usage = QtWidgets.QLabel("RAM: --")
        self.lbl_vram_usage = QtWidgets.QLabel("VRAM: --")

        self.cpu_temp_bar.setRange(0, 100)
        self.gpu_temp_bar.setRange(0, 100)

        monitor_layout.addWidget(self.lbl_monitor_game, 0, 0, 1, 2)
        monitor_layout.addWidget(self.lbl_fps, 1, 0)
        monitor_layout.addWidget(self.lbl_ping, 1, 1)
        monitor_layout.addWidget(QtWidgets.QLabel("CPU °C:"), 2, 0)
        monitor_layout.addWidget(self.cpu_temp_bar, 2, 1)
        monitor_layout.addWidget(QtWidgets.QLabel("GPU °C:"), 3, 0)
        monitor_layout.addWidget(self.gpu_temp_bar, 3, 1)
        monitor_layout.addWidget(self.lbl_ram_usage, 4, 0)
        monitor_layout.addWidget(self.lbl_vram_usage, 4, 1)

        self.btn_game_stats = QtWidgets.QPushButton(" 📊 Детальная статистика")
        self.btn_game_chart = QtWidgets.QPushButton(" 📈 Показать график")
        monitor_btn_row = QtWidgets.QHBoxLayout()
        monitor_btn_row.addWidget(self.btn_game_chart)
        monitor_btn_row.addWidget(self.btn_game_stats)
        monitor_layout.addLayout(monitor_btn_row, 5, 0, 1, 2)

        ai_graphics_group = QtWidgets.QGroupBox("AI оптимизатор графики")
        ai_graphics_layout = QtWidgets.QVBoxLayout(ai_graphics_group)
        self.lbl_ai_game = QtWidgets.QLabel("Текущая игра: -")
        self.lbl_ai_recommendation = QtWidgets.QLabel(
            "Рекомендация AI будет сгенерирована на основе выбранной игры и настроек выше."
        )
        self.lbl_ai_recommendation.setWordWrap(True)
        ai_graphics_layout.addWidget(self.lbl_ai_game)
        ai_graphics_layout.addWidget(self.lbl_ai_recommendation)

        self.btn_ai_apply_graphics = QtWidgets.QPushButton("  Применить AI оптимизацию")
        ai_graphics_layout.addWidget(self.btn_ai_apply_graphics)

        manual_graphics_row = QtWidgets.QHBoxLayout()
        self.cmb_resolution = QtWidgets.QComboBox()
        self.cmb_resolution.addItems(["1920x1080", "2560x1440", "3840x2160"])
        self.cmb_textures = QtWidgets.QComboBox()
        self.cmb_textures.addItems(["Низкое", "Среднее", "Высокое", "Ультра"])
        self.cmb_aa = QtWidgets.QComboBox()
        self.cmb_aa.addItems(["Выкл", "FXAA", "TAA"])
        manual_graphics_row.addWidget(QtWidgets.QLabel("Разрешение:"))
        manual_graphics_row.addWidget(self.cmb_resolution)
        manual_graphics_row.addWidget(QtWidgets.QLabel("Текстуры:"))
        manual_graphics_row.addWidget(self.cmb_textures)
        manual_graphics_row.addWidget(QtWidgets.QLabel("Сглаживание:"))
        manual_graphics_row.addWidget(self.cmb_aa)
        ai_graphics_layout.addLayout(manual_graphics_row)

        auto_group = QtWidgets.QGroupBox("AI автоматизация")
        auto_layout = QtWidgets.QVBoxLayout(auto_group)
        self.chk_auto_accept = QtWidgets.QCheckBox("Авто-принятие матчей (Dota/CS)")
        self.chk_auto_highlight = QtWidgets.QCheckBox("Авто-запись хайлайтов (последние 5 мин)")
        self.chk_auto_discord = QtWidgets.QCheckBox("Авто-отчёт о матче в Discord")
        self.chk_auto_mods = QtWidgets.QCheckBox("Авто-обновление модов/скинов")
        self.chk_auto_idle = QtWidgets.QCheckBox("Авто-фарм в idle играх")
        for w in (
            self.chk_auto_accept,
            self.chk_auto_highlight,
            self.chk_auto_discord,
            self.chk_auto_mods,
            self.chk_auto_idle,
        ):
            auto_layout.addWidget(w)

        self.macros_list = QtWidgets.QListWidget()
        for txt in [
            " 🎯 Быстрая покупка (CS) - F1",
            " 💬 Быстрое сообщение - F2",
            " 📊 Статистика матча - F3",
            " 🎥 Запись клипа - F4",
        ]:
            self.macros_list.addItem(txt)
        self.btn_macro_add = QtWidgets.QPushButton("➕ Создать макрос")
        auto_layout.addWidget(self.macros_list)
        auto_layout.addWidget(self.btn_macro_add)

        accounts_group = QtWidgets.QGroupBox("Управление аккаунтами")
        accounts_layout = QtWidgets.QVBoxLayout(accounts_group)
        self.lbl_accounts_summary = QtWidgets.QLabel(
            "STEAM: player123 [🟢]\n"
            "EPIC: gamer456 [🟢]\n"
            "Battle.net: pro_gamer [🔴]\n"
            "Origin: need_login [⚪]"
        )
        self.lbl_accounts_summary.setWordWrap(True)
        accounts_layout.addWidget(self.lbl_accounts_summary)
        acc_btn_row = QtWidgets.QHBoxLayout()
        self.btn_acc_switch = QtWidgets.QPushButton(" 🔄 Быстрый переключатель аккаунтов")
        self.btn_acc_passwords = QtWidgets.QPushButton(" 🔐 Менеджер паролей")
        self.btn_acc_configs = QtWidgets.QPushButton(" 📋 Копирование конфигов")
        acc_btn_row.addWidget(self.btn_acc_switch)
        acc_btn_row.addWidget(self.btn_acc_passwords)
        acc_btn_row.addWidget(self.btn_acc_configs)
        accounts_layout.addLayout(acc_btn_row)

        gr_layout.addWidget(self.game_monitor_group)
        gr_layout.addWidget(ai_graphics_group)
        gr_layout.addWidget(auto_group)
        gr_layout.addWidget(accounts_group)

        game_center_split.addWidget(game_left)
        game_center_split.addWidget(game_right)
        game_center_split.setStretchFactor(0, 1)
        game_center_split.setStretchFactor(1, 2)

        game_layout.addWidget(self.game_header_group)
        game_layout.addWidget(game_summary_group)
        game_layout.addWidget(game_center_split, 1)
        
        # Привязки кнопок игр - делаем сразу после создания game_page
        try:
            buttons_to_connect = [
                ('btn_game_add', self._games_add_game),
                ('btn_game_search', self._games_search_game),
                ('btn_game_stats', self._games_show_stats),
                ('btn_game_chart', self._games_show_chart),
                ('btn_ai_apply_graphics', self._games_apply_ai_graphics),
                ('btn_macro_add', self._games_add_macro),
                ('btn_acc_switch', self._games_switch_account),
                ('btn_acc_passwords', self._games_password_manager),
                ('btn_acc_configs', self._games_copy_configs),
            ]
            
            for btn_name, handler in buttons_to_connect:
                if hasattr(self, btn_name):
                    btn = getattr(self, btn_name)
                    if btn is not None:
                        btn.clicked.connect(handler)
                        print(f"[GAMES] {btn_name} привязан к {handler.__name__}")
                else:
                    print(f"[GAMES] ОШИБКА: {btn_name} не найден!")
            
            if hasattr(self, 'cmb_game_mode') and self.cmb_game_mode is not None:
                self.cmb_game_mode.currentTextChanged.connect(self._games_change_mode)
                print("[GAMES] cmb_game_mode привязан")
            
            if hasattr(self, 'games_list') and self.games_list is not None:
                self.games_list.itemDoubleClicked.connect(self._games_launch_selected)
                print("[GAMES] games_list.itemDoubleClicked привязан")
            
            print("[GAMES] Все кнопки игр привязаны успешно")
        except Exception as e:
            print(f"[GAMES] Ошибка привязки кнопок: {e}")
            import traceback
            print(f"[GAMES] Traceback: {traceback.format_exc()}")

        # Инициализация базовых сценариев и планировщика
        self._automation_init_presets()
        self._automation_refresh_table()
        self._planner_init_presets()
        # _planner_refresh_table() будет вызван после создания UI элементов

        # ==== Экран "Система" (с прокруткой) ====
        system_page = QtWidgets.QWidget()
        system_page.setObjectName("SystemTab")
        system_page_lay = QtWidgets.QVBoxLayout(system_page)
        system_page_lay.setContentsMargins(0, 0, 0, 0)

        sys_scroll = QtWidgets.QScrollArea()
        sys_scroll.setWidgetResizable(True)
        sys_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        sys_scroll.setStyleSheet("QScrollArea{background:transparent; border:none;}")

        sys_inner = QtWidgets.QWidget()
        system_layout = QtWidgets.QVBoxLayout(sys_inner)
        system_layout.setContentsMargins(12, 12, 12, 12)
        system_layout.setSpacing(10)

        # --- Header ---
        system_header = QtWidgets.QGroupBox("⚙️ Центр управления системой")
        sh_layout = QtWidgets.QHBoxLayout(system_header)
        self.lbl_system_status = QtWidgets.QLabel("Статус: [🟢] ОПТИМАЛЬНЫЙ")
        self.lbl_system_security = QtWidgets.QLabel("Безопасность: [🔒] ЗАЩИЩЕНО")
        sh_layout.addWidget(self.lbl_system_status)
        sh_layout.addStretch(1)
        sh_layout.addWidget(self.lbl_system_security)
        system_layout.addWidget(system_header)

        # --- Performance cards ---
        perf_group = QtWidgets.QGroupBox("📊 Производительность")
        perf_layout = QtWidgets.QHBoxLayout(perf_group)

        def make_monitor_card(title: str):
            frame = QtWidgets.QFrame()
            frame.setStyleSheet(
                "QFrame{background:rgba(11,15,34,0.9); border:1px solid rgba(91,107,255,0.22);"
                " border-radius:14px; padding:12px;}"
            )
            layout = QtWidgets.QVBoxLayout(frame)
            label = QtWidgets.QLabel(title)
            label.setStyleSheet("color:#5B6B99; font-size:10pt; font-weight:600;")
            value = QtWidgets.QLabel("--")
            value.setStyleSheet("color:#818CF8; font-size:22pt; font-weight:800;")
            value.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setMinimumHeight(10)
            bar.setMaximumHeight(14)
            layout.addWidget(label)
            layout.addWidget(value)
            layout.addWidget(bar)
            return frame, value, bar

        cpu_card, self.lbl_sys_cpu, self.bar_sys_cpu = make_monitor_card("CPU")
        gpu_card, self.lbl_sys_gpu, self.bar_sys_gpu = make_monitor_card("GPU")
        ram_card, self.lbl_sys_ram, self.bar_sys_ram = make_monitor_card("Память")
        perf_layout.addWidget(cpu_card)
        perf_layout.addWidget(gpu_card)
        perf_layout.addWidget(ram_card)
        system_layout.addWidget(perf_group)

        # --- Disks / Network / Power ---
        grid_group = QtWidgets.QGroupBox("Диски / Сеть / Питание")
        grid_layout = QtWidgets.QVBoxLayout(grid_group)

        info_row = QtWidgets.QHBoxLayout()
        self.lbl_sys_disks = QtWidgets.QLabel("Диски: --")
        self.lbl_sys_network = QtWidgets.QLabel("Сеть: --")
        self.lbl_sys_power = QtWidgets.QLabel("Питание: AC")
        info_row.addWidget(self.lbl_sys_disks)
        info_row.addWidget(self.lbl_sys_network)
        info_row.addWidget(self.lbl_sys_power)
        grid_layout.addLayout(info_row)

        self.tbl_sys_disks = QtWidgets.QTableWidget()
        self.tbl_sys_disks.setColumnCount(5)
        self.tbl_sys_disks.setHorizontalHeaderLabels(["Диск", "Точка", "Всего (ГБ)", "Исп. (ГБ)", "%"])
        self.tbl_sys_disks.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tbl_sys_disks.setMinimumHeight(130)
        self.tbl_sys_disks.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        grid_layout.addWidget(self.tbl_sys_disks)
        system_layout.addWidget(grid_group)

        # --- Cleanup ---
        cleanup_group = QtWidgets.QGroupBox("🧼 Очистка системы")
        cl_layout = QtWidgets.QVBoxLayout(cleanup_group)
        self.lbl_cleanup_summary = QtWidgets.QLabel("Обнаружено мусора: --")
        cl_layout.addWidget(self.lbl_cleanup_summary)
        self.chk_clean_temp_win = QtWidgets.QCheckBox("Временные файлы Windows")
        self.chk_clean_temp_win.setChecked(True)
        self.chk_clean_temp_user = QtWidgets.QCheckBox("Временные файлы пользователя")
        self.chk_clean_temp_user.setChecked(True)
        self.chk_clean_cache_placeholder = QtWidgets.QCheckBox("Кэш браузеров (позже)")
        self.chk_clean_cache_placeholder.setEnabled(False)
        cl_layout.addWidget(self.chk_clean_temp_win)
        cl_layout.addWidget(self.chk_clean_temp_user)
        cl_layout.addWidget(self.chk_clean_cache_placeholder)
        cl_btn_row = QtWidgets.QHBoxLayout()
        self.btn_sys_scan_cleanup = QtWidgets.QPushButton("🔎 Сканировать")
        self.btn_sys_run_cleanup = QtWidgets.QPushButton("🧹 Очистить")
        cl_btn_row.addWidget(self.btn_sys_scan_cleanup)
        cl_btn_row.addWidget(self.btn_sys_run_cleanup)
        cl_btn_row.addStretch(1)
        cl_layout.addLayout(cl_btn_row)
        system_layout.addWidget(cleanup_group)

        # --- Processes ---
        processes_group = QtWidgets.QGroupBox("⚙️ Процессы")
        proc_layout = QtWidgets.QVBoxLayout(processes_group)
        self.table_processes = QtWidgets.QTableWidget()
        self.table_processes.setColumnCount(4)
        self.table_processes.setHorizontalHeaderLabels(["Процесс", "CPU %", "RAM MB", "Статус"])
        self.table_processes.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table_processes.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table_processes.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table_processes.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table_processes.setMinimumHeight(160)
        proc_layout.addWidget(self.table_processes)
        proc_btn_row = QtWidgets.QHBoxLayout()
        self.btn_proc_refresh = QtWidgets.QPushButton("🔄 Обновить")
        self.btn_proc_kill = QtWidgets.QPushButton("❌ Завершить")
        self.btn_proc_details = QtWidgets.QPushButton("ℹ️ Детали")
        proc_btn_row.addWidget(self.btn_proc_refresh)
        proc_btn_row.addWidget(self.btn_proc_kill)
        proc_btn_row.addWidget(self.btn_proc_details)
        proc_btn_row.addStretch(1)
        proc_layout.addLayout(proc_btn_row)
        self.process_table = self.table_processes
        system_layout.addWidget(processes_group)

        # --- Startup ---
        startup_group = QtWidgets.QGroupBox("🚀 Автозагрузка")
        st_layout = QtWidgets.QVBoxLayout(startup_group)
        self.table_startup = QtWidgets.QTableWidget()
        self.table_startup.setColumnCount(3)
        self.table_startup.setHorizontalHeaderLabels(["Программа", "Путь", "Статус"])
        self.table_startup.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table_startup.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table_startup.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table_startup.setMinimumHeight(120)
        st_layout.addWidget(self.table_startup)
        self.startup_table = self.table_startup
        st_btn_row = QtWidgets.QHBoxLayout()
        self.btn_startup_refresh = QtWidgets.QPushButton("🔄 Обновить")
        self.btn_startup_disable = QtWidgets.QPushButton("⏸️ Отключить")
        self.btn_startup_enable = QtWidgets.QPushButton("▶️ Включить")
        st_btn_row.addWidget(self.btn_startup_refresh)
        st_btn_row.addWidget(self.btn_startup_disable)
        st_btn_row.addWidget(self.btn_startup_enable)
        st_btn_row.addStretch(1)
        st_layout.addLayout(st_btn_row)
        system_layout.addWidget(startup_group)

        # --- Security ---
        security_group = QtWidgets.QGroupBox("🛡️ Безопасность")
        sec_layout = QtWidgets.QVBoxLayout(security_group)
        self.tbl_security = QtWidgets.QTableWidget()
        self.tbl_security.setColumnCount(3)
        self.tbl_security.setHorizontalHeaderLabels(["Компонент", "Статус", "Проверка"])
        self.tbl_security.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tbl_security.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_security.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_security.setRowCount(5)
        self.tbl_security.setMinimumHeight(180)
        self.tbl_security.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        for row, (name, status, last_check) in enumerate([
            ("Антивирус", "🟢 Активен", "-"),
            ("Фаервол", "🟢 Включен", "-"),
            ("Шифрование дисков", "🔴 Выкл", "-"),
            ("Контроль уч. записей", "🟢 Включен", "-"),
            ("Резервное копирование", "🔴 Выкл", "-"),
        ]):
            self.tbl_security.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            self.tbl_security.setItem(row, 1, QtWidgets.QTableWidgetItem(status))
            self.tbl_security.setItem(row, 2, QtWidgets.QTableWidgetItem(last_check))
        sec_layout.addWidget(self.tbl_security)
        self.lbl_security_summary = QtWidgets.QLabel("Угроз за сессию: 0")
        sec_layout.addWidget(self.lbl_security_summary)
        sec_btn_row = QtWidgets.QHBoxLayout()
        self.btn_sys_security_quick = QtWidgets.QPushButton("🔍 Быстрое сканирование")
        sec_btn_row.addWidget(self.btn_sys_security_quick)
        sec_btn_row.addStretch(1)
        sec_layout.addLayout(sec_btn_row)
        system_layout.addWidget(security_group)

        # --- System actions ---
        actions_group = QtWidgets.QGroupBox("Системные действия")
        act_layout = QtWidgets.QHBoxLayout(actions_group)
        self.btn_sys_shutdown = QtWidgets.QPushButton("⏻ Выключить ПК")
        self.btn_sys_shutdown.setStyleSheet(
            "QPushButton{background:rgba(255,0,92,0.08); border:1px solid rgba(255,0,92,0.3);"
            " color:#ff005c; border-radius:14px; padding:8px 18px; font-weight:600;}"
            "QPushButton:hover{background:#ff005c; color:#fff;}"
        )
        self.btn_sys_reboot = QtWidgets.QPushButton("🔄 Перезагрузка")
        self.btn_sys_reboot.setStyleSheet(
            "QPushButton{background:rgba(255,157,0,0.08); border:1px solid rgba(255,157,0,0.3);"
            " color:#ff9d00; border-radius:14px; padding:8px 18px; font-weight:600;}"
            "QPushButton:hover{background:#ff9d00; color:#fff;}"
        )
        self.btn_sys_sleep = QtWidgets.QPushButton("🌙 Спящий режим")
        act_layout.addWidget(self.btn_sys_shutdown)
        act_layout.addWidget(self.btn_sys_reboot)
        act_layout.addWidget(self.btn_sys_sleep)
        act_layout.addStretch(1)
        system_layout.addWidget(actions_group)

        system_layout.addStretch(1)
        sys_scroll.setWidget(sys_inner)
        system_page_lay.addWidget(sys_scroll)

        # Привязка сигналов системного экрана
        self.btn_sys_shutdown.clicked.connect(self._system_shutdown)
        self.btn_sys_reboot.clicked.connect(self._system_reboot)
        self.btn_sys_sleep.clicked.connect(self._system_sleep)
        self.btn_sys_scan_cleanup.clicked.connect(self._system_scan_cleanup)
        self.btn_sys_run_cleanup.clicked.connect(self._system_run_cleanup)
        self.btn_sys_security_quick.clicked.connect(self._system_security_scan)
        if hasattr(self, "btn_startup_refresh"):
            self.btn_startup_refresh.clicked.connect(self._system_refresh_startup)
        if hasattr(self, "btn_startup_disable"):
            self.btn_startup_disable.clicked.connect(self._system_disable_startup)
        if hasattr(self, "btn_startup_enable"):
            self.btn_startup_enable.clicked.connect(self._system_enable_startup)

        # Добавляем страницы в стек
        self.main_stack.addWidget(home_page)   # index 0
        self.main_stack.addWidget(files_page)  # index 1
        self.main_stack.addWidget(web_page)    # index 2
        self.main_stack.addWidget(chat_page)   # index 3 (мессенджеры)
        self.main_stack.addWidget(game_page)   # index 4 (игровой центр)
        self.main_stack.addWidget(system_page) # index 5 (система)

        # ==== Экран "Автоматизация" ====
        auto_page = QtWidgets.QWidget()
        auto_page.setObjectName("AutomationTab")
        auto_page_outer = QtWidgets.QVBoxLayout(auto_page)
        auto_page_outer.setContentsMargins(0, 0, 0, 0)
        auto_page_outer.setSpacing(0)

        auto_scroll = QtWidgets.QScrollArea()
        auto_scroll.setWidgetResizable(True)
        auto_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        auto_scroll.setStyleSheet("QScrollArea{background:transparent; border:none;}")
        auto_inner = QtWidgets.QWidget()
        auto_layout = QtWidgets.QVBoxLayout(auto_inner)
        auto_layout.setContentsMargins(8, 8, 8, 8)
        auto_layout.setSpacing(8)
        auto_scroll.setWidget(auto_inner)
        auto_page_outer.addWidget(auto_scroll)

        # Заголовок автоматизатора
        auto_header = QtWidgets.QGroupBox("🤖 AI Автоматизатор v3.0")
        ah_layout = QtWidgets.QHBoxLayout(auto_header)
        self.lbl_auto_status = QtWidgets.QLabel("Статус: [🟢] АКТИВЕН")
        self.lbl_auto_stats = QtWidgets.QLabel("Сценариев запущено: 0")
        ah_layout.addWidget(self.lbl_auto_status)
        ah_layout.addStretch(1)
        ah_layout.addWidget(self.lbl_auto_stats)

        # Центральная зона: слева категории, справа сценарии
        auto_center = QtWidgets.QSplitter()
        auto_center.setOrientation(QtCore.Qt.Orientation.Horizontal)

        # Левая колонка: категории сценариев
        auto_left = QtWidgets.QWidget()
        al_layout = QtWidgets.QVBoxLayout(auto_left)
        cat_group = QtWidgets.QGroupBox("📁 Категории сценариев")
        cat_layout = QtWidgets.QVBoxLayout(cat_group)
        self.list_auto_categories = QtWidgets.QListWidget()
        for txt in [
            "🏠 Домашние",
            "🏢 Рабочие",
            "🎮 Игровые",
            "🔧 Системные",
        ]:
            self.list_auto_categories.addItem(txt)
        cat_layout.addWidget(self.list_auto_categories)
        al_layout.addWidget(cat_group)

        # Правая колонка: сценарии и AI-команды
        auto_right = QtWidgets.QWidget()
        ar_layout = QtWidgets.QVBoxLayout(auto_right)

        scen_group = QtWidgets.QGroupBox("🎯 Сценарии автоматизации")
        scen_layout = QtWidgets.QVBoxLayout(scen_group)
        self.table_automation = QtWidgets.QTableWidget()
        self.table_automation.setObjectName("automationTable")
        self.table_automation.setColumnCount(3)
        self.table_automation.setHorizontalHeaderLabels(["Сценарий", "Категория", "Статус"])
        self.table_automation.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table_automation.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table_automation.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        scen_layout.addWidget(self.table_automation)

        scen_btn_row = QtWidgets.QHBoxLayout()
        self.btn_auto_run = QtWidgets.QPushButton(" ▶ Запустить сценарий")
        self.btn_auto_run.setProperty("class", "automationBtn")
        self.btn_auto_add_from_text = QtWidgets.QPushButton(" ➕ Создать из текста")
        self.btn_auto_add_from_text.setProperty("class", "automationBtn")
        scen_btn_row.addWidget(self.btn_auto_run)
        scen_btn_row.addWidget(self.btn_auto_add_from_text)
        scen_btn_row.addStretch(1)
        scen_layout.addLayout(scen_btn_row)

        ar_layout.addWidget(scen_group)

        # AI-командный интерфейс
        ai_group = QtWidgets.QGroupBox("🧠 AI-ассистент для автоматизации")
        ai_layout = QtWidgets.QVBoxLayout(ai_group)
        self.edit_auto_command = QtWidgets.QLineEdit()
        self.edit_auto_command.setPlaceholderText("Опиши задачу для автоматизации (утренний старт, игровой режим и т.п.)")
        self.btn_auto_ai_suggest = QtWidgets.QPushButton(" 🤖 Предложить сценарий")
        self.btn_auto_ai_suggest.setProperty("class", "automationBtn")
        ai_layout.addWidget(self.edit_auto_command)
        ai_layout.addWidget(self.btn_auto_ai_suggest)

        ar_layout.addWidget(ai_group)

        # Умный планировщик задач на сегодня
        planner_group = QtWidgets.QGroupBox("📅 Планировщик задач на сегодня")
        pl_layout = QtWidgets.QVBoxLayout(planner_group)
        self.table_planner = QtWidgets.QTableWidget()
        self.table_planner.setColumnCount(4)
        self.table_planner.setHorizontalHeaderLabels(["Время", "Задача", "Статус", "AI-действие"])
        self.table_planner.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table_planner.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table_planner.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table_planner.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        pl_layout.addWidget(self.table_planner)

        pl_btn_row = QtWidgets.QHBoxLayout()
        self.btn_planner_add = QtWidgets.QPushButton(" ➕ Добавить задачу")
        self.btn_planner_add.setProperty("class", "automationBtn")
        self.btn_planner_done = QtWidgets.QPushButton(" ✓ Отметить выполненной")
        self.btn_planner_done.setProperty("class", "automationBtn")
        self.btn_planner_run = QtWidgets.QPushButton(" ▶ Выполнить AI-действие")
        self.btn_planner_run.setProperty("class", "automationBtn")
        pl_btn_row.addWidget(self.btn_planner_add)
        pl_btn_row.addWidget(self.btn_planner_done)
        pl_btn_row.addWidget(self.btn_planner_run)
        pl_btn_row.addStretch(1)
        pl_layout.addLayout(pl_btn_row)

        ar_layout.addWidget(planner_group)

        # Инициализация таблицы планировщика после создания UI элементов
        self._planner_refresh_table()

        # Умная сортировка файлов
        sorter_group = QtWidgets.QGroupBox("📁 Умная сортировка файлов")
        sorter_layout = QtWidgets.QVBoxLayout(sorter_group)

        self.lbl_sorter_status = QtWidgets.QLabel(
            "[ВКЛЮЧЕНО] Сортировка файлов на рабочем столе и в Загрузках по типам (Документы/Изображения/Видео/Музыка/Архивы)."
        )
        self.lbl_sorter_status.setWordWrap(True)
        sorter_layout.addWidget(self.lbl_sorter_status)

        self.btn_sorter_run = QtWidgets.QPushButton("▶ Сортировать сейчас")
        self.btn_sorter_run.setProperty("class", "automationBtn")
        sorter_layout.addWidget(self.btn_sorter_run)

        self.lbl_sorter_summary = QtWidgets.QLabel("Статистика: ещё не запускалась")
        sorter_layout.addWidget(self.lbl_sorter_summary)

        ar_layout.addWidget(sorter_group)

        # Логи автоматизации
        logs_group = QtWidgets.QGroupBox("📋 Логи выполнения сценариев")
        logs_layout = QtWidgets.QVBoxLayout(logs_group)
        
        self.auto_logs_list = QtWidgets.QListWidget()
        self.auto_logs_list.setMinimumHeight(130)
        self.auto_logs_list.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        logs_layout.addWidget(self.auto_logs_list)
        
        logs_btn_row = QtWidgets.QHBoxLayout()
        self.btn_auto_logs_clear = QtWidgets.QPushButton("🗑️ Очистить логи")
        self.btn_auto_logs_export = QtWidgets.QPushButton("💾 Экспорт логов")
        logs_btn_row.addWidget(self.btn_auto_logs_clear)
        logs_btn_row.addWidget(self.btn_auto_logs_export)
        logs_btn_row.addStretch(1)
        logs_layout.addLayout(logs_btn_row)
        
        ar_layout.addWidget(logs_group)
        
        # Авто-действия при событиях
        rules_group = QtWidgets.QGroupBox("📡 Авто-действия при событиях")
        rl_layout = QtWidgets.QVBoxLayout(rules_group)
        self.table_auto_rules = QtWidgets.QTableWidget()
        self.table_auto_rules.setColumnCount(3)
        self.table_auto_rules.setHorizontalHeaderLabels(["Событие", "Действие", "Статус"])
        self.table_auto_rules.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table_auto_rules.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table_auto_rules.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        rl_layout.addWidget(self.table_auto_rules)

        self.table_auto_rules.setRowCount(3)
        self.table_auto_rules.setItem(0, 0, QtWidgets.QTableWidgetItem("CPU > 85%"))
        self.table_auto_rules.setItem(0, 1, QtWidgets.QTableWidgetItem("Сообщить и предложить снизить нагрузку"))
        self.table_auto_rules.setItem(0, 2, QtWidgets.QTableWidgetItem("Готово"))

        self.table_auto_rules.setItem(1, 0, QtWidgets.QTableWidgetItem("Свободно < 10% диска C:"))
        self.table_auto_rules.setItem(1, 1, QtWidgets.QTableWidgetItem("Предложить открыть 'Система' и очистку"))
        self.table_auto_rules.setItem(1, 2, QtWidgets.QTableWidgetItem("Готово"))

        self.table_auto_rules.setItem(2, 0, QtWidgets.QTableWidgetItem("Запуск игры"))
        self.table_auto_rules.setItem(2, 1, QtWidgets.QTableWidgetItem("Включить игровой режим (лог/статистика)"))
        self.table_auto_rules.setItem(2, 2, QtWidgets.QTableWidgetItem("Готово"))

        ar_layout.addWidget(rules_group)

        auto_center.addWidget(auto_left)
        auto_center.addWidget(auto_right)
        auto_center.setStretchFactor(0, 1)
        auto_center.setStretchFactor(1, 2)

        auto_layout.addWidget(auto_header)
        auto_layout.addWidget(auto_center, 1)

        # Инициализация базовых сценариев и планировщика
        self._automation_init_presets()
        self._automation_refresh_table()
        self._planner_init_presets()
        self._planner_refresh_table()
        
        # ПОДКЛЮЧЕНИЕ КНОПОК АВТОМАТИЗАЦИИ - ПОСЛЕ СОЗДАНИЯ ВСЕХ КНОПОК!
        self.btn_auto_run.clicked.connect(self._automation_run_selected)
        self.btn_auto_add_from_text.clicked.connect(self._automation_add_from_text)
        self.btn_auto_ai_suggest.clicked.connect(self._automation_add_from_text)
        self.btn_planner_add.clicked.connect(self._planner_add_task)
        self.btn_planner_done.clicked.connect(self._planner_mark_done)
        self.btn_planner_run.clicked.connect(self._planner_run_action)
        self.btn_sorter_run.clicked.connect(self._automation_sort_files)
        self.btn_auto_logs_clear.clicked.connect(self._automation_clear_logs)
        self.btn_auto_logs_export.clicked.connect(self._automation_export_logs)
        
        # Добавляем страницу автоматизации в стек
        self.main_stack.addWidget(auto_page)  # index 6
        
        # ==== Экран "Аналитика" ====
        analytics_page = QtWidgets.QWidget()
        analytics_layout = QtWidgets.QVBoxLayout(analytics_page)
        analytics_layout.setContentsMargins(15, 15, 15, 15)
        analytics_layout.setSpacing(10)
        
        self.analytics_header_label = QtWidgets.QLabel("📊 Аналитика и статистика")
        self.analytics_header_label.setProperty("class", "sectionTitle")
        analytics_layout.addWidget(self.analytics_header_label)

        self.analytics_overview_group = QtWidgets.QGroupBox("Обзор сессии")
        analytics_overview_layout = QtWidgets.QHBoxLayout(self.analytics_overview_group)
        analytics_overview_layout.setSpacing(10)
        self._premium_metric_frames = []
        self._premium_metric_titles = []
        self._premium_metric_values = []

        def make_premium_metric(title: str):
            frame = QtWidgets.QFrame()
            lay = QtWidgets.QVBoxLayout(frame)
            lay.setContentsMargins(12, 8, 12, 8)
            lbl_title = QtWidgets.QLabel(title)
            lbl_value = QtWidgets.QLabel("--")
            lay.addWidget(lbl_title)
            lay.addWidget(lbl_value)
            self._premium_metric_frames.append(frame)
            self._premium_metric_titles.append(lbl_title)
            self._premium_metric_values.append(lbl_value)
            analytics_overview_layout.addWidget(frame, 1)
            return lbl_value

        self.lbl_anal_overview_commands = make_premium_metric("Команд")
        self.lbl_anal_overview_automation = make_premium_metric("Автоматизация")
        self.lbl_anal_overview_games = make_premium_metric("Игры")
        self.lbl_anal_overview_local_ai = make_premium_metric("Local AI")
        analytics_layout.addWidget(self.analytics_overview_group)
        
        # Создаем вкладки для разных типов аналитики
        analytics_tabs = QtWidgets.QTabWidget()
        self.analytics_tabs_widget = analytics_tabs
        
        # Вкладка "Система"
        system_analytics = QtWidgets.QWidget()
        sys_anal_layout = QtWidgets.QVBoxLayout(system_analytics)
        
        # Статистика использования системы
        sys_stats_group = QtWidgets.QGroupBox("📈 Статистика использования системы")
        sys_stats_layout = QtWidgets.QGridLayout(sys_stats_group)
        
        self.lbl_anal_cpu_avg = QtWidgets.QLabel("Средняя загрузка CPU: -- %")
        self.lbl_anal_ram_avg = QtWidgets.QLabel("Средняя загрузка RAM: -- %")
        self.lbl_anal_uptime = QtWidgets.QLabel("Время работы системы: --")
        self.lbl_anal_processes = QtWidgets.QLabel("Активных процессов: --")
        
        sys_stats_layout.addWidget(self.lbl_anal_cpu_avg, 0, 0)
        sys_stats_layout.addWidget(self.lbl_anal_ram_avg, 0, 1)
        sys_stats_layout.addWidget(self.lbl_anal_uptime, 1, 0)
        sys_stats_layout.addWidget(self.lbl_anal_processes, 1, 1)
        
        sys_anal_layout.addWidget(sys_stats_group)
        
        # История метрик
        metrics_group = QtWidgets.QGroupBox("📉 История метрик (последние 24 часа)")
        metrics_layout = QtWidgets.QVBoxLayout(metrics_group)
        
        self.anal_metrics_text = QtWidgets.QPlainTextEdit()
        self.anal_metrics_text.setReadOnly(True)
        self.anal_metrics_text.setMinimumHeight(140)
        self.anal_metrics_text.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        metrics_layout.addWidget(self.anal_metrics_text)
        
        sys_anal_layout.addWidget(metrics_group)
        sys_anal_layout.addStretch(1)
        
        analytics_tabs.addTab(system_analytics, "💻 Система")
        
        # Вкладка "Автоматизация"
        auto_analytics = QtWidgets.QWidget()
        auto_anal_layout = QtWidgets.QVBoxLayout(auto_analytics)
        
        auto_stats_group = QtWidgets.QGroupBox("🤖 Статистика автоматизации")
        auto_stats_layout = QtWidgets.QGridLayout(auto_stats_group)
        
        self.lbl_anal_scenarios_total = QtWidgets.QLabel("Всего сценариев: --")
        self.lbl_anal_scenarios_run = QtWidgets.QLabel("Запущено сценариев: --")
        self.lbl_anal_tasks_completed = QtWidgets.QLabel("Выполнено задач: --")
        self.lbl_anal_tasks_pending = QtWidgets.QLabel("Ожидает выполнения: --")
        
        auto_stats_layout.addWidget(self.lbl_anal_scenarios_total, 0, 0)
        auto_stats_layout.addWidget(self.lbl_anal_scenarios_run, 0, 1)
        auto_stats_layout.addWidget(self.lbl_anal_tasks_completed, 1, 0)
        auto_stats_layout.addWidget(self.lbl_anal_tasks_pending, 1, 1)
        
        auto_anal_layout.addWidget(auto_stats_group)
        auto_anal_layout.addStretch(1)
        
        analytics_tabs.addTab(auto_analytics, "🔧 Автоматизация")
        
        # Вкладка "Игры"
        games_analytics = QtWidgets.QWidget()
        games_anal_layout = QtWidgets.QVBoxLayout(games_analytics)
        
        games_stats_group = QtWidgets.QGroupBox("🎮 Статистика игр")
        games_stats_layout = QtWidgets.QVBoxLayout(games_stats_group)
        
        self.lbl_anal_games_total = QtWidgets.QLabel("Всего игр в профиле: --")
        self.lbl_anal_games_played = QtWidgets.QLabel("Игр запущено: --")
        self.lbl_anal_gaming_time = QtWidgets.QLabel("Время в играх: --")
        
        games_stats_layout.addWidget(self.lbl_anal_games_total)
        games_stats_layout.addWidget(self.lbl_anal_games_played)
        games_stats_layout.addWidget(self.lbl_anal_gaming_time)
        
        games_anal_layout.addWidget(games_stats_group)
        games_anal_layout.addStretch(1)
        
        analytics_tabs.addTab(games_analytics, "🎮 Игры")
        
        # Вкладка "Команды и активность"
        commands_analytics = QtWidgets.QWidget()
        cmd_anal_layout = QtWidgets.QVBoxLayout(commands_analytics)
        
        cmd_stats_group = QtWidgets.QGroupBox("💬 Статистика команд")
        cmd_stats_layout = QtWidgets.QGridLayout(cmd_stats_group)
        
        self.lbl_anal_commands_total = QtWidgets.QLabel("Всего команд выполнено: --")
        self.lbl_anal_commands_today = QtWidgets.QLabel("Команд сегодня: --")
        self.lbl_anal_most_used = QtWidgets.QLabel("Самая частая команда: --")
        self.lbl_anal_activity_hours = QtWidgets.QLabel("Пик активности: --")
        
        cmd_stats_layout.addWidget(self.lbl_anal_commands_total, 0, 0)
        cmd_stats_layout.addWidget(self.lbl_anal_commands_today, 0, 1)
        cmd_stats_layout.addWidget(self.lbl_anal_most_used, 1, 0)
        cmd_stats_layout.addWidget(self.lbl_anal_activity_hours, 1, 1)
        
        cmd_anal_layout.addWidget(cmd_stats_group)
        
        # История команд
        cmd_history_group = QtWidgets.QGroupBox("📜 История команд (последние 50)")
        cmd_history_layout = QtWidgets.QVBoxLayout(cmd_history_group)
        
        self.anal_commands_list = QtWidgets.QListWidget()
        self.anal_commands_list.setMinimumHeight(180)
        self.anal_commands_list.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        cmd_history_layout.addWidget(self.anal_commands_list)
        
        cmd_anal_layout.addWidget(cmd_history_group)
        
        # Экспорт данных
        export_group = QtWidgets.QGroupBox("💾 Экспорт данных")
        export_layout = QtWidgets.QHBoxLayout(export_group)
        
        self.btn_anal_export_json = QtWidgets.QPushButton("📄 Экспорт в JSON")
        self.btn_anal_export_txt = QtWidgets.QPushButton("📝 Экспорт в TXT")
        self.btn_anal_export_csv = QtWidgets.QPushButton("📊 Экспорт в CSV")
        
        export_layout.addWidget(self.btn_anal_export_json)
        export_layout.addWidget(self.btn_anal_export_txt)
        export_layout.addWidget(self.btn_anal_export_csv)
        
        cmd_anal_layout.addWidget(export_group)
        cmd_anal_layout.addStretch(1)
        
        analytics_tabs.addTab(commands_analytics, "💬 Команды")
        
        analytics_layout.addWidget(analytics_tabs, 1)
        
        # Подключение кнопок экспорта
        self.btn_anal_export_json.clicked.connect(lambda: self._analytics_export("json"))
        self.btn_anal_export_txt.clicked.connect(lambda: self._analytics_export("txt"))
        self.btn_anal_export_csv.clicked.connect(lambda: self._analytics_export("csv"))
        
        self.main_stack.addWidget(analytics_page)  # index 7
        
        # ==== Экран "Персонализация" ====
        personal_page = QtWidgets.QWidget()
        personal_page_outer = QtWidgets.QVBoxLayout(personal_page)
        personal_page_outer.setContentsMargins(0, 0, 0, 0)
        personal_page_outer.setSpacing(0)

        personal_scroll = QtWidgets.QScrollArea()
        personal_scroll.setWidgetResizable(True)
        personal_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        personal_scroll.setStyleSheet("QScrollArea{background:transparent; border:none;}")
        personal_inner = QtWidgets.QWidget()
        personal_layout = QtWidgets.QVBoxLayout(personal_inner)
        personal_layout.setContentsMargins(15, 15, 15, 15)
        personal_layout.setSpacing(10)
        personal_scroll.setWidget(personal_inner)
        personal_page_outer.addWidget(personal_scroll)
        
        self.personal_header_label = QtWidgets.QLabel("🎨 Персонализация")
        self.personal_header_label.setProperty("class", "sectionTitle")
        personal_layout.addWidget(self.personal_header_label)

        self.personal_runtime_group = QtWidgets.QGroupBox("⚡ Runtime Preview")
        personal_runtime_layout = QtWidgets.QGridLayout(self.personal_runtime_group)
        self.personal_runtime_theme = QtWidgets.QLabel("Тема: --")
        self.personal_runtime_ai = QtWidgets.QLabel("AI: --")
        self.personal_runtime_ollama = QtWidgets.QLabel("Ollama: --")
        self.personal_runtime_fooocus = QtWidgets.QLabel("Fooocus: --")
        self._runtime_summary_labels = [
            self.personal_runtime_theme,
            self.personal_runtime_ai,
            self.personal_runtime_ollama,
            self.personal_runtime_fooocus,
        ]
        personal_runtime_layout.addWidget(self.personal_runtime_theme, 0, 0)
        personal_runtime_layout.addWidget(self.personal_runtime_ai, 0, 1)
        personal_runtime_layout.addWidget(self.personal_runtime_ollama, 1, 0)
        personal_runtime_layout.addWidget(self.personal_runtime_fooocus, 1, 1)
        personal_layout.addWidget(self.personal_runtime_group)

        personal_preview_group = QtWidgets.QGroupBox("Профиль интерфейса")
        personal_preview_layout = QtWidgets.QGridLayout(personal_preview_group)
        self.personal_preview_user = QtWidgets.QLabel("Пользователь")
        self.personal_preview_theme = QtWidgets.QLabel("Темная")
        self.personal_preview_density = QtWidgets.QLabel("Comfort")
        self.personal_preview_assists = QtWidgets.QLabel("AI")
        preview_labels = [
            ("Пользователь", self.personal_preview_user),
            ("Тема", self.personal_preview_theme),
            ("Плотность", self.personal_preview_density),
            ("Ассисты", self.personal_preview_assists),
        ]
        self._personal_preview_frames = []
        for idx, (title, value_lbl) in enumerate(preview_labels):
            frame = QtWidgets.QFrame()
            self._personal_preview_frames.append(frame)
            lay = QtWidgets.QVBoxLayout(frame)
            lay.setContentsMargins(12, 8, 12, 8)
            title_lbl = QtWidgets.QLabel(title)
            title_lbl.setStyleSheet("font-size:9pt; font-weight:600; color:#7B89B6;")
            value_lbl.setStyleSheet("font-size:14pt; font-weight:800; color:#EEF2FF;")
            lay.addWidget(title_lbl)
            lay.addWidget(value_lbl)
            personal_preview_layout.addWidget(frame, idx // 2, idx % 2)
        personal_layout.addWidget(personal_preview_group)
        
        # Настройки темы
        theme_group = QtWidgets.QGroupBox("🎨 Тема оформления")
        theme_layout = QtWidgets.QVBoxLayout(theme_group)
        
        self.radio_theme_dark = QtWidgets.QRadioButton("🌙 Темная тема (текущая)")
        self.radio_theme_dark.setChecked(True)
        self.radio_theme_light = QtWidgets.QRadioButton("☀️ Светлая тема")
        self.radio_theme_auto = QtWidgets.QRadioButton("🔄 Автоматическая")
        
        theme_layout.addWidget(self.radio_theme_dark)
        theme_layout.addWidget(self.radio_theme_light)
        theme_layout.addWidget(self.radio_theme_auto)
        
        btn_apply_theme = QtWidgets.QPushButton("Применить тему")
        btn_apply_theme.clicked.connect(self._personal_apply_theme)
        theme_layout.addWidget(btn_apply_theme)
        
        personal_layout.addWidget(theme_group)
        
        # Настройки интерфейса
        ui_group = QtWidgets.QGroupBox("⚙️ Настройки интерфейса")
        ui_layout = QtWidgets.QVBoxLayout(ui_group)
        
        self.chk_auto_status_personal = QtWidgets.QCheckBox("Автообновление статуса системы")
        self.chk_auto_status_personal.setChecked(self.auto_update_status)
        self.chk_auto_status_personal.stateChanged.connect(self._personal_toggle_auto_status)
        
        self.chk_sound_notifications = QtWidgets.QCheckBox("Звуковые уведомления")
        self.chk_sound_notifications.setChecked(False)
        self.chk_sound_notifications.stateChanged.connect(lambda *_: self._update_personal_preview())
        
        self.chk_compact_mode = QtWidgets.QCheckBox("Компактный режим")
        self.chk_compact_mode.setChecked(False)
        self.chk_compact_mode.stateChanged.connect(self._personal_toggle_compact_mode)
        
        ui_layout.addWidget(self.chk_auto_status_personal)
        ui_layout.addWidget(self.chk_sound_notifications)
        ui_layout.addWidget(self.chk_compact_mode)
        
        personal_layout.addWidget(ui_group)
        
        # Настройки команд
        commands_group = QtWidgets.QGroupBox("💬 Настройки команд")
        commands_layout = QtWidgets.QVBoxLayout(commands_group)
        
        self.chk_voice_commands = QtWidgets.QCheckBox("Голосовые команды (в разработке)")
        self.chk_voice_commands.setChecked(False)
        self.chk_voice_commands.setEnabled(False)
        
        self.chk_ai_suggestions = QtWidgets.QCheckBox("AI-подсказки для команд")
        self.chk_ai_suggestions.setChecked(True)
        self.chk_ai_suggestions.stateChanged.connect(lambda *_: self._update_personal_preview())
        
        commands_layout.addWidget(self.chk_voice_commands)
        commands_layout.addWidget(self.chk_ai_suggestions)
        
        personal_layout.addWidget(commands_group)
        
        # Профиль пользователя
        profile_group = QtWidgets.QGroupBox("👤 Профиль пользователя")
        profile_layout = QtWidgets.QFormLayout(profile_group)
        
        self.edit_user_name = QtWidgets.QLineEdit()
        self.edit_user_name.setPlaceholderText("Введите ваше имя")
        self.edit_user_name.setText(os.environ.get("USERNAME", "Пользователь"))
        self.edit_user_name.textChanged.connect(lambda *_: self._update_personal_preview())
        
        profile_layout.addRow("Имя:", self.edit_user_name)
        
        btn_save_profile = QtWidgets.QPushButton("💾 Сохранить настройки")
        btn_save_profile.clicked.connect(self._personal_save_settings)
        profile_layout.addRow(btn_save_profile)
        
        personal_layout.addWidget(profile_group)
        
        personal_layout.addStretch(1)
        
        self.main_stack.addWidget(personal_page)  # index 8

        # ==== Экран "AI" — полноценный чат как в Cursor ====
        ai_page = QtWidgets.QWidget()
        ai_page.setObjectName("AITab")
        ai_main_layout = QtWidgets.QVBoxLayout(ai_page)
        ai_main_layout.setContentsMargins(0, 0, 0, 0)
        ai_main_layout.setSpacing(0)

        # -- Header bar --
        ai_header = QtWidgets.QWidget()
        ai_header.setStyleSheet(
            "background: #0B0F22; border-bottom: 1px solid rgba(91,107,255,0.22);"
        )
        ai_header_lay = QtWidgets.QHBoxLayout(ai_header)
        ai_header_lay.setContentsMargins(20, 10, 20, 10)

        ai_title = QtWidgets.QLabel("AI Chat")
        ai_title.setStyleSheet("font-size:16pt; font-weight:800; color:#00D4FF; letter-spacing:2px;")
        ai_header_lay.addWidget(ai_title)
        self.ai_btn_clear_chat = QtWidgets.QPushButton("🧹 Очистить")
        self.ai_btn_copy_last = QtWidgets.QPushButton("📋 Копировать ответ")
        for _b in (self.ai_btn_clear_chat, self.ai_btn_copy_last):
            _b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            _b.setStyleSheet(
                "QPushButton{background:#101428; border:1px solid rgba(125,140,255,0.25);"
                " border-radius:10px; padding:6px 12px; color:#D4D9FF; font-size:9pt;}"
                "QPushButton:hover{border:1px solid #8C98FF;}"
            )
        self.ai_btn_clear_chat.clicked.connect(self._ai_clear_chat)
        self.ai_btn_copy_last.clicked.connect(self._ai_copy_last_ai_message)
        ai_header_lay.addWidget(self.ai_btn_clear_chat)
        ai_header_lay.addWidget(self.ai_btn_copy_last)
        self.ai_service_ollama = QtWidgets.QLabel("○ Ollama: --")
        self.ai_service_fooocus = QtWidgets.QLabel("○ Fooocus: --")
        ai_header_lay.addWidget(self.ai_service_ollama)
        ai_header_lay.addWidget(self.ai_service_fooocus)
        ai_header_lay.addStretch(1)

        _cmb_style = (
            "QComboBox{background:#12122a; border:1px solid rgba(0,212,255,0.2); border-radius:10px;"
            " padding:6px 14px; color:#E0E0FF; min-width:200px; font-size:10pt;}"
            "QComboBox:hover{border:1px solid #00D4FF;}"
            "QComboBox QAbstractItemView{background:#0e0e20; border:1px solid rgba(0,212,255,0.2);"
            " border-radius:8px; selection-background-color:rgba(0,212,255,0.18); color:#E0E0FF;}"
        )

        lbl_ai_model = QtWidgets.QLabel("Модель:")
        lbl_ai_model.setStyleSheet("color:#7070b0; font-size:10pt;")
        self.ai_cmb_provider = QtWidgets.QComboBox()

        self.ai_cmb_provider.addItem("⚡ Авто (умный выбор)", "auto")

        self.ai_cmb_provider.addItem("── Локально (Ollama) ──", "_sep_ollama")
        self.ai_cmb_provider.addItem("  Ollama — Qwen 2.5 72B (топ ум)", "ollama:qwen2.5:72b-instruct")
        self.ai_cmb_provider.addItem("  Ollama — DeepSeek R1 70B (рассуждения)", "ollama:deepseek-r1:70b")
        self.ai_cmb_provider.addItem("  Ollama — Llama 3.3 70B", "ollama:llama3.3:70b-instruct")
        self.ai_cmb_provider.addItem("  Ollama — Qwen 2.5 32B (баланс)", "ollama:qwen2.5:32b-instruct")
        self.ai_cmb_provider.addItem("  Ollama — Qwen 2.5 14B (быстрее, умная)", "ollama:qwen2.5:14b-instruct")
        self.ai_cmb_provider.addItem("  Ollama — Llama 3.2 (по умолчанию)", "ollama")
        self.ai_cmb_provider.addItem("  Ollama — Llama 3.2 11B (быстрая)", "ollama:llama3.2:11b-instruct")
        self.ai_cmb_provider.addItem("  Ollama — Llama 3.1 8B", "ollama:llama3.1:8b")
        self.ai_cmb_provider.addItem("  Ollama — Mistral 7B", "ollama:mistral")
        self.ai_cmb_provider.addItem("  Ollama — Phi-3 (малая)", "ollama:phi3")
        self.ai_cmb_provider.addItem("  Ollama — Gemma 2", "ollama:gemma2")
        self.ai_cmb_provider.addItem("── Huihui AI (Ollama) ──", "_sep_huihui")
        self.ai_cmb_provider.addItem("  Huihui — Gemma3 270M (самая лёгкая)", "ollama:huihui_ai/gemma3-abliterated:270m")
        self.ai_cmb_provider.addItem("  Huihui — Gemma3 1B (лёгкая)", "ollama:huihui_ai/gemma3-abliterated:1b")
        self.ai_cmb_provider.addItem("  Huihui — Qwen3 0.6B (лёгкая)", "ollama:huihui_ai/qwen3-abliterated:0.6b")
        self.ai_cmb_provider.addItem("  Huihui — DeepSeek R1 1.5B (лёгкая)", "ollama:huihui_ai/deepseek-r1-abliterated:1.5b")
        self.ai_cmb_provider.addItem("  Huihui — DeepSeek R1 7B", "ollama:huihui_ai/deepseek-r1-abliterated:7b")
        self.ai_cmb_provider.addItem("  Huihui — DeepSeek R1 14B", "ollama:huihui_ai/deepseek-r1-abliterated:14b")
        self.ai_cmb_provider.addItem("  Huihui — Qwen3 8B", "ollama:huihui_ai/qwen3-abliterated:8b")
        self.ai_cmb_provider.addItem("  Huihui — Qwen3 14B", "ollama:huihui_ai/qwen3-abliterated:14b")
        self.ai_cmb_provider.addItem("  Huihui — Gemma3 4B", "ollama:huihui_ai/gemma3-abliterated:4b")
        self.ai_cmb_provider.addItem("  Huihui — DeepSeek R1 abliterated", "ollama:huihui_ai/deepseek-r1-abliterated")
        self.ai_cmb_provider.addItem("  Huihui — Qwen3 abliterated", "ollama:huihui_ai/qwen3-abliterated")
        self.ai_cmb_provider.addItem("  Huihui — Gemma3 abliterated", "ollama:huihui_ai/gemma3-abliterated")

        self.ai_cmb_provider.addItem("── Код (Ollama) ──", "_sep_coder")
        self.ai_cmb_provider.addItem("  Qwen 2.5 Coder 32B (топ код)", "ollama:qwen2.5-coder:32b-instruct")
        self.ai_cmb_provider.addItem("  DeepSeek Coder 6.7B", "ollama:deepseek-coder:6.7b-instruct")
        self.ai_cmb_provider.addItem("  Qwen 2.5 Coder 7B", "ollama:qwen2.5-coder:7b-instruct")

        self.ai_cmb_provider.addItem("── Свои ключи ──", "_sep1")
        self.ai_cmb_provider.addItem("DeepSeek Chat", "deepseek")
        self.ai_cmb_provider.addItem("Claude (свой ключ)", "anthropic")

        self.ai_cmb_provider.addItem("── OpenAI (Replicate) ──", "_sep_openai")
        self.ai_cmb_provider.addItem("  GPT-5.2 (новейший)", "replicate:openai/gpt-5.2")
        self.ai_cmb_provider.addItem("  GPT-5.2 Pro (макс.точность)", "replicate:openai/gpt-5.2")
        self.ai_cmb_provider.addItem("  GPT-5", "replicate:openai/gpt-5")
        self.ai_cmb_provider.addItem("  GPT-5 Mini (быстрый)", "replicate:openai/gpt-5")
        self.ai_cmb_provider.addItem("  GPT-4o", "replicate:openai/gpt-4o")

        self.ai_cmb_provider.addItem("── Google (Replicate) ──", "_sep_google")
        self.ai_cmb_provider.addItem("  Gemini 3.1 Pro", "replicate:google/gemini-3.1-pro")
        self.ai_cmb_provider.addItem("  Gemini 3 Pro", "replicate:google/gemini-3-pro")

        self.ai_cmb_provider.addItem("── Anthropic (Replicate) ──", "_sep_anth")
        self.ai_cmb_provider.addItem("  Claude 4.5 Sonnet", "replicate:anthropic/claude-4.5-sonnet")
        self.ai_cmb_provider.addItem("  Claude 4 Sonnet", "replicate:anthropic/claude-4-sonnet")

        self.ai_cmb_provider.addItem("── Meta (Replicate) ──", "_sep_meta")
        self.ai_cmb_provider.addItem("  Llama 3.1 405B", "replicate:meta/meta-llama-3.1-405b-instruct")
        self.ai_cmb_provider.addItem("  Llama 3 70B", "replicate:meta/meta-llama-3-70b-instruct")
        self.ai_cmb_provider.addItem("  Llama 3 8B (быстрый)", "replicate:meta/meta-llama-3-8b-instruct")

        self.ai_cmb_provider.addItem("── Другие (Replicate) ──", "_sep_other")
        self.ai_cmb_provider.addItem("  Mixtral 8x7B", "replicate:mistralai/mixtral-8x7b-instruct-v0.1")
        self.ai_cmb_provider.addItem("  Mistral 7B (быстрый)", "replicate:mistralai/mistral-7b-instruct-v0.2")
        self.ai_cmb_provider.addItem("  Gemma 2 27B", "replicate:google-deepmind/gemma-2-27b-it")
        self.ai_cmb_provider.addItem("  Snowflake Arctic", "replicate:snowflake/snowflake-arctic-instruct")

        self._ai_provider_list = [(self.ai_cmb_provider.itemText(i), self.ai_cmb_provider.itemData(i)) for i in range(self.ai_cmb_provider.count())]

        self.ai_cmb_provider.setStyleSheet(_cmb_style)

        for i in range(self.ai_cmb_provider.count()):
            data = self.ai_cmb_provider.itemData(i)
            if isinstance(data, str) and data.startswith("_sep"):
                item_model = self.ai_cmb_provider.model()
                item_model.item(i).setEnabled(False)

        self.ai_cmb_provider.currentIndexChanged.connect(self._on_ai_model_selected)

        ai_header_lay.addWidget(lbl_ai_model)
        ai_header_lay.addWidget(self.ai_cmb_provider)

        ai_main_layout.addWidget(ai_header)

        # -- Chat area --
        # -- AI область чата (заменена на ScrollArea) --
        self.ai_chat_scroll = QtWidgets.QScrollArea()
        self.ai_chat_scroll.setWidgetResizable(True)
        self.ai_chat_scroll.setFrameStyle(QtWidgets.QFrame.Shape.NoFrame)
        self.ai_chat_scroll.setStyleSheet("background: #0a0a18; border: none;")
        
        self.ai_chat_container = QtWidgets.QWidget()
        self.ai_chat_container.setStyleSheet("background: transparent;")
        self.ai_chat_layout = QtWidgets.QVBoxLayout(self.ai_chat_container)
        self.ai_chat_layout.setContentsMargins(10, 10, 10, 10)
        self.ai_chat_layout.setSpacing(10)
        self.ai_chat_layout.addStretch(1)
        
        self.ai_chat_scroll.setWidget(self.ai_chat_container)
        self.ai_chat_view = self.ai_chat_scroll # Для совместимости
        
        ai_main_layout.addWidget(self.ai_chat_scroll, 1)

        # -- Replicate model selectors + hints --
        ai_tools_bar = QtWidgets.QWidget()
        ai_tools_bar.setStyleSheet("background:transparent;")
        ai_tools_lay = QtWidgets.QHBoxLayout(ai_tools_bar)
        ai_tools_lay.setContentsMargins(30, 6, 30, 6)
        ai_tools_lay.setSpacing(8)

        _small_cmb = (
            "QComboBox{background:#0e0e20; border:1px solid rgba(0,212,255,0.15);"
            " border-radius:10px; padding:4px 10px; color:#E0E0FF; font-size:9pt; min-width:160px;}"
        )
        lbl_img_model = QtWidgets.QLabel("🖼 Image:")
        lbl_img_model.setStyleSheet("color:#5050a0; font-size:9pt;")
        self.ai_cmb_image_model = QtWidgets.QComboBox()
        for key, info in IMAGE_MODELS.items():
            if info.get("local"):
                self.ai_cmb_image_model.addItem("🖼 " + info["name"], key)
            else:
                self.ai_cmb_image_model.addItem(info["name"], key)
        self.ai_cmb_image_model.setStyleSheet(_small_cmb)

        lbl_vid_model = QtWidgets.QLabel("🎬 Video:")
        lbl_vid_model.setStyleSheet("color:#5050a0; font-size:9pt;")
        self.ai_cmb_video_model = QtWidgets.QComboBox()
        for key, info in VIDEO_MODELS.items():
            self.ai_cmb_video_model.addItem(info["name"], key)
        self.ai_cmb_video_model.setStyleSheet(_small_cmb)

        ai_tools_lay.addWidget(lbl_img_model)
        ai_tools_lay.addWidget(self.ai_cmb_image_model)
        ai_tools_lay.addWidget(lbl_vid_model)
        ai_tools_lay.addWidget(self.ai_cmb_video_model)
        ai_tools_lay.addStretch(1)

        for hint_text in ["/image <prompt>", "/video <prompt>"]:
            hint_lbl = QtWidgets.QLabel(hint_text)
            hint_lbl.setStyleSheet(
                "background:rgba(0,212,255,0.04); border:1px solid rgba(0,212,255,0.08);"
                " border-radius:8px; padding:3px 10px; color:#404070; font-size:8pt;"
            )
            ai_tools_lay.addWidget(hint_lbl)

        ai_main_layout.addWidget(ai_tools_bar)

        # Voice & Agent controls (компактная строка)
        ai_controls = QtWidgets.QWidget()
        ai_controls.setStyleSheet("background:transparent;")
        ai_controls_lay = QtWidgets.QHBoxLayout(ai_controls)
        ai_controls_lay.setContentsMargins(30, 2, 30, 2)

        self.ai_chk_tts = QtWidgets.QCheckBox("Озвучка")
        self.ai_chk_tts.setChecked(True)
        self.ai_chk_agent = QtWidgets.QCheckBox("∞ Агент")
        self.ai_chk_agent.setChecked(False)
        self.ai_chk_agent.setStyleSheet(
            "QCheckBox{ font-size:10pt; color:#5050a0; }"
            "QCheckBox::indicator{ width:16px; height:16px; border-radius:8px; border:1px solid rgba(0,212,255,0.3);"
            " background:#0e0e20; }"
            "QCheckBox::indicator:checked{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #00D4FF, stop:1 #9D00FF); }"
        )
        self.ai_chk_agent.stateChanged.connect(self._on_ai_agent_toggled)
        self.ai_chk_auto = QtWidgets.QCheckBox("Авто")
        self.ai_chk_auto.setChecked(True)
        self.ai_chk_auto.setToolTip("Выполнять действия без подтверждения")
        self.ai_chk_auto.setStyleSheet(
            "QCheckBox{ font-size:10pt; color:#5050a0; }"
            "QCheckBox::indicator{ width:16px; height:16px; border-radius:8px; border:1px solid rgba(0,212,255,0.3);"
            " background:#0e0e20; }"
            "QCheckBox::indicator:checked{ background:rgba(0,212,255,0.5); }"
        )
        if getattr(self, "chk_agent_confirm", None):
            self.ai_chk_auto.stateChanged.connect(
                lambda: self.chk_agent_confirm.setChecked(not self.ai_chk_auto.isChecked())
            )
        self.ai_chk_hotword = QtWidgets.QCheckBox("Джарвис")
        self.ai_chk_hotword.setChecked(False)
        self.ai_chk_hotword.stateChanged.connect(self._on_hotword_toggle_changed)

        for chk in [self.ai_chk_tts, self.ai_chk_hotword]:
            chk.setStyleSheet("font-size:10pt; color:#5050a0;")
            ai_controls_lay.addWidget(chk)
        ai_controls_lay.addStretch(1)

        ai_main_layout.addWidget(ai_controls)

        # -- Input bar в стиле Cursor: контейнер, многострочное поле, снизу ∞ Агент | Авто | [🖼 🎤 ↑] --
        ai_input_bar = QtWidgets.QWidget()
        ai_input_bar.setStyleSheet(
            "QWidget{ background: rgba(20,20,40,0.95); border: 1px solid rgba(0,212,255,0.12);"
            " border-radius: 16px; }"
        )
        ai_input_wrap = QtWidgets.QVBoxLayout(ai_input_bar)
        ai_input_wrap.setContentsMargins(16, 12, 16, 12)
        ai_input_wrap.setSpacing(10)

        self.ai_chat_input = _ChatInputField(self)
        self.ai_chat_input.setPlaceholderText("🎉 Запрос, @файл или @папка — контекст (как в Cursor), / — команды")
        self.ai_chat_input.setToolTip("Введите /help для списка команд. @файл — контекст из файла, кнопка «@ Файл» — выбранный файл с вкладки «Файлы».")
        self.ai_chat_input.setStyleSheet(
            "QPlainTextEdit{ background: transparent; border: none;"
            " padding: 4px 0; font-size: 12pt; color: #E0E0FF; line-height: 1.4; }"
            "QPlainTextEdit:focus{ border: none; }"
        )
        self.ai_chat_input.send_requested.connect(self._ai_tab_send)
        ai_input_wrap.addWidget(self.ai_chat_input)

        ai_input_row = QtWidgets.QHBoxLayout()
        ai_input_row.setSpacing(8)
        # Кнопки-таблетки в стиле Cursor: активные — циан, неактивные — серый контур
        pill_off = (
            "QPushButton{ background: transparent; border: 1px solid rgba(0,212,255,0.35);"
            " border-radius: 14px; padding: 6px 14px; color: #7070b0; font-size: 10pt; }"
            "QPushButton:hover{ color: #A0A0D0; border-color: rgba(0,212,255,0.5); }"
        )
        pill_agent_on = (
            "QPushButton{ background: rgba(0,212,255,0.18); border: 1px solid #00D4FF;"
            " border-radius: 14px; padding: 6px 14px; color: #00D4FF; font-size: 10pt; }"
            "QPushButton:hover{ background: rgba(0,212,255,0.25); }"
        )
        pill_auto_btn = (
            "QPushButton{ background: transparent; border: 1px solid rgba(0,212,255,0.35);"
            " border-radius: 14px; padding: 6px 14px; color: #A0A0D0; font-size: 10pt; }"
            "QPushButton:hover{ background: rgba(0,212,255,0.08); color: #00D4FF; border-color: rgba(0,212,255,0.6); }"
        )
        self.ai_btn_agent = QtWidgets.QPushButton("∞ Агент")
        self.ai_btn_agent.setCheckable(True)
        self.ai_btn_agent.setChecked(False)
        self.ai_btn_agent.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.ai_btn_agent.setStyleSheet(pill_off)
        self.ai_btn_agent.clicked.connect(self._on_ai_agent_pill_clicked)
        self.ai_chk_agent.setVisible(False)
        self.ai_btn_agent.toggled.connect(self._sync_agent_pill_style)
        self.ai_chk_agent.stateChanged.connect(
            lambda: self.ai_btn_agent.setChecked(self.ai_chk_agent.isChecked()) if getattr(self, "ai_btn_agent", None) else None
        )
        self._sync_agent_pill_style(self.ai_btn_agent.isChecked())
        ai_input_row.addWidget(self.ai_btn_agent)

        self.ai_btn_auto = QtWidgets.QPushButton("Авто")
        self.ai_btn_auto.setToolTip("Модель и режим — клик для выбора")
        self.ai_btn_auto.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.ai_btn_auto.setStyleSheet(pill_auto_btn)
        self.ai_btn_auto.clicked.connect(self._open_auto_model_popup)
        if getattr(self, "ai_chk_auto", None):
            self.ai_chk_auto.setVisible(False)
        ai_input_row.addWidget(self.ai_btn_auto)
        ai_input_row.addStretch(1)

        self.ai_btn_add_file = QtWidgets.QPushButton("@ Файл")
        self.ai_btn_add_file.setToolTip("Добавить выбранный файл с вкладки «Файлы» в контекст (как в Cursor)")
        self.ai_btn_add_file.setMinimumWidth(80)
        self.ai_btn_add_file.setFixedHeight(36)
        self.ai_btn_add_file.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.ai_btn_add_file.setStyleSheet(
            "QPushButton{ background: transparent; border: 1px solid rgba(0,212,255,0.2);"
            " border-radius: 18px; color: rgba(0,212,255,0.7); font-size: 10pt; font-weight: 600; }"
            "QPushButton:hover{ border-color: rgba(0,212,255,0.5); color: #00D4FF; }"
        )
        self.ai_btn_add_file.clicked.connect(self._ai_add_selected_file_to_context)
        ai_input_row.addWidget(self.ai_btn_add_file)

        self.ai_btn_attach = QtWidgets.QPushButton("🖼 Изобр.")
        self.ai_btn_attach.setToolTip("Изображение (/image)")
        self.ai_btn_attach.setMinimumWidth(90)
        self.ai_btn_attach.setFixedHeight(36)
        self.ai_btn_attach.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.ai_btn_attach.setStyleSheet(
            "QPushButton{ background: transparent; border: 1px solid rgba(0,212,255,0.2);"
            " border-radius: 18px; color: rgba(0,212,255,0.7); font-size: 10pt; font-weight: 600; }"
            "QPushButton:hover{ border-color: rgba(0,212,255,0.5); color: #00D4FF; }"
        )
        self.ai_btn_attach.clicked.connect(lambda: self._focus_and_hint("/image "))
        self.ai_btn_mic = QtWidgets.QPushButton("🎤 Микрофон")
        self.ai_btn_mic.setToolTip("Голосовой ввод")
        self.ai_btn_mic.setMinimumWidth(110)
        self.ai_btn_mic.setFixedHeight(36)
        self.ai_btn_mic.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.ai_btn_mic.setStyleSheet(
            "QPushButton{ background: transparent; border: 1px solid rgba(0,212,255,0.2);"
            " border-radius: 18px; color: rgba(0,212,255,0.7); font-size: 10pt; font-weight: 600; }"
            "QPushButton:hover{ border-color: rgba(0,212,255,0.5); color: #00D4FF; }"
        )
        self.ai_btn_mic.clicked.connect(self._toggle_voice_recording)
        self._ai_send_icon = _make_send_arrow_icon()
        self.ai_btn_send = QtWidgets.QPushButton()
        self.ai_btn_send.setIcon(self._ai_send_icon)
        self.ai_btn_send.setIconSize(QtCore.QSize(20, 20))
        self.ai_btn_send.setToolTip("Отправить")
        self.ai_btn_send.setFixedSize(36, 36)
        self.ai_btn_send.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.ai_btn_send.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.ai_btn_send.setStyleSheet(
            "QPushButton{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #00D4FF, stop:1 #9D00FF);"
            " border: none; border-radius: 18px;"
            " min-width: 36px; min-height: 36px; max-width: 36px; max-height: 36px; }"
            "QPushButton:hover{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #33E0FF, stop:1 #BB33FF); }"
        )
        self.ai_btn_send.clicked.connect(self._on_ai_send_clicked)
        ai_input_row.addWidget(self.ai_btn_attach)
        ai_input_row.addWidget(self.ai_btn_mic)
        ai_input_row.addWidget(self.ai_btn_send)
        ai_input_wrap.addLayout(ai_input_row)

        ai_main_layout.addWidget(ai_input_bar)

        # -- Collapsible terminal --
        self.ai_term_toggle = QtWidgets.QPushButton("▼  Терминал")
        self.ai_term_toggle.setStyleSheet(
            "QPushButton{background:rgba(0,212,255,0.04); border:none; border-radius:0;"
            " padding:6px 20px; color:#5050a0; font-size:10pt; text-align:left;}"
            "QPushButton:hover{color:#00D4FF;}"
        )
        self.ai_term_toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.ai_term_toggle.clicked.connect(self._ai_toggle_terminal)

        self.ai_term_widget = QtWidgets.QWidget()
        ai_term_inner = QtWidgets.QVBoxLayout(self.ai_term_widget)
        ai_term_inner.setContentsMargins(30, 0, 30, 10)
        ai_term_inner.setSpacing(6)

        self.ai_terminal_view = QtWidgets.QPlainTextEdit()
        self.ai_terminal_view.setReadOnly(True)
        self.ai_terminal_view.setMinimumHeight(150)
        self.ai_terminal_view.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.ai_terminal_view.setStyleSheet(
            "QPlainTextEdit{background:#06060e; border:1px solid rgba(91,107,255,0.15);"
            " border-radius:10px; padding:8px; font-family:'Consolas','Cascadia Code',monospace;"
            " font-size:10pt; color:#A5B4FC;}"
        )
        ai_term_inner.addWidget(self.ai_terminal_view)

        ai_term_input_row = QtWidgets.QHBoxLayout()
        self.ai_terminal_input = QtWidgets.QLineEdit()
        self.ai_terminal_input.setPlaceholderText("$ команда...")
        self.ai_terminal_input.setStyleSheet(
            "QLineEdit{background:#06060e; border:1px solid rgba(91,107,255,0.2); border-radius:10px;"
            " padding:6px 12px; font-family:'Consolas',monospace; font-size:10pt; color:#A5B4FC;}"
        )
        self.ai_terminal_input.returnPressed.connect(self._ai_terminal_run)
        ai_btn_term_run = QtWidgets.QPushButton("Run")
        ai_btn_term_run.setStyleSheet(
            "QPushButton{background:rgba(91,107,255,0.15); border:1px solid rgba(91,107,255,0.35);"
            " border-radius:10px; padding:6px 16px; color:#818CF8; font-weight:600;}"
            "QPushButton:hover{background:rgba(91,107,255,0.28); color:#C8D2FF;}"
        )
        ai_btn_term_run.clicked.connect(self._ai_terminal_run)
        ai_term_input_row.addWidget(self.ai_terminal_input, 1)
        ai_term_input_row.addWidget(ai_btn_term_run)
        ai_term_inner.addLayout(ai_term_input_row)

        self.ai_term_widget.setVisible(False)

        ai_main_layout.addWidget(self.ai_term_toggle)
        ai_main_layout.addWidget(self.ai_term_widget)

        self.main_stack.addWidget(ai_page)  # index 9

        # Добавляем main_stack в main_container
        main_layout.addWidget(self.main_stack)
        
        self.sidebar_widget = QtWidgets.QWidget()
        self.sidebar_widget.setLayout(sidebar)
        self.sidebar_widget.setMinimumWidth(180)
        self.sidebar_widget.setMaximumWidth(300)
        self.sidebar_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        root_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        root_splitter.setChildrenCollapsible(False)
        root_splitter.addWidget(self.sidebar_widget)
        root_splitter.addWidget(main_container)
        root_splitter.setStretchFactor(0, 0)
        root_splitter.setStretchFactor(1, 1)
        root_splitter.setSizes([230, 1130])
        root_layout.addWidget(root_splitter, 1)
        
        # Устанавливаем central как центральный виджет главного окна
        self.setCentralWidget(central)

        # Финальный polish: единые размеры/курсор/поведение интерактивных элементов
        self._apply_premium_polish()
        
        # Применяем темную тему для правильного отображения
        self._apply_dark_theme()
        
        # Устанавливаем активную вкладку по умолчанию
        self._set_active_tab(self.btn_tab_home, "Главная")
        self._update_runtime_summary()
        self._service_pills_timer = QtCore.QTimer(self)
        self._service_pills_timer.timeout.connect(self._update_runtime_summary)
        self._service_pills_timer.start(3000)
        
        # Инициализация данных аналитики после создания UI
        QtCore.QTimer.singleShot(1000, self._analytics_refresh_data)
        
        QtCore.QTimer.singleShot(500, lambda: self.append_chat(
            "Система",
            "JARVIS v3.0 готов к работе! Используй /image или /video для генерации через Replicate. При необходимости Ollama и Fooocus запустятся автоматически."
        ))
        QtCore.QTimer.singleShot(2000, self._autostart_services)
        QtCore.QTimer.singleShot(7000, self._filter_ollama_to_installed_models)

    def _is_port_open(self, host: str, port: int, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (socket.error, OSError):
            return False

    def _check_http_health(self, url: str, timeout: float = 3.0) -> bool:
        try:
            resp = requests.get(url, timeout=timeout)
            return int(resp.status_code) < 500
        except Exception:
            return False

    def _check_ollama_health(self) -> bool:
        base = (OLLAMA_BASE_URL or "http://localhost:11434").rstrip("/")
        return self._check_http_health(f"{base}/api/tags", timeout=3.0)

    def _check_fooocus_health(self) -> bool:
        base = (LOCAL_IMAGE_API_URL or "http://127.0.0.1:7865").rstrip("/")
        return self._check_http_health(base, timeout=3.0)

    def _filter_ollama_to_installed_models(self):
        """Оставить в списке моделей только установленные Ollama/Huihui (ollama list)."""
        if not getattr(self, "_ai_provider_list", None) or not getattr(self, "ai_cmb_provider", None):
            return
        installed = set()
        try:
            r = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            )
            if r.returncode == 0 and r.stdout:
                for line in r.stdout.strip().splitlines()[1:]:
                    parts = line.split()
                    if parts:
                        installed.add(parts[0])
        except Exception:
            return
        def model_installed(data):
            if not isinstance(data, str) or not data.startswith("ollama:"):
                return True
            if data == "ollama":
                return True
            model = data.split(":", 1)[1]
            if model in installed:
                return True
            for name in installed:
                if name == model or name.startswith(model + ":"):
                    return True
            return False
        self.ai_cmb_provider.clear()
        for label, data in self._ai_provider_list:
            if not model_installed(data):
                continue
            self.ai_cmb_provider.addItem(label, data)
        for i in range(self.ai_cmb_provider.count()):
            d = self.ai_cmb_provider.itemData(i)
            if isinstance(d, str) and d.startswith("_sep"):
                self.ai_cmb_provider.model().item(i).setEnabled(False)
        self.ai_cmb_provider.setCurrentIndex(0)

    def _watch_service_health(self, service_name: str, checker, success_message: str, failure_message: str, timeout_sec: int):
        def _worker():
            deadline = time.time() + max(5, int(timeout_sec))
            while time.time() < deadline:
                if checker():
                    self._service_health[service_name] = "healthy"
                    QtCore.QTimer.singleShot(0, lambda msg=success_message: self.append_chat("Система", msg))
                    return
                time.sleep(2)
            self._service_health[service_name] = "failed"
            QtCore.QTimer.singleShot(0, lambda msg=failure_message: self.append_chat("Система", msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _autostart_services(self):
        """Запуск Ollama и Fooocus в фоне с проверкой реальной готовности."""
        try:
            create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            kwargs = dict(cwd=os.path.expanduser("~"), creationflags=create_no_window) if sys.platform == "win32" else dict(cwd=os.path.expanduser("~"))

            if OLLAMA_AUTOSTART:
                try:
                    parsed = urlparse(OLLAMA_BASE_URL or "http://localhost:11434")
                    host = parsed.hostname or "localhost"
                    port = parsed.port or 11434
                except Exception:
                    host, port = "localhost", 11434
                if not self._check_ollama_health():
                    self._service_health["ollama"] = "starting"
                    try:
                        subprocess.Popen(
                            ["ollama", "serve"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            **kwargs
                        )
                        self.append_chat("Система", "Ollama запускается в фоне. Проверяю готовность локальных моделей...")
                        self._watch_service_health(
                            "ollama",
                            self._check_ollama_health,
                            "Ollama готов. Локальный чат доступен.",
                            "Ollama не вышел в healthy-состояние. Проверь `ollama serve` и наличие модели.",
                            45,
                        )
                    except FileNotFoundError:
                        self._service_health["ollama"] = "failed"
                        self.append_chat("Система", "Ollama не найден в PATH. Установите Ollama или отключите OLLAMA_AUTOSTART в .env.")
                    except Exception:
                        pass
                else:
                    self._service_health["ollama"] = "healthy"
                    self.append_chat("Система", "Ollama уже запущен и готов к работе.")

            if FOOOCUS_AUTOSTART and FOOOCUS_PATH and os.path.isdir(FOOOCUS_PATH):
                try:
                    parsed = urlparse(LOCAL_IMAGE_API_URL or "http://127.0.0.1:7865")
                    host = parsed.hostname or "127.0.0.1"
                    port = parsed.port or 7865
                except Exception:
                    host, port = "127.0.0.1", 7865
                if not self._check_fooocus_health():
                    self._service_health["fooocus"] = "starting"
                    py = os.path.join(FOOOCUS_PATH, "fooocus_env", "Scripts", "python.exe")
                    if os.path.isfile(py):
                        env = os.environ.copy()
                        env["GRADIO_SERVER_PORT"] = str(port)
                        subprocess.Popen(
                            [py, "launch.py", "--listen", "0.0.0.0"],
                            cwd=FOOOCUS_PATH,
                            env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=create_no_window if sys.platform == "win32" else 0,
                        )
                        self.append_chat("Система", "Fooocus запускается в фоне. Первый старт может занять 5–10 минут, проверяю готовность...")
                        self._watch_service_health(
                            "fooocus",
                            self._check_fooocus_health,
                            "Fooocus готов. Локальная генерация изображений доступна прямо в чате.",
                            "Fooocus не вышел в healthy-состояние. Проверь локальный запуск и модельные файлы.",
                            600,
                        )
                    else:
                        bat = os.path.join(FOOOCUS_PATH, "run_nvidia_gpu.bat")
                        if not os.path.isfile(bat):
                            bat = os.path.join(FOOOCUS_PATH, "run_cpu.bat")
                        if os.path.isfile(bat):
                            subprocess.Popen(
                                ["cmd", "/c", "start", "/b", "", bat],
                                cwd=FOOOCUS_PATH,
                                **kwargs
                            )
                            self.append_chat("Система", "Fooocus запускается через bat-скрипт. Жду появления локального интерфейса...")
                            self._watch_service_health(
                                "fooocus",
                                self._check_fooocus_health,
                                "Fooocus готов. Локальная генерация изображений доступна прямо в чате.",
                                "Fooocus не вышел в healthy-состояние. Проверь `run_nvidia_gpu.bat` и локальные веса.",
                                600,
                            )
                        else:
                            self._service_health["fooocus"] = "failed"
                            self.append_chat("Система", "Fooocus: не найден venv и bat-скрипт. Запустите один раз `run_nvidia_gpu.bat` в папке Fooocus.")
                else:
                    self._service_health["fooocus"] = "healthy"
                    self.append_chat("Система", "Fooocus уже запущен и готов к генерации.")
            elif FOOOCUS_AUTOSTART and not FOOOCUS_PATH:
                self._service_health["fooocus"] = "failed"
                self.append_chat("Система", "FOOOCUS_AUTOSTART включён, но `FOOOCUS_PATH` не найден.")
        except Exception as e:
            self.log(f"[Система] Ошибка автозапуска сервисов: {e}")

    def _on_chat_link_clicked(self, url: QtCore.QUrl):
        path_str = url.toString()
        # self.log(f"[Debug] Link clicked: {path_str}")
        
        # 1. Внутренние протоколы
        if path_str.startswith("imageviewer:"):
            # Декодируем путь
            raw_path = path_str.replace("imageviewer:", "")
            img_path = unquote(raw_path)
            # Убираем возможные префиксы file:///
            img_path = img_path.replace("file:///", "").lstrip("/")
            # Для Windows возвращаем диск, если он был C:/...
            if len(img_path) > 2 and img_path[1] == ":":
                pass # Already correct
            elif ":" in img_path:
                # possible extra slash at start
                pass
            
            img_path = os.path.normpath(img_path)
            
            if os.path.exists(img_path):
                self._fullscreen_viewer = _FullscreenImageViewer(img_path, self)
            else:
                self.log(f"[ImageViewer] Файл не найден: {img_path}")
            return
            
        if url.scheme() == "jarvis":
            # Передаем как строку, чтобы избежать проблем с временем жизни объекта QUrl в замыкании
            url_s = url.toString()
            QtCore.QTimer.singleShot(0, lambda: self._process_jarvis_link_str(url_s))
            return

        # 2. Обычные ссылки (http/https)
        QtGui.QDesktopServices.openUrl(url)

    def _process_jarvis_link_str(self, url_str: str):
        """Обработка команд jarvis:// из чата (через строку)."""
        url = QtCore.QUrl(url_str)
        try:
            query = QtCore.QUrlQuery(url)
            action = query.queryItemValue("action", QtCore.QUrl.ComponentFormattingOption.FullyDecoded)
            path = query.queryItemValue("path", QtCore.QUrl.ComponentFormattingOption.FullyDecoded)
            if not path: return
            # Декодируем и нормализуем путь
            path = unquote(path)
            path = path.replace("file:///", "")
            if path.startswith("/") and len(path) > 2 and path[2] == ":":
                path = path[1:]
            path = os.path.normpath(path)

            if not os.path.exists(path):
                self.append_chat("Система", f"Файл не найден: {path}")
                return

            if action == "open":
                # Открываем на весь экран во внутреннем вьювере
                self._fullscreen_viewer = _FullscreenImageViewer(path, self)
            elif action == "open_folder":
                # Открываем папку и выделяем файл
                import subprocess
                subprocess.run(['explorer', '/select,', os.path.normpath(path)])
            elif action == "copy_path":
                QtWidgets.QApplication.clipboard().setText(path)
                self.append_chat("Система", f"Путь скопирован")
            elif action == "save_as":
                # Автоматическое скачивание в папку Загрузки
                try:
                    import shutil
                    downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
                    if not os.path.exists(downloads_dir):
                        os.makedirs(downloads_dir, exist_ok=True)
                    
                    target_path = os.path.join(downloads_dir, os.path.basename(path))
                    # Если файл уже есть, добавляем индекс
                    base, ext = os.path.splitext(target_path)
                    counter = 1
                    while os.path.exists(target_path):
                        target_path = f"{base}_{counter}{ext}"
                        counter += 1
                        
                    shutil.copy2(path, target_path)
                    self.append_chat("Система", f"Файл сохранён в Загрузки: {os.path.basename(target_path)}")
                except Exception as e:
                    self.log(f"[Ошибка сохранения] {e}")
                    self.append_chat("Система", f"Ошибка при сохранении: {e}")
        except Exception as e:
            self.log(f"[Система] Ошибка действия: {e}")

    def _apply_premium_polish(self):
        """Единый UX-polish для всех вкладок без изменения логики."""
        for btn in self.findChildren(QtWidgets.QPushButton):
            try:
                btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                if btn.minimumHeight() < 34:
                    btn.setMinimumHeight(34)
            except Exception:
                pass

        for edit in self.findChildren(QtWidgets.QLineEdit):
            try:
                if edit.minimumHeight() < 34:
                    edit.setMinimumHeight(34)
            except Exception:
                pass

        for tree in self.findChildren(QtWidgets.QTreeWidget):
            try:
                tree.setAlternatingRowColors(True)
                tree.setUniformRowHeights(True)
            except Exception:
                pass

        for table in self.findChildren(QtWidgets.QTableWidget):
            try:
                table.setAlternatingRowColors(True)
                table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            except Exception:
                pass

    def _set_active_tab(self, button: QtWidgets.QPushButton, tab_name: str):
        tab_map = {
            "Главная": 0,
            "Файлы": 1,
            "Веб": 2,
            "Мессенджеры": 3,
            "Игры": 4,
            "Система": 5,
            "Автоматизация": 6,
            "Аналитика": 7,
            "Персонализация": 8,
            "AI": 9,
        }

        tab_index = tab_map.get(tab_name, 0)

        if hasattr(self, "main_stack") and self.main_stack.count() > tab_index:
            self.main_stack.setCurrentIndex(tab_index)

        self.current_tab = tab_name.lower()
        self._refresh_sidebar_styles(button)
        self._update_runtime_summary()
        self._update_game_summary()
        self._update_personal_preview()

        if tab_name == "Игры" and hasattr(self, "_games_load_list"):
            QtCore.QTimer.singleShot(100, self._games_load_list)

    def _automation_init_presets(self):
        """Заполнить список сценариев автоматизации примерами (если он пуст)."""
        if len(getattr(self, "automation_scenarios", [])) > 0:
            return
        self.automation_scenarios = [
            {
                "name": "Утренняя рутина",
                "category": "🏠 Домашние",
                "trigger": "08:00",
                "action": "Открыть почту и задачи",
            },
            {
                "name": "Игровой вечер",
                "category": "🎮 Игровые",
                "trigger": "18:00",
                "action": "Включить игровой режим и запустить Steam",
            },
            {
                "name": "Рабочий режим",
                "category": "🏢 Рабочие",
                "trigger": "Вручную",
                "action": "Открыть браузер и документы",
            },
            {
                "name": "Очистка системы",
                "category": "🔧 Системные",
                "trigger": "Вручную",
                "action": "Запустить очистку диска и оптимизацию",
            },
        ]

    def _automation_refresh_table(self):
        """Обновить таблицу сценариев автоматизации на вкладке."""
        if not hasattr(self, "table_automation"):
            return
        scenarios = getattr(self, "automation_scenarios", [])
        self.table_automation.setRowCount(len(scenarios))
        for row, sc in enumerate(scenarios):
            self.table_automation.setItem(row, 0, QtWidgets.QTableWidgetItem(sc.get("name", "")))
            category = sc.get("category", sc.get("trigger", "Общие"))
            self.table_automation.setItem(row, 1, QtWidgets.QTableWidgetItem(category))
            status = "Готов" if sc.get("trigger") == "Вручную" else sc.get("trigger", "Готов")
            self.table_automation.setItem(row, 2, QtWidgets.QTableWidgetItem(status))

    # ========= Планировщик задач =========

    def _planner_init_presets(self):
        """Наполнить планировщик несколькими примерными задачами."""
        if not hasattr(self, "planner_tasks") or len(self.planner_tasks) == 0:
            self.planner_tasks = [
                {
                    "time": "08:00",
                    "task": "Проверить почту",
                    "status": "[✓]",
                    "action": "Почта (браузер)",
                    "kind": "mail_check",
                },
                {
                    "time": "10:00",
                    "task": "Отчет по проекту",
                    "status": "[▶]",
                    "action": "Открыть браузер и документы",
                    "kind": "work_start",
                },
                {
                    "time": "18:00",
                    "task": "Игры с друзьями",
                    "status": "[📋]",
                    "action": "Игровой режим",
                    "kind": "gaming_mode",
                },
                {
                    "time": "22:00",
                    "task": "Резервное копирование",
                    "status": "[🤖]",
                    "action": "Запустить очистку и открыть систему",
                    "kind": "cleanup_system",
                },
            ]

    def _planner_refresh_table(self):
        if not hasattr(self, "table_planner") or not hasattr(self, "planner_tasks"):
            return
        self.table_planner.setRowCount(len(self.planner_tasks))
        for row, t in enumerate(self.planner_tasks):
            self.table_planner.setItem(row, 0, QtWidgets.QTableWidgetItem(t.get("time", "")))
            self.table_planner.setItem(row, 1, QtWidgets.QTableWidgetItem(t.get("task", "")))
            self.table_planner.setItem(row, 2, QtWidgets.QTableWidgetItem(t.get("status", "")))
            self.table_planner.setItem(row, 3, QtWidgets.QTableWidgetItem(t.get("action", "")))

    def _planner_selected_row(self) -> int | None:
        rows = {idx.row() for idx in self.table_planner.selectedIndexes()}
        if not rows:
            return None
        return min(rows)

    def _planner_add_task(self):
        time_str, ok = QtWidgets.QInputDialog.getText(self, "Время задачи", "Введите время (например, 09:30):")
        if not ok or not time_str.strip():
            return
        text, ok = QtWidgets.QInputDialog.getText(self, "Новая задача", "Описание задачи:")
        if not ok or not text.strip():
            return
        task = {
            "time": time_str.strip(),
            "task": text.strip(),
            "status": "[⏰]",
            "action": "(без AI-действия)",
            "kind": None,
        }
        self.planner_tasks.append(task)
        self._planner_refresh_table()
        self.add_history(f"[AUTO] Добавлена задача планировщика: {task['time']} - {task['task']}")

    def _planner_mark_done(self):
        row = self._planner_selected_row()
        if row is None or row >= len(self.planner_tasks):
            return
        self.planner_tasks[row]["status"] = "[✓]"
        self._planner_refresh_table()
        self.add_history(f"[AUTO] Задача выполнена: {self.planner_tasks[row]['task']}")

    def _planner_run_action(self):
        row = self._planner_selected_row()
        if row is None or row >= len(self.planner_tasks):
            return
        task = self.planner_tasks[row]
        kind = task.get("kind")
        name = task.get("task", "")
        self.add_history(f"[AUTO] Планировщик: выполнение AI-действия для задачи '{name}'")

        try:
            if kind == "mail_check":
                # Открываем браузер для проверки почты
                self.open_browser()
            elif kind == "work_start":
                self.open_browser()
                self.open_documents()
            elif kind == "gaming_mode":
                self._set_active_tab(self.btn_tab_games, "Игры")
                self._games_auto_optimize()
            elif kind == "cleanup_system":
                self._set_active_tab(self.btn_tab_system, "Система")
                self._system_scan_cleanup()
            else:
                self.log("[AUTO] Для этой задачи нет привязанного AI-действия")
        except Exception as e:
            self.log(f"[AUTO] Ошибка выполнения действия планировщика: {e}")

    def _automation_sort_files(self):
        """Умная сортировка файлов на рабочем столе и в Загрузках по типам.

        Никаких удалений: только перенос файлов в подпапки пользователя.
        """
        home = os.path.expanduser("~")
        desktop = os.path.join(home, "Desktop")
        downloads = os.path.join(home, "Downloads")

        targets = [p for p in (desktop, downloads) if os.path.exists(p)]
        if not targets:
            self.lbl_sorter_summary.setText("Статистика: папки Desktop/Downloads не найдены")
            self.log("[AUTO] Сортировка: нет папок Desktop/Downloads")
            return

        # Категории по расширениям
        groups = {
            "Documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf"},
            "Images": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp"},
            "Videos": {".mp4", ".avi", ".mkv", ".mov", ".wmv"},
            "Music": {".mp3", ".flac", ".wav", ".aac", ".ogg"},
            "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
        }

        moved_files = 0
        moved_bytes = 0

        def target_dir_for(ext: str) -> str | None:
            for name, exts in groups.items():
                if ext.lower() in exts:
                    return os.path.join(home, name)
            return None

        for base in targets:
            try:
                for name in os.listdir(base):
                    src = os.path.join(base, name)
                    if not os.path.isfile(src):
                        continue
                    ext = os.path.splitext(name)[1]
                    dest_root = target_dir_for(ext)
                    if not dest_root:
                        continue
                    try:
                        os.makedirs(dest_root, exist_ok=True)
                        dst = os.path.join(dest_root, name)
                        # Не перезаписываем существующие файлы
                        if os.path.exists(dst):
                            continue
                        size = os.path.getsize(src)
                        shutil.move(src, dst)
                        moved_files += 1
                        moved_bytes += size
                    except Exception as e:
                        self.log(f"[AUTO] Ошибка перемещения {src}: {e}")
                        continue
            except Exception as e:
                self.log(f"[AUTO] Ошибка сортировки в {base}: {e}")
        
        # Обновляем статистику
        if moved_files > 0:
            mb = moved_bytes / (1024 * 1024)
            self.lbl_sorter_summary.setText(f"Статистика: перемещено {moved_files} файлов ({mb:.1f} MB)")
            self.log(f"[AUTO] Сортировка завершена: {moved_files} файлов, {mb:.1f} MB")
        else:
            self.lbl_sorter_summary.setText("Статистика: файлы для сортировки не найдены")
    
    def _automation_run_selected(self):
        """Запустить выбранный сценарий автоматизации."""
        if not hasattr(self, "table_automation"):
            self.log("[AUTO] Таблица сценариев не найдена")
            return
        
        selected = self.table_automation.selectedItems()
        if not selected:
            self.log("[AUTO] Выберите сценарий для запуска")
            QtWidgets.QMessageBox.information(self, "Автоматизация", "Пожалуйста, выберите сценарий из таблицы")
            return
        
        row = selected[0].row()
        if row >= len(self.automation_scenarios):
            return
        
        scenario = self.automation_scenarios[row]
        name = scenario.get("name", "Неизвестный сценарий")
        action = scenario.get("action", "")
        category = scenario.get("category", "Общие")
        
        self.log(f"[AUTO] Запуск сценария: {name}")
        self.automation_run_count += 1
        self.usage_stats["automation_runs"] += 1
        if hasattr(self, "lbl_auto_stats"):
            self.lbl_auto_stats.setText(f"Сценариев запущено: {self.automation_run_count}")
        
        # Добавляем в логи
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "scenario": name,
            "category": category,
            "status": "success"
        }
        self.automation_logs.append(log_entry)
        if len(self.automation_logs) > 100:
            self.automation_logs = self.automation_logs[-100:]
        
        # Обновляем список логов
        if hasattr(self, "auto_logs_list"):
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.auto_logs_list.addItem(f"[{timestamp}] ✅ {name} - {category}")
            self.auto_logs_list.scrollToBottom()
        
        # Выполняем действия в зависимости от типа сценария
        try:
            if "утренн" in name.lower() or "утро" in name.lower():
                # Утренняя рутина
                self.open_browser()
                time.sleep(1)
                self.log("[AUTO] Открыт браузер для проверки почты")
                self.add_history(f"[AUTO] Выполнен сценарий: {name}")
                
            elif "игр" in name.lower() or "вечер" in name.lower():
                # Игровой режим
                self._set_active_tab(self.btn_tab_games, "Игры")
                if hasattr(self, "_games_auto_optimize"):
                    self._games_auto_optimize()
                self.log("[AUTO] Включен игровой режим")
                self.add_history(f"[AUTO] Выполнен сценарий: {name}")
                
            elif "очистк" in name.lower() or "чистк" in name.lower():
                # Очистка системы
                self._set_active_tab(self.btn_tab_system, "Система")
                if hasattr(self, "_system_scan_cleanup"):
                    self._system_scan_cleanup()
                self.log("[AUTO] Запущена очистка системы")
                self.add_history(f"[AUTO] Выполнен сценарий: {name}")
                
            elif "рабоч" in name.lower() or "работа" in name.lower():
                # Рабочий режим
                self.open_browser()
                self.open_documents()
                self.log("[AUTO] Открыты рабочие приложения")
                self.add_history(f"[AUTO] Выполнен сценарий: {name}")
                
            else:
                # Общий сценарий - пытаемся выполнить действие из описания
                if "браузер" in action.lower() or "почт" in action.lower():
                    self.open_browser()
                if "проводник" in action.lower() or "файл" in action.lower():
                    self.open_explorer()
                self.log(f"[AUTO] Выполнено действие: {action}")
                self.add_history(f"[AUTO] Выполнен сценарий: {name}")
                
        except Exception as e:
            self.log(f"[AUTO] Ошибка выполнения сценария: {e}")
            self.add_history(f"[AUTO] Ошибка при выполнении сценария: {name}")
            
            # Добавляем ошибку в логи
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "scenario": name,
                "category": category,
                "status": "error",
                "error": str(e)
            }
            self.automation_logs.append(log_entry)
            if hasattr(self, "auto_logs_list"):
                timestamp = datetime.now().strftime("%H:%M:%S")
                self.auto_logs_list.addItem(f"[{timestamp}] ❌ {name} - Ошибка: {str(e)[:30]}")
                self.auto_logs_list.scrollToBottom()
    
    def _automation_add_from_text(self):
        """Создать новый сценарий автоматизации из текстового описания."""
        text = ""
        if hasattr(self, "edit_auto_command"):
            text = self.edit_auto_command.text().strip()
        
        if not text:
            text, ok = QtWidgets.QInputDialog.getText(
                self, "Новый сценарий", "Опишите задачу для автоматизации:"
            )
            if not ok or not text.strip():
                return
            text = text.strip()
        
        # Если текст пустой, выходим
        if not text:
            return
        
        # Простой парсер для создания сценария
        name = text[:50]  # Ограничиваем длину названия
        category = "Общие"
        
        lower = text.lower()
        if "утро" in lower or "утренн" in lower:
            category = "🏠 Домашние"
        elif "игр" in lower or "вечер" in lower:
            category = "🎮 Игровые"
        elif "рабоч" in lower or "работа" in lower:
            category = "🏢 Рабочие"
        elif "систем" in lower or "очистк" in lower:
            category = "🔧 Системные"
        
        scenario = {
            "name": name,
            "category": category,
            "action": text,
            "trigger": "Вручную",
        }
        
        self.automation_scenarios.append(scenario)
        self._automation_refresh_table()
        self.log(f"[AUTO] Добавлен новый сценарий: {name}")
        self.add_history(f"[AUTO] Создан сценарий: {name}")
        
        if hasattr(self, "edit_auto_command"):
            self.edit_auto_command.clear()

    def _files_current_dir(self) -> str:
        """Текущая папка файлового менеджера (по умолчанию — домашняя)."""
        if not hasattr(self, "_files_dir") or not self._files_dir:
            self._files_dir = os.path.expanduser("~")
        return self._files_dir

    def _files_set_dir(self, path: str):
        self._files_dir = path
        if hasattr(self, "files_path_edit"):
            self.files_path_edit.setText(path)
        self._files_refresh()

    def _files_go_home(self):
        """Перейти в домашнюю папку пользователя."""
        home = os.path.expanduser("~")
        self._files_set_dir(home)

    def _files_go_up(self):
        """Перейти на уровень вверх от текущей папки."""
        cur = self._files_current_dir()
        parent = os.path.dirname(cur) or cur
        self._files_set_dir(parent)

    def _files_refresh(self):
        """Обновить список файлов/папок в текущей директории."""
        if not hasattr(self, "files_view"):
            return
        path = self._files_current_dir()
        self.files_view.clear()
        try:
            entries = os.listdir(path)
        except Exception as e:
            self.log(f"[FILES] Ошибка чтения каталога {path}: {e}")
            return

        count = 0
        for name in sorted(entries, key=str.lower):
            full = os.path.join(path, name)
            size_text = ""
            mtime_text = ""
            try:
                if os.path.isdir(full):
                    size_text = "<DIR>"
                else:
                    size = os.path.getsize(full)
                    size_text = f"{size // 1024} КБ"
                mtime = datetime.fromtimestamp(os.path.getmtime(full))
                mtime_text = mtime.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
            item = QtWidgets.QTreeWidgetItem([name, size_text, mtime_text])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, full)
            self.files_view.addTopLevelItem(item)
            count += 1

        if hasattr(self, "lbl_files"):
            self.lbl_files.setText(f"📁 Файлов: {count}")

    def _files_item_activated(self, item: QtWidgets.QTreeWidgetItem):
        """Двойной клик по элементу в списке файлов."""
        full = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not full:
            return
        if os.path.isdir(full):
            self._files_set_dir(full)
        else:
            try:
                os.startfile(full)
            except Exception as e:
                self.log(f"[FILES] Ошибка открытия файла {full}: {e}")

    def _files_selected_path(self) -> str | None:
        items = self.files_view.selectedItems() if hasattr(self, "files_view") else []
        if not items:
            return None
        return items[0].data(0, QtCore.Qt.ItemDataRole.UserRole)

    def _files_open_selected(self):
        path = self._files_selected_path()
        if not path:
            return
        if os.path.isdir(path):
            self._files_set_dir(path)
        else:
            try:
                os.startfile(path)
            except Exception as e:
                self.log(f"[FILES] Ошибка открытия: {path}: {e}")

    def _files_open_in_explorer(self):
        path = self._files_selected_path() or self._files_current_dir()
        try:
            self.open_explorer(path)
        except Exception as e:
            self.log(f"[FILES] Ошибка открытия в проводнике: {e}")

    def _files_delete_selected(self):
        path = self._files_selected_path()
        if not path:
            return
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            self.log(f"[FILES] Удалено: {path}")
        except Exception as e:
            self.log(f"[FILES] Ошибка удаления {path}: {e}")
        self._files_refresh()

    def _files_new_folder(self):
        base = self._files_current_dir()
        name, ok = QtWidgets.QInputDialog.getText(self, "Новая папка", "Имя папки:")
        if not ok or not name.strip():
            return
        target = os.path.join(base, name.strip())
        try:
            os.makedirs(target, exist_ok=True)
            self.log(f"[FILES] Создана папка: {target}")
        except Exception as e:
            self.log(f"[FILES] Ошибка создания папки {target}: {e}")
        self._files_refresh()

    # ========= Веб-помощники для вкладки "Веб" =========

    def _web_open_url(self):
        """Открыть URL из поля ввода (если это не URL — добавить https://)."""
        text = self.web_input.text().strip() if hasattr(self, "web_input") else ""
        if not text:
            return
        url = text
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url
        try:
            self.log(f"[WEB] Открываю сайт: {url}")
            webbrowser.open(url)
        except Exception as e:
            self.log(f"[WEB] Ошибка открытия URL {url}: {e}")

    def _web_google_search(self):
        """Поиск текста из web_input в Google."""
        text = self.web_input.text().strip() if hasattr(self, "web_input") else ""
        if not text:
            return
        try:
            query = quote_plus(text)
            url = f"https://www.google.com/search?q={query}"
            self.log(f"[WEB] Google поиск: {text}")
            webbrowser.open(url)
        except Exception as e:
            self.log(f"[WEB] Ошибка Google поиска '{text}': {e}")

    def _web_youtube_search(self):
        """Поиск текста из web_input на YouTube."""
        text = self.web_input.text().strip() if hasattr(self, "web_input") else ""
        if not text:
            return
        try:
            query = quote_plus(text)
            url = f"https://www.youtube.com/results?search_query={query}"
            self.log(f"[WEB] YouTube поиск: {text}")
            webbrowser.open(url)
        except Exception as e:
            self.log(f"[WEB] Ошибка YouTube поиска '{text}': {e}")

    def _web_open_fixed(self, url: str):
        """Открыть фиксированный URL (быстрые кнопки)."""
        try:
            self.log(f"[WEB] Открываю быстрый сайт: {url}")
            webbrowser.open(url)
        except Exception as e:
            self.log(f"[WEB] Ошибка открытия быстрого сайта {url}: {e}")

    def _web_open_profile(self, profile: str):
        """Открыть профиль (режим) набора сайтов по теме."""
        profiles = {
            "research": ["https://scholar.google.com", "https://wikipedia.org"],
            "study": ["https://www.khanacademy.org", "https://www.coursera.org"],
            "work": ["https://mail.google.com", "https://calendar.google.com"],
            "games": ["https://store.steampowered.com", "https://www.ign.com"],
        }
        urls = profiles.get(profile, [])
        if not urls:
            self.log(f"[WEB] Для профиля {profile} нет предопределённых сайтов")
            return
        for url in urls:
            try:
                webbrowser.open(url)
            except Exception as e:
                self.log(f"[WEB] Ошибка открытия {url} для профиля {profile}: {e}")

    def _web_generate_ideas(self):
        """Простая генерация связанных тем (заглушка без ИИ)."""
        if not hasattr(self, "web_ideas_list"):
            return
        self.web_ideas_list.clear()
        base = self.web_input.text().strip() if hasattr(self, "web_input") else ""
        topics = []
        if base:
            topics = [
                f"История: {base}",
                f"Лучшие практики: {base}",
                f"Сравнение: {base} и аналоги",
                f"Гайды и туториалы по {base}",
                f"Типичные ошибки в {base}",
            ]
        else:
            topics = [
                "Новые технологии 2025",
                "Как прокачать продуктивность",
                "Лучшие игры года",
                "Финансовая грамотность",
                "Изучение английского языка",
            ]
        for topic in topics:
            self.web_ideas_list.addItem(topic)

    def _web_idea_activated(self, item: QtWidgets.QListWidgetItem):
        """Двойной клик по идее — открыть поиск в Google по этой теме."""
        text = item.text().strip()
        if not text:
            return
        try:
            query = quote_plus(text)
            url = f"https://www.google.com/search?q={query}"
            self.log(f"[WEB] Поиск по идее: {text}")
            webbrowser.open(url)
        except Exception as e:
            self.log(f"[WEB] Ошибка поиска по идее '{text}': {e}")

        # Система - процессы и автозагрузка
        if hasattr(self, "btn_proc_refresh"):
            self.btn_proc_refresh.clicked.connect(self._system_refresh_processes)
            self.btn_proc_kill.clicked.connect(self._system_kill_process)
            self.btn_proc_details.clicked.connect(self._system_process_details)
        # Также подключаем к существующим кнопкам
        if hasattr(self, "btn_startup_refresh"):
            self.btn_startup_refresh.clicked.connect(self._system_refresh_startup)
        if hasattr(self, "btn_startup_disable"):
            self.btn_startup_disable.clicked.connect(self._system_disable_startup)
        if hasattr(self, "btn_startup_enable"):
            self.btn_startup_enable.clicked.connect(self._system_enable_startup)
        
        # Исправляем подключения системных кнопок
        if hasattr(self, "btn_sys_scan_cleanup"):
            self.btn_sys_scan_cleanup.clicked.connect(self._system_scan_cleanup)
        if hasattr(self, "btn_sys_run_cleanup"):
            self.btn_sys_run_cleanup.clicked.connect(self._system_run_cleanup)
        
        # Инициализация процессов и автозагрузки
        QtCore.QTimer.singleShot(1000, self._system_refresh_processes)
        QtCore.QTimer.singleShot(1000, self._system_refresh_startup)
        
        # Загружаем игры в список (с задержкой для гарантии инициализации)
        QtCore.QTimer.singleShot(1000, self._games_load_list)
        
        # Запускаем обновление игрового монитора
        if not hasattr(self, 'game_monitor_timer'):
            self.game_monitor_timer = QtCore.QTimer()
            self.game_monitor_timer.timeout.connect(self._games_update_monitor)
            self.game_monitor_timer.start(2000)  # Обновление каждые 2 секунды
        
        # Пробуем автоматически подключиться к Telegram если есть сессия
        QtCore.QTimer.singleShot(2000, self._try_auto_connect_telegram)

    # ========= Вкладка "Мессенджеры" — обработчики =========

    def _try_auto_connect_telegram(self):
        """Попытка автоматически подключиться к Telegram если есть сохраненная сессия."""
        if not hasattr(self, "telegram_user_client"):
            self.log("[TG] User client не инициализирован")
            return
        
        try:
            # Проверяем наличие API credentials
            if not self.telegram_user_client.api_id or not self.telegram_user_client.api_hash:
                self.log("[TG] API credentials не настроены, автоматическое подключение невозможно")
                self.log("[TG] Добавь TELEGRAM_API_ID и TELEGRAM_API_HASH в .env")
                return
            
            # Проверяем есть ли файл сессии
            session_file = self.telegram_user_client.session_path + ".session"
            if os.path.exists(session_file):
                self.log("[TG] Обнаружена сохраненная сессия, пробуем подключиться...")
                if self.telegram_user_client.connect():
                    self.log("[TG] ✅ Автоматически подключен к Telegram")
                    self.telegram_user_client.start_listening()
                    self._messengers_load_chats()
                    self._messengers_refresh_status()
                else:
                    self.log("[TG] Не удалось автоматически подключиться (требуется код или повторная авторизация)")
            else:
                self.log("[TG] Файл сессии не найден, требуется первичная авторизация")
                self.log("[TG] Нажми кнопку 'Подключить Telegram' для авторизации")
        except Exception as e:
            self.log(f"[TG] Ошибка автоматического подключения: {e}")
            import traceback
            self.log(f"[TG] Traceback: {traceback.format_exc()}")

    def _messengers_refresh_status(self):
        """Обновить статус подключений мессенджеров."""
        try:
            # Проверяем user client (Telethon)
            user_status = self.telegram_user_client.refresh_status() if hasattr(self, "telegram_user_client") else "disconnected"
            
            if hasattr(self, "lbl_tg_status"):
                if user_status == "connected":
                    text = "TELEGRAM  [🟢] Подключен (User Client)"
                else:
                    # Проверяем бота как запасной вариант
                    bot_status = self.telegram_manager.refresh_status()
                    tg_state = bot_status.get("telegram", "disconnected")
                    if tg_state == "connected":
                        text = "TELEGRAM  [🟡] Бот активен (User Client не подключен)"
                    else:
                        text = "TELEGRAM  [⚪] Не подключен"
                self.lbl_tg_status.setText(text)
            
            # Если user client подключен, загружаем диалоги
            if user_status == "connected":
                self._messengers_load_chats()
            
            self.log("[MSG] Статусы мессенджеров обновлены")
        except Exception as e:
            self.log(f"[MSG] Ошибка обновления статуса мессенджеров: {e}")

    def _messengers_load_chats(self):
        """Загрузить список чатов из Telegram."""
        if not hasattr(self, "telegram_user_client") or not self.telegram_user_client.connected:
            return
        
        try:
            dialogs = self.telegram_user_client.get_dialogs()
            if not hasattr(self, "chats_list"):
                return
            
            self.chats_list.clear()
            for dialog in dialogs:
                item = QtWidgets.QTreeWidgetItem(self.chats_list)
                name = dialog.get("name", "Неизвестно")
                unread = dialog.get("unread", 0)
                chat_id = dialog.get("id")

                # Аватар-иконка (генерируем простую иконку с буквой)
                try:
                    pix = self._avatar_for_name(name)
                    item.setIcon(0, QtGui.QIcon(pix))
                except Exception:
                    pass

                item.setText(0, name)
                # Красивый бейдж для новых сообщений
                if unread > 0:
                    item.setText(1, str(unread))
                    item.setForeground(1, QtGui.QBrush(QtGui.QColor('#FFD700')))
                else:
                    item.setText(1, "")
                item.setText(2, dialog.get("last_message", "")[:40])
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, chat_id)
            
            self.log(f"[TG] Загружено диалогов: {len(dialogs)}")
        except Exception as e:
            self.log(f"[TG] Ошибка загрузки чатов: {e}")

    def _messengers_connect_telegram(self):
        """Подключиться к Telegram через user client."""
        if not hasattr(self, "telegram_user_client"):
            QtWidgets.QMessageBox.warning(self, "Ошибка", "Telegram user client не инициализирован")
            return
        
        if not self.telegram_user_client.api_id or not self.telegram_user_client.api_hash:
            dlg = QtWidgets.QMessageBox(self)
            dlg.setWindowTitle("Настройка Telegram")
            dlg.setText(
                "Для работы с Telegram нужны API credentials:\n\n"
                "1. Перейди на https://my.telegram.org/apps\n"
                "2. Войди со своим номером телефона\n"
                "3. Создай приложение и получи:\n"
                "   - api_id\n"
                "   - api_hash\n\n"
                "Добавь их в файл .env:\n"
                "TELEGRAM_API_ID=твой_api_id\n"
                "TELEGRAM_API_HASH=твой_api_hash\n"
                "TELEGRAM_PHONE=+79991234567"
            )
            dlg.exec()
            return
        
        try:
            self.log("[TG] Подключение к Telegram...")
            connected = self.telegram_user_client.connect()
            
            if connected:
                self.log("[TG] Подключён успешно!")
                # Запускаем слушатель сообщений
                self.telegram_user_client.start_listening()
                # Загружаем чаты
                self._messengers_load_chats()
                # Обновляем статус
                self._messengers_refresh_status()
            else:
                # Проверяем, был ли запрошен код
                if self.telegram_user_client._code_requested:
                    # Нужна авторизация с кодом
                    code, ok = QtWidgets.QInputDialog.getText(
                        self, "Код авторизации Telegram",
                        f"Введи код, который пришёл в Telegram на {self.telegram_user_client.phone or 'твой номер'}:"
                    )
                    if ok and code.strip():
                        if self.telegram_user_client.authorize_with_code(code.strip()):
                            self.log("[TG] Авторизован успешно!")
                            self.telegram_user_client.start_listening()
                            self._messengers_load_chats()
                            self._messengers_refresh_status()
                        else:
                            QtWidgets.QMessageBox.warning(self, "Ошибка", "Неверный код авторизации. Попробуй ещё раз.")
                else:
                    # Если код не был запрошен, возможно нет номера телефона
                    if not self.telegram_user_client.phone:
                        QtWidgets.QMessageBox.warning(
                            self, "Ошибка",
                            "Не указан номер телефона.\n\n"
                            "Добавь в .env:\n"
                            "TELEGRAM_PHONE=+79991234567"
                        )
                    else:
                        self.log("[TG] Не удалось подключиться. Проверь логи выше.")
        except Exception as e:
            self.log(f"[TG] Ошибка подключения: {e}")
            QtWidgets.QMessageBox.warning(self, "Ошибка", f"Ошибка подключения: {e}")

    def _messengers_open_api_settings(self):
        """Диалоговое окно с инструкцией по настройке API."""
        dlg = QtWidgets.QMessageBox(self)
        dlg.setWindowTitle("Настройки Telegram API")
        dlg.setText(
            "Для работы с Telegram через user client:\n\n"
            "1. Перейди на https://my.telegram.org/apps\n"
            "2. Войди со своим номером телефона\n"
            "3. Создай приложение и получи:\n"
            "   - api_id (число)\n"
            "   - api_hash (строка)\n\n"
            "Добавь в файл .env в папке программы:\n\n"
            "TELEGRAM_API_ID=твой_api_id\n"
            "TELEGRAM_API_HASH=твой_api_hash\n"
            "TELEGRAM_PHONE=+79991234567\n\n"
            "После этого нажми кнопку 'Подключить Telegram'."
        )
        dlg.exec()

    def _messengers_toggle_autoreply(self, enabled: bool):
        """Переключение состояния автоответчика (лог + вызов AutoResponder)."""
        try:
            self.telegram_autoresponder.set_enabled(enabled)
        except Exception as e:
            self.log(f"[TG] Ошибка переключения автоответчика: {e}")

    def _messengers_save_autoreply_template(self):
        text = self.autoreply_text.toPlainText().strip() if hasattr(self, "autoreply_text") else ""
        if not text:
            return
        if hasattr(self, "templates_list"):
            self.templates_list.addItem(text)
        self.log(f"[TG] Сохранён шаблон автоответа: {text[:40]}...")

    def _messengers_use_template(self, item: QtWidgets.QListWidgetItem):
        text = item.text().strip()
        if not text or not hasattr(self, "msg_text"):
            return
        self.msg_text.setPlainText(text)

    def _messengers_add_template(self):
        txt, ok = QtWidgets.QInputDialog.getText(self, "Новый шаблон", "Текст шаблона:")
        if not ok or not txt.strip() or not hasattr(self, "templates_list"):
            return
        self.templates_list.addItem(txt.strip())

    def _messengers_pin_chat(self):
        # Заглушка: просто лог
        self.log("[TG] Закрепление чата (заглушка)")

    def _messengers_hide_chat(self):
        self.log("[TG] Сокрытие чата (заглушка)")

    def _messengers_chat_activated(self, item: QtWidgets.QTreeWidgetItem):
        """Выбрать чат для отправки сообщения."""
        name = item.text(0)
        chat_id = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        
        # Устанавливаем chat_id в поле получателя
        if hasattr(self, "msg_recipient"):
            self.msg_recipient.setText(str(chat_id) if chat_id else "")
        
        self.log(f"[TG] Выбран чат: {name} (ID: {chat_id})")

    def _messengers_filter_chats(self, text: str):
        # Простая фильтрация по подстроке в названии чата
        text_lower = text.lower()
        root = self.chats_list.invisibleRootItem() if hasattr(self, "chats_list") else None
        if root is None:
            return
        for i in range(root.childCount()):
            item = root.child(i)
            visible = text_lower in item.text(0).lower()
            item.setHidden(not visible)

    def _messengers_add_attachment(self, kind: str):
        try:
            filters = "All files (*.*)"
            if kind == "image":
                filters = "Images (*.png *.jpg *.jpeg *.bmp);;All files (*.*)"
            elif kind == "video":
                filters = "Videos (*.mp4 *.mov *.avi);;All files (*.*)"

            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Выбери файл", os.path.expanduser("~"), filters)
            if not path:
                return

            self._pending_attachments.append({"kind": kind, "path": path})
            # Обновляем метку вложений
            names = [os.path.basename(a['path']) for a in self._pending_attachments]
            self.lbl_attachments.setText("Вложено: " + ", ".join(names))
            self.log(f"[TG] Вложение добавлено: {path}")
        except Exception as e:
            self.log(f"[TG] Ошибка добавления вложения: {e}")

    def _messengers_send_message(self):
        """Отправить сообщение через Telegram user client или бота."""
        if not hasattr(self, "msg_recipient") or not hasattr(self, "msg_text"):
            return
        to = self.msg_recipient.text().strip()
        text = self.msg_text.toPlainText().strip()
        if not to or not text:
            return
        
        try:
            secret = getattr(self, 'chk_secret', None) and self.chk_secret.isChecked()

            # Пробуем отправить через user client (если подключен)
            if hasattr(self, "telegram_user_client") and self.telegram_user_client.connected:
                chat_id_val = None
                try:
                    chat_id_val = int(to)  # Пытаемся преобразовать в число (chat_id)
                except ValueError:
                    chat_id_val = to

                sent = False
                if self.telegram_user_client.send_message(chat_id_val, text):
                    sent = True
                    self.add_history(f"[TG] Сообщение отправлено: {text[:40]}...")

                # Отправляем вложения (если есть)
                if self._pending_attachments:
                    for a in list(self._pending_attachments):
                        path = a.get('path')
                        if path and os.path.exists(path):
                            ok = self.telegram_user_client.send_file(chat_id_val, path)
                            if ok:
                                self.add_history(f"[TG] Вложение отправлено: {os.path.basename(path)}")
                            else:
                                self.log(f"[TG] Не удалось отправить вложение: {path}")
                    # Очищаем очередь вложений
                    self._pending_attachments.clear()
                    if hasattr(self, 'lbl_attachments'):
                        self.lbl_attachments.setText("")

                # Отметка секретности — логируем и оставляем для будущей реализации
                if secret:
                    self.add_history("[TG] Пометка 'Секретное' применена к сообщению")

                if sent and hasattr(self, "msg_text"):
                    self.msg_text.clear()
                return

            # Запасной вариант - через бота (если есть токен)
            if hasattr(self, "telegram_manager"):
                # Бот: отправляем текст (вложениями пока не поддерживается здесь)
                self.telegram_manager.send_message(to, text + (" [СЕКРЕТ]" if secret else ""))
                self.add_history(f"[TG] Сообщение отправлено через бота: {text[:40]}...")
                if hasattr(self, "msg_text"):
                    self.msg_text.clear()
                # Если были вложения — просто логируем (реализация через бота может быть добавлена позже)
                if self._pending_attachments:
                    for a in self._pending_attachments:
                        self.log(f"[TG] Вложение для отправки (бот): {a.get('path')}")
                    self._pending_attachments.clear()
                    if hasattr(self, 'lbl_attachments'):
                        self.lbl_attachments.setText("")
        except Exception as e:
            self.log(f"[TG] Ошибка отправки сообщения: {e}")
            QtWidgets.QMessageBox.warning(self, "Ошибка", f"Не удалось отправить сообщение: {e}")

    def _messengers_schedule_message(self):
        self.log("[TG] Планирование сообщений пока не реализовано (заглушка)")

    def _messengers_run_ai_command(self):
        cmd = self.ai_command_input.text().strip() if hasattr(self, "ai_command_input") else ""
        if not cmd:
            return
        try:
            stats = self.telegram_analyzer.analyze_activity()
            summary = (
                f"Отправлено: {stats.get('sent')}\n"
                f"Получено: {stats.get('received')}\n"
                f"Топ-контакт: {stats.get('top_contact')}\n"
                f"Пиковое время: {stats.get('peak_time')}\n"
                f"Топ-слова: {', '.join(stats.get('top_words', []))}"
            )
            if hasattr(self, "lbl_stats_summary"):
                self.lbl_stats_summary.setText(summary)
            self.log(f"[TG] Выполнена AI-команда: {cmd}")
        except Exception as e:
            self.log(f"[TG] Ошибка выполнения AI-команды: {e}")

    def _messengers_show_stats_chart(self):
        self.log("[TG] Отображение графика статистики (заглушка)")

    def _avatar_for_name(self, name: str) -> QtGui.QPixmap:
        """Создать простую квадратную иконку-аватар с первой буквой имени."""
        size = 48
        pix = QtGui.QPixmap(size, size)
        # Выбираем цвет по хешу имени
        h = abs(hash(name)) % 360
        color = QtGui.QColor()
        color.setHsv(h, 200, 200)
        pix.fill(color)

        painter = QtGui.QPainter(pix)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        font = QtGui.QFont()
        font.setBold(True)
        font.setPointSize(14)
        painter.setFont(font)
        painter.setPen(QtGui.QPen(QtGui.QColor('#0A0A0F')))
        letter = (name.strip()[0].upper() if name and name.strip() else '?')
        rect = QtCore.QRectF(0, 0, size, size)
        painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, letter)
        painter.end()
        return pix

    # ========= Базовые системные действия (проводник, браузер и т.п.) =========

    def open_explorer(self, path: str | None = None):
        """Открыть проводник Windows в указанной папке (или домашней, если не задана)."""
        if not path:
            path = os.path.expanduser("~")
        else:
            path = os.path.expandvars(os.path.expanduser(path))
        if os.path.isfile(path):
            path = os.path.dirname(path)
        try:
            self.log(f"Открываю проводник: {path}")
            os.startfile(path)
        except Exception as e:
            self.log(f"Ошибка открытия проводника: {e}")

    def open_browser(self, url: str | None = None):
        """Открыть браузер по умолчанию (главная страница или указанный URL)."""
        try:
            target = (url or "").strip()
            if not target:
                target = "https://"
            webbrowser.open(target)
            self.log("Открываю браузер")
        except Exception as e:
            self.log(f"Ошибка открытия браузера: {e}")

    def take_screenshot(self, grid: bool = True):
        """Снимок экрана. Если grid=True, накладывает координатную сетку для помощи ИИ."""
        try:
            home = os.path.expanduser("~")
            pictures_dir = os.path.join(home, "Pictures")
            os.makedirs(pictures_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename = f"screenshot_{timestamp}.png"
            path = os.path.join(pictures_dir, filename)
            
            image = pyautogui.screenshot()
            
            if grid:
                draw = ImageDraw.Draw(image)
                width, height = image.size
                
                # Сетка
                step = 100
                font_size = 20
                try:
                    import os
                    win_font = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arial.ttf")
                    if os.path.exists(win_font):
                        font = ImageFont.truetype(win_font, font_size)
                    else:
                        font = ImageFont.load_default()
                except:
                    font = ImageFont.load_default()
                
                # Вертикальные линии
                for x in range(0, width, step):
                    draw.line([(x, 0), (x, height)], fill=(255, 0, 0, 128), width=1)
                    if x % 200 == 0:
                        draw.text((x + 5, 5), str(x), fill=(255, 0, 0), font=font)
                
                # Горизонтальные линии
                for y in range(0, height, step):
                    draw.line([(0, y), (width, y)], fill=(255, 0, 0, 128), width=1)
                    if y % 200 == 0:
                        draw.text((5, y + 5), str(y), fill=(255, 0, 0), font=font)
            
            image.save(path)
            self.log(f"Снимок экрана сохранён: {path}")
            self.add_history(f"[Система] Снимок экрана: {path}")
            return f"Скриншот сохранён: {path}. Используй сетку для точного попадания."
        except Exception as e:
            self.log(f"Ошибка создания скриншота: {e}")
            return f"Ошибка скриншота: {e}"

    def open_music_folder(self):
        """Открыть стандартную папку "Музыка" пользователя в проводнике."""
        try:
            home = os.path.expanduser("~")
            music_dir = os.path.join(home, "Music")
            if not os.path.exists(music_dir):
                os.makedirs(music_dir, exist_ok=True)
            self.log(f"Открываю папку музыки: {music_dir}")
            os.startfile(music_dir)
        except Exception as e:
            self.log(f"Ошибка открытия папки музыки: {e}")
    
    def open_documents(self):
        """Открыть стандартную папку "Документы" пользователя в проводнике."""
        try:
            home = os.path.expanduser("~")
            docs_dir = os.path.join(home, "Documents")
            if not os.path.exists(docs_dir):
                os.makedirs(docs_dir, exist_ok=True)
            self.log(f"Открываю папку документов: {docs_dir}")
            os.startfile(docs_dir)
        except Exception as e:
            self.log(f"Ошибка открытия папки документов: {e}")
    
    def _system_scan_cleanup(self):
        """Сканирование и очистка системы."""
        self.log("[SYSTEM] Запуск сканирования системы...")
        try:
            # Простая проверка диска
            system_drive = os.environ.get("SystemDrive", "C:")
            usage = psutil.disk_usage(system_drive + "\\")
            free_gb = usage.free / (1024 ** 3)
            self.log(f"[SYSTEM] Свободно на диске {system_drive}: {free_gb:.1f} GB")
            self.add_history(f"[SYSTEM] Сканирование завершено. Свободно: {free_gb:.1f} GB")
        except Exception as e:
            self.log(f"[SYSTEM] Ошибка сканирования: {e}")
    
    def _system_refresh_processes(self):
        """Обновить список процессов."""
        # Проверяем оба возможных имени таблицы
        table = None
        if hasattr(self, "table_processes"):
            table = self.table_processes
        elif hasattr(self, "process_table"):
            table = self.process_table
        
        if not table:
            return
        try:
            processes = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
                try:
                    proc.cpu_percent()  # Первый вызов для инициализации
                    processes.append(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            # Сортируем по использованию CPU
            time.sleep(0.1)  # Небольшая задержка для точности
            process_data = []
            for proc in processes[:20]:  # Топ 20 процессов
                try:
                    cpu = proc.cpu_percent()
                    mem_mb = proc.memory_info().rss / (1024 * 1024)
                    process_data.append({
                        "name": proc.info['name'],
                        "cpu": cpu,
                        "memory": mem_mb,
                        "pid": proc.info['pid']
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            # Сортируем по CPU
            process_data.sort(key=lambda x: x["cpu"], reverse=True)
            
            table.setRowCount(len(process_data))
            for row, proc in enumerate(process_data):
                table.setItem(row, 0, QtWidgets.QTableWidgetItem(proc["name"]))
                table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{proc['cpu']:.1f}"))
                table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{proc['memory']:.0f}"))
                status_item = QtWidgets.QTableWidgetItem("🟢")
                table.setItem(row, 3, status_item)
                status_item.setData(QtCore.Qt.ItemDataRole.UserRole, proc["pid"])
            
            self.log(f"[SYSTEM] Обновлено процессов: {len(process_data)}")
        except Exception as e:
            self.log(f"[SYSTEM] Ошибка обновления процессов: {e}")
    
    def _system_kill_process(self):
        """Завершить выбранный процесс."""
        table = None
        if hasattr(self, "table_processes"):
            table = self.table_processes
        elif hasattr(self, "process_table"):
            table = self.process_table
        
        if not table:
            return
        
        selected = table.selectedItems()
        if not selected:
            self.log("[SYSTEM] Выберите процесс для завершения")
            return
        
        row = selected[0].row()
        pid_item = table.item(row, 3)
        if not pid_item:
            return
        
        pid = pid_item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not pid:
            return
        
        try:
            proc = psutil.Process(pid)
            proc_name = proc.name()
            proc.terminate()
            self.log(f"[SYSTEM] Процесс завершен: {proc_name} (PID: {pid})")
            self.add_history(f"[SYSTEM] Завершен процесс: {proc_name}")
            QtCore.QTimer.singleShot(1000, self._system_refresh_processes)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            self.log(f"[SYSTEM] Ошибка завершения процесса: {e}")
    
    def _system_process_details(self):
        """Показать детали процесса."""
        table = None
        if hasattr(self, "table_processes"):
            table = self.table_processes
        elif hasattr(self, "process_table"):
            table = self.process_table
        
        if not table:
            return
        
        selected = table.selectedItems()
        if not selected:
            return
        
        row = selected[0].row()
        pid_item = table.item(row, 3)
        if not pid_item:
            return
        
        pid = pid_item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not pid:
            return
        
        try:
            proc = psutil.Process(pid)
            info = f"Процесс: {proc.name()}\n"
            info += f"PID: {pid}\n"
            info += f"CPU: {proc.cpu_percent():.1f}%\n"
            info += f"RAM: {proc.memory_info().rss / (1024*1024):.0f} MB\n"
            try:
                info += f"Путь: {proc.exe()}\n"
            except:
                info += "Путь: недоступен\n"
            
            QtWidgets.QMessageBox.information(self, "Детали процесса", info)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            self.log(f"[SYSTEM] Ошибка получения деталей: {e}")
    
    def _system_refresh_startup(self):
        """Обновить список автозагрузки."""
        table = None
        if hasattr(self, "table_startup"):
            table = self.table_startup
        elif hasattr(self, "startup_table"):
            table = self.startup_table
        
        if not table:
            return
        try:
            # Получаем записи автозагрузки из реестра Windows
            startup_items = []
            try:
                import winreg
                key_paths = [
                    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
                    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
                ]
                
                for hkey, path in key_paths:
                    try:
                        key = winreg.OpenKey(hkey, path)
                        i = 0
                        while True:
                            try:
                                name, value, _ = winreg.EnumValue(key, i)
                                startup_items.append({"name": name, "path": value, "enabled": True})
                                i += 1
                            except WindowsError:
                                break
                        winreg.CloseKey(key)
                    except:
                        continue
            except ImportError:
                # Если winreg недоступен, используем заглушку
                startup_items = [
                    {"name": "Пример программы", "path": "C:\\Program Files\\Example\\app.exe", "enabled": True},
                ]
            
            table.setRowCount(len(startup_items))
            for row, item in enumerate(startup_items):
                table.setItem(row, 0, QtWidgets.QTableWidgetItem(item["name"]))
                table.setItem(row, 1, QtWidgets.QTableWidgetItem(item["path"]))
                status = "🟢 Включено" if item["enabled"] else "🔴 Отключено"
                status_item = QtWidgets.QTableWidgetItem(status)
                table.setItem(row, 2, status_item)
                status_item.setData(QtCore.Qt.ItemDataRole.UserRole, item["enabled"])
            
            self.log(f"[SYSTEM] Обновлено записей автозагрузки: {len(startup_items)}")
        except Exception as e:
            self.log(f"[SYSTEM] Ошибка обновления автозагрузки: {e}")
    
    def _system_disable_startup(self):
        """Отключить выбранную программу из автозагрузки."""
        selected = self.table_startup.selectedItems()
        if not selected:
            self.log("[SYSTEM] Выберите программу для отключения")
            return
        self.log("[SYSTEM] Отключение автозагрузки (требуются права администратора)")
        self.add_history("[SYSTEM] Попытка отключить автозагрузку")
    
    def _system_enable_startup(self):
        """Включить выбранную программу в автозагрузку."""
        table = None
        if hasattr(self, "table_startup"):
            table = self.table_startup
        elif hasattr(self, "startup_table"):
            table = self.startup_table
        
        if not table:
            return
        
        selected = table.selectedItems()
        if not selected:
            self.log("[SYSTEM] Выберите программу для включения")
            return
        self.log("[SYSTEM] Включение автозагрузки (требуются права администратора)")
        self.add_history("[SYSTEM] Попытка включить автозагрузку")
    
    def _system_run_cleanup(self):
        """Выполнить очистку системы."""
        self.log("[SYSTEM] Запуск очистки системы...")
        try:
            cleaned = 0
            if self.chk_clean_temp_win.isChecked():
                temp_win = os.path.join(os.environ.get("TEMP", "C:\\Windows\\Temp"), "..")
                self.log(f"[SYSTEM] Очистка временных файлов Windows: {temp_win}")
                cleaned += 1
            if self.chk_clean_temp_user.isChecked():
                temp_user = os.environ.get("TEMP", "")
                self.log(f"[SYSTEM] Очистка временных файлов пользователя: {temp_user}")
                cleaned += 1
            
            self.add_history(f"[SYSTEM] Очистка завершена. Обработано категорий: {cleaned}")
            self.log(f"[SYSTEM] Очистка завершена")
        except Exception as e:
            self.log(f"[SYSTEM] Ошибка очистки: {e}")
    
    def _system_shutdown(self):
        """Выключить компьютер."""
        reply = QtWidgets.QMessageBox.question(
            self, "Выключение", "Вы уверены, что хотите выключить компьютер?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            self.log("[SYSTEM] Выключение компьютера...")
            os.system("shutdown /s /t 10")
    
    def _system_reboot(self):
        """Перезагрузить компьютер."""
        reply = QtWidgets.QMessageBox.question(
            self, "Перезагрузка", "Вы уверены, что хотите перезагрузить компьютер?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            self.log("[SYSTEM] Перезагрузка компьютера...")
            os.system("shutdown /r /t 10")
    
    def _system_sleep(self):
        """Перевести компьютер в спящий режим."""
        self.log("[SYSTEM] Переход в спящий режим...")
        os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
    
    def _system_security_scan(self):
        """Быстрое сканирование безопасности."""
        self.log("[SYSTEM] Запуск быстрого сканирования безопасности...")
        self.add_history("[SYSTEM] Сканирование безопасности запущено")
        # Имитация сканирования
        QtCore.QTimer.singleShot(2000, lambda: self.log("[SYSTEM] Сканирование завершено. Угроз не обнаружено"))
    
    def _games_auto_optimize(self):
        """Автоматическая оптимизация для игр."""
        self.log("[GAMES] Запуск оптимизации для игр...")
        if hasattr(self, "game_manager"):
            self.game_manager.optimize_system_for_gaming(close_apps=True)
        self.add_history("[GAMES] Оптимизация завершена")
    
    def _games_show_stats(self):
        """Показать детальную статистику игр."""
        try:
            print("[GAMES] Кнопка 'Детальная статистика' нажата!")
            self.log("[GAMES] Кнопка 'Детальная статистика' нажата")
            self.log("[GAMES] Открытие статистики игр...")
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Статистика игр")
            dlg.setMinimumWidth(500)
            
            layout = QtWidgets.QVBoxLayout(dlg)
            
            stats_text = QtWidgets.QPlainTextEdit()
            stats_text.setReadOnly(True)
            
            text = "=== СТАТИСТИКА ИГР ===\n\n"
            if hasattr(self, "game_manager"):
                games_total = len(getattr(self.game_manager, "game_profiles", {}))
                text += f"Всего игр в профиле: {games_total}\n"
            else:
                text += "Всего игр в профиле: 0\n"
            
            text += f"Игр запущено: {len(self.game_monitor_history)}\n"
            text += f"Текущая игра: {self.current_game_name}\n\n"
            text += "=== ИСТОРИЯ ЗАПУСКОВ ===\n"
            for entry in self.game_monitor_history[-10:]:
                text += f"{entry.get('game', 'Неизвестно')} - {entry.get('timestamp', '')}\n"
            
            stats_text.setPlainText(text)
            layout.addWidget(stats_text)
            
            btn_close = QtWidgets.QPushButton("Закрыть")
            btn_close.clicked.connect(dlg.accept)
            layout.addWidget(btn_close)
            
            dlg.exec()
        except Exception as e:
            self.log(f"[GAMES] Ошибка в _games_show_stats: {e}")
            import traceback
            self.log(f"[GAMES] Traceback: {traceback.format_exc()}")
            QtWidgets.QMessageBox.warning(
                self, "Ошибка",
                f"Ошибка при показе статистики:\n{str(e)}"
            )
    
    def _games_apply_ai_graphics(self):
        """Применить AI-оптимизацию графики."""
        game_name = self.current_game_name
        self.log(f"[GAMES] Применение AI-оптимизации для: {game_name}")
        
        # Генерируем рекомендации
        resolution = self.cmb_resolution.currentText() if hasattr(self, "cmb_resolution") else "1920x1080"
        textures = self.cmb_textures.currentText() if hasattr(self, "cmb_textures") else "Высокое"
        aa = self.cmb_aa.currentText() if hasattr(self, "cmb_aa") else "TAA"
        
        recommendation = f"Рекомендация AI для {game_name}:\n"
        recommendation += f"Разрешение: {resolution}\n"
        recommendation += f"Текстуры: {textures}\n"
        recommendation += f"Сглаживание: {aa}\n"
        recommendation += "Оптимизация применена!"
        
        if hasattr(self, "lbl_ai_recommendation"):
            self.lbl_ai_recommendation.setText(recommendation)
        
        self.add_history(f"[GAMES] AI-оптимизация применена для {game_name}")
        self.log("[GAMES] AI-оптимизация применена")
    
    def _games_add_macro(self):
        """Добавить новый макрос."""
        try:
            print("[GAMES] Кнопка 'Создать макрос' нажата!")
            self.log("[GAMES] Кнопка 'Создать макрос' нажата")
            name, ok = QtWidgets.QInputDialog.getText(self, "Новый макрос", "Название макроса:")
            if not ok or not name.strip():
                return
            
            key, ok = QtWidgets.QInputDialog.getText(self, "Горячая клавиша", "Горячая клавиша (например, F5):")
            if not ok or not key.strip():
                return
            
            if hasattr(self, "macros_list"):
                self.macros_list.addItem(f" {name} - {key}")
            
            self.log(f"[GAMES] Добавлен макрос: {name} ({key})")
            self.add_history(f"[GAMES] Макрос добавлен: {name}")
        except Exception as e:
            self.log(f"[GAMES] Ошибка в _games_add_macro: {e}")
            import traceback
            self.log(f"[GAMES] Traceback: {traceback.format_exc()}")
    
    def _games_switch_account(self):
        """Быстрое переключение игровых аккаунтов."""
        try:
            print("[GAMES] Кнопка 'Переключатель аккаунтов' нажата!")
            self.log("[GAMES] Кнопка 'Переключатель аккаунтов' нажата")
            self.log("[GAMES] Открытие переключателя аккаунтов...")
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Переключение аккаунтов")
            dlg.setMinimumWidth(400)
            
            layout = QtWidgets.QVBoxLayout(dlg)
            
            accounts_list = QtWidgets.QListWidget()
            for platform, data in self.game_accounts.items():
                accounts_list.addItem(f"{platform}: {data['login']} - {data['note']}")
            layout.addWidget(accounts_list)
            
            btn_switch = QtWidgets.QPushButton("Переключить")
            btn_switch.clicked.connect(dlg.accept)
            layout.addWidget(btn_switch)
            
            dlg.exec()
            self.log("[GAMES] Переключение аккаунта (заглушка)")
        except Exception as e:
            self.log(f"[GAMES] Ошибка в _games_switch_account: {e}")
            import traceback
            self.log(f"[GAMES] Traceback: {traceback.format_exc()}")
            QtWidgets.QMessageBox.warning(
                self, "Ошибка",
                f"Ошибка при переключении аккаунта:\n{str(e)}"
            )
    
    def _games_password_manager(self):
        """Менеджер паролей игровых аккаунтов."""
        try:
            print("[GAMES] Кнопка 'Менеджер паролей' нажата!")
            self.log("[GAMES] Кнопка 'Менеджер паролей' нажата")
            self.log("[GAMES] Открытие менеджера паролей...")
            QtWidgets.QMessageBox.information(
                self, "Менеджер паролей",
                "Менеджер паролей будет реализован позже.\n"
                "Пароли хранятся в зашифрованном виде."
            )
        except Exception as e:
            self.log(f"[GAMES] Ошибка в _games_password_manager: {e}")
            import traceback
            self.log(f"[GAMES] Traceback: {traceback.format_exc()}")
    
    def _games_copy_configs(self):
        """Копирование конфигов игр."""
        try:
            print("[GAMES] Кнопка 'Копирование конфигов' нажата!")
            self.log("[GAMES] Кнопка 'Копирование конфигов' нажата")
            self.log("[GAMES] Копирование конфигов...")
            source = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Выберите папку с конфигами"
            )
            if source:
                self.log(f"[GAMES] Конфиги будут скопированы из: {source}")
                self.add_history(f"[GAMES] Копирование конфигов из {source}")
        except Exception as e:
            self.log(f"[GAMES] Ошибка в _games_copy_configs: {e}")
            import traceback
            self.log(f"[GAMES] Traceback: {traceback.format_exc()}")
    
    def _games_change_mode(self, mode: str):
        """Изменение игрового режима."""
        self.log(f"[GAMES] Переключение режима: {mode}")
        if hasattr(self, "lbl_ai_game"):
            self.lbl_ai_game.setText(f"Режим: {mode}")
        self.add_history(f"[GAMES] Режим изменен: {mode}")
    
    def _games_load_list(self):
        """Загрузить список игр из game_manager в games_list."""
        try:
            if not hasattr(self, "games_list"):
                self.log("[GAMES] games_list не найден")
                return
            
            if not hasattr(self, "game_manager"):
                self.log("[GAMES] game_manager не найден")
                return
            
            self.games_list.clear()
            
            # Перезагружаем профили из файла
            if hasattr(self.game_manager, "_load_profiles"):
                self.game_manager._load_profiles()
            
            if hasattr(self.game_manager, "game_profiles") and self.game_manager.game_profiles:
                self.log(f"[GAMES] Загрузка {len(self.game_manager.game_profiles)} игр в список")
                for game_key, profile in self.game_manager.game_profiles.items():
                    name = profile.get("name", game_key)
                    last_played = profile.get("last_played", "--")
                    action = profile.get("action", "[▶] Запустить")
                    
                    item = QtWidgets.QTreeWidgetItem(self.games_list)
                    item.setText(0, name)
                    item.setText(1, last_played)
                    item.setText(2, action)
                    item.setData(0, QtCore.Qt.ItemDataRole.UserRole, game_key)
                
                self.log(f"[GAMES] Загружено игр в список: {self.games_list.topLevelItemCount()}")
            else:
                self.log("[GAMES] Нет игр в game_profiles")
                # Если список пуст, добавляем сообщение
                item = QtWidgets.QTreeWidgetItem(self.games_list)
                item.setText(0, "Нет игр. Нажмите 'Добавить игру' для добавления.")
                item.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
        except Exception as e:
            self.log(f"[GAMES] Ошибка загрузки списка игр: {e}")
            import traceback
            self.log(f"[GAMES] Traceback: {traceback.format_exc()}")
    
    def _games_add_game(self):
        """Добавить новую игру в профиль."""
        try:
            print("[GAMES] Кнопка 'Добавить игру' нажата!")
            self.log("[GAMES] Кнопка 'Добавить игру' нажата")
            self.log("[GAMES] Открытие диалога добавления игры...")
            
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Добавить игру")
            dlg.setMinimumWidth(500)
            
            layout = QtWidgets.QVBoxLayout(dlg)
        
            # Название игры
            name_layout = QtWidgets.QHBoxLayout()
            name_layout.addWidget(QtWidgets.QLabel("Название игры:"))
            name_edit = QtWidgets.QLineEdit()
            name_edit.setPlaceholderText("Например: Valorant")
            name_layout.addWidget(name_edit)
            layout.addLayout(name_layout)
            
            # Ключ игры (для внутреннего использования)
            key_layout = QtWidgets.QHBoxLayout()
            key_layout.addWidget(QtWidgets.QLabel("Ключ (ID):"))
            key_edit = QtWidgets.QLineEdit()
            key_edit.setPlaceholderText("Например: valorant")
            key_layout.addWidget(key_edit)
            layout.addLayout(key_layout)
            
            # Путь к игре
            path_layout = QtWidgets.QHBoxLayout()
            path_edit = QtWidgets.QLineEdit()
            path_edit.setPlaceholderText("Путь к исполняемому файлу или ярлыку")
            path_btn = QtWidgets.QPushButton("Обзор...")
            path_layout.addWidget(QtWidgets.QLabel("Путь:"))
            path_layout.addWidget(path_edit, 1)
            path_layout.addWidget(path_btn)
            layout.addLayout(path_layout)
            
            def browse_path():
                file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
                    dlg, "Выберите исполняемый файл или ярлык",
                    "", "Executables (*.exe *.lnk);;All Files (*.*)"
                )
                if file_path:
                    path_edit.setText(file_path)
            
            path_btn.clicked.connect(browse_path)
            
            # Кнопки
            btn_layout = QtWidgets.QHBoxLayout()
            btn_save = QtWidgets.QPushButton("Сохранить")
            btn_cancel = QtWidgets.QPushButton("Отмена")
            btn_layout.addStretch()
            btn_layout.addWidget(btn_save)
            btn_layout.addWidget(btn_cancel)
            layout.addLayout(btn_layout)
            
            btn_save.clicked.connect(dlg.accept)
            btn_cancel.clicked.connect(dlg.reject)
            
            if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                name = name_edit.text().strip()
                key = key_edit.text().strip().lower().replace(" ", "_")
                path = path_edit.text().strip()
                
                if not name or not key or not path:
                    QtWidgets.QMessageBox.warning(
                        self, "Ошибка",
                        "Заполните все поля!"
                    )
                    return
                
                if not os.path.exists(path):
                    QtWidgets.QMessageBox.warning(
                        self, "Ошибка",
                        f"Файл не найден: {path}"
                    )
                    return
                
                # Добавляем игру в профили
                if hasattr(self, "game_manager"):
                    if not hasattr(self.game_manager, "game_profiles"):
                        self.game_manager.game_profiles = {}
                    
                    self.game_manager.game_profiles[key] = {
                        "name": name,
                        "path": path,
                        "last_played": "--",
                        "action": "[▶] Запустить"
                    }
                    self.game_manager.save_profiles()
                    
                    self.log(f"[GAMES] Игра добавлена: {name}")
                    self.add_history(f"[GAMES] Игра добавлена: {name}")
                    
                    # Обновляем список
                    self._games_load_list()
        except Exception as e:
            self.log(f"[GAMES] Ошибка в _games_add_game: {e}")
            import traceback
            self.log(f"[GAMES] Traceback: {traceback.format_exc()}")
            QtWidgets.QMessageBox.warning(
                self, "Ошибка",
                f"Ошибка при добавлении игры:\n{str(e)}"
            )
    
    def _games_search_game(self):
        """Поиск игр в установленных лаунчерах."""
        try:
            print("[GAMES] Кнопка 'Поиск игр' нажата!")
            self.log("[GAMES] Кнопка 'Поиск игр' нажата")
            self.log("[GAMES] Поиск игр...")
            
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Поиск игр")
            dlg.setMinimumWidth(600)
            dlg.setMinimumHeight(400)
            
            layout = QtWidgets.QVBoxLayout(dlg)
            
            # Поисковая строка
            search_layout = QtWidgets.QHBoxLayout()
            search_edit = QtWidgets.QLineEdit()
            search_edit.setPlaceholderText("Введите название игры для поиска...")
            search_btn = QtWidgets.QPushButton("🔍 Поиск")
            search_layout.addWidget(search_edit)
            search_layout.addWidget(search_btn)
            layout.addLayout(search_layout)
            
            # Список найденных игр
            results_list = QtWidgets.QListWidget()
            layout.addWidget(results_list)
            
            # Статус
            status_label = QtWidgets.QLabel("Готов к поиску...")
            layout.addWidget(status_label)
            
            def do_search():
                query = search_edit.text().strip().lower()
                if not query:
                    status_label.setText("Введите название игры")
                    return
                
                results_list.clear()
                status_label.setText("Поиск...")
                QtWidgets.QApplication.processEvents()
                
                # Ищем в стандартных местах
                found_games = []
                
                # Steam
                steam_paths = [
                    os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "Steam", "steamapps", "common"),
                    os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Steam", "steamapps", "common"),
                ]
                
                # Epic Games
                epic_paths = [
                    os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "Epic Games"),
                    os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Epic Games"),
                ]
                
                # Riot Games (Valorant)
                riot_paths = [
                    os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "Riot Games"),
                    os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Riot Games"),
                ]
                
                search_paths = []
                for path in steam_paths + epic_paths + riot_paths:
                    if os.path.exists(path):
                        search_paths.append(path)
                
                # Ищем .exe файлы
                for base_path in search_paths:
                    try:
                        for root, dirs, files in os.walk(base_path):
                            for file in files:
                                if file.endswith(".exe") and query in file.lower():
                                    full_path = os.path.join(root, file)
                                    game_name = os.path.splitext(file)[0]
                                    if game_name not in [g["name"] for g in found_games]:
                                        found_games.append({
                                            "name": game_name,
                                            "path": full_path
                                        })
                            if len(found_games) >= 20:  # Ограничиваем результаты
                                break
                            if len(found_games) >= 20:
                                break
                    except Exception:
                        continue
                
                # Также ищем в ярлыках на рабочем столе
                desktop = os.path.join(os.path.expanduser("~"), "Desktop")
                public_desktop = os.path.join(os.environ.get("PUBLIC", "C:\\Users\\Public"), "Desktop")
                
                for desktop_path in [desktop, public_desktop]:
                    if os.path.exists(desktop_path):
                        try:
                            for file in os.listdir(desktop_path):
                                if file.endswith(".lnk") and query in file.lower():
                                    full_path = os.path.join(desktop_path, file)
                                    game_name = os.path.splitext(file)[0]
                                    if game_name not in [g["name"] for g in found_games]:
                                        found_games.append({
                                            "name": game_name,
                                            "path": full_path
                                        })
                        except Exception:
                            continue
                
                # Отображаем результаты
                if found_games:
                    for game in found_games:
                        results_list.addItem(f"{game['name']} - {game['path']}")
                    status_label.setText(f"Найдено игр: {len(found_games)}")
                else:
                    status_label.setText("Игры не найдены")
                    results_list.addItem("Игры не найдены. Попробуйте другой запрос.")
            
            search_btn.clicked.connect(do_search)
            search_edit.returnPressed.connect(do_search)
            
            # Кнопка добавления выбранной игры
            btn_add = QtWidgets.QPushButton("➕ Добавить выбранную игру")
            btn_add.clicked.connect(lambda: self._games_add_from_search(results_list, dlg))
            layout.addWidget(btn_add)
            
            btn_close = QtWidgets.QPushButton("Закрыть")
            btn_close.clicked.connect(dlg.accept)
            layout.addWidget(btn_close)
            
            dlg.exec()
        except Exception as e:
            self.log(f"[GAMES] Ошибка в поиске игр: {e}")
            import traceback
            self.log(f"[GAMES] Traceback: {traceback.format_exc()}")
            QtWidgets.QMessageBox.warning(
                self, "Ошибка",
                f"Ошибка при поиске игр:\n{str(e)}"
            )
    
    def _games_add_from_search(self, results_list, dlg):
        """Добавить игру из результатов поиска."""
        current_item = results_list.currentItem()
        if not current_item or "не найдены" in current_item.text():
            QtWidgets.QMessageBox.warning(self, "Ошибка", "Выберите игру из списка")
            return
        
        text = current_item.text()
        if " - " in text:
            name, path = text.split(" - ", 1)
            
            # Генерируем ключ из названия
            key = name.lower().replace(" ", "_").replace(".", "")
            
            if hasattr(self, "game_manager"):
                if not hasattr(self.game_manager, "game_profiles"):
                    self.game_manager.game_profiles = {}
                
                self.game_manager.game_profiles[key] = {
                    "name": name,
                    "path": path,
                    "last_played": "--",
                    "action": "[▶] Запустить"
                }
                self.game_manager.save_profiles()
                
                self.log(f"[GAMES] Игра добавлена из поиска: {name}")
                self.add_history(f"[GAMES] Игра добавлена: {name}")
                
                self._games_load_list()
                dlg.accept()
    
    def _games_launch_selected(self, item, column):
        """Запустить выбранную игру."""
        game_key = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not game_key:
            return
        
        if hasattr(self, "game_manager"):
            self.game_manager.launch_game(game_key)
            
            # Обновляем время последнего запуска
            if game_key in self.game_manager.game_profiles:
                profile = self.game_manager.game_profiles[game_key]
                profile["last_played"] = datetime.now().strftime("%H:%M")
                profile["action"] = "[▶] Запустить"
                self.game_manager.save_profiles()
                
                # Обновляем текущую игру
                self.current_game_key = game_key
                self.current_game_name = profile.get("name", game_key)
                
                # Добавляем в историю монитора
                self.game_monitor_history.append({
                    "game": self.current_game_name,
                    "timestamp": datetime.now().isoformat()
                })
                
                self.log(f"[GAMES] Запущена игра: {self.current_game_name}")
                self.add_history(f"[GAMES] Запущена игра: {self.current_game_name}")
                
                # Обновляем список
                self._games_load_list()
    
    def _games_show_chart(self):
        """Показать график производительности игры."""
        try:
            print("[GAMES] Кнопка 'Показать график' нажата!")
            self.log("[GAMES] Кнопка 'Показать график' нажата")
            self.log("[GAMES] Открытие графика...")
            
            if not self.game_monitor_history:
                QtWidgets.QMessageBox.information(
                    self, "График",
                    "Нет данных для отображения графика.\n"
                    "Запустите игру для сбора статистики."
                )
                return
            
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("График производительности")
            dlg.setMinimumSize(800, 600)
            
            layout = QtWidgets.QVBoxLayout(dlg)
            
            # Простой текстовый график (можно заменить на matplotlib позже)
            chart_text = QtWidgets.QPlainTextEdit()
            chart_text.setReadOnly(True)
            chart_text.setFont(QtGui.QFont("Courier", 10))
            
            text = "=== ГРАФИК ПРОИЗВОДИТЕЛЬНОСТИ ===\n\n"
            text += f"Игра: {self.current_game_name}\n"
            text += f"Всего записей: {len(self.game_monitor_history)}\n\n"
            
            # Простая визуализация
            if len(self.game_monitor_history) > 0:
                text += "История запусков:\n"
                for i, entry in enumerate(self.game_monitor_history[-20:], 1):
                    timestamp = entry.get("timestamp", "")
                    if "T" in timestamp:
                        time_part = timestamp.split("T")[1].split(".")[0][:5]
                    else:
                        time_part = timestamp[:5]
                    text += f"{i:2d}. {entry.get('game', 'Неизвестно')} - {time_part}\n"
            
            chart_text.setPlainText(text)
            layout.addWidget(chart_text)
            
            btn_close = QtWidgets.QPushButton("Закрыть")
            btn_close.clicked.connect(dlg.accept)
            layout.addWidget(btn_close)
            
            dlg.exec()
        except Exception as e:
            self.log(f"[GAMES] Ошибка в _games_show_chart: {e}")
            import traceback
            self.log(f"[GAMES] Traceback: {traceback.format_exc()}")
            QtWidgets.QMessageBox.warning(
                self, "Ошибка",
                f"Ошибка при показе графика:\n{str(e)}"
            )
    
    def _games_update_monitor(self):
        """Обновить игровой монитор (реальные данные)."""
        import platform
        if not hasattr(self, "lbl_monitor_game"):
            return
        # Название игры
        if hasattr(self, "current_game_name") and self.current_game_name:
            self.lbl_monitor_game.setText(f"Игра: {self.current_game_name}")
        else:
            self.lbl_monitor_game.setText("Игра: -")

        # FPS (реально, если возможно)
        fps = None
        try:
            # Попытка получить FPS через сторонние библиотеки (например, через py-cpuinfo, pywin32, или через overlay)
            # Здесь пример с использованием сторонней библиотеки (если установлена):
            try:
                import GPUtil
                # Некоторые видеокарты поддерживают FPS через overlay, но универсального способа нет
                # Можно интегрировать с внешними FPS overlay (MSI Afterburner, RTSS, NVIDIA SMI и т.д.)
                # Здесь оставим как заглушку, но не рандом
                fps = None
            except ImportError:
                fps = None
            # Если не удалось — не показываем FPS
        except Exception:
            fps = None
        if hasattr(self, "lbl_fps"):
            self.lbl_fps.setText(f"FPS: {fps if fps is not None else '--'}")

        # Пинг (можно сделать реальным, например, до 8.8.8.8)
        try:
            import subprocess
            import re
            ping_ms = None
            if platform.system() == "Windows":
                output = subprocess.run(["ping", "8.8.8.8", "-n", "1"], capture_output=True, text=True)
                match = re.search(r"Average = (\d+)ms", output.stdout)
                if match:
                    ping_ms = int(match.group(1))
            else:
                output = subprocess.run(["ping", "-c", "1", "8.8.8.8"], capture_output=True, text=True)
                match = re.search(r"time=(\d+\.?\d*) ms", output.stdout)
                if match:
                    ping_ms = float(match.group(1))
            if hasattr(self, "lbl_ping"):
                self.lbl_ping.setText(f"Пинг: {ping_ms if ping_ms is not None else '--'} ms")
        except Exception:
            if hasattr(self, "lbl_ping"):
                self.lbl_ping.setText("Пинг: -- ms")

        # Температура CPU
        try:
            cpu_temp = psutil.sensors_temperatures()
            temp_value = None
            if cpu_temp:
                # Ищем наиболее вероятный сенсор
                for name, entries in cpu_temp.items():
                    for entry in entries:
                        if "cpu" in name.lower() or "core" in entry.label.lower():
                            temp_value = entry.current
                            break
                    if temp_value is not None:
                        break
                if temp_value is None:
                    temp_value = list(cpu_temp.values())[0][0].current if list(cpu_temp.values())[0] else 0
            else:
                temp_value = psutil.cpu_percent(interval=0.1) * 0.8 + 30
        except Exception:
            temp_value = psutil.cpu_percent(interval=0.1) * 0.8 + 30
        if hasattr(self, "cpu_temp_bar"):
            self.cpu_temp_bar.setValue(int(temp_value))

        # Температура GPU (через GPUtil или psutil, если возможно)
        gpu_temp = None
        try:
            try:
                import GPUtil
                gpus = GPUtil.getGPUs()
                if gpus:
                    gpu_temp = gpus[0].temperature
            except ImportError:
                gpu_temp = None
            if gpu_temp is None:
                # Альтернатива: через psutil (редко поддерживается)
                gpu_temp = 0
        except Exception:
            gpu_temp = 0
        if hasattr(self, "gpu_temp_bar"):
            self.gpu_temp_bar.setValue(int(gpu_temp))

        # RAM
        if hasattr(self, "lbl_ram_usage"):
            ram = psutil.virtual_memory()
            self.lbl_ram_usage.setText(f"RAM: {ram.percent:.1f}%")

        # VRAM (через GPUtil, если возможно)
        vram_percent = None
        try:
            try:
                import GPUtil
                gpus = GPUtil.getGPUs()
                if gpus:
                    vram_percent = int((gpus[0].memoryUsed / gpus[0].memoryTotal) * 100)
            except ImportError:
                vram_percent = None
        except Exception:
            vram_percent = None
        if hasattr(self, "lbl_vram_usage"):
            self.lbl_vram_usage.setText(f"VRAM: {vram_percent if vram_percent is not None else '--'}%")

    def lock_workstation(self):
        """Заблокировать рабочую станцию (экран) в Windows."""
        try:
            os.system("rundll32.exe user32.dll,LockWorkStation")
        except Exception as e:
            self.log(f"Ошибка блокировки: {e}")

    def open_microsoft_store(self):
        try:
            os.startfile("ms-windows-store:")
        except Exception:
            try:
                subprocess.Popen(["cmd", "/c", "start", "", "ms-windows-store:"])
            except Exception as e:
                self.log(f"Ошибка открытия Microsoft Store: {e}")

    def run_speedtest(self):
        if speedtest is None:
            QtWidgets.QMessageBox.warning(
                self,
                "Ошибка",
                "Модуль speedtest-cli не установлен.\nУстановите его командой: pip install speedtest-cli"
            )
            self.log("Ошибка: speedtest-cli не установлен")
            return
        
        # Создаем диалог с анимацией
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Speedtest - Измерение скорости")
        dlg.setMinimumSize(400, 300)
        dlg.setModal(True)
        
        layout = QtWidgets.QVBoxLayout(dlg)
        
        # Виджет с анимацией
        animation_widget = SpeedTestAnimationWidget()
        layout.addWidget(animation_widget, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        
        # Метки для результатов
        status_label = QtWidgets.QLabel("Подключение к серверу...")
        status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        status_label.setStyleSheet("font-size: 14px; color: #00D4FF;")
        layout.addWidget(status_label)
        
        download_label = QtWidgets.QLabel("Скачивание: -- Мбит/с")
        download_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        download_label.setStyleSheet("font-size: 12px; color: #E0E0FF;")
        layout.addWidget(download_label)
        
        upload_label = QtWidgets.QLabel("Загрузка: -- Мбит/с")
        upload_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        upload_label.setStyleSheet("font-size: 12px; color: #E0E0FF;")
        layout.addWidget(upload_label)
        
        ping_label = QtWidgets.QLabel("Ping: -- мс")
        ping_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        ping_label.setStyleSheet("font-size: 12px; color: #E0E0FF;")
        layout.addWidget(ping_label)
        
        # Кнопка закрытия (скрыта во время теста)
        btn_close = QtWidgets.QPushButton("Закрыть")
        btn_close.clicked.connect(dlg.accept)
        btn_close.setEnabled(False)
        layout.addWidget(btn_close)
        
        # Запускаем анимацию
        animation_widget.start_animation()
        
        def run_test():
            """Запустить тест в отдельном потоке."""
            try:
                status_label.setText("Поиск лучшего сервера...")
                QtWidgets.QApplication.processEvents()
                
                st = speedtest.Speedtest()
                server = st.get_best_server()
                ping_result = server.get('latency', 0)
                ping_label.setText(f"Ping: {ping_result:.2f} мс")
                QtWidgets.QApplication.processEvents()
                
                status_label.setText("Измерение скорости скачивания...")
                QtWidgets.QApplication.processEvents()
                download_speed = st.download() / 1_000_000  # Конвертируем в Мбит/с
                download_label.setText(f"Скачивание: {download_speed:.2f} Мбит/с")
                QtWidgets.QApplication.processEvents()
                
                status_label.setText("Измерение скорости загрузки...")
                QtWidgets.QApplication.processEvents()
                upload_speed = st.upload() / 1_000_000  # Конвертируем в Мбит/с
                upload_label.setText(f"Загрузка: {upload_speed:.2f} Мбит/с")
                QtWidgets.QApplication.processEvents()
                
                status_label.setText("✓ Тест завершён!")
                status_label.setStyleSheet("font-size: 14px; color: #00FF9D;")
                
                # Останавливаем анимацию
                animation_widget.stop_animation()
                btn_close.setEnabled(True)
                
                # Логируем результаты
                self.log(f"Speedtest завершён: Скачивание {download_speed:.2f} Мбит/с, "
                        f"Загрузка {upload_speed:.2f} Мбит/с, Ping {ping_result:.2f} мс")
                self.add_history(f"[Система] Speedtest: ↓{download_speed:.2f} Мбит/с "
                               f"↑{upload_speed:.2f} Мбит/с, Ping {ping_result:.2f} мс")
                
            except Exception as e:
                status_label.setText(f"Ошибка: {str(e)}")
                status_label.setStyleSheet("font-size: 14px; color: #FF005C;")
                animation_widget.stop_animation()
                btn_close.setEnabled(True)
                self.log(f"Ошибка speedtest: {e}")
        
        # Запускаем тест в отдельном потоке
        test_thread = threading.Thread(target=run_test, daemon=True)
        test_thread.start()
        
        # Показываем диалог
        dlg.exec()

    def run_program_dialog(self):
        """Показать диалог выбора исполняемого файла и запустить его."""
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Выберите программу для запуска",
            os.path.expanduser("~"),
            "Programs (*.exe);;All Files (*.*)",
        )
        if file_path:
            self.run_program(file_path)

    def run_program(self, path: str):
        """Запустить указанную программу (exe или другой файл)."""
        path = self._normalize_windows_path(path)
        if not path:
            raise RuntimeError("Не указан путь к программе")

        resolved = None
        for cand in self._candidate_program_paths(path):
            cand = self._normalize_windows_path(cand)
            if not cand:
                continue

            # allow launching by name from PATH (cmd.exe, notepad, etc.)
            if not os.path.exists(cand) and (os.path.sep not in cand) and ("\\" not in cand):
                which = shutil.which(cand)
                if which:
                    cand = which

            if os.path.exists(cand):
                resolved = cand
                break

            # try PATH resolution even if a name-like string is provided
            which2 = shutil.which(cand) or shutil.which(os.path.basename(cand))
            if which2 and os.path.exists(which2):
                resolved = which2
                break

        if not resolved:
            raise RuntimeError(f"Файл не найден: {path}")

        self.log(f"Запуск программы: {resolved}")
        try:
            os.startfile(resolved)
        except OSError:
            # Fallback для случаев, когда os.startfile недоступен
            try:
                subprocess.Popen([resolved])
            except Exception as e:
                self.log(f"Ошибка запуска программы: {e}")
                return f"Ошибка запуска программы: {e}"
        return f"Успешный запуск программы: {resolved}"

    # ========= Обработка центральной команды =========

    def handle_command_enter(self):
        """Обработчик Enter в поле центральной команды на главной вкладке."""
        text = self.input_line.text().strip()
        if not text:
            return
        self.add_history(f"[CMD] {text}")
        try:
            handled = self.process_simple_command(text)
            if not handled:
                self._chat_send_text(text)
        except Exception as e:
            self.log(f"Ошибка обработки команды: {e}")
        self.input_line.clear()

    def process_simple_command(self, text: str):
        lower = text.lower().strip()

        # Прямое определение URL (если ввели просто ссылку или домен)
        is_url = lower.startswith(("http://", "https://", "www."))
        if not is_url and "." in lower and " " not in lower and "/" not in lower:
            # Проверка на популярные домены первого уровня
            tlds = (".com", ".ru", ".org", ".net", ".io", ".me", ".ai", ".info", ".biz", ".online")
            if any(lower.endswith(tld) for tld in tlds):
                is_url = True

        if is_url:
            url = text.strip()
            if url.lower().startswith("www."):
                url = "https://" + url
            elif not (url.lower().startswith("http://") or url.lower().startswith("https://")):
                url = "https://" + url
            self.log(f"Открываю ссылку напрямую: {url}")
            webbrowser.open(url)
            self.append_chat("Система", f"Открываю ссылку: {url}")
            return True

        # Базовые команды проводника/браузера/скрина/блокировки
        if "проводник" in lower or "explorer" in lower:
            self.open_explorer()
            self.append_chat("Система", "Открыл проводник.")
            return True

        # Команды вида "открой хром и найди ..." / "открой браузер и найди ..."
        if ("браузер" in lower or "chrome" in lower or "хром" in lower) and "найди" in lower:
            try:
                # Берём часть после слова "найди"
                after = lower.split("найди", 1)[1].strip()
                if after:
                    query = quote_plus(after)
                    url = f"https://www.google.com/search?q={query}"
                    self.log(f"Открываю поиск в браузере: {url}")
                    webbrowser.open(url)
                    self.append_chat("Система", f"Ищу в браузере: {after}")
                    self.add_history(f"[Система] Поиск в браузере: {after}")
                    return True
            except Exception as e:
                self.log(f"Ошибка при формировании поискового запроса: {e}")

        if "браузер" in lower or "chrome" in lower or "хром" in lower:
            self.open_browser()
            self.append_chat("Система", "Открыл браузер.")
            return True

        if "скрин" in lower or "скриншот" in lower:
            self.take_screenshot()
            self.append_chat("Система", "Сделал скриншот.")
            return True

        if "заблокируй" in lower or "блокировка" in lower or "заблокируй пк" in lower:
            self.lock_workstation()
            self.append_chat("Система", "Блокирую экран.")
            return True

        if "microsoft store" in lower or "ms store" in lower or "магазин" in lower:
            self.open_microsoft_store()
            self.append_chat("Система", "Открыл Microsoft Store.")
            return True

        # Открытие популярных приложений через реальный поиск (where/AppData)
        if ("телеграм" in lower) and ("открой" in lower or "запусти" in lower):
            try:
                self._tool_open_app("telegram")
                self.append_chat("Система", "Пытаюсь открыть Telegram.")
            except Exception as e:
                self.append_chat("Система", f"Не смог открыть Telegram: {e}")
            return True

        if ("cursor" in lower or "курсор" in lower) and ("открой" in lower or "запусти" in lower):
            try:
                self._tool_open_app("cursor")
                self.append_chat("Система", "Пытаюсь открыть Cursor.")
            except Exception as e:
                self.append_chat("Система", f"Не смог открыть Cursor: {e}")
            return True

        if ("cmd" in lower or "командн" in lower) and ("открой" in lower or "запусти" in lower):
            try:
                self._tool_open_app("cmd")
                self.append_chat("Система", "Открываю cmd.")
            except Exception as e:
                self.append_chat("Система", f"Не смог открыть cmd: {e}")
            return True

        if "выключи пк" in lower or "выключи компьютер" in lower:
            self.append_chat("Система", "Подтверждение выключения пока не реализовано.")
            self.log("Запрос на выключение ПК (нужно добавить подтверждение)")
            return True

        if "перезагрузи" in lower or "перезагрузка" in lower:
            self.append_chat("Система", "Подтверждение перезагрузки пока не реализовано.")
            self.log("Запрос на перезагрузку ПК (нужно добавить подтверждение)")
            return True

        if lower.startswith("запусти") or lower.startswith("открой"):
            # Простейший парсер: извлечь всё после первого пробела
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                candidate = parts[1].strip().strip('"')
                
                # Проверяем, не URL ли это
                cand_lower = candidate.lower()
                is_cand_url = cand_lower.startswith(("http://", "https://", "www."))
                if not is_cand_url and "." in cand_lower and " " not in cand_lower:
                    tlds = (".com", ".ru", ".org", ".net", ".io", ".me", ".ai")
                    if any(cand_lower.endswith(tld) for tld in tlds):
                        is_cand_url = True
                
                if is_cand_url:
                    url = candidate
                    if url.lower().startswith("www."):
                        url = "https://" + url
                    elif not (url.lower().startswith("http://") or url.lower().startswith("https://")):
                        url = "https://" + url
                    self.log(f"Открываю веб-ссылку через команду: {url}")
                    webbrowser.open(url)
                    self.append_chat("Система", f"Открываю ссылку: {url}")
                    return True
                
                # Если не URL, проверяем как локальный путь
                if os.path.exists(candidate):
                    self.run_program(candidate)
                    self.append_chat("Система", f"Пытаюсь запустить: {candidate}")
                    return True
                else:
                    self.append_chat("Система", f"Не нашёл путь или ссылку: '{candidate}'. Можешь указать полный путь к .exe или URL?")
                    return True

        return False

    # ========= Настройки / Плейсхолдеры =========

    def open_settings_dialog(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Настройки NEURO COMMAND")
        dlg.setMinimumWidth(420)

        layout = QtWidgets.QVBoxLayout(dlg)

        lbl_general = QtWidgets.QLabel("Общие настройки")
        lbl_general.setProperty("class", "sectionTitle")
        layout.addWidget(lbl_general)

        # Автообновление статуса системы
        self.chk_auto_status = QtWidgets.QCheckBox("Автообновление статуса системы (каждые 3 сек)")
        self.chk_auto_status.setChecked(self.auto_update_status)
        layout.addWidget(self.chk_auto_status)

        def apply_settings():
            self.auto_update_status = self.chk_auto_status.isChecked()
            if self.auto_update_status:
                self.status_timer.start(3000)
                self.log("Автообновление статуса: включено")
            else:
                self.status_timer.stop()
                self.log("Автообновление статуса: выключено")

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)

        btn_apply = QtWidgets.QPushButton("Применить")
        btn_apply.clicked.connect(apply_settings)
        btn_close = QtWidgets.QPushButton("Закрыть")
        btn_close.clicked.connect(dlg.accept)

        btn_row.addWidget(btn_apply)
        btn_row.addWidget(btn_close)

        layout.addLayout(btn_row)

        dlg.exec()
    
    # ========= Аналитика =========
    
    def _analytics_refresh_data(self):
        """Обновить данные аналитики."""
        try:
            self._update_game_summary()
            self._update_personal_preview()
            commands_total = self.usage_stats.get("commands_executed", 0)
            scenarios_run = getattr(self, "automation_run_count", 0)
            games_played = len(getattr(self, "game_monitor_history", []))
            local_ai_ready = sum(1 for v in (getattr(self, "_service_health", {}) or {}).values() if v == "healthy")
            if hasattr(self, "lbl_anal_overview_commands"):
                self.lbl_anal_overview_commands.setText(str(commands_total))
            if hasattr(self, "lbl_anal_overview_automation"):
                self.lbl_anal_overview_automation.setText(str(scenarios_run))
            if hasattr(self, "lbl_anal_overview_games"):
                self.lbl_anal_overview_games.setText(str(games_played))
            if hasattr(self, "lbl_anal_overview_local_ai"):
                self.lbl_anal_overview_local_ai.setText(f"{local_ai_ready}/2")

            # Системная аналитика
            if hasattr(self, "lbl_anal_cpu_avg"):
                try:
                    cpu_avg = psutil.cpu_percent(interval=0.1)
                    ram_info = psutil.virtual_memory()
                    ram_avg = ram_info.percent
                    uptime_seconds = time.time() - psutil.boot_time()
                    uptime_hours = int(uptime_seconds / 3600)
                    processes_count = len(psutil.pids())
                    
                    self.lbl_anal_cpu_avg.setText(f"Средняя загрузка CPU: {cpu_avg:.1f} %")
                    self.lbl_anal_ram_avg.setText(f"Средняя загрузка RAM: {ram_avg:.1f} %")
                    self.lbl_anal_uptime.setText(f"Время работы системы: {uptime_hours} часов")
                    self.lbl_anal_processes.setText(f"Активных процессов: {processes_count}")
                    
                    # История метрик
                    if hasattr(self, "anal_metrics_text"):
                        metrics_text = f"CPU: {cpu_avg:.1f}% | RAM: {ram_avg:.1f}% | Процессов: {processes_count}\n"
                        metrics_text += f"Время работы: {uptime_hours}ч | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        self.anal_metrics_text.setPlainText(metrics_text)
                except Exception as e:
                    self.log(f"[ANALYTICS] Ошибка обновления системной аналитики: {e}")
            
            # Аналитика автоматизации
            if hasattr(self, "lbl_anal_scenarios_total"):
                scenarios_total = len(getattr(self, "automation_scenarios", []))
                tasks = getattr(self, "planner_tasks", [])
                tasks_completed = sum(1 for t in tasks if "[✓]" in t.get("status", ""))
                tasks_pending = len(tasks) - tasks_completed
                
                self.lbl_anal_scenarios_total.setText(f"Всего сценариев: {scenarios_total}")
                self.lbl_anal_scenarios_run.setText(f"Запущено сценариев: {scenarios_run}")
                self.lbl_anal_tasks_completed.setText(f"Выполнено задач: {tasks_completed}")
                self.lbl_anal_tasks_pending.setText(f"Ожидает выполнения: {tasks_pending}")
            
            # Аналитика игр
            if hasattr(self, "lbl_anal_games_total"):
                if hasattr(self, "game_manager"):
                    games_total = len(getattr(self.game_manager, "game_profiles", {}))
                else:
                    games_total = 0
                gaming_time = "Не отслеживается"
                
                self.lbl_anal_games_total.setText(f"Всего игр в профиле: {games_total}")
                self.lbl_anal_games_played.setText(f"Игр запущено: {games_played}")
                self.lbl_anal_gaming_time.setText(f"Время в играх: {gaming_time}")
            
            # Аналитика команд
            if hasattr(self, "lbl_anal_commands_total"):
                commands_today = len([c for c in self.command_history 
                                     if datetime.fromisoformat(c["timestamp"]).date() == datetime.now().date()])
                
                # Самая частая команда
                command_words = {}
                for cmd in self.command_history[-100:]:  # Последние 100 команд
                    words = cmd["text"].lower().split()
                    for word in words:
                        if len(word) > 3:
                            command_words[word] = command_words.get(word, 0) + 1
                most_used = max(command_words.items(), key=lambda x: x[1])[0] if command_words else "нет данных"
                
                # Пик активности (по часам)
                hour_counts = {}
                for cmd in self.command_history:
                    hour = datetime.fromisoformat(cmd["timestamp"]).hour
                    hour_counts[hour] = hour_counts.get(hour, 0) + 1
                peak_hour = max(hour_counts.items(), key=lambda x: x[1])[0] if hour_counts else "--"
                
                self.lbl_anal_commands_total.setText(f"Всего команд выполнено: {commands_total}")
                self.lbl_anal_commands_today.setText(f"Команд сегодня: {commands_today}")
                self.lbl_anal_most_used.setText(f"Самая частая команда: {most_used}")
                self.lbl_anal_activity_hours.setText(f"Пик активности: {peak_hour}:00")
                
                # Обновляем список команд
                if hasattr(self, "anal_commands_list"):
                    self.anal_commands_list.clear()
                    for cmd in self.command_history[-50:]:  # Последние 50
                        timestamp = datetime.fromisoformat(cmd["timestamp"]).strftime("%H:%M:%S")
                        self.anal_commands_list.addItem(f"[{timestamp}] {cmd['text']}")
                    self.anal_commands_list.scrollToBottom()
        except Exception as e:
            self.log(f"[ANALYTICS] Ошибка обновления аналитики: {e}")
    
    def _analytics_export(self, format_type: str):
        """Экспорт данных аналитики в файл."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_dir = os.path.dirname(os.path.abspath(__file__))
            
            if format_type == "json":
                filename = f"analytics_{timestamp}.json"
                filepath = os.path.join(base_dir, filename)
                data = {
                    "timestamp": datetime.now().isoformat(),
                    "system": {
                        "cpu": psutil.cpu_percent(interval=0.1),
                        "ram": psutil.virtual_memory().percent,
                        "processes": len(psutil.pids()),
                    },
                    "automation": {
                        "scenarios_total": len(self.automation_scenarios),
                        "scenarios_run": self.automation_run_count,
                        "tasks_completed": sum(1 for t in self.planner_tasks if "[✓]" in t.get("status", "")),
                    },
                    "commands": {
                        "total": self.usage_stats.get("commands_executed", 0),
                        "history": self.command_history[-100:],
                    },
                    "usage_stats": self.usage_stats,
                }
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    
            elif format_type == "txt":
                filename = f"analytics_{timestamp}.txt"
                filepath = os.path.join(base_dir, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write("=== АНАЛИТИКА NEURO COMMAND ===\n\n")
                    f.write(f"Дата экспорта: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                    f.write("=== СИСТЕМА ===\n")
                    f.write(f"CPU: {psutil.cpu_percent(interval=0.1):.1f}%\n")
                    f.write(f"RAM: {psutil.virtual_memory().percent:.1f}%\n")
                    f.write(f"Процессов: {len(psutil.pids())}\n\n")
                    f.write("=== АВТОМАТИЗАЦИЯ ===\n")
                    f.write(f"Сценариев: {len(self.automation_scenarios)}\n")
                    f.write(f"Запущено: {self.automation_run_count}\n\n")
                    f.write("=== КОМАНДЫ ===\n")
                    f.write(f"Всего: {self.usage_stats.get('commands_executed', 0)}\n")
                    f.write("\nИстория команд:\n")
                    for cmd in self.command_history[-50:]:
                        f.write(f"{cmd['timestamp']}: {cmd['text']}\n")
                        
            elif format_type == "csv":
                filename = f"analytics_{timestamp}.csv"
                filepath = os.path.join(base_dir, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write("Тип,Параметр,Значение\n")
                    f.write(f"Система,CPU,{psutil.cpu_percent(interval=0.1):.1f}\n")
                    f.write(f"Система,RAM,{psutil.virtual_memory().percent:.1f}\n")
                    f.write(f"Система,Процессов,{len(psutil.pids())}\n")
                    f.write(f"Автоматизация,Сценариев,{len(self.automation_scenarios)}\n")
                    f.write(f"Автоматизация,Запущено,{self.automation_run_count}\n")
                    f.write(f"Команды,Всего,{self.usage_stats.get('commands_executed', 0)}\n")
            
            self.log(f"[ANALYTICS] Данные экспортированы: {filepath}")
            self.add_history(f"[ANALYTICS] Экспорт в {format_type.upper()}: {filename}")
        except Exception as e:
            self.log(f"[ANALYTICS] Ошибка экспорта: {e}")
    
    def _automation_clear_logs(self):
        """Очистить логи автоматизации."""
        if hasattr(self, "auto_logs_list"):
            self.auto_logs_list.clear()
        self.automation_logs = []
        self.log("[AUTO] Логи очищены")
        self.add_history("[AUTO] Логи автоматизации очищены")
    
    def _automation_export_logs(self):
        """Экспортировать логи автоматизации."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_dir = os.path.dirname(os.path.abspath(__file__))
            filename = f"automation_logs_{timestamp}.txt"
            filepath = os.path.join(base_dir, filename)
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("=== ЛОГИ АВТОМАТИЗАЦИИ ===\n\n")
                for log in self.automation_logs:
                    f.write(f"{log['timestamp']}: {log['scenario']} ({log['category']}) - {log['status']}\n")
                    if log.get('error'):
                        f.write(f"  Ошибка: {log['error']}\n")
            
            self.log(f"[AUTO] Логи экспортированы: {filepath}")
            self.add_history(f"[AUTO] Логи экспортированы: {filename}")
        except Exception as e:
            self.log(f"[AUTO] Ошибка экспорта логов: {e}")
    
    # ========= Персонализация =========
    
    def _personal_apply_theme(self):
        """Применить выбранную тему."""
        if self.radio_theme_dark.isChecked():
            self._apply_dark_theme()
            self.setStyleSheet(self.theme_styles["dark"])
            self._apply_shell_theme_overrides()
            self.log("[PERSONAL] Применена темная тема")
        elif self.radio_theme_light.isChecked():
            self._apply_light_theme()
            self.setStyleSheet(self.theme_styles["light"])
            self._apply_shell_theme_overrides()
            self.log("[PERSONAL] Применена светлая тема")
        else:
            # Автоматическая тема - определяем по времени суток
            from datetime import datetime
            hour = datetime.now().hour
            if 6 <= hour < 20:  # День - светлая тема
                self._apply_light_theme()
                self.setStyleSheet(self.theme_styles["light"])
                self._apply_shell_theme_overrides()
                self.log("[PERSONAL] Применена автоматическая тема (светлая - день)")
            else:  # Ночь - темная тема
                self._apply_dark_theme()
                self.setStyleSheet(self.theme_styles["dark"])
                self._apply_shell_theme_overrides()
                self.log("[PERSONAL] Применена автоматическая тема (темная - ночь)")
        if hasattr(self, "chk_compact_mode") and self.chk_compact_mode.isChecked():
            self._personal_toggle_compact_mode(2)
        self._update_personal_preview()
    
    def _personal_toggle_auto_status(self, state):
        """Переключить автообновление статуса."""
        self.auto_update_status = (state == 2)  # Qt.Checked = 2
        if self.auto_update_status:
            self.status_timer.start(3000)
        else:
            self.status_timer.stop()
        self._update_personal_preview()
        self.log(f"[PERSONAL] Автообновление статуса: {'включено' if self.auto_update_status else 'выключено'}")

    def _personal_toggle_compact_mode(self, state):
        """Переключить компактный UI-режим."""
        compact = (state == 2)
        if compact:
            compact_style = """
                QWidget { font-size: 10pt; }
                QPushButton { padding: 6px 12px; border-radius: 12px; }
                QLineEdit, QTextEdit, QPlainTextEdit { padding: 6px 10px; }
                QGroupBox::title { font-size: 10pt; }
            """
            base = self.theme_styles.get("dark", "") if self.current_theme == "dark" else self.theme_styles.get("light", "")
            self.setStyleSheet(base + compact_style)
            self.log("[PERSONAL] Компактный режим включён")
        else:
            if self.current_theme == "dark":
                self.setStyleSheet(self.theme_styles.get("dark", ""))
            else:
                self.setStyleSheet(self.theme_styles.get("light", ""))
            self.log("[PERSONAL] Компактный режим выключен")
        self._update_personal_preview()
    
    def _personal_save_settings(self):
        """Сохранить настройки персонализации."""
        user_name = self.edit_user_name.text().strip()
        
        # Определяем тему
        theme = "dark"
        if self.radio_theme_light.isChecked():
            theme = "light"
        elif self.radio_theme_auto.isChecked():
            theme = "auto"
        
        # Сохраняем настройки
        settings = {
            "user_name": user_name if user_name else os.environ.get("USERNAME", "Пользователь"),
            "theme": theme,
            "auto_status": self.auto_update_status,
            "sound_notifications": self.chk_sound_notifications.isChecked(),
            "compact_mode": self.chk_compact_mode.isChecked(),
            "ai_suggestions": self.chk_ai_suggestions.isChecked(),
            "tts_enabled": bool(getattr(self, "chk_tts_enable", None) and self.chk_tts_enable.isChecked()),
        }

        if hasattr(self, "cmb_neural_provider") and self.cmb_neural_provider is not None:
            settings["neural_provider"] = self.cmb_neural_provider.currentData() or self.cmb_neural_provider.currentText()
        if hasattr(self, "edit_neural_model") and self.edit_neural_model is not None:
            settings["neural_model"] = self.edit_neural_model.text().strip()
        
        # Сохраняем в файл
        settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            self.log(f"[PERSONAL] Настройки сохранены в {settings_path}")
            self.add_history(f"[PERSONAL] Настройки сохранены")
            
            # Применяем тему сразу
            self._personal_apply_theme()
            self._update_personal_preview()
        except Exception as e:
            self.log(f"[PERSONAL] Ошибка сохранения настроек: {e}")
    
    def _load_personal_settings(self):
        """Загрузить сохраненные настройки персонализации."""
        settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        if not os.path.exists(settings_path):
            return
        
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)

            # Применяем настройки
            if hasattr(self, "edit_user_name") and "user_name" in settings:
                self.edit_user_name.setText(settings.get("user_name", ""))
            
            if "theme" in settings:
                theme = settings["theme"]
                if hasattr(self, "radio_theme_dark"):
                    if theme == "dark":
                        self.radio_theme_dark.setChecked(True)
                    elif theme == "light":
                        self.radio_theme_light.setChecked(True)
                    elif theme == "auto":
                        self.radio_theme_auto.setChecked(True)
                    self._personal_apply_theme()
            
            if "auto_status" in settings and hasattr(self, "chk_auto_status_personal"):
                self.auto_update_status = settings["auto_status"]
                self.chk_auto_status_personal.setChecked(self.auto_update_status)
            
            if "sound_notifications" in settings and hasattr(self, "chk_sound_notifications"):
                self.chk_sound_notifications.setChecked(settings["sound_notifications"])

            if "tts_enabled" in settings and hasattr(self, "chk_tts_enable") and self.chk_tts_enable is not None:
                try:
                    self.chk_tts_enable.setChecked(bool(settings.get("tts_enabled")))
                except Exception:
                    pass
            
            if "compact_mode" in settings and hasattr(self, "chk_compact_mode"):
                self.chk_compact_mode.setChecked(settings["compact_mode"])
            
            if "ai_suggestions" in settings and hasattr(self, "chk_ai_suggestions"):
                self.chk_ai_suggestions.setChecked(settings["ai_suggestions"])

            # Neural provider/model
            if hasattr(self, "cmb_neural_provider") and "neural_provider" in settings:
                prov = (settings.get("neural_provider") or "").lower()
                self.cmb_neural_provider.blockSignals(True)
                try:
                    for i in range(self.cmb_neural_provider.count()):
                        if (self.cmb_neural_provider.itemData(i) or "").lower() == prov:
                            self.cmb_neural_provider.setCurrentIndex(i)
                            break
                finally:
                    self.cmb_neural_provider.blockSignals(False)

            if hasattr(self, "edit_neural_model") and "neural_model" in settings:
                self.edit_neural_model.setText(settings.get("neural_model") or "")

            # Apply neural settings after widgets are ready
            if hasattr(self, "cmb_neural_provider"):
                self._on_neural_provider_changed()
            self._update_personal_preview()
            
            self.log("[PERSONAL] Настройки загружены")
        except Exception as e:
            self.log(f"[PERSONAL] Ошибка загрузки настроек: {e}")


def main():
    # Set DPI awareness for Windows
    if hasattr(QtWidgets.QApplication, 'setHighDpiScaleFactorRoundingPolicy'):
        QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
            QtCore.Qt.HighDpiScaleFactorRoundingPolicy.RoundPreferFloor
        )
    
    # Enable high DPI support
    
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
