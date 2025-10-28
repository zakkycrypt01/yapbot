import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import pandas as pd
from dotenv import load_dotenv
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
import time
import urllib.request
import json
from telegram import Update, Document, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
import tweepy
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()


TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TWITTER_API_KEY = os.environ["TWITTER_API_KEY"]
TWITTER_API_SECRET = os.environ["TWITTER_API_SECRET"]
TWITTER_ACCESS_TOKEN = os.environ["TWITTER_ACCESS_TOKEN"]
TWITTER_ACCESS_SECRET = os.environ["TWITTER_ACCESS_SECRET"]
TWITTER_BEARER_TOKEN = os.environ["TWITTER_BEARER_TOKEN"]
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))

if not all([TELEGRAM_BOT_TOKEN, TWITTER_API_KEY, TWITTER_API_SECRET,
           TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET, TWITTER_BEARER_TOKEN]):
    raise ValueError("Missing required environment variables")

tweet_queue: List[Dict] = []
scheduler = None
 
def _latest_post_time() -> str:
    try:
        posted_times = [str(t.get('posted_at')) for t in tweet_queue if t.get('posted') and t.get('posted_at')]
        return max(posted_times) if posted_times else ""
    except Exception:
        return ""

def get_health() -> Dict:
    sch = None
    try:
        sch = get_scheduler()
    except Exception:
        sch = None
    return {
        "status": "ok",
        "queue_size": len([t for t in tweet_queue if not t.get('posted')]),
        "total_items": len(tweet_queue),
        "scheduler_running": bool(getattr(sch, 'running', False)),
        "jobs": len(sch.get_jobs()) if sch else 0,
        "last_posted_at": _latest_post_time()
    }

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/healthz", "/live", "/ready"):
            payload = get_health()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

def start_health_server(port: int = HEALTH_PORT):
    def _serve():
        try:
            httpd = HTTPServer(("0.0.0.0", port), HealthHandler)
            logger.info(f"Health server listening on 0.0.0.0:{port}")
            httpd.serve_forever()
        except Exception as e:
            logger.error(f"Health server failed: {e}")
    Thread(target=_serve, daemon=True).start()

def start_health_ping(interval: int = 30, url: str = "http://localhost:8080/healthz"):
    def _ping():
        while True:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = resp.read()
                        import json
                        payload = json.loads(data)
                        logger.info(f"[HEALTH] status={payload.get('status')} queue={payload.get('queue_size')} jobs={payload.get('jobs')}")
                    else:
                        logger.warning(f"[HEALTH] Non-200 response: {resp.status}")
            except Exception as e:
                logger.error(f"[HEALTH] Ping failed: {e}")
            time.sleep(interval)
    Thread(target=_ping, daemon=True).start()

def get_scheduler():
    """Get or create the scheduler"""
    global scheduler
    try:
        if scheduler is not None and scheduler.running:
            return scheduler
    except Exception:
        pass
        
    scheduler = AsyncIOScheduler(
        job_defaults={
            'coalesce': False,
            'max_instances': 1,
            'misfire_grace_time': None
        }
    )
    scheduler.start()
    logger.info("Created and started new scheduler")
    return scheduler

class TweetPoster:
    """Handles Twitter API interactions"""
    
    def __init__(self):
        self.client = tweepy.Client(
            bearer_token=TWITTER_BEARER_TOKEN,
            consumer_key=TWITTER_API_KEY,
            consumer_secret=TWITTER_API_SECRET,
            access_token=TWITTER_ACCESS_TOKEN,
            access_token_secret=TWITTER_ACCESS_SECRET
        )
        logger.info("TweetPoster initialized")
    
    def post_tweet(self, text: str) -> Tuple[bool, Optional[str]]:
        """Post a tweet to Twitter.
        Returns (success, tweet_id) where tweet_id is None on failure.
        """
        try:
            response = self.client.create_tweet(text=text)
            tweet_id: Optional[str] = None
            data = getattr(response, 'data', None)
            if data is not None:
                if isinstance(data, dict):
                    tweet_id = data.get('id') or data.get('tweet_id')
                else:
                    tweet_id = getattr(data, 'id', None)
            if tweet_id:
                logger.info(f"Tweet posted successfully: {tweet_id}")
                return True, str(tweet_id)
            logger.error("Tweet response missing ID; treating as failure")
            return False, None
        except Exception as e:
            logger.error(f"Error posting tweet: {e}")
            return False, None

tweet_poster = TweetPoster()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when /start command is issued"""
    if not update.message:
        return
    welcome_message = """
🤖 *Tweet Scheduler Bot*

Welcome! This bot helps you schedule and post tweets automatically.

*How to use:*
1. Upload your spreadsheet (Excel/CSV)
2. Set your posting schedule
3. Let the bot handle the rest!

Ready to get started? Choose an option below:
    """
    
    keyboard = [
        [
            InlineKeyboardButton("📤 Upload Tweets", callback_data="help_upload"),
            InlineKeyboardButton("📊 Check Status", callback_data="status")
        ],
        [
            InlineKeyboardButton("⏰ Set Schedule", callback_data="help_schedule"),
            InlineKeyboardButton("❓ Help", callback_data="help")
        ],
        [
            InlineKeyboardButton("🗑️ Clear Queue", callback_data="confirm_clear")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_message, parse_mode='Markdown', reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send detailed help information"""
    if not update.message:
        return
        
    help_text = """
📖 *Detailed Help*

*Spreadsheet Format:*
Your file should contain a column named 'tweet' with your tweet text.

Example CSV:
```
tweet
This is my first scheduled tweet!
Another tweet to share
Check out this amazing content
```

*Scheduling Options:*
Use /schedule command followed by interval:
- `/schedule 1h` - Post every 1 hour
- `/schedule 30m` - Post every 30 minutes
- `/schedule 2h30m` - Post every 2 hours 30 minutes
- `/schedule daily 09:00` - Post daily at 9 AM
- `/schedule daily 14:30` - Post daily at 2:30 PM

*Commands:*
/status - See how many tweets are queued
/clear - Remove all scheduled tweets
/pause - Pause posting (coming soon)
/resume - Resume posting (coming soon)

*Notes:*
- Tweets are posted in the order they appear in your file
- Maximum tweet length is 280 characters
- Bot will skip tweets that are too long
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle uploaded spreadsheet files"""
    global tweet_queue
    
    if not update.message or not update.message.document:
        return
    
    document = update.message.document
    
    file_name = getattr(document, 'file_name', '')
    if not (file_name.endswith('.csv') or
            file_name.endswith('.xlsx') or
            file_name.endswith('.xls')):
        await update.message.reply_text(
            "❌ Please send a CSV or Excel file (.csv, .xlsx, .xls)"
        )
        return
    
    await update.message.reply_text("📥 Downloading file...")
    
    try:
        file = await context.bot.get_file(document.file_id)
        file_path = f"temp_{document.file_name}"
        await file.download_to_drive(file_path)
        
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)
        
        if 'tweet' not in df.columns:
            await update.message.reply_text(
                "❌ Error: Spreadsheet must have a 'tweet' column"
            )
            os.remove(file_path)
            return
        
        tweets = df['tweet'].dropna().tolist()
        
        valid_tweets = []
        skipped = 0
        
        for tweet in tweets:
            tweet_text = str(tweet).strip()
            if tweet_text and len(tweet_text) <= 280:
                valid_tweets.append({'text': tweet_text, 'posted': False})
            else:
                skipped += 1
        
        tweet_queue = valid_tweets
        
        os.remove(file_path)
        
        message = f"""
✅ *File processed successfully!*

📊 *Statistics:*
- Total tweets loaded: {len(valid_tweets)}
- Skipped (empty/too long): {skipped}
- Ready to post: {len(valid_tweets)}

Choose what to do next:
        """
        
        keyboard = [
            [
                InlineKeyboardButton("⏰ Set Schedule", callback_data="help_schedule"),
                InlineKeyboardButton("👀 Preview Tweets", callback_data="preview")
            ],
            [
                InlineKeyboardButton("📊 View Status", callback_data="status"),
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        await update.message.reply_text(
            f"❌ Error processing file: {str(e)}"
        )
        if os.path.exists(file_path):
            os.remove(file_path)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current queue status"""
    if not update.message:
        return
    global tweet_queue
    
    if not tweet_queue:
        keyboard = [[InlineKeyboardButton("📤 Upload Tweets", callback_data="help_upload")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "📭 No tweets in queue. Upload a spreadsheet to get started!",
            reply_markup=reply_markup
        )
        return
    
    total = len(tweet_queue)
    posted = sum(1 for t in tweet_queue if t['posted'])
    remaining = total - posted
    
    next_text = ""
    if remaining > 0:
        next_tweet = next((t for t in tweet_queue if not t['posted']), None)
        if next_tweet:
            next_text = f"\nNext: \"{next_tweet['text'][:50]}...\""
    
    status_message = f"""
📊 *Queue Status*

Total tweets: {total}
✅ Posted: {posted}
⏳ Remaining: {remaining}{next_text}
    """
    
    keyboard = [
        [
            InlineKeyboardButton("👀 Preview Queue", callback_data="preview"),
            InlineKeyboardButton("⏰ Schedule", callback_data="help_schedule")
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="status"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(status_message, parse_mode='Markdown', reply_markup=reply_markup)

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set up posting schedule"""
    global tweet_queue
    
    if not update.message or not update.effective_chat:
        return
    
    logger.info("Processing schedule command")
    
    if not tweet_queue:
        await update.message.reply_text(
            "⚠️ Please upload a spreadsheet with tweets first!"
        )
        logger.info("No tweets in queue")
        return
        
    scheduler = get_scheduler()
    if scheduler is None:
        await update.message.reply_text("❌ Failed to initialize scheduler. Please try again.")
        return
        
    scheduler.remove_all_jobs()
    logger.info("Removed existing jobs")
    
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please specify schedule:\n"
            "Examples:\n"
            "/schedule 1h - Every hour\n"
            "/schedule 30m - Every 30 minutes\n"
            "/schedule daily 09:00 - Daily at 9 AM"
        )
        return
        
    if not context.args:
        await update.message.reply_text("⚠️ Please specify a schedule interval")
        return
        
    logger.info(f"Setting up schedule with args: {context.args}")
    schedule_type = context.args[0].lower()
    
    try:
        if schedule_type == 'daily' and len(context.args) > 1:
            time_str = context.args[1]
            hour, minute = map(int, time_str.split(':'))
            
            scheduler = get_scheduler()
            if scheduler is None:
                await update.message.reply_text("❌ Failed to access scheduler. Please try again.")
                return
                
            scheduler.add_job(
                post_next_tweet,
                CronTrigger(hour=hour, minute=minute),
                args=[context.bot, update.effective_chat.id],
                id='tweet_job',
                replace_existing=True
            )
            
            await update.message.reply_text(
                f"✅ Scheduled to post daily at {time_str}"
            )
        else:
            interval_str = schedule_type
            minutes = parse_interval(interval_str)
            
            if minutes <= 0:
                raise ValueError("Invalid interval")
            
            chat_id = update.effective_chat.id if update.effective_chat else None
            if not chat_id:
                await update.message.reply_text("❌ Could not determine chat ID")
                return
                
            scheduler = get_scheduler()
            if scheduler is None:
                await update.message.reply_text("❌ Failed to access scheduler. Please try again.")
                return
            
            actual_minutes = 1 if minutes == 30 else minutes
            scheduler.add_job(
                post_next_tweet,
                'interval',
                minutes=actual_minutes,
                args=[context.bot, chat_id],
                id='tweet_job',
                replace_existing=True
            )
            
            msg = f"✅ Scheduled to post every {interval_str}"
            if minutes == 30:
                msg += " (running every minute for testing)"
            await update.message.reply_text(msg)
        
        logger.info(f"Schedule updated successfully. Scheduler running: {scheduler.running}")
            
    except Exception as e:
        logger.error(f"Error setting schedule: {e}")
        await update.message.reply_text(
            f"❌ Error setting schedule: {str(e)}"
        )

def parse_interval(interval_str: str) -> int:
    """Parse interval string to minutes"""
    total_minutes = 0
    
    if 'h' in interval_str:
        hours = int(interval_str.split('h')[0])
        total_minutes += hours * 60
        interval_str = interval_str.split('h')[1] if 'h' in interval_str else ''
    
    if 'm' in interval_str:
        minutes = int(interval_str.replace('m', ''))
        total_minutes += minutes
        
    if total_minutes <= 0:
        total_minutes = 1
    
    logger.info(f"Parsed interval: {interval_str} -> {total_minutes} minutes")
    return total_minutes

async def post_next_tweet(bot, chat_id):
    """Post the next tweet in queue (async-safe):
    - Posts the next unposted tweet
    - On success: marks it posted, stores tweet_id/url/posted_at and sends the link
    - Stops schedule when all tweets are posted.
    """
    global tweet_queue
    
    logger.info(f"Posting next tweet... Queue size: {len(tweet_queue)}")
    
    next_tweet = next((tweet for tweet in tweet_queue if not tweet.get('posted')), None)
    logger.info(f"Found next tweet to post: {next_tweet is not None}")
    
    if not next_tweet:
        keyboard = [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="✅ All tweets have been posted!",
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
        
        scheduler = get_scheduler()
        if scheduler:
            scheduler.remove_all_jobs()
            logger.info("All tweets posted, scheduler jobs removed")
        return
    
    try:
        success, tweet_id = tweet_poster.post_tweet(next_tweet['text'])
    except Exception as e:
        logger.error(f"Error posting tweet: {e}")
        success, tweet_id = False, None
    
    if success:
        next_tweet['posted'] = True
        if tweet_id:
            tweet_url = f"https://x.com/i/web/status/{tweet_id}"
            next_tweet['tweet_id'] = tweet_id
            next_tweet['tweet_url'] = tweet_url
        next_tweet['posted_at'] = datetime.now().isoformat(timespec='seconds')
        remaining = sum(1 for t in tweet_queue if not t.get('posted'))
        keyboard = [
            [
                InlineKeyboardButton("📊 Check Status", callback_data="status"),
                InlineKeyboardButton("👀 Preview Next", callback_data="preview")
            ],
            [
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ *Tweet Posted!*\n\n"
                    f"📝 {next_tweet['text'][:150]}{'...' if len(next_tweet['text']) > 150 else ''}\n"
                    + (f"🔗 [View Tweet]({tweet_url})\n" if tweet_id else "")
                    + f"\n⏳ {remaining} tweets remaining"
                ),
                parse_mode='Markdown',
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
        
        if remaining == 0:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.remove_all_jobs()
                logger.info("All tweets posted, removed scheduler jobs")
    else:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="❌ Failed to post tweet. Will retry next cycle."
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all scheduled tweets"""
    if not update.message:
        return
        
    scheduler = get_scheduler()
    has_jobs = scheduler and len(scheduler.get_jobs()) > 0 if scheduler else False
        
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, Clear All", callback_data="clear_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="menu")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = "⚠️ Are you sure you want to clear all tweets"
    if has_jobs:
        message += " and stop the schedule"
    message += "?"
    
    await update.message.reply_text(
        message,
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses"""
    if not update.callback_query:
        return
        
    query = update.callback_query
    if query is None:
        return
        
    await query.answer()
    
    global tweet_queue, scheduler
    
    if query.data == "menu":
        welcome_message = """
🤖 *Tweet Scheduler Bot*

Choose an option below:
        """
        keyboard = [
            [
                InlineKeyboardButton("📤 Upload Tweets", callback_data="help_upload"),
                InlineKeyboardButton("📊 Check Status", callback_data="status")
            ],
            [
                InlineKeyboardButton("⏰ Set Schedule", callback_data="help_schedule"),
                InlineKeyboardButton("❓ Help", callback_data="help")
            ],
            [
                InlineKeyboardButton("🗑️ Clear Queue", callback_data="confirm_clear")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(welcome_message, parse_mode='Markdown', reply_markup=reply_markup)
    
    elif query.data == "help":
        help_text = """
📖 *Detailed Help*

*Spreadsheet Format:*
Your file must have a column named 'tweet'

Example CSV:
```
tweet
This is my first tweet!
Another tweet here
More content to share
```

*Commands:*
/start - Main menu
/status - Check queue
/schedule - Set posting times
/clear - Clear all tweets
/help - Show this help
        """
        keyboard = [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(help_text, parse_mode='Markdown', reply_markup=reply_markup)
    
    elif query.data == "help_upload":
        message = """
📤 *Upload Instructions*

1. Prepare a CSV or Excel file (.csv, .xlsx, .xls)
2. Add a column named 'tweet'
3. Add your tweets (max 280 characters each)
4. Send the file to this bot

The bot will automatically process it!
        """
        keyboard = [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, parse_mode='Markdown', reply_markup=reply_markup)
    
    elif query.data == "help_schedule":
        message = """
⏰ *Schedule Options*

Click a preset or use custom command:

*Custom:*
/schedule 30m - Every 30 minutes
/schedule 1h - Every hour
/schedule 2h30m - Every 2.5 hours
/schedule daily 09:00 - Daily at 9 AM
        """
        keyboard = [
            [
                InlineKeyboardButton("⏱️ Every 30 min", callback_data="schedule_30m"),
                InlineKeyboardButton("⏰ Every 1 hour", callback_data="schedule_1h")
            ],
            [
                InlineKeyboardButton("🕐 Every 2 hours", callback_data="schedule_2h"),
                InlineKeyboardButton("🕒 Every 3 hours", callback_data="schedule_3h")
            ],
            [
                InlineKeyboardButton("🌅 Daily 9 AM", callback_data="schedule_daily_09"),
                InlineKeyboardButton("🌆 Daily 6 PM", callback_data="schedule_daily_18")
            ],
            [
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, parse_mode='Markdown', reply_markup=reply_markup)
    
    elif query.data == "status":
        if not tweet_queue:
            keyboard = [[InlineKeyboardButton("📤 Upload Tweets", callback_data="help_upload")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "📭 No tweets in queue. Upload a spreadsheet to get started!",
                reply_markup=reply_markup
            )
            return
        
        total = len(tweet_queue)
        posted = sum(1 for t in tweet_queue if t['posted'])
        remaining = total - posted
        
        next_text = ""
        if remaining > 0:
            next_tweet = next((t for t in tweet_queue if not t['posted']), None)
            if next_tweet:
                next_text = f"\nNext: \"{next_tweet['text'][:50]}...\""
        
        status_message = f"""
📊 *Queue Status*

Total tweets: {total}
✅ Posted: {posted}
⏳ Remaining: {remaining}{next_text}
        """
        
        keyboard = [
            [
                InlineKeyboardButton("👀 Preview Queue", callback_data="preview"),
                InlineKeyboardButton("⏰ Schedule", callback_data="help_schedule")
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="status"),
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(status_message, parse_mode='Markdown', reply_markup=reply_markup)
    
    elif query.data == "preview":
        if not tweet_queue:
            await query.edit_message_text("📭 No tweets to preview!")
            return
        
        unposted = [t for t in tweet_queue if not t['posted']][:5]
        preview_text = "👀 *Next Tweets in Queue:*\n\n"
        
        for i, tweet in enumerate(unposted, 1):
            preview_text += f"{i}. {tweet['text'][:100]}{'...' if len(tweet['text']) > 100 else ''}\n\n"
        
        if len(unposted) < len([t for t in tweet_queue if not t['posted']]):
            preview_text += f"...and {len([t for t in tweet_queue if not t['posted']]) - 5} more"
        
        keyboard = [
            [
                InlineKeyboardButton("📊 Full Status", callback_data="status"),
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(preview_text, parse_mode='Markdown', reply_markup=reply_markup)
    
    elif query.data == "confirm_clear":
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Clear All", callback_data="clear_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "⚠️ Are you sure you want to clear all tweets and stop the schedule?",
            reply_markup=reply_markup
        )
    
    elif query.data == "clear_confirm":
        tweet_queue = []
        
        scheduler = get_scheduler()
        if scheduler:
            scheduler.remove_all_jobs()
            logger.info("Cleared scheduler jobs")
            
        keyboard = [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🗑️ All tweets cleared and schedule stopped!",
            reply_markup=reply_markup
        )
    
    elif query and query.data and query.data.startswith("schedule_"):
        if not tweet_queue or not query.message or not query.message.chat:
            keyboard = [[InlineKeyboardButton("📤 Upload Tweets", callback_data="help_upload")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "⚠️ Please upload tweets first!",
                reply_markup=reply_markup
            )
            return
        
        schedule_type = query.data.replace("schedule_", "")
        
        scheduler = get_scheduler()
        if scheduler is None:
            await query.edit_message_text("❌ Failed to initialize scheduler. Please try again.")
            return
            
        scheduler.remove_all_jobs()
        
        try:
            if not query or not query.message or not query.message.chat:
                return
            
            scheduler = get_scheduler()
            if scheduler is None:
                await query.edit_message_text("❌ Failed to access scheduler. Please try again.")
                return
                
            if schedule_type == "30m":
                scheduler.add_job(post_next_tweet, 'interval', minutes=1,  
                                args=[context.bot, query.message.chat.id],
                                id='tweet_job', replace_existing=True)
                msg = "✅ Scheduled to post every minute (test mode)!"
            elif schedule_type == "1h" and query.message and query.message.chat:
                scheduler.add_job(post_next_tweet, 'interval', hours=1, 
                                args=[context.bot, query.message.chat.id],
                                id='tweet_job', replace_existing=True)
                msg = "✅ Scheduled to post every 1 hour!"
            elif schedule_type == "2h" and query.message and query.message.chat:
                scheduler.add_job(post_next_tweet, 'interval', hours=2, 
                                args=[context.bot, query.message.chat.id],
                                id='tweet_job', replace_existing=True)
                msg = "✅ Scheduled to post every 2 hours!"
            elif schedule_type == "3h" and query.message and query.message.chat:
                scheduler.add_job(post_next_tweet, 'interval', hours=3, 
                                args=[context.bot, query.message.chat.id],
                                id='tweet_job', replace_existing=True)
                msg = "✅ Scheduled to post every 3 hours!"
            elif schedule_type == "daily_09" and query.message and query.message.chat:
                scheduler.add_job(post_next_tweet, CronTrigger(hour=9, minute=0),
                                args=[context.bot, query.message.chat.id],
                                id='tweet_job', replace_existing=True)
                msg = "✅ Scheduled to post daily at 9:00 AM!"
            elif schedule_type == "daily_18" and query.message and query.message.chat:
                scheduler.add_job(post_next_tweet, CronTrigger(hour=18, minute=0),
                                args=[context.bot, query.message.chat.id],
                                id='tweet_job', replace_existing=True)
                msg = "✅ Scheduled to post daily at 6:00 PM!"
            
            scheduler = get_scheduler()
            if scheduler is None:
                await query.edit_message_text("❌ Failed to start scheduler. Please try again.")
                return
            
            logger.info(f"Job scheduled successfully. Scheduler running: {scheduler.running}")
            
            keyboard = [
                [
                    InlineKeyboardButton("📊 Check Status", callback_data="status"),
                    InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(msg, reply_markup=reply_markup)
            
        except Exception as e:
            logger.error(f"Error setting schedule: {e}")
            await query.edit_message_text(f"❌ Error setting schedule: {str(e)}")

def main():
    """Start the bot"""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    logger.info("Bot starting - scheduler will be initialized when needed")

    start_health_server(HEALTH_PORT)
    start_health_ping(30, f"https://yapbot-933z.onrender.com/ready")
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    logger.info("Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
