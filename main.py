import discord
from discord import app_commands
import re
import asyncio
import threading
import os
import unicodedata
from discord.ext import commands
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
import aiohttp
from aiohttp import ClientTimeout
import ssl
from telegram.ext import ApplicationBuilder, ContextTypes
from telegram import Bot
import asyncio
import sys

TELEGRAM_BOT_TOKEN = 'token telegram bot'
TELEGRAM_CHAT_ID = '-100123456789'
telegram_bot = None
telegram_log_queue = asyncio.Queue()

def log_action(user, action, details=None):
    """Логирование действий пользователей в файл и постановка в очередь для Telegram"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_dir = r"C:\Users\stepa\OneDrive\Рабочий стол\anticrash\logs"

    # Создаем директорию для логов, если её нет
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Формируем имя файла для текущего дня
    log_file = os.path.join(log_dir, f"bot_log_{datetime.now().strftime('%Y-%m-%d')}.txt")

    # Формируем сообщение лога
    log_message = f"[{timestamp}] Пользователь: {user} | Действие: {action}"
    if details:
        log_message += f" | Детали: {details}"

    # Записываем в файл
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(log_message + "\n")

    # Постановка сообщения в очередь для Telegram
    # Исключаем логи авто-синхронизации из отправки в Telegram
    if action not in ["commands_sync", "commands_sync_error"] and telegram_log_queue:
        # Важно: put_nowait используется, так как эта функция синхронная
        try:
            telegram_log_queue.put_nowait(log_message)
        except asyncio.QueueFull:
            print("Telegram log queue is full, dropping message.")
        except Exception as e:
            print(f"Error putting message to telegram queue: {e}")

# Асинхронная задача для отправки логов в Telegram
# Теперь функция принимает экземпляр telegram.Bot
async def telegram_logger_task(telegram_bot_instance: Bot):
    global telegram_bot # Убираем global, т.к. получаем экземпляр как аргумент
    # Инициализация Telegram бота больше не нужна здесь
    print("Телеграм-логирование запущено")

    while True:
        log_message = await telegram_log_queue.get()
        if log_message is None: # Стоп-сигнал для задачи
            break

        # Логика повторной попытки отправки
        max_send_retries = 3
        send_retry_delay = 5 # секунды

        for attempt in range(max_send_retries):
            try:
                # Используем переданный экземпляр бота
                await telegram_bot_instance.send_message(chat_id=TELEGRAM_CHAT_ID, text=log_message)
                # print(f"Sent Telegram log: {log_message}") # Опционально: логировать успешную отправку
                break # Успешно отправлено, выходим из цикла повторных попыток
            except Exception as e:
                print(f"❌ Ошибка отправки telegram сообщения (попытка {attempt + 1}/{max_send_retries}): {e}")
                if attempt < max_send_retries - 1:
                    await asyncio.sleep(send_retry_delay)
                else:
                    print(f"❌ Не удалось отправить telegram сообщение после {max_send_retries} попыток.")
                    # Можно добавить логирование в файл здесь, если очень важно
                    # log_action("SYSTEM", "telegram_send_failed", f"Сообщение: {log_message[:100]}..., Ошибка: {e}")

        telegram_log_queue.task_done()

# Настройки для HTTP клиента (перенесены ближе к месту использования или как параметры сессии)
# timeout = ClientTimeout(total=30, connect=10) # Удаляем глобальное определение таймаута

# Инициализация бота
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True
intents.guilds = True

class AntiCrashBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix='!', 
            intents=intents,
            application_id=1361442516477673573,
        )
        print("Бот инициализирован")

        # Атрибут для хранения HTTP сессии
        self.http_session: aiohttp.ClientSession | None = None
        self._telegram_logger_task = None # Для хранения ссылки на задачу

    async def periodic_sync(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                synced = await self.tree.sync()
                print(f"[AUTO-SYNC] Синхронизировано {len(synced)} команд")
            except Exception as e:
                print(f"[AUTO-SYNC] Ошибка: {e}")
                log_action(self.user.name, "commands_sync_error", str(e))
            await asyncio.sleep(300)  # 5 минут = 300 секунд

    async def setup_hook(self):
        print("Запуск бота...")
        try:
            # Создаем HTTP сессию здесь, в асинхронном контексте
            self.http_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    force_close=True,
                    enable_cleanup_closed=True,
                    ssl=ssl.create_default_context(),
                    limit=10
                ),
                timeout=ClientTimeout(total=30, connect=10)
            )
            print("HTTP сессия создана")

            synced = await self.tree.sync()
            print(f"Синхронизировано {len(synced)} команд")
            log_action(self.user.name, "commands_sync", f"Синхронизировано {len(synced)} команд")
        except Exception as e:
            print(f"Ошибка при синхронизации команд: {e}")
            log_action(self.user.name, "commands_sync_error", str(e))
        # Запуск фоновой задачи авто-синхронизации
        self.loop.create_task(self.periodic_sync())

        # --- Инициализация Telegram бота с помощью ApplicationBuilder ---
        print("Инициализация Телеграм-Логирования...")
        telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        print("Телеграм-Логирование инициализировано.")
        # Передаем экземпляр собранного бота в задачу логирования
        self._telegram_logger_task = self.loop.create_task(telegram_logger_task(telegram_app.bot))
        # --- Конец блока инициализации Telegram ---

    async def close(self):
        # Закрываем HTTP сессию при завершении работы бота
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

        # Остановка задачи логирования в Telegram
        if self._telegram_logger_task:
            await telegram_log_queue.put(None) # Отправляем стоп-сигнал
            await self._telegram_logger_task # Ожидаем завершения задачи

        await super().close()

bot = AntiCrashBot()
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот запущен, ехала пацанчик!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    server = Thread(target=run)
    server.start()

# Глобальная переменная для хранения последнего наказанного пользователя
last_punished_user = None

# Список ID администраторов
ADMIN_IDS = []  # Начальный список с вашим текущим админом

# WHITELIST_IDS = [1340022235276116069, 1373369966052769853, 1326584560368222239, 637210388995244032, 1338536314089242666, 1294307613047132231]
WHITELIST_IDS = {

}

@bot.event
async def on_ready():
    print(f'БОТ {bot.user.name} ЗАПУЩЕН')
    print(f'ID бота: {bot.user.id}')
    print(f'Количество серверов: {len(bot.guilds)}')
    log_action(bot.user.name, "bot_started", f"ID: {bot.user.id}")
    try:
        print("Выполняю автоматическую синхронизацию команд...")
        synced = await bot.tree.sync()
        print(f"Успешно синхронизировано {len(synced)} команд")
        log_action(bot.user.name, "commands_sync", f"Синхронизировано {len(synced)} команд")
    except Exception as e:
        print(f"Ошибка при синхронизации команд: {e}")
        log_action(bot.user.name, "commands_sync_error", str(e))
    # Запускаем поток для обработки консольных команд
    threading.Thread(target=console_command_handler, daemon=True).start()

@bot.event
async def on_guild_join(guild):
    """Синхронизируем команды при присоединении к новому серверу"""
    print(f"Бот присоединился к серверу: {guild.name}")
    log_action(bot.user.name, "guild_join", f"Сервер: {guild.name} (ID: {guild.id})")
    # try:
    #     synced = await bot.tree.sync(guild=guild)
    #     print(f"Синхронизировано {len(synced)} команд для нового сервера {guild.name}")
    #     log_action(bot.user.name, "guild_commands_sync", f"Сервер: {guild.name}, Синхронизировано команд: {len(synced)})"
    # except Exception as e:
    #     print(f"Ошибка синхронизации для нового сервера {guild.name}: {e}")
    #     log_action(bot.user.name, "guild_commands_sync_error", f"Сервер: {guild.name}, Ошибка: {str(e)}")

@bot.tree.command(name="addadm", description="Добавить нового администратора в список получателей уведомлений")
@app_commands.default_permissions(administrator=True)
async def add_admin(interaction: discord.Interaction, user_id: str):
    """Добавить нового администратора в список получателей уведомлений"""
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды", ephemeral=True)
        return
    log_action(interaction.user, "add_admin_attempt", f"ID нового админа: {user_id}")
    try:
        user_id = int(user_id)
        if user_id not in ADMIN_IDS:
            ADMIN_IDS.append(user_id)
            await interaction.response.send_message(f"✅ Администратор с ID {user_id} добавлен в список получателей уведомлений")
            log_action(interaction.user, "add_admin_success", f"Добавлен админ с ID: {user_id}")
        else:
            await interaction.response.send_message("❌ Этот пользователь уже является администратором")
            log_action(interaction.user, "add_admin_failed", f"ID {user_id} уже является админом")
    except ValueError:
        await interaction.response.send_message("❌ Неверный формат ID. Пожалуйста, введите числовой ID пользователя")
        log_action(interaction.user, "add_admin_error", f"Неверный формат ID: {user_id}")

@bot.tree.command(name="deladm", description="Удалить администратора из списка получателей уведомлений")
@app_commands.default_permissions(administrator=True)
async def remove_admin(interaction: discord.Interaction, user_id: str):
    """Удалить администратора из списка получателей уведомлений"""
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды", ephemeral=True)
        return
    log_action(interaction.user, "remove_admin_attempt", f"ID админа для удаления: {user_id}")
    try:
        user_id = int(user_id)
        if user_id in ADMIN_IDS:
            ADMIN_IDS.remove(user_id)
            await interaction.response.send_message(f"✅ Администратор с ID {user_id} удален из списка получателей уведомлений")
            log_action(interaction.user, "remove_admin_success", f"Удален админ с ID: {user_id}")
        else:
            await interaction.response.send_message("❌ Этот пользователь не является администратором")
            log_action(interaction.user, "remove_admin_failed", f"ID {user_id} не является админом")
    except ValueError:
        await interaction.response.send_message("❌ Неверный формат ID. Пожалуйста, введите числовой ID пользователя")
        log_action(interaction.user, "remove_admin_error", f"Неверный формат ID: {user_id}")

@bot.tree.command(name="listadm", description="Показать список всех администраторов")
@app_commands.default_permissions(administrator=True)
async def list_admins(interaction: discord.Interaction):
    """Показать список всех администраторов"""
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды", ephemeral=True)
        return
    log_action(interaction.user, "list_admins_command", "Запрошен список администраторов")
    admin_list = []
    for admin_id in ADMIN_IDS:
        try:
            admin = await bot.fetch_user(admin_id)
            admin_list.append(f"{admin.name} (ID: {admin_id})")
        except:
            admin_list.append(f"Неизвестный пользователь (ID: {admin_id})")

    embed = discord.Embed(title="📋 Список администраторов", color=discord.Color.blue())
    embed.description = "\n".join(admin_list) if admin_list else "Список пуст"
    await interaction.response.send_message(embed=embed)
    log_action(interaction.user, "list_admins_success", f"Показано админов: {len(admin_list)}")

@bot.tree.command(name="ls", description="Отправить личное сообщение пользователю")
@app_commands.default_permissions(administrator=True)
async def send_dm(interaction: discord.Interaction, user_id: str, message: str):
    """Отправить личное сообщение указанному пользователю"""
    # Сразу отправляем отложенный ответ
    await interaction.response.defer(ephemeral=True)

    if interaction.user.id not in ADMIN_IDS:
        await interaction.followup.send("❌ У вас нет прав для использования этой команды", ephemeral=True)
        return

    try:
        user_id = int(user_id)
        user = await bot.fetch_user(user_id)

        if user:
            try:
                await user.send(message)
                await interaction.followup.send(f"✅ Сообщение успешно отправлено пользователю {user.name}", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send("❌ Не удалось отправить сообщение. Возможно, у пользователя закрыты личные сообщения", ephemeral=True)
            log_action(interaction.user, "send_dm_success", f"Получатель: {user.name} ({user_id})")
        else:
            await interaction.followup.send("❌ Пользователь не найден", ephemeral=True)
            log_action(interaction.user, "send_dm_failed", f"Пользователь с ID {user_id} не найден")

    except ValueError:
        await interaction.followup.send("❌ Неверный формат ID пользователя", ephemeral=True)
        log_action(interaction.user, "send_dm_error", f"Неверный формат ID: {user_id}")
    except Exception as e:
        await interaction.followup.send(f"❌ Произошла ошибка: {str(e)}", ephemeral=True)
        log_action(interaction.user, "send_dm_error", str(e))

# Функции нормализации текста
def normalize_text(text):
    """Нормализация Unicode и удаление невидимых символов форматирования"""
    normalized = unicodedata.normalize('NFKC', text)
    return ''.join(c for c in normalized if not unicodedata.category(c).startswith('Cf'))

def prepare_content(content):
    """Подготовка текста к проверке"""
    return normalize_text(content.lower())

async def notify_admin(user, message_content, reason, matched_pattern=None):
    """Отправляет уведомление всем администраторам"""
    # Определяем название сервера, если доступно
    guild_name = "в ЛС" # По умолчанию для личных сообщений
    if isinstance(user, discord.Member) and user.guild:
        guild_name = f"на сервере {user.guild.name}"

    embed = discord.Embed(
        title=f"⚠ Нарушение обнаружено {guild_name}",
        color=discord.Color.red(),
        timestamp=datetime.now()
    )
    embed.add_field(name="Пользователь", value=f"{user.mention} ({user.id})", inline=False)
    embed.add_field(name="Сообщение", value=f"```{message_content[:1000]}```", inline=False)

    if matched_pattern:
        if isinstance(matched_pattern, re.Pattern):
            embed.add_field(name="Причина", value=f"Сработал паттерн: `{matched_pattern.pattern}`", inline=False)
        else:
            embed.add_field(name="Причина", value=f"Запрещенная фраза: `{matched_pattern}`", inline=False)

    notification_sent = False
    for admin_id in ADMIN_IDS:
        try:
            admin = await bot.fetch_user(admin_id)
            if admin:
                await admin.send(embed=embed)
                notification_sent = True
                print(f"📨 Уведомление отправлено администратору {admin}")
                log_action("SYSTEM", "admin_notification_sent", f"Админ: {admin.id}, Пользователь: {user.id}")
        except Exception as e:
            print(f"❌ Ошибка отправки уведомления администратору {admin_id}: {e}")
            log_action("SYSTEM", "admin_notification_error", f"Админ: {admin_id}, Ошибка: {str(e)}")

    if not notification_sent:
        print("❌ Не удалось отправить уведомление ни одному администратору")
        log_action("SYSTEM", "admin_notification_failed", "Не удалось отправить ни одному админу")

# Регулярные выражения для блокировки
spam_patterns = [
    # 1. Блокировка вариаций Discord-ссылок
    re.compile(r'disc[a-zA-Z0-9]rd|dis[a-zA-Z0-9]ord|di[a-zA-Z0-9]scord|disco[a-zA-Z0-9]d|dd[a-zA-Z0-9]scord|d[a-zA-Z0-9]sc[a-zA-Z0-9]rd|www\.|dd[a-zA-Z0-9]scord|dіsсоrd', re.IGNORECASE),

    # 2. Блокировка обходных вариантов Discord
    re.compile(r'дискорд\.гг|``|dsc|dcs|https://|https:/|http://|http:/|[a-zA-Z0-9]*[||d||][a-zA-Z0-9]*[||i||][a-zA-Z0-9]*[||s||][a-zA-Z0-9]*[||cс||][a-zA-Z0-9]*[||oо||][a-zA-Z0-9]*[||r||][a-zA-Z0-9]*[||d||]', re.IGNORECASE),

    # 3. Блокировка Discord-ссылок и вариаций
    re.compile(r'http[s]?://discord|discord.{0,5}(invite|link|gg)|\bdot\b|(d i s c o r d)', re.IGNORECASE),

    # 4. Блокировка обходных написаний Discord
    re.compile(r'disc0rd|discord.*?invite|discord[a-zA-Z]{2,3}com|(d1scord|d1sc0rd|d!scord)', re.IGNORECASE),

    # 5. Блокировка сокращённых ссылок и специальных обозначений
    re.compile(r'discord\.\S+|\\[dot\\]|(https?://)?(bit\.ly|tinyurl\.com|goo\.gl|t\.co)/\S+', re.IGNORECASE),

    # 6. Блокировка Discord-приглашений (стандартные форматы)
    re.compile(r'(discordapp\.com/invite|discord\.com/invite|discord\.me|discord\.gg)(?:/#)?(?:/invite)?/([a-zA-Z0-9-]+)', re.IGNORECASE),

    # 7. Блокировка альтернативных Discord-доменов (с http/s и www)
    re.compile(r'(https?://)?(www\.)?(discord\.(gg|io|me|li)|discordapp\.com/invite)/\S+', re.IGNORECASE),

    # 8. Блокировка вариаций с повторяющимися символами
    re.compile(r'[dд]!!!!![сcс0-9][кk0-9][oооiі0-9][рpr0-9][дdp]|[дд]!!!![сcс0-9][кk0-9][oооiі0-9][рpr0-9][дdp]|[дд]!!![сcс0-9][кk0-9][oооiі0-9][рpr0-9][дdp]|[дд]!![сcс0-9][кk0-9][oооiі0-9][рpr0-9][дdp]|[дд]![сcс0-9][кk0-9][oооiі0-9][рpr0-9][дdp]', re.IGNORECASE),

    # 9. Блокировка оскорбительных слов (существующий)
    re.compile(r'(в[ыііeе][еeє]б[аaа@][нnн][ыіiуy])|([vV]ы[eе]б[aа][nн][ыi])|(вы[еeє][бb][аa@][нn][ыiуy])|(в[іiы][eе]баны)|(в[ыiі]еб[аa@]ны)|(в[ыiі][ее]ба[нn][ыiуy])', re.IGNORECASE),

    # 10. Блокировка слова "переезд" в разных вариациях (существующий)
    re.compile(r'(п[еeё]р[еeё]е[зz3]д)|(пер[еeё]ез[дd])|(п[еe]ре[еe]зд)|(п[иеe]р[иеe][ее]зд)|([pP][ее][rр][ее][ее][zз3][dд])|(п[ее]р[ее]зд)|(пере[зz3]д)|(пер[еe]зд)', re.IGNORECASE),

    # 11. Блокировка скрытых сообщений с Discord-ссылками (существующий)
    re.compile(r'(?i)(?:^|\s)([^\s]+)(?:\s*\|\|[​‌‍﻿]*)+\s*\n*#[0-9]+(?:discord(?:app)?\.(?:com|gg)/[\w-]+|[\w-]+\.(?:com|net|org|ru|xyz\|tk))\b'),

    # 12. Блокировка музыкальных приглашений (существующие)
    re.compile(r'(слуша[йьт]|послуша[йь]|присоединя[йь]|приглаш[а-я]+)\s*(вместе|со мной)?\s*(музык[а-я]+|трек[а-я]*|песн[а-я]*)?\s*(на|в)\s*(spotify|я\.?музык[еи]|яндекс\s*музык[еи]|apple\s*music|deezer|soundcloud)', re.IGNORECASE),
    re.compile(r'(spotify:|spotify\.com|music\.yandex|yandex\.ru/music|apple\.com/music|deezer\.com|soundcloud\.com)[^\s]*', re.IGNORECASE),

    # 13. Блокировка невидимых символов (существующий)
    re.compile(r'[\u200B-\u200D\uFEFF\u034F\u115F\u1160\u17B4\u17B5\u180E]'),

    # 14. Блокировка обфусцированных ссылок (существующий)
    re.compile(r'[\W_]*[дd][\W_]*[иi1!][\W_]*[сsc$][\W_]*[кkсc][\W_]*[оo0][\W_]*[рpr0-9][\W_]*[дd][\W_]*\.[\W_]*[cс][\W_]*[оo0][\W_]*[мm][\W_]*\/[\W_]*[иi1!][\W_]*[нn][\W_]*[вvb][\W_]*[иi1!][\W_]*[тt][\W_]*[еe][\W_]*\/?', re.IGNORECASE),

    # 15. Блокировка оскорбительных слов и слов, связанных со спамом/рейдами (с границами слова)
    re.compile(r'(?i)\b(выеб|въеб|краш|crash)\b'),

    # 16. Блокировка слов, связанных со спамом/рейдами/приглашениями (с границами слова)
    re.compile(r'(?i)\b(raid|рейд|спам|spam|invite|приглаш)\b'),

    # 17. Блокировка ссылок Telegram
    re.compile(r'https?://t\.me/[a-zA-Z0-9_]+'),

]

# Фразы для блокировки
blocked_phrases = [
    "ᴅ𝟣$ᴄᴏʀᴅ.ᴄᴏᴍ", "ДСГ.ГГ", "dисord", "Delusions Grandeur", "-->", "->", "titangroup", "fucked", "#1discord.com/invite\ ", "#1discord.com/invite/", "#1discord.com", "#1discord", "Bac E6et", "E6et", "dисkoрд", "cepBak", "Dе1usiоns Grаndеur", "Grаndеur", "СЛЕШ", "СЛЭШ", "расx0dimся", "# ", "titan", "group", "ДСГ", "дсг", "ГГ", "ПЕРЕЕЗД", "въебаны", "въёбаны", "вьебаны",  "ᴅ𝟣$ᴄᴏʀᴅ.ᴄᴏᴍ/ɪɴᴠɪᴛᴇ/", "ɪɴᴠɪᴛᴇ", "ᴅ𝟣$ᴄᴏʀᴅ", "-#", ".gg", ".gG", ".ɢɢ", ".GG", ".gġ", ".ģġ", ".ģģ", ".gg/", "[d̲̅][i̅][s][c̲̅][o̲̅][r̲̅][d̲̅][.̲̅][g̲̅][g̲̅][/̲̅]", "[̲̅d][̲̅i][̲̅s][̲̅c][̲̅o][̲̅r][̲̅d][̲̅.][̲̅g][̲̅g][̲̅/]", "[d̲̅][s̲̅][c̲̅][.][g̲][g̲̅]", "⧼d̼⧽⧼i̼⧽⧼s̼⧽⧼c̼⧽⧼o̼⧽⧼r̼⧽⧼d̼⧽⧼.̼⧽⧼g̼⧽⧼g̼⧽⧼/̼⧽", "⧼d̼⧽⧼s̼⧽⧼c̼⧽⧼.̼⧽⧼g̼⧽⧼g̼⧽", "⦏d̂⦎⦏î⦎⦏ŝ⦎⦏ĉ⦎⦏ô⦎⦏r̂⦎⦏d̂⦎⦏.̂⦎⦏ĝ⦎⦏ĝ⦎⦏/̂⦎", "⦏d̂⦎⦏ŝ⦎⦏ĉ⦎⦏.̂⦎⦏ĝ⦎⦏ĝ⦎", "/", "⦑d⦒⦑i⦒⦑s⦒⦑c⦒⦑o⦒⦑r⦒⦑d⦒⦑.⦒⦑g⦒⦑g⦒⦑/⦒", "⦑d⦒⦑s⦒⦑c⦒⦑.⦒⦑g⦒⦑g⦒", "⟦d⟧⟦i⟧⟦s⟧⟦c⟧⟦o⟧⟦r⟧⟦d⟧⟦.⟧⟦g⟧⟦g⟧⟦/⟧", "⟦d⟧⟦s⟧⟦c⟧⟦.⟧⟦g⟧⟦g⟧", "『d』『i』『s』『c』『o』『r』『d』『.』『g』『g』『/』", "【d】【i】【s】【c】【o】【r】【d】【.】【g】【g】【/】", "﴾d̤̈﴿﴾ï̤﴿﴾s̤̈﴿﴾c̤̈﴿﴾ö̤﴿﴾r̤̈﴿﴾d̤̈﴿﴾.̤̈﴿﴾g̤̈﴿﴾g̤̈﴿﴾/̤̈﴿", "﴾d̤̈﴿﴾s̤̈﴿﴾c̤̈﴿﴾.̤̈﴿﴾g̤̈﴿﴾g̤̈﴿", "//", "/ƃƃ˙pɹoɔsıp", "/gg.bᴙoↄꙅib", "/gg.drocsid", "\\", "#", "# 1discordapp.com", "##", "###", "#1discordapp.com", "꜍d꜉꜍i꜉꜍s꜉꜍c꜉꜍o꜉꜍r꜉꜍d꜉꜍.꜉꜍g꜉꜍g꜉꜍/꜉", "꜍d꜉꜍s꜉꜍c꜉꜍.꜉꜍g꜉꜍g꜉", "∂ѕ¢.gg", "discord.com/invite/", "discord.com/invite\ ", "1discord.com/invite", "1discordapp", "1discordapp.com", "໓iŞ¢໐r໓.ງງ/", "໓Ş¢.ງງ", "๔รς.ﻮﻮ", "๔เรς๏г๔.ﻮﻮ/", "ВЫЕБАНЫ", "выебаны by", "выеbаны", "ВЫEБAHЫ", "ВЬЕБАНЫЕ", "ВЬEБAНЫ", "гг", "где ДАМАГ", "ԃʂƈ.ɠɠ", "ԃιʂƈσɾԃ.ɠɠ", "ԃιʂƈσɾԃ.ɠɠ/", "переезд", "переезд", "Переезд", "ПЕРЕЕЗД", "ПEРЕЕЗД", "ПEРЕEЗД", "ПEPЕЕЗД", "ПEPEЕЗД", "ПEPEEЗД", "РЕЙД", "РEЙД", "слеш", "слэш", "Сkвaд", "bу", "BЫЕБAHЫ", "BЫEБАНЫ", "BЫEБAНЫ", "BЫEБAHЫ", "BЬEБAНЫE", "BЬEБAHЫ", "biꙅↄoᴙb.gg/", "by", "c0m", "Cквaд", "d⃣ i⃣ s⃣ c⃣ o⃣ r⃣ d⃣ .⃣ g⃣ g⃣", "d⃣ i⃣ s⃣ c⃣ o⃣ r⃣ d⃣ .⃣ g⃣ g⃣ /⃣", "d⃣ s⃣ c⃣ .⃣ g⃣ g⃣", "D!SC0!R!D", "Ð§¢.gg", "d♥i♥s♥c♥o♥r♥d♥.♥g♥g", "Đ₴₵.₲₲", "d1$c0rd", "d1sc0rd", "d1sc0rd", "d1scopd", "d1scopd.гг/", "d1scord.гг слеш", "d1scord.гг слэш", "dіsсоrd.гг", "dсs", "dcs", "Ðï§¢ðrÐ.gg/ बिस्तर", "disс0rd", "🅳🅸🆂🅲:o2:🆁🅳.🅶🅶/", "disc0rd", "discord", "discord.гг", "discord.com", "discord.gg", "discord.gg", "ｄｉｓｃｏｒｄ．ｇｇ／", "𝚍𝚒𝚜𝚌𝚘𝚛𝚍.𝚐𝚐", "𝑑ᵢ𝑠𝑐ₒᵣ𝑑.𝑔𝑔", "d҉i҉s҉c҉o҉r҉d҉.҉g҉g҉/҉", "di𝓼c𝓞rd.gg/", "ｄｉｓｃｏｒｄ．ｇｇ／", "𝐝𝐢𝐬𝐜𝐨𝐫𝐝.𝐠𝐠/", "𝚍𝚒𝚜ｃｏｒｄ.𝚐𝚐/", "𝒹𝒾𝒸𝑜𝓇𝒹.𝑔𝑔/", "𝗱𝗶𝘀𝗰𝗼𝗿𝗱.𝗴𝗴/", "𝘥𝘪𝘴𝘤𝘰𝘳𝘥.𝘨𝘨/", "𝑑𝑖𝑠𝘤𝑜𝘳𝑑.𝑔𝑔/", "𝒅𝒊𝒔𝒄𝒐𝒓𝒅.𝒈𝒈/", "𝙙𝙞𝙨𝙘𝙤𝙧𝙙.𝙜𝙜/", "𝕕𝕚𝕤𝕔𝕠𝕣𝕕.𝕘𝕘/", "𝔡𝔦𝔰𝔠𝔬𝔯𝔡.𝔤𝔤/", "𝖉𝖎𝖘𝖈𝖔𝖗𝖉.𝖌𝖌/", "𝓭𝓲𝓼𝓬𝓸𝓻𝓭.𝓰𝓰/", "𝖽𝗂𝗌𝖼𝗈𝗋𝖽.𝗀𝗀/", "dᵢsₒrₓ.gg", "ⓓⓘⓢⓒⓞⓡⓓ.ⓖⓖ/", "🅓🅘🅢🅒🅞🅡🅳.🅖🅖/", "ᵈⁱˢᶜᵒʳᵈ.ᵍᵍ/", "🄳🄸🅂🄲🄾🅁🄳.🄶🄶/", "discord.͛g͛g͛／͛", "discor̀͘d̸͛̌.̶gg/", "disco̾r̾d̾.̾g̾g̾／̾", "discơ̵̥r̶̀͘d̸͛̌.̶gg/", "disc͢ord͢.͢g͢g͢／͢", "disc͙o͙rd.͙gg／", "disc̴o̴r̴d̴.̴g̴g̴／̴", "diśc̶̉̈́o̸͆͑r̵͠d.͛gg／", "di͓s̽co͓rd͓.gg͓／͓̽", "d̲i̲s̲c̲o̲r̲d̲.̲g̲g̲／̲", "d̸iscord.g͌̔g̸̿̈／̴̛̓", "d̾i̾s̾c̾o̾r̾d̾.̾g̾g̾／̾", "d̳i̳s̳c̳o̳r̳d̳.̳g̳g̳／̳", "d͎i͎s͎c͎o͎r͎d͎.͎g͎g／", "d͎i͎s͎c͎o͎r͎d͎.͎g͎g͎／͎", "d̳i̳s̳c̳o̳r̳d̳.̳g̳g̳／̳", "d͟i͟s͟c͟o͟r͟d͟.͟g͟g͟／͟", "d̼i̼s̼c̼o̼r̼d̼.̼g̼g̼／̼", "d͓̽i͓̽s͓̽c͓̽o͓̽r͓̽d.gg／", "d̷iscor̵̍̀d̶̎̈.̷͗͋g̷͊͛g̴͂͆／̴̊̎", "𝚍̷𝚒𝚜𝚌𝚘̷𝚛̷𝚍̷.𝚐̷𝚐̷／", "d̷is̷c̷o̷r̷d.̷g̷g／̷", "d̶is̶c̶o̶r̶d̶.̶g̶g̶／̶", "d̷i̷s̷c̷o̷r̷d̷.̷gg／", "d̶i̶s̶c̶o̶r̶d̶.̶g̶g̶／̶", "d̵i̵s̵c̵o̵r̵d̵.g̵g̵／̵", "d̵̰͋i̸̙̍s̷̯̕c̶̼̈́ô̶̘r̶̻͌d̵͋.gg／", "d̤̊i̤̊s̤̊c̤̊o̊rd.g̤̊g̤̊／", "d̴is͢co͡rd̷.g̡g̕／͡", "d̴i̴s̴c̴ord̴.̴g̴g／̴", "d̴i̴͗̈́sco̶rd̶͒͑.̸͙̚ḡ̸͋g̶̀̆／̷̕", "d̴̿͋i̴͗̈́s̶̈́͘c̸̓͂ò̶͋ṙ̶̅d̶͒.gg／", "d̴̞̑i̶͒scord.̵̑͝g̵̓̋g̷／", "d͘i̵͛͑scord.gg̷̢̅／̶̨͑", "discord.gg/naberius", "discord.gg/selfkill", "discordapp.com/invite\\", "𝒹𝒾𝓈𝒸𝓇𝒹", "𝓭𝒾𝓼𝐂σʳ∂", "đīꞩȼꝋɍđ.ꞡꞡ/", "DIƧᄃӨЯD", "dıscord.gg", "dısɔoɹd˙ɓɓ/", "Đł₴₵ØⱤĐ", "ds", "ds.inv1t3", "dsс.gg", "d҉s҉c҉.҉g҉g҉", "ｄｓｃ．ｇｇ", "𝐝𝐬𝐜.𝐠𝐠", "𝗱𝘀𝗰.𝗴𝗴", "𝘥𝘴𝘤.𝘨𝘨", "𝑑𝑠𝑐.𝑔𝑔", "𝒅𝒔𝒄.𝒈𝒈", "𝙙𝙨𝙘.𝙜𝙜", "𝚍𝚜𝚌.𝚐𝚐", "𝕕𝕤𝕔.𝕘𝕘", "𝔡𝔰𝔠.𝔤𝔤", "𝖉𝖘𝖈.𝖌𝖌", "𝒹𝓈𝒸.𝑔𝑔", "𝓭𝓼𝓬.𝓰𝓰", "𝖽𝗌𝖼.𝗀𝗀", "ⓓⓢⓒ.ⓖⓖ", "🅓🅢🅒.🅖🅖", "ᵈˢᶜ.ᵍᵍ", "🄳🅂🄲.🄶🄶", "🅳🆂🅲.🅶🅶", "dsc.̘g̴͑̈g̸̰̔", "dsc.̤̊g̤̊g̤̊", "dsc̍͠.̶͌̾ĝ̷̊g̸͂̚", "d̲s̲c̲.gg", "d̲s̲c̲.g̲g̲", "d͛s͛c͛.gg", "d̾s̾c̾.̾g̾g̾", "d̼sc̼.̼g̼g̼", "d͙s͙c͙.͙gg", "d̳s̳c̳.̳g̳g", "d͎i͎s͎c.͎g͎g͎", "d͟s͟c͟.͟g͟g͟", "d͢s͢c͢.͢g͢g͢", "d͓̽s͓̽c͓̽.gg", "d͓̽s͓̽c͓̽.͓̽g͓̽g͓̽", "d̶s̶c̶.gg", "d̷s̷c̷.gg", "d̶s̶c̶.g̶g̶", "𝚍̷𝚜̷𝚌̷.𝚐̷𝚐̷", "d̷s̷c̷.̷gg", "d̶s̶c̶.̶g̶g̶", "d̷̽sc.gg", "d̷̜̕ṡ̵͙c̶̅͝.̴̽̂g̵̦̅g̷̈̾", "d̵s̵c̵.̵g̵g̵", "d̵̏s̶̈́̄c̷̥̎.gg̷̍", "d̵̰͋s̷̯̕c̶̼̈́.̷̖̉g̶̙͌g̶̙͌", "ḍṣc̣.g̣g̣", "d̴s̴c̴.gg", "d̴s̴c̴.̴g̴g̴", "d̴̎̎s̷͝c.͒̒g̷͔͝g̵", "ⓓⓢⓒ⃝ⓖⓖ", "dsɔ˙ɓɓ", "𝔡𝔰.𝔤𝔤", "dȿc.gg", "DƧᄃ.GG", "dᎥsᏟoʀd.gg", "dᎥsᏟoʀd̷ᵍᵍ／̶", "ᴅ𝟣ꜱᴄᴀʀᴅ.ᴄᴏᴍ", "ᴅ𝟣ꜱᴄᴀʀᴅ.ɢɢ", "ᴅɪꜱᴄᴏʀᴅ", "ᴅɪꜱᴄᴏʀᴅ.ᴄᴏᴍ/ɪɴᴠɪᴛᴇ\\", "ᴅɪꜱᴄᴏʀᴅ.ɢɢ", "ᴅɪꜱᴄᴏʀᴅ.ɢɢ/", "ᴅsᴄ.ɢɢ/", "ᴅꜱᴄ.ɢɢ", "ɖ1ƈƙɑɾɖ", "ɖıʂƈơཞɖ", "ɖɨֆƈօʀɖ", "ɖɨֆƈօʀɖ.ɢɢ/", "ɖʂƈ.ɠɠ", "ɖֆƈ.ɢɢ", "ƊƖⳜƇⰙⱤƊ.ƓƓ", "ɗʂɔ.ɠɠ", "ƊⲊζ.𐌾𐌾", "Ɗ𐍊Ⲋζ𐍈𐍂Ɗ.𐌾𐌾/", "everyone", "ᴇбᴇʍ ʍᴀʍу оʙнᴇᴩоʙ ʙʏ !⛧", "ᴇʙᴇʍ xуйню ʙʏ ->", "ꜰᴜᴄᴋᴇᴅ ᴇᴨᴛᴀ ʙʏ ->", "gg? By", "gg./", "gg.🅳|🅂🅲", "gg.discord", "gg.Ꭰ|ꙅᏟ", "gg.ꓷꙅᏟ", "gg/discord", "gg/invite", "goo.su", "ɢɢ", "hello", "HELLO BY", "here", "https", "https://discord.gg/", "𝒽𝓉𝓉𝓅𝓈://𝒹𝒾𝓈𝒸ℴ𝓇𝒹.ℊℊ/", "𝕙𝕥𝕥𝕡𝕤://𝕕𝕚𝕤𝕔𝕠𝕣𝕕.𝕘𝕘/", "𝓱𝓽𝓽𝓫𝕤://𝓭𝓲𝓼𝓬𝓸𝓻𝓭.𝓰𝓰/", "𝔥𝔱𝔱𝔭𝔰://𝔡𝔦𝔰𝔠𝔬𝔯𝔡.𝔤𝔤/", "https:̳/̳/̳d̳isc̳o̳r̳d̳.̳g̳g̳/", "htt̷ps̷:̷/̷/̷d̷i̷sco̷r̷d̷.̷g̷g/", "h̾tp̾s̾:/̾/̾d̾i̾s̾c̾or̾d̾.̾gg/", "h͎t͎tps͎:͎/͎/d͎i͎s͎c͎o͎r͎d.gg/", "h͓̽t̽tp͓̽s͓̽://disco͓̽r͓̽d͓̽.͓̽g̽g/", "h̶t̶t̶p̶s://̶d̶i̶s̶c̶o̶r̶d̶.̶g̶g̶/̶", "h̴https:/̴/̴dis̴co̴rd̴.̴g̴g̴/", "https://discord.gg/4D3CmGuA", "ｈｔｔｐｓ：／／ｄｉｓｃｏｒｄ。ｇｇ／", "ʰᵗᵗᵖˢ⠃ᐟᐟᵈⁱˢᶜᵒʳᵈ·ᵍᵍᐟ", "ʜᴇʟʟᴏ ꜰʀ0ᴍ ->", "ʜᴛᴛᴘs://ᴅɪsᴄᴏʀᴅ.ɢɢ/", "ʜᴛᴛᴘꜱ://ᴅɪꜱᴄᴏʀᴅ.ɢɢ/", "invite", "🅸🅽🆅🅸🆃🅴", "katantikadox", "krush", "mе/", "ᴎꙅʏlᴎႱƹƹႱ", "ᴎႱƹƹ", "p3r33zd", "PЕЙД", "pеreezd", "PEЙД", "pereezd", "ｐｅｒｅｅｚｄ", "𝐩𝐞𝐫𝐞𝐞𝐳𝐝", "𝗽𝗲𝗿𝗲𝗲𝘇𝗱", "𝑝𝑒𝑟𝑒𝑒𝑧𝑑", "𝘱𝘦𝘳𝘦ｅｚｄ", "𝒑𝒆𝒓𝒆𝒆𝒛𝒅", "𝙥𝒓𝒊𝒗𝒆𝒕", "𝕡𝕖𝕣𝕖𝕖𝕫𝕕", "𝖕𝖊𝖗𝖊𝖊𝖟𝖉", "𝔭𝖊𝖗𝖊𝖊𝔷𝔡", "𝓅𝑒𝓇𝑒𝑒𝓏𝒹", "𝚙𝚎𝚛ｅｅｚｄ", "ⓅⓔⓡⓔⓔⓏⓓ", "🄿🄴🅁🄴🄴🅉🄳", "🅿🄴🆁🅴🅴🅉🅳", "pêrêêžd", "p̲e̲r̲e̲e̲z̲d̲", "p̶e̶r̶e̶e̶z̶d̶", "p̣ẹṛẹẹẓḍ", "pısɔoɹp˙ƃƃ/", "ｐｒｉｖｅｔ", "𝐩𝐫𝐢𝐯𝐞𝐭", "𝗽𝗿𝗶𝘃𝗲𝘁", "𝑝𝑟𝑖𝑣𝑒𝑡", "𝘱𝘳𝘪𝘷𝘦𝘵", "𝒑𝒓𝒊𝒗𝒆𝒕", "𝙥𝒓𝒊𝒗𝒆𝒕", "𝕡𝕣𝕚𝕧𝕖𝕥", "𝖕𝖗𝖎𝖛𝖊𝖙", "𝔭𝔯𝔦𝔳𝔢𝔱", "𝓅𝓇𝒾𝓋𝑒𝓉", "𝚙𝚛ｉ𝚟ｅｔ", "ⓟⓡⓘⓥⓔⓣ", "🄿🅁🄸🅅🄴🅃", "🅿🆁🅸🅅🅴🆃", "p̲r̲i̲v̲e̲t̲", "p̶r̶i̶v̶e̶t̶", "p̣ṛịṿẹṭ", "pɹoɔsıp", "ps:/", "r@id", "rаid", "rаíd", "raid", "ｒａｉｄ", "𝐫𝐚𝐢𝐝", "𝗋𝖺𝗂𝖽", "𝗿𝗮𝗶𝗱", "𝑟𝑎𝑖𝑑", "𝘴𝘳𝘢𝘪𝘥", "𝒓𝒂𝒊𝒅", "𝙧𝒂𝒊𝒅", "𝕣𝕒𝕚𝕕", "𝖗𝖆𝖎𝖉", "𝔯𝔞𝔦𝔡", "𝓇𝒶𝒾𝒹", "𝚛𝚊𝚒𝚍", "RAID", "Ⓡⓐⓘⓓ", " enligt 🅸🅳", "🆁🅰🅸🅳", "r̲a̲i̲d̲", "r̶a̶i̶d̶", "ṛạịḍ", "rαíd", "spam", "ｓｐａｍ", "𝐬𝐩𝐚𝐦", "𝗌𝗉𝖺𝗆", "𝘀𝗽𝗮𝗺", "𝑠𝑝𝑎𝑚", "𝘴𝘱𝘢𝘮", "𝒔𝒑𝒂𝒎", "𝙨𝙥𝙖𝙢", "𝕤𝕡𝕒𝕞", "𝖘𝖕𝖆𝖒", "𝔰𝔭𝔞𝔪", "𝓈𝓅𝒶𝓂", "𝚜𝚙𝚊𝚖", "ⓢⓟⓐⓜ", "🅂🄿🄰🄼", "🆂🅿🅰🅼", "s̲p̲a̲m̲", "s̶p̶a̶m̶", "ṣp̣ạṃ", "spotify*", "*spotify*", "spotify", "spαm", "Squаd", "Squaд", "squad", "Squad", "SQUAD", "suka.mom", "t.ме", "t.мe", "t.mе", "t.mе/", "t.me", "t.me/", "telegram", "telegram.com", "telegram.dog", "telegram.me", "telegram.org", "terractov", "tinyurl", "Wingdings: ♎︎♓︎⬧︎♍︎□︎❒︎♎︎", "workupload", "youtube.com /invite\\", "ᴧиʙᴀᴇʍ ᴄ ᴨоʍойᴋи нᴀхуй ʙʏ sɪʟᴇɴᴄᴇ ᴄᴀʀᴀᴛᴇʟs -⛧", "ᴨоᴄᴀдиᴧ нᴀ чᴧᴇн ʙʏ -⛧", "ღ(¯`◕‿◕´¯) ♫ ♪ ♫ 𝐃𝕚Ş𝓬𝓸𝐑𝓓 ♫ ♪ ♫ (¯`◕‿◕´¯)ღ", "ժìʂçօɾժ", "ժìʂçօɾժ.ցց/", "ժʂç.ցց", "ඏ☆ đ𝕀Ş𝐂ｏⓡ∂ ✋👹", "ඏ✊ ᗪᎥ𝕊Ｃ𝔬яＤ 🐠✋", "ᦔᦓᥴ.ᧁᧁ", "Ꭰ1ᏟᏦᎯᎡᎠ", "ᎴᎥᏕፈᎧᏒᎴ", "ᎴᎥᏕፈᎧᏒᎴ.ᎶᎶ/", "ᎴᏕፈ.ᎶᎶ", "Ꮷiscord.gg", "ᕲSᑢ.ᘜᘜ", "ᕲᓰSᑢᓍᖇᕲ", "ᕲᓰSᑢᑢᓍᖇᕲ.ᘜᘜ/", "ᗪ1ᑕᛕᗩᖇᗪ", "ᗪIᔕᑕOᖇᗪ", "ᗪIᔕᑕOᖇᗪ.GG/", "ᗪ丨丂匚ㄖ尺ᗪ", "ᗪ丨丂匚ㄖ尺ᗪ.ᎶᎶ/", "りﾉ丂ᄃの尺り", "りﾉ丂ᄃの尺り.ムム/", "り丂ᄃ.ムム", "ꓷꙅcoᴙꓷ.gg", "ꓷiscord.gg", "𐌃𐌉𐌔𐌂Ꝋ𐌓𐌃.ᏵᏵ/", "𐌃𐌔𐌂.ᏵᏵ", "𓆩đꞩȼ.ꞡꞡ𓆪", "𓆩đꞩȼ.ꞡꞡ𓆪 https ᴅɪꜱᴄᴏʀᴅ.ɢɢ", "̷𝚒𝚜𝚌𝚘̷𝚛̷𝚍̷.𝚐̷𝚐̷/", "rg3w telegram.dog", "𝖘𝖈𝖔𝖗𝖉.𝖌𝖌/", "$$$$ discord.gg",
]

async def punish_user(message, reason, matched_pattern=None):
    """Функция наказания с сохранением информации о пользователе"""
    global last_punished_user

    try:
        # Получаем название сервера
        guild_name = message.guild.name if message.guild else "Личные сообщения/Неизвестный сервер"

        # Логирование в консоль и файл
        log_msg = f"🔍 Обнаружено нарушение от {message.author} на сервере [{guild_name}]: {message.content}"
        if matched_pattern:
            if isinstance(matched_pattern, re.Pattern):
                log_msg += f"\n🚫 Причина: сработал паттерн - {matched_pattern.pattern}"
            else:
                log_msg += f"\n🚫 Причина: запрещенная фраза - '{matched_pattern}'"
        print(log_msg)
        log_action(message.author, "violation_detected", f"Причина: {reason}")

        # Удаление сообщения
        try:
            await message.delete()
            print("✅ Сообщение удалено")
            log_action(message.author, "message_deleted", "Нарушение правил")
        except Exception as e:
            print(f"❌ Ошибка удаления: {e}")
            log_action(message.author, "message_delete_error", str(e))
            return

        # Тайм-аут на 1 день
        try:
            timeout_duration = timedelta(days=1)
            await message.author.timeout(timeout_duration, reason=reason)
            print(f"⏳ Пользователь {message.author} получил тайм-аут на 1 день")
            log_action(message.author, "timeout_applied", f"Длительность: 1 день, Причина: {reason}")
            last_punished_user = message.author
        except Exception as e:
            print(f"❌ Ошибка тайм-аута: {e}")
            log_action(message.author, "timeout_error", str(e))

        # Отправка уведомления администратору
        await notify_admin(message.author, message.content, reason, matched_pattern)

    except Exception as e:
        print(f"🔥 Критическая ошибка: {e}")
        log_action(message.author, "critical_error", str(e))

async def remove_timeout(user):
    """Снять тайм-аут с пользователя"""
    try:
        await user.timeout(None)
        print(f"\n✅ Тайм-аут снят с пользователя {user}")
        log_action("SYSTEM", "timeout_removed", f"Пользователь: {user}")
        return True
    except Exception as e:
        print(f"\n❌ Ошибка при снятии тайм-аута: {e}")
        log_action("SYSTEM", "timeout_remove_error", f"Пользователь: {user}, Ошибка: {str(e)}")
        return False

def console_command_handler():
    """Обработчик команд из консоли"""
    while True:
        cmd = input("\nВведите команду ('unlock' для снятия тайм-аута, 'exit' для выхода): ").strip().lower()

        if cmd == 'exit':
            log_action("SYSTEM", "console_command", "Команда: exit")
            print("Завершение работы...")
            # Отправляем стоп-сигнал в очередь перед выходом
            try:
                asyncio.run_coroutine_threadsafe(telegram_log_queue.put(None), bot.loop)
            except Exception as e:
                print(f"Error sending stop signal to telegram queue: {e}")
            os._exit(0)

        elif cmd == 'unlock':
            log_action("SYSTEM", "console_command", "Команда: unlock")
            if last_punished_user:
                asyncio.run_coroutine_threadsafe(remove_timeout(last_punished_user), bot.loop)
            else:
                print("❌ Нет информации о последнем наказанном пользователе")
                log_action("SYSTEM", "unlock_failed", "Нет информации о последнем наказанном пользователе")

@bot.event
async def on_message(message):
    # Пропускаем сообщения от самого бота, админов и пользователей из вайтлиста
    if (message.author.id == bot.user.id) or \
       (message.guild and message.guild.get_member(message.author.id) and message.guild.get_member(message.author.id).guild_permissions.administrator):
        return

    # Проверяем вайтлист только для сообщений на сервере
    if message.guild:
        guild_id = message.guild.id
        # Если сервер есть в вайтлисте и автор сообщения в списке этого сервера
        if guild_id in WHITELIST_IDS and message.author.id in WHITELIST_IDS[guild_id]:
            log_action(message.author, "on_whitelist", f"Пользователь в вайтлисте сервера {message.guild.name}")
            return # Пропускаем проверку на спам для пользователей из вайтлиста сервера

    content = message.content

    # Логируем сообщение для проверки
    log_action(message.author, "message_check", f"Канал: {message.channel.name}")

    # Сначала проверяем точные совпадения
    for phrase in blocked_phrases:
        if phrase in content:
            log_action(message.author, "blocked_phrase_detected", f"Фраза: {phrase}")
            await punish_user(message, f"Запрещенная фраза: {phrase}")
            return

    # Затем проверяем регулярные выражения
    for pattern in spam_patterns:
        try:
            if pattern.search(content):
                log_action(message.author, "spam_pattern_detected", f"Паттерн: {pattern.pattern}")
                await punish_user(message, "Нарушение правил сервера")
                return
        except Exception as e:
            print(f"⚠ Ошибка при проверке паттерна {pattern.pattern}: {e}")
            log_action(message.author, "pattern_check_error", f"Паттерн: {pattern.pattern}, Ошибка: {str(e)}")

    # Проверка embed-ов на любые запрещённые ссылки и фразы (включая все поля)
    for embed in message.embeds:
        title = (getattr(embed, 'title', '') or '').lower()
        description = (getattr(embed, 'description', '') or '').lower()
        url = (getattr(embed, 'url', '') or '').lower()

        # Сначала специфичная проверка на музыкальные приглашения (Spotify, Яндекс.Музыка и т.д.)
        if (
            'spotify' in title or
            'listen along' in title or
            'spotify' in description or
            'spotify.com' in url or
            'soundcloud' in title or
            'soundcloud' in description or
            'soundcloud.com' in url or
            'deezer' in title or
            'deezer' in description or
            'deezer.com' in url or
            'apple music' in title or
            'apple music' in description or
            'apple.com/music' in url or
            'яндекс' in title or
            'яндекс' in description or
            'music.yandex' in url
        ):
            log_action(message.author, "music_invite_embed_detected", f"Embed title: {title}, url: {url}")
            await punish_user(message, "Приглашение слушать музыку через embed-инвайт")
            return # Останавливаем обработку, так как нарушение найдено

        # Затем универсальная проверка всех полей embed на остальные запрещённые ссылки и фразы
        embed_fields = [title, description, url]
        # Добавляем все значения из embed.fields
        for field in getattr(embed, 'fields', []):
            embed_fields.append((getattr(field, 'name', '') or '').lower())
            embed_fields.append((getattr(field, 'value', '') or '').lower())
        # Проверка по точным фразам
        for phrase in blocked_phrases:
            for field in embed_fields:
                if phrase in field:
                    log_action(message.author, "blocked_phrase_detected_in_embed", f"Фраза: {phrase}")
                    await punish_user(message, f"Запрещенная фраза в embed: {phrase}")
                    return
        # Проверка по паттернам
        for pattern in spam_patterns:
            for field in embed_fields:
                try:
                    if pattern.search(field):
                        log_action(message.author, "spam_pattern_detected_in_embed", f"Паттерн: {pattern.pattern}")
                        await punish_user(message, "Нарушение правил сервера (embed)", pattern)
                        return
                except Exception as e:
                    print(f"⚠ Ошибка при проверке паттерна {pattern.pattern} в embed: {e}")
                    log_action(message.author, "pattern_check_error_in_embed", f"Паттерн: {pattern.pattern}, Ошибка: {str(e)}")

    await bot.process_commands(message)

# Команды для управления вайтлистом по серверам

@bot.tree.command(name="addwl", description="Добавить пользователя в вайтлист текущего сервера")
@app_commands.default_permissions(administrator=True)
async def add_whitelist(interaction: discord.Interaction, user_id: str):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("❌ Эту команду можно использовать только на сервере", ephemeral=True)
        return

    log_action(interaction.user, "add_whitelist_attempt", f"Сервер: {interaction.guild.name}, ID пользователя: {user_id}")
    guild_id = interaction.guild.id

    try:
        user_id_int = int(user_id)

        # Создаем список для сервера, если его еще нет
        if guild_id not in WHITELIST_IDS:
            WHITELIST_IDS[guild_id] = []

        if user_id_int not in WHITELIST_IDS[guild_id]:
            WHITELIST_IDS[guild_id].append(user_id_int)
            await interaction.response.send_message(f"✅ Пользователь с ID {user_id} добавлен в вайтлист сервера `{interaction.guild.name}`", ephemeral=True)
            log_action(interaction.user, "add_whitelist_success", f"Сервер: {interaction.guild.name}, Добавлен ID: {user_id}")
        else:
            await interaction.response.send_message("❌ Этот пользователь уже находится в вайтлисте сервера", ephemeral=True)
            log_action(interaction.user, "add_whitelist_failed", f"Сервер: {interaction.guild.name}, ID {user_id} уже в вайтлисте")

    except ValueError:
        await interaction.response.send_message("❌ Неверный формат ID. Пожалуйста, введите числовой ID пользователя", ephemeral=True)
        log_action(interaction.user, "add_whitelist_error", f"Сервер: {interaction.guild.name}, Неверный формат ID: {user_id}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Произошла ошибка при добавлении в вайтлист: {str(e)}", ephemeral=True)
        log_action(interaction.user, "add_whitelist_error", f"Сервер: {interaction.guild.name}, Ошибка: {str(e)}")

@bot.tree.command(name="delwl", description="Удалить пользователя из вайтлиста текущего сервера")
@app_commands.default_permissions(administrator=True)
async def remove_whitelist(interaction: discord.Interaction, user_id: str):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("❌ Эту команду можно использовать только на сервере", ephemeral=True)
        return

    log_action(interaction.user, "remove_whitelist_attempt", f"Сервер: {interaction.guild.name}, ID пользователя: {user_id}")
    guild_id = interaction.guild.id

    try:
        user_id_int = int(user_id)

        if guild_id in WHITELIST_IDS and user_id_int in WHITELIST_IDS[guild_id]:
            WHITELIST_IDS[guild_id].remove(user_id_int)
            # Удаляем запись о сервере, если список вайтлиста пуст
            if not WHITELIST_IDS[guild_id]:
                del WHITELIST_IDS[guild_id]
            await interaction.response.send_message(f"✅ Пользователь с ID {user_id} удален из вайтлиста сервера `{interaction.guild.name}`", ephemeral=True)
            log_action(interaction.user, "remove_whitelist_success", f"Сервер: {interaction.guild.name}, Удален ID: {user_id}")
        else:
            await interaction.response.send_message("❌ Этот пользователь не найден в вайтлисте сервера", ephemeral=True)
            log_action(interaction.user, "remove_whitelist_failed", f"Сервер: {interaction.guild.name}, ID {user_id} не в вайтлисте")

    except ValueError:
        await interaction.response.send_message("❌ Неверный формат ID. Пожалуйста, введите числовой ID пользователя", ephemeral=True)
        log_action(interaction.user, "remove_whitelist_error", f"Сервер: {interaction.guild.name}, Неверный формат ID: {user_id}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Произошла ошибка при удалении из вайтлиста: {str(e)}", ephemeral=True)
        log_action(interaction.user, "remove_whitelist_error", f"Сервер: {interaction.guild.name}, Ошибка: {str(e)}")

@bot.tree.command(name="listwl", description="Показать список пользователей в вайтлисте текущего сервера")
@app_commands.default_permissions(administrator=True)
async def list_whitelist(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("❌ Эту команду можно использовать только на сервере", ephemeral=True)
        return

    log_action(interaction.user, "list_whitelist_command", f"Сервер: {interaction.guild.name}")
    guild_id = interaction.guild.id

    embed = discord.Embed(title=f"📋 Вайтлист сервера `{interaction.guild.name}`", color=discord.Color.green())

    if guild_id in WHITELIST_IDS and WHITELIST_IDS[guild_id]:
        user_list = []
        for user_id in WHITELIST_IDS[guild_id]:
            try:
                user = await bot.fetch_user(user_id)
                user_list.append(f"{user.name} (ID: {user_id})")
            except:
                user_list.append(f"Неизвестный пользователь (ID: {user_id})")
        embed.description = "\n".join(user_list)
        log_action(interaction.user, "list_whitelist_success", f"Сервер: {interaction.guild.name}, Показано пользователей: {len(user_list)}")
    else:
        embed.description = "Вайтлист сервера пуст"
        log_action(interaction.user, "list_whitelist_success", f"Сервер: {interaction.guild.name}, Вайтлист пуст")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# Важно! Замените этот токен на ваш реальный токен бота
TOKEN = 'token discord bot'

async def start_bot():
    max_retries = 5
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            # Используем обычный запуск без параметра mobile
            await bot.start(TOKEN)
        except aiohttp.ClientConnectorError as e:
            if attempt < max_retries - 1:
                print(f"Ошибка подключения (попытка {attempt + 1}/{max_retries}): {e}")
                print(f"Повторная попытка через {retry_delay} секунд...")
                await asyncio.sleep(retry_delay)
            else:
                print("Превышено максимальное количество попыток подключения")
                raise
        except Exception as e:
            print(f"Неожиданная ошибка: {e}")
            raise

@bot.tree.command(name="srv", description="Показать список серверов, где находится бот")
@app_commands.default_permissions(administrator=True)
async def list_guilds(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды", ephemeral=True)
        return
    guilds_info = [f"{g.name} (ID: {g.id})" for g in bot.guilds]
    await interaction.response.send_message("\n".join(guilds_info), ephemeral=True)

@bot.tree.command(name="lvsrv", description="Выйти с сервера по ID")
@app_commands.default_permissions(administrator=True)
async def leave_guild(interaction: discord.Interaction, guild_id: str):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("❌ У вас нет прав для использования этой команды", ephemeral=True)
        return
    try:
        guild = bot.get_guild(int(guild_id))
        if guild:
            await guild.leave()
            await interaction.response.send_message(f"✅ Бот покинул сервер: {guild.name} (ID: {guild_id})", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Сервер с таким ID не найден среди серверов бота", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {str(e)}", ephemeral=True)

if __name__ == "__main__":
    # Fix for aiodns on Windows requiring SelectorEventLoop
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    keep_alive()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(start_bot())
    except KeyboardInterrupt:
        # Добавляем ожидание завершения задачи Telegram логирования при KeyboardInterrupt
        if bot and bot._telegram_logger_task:
            asyncio.run_coroutine_threadsafe(telegram_log_queue.put(None), loop).result()
            loop.run_until_complete(bot._telegram_logger_task)
        # Здесь не вызываем bot.close(), т.к. он вызывается в start_bot() при нормальном завершении
        # Если KeyboardInterrupt происходит во время start_bot(), close() должен быть вызван внутри
        # Если нужно гарантировать закрытие при KeyboardInterrupt, можно добавить try/finally вокруг loop.run_until_complete
        pass # Убираем лишний вызов close()
    # finally:
        # loop.close() # Не закрываем loop, если он уже используется