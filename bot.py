import logging
import os
import traceback
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from db import (
    init_db, add_shop, remove_shop, get_all_shops, get_shop_by_id,
    find_shops_within_radius, maps_link,
    add_pending_shop, get_pending_shop,
    get_user_pending_shops, approve_pending_shop, reject_pending_shop,
    add_review, get_shop_reviews, get_shop_avg_rating, user_has_reviewed,
    upsert_user,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SEARCH_RADIUS_MILES = 100

SERVICE_TYPES = [
    "Ремонт грузовика",
    "Шиномонтаж",
    "Ремонт прицепа",
    "Выездной сервис",
    "Мобильный механик",
]

# /addrepair states
(ADD_NAME, ADD_LOCATION, ADD_PHONE, ADD_LANGUAGE, ADD_SERVICE_TYPE, ADD_COMMENT) = range(6)

# Suggest states
(SUG_NAME, SUG_LOCATION, SUG_PHONE, SUG_LANGUAGE, SUG_SERVICE_TYPE, SUG_COMMENT) = range(100, 106)
SUG_CONFIRM_ADDRESS = 106

# Find nearby state
FIND_AWAITING_LOCATION = 200

# Rate states
RATE_AWAITING_STARS = 300
RATE_AWAITING_COMMENT = 301


def get_admin_ids() -> set[int]:
    raw = os.environ.get("ADMIN_IDS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_admin(user_id: int) -> bool:
    return user_id in get_admin_ids()


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Эта команда только для администраторов.", reply_markup=main_keyboard())
            return ConversationHandler.END
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def stars(n: int) -> str:
    return "⭐" * n + "☆" * (5 - n)


# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📍 Найти рядом", "➕ Добавить сервис"],
            ["📋 Мои заявки", "ℹ️ Помощь"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def service_type_inline(prefix: str = "stype"):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(s, callback_data=f"{prefix}:{s}")] for s in SERVICE_TYPES]
    )


def shop_inline_keyboard(shop: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗺 Маршрут", url=maps_link(shop["latitude"], shop["longitude"])),
        ],
        [
            InlineKeyboardButton("⭐ Отзывы", callback_data=f"reviews:{shop['id']}"),
            InlineKeyboardButton("⭐ Оставить отзыв", callback_data=f"rate:{shop['id']}"),
        ],
    ])


def shop_text(shop: dict, index: int = None, show_id: bool = False) -> str:
    id_line = f"🆔 ID: `{shop['id']}`\n" if show_id else ""
    num_line = f"*{index}. {shop['name']}*\n" if index else f"*{shop['name']}*\n"
    dist_line = f"📍 {shop['distance_miles']} мили от вас\n" if "distance_miles" in shop else ""
    comment_line = f"💬 {shop['comment']}\n" if shop.get("comment") else ""
    avg = get_shop_avg_rating(shop["id"])
    rating_line = f"⭐ Рейтинг: {avg}/5\n" if avg else "⭐ Рейтинг: пока нет отзывов\n"
    return (
        f"{id_line}"
        f"{num_line}"
        f"{dist_line}"
        f"🔧 {shop['service_type']}\n"
        f"🗣 {shop['speaks_language']}\n"
        f"📞 {shop['phone']}\n"
        f"{comment_line}"
        f"{rating_line}"
    )


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    upsert_user(u.id, u.username or "", u.full_name or "")
    await update.message.reply_text(
        "👋 *Добро пожаловать в Поиск Автосервисов!*\n\n"
        "Используйте кнопки ниже, чтобы найти проверенные сервисы рядом с вами "
        "или предложить новый.\n\n"
        "📍 *Найти рядом* — сервисы в радиусе 100 миль\n"
        "➕ *Добавить сервис* — предложить сервис на проверку\n"
        "📋 *Мои заявки* — статус ваших предложений\n"
        "ℹ️ *Помощь* — справка",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ─── ℹ️ Помощь ────────────────────────────────────────────────────────────────

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    base = (
        "ℹ️ *Поиск Автосервисов — Справка*\n\n"
        "*Для водителей:*\n"
        "• 📍 *Найти рядом* — поделитесь геолокацией, получите список сервисов\n"
        "• ➕ *Добавить сервис* — предложить сервис на проверку администратору\n"
        "• 📋 *Мои заявки* — статус ваших предложений\n\n"
        "_Все показанные сервисы проверены администратором._"
    )
    admin_section = (
        "\n\n*Команды администратора:*\n"
        "• /addrepair — добавить сервис напрямую (без проверки)\n"
        "• /listrepair — список всех одобренных сервисов\n"
        "• /removerepair — удалить сервис\n"
    ) if is_admin(user_id) else ""
    await update.message.reply_text(base + admin_section, parse_mode="Markdown", reply_markup=main_keyboard())


# ─── 📍 Найти рядом ───────────────────────────────────────────────────────────

async def find_nearby_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📍 Нажмите кнопку ниже, чтобы поделиться геолокацией:",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    return FIND_AWAITING_LOCATION


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_lat = update.message.location.latitude
    user_lng = update.message.location.longitude

    await update.message.reply_text("🔍 Ищу сервисы рядом с вами...", reply_markup=main_keyboard())

    shops = find_shops_within_radius(user_lat, user_lng, SEARCH_RADIUS_MILES)

    if not shops:
        await update.message.reply_text(
            f"😕 В радиусе {SEARCH_RADIUS_MILES} миль сервисов не найдено.\n\n"
            "Вы можете предложить сервис через ➕ *Добавить сервис*!",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
        return ConversationHandler.END

    n = len(shops)
    if n == 1:
        word = "сервис"
    elif 2 <= n <= 4:
        word = "сервиса"
    else:
        word = "сервисов"

    await update.message.reply_text(
        f"✅ Найдено *{n} {word}* в радиусе {SEARCH_RADIUS_MILES} миль:",
        parse_mode="Markdown",
    )

    for i, shop in enumerate(shops, start=1):
        await update.message.reply_text(
            shop_text(shop, index=i),
            parse_mode="Markdown",
            reply_markup=shop_inline_keyboard(shop),
        )

    await update.message.reply_text(
        "Нажмите «📍 Найти рядом» для нового поиска.",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


async def find_nearby_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ─── Geocoding ────────────────────────────────────────────────────────────────

def geocode_address(address: str):
    """Returns (lat, lng, display_name) via Nominatim (OpenStreetMap). No API key needed."""
    geolocator = Nominatim(user_agent="truck_repair_bot_v1")
    try:
        location = geolocator.geocode(address, timeout=10)
        if location:
            return location.latitude, location.longitude, location.address
    except (GeocoderTimedOut, GeocoderUnavailable):
        pass
    return None


# ─── ➕ Добавить сервис ───────────────────────────────────────────────────────

async def suggest_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "➕ *Предложить сервис*\n\n"
        "Шаг 1 из 6. Введите *полный адрес* сервиса.\n\n"
        "Пример:\n`3433 Ramona Ave, Sacramento, CA 95826`\n\n"
        "_В любой момент введите /cancel для отмены._",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return SUG_LOCATION


async def sug_got_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    address = update.message.text.strip()
    await update.message.reply_text("🔍 Ищу адрес на карте...")

    result = geocode_address(address)
    if not result:
        await update.message.reply_text(
            "❌ Адрес не найден. Попробуйте ввести точнее.\n\n"
            "Пример: `3433 Ramona Ave, Sacramento, CA 95826`",
            parse_mode="Markdown",
        )
        return SUG_LOCATION

    lat, lng, display_name = result
    context.user_data["sug_lat_pending"] = lat
    context.user_data["sug_lng_pending"] = lng
    context.user_data["sug_address_pending"] = display_name

    await update.message.reply_location(latitude=lat, longitude=lng)
    await update.message.reply_text(
        f"📍 *Адрес найден:*\n{display_name}\n\n✅ Верно?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да", callback_data="sug_confirm:yes"),
            InlineKeyboardButton("❌ Нет", callback_data="sug_confirm:no"),
        ]]),
    )
    return SUG_CONFIRM_ADDRESS


async def sug_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.replace("sug_confirm:", "")

    if choice == "no":
        await query.edit_message_text("Хорошо, введите адрес ещё раз:")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Введите *полный адрес* сервиса:\n\nПример:\n`3433 Ramona Ave, Sacramento, CA 95826`",
            parse_mode="Markdown",
        )
        return SUG_LOCATION

    context.user_data["sug_lat"] = context.user_data.pop("sug_lat_pending")
    context.user_data["sug_lng"] = context.user_data.pop("sug_lng_pending")
    context.user_data.pop("sug_address_pending", None)

    await query.edit_message_text("✅ Адрес подтверждён.")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Шаг 2 из 6. Введите *название сервиса*.",
        parse_mode="Markdown",
    )
    return SUG_NAME


async def sug_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["sug_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 3 из 6. Введите *номер телефона* сервиса.",
        parse_mode="Markdown",
    )
    return SUG_PHONE


async def sug_got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["sug_phone"] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 4 из 6. Выберите *тип сервиса*:",
        parse_mode="Markdown",
        reply_markup=service_type_inline("stype"),
    )
    return SUG_SERVICE_TYPE


async def sug_service_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    service = query.data.replace("stype:", "")
    if service not in SERVICE_TYPES:
        await query.edit_message_reply_markup(reply_markup=service_type_inline("stype"))
        return SUG_SERVICE_TYPE
    context.user_data["sug_service_type"] = service
    await query.edit_message_text(f"✅ Тип сервиса: *{service}*", parse_mode="Markdown")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Шаг 5 из 6. На каких *языках* говорят в сервисе?\n_(например: Русский, Английский, Испанский)_",
        parse_mode="Markdown",
    )
    return SUG_LANGUAGE


async def sug_got_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["sug_language"] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 6 из 6. Добавьте *комментарий* (режим работы, особенности).\n_Или введите /skip, чтобы пропустить._",
        parse_mode="Markdown",
    )
    return SUG_COMMENT


async def sug_comment_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["sug_comment"] = ""
    return await sug_save(update, context)


async def sug_got_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["sug_comment"] = update.message.text.strip()
    return await sug_save(update, context)


async def sug_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data
    user = update.effective_user
    username = user.full_name or user.username or str(user.id)

    pending_id = add_pending_shop(
        submitted_by_id=user.id,
        submitted_by_name=username,
        name=d["sug_name"],
        latitude=d["sug_lat"],
        longitude=d["sug_lng"],
        phone=d["sug_phone"],
        speaks_language=d["sug_language"],
        service_type=d["sug_service_type"],
        comment=d.get("sug_comment", ""),
    )

    await update.message.reply_text(
        "✅ *Спасибо! Ваша заявка отправлена.*\n\n"
        "Администратор проверит её в ближайшее время. "
        "Отслеживайте статус в разделе 📋 *Мои заявки*.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )

    await _notify_admins(context, pending_id, d, username)
    context.user_data.clear()
    return ConversationHandler.END


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, pending_id: int, d: dict, username: str):
    link = maps_link(d["sug_lat"], d["sug_lng"])
    comment_line = f"💬 {d['sug_comment']}\n" if d.get("sug_comment") else ""
    text = (
        f"🆕 *Новая заявка на добавление сервиса* (ID: `{pending_id}`)\n\n"
        f"👤 От: {username}\n"
        f"🔧 *{d['sug_name']}*\n"
        f"🛠 {d['sug_service_type']}\n"
        f"🗣 {d['sug_language']}\n"
        f"📞 {d['sug_phone']}\n"
        f"{comment_line}"
        f"🗺 [Посмотреть на карте]({link})"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{pending_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{pending_id}"),
    ]])
    for admin_id in get_admin_ids():
        try:
            await context.bot.send_location(chat_id=admin_id, latitude=d["sug_lat"], longitude=d["sug_lng"])
            await context.bot.send_message(
                chat_id=admin_id, text=text,
                parse_mode="Markdown", reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить администратора {admin_id}: {e}")


async def suggest_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Заявка отменена.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ─── 📋 Мои заявки ────────────────────────────────────────────────────────────

async def my_suggestions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    suggestions = get_user_pending_shops(user_id)

    if not suggestions:
        await update.message.reply_text(
            "📋 Вы ещё не предлагали сервисы.\n\nНажмите ➕ *Добавить сервис*, чтобы подать заявку!",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text(
        f"📋 *Ваши заявки ({len(suggestions)} шт.):*",
        parse_mode="Markdown",
    )

    status_labels = {"pending": "⏳ На проверке", "approved": "✅ Одобрено", "rejected": "❌ Отклонено"}
    for s in suggestions:
        status = status_labels.get(s["status"], "❓")
        comment_line = f"💬 {s['comment']}\n" if s.get("comment") else ""
        link = maps_link(s["latitude"], s["longitude"])
        text = (
            f"{status}\n"
            f"🔧 *{s['name']}*\n"
            f"🛠 {s['service_type']}\n"
            f"📞 {s['phone']}\n"
            f"{comment_line}"
            f"🗺 [На карте]({link})\n"
            f"_Подана: {s['submitted_at'][:10]}_"
        )
        await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

    await update.message.reply_text("\u200b", reply_markup=main_keyboard())


# ─── ⭐ Отзывы ────────────────────────────────────────────────────────────────

async def reviews_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    shop_id = int(query.data.replace("reviews:", ""))
    shop = get_shop_by_id(shop_id)
    if not shop:
        await query.answer("Сервис не найден.", show_alert=True)
        return

    reviews = get_shop_reviews(shop_id)
    if not reviews:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"⭐ *{shop['name']}* — отзывов пока нет.\n\nБудьте первым — нажмите ⭐ Оценить!",
            parse_mode="Markdown",
        )
        return

    avg = get_shop_avg_rating(shop_id)
    header = f"⭐ *Отзывы: {shop['name']}*\nСредняя оценка: *{avg}/5* ({len(reviews)} отзывов)\n\n"
    lines = []
    for r in reviews[:10]:
        comment_line = f"\n_{r['comment']}_" if r.get("comment") else ""
        lines.append(f"{stars(r['rating'])} {r['user_name']}{comment_line}")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=header + "\n\n".join(lines),
        parse_mode="Markdown",
    )


# ─── ⭐ Оценить ───────────────────────────────────────────────────────────────

async def rate_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    shop_id = int(query.data.replace("rate:", ""))
    shop = get_shop_by_id(shop_id)
    if not shop:
        await query.answer("Сервис не найден.", show_alert=True)
        return ConversationHandler.END

    if user_has_reviewed(shop_id, query.from_user.id):
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"ℹ️ Вы уже оставили отзыв на *{shop['name']}*.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    context.user_data["rate_shop_id"] = shop_id
    context.user_data["rate_shop_name"] = shop["name"]

    stars_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ 1", callback_data="stars:1"),
        InlineKeyboardButton("⭐ 2", callback_data="stars:2"),
        InlineKeyboardButton("⭐ 3", callback_data="stars:3"),
        InlineKeyboardButton("⭐ 4", callback_data="stars:4"),
        InlineKeyboardButton("⭐ 5", callback_data="stars:5"),
    ]])
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"⭐ *Оценить: {shop['name']}*\n\nВыберите оценку от 1 до 5:",
        parse_mode="Markdown",
        reply_markup=stars_keyboard,
    )
    return RATE_AWAITING_STARS


async def rate_got_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    rating = int(query.data.replace("stars:", ""))
    context.user_data["rate_stars"] = rating
    await query.edit_message_text(
        f"Ваша оценка: {stars(rating)}\n\nДобавьте *комментарий* (необязательно).\n_Или введите /skip._",
        parse_mode="Markdown",
    )
    return RATE_AWAITING_COMMENT


async def rate_comment_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["rate_comment"] = ""
    return await rate_save(update, context)


async def rate_got_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["rate_comment"] = update.message.text.strip()
    return await rate_save(update, context)


async def rate_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data
    user = update.effective_user
    username = user.full_name or user.username or str(user.id)
    add_review(
        shop_id=d["rate_shop_id"],
        user_id=user.id,
        user_name=username,
        rating=d["rate_stars"],
        comment=d.get("rate_comment", ""),
    )
    await update.message.reply_text(
        f"✅ Спасибо за отзыв на *{d['rate_shop_name']}*!\n{stars(d['rate_stars'])}",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def rate_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Оценка отменена.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ─── Admin approve / reject ───────────────────────────────────────────────────

async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Только для администраторов.", show_alert=True)
        return
    pending_id = int(query.data.replace("approve:", ""))
    shop = get_pending_shop(pending_id)
    if not shop:
        await query.edit_message_text("❌ Заявка не найдена.")
        return
    if shop["status"] != "pending":
        await query.edit_message_text(f"ℹ️ Заявка уже *{shop['status']}*.", parse_mode="Markdown")
        return
    new_id = approve_pending_shop(pending_id)
    await query.edit_message_text(
        f"✅ *Одобрено!* Добавлено как сервис ID `{new_id}`.\n🔧 {shop['name']}",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            chat_id=shop["submitted_by_id"],
            text=f"✅ Ваш сервис *{shop['name']}* был *одобрен* и теперь доступен в поиске!",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя: {e}")


async def reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Только для администраторов.", show_alert=True)
        return
    pending_id = int(query.data.replace("reject:", ""))
    shop = get_pending_shop(pending_id)
    if not shop:
        await query.edit_message_text("❌ Заявка не найдена.")
        return
    if shop["status"] != "pending":
        await query.edit_message_text(f"ℹ️ Заявка уже *{shop['status']}*.", parse_mode="Markdown")
        return
    reject_pending_shop(pending_id)
    await query.edit_message_text(f"❌ *Отклонено.* {shop['name']}", parse_mode="Markdown")
    try:
        await context.bot.send_message(
            chat_id=shop["submitted_by_id"],
            text=f"❌ Ваша заявка на сервис *{shop['name']}* была отклонена.",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя: {e}")


# ─── Admin /addrepair ─────────────────────────────────────────────────────────

@admin_only
async def addrepair_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "➕ *Добавить сервис (Администратор)*\n\n"
        "Шаг 1 из 6. Введите *название сервиса*.\n\n_/cancel для отмены._",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADD_NAME


async def add_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 2 из 6. Отправьте *геолокацию сервиса*.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Отправить геолокацию сервиса", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    return ADD_LOCATION


async def add_got_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["latitude"] = update.message.location.latitude
    context.user_data["longitude"] = update.message.location.longitude
    await update.message.reply_text(
        "Шаг 3 из 6. Введите *номер телефона*.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADD_PHONE


async def add_got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("Шаг 4 из 6. На каких *языках* говорят?", parse_mode="Markdown")
    return ADD_LANGUAGE


async def add_got_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["speaks_language"] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 5 из 6. Выберите *тип сервиса*:",
        parse_mode="Markdown",
        reply_markup=service_type_inline("astype"),
    )
    return ADD_SERVICE_TYPE


async def add_service_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    service = query.data.replace("astype:", "")
    if service not in SERVICE_TYPES:
        return ADD_SERVICE_TYPE
    context.user_data["service_type"] = service
    await query.edit_message_text(f"✅ Тип сервиса: *{service}*", parse_mode="Markdown")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Шаг 6 из 6. *Комментарий*? (или /skip)",
        parse_mode="Markdown",
    )
    return ADD_COMMENT


async def add_comment_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["comment"] = ""
    return await add_save(update, context)


async def add_got_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["comment"] = update.message.text.strip()
    return await add_save(update, context)


async def add_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data
    shop_id = add_shop(
        name=d["name"], latitude=d["latitude"], longitude=d["longitude"],
        phone=d["phone"], speaks_language=d["speaks_language"],
        service_type=d["service_type"], comment=d.get("comment", ""),
    )
    link = maps_link(d["latitude"], d["longitude"])
    await update.message.reply_text(
        f"✅ *Сервис добавлен!* ID: `{shop_id}`\n"
        f"🔧 {d['name']}\n"
        f"🗺 [На карте]({link})",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=main_keyboard(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


# ─── Admin /listrepair & /removerepair ───────────────────────────────────────

async def list_repair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    shops = get_all_shops()
    if not shops:
        await update.message.reply_text("📭 Нет одобренных сервисов.", reply_markup=main_keyboard())
        return
    await update.message.reply_text(f"🗂 *Одобренные сервисы ({len(shops)} шт.):*", parse_mode="Markdown")
    for s in shops:
        await update.message.reply_text(
            shop_text(s, show_id=True),
            parse_mode="Markdown",
            reply_markup=shop_inline_keyboard(s),
        )
    await update.message.reply_text("\u200b", reply_markup=main_keyboard())


@admin_only
async def removerepair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await _do_remove(update, context.args[0])
        return
    shops = get_all_shops()
    if not shops:
        await update.message.reply_text("📭 Нет сервисов для удаления.", reply_markup=main_keyboard())
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"❌ {s['name']}", callback_data=f"remove:{s['id']}")]
        for s in shops
    ])
    await update.message.reply_text("Выберите сервис для удаления:", reply_markup=keyboard)


async def _do_remove(update_or_query, id_str: str) -> None:
    try:
        shop_id = int(id_str)
    except ValueError:
        msg = "❌ Неверный ID."
        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(msg)
        else:
            await update_or_query.edit_message_text(msg)
        return
    shop = get_shop_by_id(shop_id)
    if not shop:
        msg = f"❌ Сервис с ID {shop_id} не найден."
        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(msg)
        else:
            await update_or_query.edit_message_text(msg)
        return
    remove_shop(shop_id)
    msg = f"🗑 *{shop['name']}* (ID `{shop_id}`) удалён."
    if hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        await update_or_query.edit_message_text(msg, parse_mode="Markdown")


async def remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Только для администраторов.", show_alert=True)
        return
    await _do_remove(query, query.data.replace("remove:", ""))


# ─── Fallback ─────────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u:
        upsert_user(u.id, u.username or "", u.full_name or "")
    await update.message.reply_text(
        "Используйте кнопки меню ниже или нажмите 📍 *Найти рядом*.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ─── Error handler ────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update:", exc_info=context.error)
    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    logger.error(tb)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Произошла ошибка. Попробуйте ещё раз или нажмите /start.",
                reply_markup=main_keyboard(),
            )
        except Exception:
            pass


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан.")

    init_db()
    logger.info("База данных инициализирована.")

    if not get_admin_ids():
        logger.warning("ADMIN_IDS не задан — команды администратора недоступны.")

    app = Application.builder().token(token).build()

    find_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📍 Найти рядом$"), find_nearby_button)],
        states={
            FIND_AWAITING_LOCATION: [MessageHandler(filters.LOCATION, handle_location)],
        },
        fallbacks=[CommandHandler("cancel", find_nearby_cancel)],
        allow_reentry=True,
    )

    suggest_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить сервис$"), suggest_start)],
        states={
            SUG_LOCATION:         [MessageHandler(filters.TEXT & ~filters.COMMAND, sug_got_address)],
            SUG_CONFIRM_ADDRESS:  [CallbackQueryHandler(sug_confirm_callback, pattern=r"^sug_confirm:")],
            SUG_NAME:             [MessageHandler(filters.TEXT & ~filters.COMMAND, sug_got_name)],
            SUG_PHONE:            [MessageHandler(filters.TEXT & ~filters.COMMAND, sug_got_phone)],
            SUG_SERVICE_TYPE:     [CallbackQueryHandler(sug_service_type_chosen, pattern=r"^stype:")],
            SUG_LANGUAGE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, sug_got_language)],
            SUG_COMMENT: [
                CommandHandler("skip", sug_comment_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sug_got_comment),
            ],
        },
        fallbacks=[CommandHandler("cancel", suggest_cancel)],
        allow_reentry=True,
    )

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("addrepair", addrepair_start)],
        states={
            ADD_NAME:         [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_name)],
            ADD_LOCATION:     [MessageHandler(filters.LOCATION, add_got_location)],
            ADD_PHONE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_phone)],
            ADD_LANGUAGE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_language)],
            ADD_SERVICE_TYPE: [CallbackQueryHandler(add_service_type_chosen, pattern=r"^astype:")],
            ADD_COMMENT: [
                CommandHandler("skip", add_comment_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_comment),
            ],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
        allow_reentry=True,
    )

    rate_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(rate_start, pattern=r"^rate:")],
        states={
            RATE_AWAITING_STARS:   [CallbackQueryHandler(rate_got_stars, pattern=r"^stars:")],
            RATE_AWAITING_COMMENT: [
                CommandHandler("skip", rate_comment_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, rate_got_comment),
            ],
        },
        fallbacks=[CommandHandler("cancel", rate_cancel)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("listrepair", list_repair))
    app.add_handler(CommandHandler("removerepair", removerepair))
    app.add_handler(find_conv)
    app.add_handler(suggest_conv)
    app.add_handler(add_conv)
    app.add_handler(rate_conv)
    app.add_handler(CallbackQueryHandler(reviews_callback, pattern=r"^reviews:"))
    app.add_handler(CallbackQueryHandler(approve_callback, pattern=r"^approve:"))
    app.add_handler(CallbackQueryHandler(reject_callback, pattern=r"^reject:"))
    app.add_handler(CallbackQueryHandler(remove_callback, pattern=r"^remove:"))
    app.add_handler(MessageHandler(filters.Regex("^📋 Мои заявки$"), my_suggestions))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Помощь$"), help_handler))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
