#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Совместимо с python-telegram-bot v12.8 и Python 3.6
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
    CallbackQueryHandler,
)
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InputMediaPhoto
)
import face_recognition
import logging
from io import BytesIO
from PIL import Image
import numpy as np
import sqlite3
import os
import random  # Для выбора случайного фото

# === Настройка логирования ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Конфигурация ===
TOKEN = 'YOUR_BOT_TOKEN'  # Замените на ваш токен
OWNER_ID = 123456789  # Замените на ваш ID
DB_NAME = "bot_data.db"
MEDIA_GROUP_SIZE = 10
REQUIRED_USER_PHOTOS = 2  # Минимум 2 фото от пользователя
MIN_ABOUT_TEXT_LENGTH = 50  # Минимальная длина текста "О себе" пользователя

# Состояния для ConversationHandler (владелец)
SET_PHOTO, SET_INTERESTS, SET_LOOKING_FOR, SET_OWNER_ABOUT = range(4)  # 0, 1, 2, 3
CONFIRM_CLEAR_PHOTOS, CONFIRM_CLEAR_INTERESTS, CONFIRM_CLEAR_LOOKING_FOR, CONFIRM_CLEAR_OWNER_ABOUT = range(4,
                                                                                                            8)  # 4, 5, 6, 7

# Состояния пользователя (в user_data)
# USER_STATES: 'awaiting_verification' -> 'in_main_menu'
# Дополнительные состояния для редактирования "О себе"
AWAITING_NEW_ABOUT_TEXT = 10  # Новое состояние для ожидания нового текста

# Глобальные переменные для данных владельца (загружаются из БД при запуске)
owner_data = {
    'photos': [],
    'interests': "",
    'looking_for': "",
    'about': ""  # Новое поле для "о себе" владельца
}


# === Работа с базой данных SQLite ===
def init_db():
    """Создает подключение к БД и таблицы, если они не существуют."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS owner (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                interests TEXT,
                looking_for TEXT,
                about TEXT -- Новое поле для "о себе" владельца
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS owner_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL UNIQUE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS interested_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                photo_file_id TEXT,
                about_text TEXT -- Добавляем поле для текста "о себе" пользователя
            )
        ''')
        conn.commit()
        logger.info(f"База данных '{DB_NAME}' инициализирована.")
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}")


def save_owner_data(key, value):
    """Сохраняет interests, looking_for или about владельца в БД."""
    if key not in ['interests', 'looking_for', 'about']:
        logger.error(f"Попытка сохранить неизвестное поле owner_ {key}")
        return
    try:
        # Сначала загрузим текущие данные, чтобы не потерять другие поля
        current_data = load_owner_data()
        logger.debug(f"Текущие данные владельца перед сохранением '{key}': {current_data}")

        # Обновим только переданное поле
        current_data[key] = value
        logger.debug(f"Данные владельца после обновления '{key}': {current_data}")

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # INSERT OR REPLACE с ВСЕМИ полями, чтобы не потерять данные
        cursor.execute('''
            INSERT OR REPLACE INTO owner (id, interests, looking_for, about) 
            VALUES (1, ?, ?, ?)
        ''', (current_data['interests'], current_data['looking_for'], current_data['about']))
        conn.commit()
        conn.close()
        logger.info(f"Поле '{key}' владельца сохранено/обновлено в БД. Новое значение: '{value}'")
        # Дополнительная проверка: загрузим и проверим
        check_data = load_owner_data()
        if check_data[key] == value:
            logger.debug(f"Проверка сохранения '{key}': УСПЕШНО")
        else:
            logger.warning(
                f"Проверка сохранения '{key}': НЕ УДАЛОСЬ! Ожидаемое: '{value}', Полученное: '{check_data[key]}'")
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при сохранении поля '{key}' владельца: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при сохранении поля '{key}' владельца: {e}")


def load_owner_data():
    """Загружает interests, looking_for и about владельца из БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # Добавлено поле 'about' в SELECT
        cursor.execute('SELECT interests, looking_for, about FROM owner WHERE id = 1')
        row = cursor.fetchone()
        conn.close()
        if row:
            data = {
                'interests': row[0] if row[0] else "",
                'looking_for': row[1] if row[1] else "",
                'about': row[2] if row[2] else ""  # Загружаем "о себе" владельца
            }
            logger.debug(f"Загружены данные владельца из БД: {data}")
            return data
        else:
            logger.debug("В таблице owner нет записи с id=1")
            return {'interests': "", 'looking_for': "", 'about': ""}  # Инициализируем 'about'
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при загрузке данных владельца: {e}")
        # Возвращаем значения по умолчанию в случае ошибки
        return {'interests': "", 'looking_for': "", 'about': ""}
    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке данных владельца: {e}")
        return {'interests': "", 'looking_for': "", 'about': ""}


def save_owner_photo(file_id):
    """Добавляет file_id фото владельца в БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO owner_photos (file_id) VALUES (?)', (file_id,))
        conn.commit()
        rows_affected = cursor.rowcount
        conn.close()
        if rows_affected > 0:
            logger.info(f"Фото владельца (ID: {file_id[:10]}...) добавлено в БД.")
        else:
            logger.info(f"Фото владельца (ID: {file_id[:10]}...) уже существует в БД.")
        # Дополнительная проверка: загрузим и проверим
        current_photos = load_owner_photos()
        if file_id in current_photos:
            logger.debug(f"Проверка сохранения фото: УСПЕШНО")
        else:
            logger.warning(f"Проверка сохранения фото: НЕ УДАЛОСЬ! ID {file_id[:10]}... не найден в списке.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при добавлении фото владельца: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при добавлении фото владельца: {e}")


def load_owner_photos():
    """Загружает список file_id фото владельца из БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT file_id FROM owner_photos')
        rows = cursor.fetchall()
        conn.close()
        photo_ids = [row[0] for row in rows]
        logger.debug(f"Загружено {len(photo_ids)} фото владельца из БД.")
        return photo_ids
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при загрузке фото владельца: {e}")
        return []
    except Exception as e:
        logger.error(f"Неожиданная ошибка при загрузке фото владельца: {e}")
        return []


def clear_owner_photos_from_db():
    """Очищает все фото владельца из БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM owner_photos')
        rows_deleted = cursor.rowcount
        conn.commit()
        conn.close()
        logger.info(f"Из БД удалено {rows_deleted} фото владельца.")
        # Дополнительная проверка: загрузим и проверим
        current_photos = load_owner_photos()
        if len(current_photos) == 0:
            logger.debug(f"Проверка очистки фото: УСПЕШНО")
        else:
            logger.warning(f"Проверка очистки фото: НЕ УДАЛОСЬ! Осталось {len(current_photos)} фото.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при удалении фото владельца из БД: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при удалении фото владельца из БД: {e}")


def save_interested_user(user_id, username, photo_file_id, about_text):
    """Сохраняет информацию о заинтересованном пользователе в БД."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # Используем INSERT OR REPLACE
        cursor.execute('''
            INSERT OR REPLACE INTO interested_users (user_id, username, photo_file_id, about_text)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, photo_file_id, about_text))
        conn.commit()
        conn.close()
        logger.info(f"Информация о заинтересованном пользователе {username} ({user_id}) сохранена в БД.")
        # Дополнительная проверка: загрузим и проверим (упрощенно)
        # (Можно добавить более сложную проверку, если нужно)
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при сохранении заинтересованного пользователя {user_id}: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при сохранении заинтересованного пользователя {user_id}: {e}")


def clear_owner_field_from_db(field_name):
    """Очищает указанное текстовое поле (interests, looking_for, about) владельца в БД."""
    # Добавлено 'about' в список разрешенных полей для очистки
    if field_name not in ['interests', 'looking_for', 'about']:
        logger.error(f"Попытка очистить неизвестное поле: {field_name}")
        return
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        # Обновляем только указанное поле на NULL для владельца с id=1
        cursor.execute(f'UPDATE owner SET {field_name} = NULL WHERE id = 1')
        conn.commit()
        conn.close()
        logger.info(f"Поле '{field_name}' владельца удалено из БД (установлено в NULL).")
        # Дополнительная проверка: загрузим и проверим
        check_data = load_owner_data()
        if not check_data[field_name]:
            logger.debug(f"Проверка очистки '{field_name}': УСПЕШНО")
        else:
            logger.warning(f"Проверка очистки '{field_name}': НЕ УДАЛОСЬ! Значение: '{check_data[field_name]}'")
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при удалении поля '{field_name}' владельца из БД: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при удалении поля '{field_name}' владельца из БД: {e}")


# === Функции владельца ===
def owner_start(update, context):
    """Старт для владельца - настройка данных."""
    keyboard = [
        # Первая строка: Установить / Посмотреть фото
        [KeyboardButton("Установить фото"), KeyboardButton("Посмотреть текущие фото")],
        # Вторая строка: Установить / Посмотреть интересы
        [KeyboardButton("Установить интересы"), KeyboardButton("Посмотреть текущие интересы")],
        # Третья строка: Установить / Посмотреть "кого ищу"
        [KeyboardButton("Добавить, кого ищу"), KeyboardButton("Посмотреть, кого ищу")],
        # Четвертая строка: Установить / Посмотреть / Очистить "о себе"
        [KeyboardButton("Добавить о себе"), KeyboardButton("Посмотреть о себе")],
        # Пятая строка: Все кнопки "Очистить" (внизу, как и просили)
        [KeyboardButton("Очистить фото"), KeyboardButton("Очистить интересы"), KeyboardButton("Очистить, кого ищу"), KeyboardButton("Очистить о себе")],
        # Шестая строка: Главное меню
        [KeyboardButton("Главное меню")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    update.message.reply_text(
        "Привет, владелец! Здесь ты можешь настроить свои данные.",
        reply_markup=reply_markup
    )


def _send_photos_in_groups(context, chat_id, photo_ids, caption_prefix="Фото"):
    """Вспомогательная функция для отправки фото группами."""
    if not photo_ids:
        return
    for i in range(0, len(photo_ids), MEDIA_GROUP_SIZE):
        group = photo_ids[i:i + MEDIA_GROUP_SIZE]
        if len(group) == 1:
            context.bot.send_photo(chat_id=chat_id, photo=group[0], caption=f"{caption_prefix} #{i + 1}")
        else:
            try:
                media_group = [
                    InputMediaPhoto(media=photo_id, caption=f"{caption_prefix} #{i + j + 1}" if j == 0 else None)
                    for j, photo_id in enumerate(group)
                ]
                context.bot.send_media_group(chat_id=chat_id, media=media_group)
            except Exception as e:
                logger.warning(f"Не удалось отправить группу фото: {e}. Отправка по одному.")
                for j, photo_id in enumerate(group):
                    context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=f"{caption_prefix} #{i + j + 1}")


def view_current_photos(update, context):
    """Отправляет владельцу его текущие фото, группируя их по возможности."""
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    if not owner_data['photos']:
        update.message.reply_text("У вас пока нет установленных фото.")
        return
    logger.info(f"Владелец просматривает свои текущие фото ({len(owner_data['photos'])} шт.).")
    update.message.reply_text(f"Ваши текущие фото ({len(owner_data['photos'])}):")
    _send_photos_in_groups(context, update.effective_message.chat_id, owner_data['photos'], "Фото владельца")


def view_current_interests(update, context):
    """Отправляет владельцу его текущие интересы."""
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    if not owner_data['interests']:
        update.message.reply_text("У вас пока не установлены интересы.")
        return
    logger.info(f"Владелец просматривает свои текущие интересы.")
    update.message.reply_text(f"Ваши текущие интересы:\n{owner_data['interests']}")


def view_looking_for(update, context):
    """Отправляет владельцу текст 'кого ищу'."""
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    if not owner_data['looking_for']:
        update.message.reply_text("Вы пока не указали, кого ищете.")
        return
    logger.info(f"Владелец просматривает 'кого ищу'.")
    update.message.reply_text(f"Вы ищете:\n{owner_data['looking_for']}")


def view_owner_about(update, context):
    """Отправляет владельцу текст 'о себе'."""
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    if not owner_data['about']:
        update.message.reply_text("Вы пока не указали 'о себе'.")
        return
    logger.info(f"Владелец просматривает 'о себе'.")
    update.message.reply_text(f"Ваш текст 'о себе':\n{owner_data['about']}")


def set_photo_start(update, context):
    """Начало установки фото владельца."""
    update.message.reply_text("Отправь мне фото, которое будет отображаться другим пользователям.")
    return SET_PHOTO


def set_photo_received(update, context):
    """Получение и сохранение фото владельца."""
    photo_file_id = update.message.photo[-1].file_id
    owner_data['photos'].append(photo_file_id)
    save_owner_photo(photo_file_id)
    logger.info(f"Фото добавлено владельцем. Всего фото: {len(owner_data['photos'])}")
    update.message.reply_text("Фото добавлено!")
    owner_start(update, context)
    return ConversationHandler.END


def set_interests_start(update, context):
    """Начало установки интересов владельца."""
    update.message.reply_text("Отправь мне текст с твоими интересами.")
    return SET_INTERESTS


def set_interests_received(update, context):
    """Получение и сохранение интересов владельца."""
    interests_text = update.message.text
    owner_data['interests'] = interests_text
    save_owner_data('interests', interests_text)
    logger.info(f"Интересы владельца обновлены: {owner_data['interests'][:20]}...")
    update.message.reply_text("Интересы обновлены!")
    owner_start(update, context)
    return ConversationHandler.END


def set_looking_for_start(update, context):
    """Начало установки 'кого ищу' владельца."""
    update.message.reply_text("Опишите, кого вы ищете.")
    return SET_LOOKING_FOR


def set_looking_for_received(update, context):
    """Получение и сохранение 'кого ищу' владельца."""
    looking_for_text = update.message.text
    owner_data['looking_for'] = looking_for_text
    save_owner_data('looking_for', looking_for_text)
    logger.info(f"'Кого ищу' владельца обновлено: {owner_data['looking_for'][:20]}...")
    update.message.reply_text("'Кого ищу' обновлено!")
    owner_start(update, context)
    return ConversationHandler.END


def set_owner_about_start(update, context):
    """Начало установки 'о себе' владельца."""
    update.message.reply_text("Напишите текст 'о себе'.")
    return SET_OWNER_ABOUT


def set_owner_about_received(update, context):
    """Получение и сохранение 'о себе' владельца."""
    about_text = update.message.text
    owner_data['about'] = about_text
    save_owner_data('about', about_text)
    logger.info(f"'О себе' владельца обновлено: {owner_data['about'][:20]}...")
    update.message.reply_text("'О себе' обновлено!")
    owner_start(update, context)
    return ConversationHandler.END


def confirm_clear_photos_start(update, context):
    """Начало процесса подтверждения очистки фото."""
    keyboard = [[KeyboardButton("Да"), KeyboardButton("Нет")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text("⚠️ Вы уверены, что хотите очистить ВСЕ ваши фото?", reply_markup=reply_markup)
    return CONFIRM_CLEAR_PHOTOS


def confirm_clear_photos_received(update, context):
    """Обработка ответа на подтверждение очистки фото."""
    answer = update.message.text
    if answer == "Да":
        owner_data['photos'].clear()
        clear_owner_photos_from_db()
        logger.info("Владелец подтвердил очистку фото.")
        update.message.reply_text("✅ Все фото владельца очищены.", reply_markup=ReplyKeyboardRemove())
    elif answer == "Нет":
        logger.info("Владелец отменил очистку фото.")
        update.message.reply_text("❌ Очистка фото отменена.", reply_markup=ReplyKeyboardRemove())
    else:
        update.message.reply_text("Пожалуйста, ответьте 'Да' или 'Нет'.", reply_markup=ReplyKeyboardRemove())
        return CONFIRM_CLEAR_PHOTOS
    owner_start(update, context)
    return ConversationHandler.END


def confirm_clear_interests_start(update, context):
    """Начало процесса подтверждения очистки интересов."""
    keyboard = [[KeyboardButton("Да"), KeyboardButton("Нет")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text("⚠️ Вы уверены, что хотите очистить ваши интересы?", reply_markup=reply_markup)
    return CONFIRM_CLEAR_INTERESTS


def confirm_clear_interests_received(update, context):
    """Обработка ответа на подтверждение очистки интересов."""
    answer = update.message.text
    if answer == "Да":
        owner_data['interests'] = ""
        clear_owner_field_from_db('interests')
        logger.info("Владелец подтвердил очистку интересов.")
        update.message.reply_text("✅ Интересы владельца очищены.", reply_markup=ReplyKeyboardRemove())
    elif answer == "Нет":
        logger.info("Владелец отменил очистку интересов.")
        update.message.reply_text("❌ Очистка интересов отменена.", reply_markup=ReplyKeyboardRemove())
    else:
        update.message.reply_text("Пожалуйста, ответьте 'Да' или 'Нет'.", reply_markup=ReplyKeyboardRemove())
        return CONFIRM_CLEAR_INTERESTS
    owner_start(update, context)
    return ConversationHandler.END


def confirm_clear_looking_for_start(update, context):
    """Начало процесса подтверждения очистки 'кого ищу'."""
    keyboard = [[KeyboardButton("Да"), KeyboardButton("Нет")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text("⚠️ Вы уверены, что хотите очистить 'кого ищу'?", reply_markup=reply_markup)
    return CONFIRM_CLEAR_LOOKING_FOR


def confirm_clear_looking_for_received(update, context):
    """Обработка ответа на подтверждение очистки 'кого ищу'."""
    answer = update.message.text
    if answer == "Да":
        owner_data['looking_for'] = ""
        clear_owner_field_from_db('looking_for')
        logger.info("Владелец подтвердил очистку 'кого ищу'.")
        update.message.reply_text("✅ 'Кого ищу' очищено.", reply_markup=ReplyKeyboardRemove())
    elif answer == "Нет":
        logger.info("Владелец отменил очистку 'кого ищу'.")
        update.message.reply_text("❌ Очистка 'кого ищу' отменена.", reply_markup=ReplyKeyboardRemove())
    else:
        update.message.reply_text("Пожалуйста, ответьте 'Да' или 'Нет'.", reply_markup=ReplyKeyboardRemove())
        return CONFIRM_CLEAR_LOOKING_FOR
    owner_start(update, context)
    return ConversationHandler.END


def confirm_clear_owner_about_start(update, context):
    """Начало процесса подтверждения очистки 'о себе'."""
    keyboard = [[KeyboardButton("Да"), KeyboardButton("Нет")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    update.message.reply_text("⚠️ Вы уверены, что хотите очистить 'о себе'?", reply_markup=reply_markup)
    return CONFIRM_CLEAR_OWNER_ABOUT


def confirm_clear_owner_about_received(update, context):
    """Обработка ответа на подтверждение очистки 'о себе'."""
    answer = update.message.text
    if answer == "Да":
        owner_data['about'] = ""
        clear_owner_field_from_db('about')
        logger.info("Владелец подтвердил очистку 'о себе'.")
        update.message.reply_text("✅ 'О себе' очищено.", reply_markup=ReplyKeyboardRemove())
    elif answer == "Нет":
        logger.info("Владелец отменил очистку 'о себе'.")
        update.message.reply_text("❌ Очистка 'о себе' отменена.", reply_markup=ReplyKeyboardRemove())
    else:
        update.message.reply_text("Пожалуйста, ответьте 'Да' или 'Нет'.", reply_markup=ReplyKeyboardRemove())
        return CONFIRM_CLEAR_OWNER_ABOUT
    owner_start(update, context)
    return ConversationHandler.END


# === Функции для пользователей ===
def start(update, context):
    """Старт для обычных пользователей и владельца."""
    user_id = update.effective_user.id
    username = update.effective_user.username or "Без_имени"
    if user_id == OWNER_ID:
        owner_start(update, context)
        return
    # Получаем актуальное количество фото владельца
    num_owner_photos = len(owner_data['photos'])
    if num_owner_photos == 0:
        update.message.reply_text(
            f"Привет, {username}! Владелец пока не добавил свои фото. Попробуйте позже."
        )
        return
    update.message.reply_text(
        f"Привет, {username}! Отправь мне {REQUIRED_USER_PHOTOS} фото, и я проверю, есть ли на них лица. "
        f"Взамен вы получите по одному фото владельца за каждое ваше проверенное фото. "
        f"Вам нужно отправить ровно {REQUIRED_USER_PHOTOS} фото для продолжения."
    )
    # Инициализируем данные пользователя
    context.user_data['state'] = 'awaiting_verification'
    context.user_data['verified_photos'] = []  # Список проверенных фото пользователя
    context.user_data['sent_owner_photos'] = []  # Список ID фото владельца, которые уже были отправлены пользователю
    context.user_data['about_text'] = ""  # Текст "о себе"
    context.user_data['has_expressed_interest'] = False  # Флаг интереса


def send_random_owner_photo(context, chat_id, sent_owner_photos_list, all_owner_photos):
    """Отправляет пользователю одно случайное фото владельца, которого ещё не было."""
    if not all_owner_photos:
        logger.warning("Нет фото владельца для отправки.")
        context.bot.send_message(chat_id=chat_id, text="У владельца пока нет фото для обмена.")
        return None
    # Получаем список ещё не отправленных фото
    remaining_photos = list(set(all_owner_photos) - set(sent_owner_photos_list))
    if not remaining_photos:
        logger.info("Все фото владельца уже были отправлены пользователю.")
        context.bot.send_message(chat_id=chat_id, text="К сожалению, все фото владельца уже были отправлены вам.")
        return None
    # Выбираем случайное фото из оставшихся
    random_photo_id = random.choice(remaining_photos)
    try:
        context.bot.send_photo(chat_id=chat_id, photo=random_photo_id, caption="Вот фото владельца в обмен на ваше:")
        logger.info(f"Отправлено случайное фото владельца (ID: {random_photo_id[:10]}...) пользователю.")
        return random_photo_id
    except Exception as e:
        logger.error(f"Ошибка при отправке случайного фото владельца пользователю: {e}")
        context.bot.send_message(chat_id=chat_id, text="Ошибка при отправке фото владельца.")
        return None


def handle_photo(update, context):
    """Обрабатывает входящие фотографии от пользователей."""
    user_id = update.effective_user.id
    username = update.effective_user.username or "Без_имени"
    if user_id == OWNER_ID:
        logger.info(f"Владелец {username} ({user_id}) отправил фото. Проверка лица пропущена.")
        owner_start(update, context)
        return
    # Проверяем состояние пользователя
    user_state = context.user_data.get('state')
    if user_state not in ['awaiting_verification']:
        # Если пользователь уже в меню или на шаге "о себе", фото не обрабатываем
        # Можно добавить уведомление, если нужно
        return
    # Получаем актуальное количество фото владельца
    num_owner_photos = len(owner_data['photos'])
    # Дополнительная проверка на случай, если владелец удалил фото во время регистрации пользователя
    if num_owner_photos == 0:
        update.message.reply_text("Извините, владелец удалил свои фото. Регистрация невозможна.")
        context.user_data['state'] = 'awaiting_verification'
        context.user_data['verified_photos'] = []
        context.user_data['sent_owner_photos'] = []
        return
    try:
        photo_file_id = update.message.photo[-1].file_id
        photo_file = context.bot.get_file(photo_file_id)
        photo_bytes = photo_file.download_as_bytearray()
        image_stream = BytesIO(photo_bytes)
        image_stream.seek(0)
        image = Image.open(image_stream)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        image_array = np.array(image)
        face_locations = face_recognition.face_locations(image_array)
        if face_locations:
            logger.info(f"Пользователь {username} ({user_id}) прошел проверку лица.")
            # Добавляем фото в список проверенных
            verified_photos = context.user_data.setdefault('verified_photos', [])
            sent_owner_photos = context.user_data.setdefault('sent_owner_photos', [])
            # Проверка на дубликаты (простая проверка по file_id)
            if photo_file_id in verified_photos:
                update.message.reply_text("Это фото уже было загружено. Пожалуйста, отправьте другое.")
                return
            verified_photos.append(photo_file_id)
            context.user_data['last_photo_id'] = photo_file_id  # Последнее фото для "Заинтересована"
            update.message.reply_text(f"✅ Фото принято! ({len(verified_photos)}/{REQUIRED_USER_PHOTOS})")
            # Отправляем случайное фото владельца в ответ
            sent_photo_id = send_random_owner_photo(context, update.effective_message.chat_id, sent_owner_photos,
                                                    owner_data['photos'])
            if sent_photo_id:
                sent_owner_photos.append(sent_photo_id)
            # Проверяем, набралось ли нужное количество фото
            if len(verified_photos) >= REQUIRED_USER_PHOTOS:
                # Открываем меню после отправки всех фото
                context.user_data['state'] = 'in_main_menu'
                logger.info(
                    f"Пользователь {username} ({user_id}) загрузил {REQUIRED_USER_PHOTOS} фото. Открывается меню.")
                update.message.reply_text(
                    f"Отлично! Вы загрузили {REQUIRED_USER_PHOTOS} фото и получили столько же фото владельца.\n"
                    f"Теперь у вас есть доступ к меню."
                )
                show_main_menu(update, context)
            else:
                update.message.reply_text(
                    f"Отправьте еще {REQUIRED_USER_PHOTOS - len(verified_photos)} фото."
                )
        else:
            logger.info(f"Пользователь {username} ({user_id}) НЕ прошел проверку лица.")
            update.message.reply_text(
                "❌ Лицо человека не обнаружено. Возможно, это не фото человека. Попробуйте другое.")
    except Exception as e:
        logger.error(f"Ошибка при обработке изображения от пользователя {username} ({user_id}): {e}", exc_info=True)
        update.message.reply_text("❌ Произошла ошибка при обработке фото. Попробуйте другое изображение.")


# --- ФУНКЦИИ ДЛЯ ПОЛЬЗОВАТЕЛЯ ---
def show_main_menu(update, context):
    """Отображает главное меню пользователю."""
    context.user_data['state'] = 'in_main_menu'
    # Проверяем, заполнено ли поле "О себе" пользователем
    user_about_text = context.user_data.get('about_text', '')
    # Кнопка "Заинтересована" активна только если "О себе" заполнено
    interest_button_text = "Заинтересована" if len(
        user_about_text) >= MIN_ABOUT_TEXT_LENGTH else "Заинтересована (недоступно)"
    keyboard = [
        [KeyboardButton("Посмотреть фото")],
        [KeyboardButton("Посмотреть интересы")],
        [KeyboardButton("Посмотреть, кого ищут")],
        [KeyboardButton("О себе")],  # <--- ДОБАВЛЕНА КНОПКА ---
        [KeyboardButton(interest_button_text)]  # Кнопка активна теперь
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    update.message.reply_text("Выберите действие:", reply_markup=reply_markup)


# --- ФУНКЦИИ МЕНЮ ПОЛЬЗОВАТЕЛЯ ---
def view_photos(update, context):
    """Отправляет фото владельца пользователю, группируя их по возможности."""
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        update.message.reply_text("Вы владелец.")
        return
    user_state = context.user_data.get('state')
    if user_state != 'in_main_menu':
        update.message.reply_text("Сначала пройдите все шаги регистрации.")
        return
    if not owner_data['photos']:
        update.message.reply_text("У владельца пока нет фото.")
        return
    logger.info(
        f"Пользователь {update.effective_user.username or 'Без_имени'} ({user_id}) просматривает фото владельца.")
    update.message.reply_text(f"Фото владельца ({len(owner_data['photos'])} шт.):")
    _send_photos_in_groups(context, update.effective_message.chat_id, owner_data['photos'], "Фото владельца")


def view_interests(update, context):
    """Отправляет интересы владельца."""
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        update.message.reply_text("Вы владелец.")
        return
    user_state = context.user_data.get('state')
    if user_state != 'in_main_menu':
        update.message.reply_text("Сначала пройдите все шаги регистрации.")
        return
    if not owner_data['interests']:
        update.message.reply_text("У владельца пока нет интересов.")
        return
    logger.info(
        f"Пользователь {update.effective_user.username or 'Без_имени'} ({user_id}) просматривает интересы владельца.")
    update.message.reply_text(f"Интересы владельца:\n{owner_data['interests']}")


def view_looking_for_for_users(update, context):
    """Отправляет пользователю текст 'кого ищет' владелец."""
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        update.message.reply_text("Вы владелец.")
        return
    user_state = context.user_data.get('state')
    if user_state != 'in_main_menu':
        update.message.reply_text("Сначала пройдите все шаги регистрации.")
        return
    if not owner_data['looking_for']:
        update.message.reply_text("Владелец пока не указал, кого ищет.")
        return
    logger.info(
        f"Пользователь {update.effective_user.username or 'Без_имени'} ({user_id}) просматривает 'кого ищет' владельца.")
    update.message.reply_text(f"Владелец ищет:\n{owner_data['looking_for']}")


# --- НОВАЯ ФУНКЦИЯ ДЛЯ "О себе" ---
def handle_about_me_button(update, context):
    """Обрабатывает нажатие кнопки 'О себе'."""
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        update.message.reply_text("Вы владелец.")
        return
    user_state = context.user_data.get('state')
    if user_state != 'in_main_menu':
        update.message.reply_text("Сначала пройдите все шаги регистрации.")
        return
    current_about = context.user_data.get('about_text', '')
    if current_about:
        update.message.reply_text(
            f"Ваше сообщение 'О себе':\n{current_about}\n"
            f"Хотите изменить? Просто отправьте новый текст (минимум {MIN_ABOUT_TEXT_LENGTH} символов)."
        )
    else:
        update.message.reply_text(
            f"Вы пока не добавили информацию 'О себе'.\n"
            f"Отправьте текст (минимум {MIN_ABOUT_TEXT_LENGTH} символов), и он будет сохранен."
        )
    # Устанавливаем состояние ожидания нового текста
    context.user_data['state'] = 'awaiting_new_about_text'


def handle_new_about_text(update, context):
    """Обрабатывает новый текст 'О себе', отправленный пользователем."""
    user_id = update.effective_user.id
    username = update.effective_user.username or "Без_имени"
    if user_id == OWNER_ID:
        return  # Владельцу не нужно это обрабатывать тут
    user_state = context.user_data.get('state')
    if user_state != 'awaiting_new_about_text':
        # Если пользователь не ожидал ввода текста, игнорируем
        return
    new_about_text = update.message.text.strip()
    if len(new_about_text) < MIN_ABOUT_TEXT_LENGTH:
        update.message.reply_text(
            f"Текст слишком короткий. Пожалуйста, введите не менее {MIN_ABOUT_TEXT_LENGTH} символов.")
        return  # Остаемся в состоянии ожидания
    # Сохраняем новый текст
    context.user_data['about_text'] = new_about_text
    # --- ОТПРАВКА ТЕКСТА "О СЕБЕ" ВЛАДЕЛЬЦА ПОЛЬЗОВАТЕЛЮ ---
    try:
        if owner_data['about']:
            context.bot.send_message(chat_id=user_id, text=f"Текст 'о себе' от владельца:\n{owner_data['about']}")
            logger.info(
                f"Текст 'о себе' владельца отправлен пользователю {username} ({user_id}) после заполнения 'о себе'.")
        else:
            context.bot.send_message(chat_id=user_id, text="Владелец пока не добавил текст 'о себе'.")
            logger.info(
                f"Текст 'о себе' владельца отсутствует, уведомление отправлено пользователю {username} ({user_id}) после заполнения 'о себе'.")
    except Exception as e:
        logger.error(f"Ошибка при отправке текста 'о себе' владельца пользователю {user_id}: {e}")
    # --- КОНЕЦ ОТПРАВКИ ТЕКСТА "О СЕБЕ" ВЛАДЕЛЬЦА ПОЛЬЗОВАТЕЛЮ ---
    # Возвращаем пользователя в главное меню
    context.user_data['state'] = 'in_main_menu'
    update.message.reply_text("✅ Текст 'О себе' обновлен!")
    # Показываем меню снова (кнопка "Заинтересована" теперь активна)
    show_main_menu(update, context)


# --- КОНЕЦ НОВОЙ ФУНКЦИИ ---

def interested(update, context):
    """Обрабатывает нажатие кнопки 'Заинтересована'."""
    user_id = update.effective_user.id
    username = update.effective_user.username or "Без_имени"
    if user_id == OWNER_ID:
        update.message.reply_text("Вы владелец.")
        return
    user_state = context.user_data.get('state')
    if user_state != 'in_main_menu':
        update.message.reply_text("Сначала пройдите все шаги регистрации.")
        return
    # Проверка, заполнено ли поле "О себе"
    user_about_text = context.user_data.get('about_text', '')
    if len(user_about_text) < MIN_ABOUT_TEXT_LENGTH:
        update.message.reply_text(
            f"Вы не заполнили поле 'О себе' (минимум {MIN_ABOUT_TEXT_LENGTH} символов). Это необходимо для использования этой функции.")
        return
    if context.user_data.get('has_expressed_interest'):
        update.message.reply_text("Вы уже выражали интерес. Повторная отправка невозможна.")
        logger.info(f"Пользователь {username} ({user_id}) попытался снова нажать 'Заинтересована'.")
        return
    user_photo_id = context.user_data.get('last_photo_id')  # Используем последнее проверенное фото
    verified_photos = context.user_data.get('verified_photos', [])
    if not user_photo_id:
        update.message.reply_text("Ошибка: не найдено фото пользователя.")
        logger.error(f"Ошибка при отправке интереса: нет фото для пользователя {user_id}")
        return
    # Сохраняем информацию о заинтересованном пользователе в БД
    save_interested_user(user_id, username, user_photo_id, user_about_text)
    context.user_data['has_expressed_interest'] = True
    logger.info(f"Пользователь {username} ({user_id}) проявил интерес.")
    update.message.reply_text("✅ Ваш интерес отправлен владельцу!")

    # --- ОТПРАВКА ИНФОРМАЦИИ ВЛАДЕЛЬЦУ ---
    try:
        # Формируем сообщение для владельца
        notification_text = (
            f"💌 Новый интерес!\n"
            f"Пользователь: @{username} (ID: {user_id})\n"
            f"О себе:\n{user_about_text}"
        )
        context.bot.send_message(chat_id=OWNER_ID, text=notification_text)
        # Отправляем все проверенные фото пользователя владельцу
        if verified_photos:
            update.message.reply_text("Отправка ваших фото владельцу...")
            # Отправляем фото группами
            _send_photos_in_groups(context, OWNER_ID, verified_photos, f"Фото от @{username}")
        else:
            # На всякий случай, если фото как-то потерялись
            context.bot.send_photo(chat_id=OWNER_ID, photo=user_photo_id, caption=f"Фото от @{username} (резервное)")
        logger.info(f"Интерес и фото пользователя {username} ({user_id}) отправлены владельцу {OWNER_ID}.")
    except Exception as e:
        logger.error(f"Ошибка при отправке интереса владельцу {OWNER_ID}: {e}")
        update.message.reply_text("⚠️ Возникла проблема при отправке интереса. Попробуйте позже.")
    # --- КОНЕЦ ОТПРАВКИ ИНФОРМАЦИИ ВЛАДЕЛЬЦУ ---


# --- КОНЕЦ ФУНКЦИЙ МЕНЮ ПОЛЬЗОВАТЕЛЯ ---

# === Обработка текстовых сообщений ===
def handle_text(update, context):
    """Обрабатывает текстовые сообщения: меню."""
    text = update.message.text
    user_id = update.effective_user.id
    # --- Обработка владельца ---
    if user_id == OWNER_ID:
        if text == "Посмотреть текущие фото":
            view_current_photos(update, context)
        elif text == "Посмотреть текущие интересы":
            view_current_interests(update, context)
        elif text == "Посмотреть, кого ищу":
            view_looking_for(update, context)
        elif text == "Посмотреть о себе":  # <--- НОВАЯ КНОПКА ---
            view_owner_about(update, context)
        elif text == "Главное меню":
            owner_start(update, context)
        # Остальные команды владельца обрабатываются ConversationHandler'ом
        return
    # --- Обработка пользователя ---
    user_state = context.user_data.get('state')
    # 1. Если пользователь вводит новый текст "О себе"
    if user_state == 'awaiting_new_about_text':
        handle_new_about_text(update, context)
        return  # Важно: завершаем обработку после этого шага
    # 2. Если пользователь в главном меню
    elif user_state == 'in_main_menu':
        if text == "Посмотреть фото":
            view_photos(update, context)
        elif text == "Посмотреть интересы":
            view_interests(update, context)
        elif text == "Посмотреть, кого ищут":
            view_looking_for_for_users(update, context)
        elif text == "О себе":  # <--- ОБРАБОТКА КНОПКИ ---
            handle_about_me_button(update, context)
        elif text == "Заинтересована" or text == "Заинтересована (недоступно)":
            # Проверяем, действительно ли доступна кнопка
            user_about_text = context.user_data.get('about_text', '')
            if len(user_about_text) >= MIN_ABOUT_TEXT_LENGTH:
                interested(update, context)
            else:
                update.message.reply_text(f"Вы не заполнили поле 'О себе' (минимум {MIN_ABOUT_TEXT_LENGTH} символов).")
        else:
            update.message.reply_text("Пожалуйста, используйте кнопки меню.")
    # 3. Если пользователь ожидает проверки фото или на других этапах,
    # текстовые сообщения (кроме команд бота) игнорируются.


# === Основная функция ===
def main():
    """Запуск бота."""
    if not TOKEN or TOKEN == 'YOUR_BOT_TOKEN':
        logger.error("Пожалуйста, установите ваш TELEGRAM BOT TOKEN.")
        return
    if OWNER_ID == 123456789:
        logger.warning("OWNER_ID не установлен! Пожалуйста, укажите ваш Telegram ID.")
    logger.info("Запуск бота...")
    init_db()
    try:
        loaded_data = load_owner_data()
        owner_data['interests'] = loaded_data['interests']
        owner_data['looking_for'] = loaded_data['looking_for']
        # Добавлено: загрузка 'about' владельца из БД
        owner_data['about'] = loaded_data['about']
        owner_data['photos'] = load_owner_photos()
        logger.info("Данные владельца загружены из базы данных.")
        logger.info(f"У владельца загружено {len(owner_data['photos'])} фото.")
        logger.debug(
            f"Полные загруженные данные владельца: Interests='{owner_data['interests']}', Looking_for='{owner_data['looking_for']}', About='{owner_data['about']}'")
    except Exception as e:
        logger.error(f"Ошибка при загрузке данных из БД при запуске: {e}")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # --- ConversationHandler для владельца ---
    owner_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(Filters.text & Filters.regex('^Установить фото$'), set_photo_start),
            MessageHandler(Filters.text & Filters.regex('^Установить интересы$'), set_interests_start),
            MessageHandler(Filters.text & Filters.regex('^Добавить, кого ищу$'), set_looking_for_start),
            MessageHandler(Filters.text & Filters.regex('^Добавить о себе$'), set_owner_about_start),
            # <--- НОВАЯ КНОПКА ---
            MessageHandler(Filters.text & Filters.regex('^Очистить фото$'), confirm_clear_photos_start),
            MessageHandler(Filters.text & Filters.regex('^Очистить интересы$'), confirm_clear_interests_start),
            MessageHandler(Filters.text & Filters.regex('^Очистить, кого ищу$'), confirm_clear_looking_for_start),
            MessageHandler(Filters.text & Filters.regex('^Очистить о себе$'), confirm_clear_owner_about_start),
            # <--- НОВАЯ КНОПКА ---
        ],
        states={
            SET_PHOTO: [MessageHandler(Filters.photo, set_photo_received)],
            SET_INTERESTS: [MessageHandler(Filters.text & ~Filters.command, set_interests_received)],
            SET_LOOKING_FOR: [MessageHandler(Filters.text & ~Filters.command, set_looking_for_received)],
            SET_OWNER_ABOUT: [MessageHandler(Filters.text & ~Filters.command, set_owner_about_received)],
            # <--- НОВОЕ СОСТОЯНИЕ ---
            CONFIRM_CLEAR_PHOTOS: [
                MessageHandler(Filters.text & Filters.regex('^Да$'), confirm_clear_photos_received),
                MessageHandler(Filters.text & Filters.regex('^Нет$'), confirm_clear_photos_received),
                MessageHandler(Filters.text & ~Filters.command,
                               lambda u, c: u.message.reply_text("Пожалуйста, ответьте 'Да' или 'Нет'.")),
            ],
            CONFIRM_CLEAR_INTERESTS: [
                MessageHandler(Filters.text & Filters.regex('^Да$'), confirm_clear_interests_received),
                MessageHandler(Filters.text & Filters.regex('^Нет$'), confirm_clear_interests_received),
                MessageHandler(Filters.text & ~Filters.command,
                               lambda u, c: u.message.reply_text("Пожалуйста, ответьте 'Да' или 'Нет'.")),
            ],
            CONFIRM_CLEAR_LOOKING_FOR: [
                MessageHandler(Filters.text & Filters.regex('^Да$'), confirm_clear_looking_for_received),
                MessageHandler(Filters.text & Filters.regex('^Нет$'), confirm_clear_looking_for_received),
                MessageHandler(Filters.text & ~Filters.command,
                               lambda u, c: u.message.reply_text("Пожалуйста, ответьте 'Да' или 'Нет'.")),
            ],
            CONFIRM_CLEAR_OWNER_ABOUT: [  # <--- НОВОЕ СОСТОЯНИЕ ---
                MessageHandler(Filters.text & Filters.regex('^Да$'), confirm_clear_owner_about_received),
                MessageHandler(Filters.text & Filters.regex('^Нет$'), confirm_clear_owner_about_received),
                MessageHandler(Filters.text & ~Filters.command,
                               lambda u, c: u.message.reply_text("Пожалуйста, ответьте 'Да' или 'Нет'.")),
            ],
        },
        fallbacks=[
            MessageHandler(Filters.text & Filters.regex('^Главное меню$'), lambda u, c: ConversationHandler.END),
        ],
        allow_reentry=True
    )
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(owner_conv_handler)
    dp.add_handler(MessageHandler(Filters.photo, handle_photo))
    # ВАЖНО: handle_text должен быть последним среди MessageHandler
    dp.add_handler(MessageHandler(Filters.text, handle_text))

    updater.start_polling()
    logger.info("Бот запущен и ожидает сообщений.")
    updater.idle()


if __name__ == '__main__':
    main()
