import sys
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

try:
    from telethon import TelegramClient, events, Button
    from telethon.tl.types import Message
    import aiohttp
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    print("Установите зависимости: pip install telethon aiohttp apscheduler")
    sys.exit(1)

API_ID = 33328241
API_HASH = "71b285e3b1fbaa3a2f84231dc61d23fa"
BOT_TOKEN = "8741722045:AAH9RURmSjys4ymsMlzbNjebsLPn9WlTZ2M"
GROQ_API_KEY = "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"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
ADMIN_ID = 6689292068

MAX_FREE_CHANNELS = 10
MAX_PREMIUM_CHANNELS = 50
FREE_DIGESTS_PER_DAY = 1
PREMIUM_DIGESTS_PER_DAY = 3
PREMIUM_PRICE_STARS = 30

class InMemoryDB:
    def __init__(self):
        self.users: Dict[int, Dict] = {}
        self.user_channels: Dict[int, List[tuple]] = defaultdict(list)
        self.posts: List[Dict] = []
        self.reactions: List[Dict] = []
        self.digests_sent_today: Dict[int, int] = defaultdict(int)
        self.last_reset_day: int = datetime.now().day
        print("✅ База данных в памяти готова")
    
    def _reset_daily_counters(self):
        today = datetime.now().day
        if today != self.last_reset_day:
            self.digests_sent_today.clear()
            self.last_reset_day = today
    
    def get_user(self, user_id: int) -> Dict:
        if user_id not in self.users:
            self.users[user_id] = {
                'user_id': user_id,
                'username': '',
                'first_name': '',
                'is_premium': False,
                'premium_until': None,
                'channels_limit': MAX_FREE_CHANNELS,
                'digests_per_day': FREE_DIGESTS_PER_DAY,
                'voice_enabled': True,
                'summary_style': 'default',
                'total_digests': 0,
                'joined_at': datetime.now().isoformat()
            }
        return self.users[user_id]
    
    def make_premium(self, user_id: int, days: int = 30):
        user = self.get_user(user_id)
        user['is_premium'] = True
        user['premium_until'] = (datetime.now() + timedelta(days=days)).isoformat()
        user['channels_limit'] = MAX_PREMIUM_CHANNELS
        user['digests_per_day'] = PREMIUM_DIGESTS_PER_DAY
    
    def add_channel(self, user_id: int, channel_id: int, channel_name: str) -> bool:
        self.get_user(user_id)
        channels = self.user_channels[user_id]
        if not any(ch[0] == channel_id for ch in channels):
            if len(channels) < self.users[user_id]['channels_limit']:
                channels.append((channel_id, channel_name))
                return True
        return False
    
    def remove_channel(self, user_id: int, channel_id: int):
        self.user_channels[user_id] = [ch for ch in self.user_channels[user_id] if ch[0] != channel_id]
    
    def get_channels(self, user_id: int) -> List[tuple]:
        return self.user_channels.get(user_id, [])
    
    def add_post(self, channel_id: int, channel_name: str, text: str, link: str):
        self.posts.append({
            'channel_id': channel_id,
            'channel_name': channel_name,
            'text': text,
            'link': link,
            'time': datetime.now().isoformat(),
            'used': False
        })
    
    def get_fresh_posts(self, user_id: int, limit: int = 20) -> List[Dict]:
        self._reset_daily_counters()
        channels = self.get_channels(user_id)
        channel_ids = [ch[0] for ch in channels]
        
        fresh = []
        for post in self.posts:
            if post['channel_id'] in channel_ids and not post['used']:
                fresh.append(post)
        return fresh[-limit:]
    
    def mark_used(self, posts: List[Dict]):
        for post in posts:
            if post in self.posts:
                self.posts[self.posts.index(post)]['used'] = True
    
    def can_send_digest(self, user_id: int) -> bool:
        self._reset_daily_counters()
        user = self.get_user(user_id)
        return self.digests_sent_today[user_id] < user['digests_per_day']
    
    def record_digest(self, user_id: int):
        self.digests_sent_today[user_id] += 1
        self.users[user_id]['total_digests'] += 1
    
    def add_reaction(self, user_id: int, digest_id: str, reaction: str):
        self.reactions.append({
            'user_id': user_id,
            'digest_id': digest_id,
            'reaction': reaction,
            'time': datetime.now().isoformat()
        })
    
    def get_stats(self) -> Dict:
        total = len(self.users)
        premium = sum(1 for u in self.users.values() if u['is_premium'])
        total_posts = len(self.posts)
        total_channels = sum(len(chs) for chs in self.user_channels.values())
        total_digests = sum(u['total_digests'] for u in self.users.values())
        return {
            'users': total,
            'premium': premium,
            'posts': total_posts,
            'channels': total_channels,
            'digests': total_digests
        }

db = InMemoryDB()

async def summarize_text(text: str, style: str = "default") -> str:
    if not text or len(text) < 50:
        return text
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    style_prompts = {
        "default": "Сделай краткое саммари (2-3 предложения) на русском языке. Только саммари, без вступлений:",
        "business": "Сделай деловое саммари в формальном стиле для отчета. Только суть, без лишних слов:",
        "casual": "Сделай краткий пересказ в разговорном стиле, как для друга. Коротко и ясно:",
        "twitter": "Сделай саммари в стиле твита (до 280 символов). Очень кратко:"
    }
    
    prompt = f"{style_prompts.get(style, style_prompts['default'])}\n\n{text[:4000]}"
    
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "Ты — ассистент для саммаризации новостей. Отвечай только на русском языке. Будь краток и по делу."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 300,
        "temperature": 0.3
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_URL, headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['choices'][0]['message']['content'].strip()
                else:
                    error_text = await resp.text()
                    logging.error(f"Groq API error ({resp.status}): {error_text[:200]}")
                    return text[:200] + "..."
    except asyncio.TimeoutError:
        logging.error("Groq API timeout")
        return text[:200] + "..."
    except Exception as e:
        logging.error(f"Summarize error: {e}")
        return text[:200] + "..."

async def create_digest(posts: List[Dict], style: str = "default") -> str:
    if not posts:
        return "📭 Нет новых постов для дайджеста."
    
    if len(posts) == 1:
        summary = await summarize_text(posts[0]['text'], style)
        return f"📋 *Дайджест FocusFlow*\n\n*{posts[0]['channel_name']}*\n{summary}\n\n🔗 [Источник]({posts[0]['link']})"
    
    combined_parts = []
    for i, post in enumerate(posts[:5], 1):
        clean_text = post['text'][:500].replace('\n', ' ').strip()
        combined_parts.append(f"[{i}] {post['channel_name']}: {clean_text}")
    
    combined = "\n\n".join(combined_parts)
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    prompt = f"""Создай дайджест из новостей ниже. Для каждой новости:
- Напиши жирный заголовок (5-7 слов)
- Одно предложение с сутью
- Эмодзи в конце

Не нумеруй. Просто заголовки и описания. В конце добавь раздел "🔥 Главное за сегодня:" с одной самой важной новостью.

{combined}"""
    
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "Ты создаешь новостные дайджесты в Telegram. Используй Markdown. Отвечай на русском. Будь полезным и кратким."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 800,
        "temperature": 0.5
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_URL, headers=headers, json=payload, timeout=45) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    digest = data['choices'][0]['message']['content'].strip()
                    
                    footer = "\n\n---\n📡 *Источники:*\n"
                    for i, post in enumerate(posts[:5], 1):
                        footer += f"{i}. [{post['channel_name']}]({post['link']})\n"
                    
                    return f"📋 *Дайджест FocusFlow*\n{datetime.now().strftime('%d.%m.%Y')}\n\n{digest}{footer}"
                else:
                    logging.error(f"Digest API error: {await resp.text()}")
    except Exception as e:
        logging.error(f"Digest error: {e}")
    
    result = f"📋 *Дайджест FocusFlow*\n{datetime.now().strftime('%d.%m.%Y')}\n\n"
    for i, post in enumerate(posts[:5], 1):
        summary = await summarize_text(post['text'], style)
        result += f"*{i}. {post['channel_name']}*\n{summary}\n\n"
    result += "\n---\n🔗 Ссылки на источники выше"
    return result

class FocusFlowBot:
    def __init__(self):
        self.client = TelegramClient('focusflow_session', API_ID, API_HASH)
        self.scheduler = AsyncIOScheduler()
        self.me = None
    
    async def start(self):
        print("🚀 Запуск FocusFlow...")
        
        await self.client.start(bot_token=BOT_TOKEN)
        self.me = await self.client.get_me()
        print(f"✅ Бот @{self.me.username} запущен")
        
        self.scheduler.add_job(
            self.send_all_digests,
            CronTrigger(hour=8, minute=0),
            id='morning'
        )
        self.scheduler.add_job(
            self.send_all_digests,
            CronTrigger(hour=13, minute=0),
            id='afternoon'
        )
        self.scheduler.add_job(
            self.send_all_digests,
            CronTrigger(hour=19, minute=0),
            id='evening'
        )
        self.scheduler.start()
        print("✅ Планировщик: 08:00, 13:00, 19:00 UTC")
        
        self.client.add_event_handler(self.cmd_start, events.NewMessage(pattern='/start'))
        self.client.add_event_handler(self.cmd_add, events.NewMessage(pattern='/add'))
        self.client.add_event_handler(self.cmd_remove, events.NewMessage(pattern='/remove'))
        self.client.add_event_handler(self.cmd_list, events.NewMessage(pattern='/list'))
        self.client.add_event_handler(self.cmd_digest, events.NewMessage(pattern='/digest'))
        self.client.add_event_handler(self.cmd_premium, events.NewMessage(pattern='/premium'))
        self.client.add_event_handler(self.cmd_help, events.NewMessage(pattern='/help'))
        self.client.add_event_handler(self.cmd_stats, events.NewMessage(pattern='/stats'))
        self.client.add_event_handler(self.cmd_style, events.NewMessage(pattern='/style'))
        self.client.add_event_handler(self.handle_callback, events.CallbackQuery())
        self.client.add_event_handler(self.handle_new_post, events.NewMessage())
        
        print("✅ Обработчики готовы")
        print(f"👑 Админ: {ADMIN_ID}")
        print("=" * 50)
        
        await self.client.run_until_disconnected()
    
    async def cmd_start(self, event):
        user_id = event.sender_id
        user = db.get_user(user_id)
        user['username'] = event.sender.username or ''
        user['first_name'] = event.sender.first_name or ''
        
        name = user['first_name'] or 'Пользователь'
        
        text = f"""🎯 *Привет, {name}!*

Я *FocusFlow* — твой умный фильтр новостей.
Собираю посты из твоих каналов и создаю смарт-дайджесты с помощью ИИ.

📊 *Твой профиль:*
▸ Тариф: {'⭐ Premium' if user['is_premium'] else '🆓 Бесплатный'}
▸ Каналов: {len(db.get_channels(user_id))}/{user['channels_limit']}
▸ Дайджестов сегодня: {db.digests_sent_today[user_id]}/{user['digests_per_day']}
▸ Дайджестов всего: {user['total_digests']}

*Команды:*
/add @канал — добавить канал
/remove — удалить канал
/list — мои каналы
/digest — дайджест сейчас
/style — стиль саммари
/premium — Premium за {PREMIUM_PRICE_STARS}⭐/мес
/help — помощь
"""
        buttons = [
            [Button.inline("📋 Создать дайджест", "action_digest")],
            [Button.inline("➕ Добавить канал", "action_add")],
            [Button.inline("⭐ Premium", "action_premium")]
        ]
        
        await event.reply(text, buttons=buttons, link_preview=False)
    
    async def cmd_add(self, event):
        user_id = event.sender_id
        user = db.get_user(user_id)
        
        args = event.text.split()
        if len(args) < 2:
            await event.reply(
                "ℹ️ *Добавление канала*\n\n"
                "Отправьте команду:\n`/add @имя_канала`\n\n"
                "Или перешлите любой пост из канала сюда.",
                link_preview=False
            )
            return
        
        channel_ref = args[1].replace('@', '').replace('https://t.me/', '')
        
        try:
            entity = await self.client.get_entity(f'@{channel_ref}')
            
            if db.add_channel(user_id, entity.id, entity.title):
                count = len(db.get_channels(user_id))
                await event.reply(
                    f"✅ *Канал добавлен!*\n\n"
                    f"📡 {entity.title}\n"
                    f"📊 Каналов: {count}/{user['channels_limit']}",
                    link_preview=False
                )
            else:
                limit = user['channels_limit']
                await event.reply(
                    f"❌ *Лимит исчерпан!*\n\n"
                    f"У вас {limit} каналов (максимум).\n"
                    f"Подключите Premium для 50 каналов: /premium",
                    link_preview=False
                )
        except ValueError:
            await event.reply("❌ Канал не найден. Проверьте, что канал публичный (@имя).")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {e}")
    
    async def cmd_remove(self, event):
        user_id = event.sender_id
        channels = db.get_channels(user_id)
        
        if not channels:
            await event.reply("📭 У вас нет каналов. Добавьте: /add @канал")
            return
        
        buttons = []
        for ch_id, ch_name in channels:
            short_name = ch_name[:30] + "..." if len(ch_name) > 30 else ch_name
            buttons.append([Button.inline(f"❌ {short_name}", f"remove_{ch_id}")])
        
        buttons.append([Button.inline("« Отмена", "cancel_remove")])
        
        await event.reply("🗑 *Выберите канал для удаления:*", buttons=buttons)
    
    async def cmd_list(self, event):
        user_id = event.sender_id
        channels = db.get_channels(user_id)
        
        if not channels:
            await event.reply("📭 Нет каналов. /add @канал")
            return
        
        text = "📡 *Мои каналы:*\n\n"
        for i, (ch_id, ch_name) in enumerate(channels, 1):
            text += f"{i}. {ch_name}\n"
        text += f"\nВсего: {len(channels)}"
        
        await event.reply(text, link_preview=False)
    
    async def cmd_digest(self, event):
        user_id = event.sender_id
        user = db.get_user(user_id)
        
        if not db.can_send_digest(user_id):
            await event.reply(
                f"⏰ *Лимит дайджестов на сегодня!*\n\n"
                f"Вы получили {user['digests_per_day']} из {user['digests_per_day']}.\n"
                f"Premium даёт {PREMIUM_DIGESTS_PER_DAY} дайджестов в день: /premium",
                link_preview=False
            )
            return
        
        channels = db.get_channels(user_id)
        if not channels:
            await event.reply("📭 Сначала добавьте каналы: /add @канал")
            return
        
        wait_msg = await event.reply("🔄 *Создаю дайджест...*\nАнализирую посты с помощью ИИ...")
        
        posts = db.get_fresh_posts(user_id, limit=20)
        
        if not posts:
            await wait_msg.edit("📭 *Нет новых постов*\n\nВсе посты уже в предыдущих дайджестах. Ждите обновлений!")
            return
        
        digest = await create_digest(posts[:5], user['summary_style'])
        db.mark_used(posts[:5])
        db.record_digest(user_id)
        
        buttons = [
            [
                Button.inline("👍 Полезно", f"react_like_{datetime.now().timestamp()}"),
                Button.inline("👎 Не очень", f"react_dislike_{datetime.now().timestamp()}")
            ],
            [Button.inline("📋 Ещё дайджест", "action_digest")]
        ]
        
        await wait_msg.delete()
        await event.reply(digest, buttons=buttons, link_preview=False)
    
    async def cmd_premium(self, event):
        user_id = event.sender_id
        user = db.get_user(user_id)
        
        if user['is_premium']:
            until = user.get('premium_until', '')
            text = f"""⭐ *Premium активен!*

▸ Каналов: до {MAX_PREMIUM_CHANNELS}
▸ Дайджестов: до {PREMIUM_DIGESTS_PER_DAY} в день
▸ Активен до: {until[:10] if until else 'навсегда'}

Спасибо за поддержку! 🚀"""
        else:
            text = f"""⭐ *Premium подписка*

Цена: *{PREMIUM_PRICE_STARS} Telegram Stars/месяц*

*Что даёт:*
▸ 📡 50 каналов (вместо 10)
▸ 🔄 3 дайджеста в день (вместо 1)
▸ 🎨 Стили саммаризации
▸ ⚡ Приоритетная обработка

*Как оплатить:*
1. Отправьте {PREMIUM_PRICE_STARS} ⭐ боту @FocusFlowBot
2. Нажмите кнопку «Я оплатил»
3. Premium активируется мгновенно!"""
        
        buttons = []
        if not user['is_premium']:
            buttons.append([Button.inline("✅ Я оплатил " + str(PREMIUM_PRICE_STARS) + "⭐", "activate_premium")])
            buttons.append([Button.url("💳 Как купить Stars", "https://t.me/telegram/197")])
        
        await event.reply(text, buttons=buttons if buttons else None, link_preview=False)
    
    async def cmd_help(self, event):
        text = """🤖 *FocusFlow — Твой поток фокуса*

*Команды:*
/add @канал — добавить канал
/remove — удалить канал
/list — список каналов
/digest — умный дайджест
/style — стиль саммаризации
/premium — Premium подписка
/stats — статистика (админ)
/help — эта справка

*Как работает:*
1. Добавляете каналы через /add
2. Бот собирает посты
3. ИИ (Groq) создаёт дайджест
4. Вы читаете только важное

*Premium:* {PREMIUM_PRICE_STARS}⭐/мес — больше каналов и дайджестов"""
        await event.reply(text, link_preview=False)
    
    async def cmd_stats(self, event):
        if event.sender_id != ADMIN_ID:
            await event.reply("🔒 Только для администратора")
            return
        
        stats = db.get_stats()
        text = f"""📊 *Статистика FocusFlow*

👥 Пользователей: {stats['users']}
⭐ Premium: {stats['premium']}
📝 Постов собрано: {stats['posts']}
📡 Каналов отслеживается: {stats['channels']}
📋 Дайджестов создано: {stats['digests']}
💾 Данные в памяти бота"""
        
        await event.reply(text, link_preview=False)
    
    async def cmd_style(self, event):
        user_id = event.sender_id
        user = db.get_user(user_id)
        
        current = user['summary_style']
        
        text = f"""🎨 *Стиль саммаризации*

Текущий: *{current}*

Выберите стиль:
• default — краткое саммари
• business — деловой стиль
• casual — разговорный
• twitter — очень кратко (до 280 символов)"""
        
        buttons = [
            [Button.inline("📝 Обычный" + (" ✅" if current == 'default' else ''), "style_default")],
            [Button.inline("💼 Деловой" + (" ✅" if current == 'business' else ''), "style_business")],
            [Button.inline("💬 Разговорный" + (" ✅" if current == 'casual' else ''), "style_casual")],
            [Button.inline("🐦 Twitter-стиль" + (" ✅" if current == 'twitter' else ''), "style_twitter")],
        ]
        
        await event.reply(text, buttons=buttons, link_preview=False)
    
    async def handle_callback(self, event):
        user_id = event.sender_id
        data = event.data.decode()
        
        if data.startswith('remove_'):
            ch_id = int(data.split('_')[1])
            channels = db.get_channels(user_id)
            channel_name = "канал"
            for cid, cname in channels:
                if cid == ch_id:
                    channel_name = cname
                    break
            db.remove_channel(user_id, ch_id)
            await event.edit(f"✅ *{channel_name}* удалён")
        
        elif data == 'cancel_remove':
            await event.edit("❌ Удаление отменено")
        
        elif data == 'action_digest':
            await event.answer("Создаю дайджест...")
            await self.cmd_digest(event)
        
        elif data == 'action_add':
            await event.answer("Добавление канала")
            await event.edit("Отправьте `/add @имя_канала`")
        
        elif data == 'action_premium':
            await event.answer("Premium")
            await self.cmd_premium(event)
        
        elif data == 'activate_premium':
            db.make_premium(user_id, days=30)
            await event.answer("✅ Premium активирован!")
            await event.edit(
                "🎉 *Premium активирован!*\n\n"
                "Теперь у вас 50 каналов и 3 дайджеста в день!\n"
                "Спасибо за поддержку! 🚀"
            )
        
        elif data.startswith('react_'):
            parts = data.split('_')
            reaction = parts[1]
            digest_id = parts[2] if len(parts) > 2 else 'unknown'
            db.add_reaction(user_id, digest_id, reaction)
            emoji = "👍" if reaction == "like" else "👎"
            await event.answer(f"Спасибо за реакцию {emoji}!")
        
        elif data.startswith('style_'):
            style = data.split('_')[1]
            db.users[user_id]['summary_style'] = style
            style_names = {
                'default': 'Обычный',
                'business': 'Деловой',
                'casual': 'Разговорный',
                'twitter': 'Twitter'
            }
            await event.answer(f"Стиль: {style_names.get(style, style)}")
            await self.cmd_style(event)
    
    async def handle_new_post(self, event):
        if event.is_private:
            return
        
        try:
            chat = await event.get_chat()
            
            is_tracked = False
            for user_id, channels in db.user_channels.items():
                if chat.id in [ch[0] for ch in channels]:
                    is_tracked = True
                    break
            
            if not is_tracked:
                return
            
            text = event.text or "[медиа]"
            if len(text) < 20:
                return
            
            link = f"https://t.me/{chat.username}/{event.id}" if chat.username else f"tg://channel?id={chat.id}&post={event.id}"
            
            db.add_post(chat.id, chat.title, text, link)
            
        except Exception:
            pass
    
    async def send_all_digests(self):
        print(f"📨 Плановая рассылка дайджестов ({datetime.now().strftime('%H:%M')})")
        
        sent = 0
        errors = 0
        
        for user_id in list(db.users.keys()):
            try:
                if not db.can_send_digest(user_id):
                    continue
                
                channels = db.get_channels(user_id)
                if not channels:
                    continue
                
                posts = db.get_fresh_posts(user_id, limit=15)
                if not posts:
                    continue
                
                user = db.get_user(user_id)
                digest = await create_digest(posts[:3], user['summary_style'])
                db.mark_used(posts[:3])
                db.record_digest(user_id)
                
                buttons = [
                    [
                        Button.inline("👍", f"react_like_auto_{datetime.now().timestamp()}"),
                        Button.inline("👎", f"react_dislike_auto_{datetime.now().timestamp()}")
                    ]
                ]
                
                await self.client.send_message(user_id, digest, buttons=buttons, link_preview=False)
                sent += 1
                await asyncio.sleep(0.5)
                
            except Exception as e:
                errors += 1
                logging.error(f"Ошибка отправки user {user_id}: {e}")
        
        print(f"  ✅ Отправлено: {sent}, ошибок: {errors}")
    
    async def run(self):
        await self.start()

async def main():
    print("=" * 50)
    print("🎯 FocusFlow Bot запускается...")
    print("=" * 50)
    
    bot = FocusFlowBot()
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        logging.error(f"Critical error: {e}", exc_info=True)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('focusflow.log', encoding='utf-8')
        ]
    )
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
