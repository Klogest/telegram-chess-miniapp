"""Однофайловый шаблон коммерческого Telegram-бота на aiogram 3 (RU/EN)."""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv
from openai import AsyncOpenAI

import config_texts


BASE_DIR = Path(__file__).resolve().parent
BUTTONS_FILE = BASE_DIR / "buttons.json"
USERS_FILE = BASE_DIR / "users.json"
LANGS_FILE = BASE_DIR / "langs.json"
KNOWLEDGE_FILE = BASE_DIR / "knowledge.txt"
APPLICATION_BUTTON = config_texts.TEXTS["ru"]["application_button"]
CATALOG_BUTTON = config_texts.TEXTS["ru"]["catalog_button"]
LANGUAGE_BUTTON = config_texts.LANGUAGE_BUTTON
MAX_CONTEXT_MESSAGES = 6
AI_COOLDOWN_SECONDS = 3

load_dotenv(BASE_DIR / ".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
AI_ENABLED = os.getenv("AI_ENABLED", "false").strip().lower() == "true"
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1").strip()
AI_MODEL = os.getenv("AI_MODEL", "llama-3.1-8b-instant").strip()
PORT = int(os.getenv("PORT", "8080"))

try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else 0
except ValueError:
    ADMIN_ID = 0
    logger.error("ADMIN_ID должен быть числом.")

if not BOT_TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в файле .env.")
if AI_ENABLED and not AI_API_KEY:
    logger.warning("AI_ENABLED=true, но AI_API_KEY не задан; ИИ будет недоступен.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = (
    AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    if AI_ENABLED and AI_API_KEY
    else None
)
ai_history: dict[int, list[dict[str, str]]] = {}
ai_last_request: dict[int, float] = {}


class ApplicationForm(StatesGroup):
    name = State()
    contact = State()
    task = State()


class AdminForm(StatesGroup):
    button_name = State()
    button_answer = State()
    knowledge = State()
    broadcast = State()


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        logger.exception("Не удалось прочитать %s: %s", path.name, exc)
        return default


def _write_json(path: Path, data: Any) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    temp_path.replace(path)


def ensure_data_files() -> None:
    """Создает хранилища при первом запуске, не перезаписывая существующие."""
    for path, initial in (
        (BUTTONS_FILE, {}),
        (USERS_FILE, []),
        (LANGS_FILE, {}),
    ):
        if not path.exists():
            _write_json(path, initial)


def get_custom_buttons() -> dict[str, str]:
    data = _read_json(BUTTONS_FILE, {})
    if not isinstance(data, dict):
        logger.error("buttons.json должен содержать JSON-словарь.")
        return {}
    return {
        str(name): str(answer)
        for name, answer in data.items()
        if isinstance(name, str) and isinstance(answer, str)
    }


def add_user(user_id: int) -> None:
    users = _read_json(USERS_FILE, [])
    if not isinstance(users, list):
        users = []
    if user_id not in users:
        users.append(user_id)
        try:
            _write_json(USERS_FILE, users)
        except OSError:
            logger.exception("Не удалось сохранить users.json")


def get_lang(user_id: int | None, language_code: str | None = None) -> str:
    """Возвращает сохраненный язык пользователя, иначе язык из Telegram."""
    if not user_id:
        return config_texts.DEFAULT_LANG
    data = _read_json(LANGS_FILE, {})
    saved = data.get(str(user_id)) if isinstance(data, dict) else None
    if saved in config_texts.LANGS:
        return saved
    return config_texts.normalize_lang(language_code)


def set_lang(user_id: int, lang: str) -> None:
    """Сохраняет выбранный язык пользователя."""
    data = _read_json(LANGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data[str(user_id)] = lang if lang in config_texts.LANGS else config_texts.DEFAULT_LANG
    try:
        _write_json(LANGS_FILE, data)
    except OSError:
        logger.exception("Не удалось сохранить язык пользователя")


def tr(user_id: int | None, key: str, **kwargs: object) -> str:
    """Короткий доступ к тексту на языке пользователя."""
    return config_texts.get_text(get_lang(user_id), key, **kwargs)


def uid_of(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def build_main_keyboard(user_id: int | None = None) -> ReplyKeyboardMarkup:
    lang = get_lang(user_id)
    texts = config_texts.TEXTS.get(lang, config_texts.TEXTS[config_texts.DEFAULT_LANG])
    rows: list[list[KeyboardButton]] = []
    base_buttons = list(texts["menu"].keys())
    for index in range(0, len(base_buttons), 2):
        rows.append([KeyboardButton(text=item) for item in base_buttons[index : index + 2]])

    rows.append([KeyboardButton(text=texts["application_button"])])
    custom_buttons = list(get_custom_buttons().keys())
    for index in range(0, len(custom_buttons), 2):
        rows.append(
            [KeyboardButton(text=item) for item in custom_buttons[index : index + 2]]
        )
    if WEBAPP_URL:
        rows.append(
            [
                KeyboardButton(
                    text=texts["catalog_button"],
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        )
    rows.append([KeyboardButton(text=LANGUAGE_BUTTON)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="admin:add")],
            [InlineKeyboardButton(text="➖ Удалить кнопку", callback_data="admin:delete")],
            [InlineKeyboardButton(text="🧠 Обновить базу ИИ", callback_data="admin:knowledge")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        ]
    )


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID and user_id == ADMIN_ID)


async def safe_admin_send(text: str) -> bool:
    if not ADMIN_ID:
        logger.error("Не задан корректный ADMIN_ID; уведомление не отправлено.")
        return False
    try:
        await bot.send_message(ADMIN_ID, text)
        return True
    except TelegramAPIError:
        logger.exception("Не удалось отправить сообщение администратору.")
        return False


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    try:
        await state.clear()
        if user_id:
            add_user(user_id)
            get_lang(user_id, message.from_user.language_code if message.from_user else None)
        await message.answer(tr(user_id, "welcome"), reply_markup=build_main_keyboard(user_id))
    except Exception:
        logger.exception("Ошибка в обработчике /start")
        await message.answer(tr(user_id, "start_error"))


@dp.message(Command("id"))
async def id_handler(message: Message) -> None:
    user_id = uid_of(message)
    if not user_id:
        return
    await message.answer(tr(user_id, "id", user_id=user_id))


@dp.message(Command("lang"))
async def lang_handler(message: Message) -> None:
    user_id = uid_of(message)
    if not user_id:
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            ]
        ]
    )
    await message.answer(tr(user_id, "lang_prompt"), reply_markup=keyboard)


@dp.callback_query(F.data.startswith("lang:"))
async def lang_callback(callback: CallbackQuery) -> None:
    if not callback.from_user:
        await callback.answer()
        return
    user_id = callback.from_user.id
    lang = (callback.data or "").split(":", maxsplit=1)[1]
    set_lang(user_id, lang)
    await callback.message.answer(
        tr(user_id, "lang_set"), reply_markup=build_main_keyboard(user_id)
    )
    await callback.answer()


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    await state.clear()
    await message.answer(tr(user_id, "cancel"), reply_markup=build_main_keyboard(user_id))


@dp.message(Command("admin"))
async def admin_handler(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    if not user_id or not is_admin(user_id):
        await message.answer(tr(user_id, "admin_only"))
        return
    await state.clear()
    await message.answer(tr(user_id, "admin_panel"), reply_markup=admin_keyboard())


@dp.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    if not user_id or not is_admin(user_id):
        await callback.answer(tr(user_id, "no_access"), show_alert=True)
        return

    try:
        action = (callback.data or "").split(":", maxsplit=1)[1]
        await state.clear()
        if action == "add":
            await state.set_state(AdminForm.button_name)
            await callback.message.answer(tr(user_id, "admin_add_name"))
        elif action == "delete":
            buttons = get_custom_buttons()
            if not buttons:
                await callback.message.answer(tr(user_id, "admin_no_buttons"))
            else:
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"Удалить: {name[:50]}",
                                callback_data=f"delete:{index}",
                            )
                        ]
                        for index, name in enumerate(buttons)
                    ]
                )
                await callback.message.answer(
                    tr(user_id, "admin_delete_prompt"), reply_markup=keyboard
                )
        elif action == "knowledge":
            await state.set_state(AdminForm.knowledge)
            await callback.message.answer(tr(user_id, "admin_knowledge_prompt"))
        elif action == "broadcast":
            await state.set_state(AdminForm.broadcast)
            await callback.message.answer(tr(user_id, "admin_broadcast_prompt"))
        else:
            await callback.answer(tr(user_id, "unknown_action"))
            return
        await callback.answer()
    except Exception:
        logger.exception("Ошибка в панели администратора")
        await callback.answer(tr(user_id, "action_error"), show_alert=True)


@dp.callback_query(F.data.startswith("delete:"))
async def delete_button_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    if not user_id or not is_admin(user_id):
        await callback.answer(tr(user_id, "no_access"), show_alert=True)
        return
    try:
        index = int((callback.data or "").split(":", maxsplit=1)[1])
        buttons = get_custom_buttons()
        names = list(buttons)
        if index < 0 or index >= len(names):
            await callback.answer(tr(user_id, "admin_delete_stale"), show_alert=True)
            return
        removed_name = names[index]
        del buttons[removed_name]
        _write_json(BUTTONS_FILE, buttons)
        await callback.message.answer(tr(user_id, "admin_deleted", name=removed_name))
        await callback.answer()
    except (ValueError, OSError):
        logger.exception("Не удалось удалить кнопку")
        await callback.answer(tr(user_id, "admin_delete_error"), show_alert=True)


@dp.message(AdminForm.button_name)
async def admin_button_name(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    name = (message.text or "").strip()
    reserved = {LANGUAGE_BUTTON, APPLICATION_BUTTON, CATALOG_BUTTON, "/start", "/admin", "/cancel", "/lang", "/id"}
    for texts in config_texts.TEXTS.values():
        reserved.update(texts["menu"])
        reserved.add(texts["application_button"])
        reserved.add(texts["catalog_button"])
    if not name or len(name) > 64 or name in reserved:
        await message.answer(tr(user_id, "admin_unique"))
        return
    await state.update_data(button_name=name)
    await state.set_state(AdminForm.button_answer)
    await message.answer(tr(user_id, "admin_add_answer"))


@dp.message(AdminForm.button_answer)
async def admin_button_answer(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    answer = (message.text or "").strip()
    if not answer:
        await message.answer(tr(user_id, "admin_answer_empty"))
        return
    try:
        data = await state.get_data()
        buttons = get_custom_buttons()
        buttons[data["button_name"]] = answer
        _write_json(BUTTONS_FILE, buttons)
        await state.clear()
        await message.answer(tr(user_id, "admin_button_saved"))
    except (OSError, KeyError):
        logger.exception("Не удалось сохранить новую кнопку")
        await message.answer(tr(user_id, "admin_button_save_error"))


@dp.message(AdminForm.knowledge)
async def admin_knowledge(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    text = (message.text or "").strip()
    if not text:
        await message.answer(tr(user_id, "admin_knowledge_empty"))
        return
    try:
        KNOWLEDGE_FILE.write_text(text, encoding="utf-8")
        ai_history.clear()
        await state.clear()
        await message.answer(tr(user_id, "admin_knowledge_saved"))
    except OSError:
        logger.exception("Не удалось обновить базу знаний")
        await message.answer(tr(user_id, "admin_knowledge_error"))


@dp.message(AdminForm.broadcast)
async def admin_broadcast(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    text = (message.text or "").strip()
    if not text:
        await message.answer(tr(user_id, "admin_broadcast_empty"))
        return
    users = _read_json(USERS_FILE, [])
    if not isinstance(users, list):
        users = []
    delivered = 0
    failed = 0
    for recipient_id in users:
        try:
            await bot.send_message(int(recipient_id), text)
            delivered += 1
        except (TelegramAPIError, ValueError, TypeError):
            failed += 1
            logger.info("Не удалось доставить рассылку пользователю %r", recipient_id)
    await state.clear()
    await message.answer(
        tr(user_id, "admin_broadcast_done", delivered=delivered, failed=failed)
    )


@dp.message(ApplicationForm.name)
async def application_name(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    name = (message.text or "").strip()
    if not name:
        await message.answer(tr(user_id, "name_required"))
        return
    await state.update_data(name=name)
    await state.set_state(ApplicationForm.contact)
    await message.answer(tr(user_id, "ask_contact"))


@dp.message(ApplicationForm.contact)
async def application_contact(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    contact = (message.text or "").strip()
    if not contact:
        await message.answer(tr(user_id, "contact_required"))
        return
    await state.update_data(contact=contact)
    await state.set_state(ApplicationForm.task)
    await message.answer(tr(user_id, "ask_task"))


@dp.message(ApplicationForm.task)
async def application_task(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    task = (message.text or "").strip()
    if not task:
        await message.answer(tr(user_id, "task_required"))
        return
    try:
        data = await state.get_data()
        username = f"@{message.from_user.username}" if message.from_user and message.from_user.username else "не указан"
        admin_text = (
            f"📝 Новая заявка — {config_texts.COMPANY_NAME}\n\n"
            f"Имя: {data['name']}\n"
            f"Контакт: {data['contact']}\n"
            f"Задача: {task}\n"
            f"Telegram: {username}\nID: {user_id if user_id else 'неизвестен'}"
        )
        sent = await safe_admin_send(admin_text)
        await state.clear()
        if sent:
            await message.answer(tr(user_id, "request_done"), reply_markup=build_main_keyboard(user_id))
        else:
            await message.answer(
                tr(user_id, "request_fail"),
                reply_markup=build_main_keyboard(user_id),
            )
    except Exception:
        logger.exception("Ошибка при обработке заявки")
        await state.clear()
        await message.answer(tr(user_id, "request_error"))


@dp.message(F.web_app_data)
async def webapp_order_handler(message: Message) -> None:
    user_id = uid_of(message)
    try:
        payload = json.loads(message.web_app_data.data)
        if not isinstance(payload, dict):
            raise ValueError("Ожидался JSON-объект")
        items = payload.get("items", [])
        if not isinstance(items, list):
            items = []
        lines = []
        for item in items[:50]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", item.get("id", "Позиция")))[:150]
            quantity = item.get("quantity", item.get("qty", 1))
            price = item.get("price", "")
            lines.append(f"• {title} × {quantity}" + (f" — {price}" if price != "" else ""))
        name = str(payload.get("name", "Не указано"))[:200]
        comment = str(payload.get("comment", ""))[:1000]
        order_text = (
            f"🛍 Новый заказ из Mini App — {config_texts.COMPANY_NAME}\n\n"
            f"Позиции:\n{chr(10).join(lines) if lines else 'Не указаны'}\n\n"
            f"Имя: {name}\nКомментарий: {comment or 'нет'}\n"
            f"Telegram ID: {user_id if user_id else 'неизвестен'}"
        )
        if await safe_admin_send(order_text):
            await message.answer(tr(user_id, "order_done"))
        else:
            await message.answer(tr(user_id, "order_fail"))
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        logger.exception("Получены некорректные данные заказа из Mini App")
        await message.answer(tr(user_id, "order_bad"))
    except Exception:
        logger.exception("Ошибка обработки заказа из Mini App")
        await message.answer(tr(user_id, "order_error"))


def read_knowledge() -> str:
    try:
        return KNOWLEDGE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        logger.exception("Не удалось прочитать knowledge.txt")
        return ""


async def answer_with_ai(message: Message) -> None:
    user_id = uid_of(message)
    if not ai_client or not user_id:
        await message.answer(tr(user_id, "ai_unavailable"))
        return

    now = asyncio.get_running_loop().time()
    elapsed = now - ai_last_request.get(user_id, 0.0)
    if elapsed < AI_COOLDOWN_SECONDS:
        await message.answer(tr(user_id, "ai_cooldown"))
        return
    ai_last_request[user_id] = now

    knowledge = read_knowledge()
    if not knowledge:
        await message.answer(tr(user_id, "ai_empty_knowledge"))
        return
    history = ai_history.setdefault(user_id, [])
    history.append({"role": "user", "content": (message.text or "")[:4000]})
    history[:] = history[-MAX_CONTEXT_MESSAGES:]
    system_prompt = tr(user_id, "ai_system", knowledge=knowledge[:12000])
    try:
        response = await ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "system", "content": system_prompt}, *history],
            temperature=0.3,
            max_tokens=350,
        )
        answer = response.choices[0].message.content if response.choices else None
        answer = (answer or "").strip()
        if not answer:
            answer = tr(user_id, "ai_empty_answer")
        history.append({"role": "assistant", "content": answer})
        history[:] = history[-MAX_CONTEXT_MESSAGES:]
        await message.answer(answer)
    except Exception:
        logger.exception("Ошибка запроса к ИИ-провайдеру")
        await message.answer(tr(user_id, "ai_error"))


@dp.message(F.text)
async def text_router(message: Message, state: FSMContext) -> None:
    user_id = uid_of(message)
    try:
        if user_id:
            add_user(user_id)
            get_lang(user_id, message.from_user.language_code if message.from_user else None)
        text = (message.text or "").strip()
        if await state.get_state() is not None:
            return
        lang = get_lang(user_id)
        texts = config_texts.TEXTS.get(lang, config_texts.TEXTS[config_texts.DEFAULT_LANG])
        if text == LANGUAGE_BUTTON:
            await lang_handler(message)
            return
        if text in (texts["application_button"], APPLICATION_BUTTON):
            await state.set_state(ApplicationForm.name)
            await message.answer(tr(user_id, "ask_name"))
            return
        if text in texts["menu"]:
            await message.answer(
                texts["menu"][text],
                reply_markup=build_main_keyboard(user_id),
            )
            return
        custom_buttons = get_custom_buttons()
        if text in custom_buttons:
            await message.answer(custom_buttons[text], reply_markup=build_main_keyboard(user_id))
            return
        if text in (texts["catalog_button"], CATALOG_BUTTON) and WEBAPP_URL:
            await message.answer(tr(user_id, "catalog_hint"), reply_markup=build_main_keyboard(user_id))
            return
        if AI_ENABLED:
            await answer_with_ai(message)
        else:
            await message.answer(
                tr(user_id, "fallback"),
                reply_markup=build_main_keyboard(user_id),
            )
    except Exception:
        logger.exception("Ошибка обработки текстового сообщения")
        await message.answer(tr(user_id, "start_error"))


async def healthcheck(_: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def run_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    try:
        await site.start()
        logger.info("Веб-сервер запущен на порту %s", PORT)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    ensure_data_files()
    try:
        await asyncio.gather(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
            run_web_server(),
        )
    finally:
        if ai_client:
            await ai_client.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
