# -*- coding: utf-8 -*-
import re
import asyncio
import time
import difflib
from typing import Callable, Dict, Any, Awaitable, Union
from aiogram import Router, types, F, Bot, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, ContentType
from config import WARN_LIMIT, OWNER_ID
from database import (
    get_list, manage_warn, get_user, 
    set_moderator_level, get_user_stats_full,
    update_xp, reset_free_dice_cooldown,
    farm_adjust_coins,
    track_recent_message, get_recent_message_ids,
    mark_recent_messages_deleted,
    is_human_verification_pending,
)
from utils import (
    answer_temp, answer_persistent, get_user_link, delete_later,
    parse_command_complex, text_analyzer, moderation_help_text
)
from level_tags import sync_level_tag

router = Router()

# === КОНСТАНТЫ УРОВНЕЙ ДОСТУПА ===
LVL_USER = 0
LVL_HELPER = 1      # Ур1: мут, варн, снятие, профиль, стафф
LVL_MODER = 2       # Ур2: кик, профиль @user
LVL_SENIOR = 3      # Ур3: бан, разбан
LVL_MANAGER = 4     # Менеджер: выдача прав до ур3
LVL_ADMIN = 5       # Владелец

async def get_sender_level(chat: types.Chat, user_id: int) -> int:
    """
    Определяет уровень доступа пользователя.
    """
    # 1. Хардкод
    if user_id == OWNER_ID: 
        return LVL_ADMIN
    if user_id in [1087968824, 777000]: # Group Anonymous Bot, Telegram
        return LVL_ADMIN
    
    # 2. База данных
    user_data = await get_user(user_id)
    db_level = user_data[6] if user_data and len(user_data) > 6 else 0
    
    if db_level > 0:
        return db_level

    # 3. Админы чата
    if chat.type != 'private':
        try:
            member = await chat.get_member(user_id)
            if member.status in ['creator', 'administrator']: 
                return LVL_ADMIN
        except:
            pass
            
    return LVL_USER

async def is_admin(chat: types.Chat, user_id: int, sender_chat: types.Chat = None, required_level: int = 1) -> bool:
    if sender_chat and sender_chat.id == chat.id: 
        return True 
    
    actual_level = await get_sender_level(chat, user_id)
    return actual_level >= required_level

# === ЕДИНАЯ АВТОМОДЕРАЦИЯ ===
def _plain_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def _compact_text(text: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", _plain_text(text))


def _message_urls(message: types.Message):
    """Return URLs hidden behind Telegram text-link entities."""
    entities = message.entities if message.text else message.caption_entities
    urls = []
    for entity in entities or []:
        entity_type = getattr(entity.type, "value", str(entity.type))
        url = getattr(entity, "url", None)
        if entity_type == "text_link" and url:
            urls.append(str(url))
    return urls


def _message_content_for_analysis(message: types.Message) -> str:
    # Media without a caption has no text to analyze. Do not substitute the
    # enum name (for example, "ContentType.PHOTO"): its dot looks like a URL.
    visible = message.text or message.caption or ""
    hidden_urls = _message_urls(message)
    if not hidden_urls:
        return visible
    return f"{visible} {' '.join(hidden_urls)}"


def _is_short_loan_bait(text: str) -> bool:
    """Catch standalone loan bait without treating normal conversation as advertising."""
    value = _compact_text(text)
    return bool(re.fullmatch(
        r"(?:дамвдолг|даювдолг|выдамвдолг|дамзайм|выдамзайм|одолжуденьги)"
        r"(?:сейчас|сегодня|быстро|срочно|моментально|безотказа)?\d*",
        value,
    ))


def _has_newcomer_action(text: str) -> bool:
    """Broad action/recruitment pattern used only for a direct member's first two messages."""
    normalized = _plain_text(text)
    return bool(re.search(
        r"(?:"
        r"\b(?:нужен|нужны|ищу|ищем|набираем|требуется|требуются)\s+"
        r"(?:человек\w*|люд\w*|курьер\w*|доставщик\w*|исполнител\w*|"
        r"сотрудник\w*|работник\w*)|"
        r"\bкто\s.{0,35}\b(?:готов|хочет|может|сможет|играть|поиграть|пойдет)\b|"
        r"\b(?:заходи|заходите|переходи|переходите|жми|жмите|пиши|пишите|"
        r"напиши|напишите|откликайся|откликайтесь|присоединяйся|присоединяйтесь|"
        r"забирай|забирайте|проверяй|проверяйте|отмечай|отмечайте)\b|"
        r"\b(?:давайте|го)\s+\w+|"
        r"\bинформаци\w*\s+(?:в|на)\s+(?:профил\w*|канал\w*|бот\w*)"
        r")",
        normalized,
    ))


def _is_channel_post_comment(message: types.Message) -> bool:
    reply = message.reply_to_message
    if not reply:
        return False
    if getattr(reply, "is_automatic_forward", False):
        return True
    sender_chat = getattr(reply, "sender_chat", None)
    return bool(sender_chat and getattr(sender_chat, "type", None) == "channel")


async def _is_current_chat_member(chat: types.Chat, user_id: int) -> bool:
    """Fail closed for punishment: unknown membership never enables the strict newcomer rule."""
    try:
        member = await chat.get_member(user_id)
        status = getattr(member.status, "value", str(member.status))
        if status in {"creator", "administrator", "member"}:
            return True
        if status == "restricted":
            return bool(getattr(member, "is_member", False))
        return False
    except Exception:
        return False


def _advertising_score(text: str, whitelist=None, compact: bool = False) -> int:
    """Score combinations typical for unsolicited money/job/drug advertising."""
    whitelist = whitelist or []
    normalized = _plain_text(text)

    if compact:
        value = _compact_text(text)
        has_link = any(token in value for token in ("http", "www", "tme"))
        has_bot_handle = bool(re.search(r"[a-z0-9]{3,}bot$", value))
        has_contact_handle = False
        has_cta = any(token in value for token in (
            "влс", "вличку", "пишитевлс", "напишивлс", "кидайплюс",
            "кидайте", "ставьплюс", "отклик",
        ))
        has_mass_offer = any(token in value for token in (
            "раздаю", "каждому", "первым", "всемдам", "скину", "дамденег",
        ))
        has_loan_offer = any(token in value for token in (
            "дамвдолг", "даювдолг", "выдамвдолг", "дамзайм",
            "выдамзайм", "одолжуденьги",
        ))
        has_money = bool(re.search(
            r"(?:\d{2,}(?:руб|рублей|₽|вдень|задень|засмену)|"
            r"\d+(?:usdt|usdc|btc|eth|ton))",
            value,
        ))
        has_work = any(token in value for token in (
            "вакансия", "подработка", "уборка", "заработок", "оплатавдень",
        ))
        has_recruitment = any(token in value for token in (
            "нуженчеловек", "нужнылюди", "нуженкурьер", "требуетсякурьер",
            "требуютсясотрудники", "ищемлюдей", "ищукурьера", "набираемлюдей",
        ))
        has_earning_lure = any(token in value for token in (
            "хочешьзарабатывать", "зарабатываймного", "высокийдоход",
            "легкийзаработок", "деньгикаждыйдень", "платимсразу",
        ))
        has_cash_lure = any(token in value for token in (
            "находноги", "беззалога", "аванссразу", "деньгисразу", "оплатасразу",
        ))
        logistics_hits = sum(token in value for token in (
            "курьер", "проверятьадреса", "проверкаадресов", "устройства", "залог",
        ))
        has_suspicious_logistics = logistics_hits >= 2 or any(token in value for token in (
            "проверятьадреса", "проверкаадресов",
        ))
        has_redirect = any(token in value for token in (
            "заходисюда", "переходисюда", "жмисюда", "запускайбота",
            "подробноститут", "ссылкавпрофиле",
        ))
        has_game_context = any(token in value for token in (
            "вигре", "игровой", "монеты", "опыт", "дуэль", "цех", "ставка",
        ))
        has_drug = any(token in value for token in (
            "закладка", "закладки", "меф", "шишки", "грамм",
        ))
    else:
        has_link = bool(re.search(r"(?:https?://|www\.|t\.me/|\b[a-z0-9-]{2,}\.[a-z]{2,6}\b)", normalized))
        if has_link and any(item.lower() in normalized for item in whitelist):
            has_link = False
        has_bot_handle = bool(re.search(r"@[a-z0-9_]{3,}bot\b", normalized, re.IGNORECASE))
        has_contact_handle = bool(re.search(r"@[a-z0-9_]{4,}\b", normalized, re.IGNORECASE)) and not has_bot_handle
        if any(item.lower() in normalized for item in whitelist):
            has_bot_handle = False
            has_contact_handle = False
        has_cta = bool(re.search(
            r"(?:\bв\s+(?:лс|личк\w*)\b|\b(?:на)?пиш\w*\b|"
            r"\bкида\w*\s*\+|\bстав\w*\s*\+|\bотклик\w*\b)",
            normalized,
        ))
        has_mass_offer = bool(re.search(
            r"\b(?:раздаю|каждому|первым\s*\d*|всем\s+(?:дам|скину)|"
            r"скину|дам\s+денег)\b",
            normalized,
        ))
        has_loan_offer = bool(re.search(
            r"\b(?:(?:дам|даю|выдам|предоставлю)\s+(?:деньги\s+)?в\s+долг|"
            r"(?:дам|выдам|оформлю)\s+(?:займ|кредит)|одолжу\s+(?:деньги|сумму))\b",
            normalized,
        ))
        has_money = bool(re.search(
            r"(?:\b\d{2,}\s*(?:руб\w*|₽)|\b\d{3,}\s*(?:в|за)\s*(?:день|смену)|"
            r"\b\d+(?:[.,]\d+)?\s*(?:usdt|usdc|btc|eth|ton)\b)",
            normalized,
        ))
        has_work = bool(re.search(
            r"\b(?:работа|ваканси\w*|подработк\w*|уборк\w*|заработок|"
            r"оплата\s+(?:в|за)\s+день)\b",
            normalized,
        ))
        has_recruitment = bool(re.search(
            r"\b(?:(?:нужен|нужны|требуется|требуются|ищем|набираем)\s+"
            r"(?:человек|люди|людей|курьер\w*|сотрудник\w*|работник\w*)|"
            r"ищу\s+(?:человека|людей|курьера))\b",
            normalized,
        ))
        has_earning_lure = bool(re.search(
            r"\b(?:хочешь\s+(?:много\s+)?зарабатывать|зарабатывай\s+много|"
            r"высокий\s+доход|легкий\s+заработок|деньги\s+каждый\s+день|"
            r"(?:платим|оплата)\s+сразу)\b",
            normalized,
        ))
        has_cash_lure = bool(re.search(
            r"\b(?:на\s+ход\s+ноги|без\s+залог\w*|аванс\s+сразу|"
            r"деньги\s+сразу|оплата\s+сразу)\b",
            normalized,
        ))
        logistics_signals = (
            bool(re.search(r"\bкурьер\w*\b", normalized)),
            bool(re.search(r"\b(?:провер\w*\s+адрес\w*|адрес\w*\s+провер\w*)\b", normalized)),
            bool(re.search(r"\bустройств\w*\b", normalized)),
            bool(re.search(r"\bзалог\w*\b", normalized)),
        )
        has_suspicious_logistics = sum(logistics_signals) >= 2 or logistics_signals[1]
        has_redirect = bool(re.search(
            r"\b(?:заходи|переходи|жми|открывай|запускай)\s+(?:сюда|тут|бот\w*)|"
            r"\b(?:подробности\s+(?:тут|здесь)|ссылка\s+в\s+профиле)\b",
            normalized,
        ))
        has_game_context = bool(re.search(
            r"\b(?:игр\w*|монет\w*|xp|опыт|дуэл\w*|цех|ставк\w*)\b",
            normalized,
        ))
        has_drug = bool(re.search(
            r"\b(?:закладк\w*|клад\w*|меф\w*|шишк\w*|грамм\w*)\b",
            normalized,
        ))

    score = sum((
        2 if has_link else 0,
        3 if has_bot_handle else 0,
        1 if has_contact_handle else 0,
        2 if has_cta else 0,
        2 if has_mass_offer else 0,
        2 if has_loan_offer else 0,
        2 if has_money else 0,
        2 if has_work else 0,
        2 if has_recruitment else 0,
        2 if has_earning_lure else 0,
        3 if has_cash_lure else 0,
        3 if has_suspicious_logistics else 0,
        2 if has_redirect else 0,
        2 if has_drug else 0,
    ))
    if has_game_context and not any((
        has_money,
        has_recruitment,
        has_cash_lure,
        has_suspicious_logistics,
        has_drug,
    )):
        score = max(0, score - 4)
    return score


async def _delete_message_ids(bot: Bot, chat_id: int, message_ids) -> int:
    deleted = []
    for message_id in dict.fromkeys(int(item) for item in message_ids):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted.append(message_id)
        except Exception:
            continue
    await mark_recent_messages_deleted(chat_id, deleted)
    return len(deleted)


async def _clear_user_messages(
    bot: Bot,
    chat_id: int,
    user_id: int,
    count: int,
    within_seconds: int = 48 * 60 * 60,
) -> int:
    message_ids = await get_recent_message_ids(chat_id, user_id, count, within_seconds)
    return await _delete_message_ids(bot, chat_id, message_ids)


class AutoModerationTracker:
    def __init__(self):
        self.messages = {}
        self.last_behavior_hit = {}

    def _register_behavior_hit(self, key, now: float) -> bool:
        previous = self.last_behavior_hit.get(key, 0)
        self.last_behavior_hit[key] = now
        return now - previous <= 10 * 60

    def _remove_ids(self, key, ids):
        removed = set(ids)
        self.messages[key] = [item for item in self.messages.get(key, []) if item["id"] not in removed]

    def analyze(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        text: str,
        user_level: int,
        badwords,
        whitelist,
        is_reply: bool = False,
        user_xp: int = 0,
        strict_newcomer: bool = False,
    ):
        now = time.time()
        key = (chat_id, user_id)
        history = [item for item in self.messages.get(key, []) if now - item["time"] <= 60]
        normalized = _plain_text(text)
        compact = _compact_text(text)
        history.append({"id": message_id, "time": now, "text": normalized, "compact": compact})
        self.messages[key] = history

        recent_text = [item for item in history if now - item["time"] <= 15 and item["text"]]
        combined = " ".join(item["text"] for item in recent_text)
        is_untrusted_loan_bait = (
            user_level <= 1
            and user_xp < 50
            and not is_reply
            and _is_short_loan_bait(normalized)
        )
        is_direct_newcomer_action = strict_newcomer and _has_newcomer_action(normalized)
        if is_direct_newcomer_action or is_untrusted_loan_bait or _advertising_score(normalized, whitelist) >= 4 or (
            len(recent_text) >= 2 and _advertising_score(combined, whitelist) >= 4
        ):
            return {
                "action": "advertising",
                "ids": [item["id"] for item in recent_text],
                "kick_newcomer": bool(strict_newcomer),
            }

        short_parts = []
        for item in reversed(history):
            if now - item["time"] > 30 or not item["compact"] or len(item["compact"]) > 4:
                break
            short_parts.append(item)
        short_parts.reverse()
        if len(short_parts) >= 2:
            joined_short = "".join(item["compact"] for item in short_parts)
            if text_analyzer.is_bad_word(joined_short, badwords):
                ids = [item["id"] for item in short_parts]
                self._remove_ids(key, ids)
                return {"action": "split_badword", "ids": ids}
            if len(short_parts) >= 3 and _advertising_score(joined_short, whitelist, compact=True) >= 4:
                ids = [item["id"] for item in short_parts]
                self._remove_ids(key, ids)
                return {
                    "action": "advertising",
                    "ids": ids,
                    "kick_newcomer": bool(strict_newcomer),
                }

        letter_parts = []
        for item in reversed(history):
            if now - item["time"] > 12 or not item["compact"] or len(item["compact"]) > 2:
                break
            letter_parts.append(item)
        letter_parts.reverse()
        if len(letter_parts) >= 6:
            ids = [item["id"] for item in letter_parts]
            repeated = self._register_behavior_hit(key, now)
            self._remove_ids(key, ids)
            return {
                "action": "behavior_mute" if repeated else "behavior_warn",
                "reason": "Сообщения по одной букве",
                "ids": ids,
            }

        tier = 2 if user_level >= 4 else 1 if user_level >= 2 else 0
        all_12_limit = (8, 11, 14)[tier]
        all_60_limit = (12, 16, 20)[tier]
        short_12_limit = (5, 7, 9)[tier]
        duplicate_limit = (3, 4, 5)[tier]

        recent_12 = [item for item in history if now - item["time"] <= 12]
        # One- and two-character chains are handled above as a complete
        # fragmented sequence, so the generic short-message limit must not
        # punish the fifth letter before the sixth one arrives.
        short_12 = [item for item in recent_12 if len(item["compact"]) == 3]
        similar = [
            item for item in history
            if normalized and item["text"] and len(normalized) >= 4
            and difflib.SequenceMatcher(None, normalized, item["text"]).ratio() >= 0.9
        ]

        flood_reason = None
        flood_ids = [message_id]
        if len(similar) >= duplicate_limit:
            flood_reason = "Повтор одинаковых сообщений"
            flood_ids = [item["id"] for item in similar]
        elif len(short_12) >= short_12_limit:
            flood_reason = "Слишком много коротких сообщений"
        elif len(recent_12) >= all_12_limit or len(history) >= all_60_limit:
            flood_reason = "Слишком много сообщений подряд"

        if flood_reason:
            repeated = self._register_behavior_hit(key, now)
            if repeated:
                flood_ids = [item["id"] for item in history[-10:]]
            self._remove_ids(key, flood_ids)
            return {
                "action": "behavior_mute" if repeated else "behavior_warn",
                "reason": flood_reason,
                "ids": flood_ids,
            }

        return {"action": "allow", "ids": []}


auto_moderation = AutoModerationTracker()


class FloodMiddleware(BaseMiddleware):
    async def __call__(
        self, 
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]], 
        event: types.Message, 
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, types.Message):
            return await handler(event, data)
        
        if event.chat.type == 'private':
            return await handler(event, data)

        if event.content_type in {ContentType.NEW_CHAT_MEMBERS, ContentType.LEFT_CHAT_MEMBER}:
            return await handler(event, data)

        if event.from_user:
            # Service messages must reach the join/leave handlers. Every ordinary
            # message from an unverified newcomer stops here, before XP/activity.
            if await is_human_verification_pending(event.chat.id, event.from_user.id):
                try:
                    await event.delete()
                except Exception:
                    pass
                await answer_temp(
                    event,
                    "🧩 Сначала нажмите кнопку <b>«Я человек»</b> в приветственном сообщении.",
                    key=f"human_verification:{event.from_user.id}",
                )
                return

            user_data = await get_user(event.from_user.id, event.from_user.username, event.from_user.full_name)
            user_id = event.from_user.id
            message_number = await track_recent_message(
                event.chat.id,
                user_id,
                event.message_id,
                int(event.date.timestamp()),
            )
            is_adm = await is_admin(event.chat, user_id, event.sender_chat, required_level=LVL_HELPER)
            if is_adm:
                return await handler(event, data)

            content = _message_content_for_analysis(event)
            badwords, whitelist = await asyncio.gather(get_list('badwords'), get_list('whitelist'))
            user_level = int(user_data[4] or 1) if user_data else 1
            strict_newcomer = False
            if user_level <= 1 and message_number <= 2 and not _is_channel_post_comment(event):
                strict_newcomer = await _is_current_chat_member(event.chat, user_id)
            decision = auto_moderation.analyze(
                event.chat.id,
                user_id,
                event.message_id,
                content,
                user_level,
                badwords,
                whitelist,
                is_reply=event.reply_to_message is not None,
                user_xp=int(user_data[3] or 0) if user_data else 0,
                strict_newcomer=strict_newcomer,
            )
            action = decision["action"]
            if action == "allow":
                return await handler(event, data)

            user_link = get_user_link(user_id, event.from_user.full_name)
            if action == "advertising":
                deleted = await _clear_user_messages(event.bot, event.chat.id, user_id, 20, 10 * 60)
                try:
                    await event.chat.restrict(
                        user_id=user_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=int(time.time()) + 24 * 60 * 60,
                    )
                    if decision.get("kick_newcomer"):
                        await event.chat.ban(user_id=user_id)
                        await event.chat.unban(user_id)
                        notice = (
                            f"🚪 {user_link} автоматически исключён из чата,\n"
                            f"причина: <i>Рекламный спам сразу после входа</i>. "
                            f"Удалено сообщений: <b>{deleted}</b>."
                        )
                    else:
                        notice = (
                            f"🔇 {user_link} получил блокировку чата на <b>24 часа</b>,\n"
                            f"причина: <i>Рекламный спам</i>. Удалено сообщений: <b>{deleted}</b>."
                        )
                    await answer_temp(
                        event,
                        notice,
                        delay=120,
                        key=f"auto_punishment:{user_id}",
                    )
                except Exception as error:
                    await answer_temp(event, f"Ошибка автомодерации: {error}")
                return

            if action == "split_badword":
                await _delete_message_ids(event.bot, event.chat.id, decision["ids"])
                current_warns = await manage_warn(user_id, "add", reason="Запрещенное слово")
                if current_warns >= WARN_LIMIT:
                    await event.chat.restrict(
                        user_id=user_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=int(time.time()) + 30 * 60,
                    )
                    await manage_warn(user_id, "reset")
                    await answer_temp(
                        event,
                        f"🔇 {user_link} получил блокировку чата на <b>30 мин</b>,\n"
                        f"причина: <i>Запрещенное слово, разбитое на сообщения</i>.",
                        delay=120,
                        key=f"auto_punishment:{user_id}",
                    )
                else:
                    await answer_temp(
                        event,
                        f"⚠️ {user_link} получил предупреждение ({current_warns}/{WARN_LIMIT}),\n"
                        f"причина: <i>Запрещенное слово, разбитое на сообщения</i>.",
                        delay=120,
                        key=f"auto_punishment:{user_id}",
                    )
                return

            await _delete_message_ids(event.bot, event.chat.id, decision["ids"])
            if action == "behavior_warn":
                await answer_temp(event, f"⚠️ {user_link}, не засоряйте чат сообщениями подряд.")
                return

            try:
                await event.chat.restrict(
                    user_id=user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=int(time.time()) + 10 * 60,
                )
                await answer_temp(
                    event,
                    f"🔇 {user_link} получил блокировку чата на <b>10 мин</b>,\n"
                    f"причина: <i>{decision['reason']}</i>.",
                    delay=120,
                    key=f"auto_punishment:{user_id}",
                )
            except Exception as error:
                await answer_temp(event, f"Ошибка автомодерации: {error}")
            return

        return await handler(event, data)

router.message.outer_middleware(FloodMiddleware())


# === ЛОГИКА ФИЛЬТРАЦИИ КОНТЕНТА ===
async def bad_content_checker(message: types.Message) -> Union[bool, Dict[str, Any]]:
    if message.chat.type == 'private': return False
    
    if (message.text and message.text.startswith('/')) or (message.caption and message.caption.startswith('/')):
        return False
    
    user_id = message.from_user.id
    is_adm = await is_admin(message.chat, user_id, message.sender_chat, required_level=LVL_HELPER)
    if is_adm: return False

    visible_text = message.text or message.caption or ""
    text_to_analyze = _message_content_for_analysis(message)
    if not text_to_analyze: return False
    
    reason = None
    whitelist = await get_list('whitelist')
    badwords = await get_list('badwords')
    text_lower = text_to_analyze.lower()
    
    # Ссылки
    link_patterns = [r"(https?://|www\.|t\.me/)[^\s]+", r"[a-zA-Z0-9-]{2,}\.[a-zA-Z]{2,6}\b"]
    is_link = False
    for pattern in link_patterns:
        if re.search(pattern, text_lower):
            is_link = True
            break
            
    if is_link:
        is_allowed = False
        for wl_item in whitelist:
            if wl_item in text_lower:
                is_allowed = True
                break
        if not is_allowed: 
            reason = "Реклама / Ссылки"

    # Маты
    if not reason and text_analyzer.is_bad_word(visible_text, badwords):
        reason = "Запрещенное слово"

    if reason:
        return {'reason': reason}
    
    return False

@router.message(
    F.content_type.in_({'text', 'sticker', 'photo', 'animation', 'video', 'voice', 'video_note', 'document', 'audio'}),
    bad_content_checker
)
async def handle_bad_content(message: types.Message, reason: str):
    await _delete_message_ids(message.bot, message.chat.id, [message.message_id])
    
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    
    current_warns = await manage_warn(user_id, "add", reason=reason)
    # Используем кликабельное имя из БД или текущего сообщения
    user_link_html = get_user_link(user_id, user_name)

    if current_warns >= WARN_LIMIT:
        try:
            until = int(time.time()) + 1800 # 30 минут
            await message.chat.restrict(
                user_id=user_id, 
                permissions=ChatPermissions(can_send_messages=False), 
                until_date=until
            )
            await manage_warn(user_id, "reset")
            # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
            await answer_temp(message,
                f"🔇 {user_link_html} получил блокировку чата на <b>30 мин</b>,\n"
                f"причина: <i>{reason}</i> (Лимит предупреждений).",
                delay=120,
                key=f"auto_punishment:{user_id}",
            )
        except Exception as e:
            await answer_temp(message, f"Ошибка при выдаче наказания: {e}")
    else:
        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
        await answer_temp(message,
            f"⚠️ {user_link_html} получил предупреждение ({current_warns}/{WARN_LIMIT}),\n"
            f"причина: <i>{reason}</i>.",
            delay=120,
            key=f"auto_punishment:{user_id}",
        )


# === КОМАНДЫ МОДЕРАЦИИ ===

@router.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): 
        return await answer_temp(message, "Нет прав (Нужен <b>Moder¹</b>).")

    data = await parse_command_complex(message, command.args)
    if data['parse_error']:
        return await answer_temp(message, data['parse_error'])
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data['target_id'])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя заглушить равного или старшего по званию.")

    duration = data['duration'] if data['duration'] else 600 # default 10 min
    minutes = int(duration / 60)
    
    try:
        await message.chat.restrict(user_id=data['target_id'], permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time())+duration)
        deleted_count = 0
        if data['clear_count']:
            deleted_count = await _clear_user_messages(
                message.bot,
                message.chat.id,
                data['target_id'],
                data['clear_count'],
            )
        elif data['delete_flag'] and message.reply_to_message:
            deleted_count = await _delete_message_ids(
                message.bot,
                message.chat.id,
                [message.reply_to_message.message_id],
            )
        
        target_link = get_user_link(data['target_id'], data['target_name'])
        clear_text = f" Удалено сообщений: <b>{deleted_count}</b>." if data['clear_count'] or data['delete_flag'] else ""
        await answer_persistent(message,
            f"🔇 {target_link} получил блокировку чата на <b>{minutes} мин</b>,\n"
            f"причина: <i>{data['reason']}</i>.{clear_text}"
        )
    except Exception as e: 
        await answer_temp(message, f"Ошибка: {e}")


@router.message(Command("clear"))
async def cmd_clear_user_messages(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER):
        return await answer_temp(message, "Нет прав (Нужен <b>Moder¹</b>).")

    args = (command.args or "").split()
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
        count_token = args[0] if args else ""
    else:
        if len(args) < 2:
            return await answer_temp(message, "Использование: ответьте <code>/clear 10</code> или укажите <code>/clear @user 10</code>.")
        target_data = await parse_command_complex(message, args[0])
        target_id = target_data['target_id']
        target_name = target_data['target_name']
        count_token = args[1]

    if not target_id:
        return await answer_temp(message, "Пользователь не найден.")
    try:
        count = int(count_token)
    except (TypeError, ValueError):
        count = 0
    if not 1 <= count <= 100:
        return await answer_temp(message, "Количество сообщений должно быть от 1 до 100.")

    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, target_id)
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя очищать сообщения равного или старшего модератора.")

    deleted = await _clear_user_messages(message.bot, message.chat.id, target_id, count)
    target_link = get_user_link(target_id, target_name)
    await answer_temp(message, f"🧹 Удалено сообщений пользователя {target_link}: <b>{deleted}</b> из {count}.")

@router.message(Command("warn"))
async def cmd_warn(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): 
        return await answer_temp(message, "Нет прав (Нужен <b>Moder¹</b>).")
    
    data = await parse_command_complex(message, command.args)
    if data['parse_error']:
        return await answer_temp(message, data['parse_error'])
    if data['clear_count']:
        return await answer_temp(message, "Параметр -clear доступен только для команды /mute.")
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data['target_id'])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя выдать предупреждение равному или старшему.")
    
    cnt = await manage_warn(data['target_id'], "add", reason=data['reason'])
    target_link = get_user_link(data['target_id'], data['target_name'])
    
    if data['delete_flag'] and message.reply_to_message:
        await _delete_message_ids(message.bot, message.chat.id, [message.reply_to_message.message_id])
        
    if cnt >= WARN_LIMIT:
        until = int(time.time()) + 1800
        try:
            await message.chat.restrict(
                user_id=data['target_id'], 
                permissions=ChatPermissions(can_send_messages=False), 
                until_date=until
            )
            await manage_warn(data['target_id'], "reset")
            # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
            await answer_persistent(message,
                f"🔇 {target_link} получил блокировку чата на <b>30 мин</b>,\n"
                f"причина: <i>{data['reason']}</i> (Лимит предупреждений)."
            )
        except Exception as e:
            await answer_temp(message, f"Ошибка блокировки: {e}")
    else:
        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
        await answer_persistent(message,
            f"⚠️ {target_link} получил предупреждение ({cnt}/{WARN_LIMIT}),\n"
            f"причина: <i>{data['reason']}</i>."
        )

@router.message(Command("unwarn"))
async def cmd_unwarn(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): 
        return
        
    data = await parse_command_complex(message, command.args)
    if data['parse_error']:
        return await answer_temp(message, data['parse_error'])
    if data['clear_count']:
        return await answer_temp(message, "Параметр -clear доступен только для команды /mute.")
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    action = "reset" if "all" in (command.args or "").lower() else "remove"
    cnt = await manage_warn(data['target_id'], action)
    target_link = get_user_link(data['target_id'], data['target_name'])
    await answer_temp(message, f"✅ Предупреждение снято для {target_link}. Текущее количество: {cnt}")

@router.message(Command("unmute"))
async def cmd_unmute(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): 
        return
        
    data = await parse_command_complex(message, command.args)
    if data['parse_error']:
        return await answer_temp(message, data['parse_error'])
    if data['clear_count']:
        return await answer_temp(message, "Параметр -clear доступен только для команды /mute.")
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    await message.chat.restrict(
        user_id=data['target_id'], 
        permissions=ChatPermissions(
            can_send_messages=True, 
            can_send_media_messages=True, 
            can_send_other_messages=True, 
            can_add_web_page_previews=True
        )
    )
    target_link = get_user_link(data['target_id'], data['target_name'])
    # ИЗМЕНЕНО: СЛЕНГ УБРАН
    await answer_temp(message, f"🔊 Ограничения чата сняты с пользователя: {target_link}")

# --- УРОВЕНЬ 2: МОДЕРАТОР (Kick) ---

@router.message(Command("kick"))
async def cmd_kick(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_MODER):
        return await answer_temp(message, "Нужен уровень <b>Moder²</b>.")

    data = await parse_command_complex(message, command.args)
    if data['parse_error']:
        return await answer_temp(message, data['parse_error'])
    if data['clear_count']:
        return await answer_temp(message, "Параметр -clear доступен только для команды /mute.")
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data['target_id'])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя исключить равного или старшего.")

    try:
        await message.chat.ban(user_id=data['target_id'])
        await message.chat.unban(data['target_id']) 
        if data['delete_flag'] and message.reply_to_message:
            await _delete_message_ids(message.bot, message.chat.id, [message.reply_to_message.message_id])
            
        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
        target_link = get_user_link(data['target_id'], data['target_name'])
        await answer_persistent(message,
            f"🚪{target_link} был исключен из чата,\n"
            f"причина: <i>{data['reason']}</i>."
        )
    except Exception as e: 
        await answer_temp(message, f"Ошибка: {e}")

# --- УРОВЕНЬ 3: СТАРШИЙ МОДЕРАТОР (Ban, Unban) ---

@router.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_SENIOR):
        return await answer_temp(message, "Нужен уровень <b>Moder³</b>.")

    data = await parse_command_complex(message, command.args)
    if data['parse_error']:
        return await answer_temp(message, data['parse_error'])
    if data['clear_count']:
        return await answer_temp(message, "Параметр -clear доступен только для команды /mute.")
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data['target_id'])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя заблокировать равного или старшего.")

    try:
        until = int(time.time()) + data['duration'] if data['duration'] else 0
        if until > 0: 
            await message.chat.ban(user_id=data['target_id'], until_date=until)
            days = int(data['duration'] / 86400)
            time_str = f"на {days} дней" if days > 0 else "временно"
        else: 
            await message.chat.ban(user_id=data['target_id'])
            time_str = "навсегда"
        
        if data['delete_flag'] and message.reply_to_message:
            await _delete_message_ids(message.bot, message.chat.id, [message.reply_to_message.message_id])
            
        target_link = get_user_link(data['target_id'], data['target_name'])
        
        # ИЗМЕНЕНО: СЛЕНГ УБРАН, ДОБАВЛЕНО ФОРМАТИРОВАНИЕ
        await answer_persistent(message,
            f"⛔ {target_link} был заблокирован <b>{time_str}</b>,\n"
            f"причина: <i>{data['reason']}</i>."
        )
    except Exception as e: 
        await answer_temp(message, f"Ошибка: {e}")

@router.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_SENIOR): 
        return
        
    data = await parse_command_complex(message, command.args)
    if not data['target_id']: 
        return await answer_temp(message, "Укажите цель.")
    try:
        await message.chat.unban(data['target_id'])
        target_link = get_user_link(data['target_id'], data['target_name'])
        # ИЗМЕНЕНО: СЛЕНГ УБРАН
        await answer_temp(message, f"✅ Блокировка снята с пользователя: {target_link}")
    except: 
        await answer_temp(message, "Ошибка снятия блокировки.")


@router.message(Command("resetdice", "resetcubecd", "freedicereset"))
async def cmd_reset_free_dice_cd(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_SENIOR):
        return await answer_temp(message, "Нужен уровень <b>Moder³</b>.")

    data = await parse_command_complex(message, command.args)
    if not data["target_id"]:
        return await answer_temp(message, "Укажите цель.")

    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data["target_id"])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя сбросить КД равному или старшему.")

    await reset_free_dice_cooldown(data["target_id"])
    target_link = get_user_link(data["target_id"], data["target_name"])
    await answer_temp(message, f"🎁 КД бесплатного кубика сброшен для {target_link}.")

# --- УРОВЕНЬ МЕНЕДЖЕРА: ВЫДАЧА ПРАВ (promote) ---

@router.message(Command("promote", "setlevel"))
async def cmd_promote(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    if sender_lvl < LVL_MANAGER:
        return await answer_temp(message, "Доступно только <b>Manager</b> и выше.")

    args = command.args.split() if command.args else []
    if len(args) < 2:
        return await answer_temp(message, "Использование: <code>/setlevel @user [0-3]</code>")

    try:
        new_level = int(args[-1])
    except ValueError:
        return await answer_temp(message, "Уровень должен быть числом.")

    user_str = " ".join(args[:-1])
    fake_msg = message.model_copy(update={'text': f"/cmd {user_str}"})
    data = await parse_command_complex(fake_msg, user_str)
    
    if not data['target_id']:
         return await answer_temp(message, "Пользователь не найден.")

    if sender_lvl == LVL_MANAGER:
        if new_level >= LVL_MANAGER:
            return await answer_temp(message, "Manager может назначать только до 3 уровня.")
        target_current_lvl = await get_sender_level(message.chat, data['target_id'])
        if target_current_lvl >= LVL_MANAGER:
            return await answer_temp(message, "Нельзя менять права равного или старшего.")

    await set_moderator_level(data['target_id'], new_level)
    
    # Новые названия ролей
    role_name = "Пользователь"
    if new_level == 1: role_name = "Moder¹"
    if new_level == 2: role_name = "Moder²"
    if new_level == 3: role_name = "Moder³"
    
    target_link = get_user_link(data['target_id'], data['target_name'])
    await answer_temp(message, f"🆙 Пользователю {target_link} установлен уровень <b>{new_level} ({role_name})</b>.")

@router.message(Command("addxp", "givexp", "addexp"))
async def cmd_addxp(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_MANAGER):
        return await answer_temp(message, "Доступно с уровня <b>Manager (4)</b>.")

    args = command.args.split() if command.args else []
    if len(args) < 2:
        return await answer_temp(message, "Использование: <code>/addxp @user [кол-во]</code>")

    try:
        amount = int(args[-1])
    except ValueError:
        return await answer_temp(message, "Сумма XP должна быть целым числом.")

    user_str = " ".join(args[:-1])
    fake_msg = message.model_copy(update={'text': f"/cmd {user_str}"})
    data = await parse_command_complex(fake_msg, user_str)
    
    if not data['target_id']:
         return await answer_temp(message, "Пользователь не найден.")

    old_lvl, new_lvl, _ = await update_xp(data['target_id'], amount)
    if message.chat.type != "private":
        await sync_level_tag(
            message.bot,
            message.chat.id,
            data["target_id"],
            new_lvl,
            force=True,
        )
    
    target_link = get_user_link(data['target_id'], data['target_name'])
    msg_text = f"💳 Администратор выдал <code>{amount} XP</code> пользователю {target_link}."
    
    if new_lvl > old_lvl:
        msg_text += f"\n\n🆙 <b>Уровень повышен до {new_lvl}!</b>"
    elif new_lvl < old_lvl:
        msg_text += f"\n\n📉 <b>Уровень понижен до {new_lvl}...</b>"
    await answer_temp(message, msg_text)


@router.message(Command("addcoins", "givecoins", "farmcoins"))
async def cmd_addcoins(message: types.Message, command: CommandObject):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_SENIOR):
        return await answer_temp(message, "Доступно с уровня <b>Moder³ (3)</b>.")

    args = command.args.split() if command.args else []
    if len(args) < 2:
        return await answer_temp(message, "Использование: <code>/addcoins @user [количество]</code>")

    try:
        amount = int(args[-1])
    except ValueError:
        return await answer_temp(message, "Сумма монет должна быть целым числом.")

    if amount <= 0:
        return await answer_temp(message, "Сумма должна быть больше 0.")

    user_str = " ".join(args[:-1])
    fake_msg = message.model_copy(update={'text': f"/cmd {user_str}"})
    data = await parse_command_complex(fake_msg, user_str)
    if not data['target_id']:
        return await answer_temp(message, "Пользователь не найден.")

    sender_lvl = await get_sender_level(message.chat, message.from_user.id)
    target_lvl = await get_sender_level(message.chat, data['target_id'])
    if target_lvl >= sender_lvl and message.from_user.id != OWNER_ID:
        return await answer_temp(message, "Нельзя выдать монеты равному или старшему.")

    new_balance = await farm_adjust_coins(data["target_id"], amount)
    target_link = get_user_link(data["target_id"], data["target_name"])
    await answer_temp(
        message,
        f"💰 Выдано <code>{amount}</code> монет игроку {target_link}.\n"
        f"Новый баланс фермы: <code>{new_balance}</code> монет.",
    )

@router.message(Command("modhelp"))
async def cmd_modhelp(message: types.Message):
    await delete_later(message, 0)
    if not await is_admin(message.chat, message.from_user.id, message.sender_chat, required_level=LVL_HELPER): return
    
    text = moderation_help_text()
    
    await answer_temp(
        message, 
        text, 
        delay=60,
    )
