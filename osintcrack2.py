import telebot
from telebot import types
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import re
import time
import sqlite3
import threading
import random
import string
import urllib.parse
import uuid
from datetime import datetime, timedelta

# ============================================================
#                    CONFIGURATION
# ============================================================

BOT_TOKEN = '8903667323:AAEYX45CMdQkE8hiUBhN_E-ndj_WyuEQ-_w'
ADMIN_USER_ID = 8637412597
ADMIN_USERNAME = "admin"
API_KEY = "mysecretkey123"
BOT_NAME = "CRACK"

DEVELOPER_LINK = 'https://t.me/+FWjX6RK2YnhiOTA9'

# Force Join Channels (Private channel supported)
# 'id' = Channel ID (numeric, get from @userinfobot or @getidsbot)
# 'link' = Invite link for join button
# 'name' = Button pe dikhne wala naam
FORCE_JOIN_CHANNELS = [
    {
        "id": -1003985746268,                        # ← Apn404647484a channel ID dalo
        "link": "https://t.me/+NXtwrbNr9CtlMTY1",  # ← Apna invite link dalo
        "name": "CRACK Channel"                      # ← Display name
    }
]

# Payment Config
UPI_ID = "your_upi@upi"           # Change to your UPI ID
USDT_WALLET = "your_usdt_address"  # Change to your USDT wallet address
STORE_LINK = "https://your-store-link.com"  # Change to your store link

# API URLs
BASE_API_URL = "http://127.0.0.1:8000/search"
NUMBER_API_URL = f"{BASE_API_URL}/number?number={{}}&key={API_KEY}"
AADHAAR_API_URL = f"{BASE_API_URL}/aadhar?aadhar={{}}&key={API_KEY}"
VEHICLE_INFO_API = "https://anon-vehicle-info.vercel.app/veh2num?key=AnonyLomda@2009&rc={}"
FAMILY_API_URL = "https://anon-family-info.vercel.app/advanced/aadhar?key=AnonyLomda@2009&q={}"

# Default Bot Settings (stored in DB, admin can toggle)
DEFAULT_SETTINGS = {
    'force_join_enabled': '1',
    'upi_enabled': '1',
    'usdt_enabled': '1',
    'auto_verify': '0',
    'buy_from_store_enabled': '0',
    'redeem_code_enabled': '0',
    'credit_price': '5',
    'monthly_price': '30',
    'store_link': STORE_LINK,
    'upi_id': UPI_ID,
    'usdt_wallet': USDT_WALLET,
}

# ============================================================
#                    DATABASE (HIGH CONCURRENCY WAL)
# ============================================================

def get_db():
    """Get thread-safe database connection with WAL mode and 60s busy timeout"""
    conn = sqlite3.connect('data.db', timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 60000;")
    conn.execute("PRAGMA cache_size = -64000;")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    
    # Original tables
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, 
                  last_name TEXT, date_joined TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                 (user_id INTEGER PRIMARY KEY, 
                  username TEXT, first_name TEXT, last_name TEXT,
                  banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  banned_by INTEGER,
                  reason TEXT DEFAULT 'No reason provided')''')
    
    # Credits & Membership
    c.execute('''CREATE TABLE IF NOT EXISTS user_credits
                 (user_id INTEGER PRIMARY KEY,
                  credits INTEGER DEFAULT 0,
                  free_credits_given INTEGER DEFAULT 0,
                  membership_type TEXT DEFAULT 'none',
                  membership_expiry TIMESTAMP,
                  total_searches INTEGER DEFAULT 0,
                  daily_free_credits INTEGER DEFAULT 0,
                  last_daily_credit_date DATE)''')
    
    # Payment Requests
    c.execute('''CREATE TABLE IF NOT EXISTS payment_requests
                 (request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  amount REAL,
                  payment_type TEXT,
                  plan_type TEXT,
                  credits_requested INTEGER DEFAULT 0,
                  transaction_id TEXT,
                  status TEXT DEFAULT 'pending',
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  verified_at TIMESTAMP,
                  verified_by INTEGER)''')
    
    # Redeem Codes
    c.execute('''CREATE TABLE IF NOT EXISTS redeem_codes
                 (code TEXT PRIMARY KEY,
                  plan_type TEXT,
                  credits_amount INTEGER DEFAULT 0,
                  days_amount INTEGER DEFAULT 0,
                  used_by INTEGER,
                  used_at TIMESTAMP,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  is_used INTEGER DEFAULT 0)''')
    
    # Bot Settings
    c.execute('''CREATE TABLE IF NOT EXISTS bot_settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Insert default settings if not exists
    for key, value in DEFAULT_SETTINGS.items():
        c.execute('INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)', (key, value))
        
    try:
        c.execute('ALTER TABLE user_credits ADD COLUMN daily_free_credits INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    
    try:
        c.execute('ALTER TABLE user_credits ADD COLUMN last_daily_credit_date DATE')
    except sqlite3.OperationalError:
        pass
    
    try:
        c.execute('ALTER TABLE redeem_codes ADD COLUMN batch_id TEXT')
    except sqlite3.OperationalError:
        pass
        
    try:
        c.execute('ALTER TABLE user_credits ADD COLUMN master_search_access INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    
    # High performance indexes for 10k users
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_id ON users(user_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_credits_id ON user_credits(user_id);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_redeem_code ON redeem_codes(code);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_payment_status ON payment_requests(status);")
    
    conn.commit()
    conn.close()

init_db()

# Multi-threaded bot worker pool (25 threads for high concurrency)
bot = telebot.TeleBot(BOT_TOKEN, num_threads=25)

# Shared HTTP connection pool for API queries
http_session = requests.Session()
retry_strategy = Retry(
    total=2,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=retry_strategy)
http_session.mount("http://", adapter)
http_session.mount("https://", adapter)

# Sliding window rate limiter to protect from spam / DDoS
class RateLimiter:
    def __init__(self):
        self._user_last_action = {}
        self._lock = threading.Lock()
        
    def is_rate_limited(self, user_id, cooldown_seconds=1.0):
        if user_id == ADMIN_USER_ID:
            return False
        now = time.time()
        with self._lock:
            last_time = self._user_last_action.get(user_id, 0)
            if now - last_time < cooldown_seconds:
                return True
            self._user_last_action[user_id] = now
            if len(self._user_last_action) > 20000:
                cutoff = now - 60
                self._user_last_action = {k: v for k, v in self._user_last_action.items() if v > cutoff}
            return False

rate_limiter = RateLimiter()

broadcast_storage = {}
admin_announcement_data = {}

# ============================================================
#                 SETTINGS FUNCTIONS
# ============================================================

def get_setting(key):
    """Get a bot setting from database"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT value FROM bot_settings WHERE key = ?', (key,))
    result = c.fetchone()
    conn.close()
    if result:
        return result['value']
    return DEFAULT_SETTINGS.get(key, '')

def set_setting(key, value):
    """Set a bot setting in database"""
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', (key, str(value)))
    conn.commit()
    conn.close()

def is_setting_on(key):
    """Check if a toggle setting is ON"""
    return get_setting(key) == '1'

# ============================================================
#                 USER FUNCTIONS
# ============================================================

def generate_broadcast_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

def save_user(user_id, username, first_name, last_name):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO users (user_id, username, first_name, last_name) 
                 VALUES (?, ?, ?, ?)''', (user_id, username, first_name, last_name))
    conn.commit()
    conn.close()

def delete_user(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT user_id FROM users')
    users = [row['user_id'] for row in c.fetchall()]
    conn.close()
    return users

def get_total_users_count():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) as cnt FROM users')
    result = c.fetchone()
    conn.close()
    return result['cnt'] if result else 0

# ============================================================
#                 BAN FUNCTIONS
# ============================================================

def is_user_banned(user_id):
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('SELECT user_id FROM banned_users WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        conn.close()
        return result is not None
    except sqlite3.OperationalError as e:
        conn.close()
        if "no such table" in str(e):
            init_db()
            return False
        raise e

def get_banned_user_info(user_id):
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('SELECT * FROM banned_users WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        conn.close()
        return result
    except sqlite3.OperationalError:
        conn.close()
        return None

def ban_user(user_id, username, first_name, last_name, banned_by, reason=None):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO banned_users 
                 (user_id, username, first_name, last_name, banned_by, reason) 
                 VALUES (?, ?, ?, ?, ?, ?)''', 
                 (user_id, username, first_name, last_name, banned_by, reason))
    conn.commit()
    conn.close()
    return True

def unban_user(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM banned_users WHERE user_id = ?', (user_id,))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0

def get_all_banned_users():
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('SELECT user_id, username, first_name, last_name, banned_at FROM banned_users')
        users = c.fetchall()
        conn.close()
        return users
    except sqlite3.OperationalError:
        conn.close()
        return []

# ============================================================
#                 CREDIT & MEMBERSHIP FUNCTIONS
# ============================================================

def ensure_user_credits(user_id):
    """Make sure user has a row in user_credits table"""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO user_credits (user_id, credits, free_credits_given) VALUES (?, 0, 0)', (user_id,))
        
        # Check daily free credit
        c.execute('SELECT last_daily_credit_date FROM user_credits WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        today = datetime.now().date().isoformat()
        if row and row['last_daily_credit_date'] != today:
            c.execute('UPDATE user_credits SET daily_free_credits = 1, last_daily_credit_date = ? WHERE user_id = ?', (today, user_id))
        
        conn.commit()
    finally:
        conn.close()

def get_user_credits(user_id):
    """Get user's credit info"""
    ensure_user_credits(user_id)
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM user_credits WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result

def give_free_credits(user_id):
    """Give 2 free credits to first-time user. Returns True if given, False if already given."""
    ensure_user_credits(user_id)
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT free_credits_given FROM user_credits WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    
    if result and result['free_credits_given'] == 0:
        c.execute('UPDATE user_credits SET credits = credits + 2, free_credits_given = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def add_credits(user_id, amount):
    """Add credits to user"""
    ensure_user_credits(user_id)
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE user_credits SET credits = credits + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def deduct_credit(user_id):
    """Deduct 1 credit. Returns True if successful, False if no credits."""
    user_info = get_user_credits(user_id)
    if not user_info:
        return False
    
    # Check if user has active membership (unlimited)
    if has_active_membership(user_id):
        # Don't deduct, just increment search count
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE user_credits SET total_searches = total_searches + 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True
    
    # Prioritize daily free credits
    if 'daily_free_credits' in user_info.keys() and user_info['daily_free_credits'] > 0:
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE user_credits SET daily_free_credits = daily_free_credits - 1, total_searches = total_searches + 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True
        
    # Then fallback to regular credits
    if user_info['credits'] > 0:
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE user_credits SET credits = credits - 1, total_searches = total_searches + 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True
    
    return False

def has_active_membership(user_id):
    """Check if user has active (non-expired) membership"""
    user_info = get_user_credits(user_id)
    if not user_info:
        return False
    if user_info['membership_type'] == 'monthly' and user_info['membership_expiry']:
        try:
            expiry = datetime.strptime(user_info['membership_expiry'], '%Y-%m-%d %H:%M:%S')
            return expiry > datetime.now()
        except (ValueError, TypeError):
            try:
                expiry = datetime.fromisoformat(user_info['membership_expiry'])
                return expiry > datetime.now()
            except:
                return False
    return False

def give_membership(user_id, days=30):
    """Give monthly membership to user"""
    ensure_user_credits(user_id)
    expiry = datetime.now() + timedelta(days=days)
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE user_credits SET membership_type = ?, membership_expiry = ? WHERE user_id = ?',
              ('monthly', expiry.strftime('%Y-%m-%d %H:%M:%S'), user_id))
    conn.commit()
    conn.close()

def can_search(user_id):
    """Check if user can perform a search (has credits or membership)"""
    if has_active_membership(user_id):
        return True
    user_info = get_user_credits(user_id)
    if user_info and user_info['credits'] > 0:
        return True
    return False

def get_vip_users_count():
    """Count users with active membership"""
    conn = get_db()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute("SELECT COUNT(*) as cnt FROM user_credits WHERE membership_type = 'monthly' AND membership_expiry > ?", (now,))
    result = c.fetchone()
    conn.close()
    return result['cnt'] if result else 0

def is_master_search_allowed(user_id):
    """Check if user has access to Master Search (Admin, VIP member, or specifically granted by admin)"""
    if user_id == ADMIN_USER_ID:
        return True
    if has_active_membership(user_id):
        return True
    user_info = get_user_credits(user_id)
    if user_info:
        try:
            if user_info['master_search_access'] == 1:
                return True
        except (IndexError, KeyError):
            pass
    return False

def set_master_search_access(user_id, status: bool):
    """Grant or revoke Master Search access for a user"""
    ensure_user_credits(user_id)
    val = 1 if status else 0
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE user_credits SET master_search_access = ? WHERE user_id = ?', (val, user_id))
    conn.commit()
    conn.close()

# ============================================================
#                 PAYMENT FUNCTIONS
# ============================================================

def create_payment_request(user_id, amount, payment_type, plan_type, credits_requested=0, transaction_id=""):
    """Create a new payment request"""
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO payment_requests 
                 (user_id, amount, payment_type, plan_type, credits_requested, transaction_id)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (user_id, amount, payment_type, plan_type, credits_requested, transaction_id))
    conn.commit()
    request_id = c.lastrowid
    conn.close()
    return request_id

def get_pending_payments():
    """Get all pending payment requests"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM payment_requests WHERE status = 'pending' ORDER BY created_at DESC")
    results = c.fetchall()
    conn.close()
    return results

def get_pending_payments_count():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM payment_requests WHERE status = 'pending'")
    result = c.fetchone()
    conn.close()
    return result['cnt'] if result else 0

def approve_payment(request_id, admin_id):
    """Approve a payment request and give user their plan"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM payment_requests WHERE request_id = ? AND status = ?', (request_id, 'pending'))
    req = c.fetchone()
    
    if not req:
        conn.close()
        return None
    
    c.execute('''UPDATE payment_requests SET status = 'approved', verified_at = ?, verified_by = ? 
                 WHERE request_id = ?''',
              (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), admin_id, request_id))
    conn.commit()
    conn.close()
    
    user_id = req['user_id']
    plan_type = req['plan_type']
    
    if plan_type == 'monthly':
        give_membership(user_id, 30)
    elif plan_type == 'credits':
        add_credits(user_id, req['credits_requested'])
    
    return req

def reject_payment(request_id, admin_id):
    """Reject a payment request"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM payment_requests WHERE request_id = ? AND status = ?', (request_id, 'pending'))
    req = c.fetchone()
    
    if not req:
        conn.close()
        return None
    
    c.execute('''UPDATE payment_requests SET status = 'rejected', verified_at = ?, verified_by = ? 
                 WHERE request_id = ?''',
              (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), admin_id, request_id))
    conn.commit()
    conn.close()
    return req

# ============================================================
#                 REDEEM CODE FUNCTIONS
# ============================================================

def generate_redeem_code(plan_type, amount, batch_id=None, is_free=False):
    """Generate a unique redeem code"""
    prefix = 'FREE-' if is_free else 'CRACK-'
    code = prefix + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    conn = get_db()
    c = conn.cursor()
    
    if plan_type == 'monthly':
        c.execute('INSERT INTO redeem_codes (code, plan_type, days_amount, batch_id) VALUES (?, ?, ?, ?)',
                  (code, 'monthly', amount, batch_id))
    elif plan_type == 'credits':
        c.execute('INSERT INTO redeem_codes (code, plan_type, credits_amount, batch_id) VALUES (?, ?, ?, ?)',
                  (code, 'credits', amount, batch_id))
    
    conn.commit()
    conn.close()
    return code

def redeem_code(code, user_id):
    """Redeem a code. Returns (success, message, plan_info)"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM redeem_codes WHERE code = ?', (code,))
    result = c.fetchone()
    
    if not result:
        conn.close()
        return False, "❌ Invalid code!", None
    
    if result['is_used'] == 1:
        conn.close()
        return False, "❌ This code has already been used!", None
    
    # Check if user already redeemed a code from the same batch
    try:
        batch_id = result['batch_id']
    except (IndexError, KeyError):
        batch_id = None
    if batch_id:
        c.execute('SELECT code FROM redeem_codes WHERE batch_id = ? AND used_by = ?', (batch_id, user_id))
        if c.fetchone():
            conn.close()
            return False, "❌ You have already claimed a code from this giveaway!", None
    
    # Mark code as used
    c.execute('UPDATE redeem_codes SET is_used = 1, used_by = ?, used_at = ? WHERE code = ?',
              (user_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), code))
    conn.commit()
    conn.close()
    
    # Apply the code
    if result['plan_type'] == 'monthly':
        give_membership(user_id, result['days_amount'])
        return True, f"✅ {result['days_amount']} days membership activated!", {'type': 'monthly', 'days': result['days_amount']}
    elif result['plan_type'] == 'credits':
        add_credits(user_id, result['credits_amount'])
        return True, f"✅ {result['credits_amount']} credits added!", {'type': 'credits', 'amount': result['credits_amount']}
    
    return False, "❌ Invalid code type!", None

# ============================================================
#                 FORCE JOIN FUNCTIONS
# ============================================================

def check_force_join(user_id):
    """Check if user has joined all required channels (supports private channels via numeric ID)"""
    if not is_setting_on('force_join_enabled'):
        return True
    
    for channel in FORCE_JOIN_CHANNELS:
        try:
            channel_id = channel.get('id') if isinstance(channel, dict) else channel
            member = bot.get_chat_member(channel_id, user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception as e:
            print(f"Force join check error for {channel}: {e}")
            # If we can't check, allow access (bot might not be admin in channel)
            continue
    return True

def send_force_join_message(chat_id):
    """Send force join message with buttons (supports private channels with invite links)"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    for channel in FORCE_JOIN_CHANNELS:
        if isinstance(channel, dict):
            name = channel.get('name', 'Channel')
            link = channel.get('link', '')
            join_btn = types.InlineKeyboardButton(f"📢 Join {name}", url=link)
        else:
            # Fallback for simple @username format
            channel_name = channel.replace('@', '')
            join_btn = types.InlineKeyboardButton(f"📢 Join {channel}", url=f"https://t.me/{channel_name}")
        markup.add(join_btn)
    
    check_btn = types.InlineKeyboardButton("✅ Check Joined", callback_data="check_force_join")
    markup.add(check_btn)
    
    msg = f"🔒 *{BOT_NAME} Bot — Access Locked*\n\n"
    msg += "⚠️ Sabse pehle hamara channel join karo!\n"
    msg += "Jab tak aap join nahi karoge, sab features locked rahenge.\n\n"
    msg += "👇 Neeche button se join karo, phir ✅ Check dabao:"
    
    bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)

# ============================================================
#                 HELPER / UTILITY FUNCTIONS
# ============================================================


def sanitize_mobile_number(text):
    """Clean mobile number: strip spaces, hyphens, +91, leading 0, etc."""
    if not text:
        return None
    clean = re.sub(r'[\s\-\(\)\+]', '', str(text).strip())
    if len(clean) == 12 and clean.startswith("91"):
        clean = clean[2:]
    elif len(clean) == 11 and clean.startswith("0"):
        clean = clean[1:]
    if len(clean) == 10 and clean.isdigit():
        return clean
    return None

def sanitize_aadhaar_number(text):
    """Clean Aadhaar number: strip spaces and hyphens, ensure 12 digits"""
    if not text:
        return None
    clean = re.sub(r'[\s\-\(\)]', '', str(text).strip())
    if len(clean) == 12 and clean.isdigit():
        return clean
    return None

def is_command_or_menu(message):
    if not message.text:
        return False
    if message.text.startswith('/'):
        bot.process_new_messages([message])
        return True
    
    menu_buttons = [
        "⚡ Master Search", "🔢 Num Info", "🆔 Adhar Info", "👨‍👩‍👧‍👦 Family Info", "🚗 Vehicle Info", 
        "💎 VIP Plans", "👤 My Account", "💬 Chat with Developer", "⚙️ Admin Panel"
    ]
    if message.text in menu_buttons:
        bot.process_new_messages([message])
        return True
    return False

def format_address(address_string):
    if not address_string or address_string == "N/A":
        return "N/A"
    return address_string.replace('!!!!', ', ').replace('!!!', ', ').replace('!!', ', ').replace('!', ', ')

def auto_delete_message(chat_id, message_id, timeout=30):
    def delete():
        time.sleep(timeout)
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    threading.Thread(target=delete, daemon=True).start()

def wrapped_send_message(chat_id, text, *args, **kwargs):
    is_announcement = kwargs.pop('is_announcement', False)
    skip_delete = kwargs.pop('skip_delete', False)
    delete_after = kwargs.pop('delete_after', 30)
    is_admin_log = (chat_id == ADMIN_USER_ID)
    msg = bot.send_message(chat_id, text, *args, **kwargs)
    if not is_announcement and not is_admin_log and not skip_delete:
        auto_delete_message(chat_id, msg.message_id, timeout=delete_after)
    return msg

def wrapped_reply_to(message, text, *args, **kwargs):
    is_announcement = kwargs.pop('is_announcement', False)
    skip_delete = kwargs.pop('skip_delete', False)
    delete_after = kwargs.pop('delete_after', 30)
    is_admin_log = (message.chat.id == ADMIN_USER_ID)
    msg = bot.reply_to(message, text, *args, **kwargs)
    if not is_announcement and not is_admin_log and not skip_delete:
        auto_delete_message(message.chat.id, msg.message_id, timeout=delete_after)
    return msg

# ============================================================
#                 API FUNCTIONS
# ============================================================

def fetch_number_info_from_api(mobile_number):
    try:
        api_url = NUMBER_API_URL.format(mobile_number)
        response = http_session.get(api_url, timeout=(5, 15))
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                results = data.get("result", [])
                if results and isinstance(results, list):
                    transformed_results = []
                    seen_combinations = set()
                    for item in results:
                        if isinstance(item, dict):
                            unique_key = f"{item.get('num', '')}_{item.get('aadhar', '')}_{item.get('alt', '')}"
                            if unique_key not in seen_combinations:
                                seen_combinations.add(unique_key)
                                transformed_results.append({
                                    "record_id": str(item.get("aadhar", "N/A")),
                                    "mobile": item.get("num", "N/A"),
                                    "name": item.get("name", "N/A"),
                                    "fname": item.get("fname", "N/A"),
                                    "address": item.get("address", "N/A"),
                                    "aadhaar_id": item.get("aadhar", "N/A"),
                                    "alt": item.get("alt", "N/A"),
                                    "circle": item.get("circle", "N/A"),
                                    "email": item.get("email", "N/A")
                                })
                    if transformed_results:
                        return {
                            "status": "success",
                            "results": transformed_results,
                            "count": len(transformed_results),
                            "input": mobile_number,
                            "search_time": datetime.now().isoformat()
                        }
                return {"status": "no_data", "message": "No data found for this number"}
            else:
                return {"status": "error", "message": "No data found for this number"}
        else:
            return {"status": "error", "message": "No data found for this number"}
    except requests.exceptions.Timeout:
        return {"status": "error", "message": "No data found for this number"}
    except requests.exceptions.ConnectionError:
        return {"status": "error", "message": "No data found for this number"}
    except Exception as e:
        print(f"API Error for {mobile_number}: {str(e)}")
        return {"status": "error", "message": "No data found for this number"}

def fetch_aadhaar_info_from_api(aadhaar_number):
    try:
        api_url = AADHAAR_API_URL.format(aadhaar_number)
        response = http_session.get(api_url, timeout=(5, 15))
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                results = data.get("result", [])
                if results and isinstance(results, list):
                    main_info = results[0] if results else {}
                    mobiles = []
                    seen_mobiles = set()
                    for item in results:
                        if isinstance(item, dict):
                            mobile = item.get("num", "N/A")
                            alt = item.get("alt", "N/A")
                            circle = item.get("circle", "N/A")
                            if mobile != "N/A" and mobile not in seen_mobiles:
                                seen_mobiles.add(mobile)
                                mobiles.append({"mobile": mobile, "alt": alt, "circle": circle})
                    return {
                        "status": "success",
                        "aadhaar": aadhaar_number,
                        "name": main_info.get("name", "N/A"),
                        "fname": main_info.get("fname", "N/A"),
                        "address": format_address(main_info.get("address", "N/A")),
                        "mobiles": mobiles
                    }
                return {"status": "no_data", "message": "No data found for this Aadhaar number"}
            else:
                return {"status": "error", "message": "No data found for this Aadhaar number"}
        else:
            return {"status": "error", "message": "No data found for this Aadhaar number"}
    except Exception as e:
        print(f"Aadhaar API Error: {str(e)}")
        return {"status": "error", "message": "No data found for this Aadhaar number"}

def fetch_vehicle_info_from_api(vehicle_number):
    try:
        api_url = VEHICLE_INFO_API.format(vehicle_number)
        response = http_session.get(api_url, timeout=(5, 15))
        data = response.json()
        resp = data.get("response", {})
        params = resp.get("parameters", {})
        results = resp.get("data", [])
        if params.get("success") == True and results:
            vehicle_info = results[0] if results else {}
            return {
                "status": "success",
                "data": vehicle_info,
                "input": vehicle_number
            }
        else:
            return {"status": "no_data", "message": "No vehicle information found for this registration number."}
    except Exception as e:
        print(f"Vehicle API Error for {vehicle_number}: {str(e)}")
        return {"status": "error", "message": "No vehicle information found for this registration number."}

def fetch_family_info_from_api(aadhaar_number):
    try:
        api_url = FAMILY_API_URL.format(aadhaar_number)
        response = http_session.get(api_url, timeout=(5, 15))
        data = response.json()
        resp = data.get("response", {})
        params = resp.get("parameters", {})
        results = resp.get("data", [])
        if params.get("success") == True and results:
            family_data = results[0] if results else {}
            members = family_data.get("members", [])
            return {
                "status": "success",
                "rcId": family_data.get("rcId", "N/A"),
                "fpsId": family_data.get("fpsId", "N/A"),
                "state": family_data.get("state", "N/A"),
                "district": family_data.get("district", "N/A"),
                "members": members
            }
        else:
            error_msg = "No family information found for this Aadhaar number."
            if results and isinstance(results, list) and len(results) > 0 and results[0].get("message"):
                error_msg = results[0].get("message")
            return {"status": "no_data", "message": error_msg}
    except Exception as e:
        print(f"Family API Error for {aadhaar_number}: {str(e)}")
        return {"status": "error", "message": "No family information found for this Aadhaar number."}

# ============================================================
#                 UI FUNCTIONS
# ============================================================

def show_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    master_btn = types.KeyboardButton("⚡ Master Search")
    num_btn = types.KeyboardButton("🔢 Num Info")
    adhar_btn = types.KeyboardButton("🆔 Adhar Info")
    family_btn = types.KeyboardButton("👨‍👩‍👧‍👦 Family Info")
    vehicle_info_btn = types.KeyboardButton("🚗 Vehicle Info")
    vip_btn = types.KeyboardButton("💎 VIP Plans")
    account_btn = types.KeyboardButton("👤 My Account")
    dev_btn = types.KeyboardButton("💬 Chat with Developer")
    
    markup.add(master_btn)
    markup.add(num_btn, adhar_btn)
    markup.add(family_btn, vehicle_info_btn)
    markup.add(vip_btn, account_btn)
    markup.add(dev_btn)
    
    # Add admin panel button only for the admin
    if chat_id == ADMIN_USER_ID:
        admin_btn = types.KeyboardButton("⚙️ Admin Panel")
        markup.add(admin_btn)
    
    bot.send_message(chat_id, "✅ Choose an option below:", parse_mode="Markdown", reply_markup=markup)

def check_ban_status(user_id, chat_id):
    if is_user_banned(user_id):
        ban_info = get_banned_user_info(user_id)
        ban_msg = f"🚫 ACCESS DENIED\n\n"
        ban_msg += f"You have been banned from using this bot.\n\n"
        ban_msg += f"🔒 Reason: Account restricted by owner\n"
        if ban_info and ban_info['reason']:
            ban_msg += f"📝 Admin Note: {ban_info['reason']}\n"
        ban_msg += f"\nIf you think this is a mistake, contact @{ADMIN_USERNAME}"
        bot.send_message(chat_id, ban_msg)
        return True
    return False

def show_no_credits_message(chat_id):
    """Show message when user has no credits"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    vip_btn = types.InlineKeyboardButton("💎 Buy VIP / Credits", callback_data="show_vip_plans")
    markup.add(vip_btn)
    
    msg = "❌ *Credits Khatam Ho Gaye!*\n\n"
    msg += "Aapke paas search credits nahi hain.\n"
    msg += "Search karne ke liye VIP Plan lo ya credits kharido!\n\n"
    msg += "👇 Neeche button dabao:"
    
    wrapped_send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)

# ============================================================
#            VIP PLANS & PAYMENT HANDLERS
# ============================================================

def show_vip_plans(chat_id):
    """Show VIP plans menu"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    monthly_price = get_setting('monthly_price')
    credit_price = get_setting('credit_price')
    
    monthly_btn = types.InlineKeyboardButton(f"💳 ₹{monthly_price}/Month — Unlimited Searches", callback_data="plan_monthly")
    credits_btn = types.InlineKeyboardButton(f"🔢 ₹{credit_price} — 10 Search Credits", callback_data="plan_credits")
    
    markup.add(monthly_btn)
    markup.add(credits_btn)
    
    if is_setting_on('buy_from_store_enabled'):
        store_btn = types.InlineKeyboardButton("🛒 Buy VIP from Store", callback_data="plan_store")
        markup.add(store_btn)
    
    if is_setting_on('redeem_code_enabled'):
        redeem_btn = types.InlineKeyboardButton("🎟️ Redeem Code", callback_data="plan_redeem")
        markup.add(redeem_btn)
    
    msg = f"💎 *{BOT_NAME} — VIP Plans*\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"💳 *Monthly Plan* — ₹{monthly_price}/month\n"
    msg += "   └ Unlimited searches for 30 days\n\n"
    msg += f"🔢 *Credit Pack* — ₹{credit_price}/10 credits\n"
    msg += "   └ Pay per search, buy any amount\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += "👇 Apna plan choose karo:"
    
    bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)

# Temporary storage for payment flow
payment_flow_data = {}

def show_payment_methods(chat_id, plan_type, amount, credits_requested=0):
    """Show UPI/USDT payment options"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    # Store payment flow data
    payment_flow_data[chat_id] = {
        'plan_type': plan_type,
        'amount': amount,
        'credits_requested': credits_requested
    }
    
    buttons = []
    if is_setting_on('upi_enabled'):
        upi_btn = types.InlineKeyboardButton("💳 UPI", callback_data=f"pay_upi")
        buttons.append(upi_btn)
    
    if is_setting_on('usdt_enabled'):
        usdt_btn = types.InlineKeyboardButton("💰 USDT", callback_data=f"pay_usdt")
        buttons.append(usdt_btn)
    
    cancel_btn = types.InlineKeyboardButton("❌ Cancel", callback_data="pay_cancel")
    
    if buttons:
        markup.add(*buttons)
    markup.add(cancel_btn)
    
    plan_name = "Monthly Unlimited" if plan_type == 'monthly' else f"{credits_requested} Credits"
    
    msg = f"💳 *Payment Method Select Karo*\n\n"
    msg += f"📦 Plan: *{plan_name}*\n"
    msg += f"💰 Amount: *₹{amount}*\n\n"
    msg += "👇 Payment method choose karo:"
    
    msg_sent = bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)
    auto_delete_message(chat_id, msg_sent.message_id, timeout=900)

def show_upi_payment(chat_id):
    """Show UPI payment details"""
    flow = payment_flow_data.get(chat_id)
    if not flow:
        bot.send_message(chat_id, "❌ Payment session expired. Please try again.")
        return
    
    upi_id = get_setting('upi_id')
    amount = flow['amount']
    plan_name = "Monthly Unlimited" if flow['plan_type'] == 'monthly' else f"{flow['credits_requested']} Credits"
    
    # Generate UPI URL
    upi_url = f"upi://pay?pa={upi_id}&pn={urllib.parse.quote(BOT_NAME)}&am={amount}&cu=INR"
    encoded_upi_url = urllib.parse.quote(upi_url)
    qr_url = f"https://quickchart.io/qr?text={encoded_upi_url}&size=300&margin=2"
    
    msg = f"💳 *UPI Payment*\n\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📦 Plan: *{plan_name}*\n"
    msg += f"💰 Amount: *₹{amount}*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"📲 *UPI ID:* `{upi_id}`\n\n"
    msg += f"⚠️ *Instructions:*\n"
    msg += f"1️⃣ Scan the QR Code OR copy the UPI ID ☝️\n"
    msg += f"2️⃣ ₹{amount} pay karo\n"
    msg += f"3️⃣ Payment ke baad Transaction ID / UTR Number bhejo\n\n"
    msg += f"📝 *Ab neeche apna Transaction ID type karo:*"
    
    try:
        sent_msg = bot.send_photo(chat_id, qr_url, caption=msg, parse_mode="Markdown")
    except Exception as e:
        # Fallback to text if QR fails to load
        sent_msg = bot.send_message(chat_id, msg, parse_mode="Markdown")
        
    auto_delete_message(chat_id, sent_msg.message_id, timeout=900)
    bot.register_next_step_handler(sent_msg, process_upi_transaction_id)

def show_usdt_payment(chat_id):
    """Show USDT payment details"""
    flow = payment_flow_data.get(chat_id)
    if not flow:
        bot.send_message(chat_id, "❌ Payment session expired. Please try again.")
        return
    
    usdt_wallet = get_setting('usdt_wallet')
    amount = flow['amount']
    plan_name = "Monthly Unlimited" if flow['plan_type'] == 'monthly' else f"{flow['credits_requested']} Credits"
    
    # Rough INR to USDT conversion (admin can set exact rate)
    usdt_amount = round(amount / 85, 2)  # Approximate rate
    
    msg = f"💰 *USDT Payment*\n\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📦 Plan: *{plan_name}*\n"
    msg += f"💰 Amount: *₹{amount}* (~${usdt_amount} USDT)\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"📲 *USDT Wallet:*\n`{usdt_wallet}`\n\n"
    msg += f"⚠️ *Instructions:*\n"
    msg += f"1️⃣ Wallet address copy karo ☝️\n"
    msg += f"2️⃣ ~${usdt_amount} USDT send karo (TRC20/ERC20)\n"
    msg += f"3️⃣ Payment ke baad Transaction Hash bhejo\n\n"
    msg += f"📝 *Ab neeche apna Transaction Hash type karo:*"
    
    sent_msg = bot.send_message(chat_id, msg, parse_mode="Markdown")
    auto_delete_message(chat_id, sent_msg.message_id, timeout=900)
    bot.register_next_step_handler(sent_msg, process_usdt_transaction_id)

def process_upi_transaction_id(message):
    """Process UPI transaction ID submitted by user"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if is_command_or_menu(message):
        return
    
    transaction_id = message.text.strip() if message.text else ""
    
    if not transaction_id or len(transaction_id) < 4:
        msg = bot.send_message(chat_id, "❌ Invalid Transaction ID. Please enter a valid UTR / Transaction ID:")
        bot.register_next_step_handler(msg, process_upi_transaction_id)
        return
    
    flow = payment_flow_data.get(chat_id)
    if not flow:
        bot.send_message(chat_id, "❌ Payment session expired. Please try again from VIP Plans.")
        return
    
    # Check auto verify setting
    if is_setting_on('auto_verify'):
        # Auto approve (future implementation)
        request_id = create_payment_request(user_id, flow['amount'], 'upi', flow['plan_type'], 
                                            flow['credits_requested'], transaction_id)
        approve_payment(request_id, ADMIN_USER_ID)
        
        bot.send_message(chat_id, "✅ *Payment Verified Successfully!*\n\nAapka plan activate ho gaya hai! 🎉", parse_mode="Markdown")
        
        # Notify admin
        notify_msg = f"💳 *Auto-Approved Payment*\n\nUser: {user_id}\nAmount: ₹{flow['amount']}\nTxn: `{transaction_id}`\nPlan: {flow['plan_type']}"
        bot.send_message(ADMIN_USER_ID, notify_msg, parse_mode="Markdown")
    else:
        # Manual verification
        request_id = create_payment_request(user_id, flow['amount'], 'upi', flow['plan_type'], 
                                            flow['credits_requested'], transaction_id)
        
        bot.send_message(chat_id, 
                        "⏳ *Payment Request Submitted!*\n\n"
                        f"📋 Request ID: `#{request_id}`\n"
                        f"💳 Method: UPI\n"
                        f"💰 Amount: ₹{flow['amount']}\n"
                        f"📝 Transaction ID: `{transaction_id}`\n\n"
                        "Admin verify karega. Approve hone pe aapko notification milega! ✅",
                        parse_mode="Markdown")
        
        # Notify admin
        user_info = message.from_user
        admin_markup = types.InlineKeyboardMarkup(row_width=2)
        approve_btn = types.InlineKeyboardButton("✅ Approve", callback_data=f"payment_approve_{request_id}")
        reject_btn = types.InlineKeyboardButton("❌ Reject", callback_data=f"payment_reject_{request_id}")
        admin_markup.add(approve_btn, reject_btn)
        
        plan_name = "Monthly 30 Days" if flow['plan_type'] == 'monthly' else f"{flow['credits_requested']} Credits"
        
        notify_msg = f"💳 *New Payment Request #{request_id}*\n\n"
        notify_msg += f"👤 User: [{user_info.first_name or 'User'}](tg://user?id={user_id})\n"
        notify_msg += f"🆔 User ID: `{user_id}`\n"
        notify_msg += f"📦 Plan: *{plan_name}*\n"
        notify_msg += f"💰 Amount: *₹{flow['amount']}*\n"
        notify_msg += f"💳 Method: UPI\n"
        notify_msg += f"📝 Transaction ID: `{transaction_id}`\n\n"
        notify_msg += f"⚡ Approve or Reject?"
        
        bot.send_message(ADMIN_USER_ID, notify_msg, parse_mode="Markdown", reply_markup=admin_markup)
    
    # Clean up flow data
    payment_flow_data.pop(chat_id, None)
    show_menu(chat_id)

def process_usdt_transaction_id(message):
    """Process USDT transaction hash submitted by user"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if is_command_or_menu(message):
        return
    
    transaction_id = message.text.strip() if message.text else ""
    
    if not transaction_id or len(transaction_id) < 4:
        msg = bot.send_message(chat_id, "❌ Invalid Transaction Hash. Please enter a valid hash:")
        bot.register_next_step_handler(msg, process_usdt_transaction_id)
        return
    
    flow = payment_flow_data.get(chat_id)
    if not flow:
        bot.send_message(chat_id, "❌ Payment session expired. Please try again from VIP Plans.")
        return
    
    if is_setting_on('auto_verify'):
        request_id = create_payment_request(user_id, flow['amount'], 'usdt', flow['plan_type'], 
                                            flow['credits_requested'], transaction_id)
        approve_payment(request_id, ADMIN_USER_ID)
        bot.send_message(chat_id, "✅ *Payment Verified Successfully!*\n\nAapka plan activate ho gaya hai! 🎉", parse_mode="Markdown")
        notify_msg = f"💰 *Auto-Approved USDT Payment*\n\nUser: {user_id}\nAmount: ₹{flow['amount']}\nHash: `{transaction_id}`\nPlan: {flow['plan_type']}"
        bot.send_message(ADMIN_USER_ID, notify_msg, parse_mode="Markdown")
    else:
        request_id = create_payment_request(user_id, flow['amount'], 'usdt', flow['plan_type'], 
                                            flow['credits_requested'], transaction_id)
        
        bot.send_message(chat_id, 
                        "⏳ *Payment Request Submitted!*\n\n"
                        f"📋 Request ID: `#{request_id}`\n"
                        f"💰 Method: USDT\n"
                        f"💰 Amount: ₹{flow['amount']}\n"
                        f"📝 Transaction Hash: `{transaction_id}`\n\n"
                        "Admin verify karega. Approve hone pe aapko notification milega! ✅",
                        parse_mode="Markdown")
        
        user_info = message.from_user
        admin_markup = types.InlineKeyboardMarkup(row_width=2)
        approve_btn = types.InlineKeyboardButton("✅ Approve", callback_data=f"payment_approve_{request_id}")
        reject_btn = types.InlineKeyboardButton("❌ Reject", callback_data=f"payment_reject_{request_id}")
        admin_markup.add(approve_btn, reject_btn)
        
        plan_name = "Monthly 30 Days" if flow['plan_type'] == 'monthly' else f"{flow['credits_requested']} Credits"
        
        notify_msg = f"💰 *New USDT Payment #{request_id}*\n\n"
        notify_msg += f"👤 User: [{user_info.first_name or 'User'}](tg://user?id={user_id})\n"
        notify_msg += f"🆔 User ID: `{user_id}`\n"
        notify_msg += f"📦 Plan: *{plan_name}*\n"
        notify_msg += f"💰 Amount: *₹{flow['amount']}*\n"
        notify_msg += f"💰 Method: USDT\n"
        notify_msg += f"📝 Txn Hash: `{transaction_id}`\n\n"
        notify_msg += f"⚡ Approve or Reject?"
        
        bot.send_message(ADMIN_USER_ID, notify_msg, parse_mode="Markdown", reply_markup=admin_markup)
    
    payment_flow_data.pop(chat_id, None)
    show_menu(chat_id)

# Credit amount input storage
credit_amount_input = {}

def ask_credit_amount(chat_id):
    """Ask user how many credits they want to buy"""
    credit_price = get_setting('credit_price')
    msg = f"🔢 *Custom Credit Purchase*\n\n"
    msg += f"💰 Price: ₹{credit_price} per 10 credits\n\n"
    msg += f"📝 Kitne credits chahiye? (Minimum 10)\n"
    msg += f"Example: 10, 20, 30, 50, 100..."
    
    sent_msg = bot.send_message(chat_id, msg, parse_mode="Markdown")
    bot.register_next_step_handler(sent_msg, process_credit_amount_input)

def process_credit_amount_input(message):
    """Process credit amount input"""
    chat_id = message.chat.id
    
    if is_command_or_menu(message):
        return
    
    try:
        amount = int(message.text.strip())
        if amount < 10:
            msg = bot.send_message(chat_id, "❌ Minimum 10 credits chahiye. Dobara enter karo:")
            bot.register_next_step_handler(msg, process_credit_amount_input)
            return
        
        credit_price = int(get_setting('credit_price'))
        total_price = (amount // 10) * credit_price
        actual_credits = (amount // 10) * 10
        
        if actual_credits == 0:
            msg = bot.send_message(chat_id, "❌ Minimum 10 credits chahiye. Dobara enter karo:")
            bot.register_next_step_handler(msg, process_credit_amount_input)
            return
        
        show_payment_methods(chat_id, 'credits', total_price, actual_credits)
        
    except ValueError:
        msg = bot.send_message(chat_id, "❌ Sirf number enter karo. Example: 10, 20, 50")
        bot.register_next_step_handler(msg, process_credit_amount_input)

# ============================================================
#              ADMIN PANEL FUNCTIONS
# ============================================================

def show_admin_panel(chat_id):
    """Show admin panel"""
    total_users = get_total_users_count()
    vip_users = get_vip_users_count()
    pending_payments = get_pending_payments_count()
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    users_btn = types.InlineKeyboardButton("👥 Users", callback_data="admin_users")
    payments_btn = types.InlineKeyboardButton(f"💳 Payments ({pending_payments})", callback_data="admin_payments")
    codes_btn = types.InlineKeyboardButton("🎟️ Gen Codes", callback_data="admin_codes")
    settings_btn = types.InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")
    stats_btn = types.InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")
    report_btn = types.InlineKeyboardButton("📄 Gen Users Report", callback_data="admin_report")
    manage_user_btn = types.InlineKeyboardButton("👤 Manage User", callback_data="admin_manage_user")
    
    markup.add(users_btn, payments_btn)
    markup.add(codes_btn, settings_btn)
    markup.add(stats_btn, report_btn)
    markup.add(manage_user_btn)
    
    msg = f"⚙️ *{BOT_NAME} — Admin Panel*\n\n"
    msg += f"👥 Total Users: *{total_users}*\n"
    msg += f"💎 VIP Users: *{vip_users}*\n"
    msg += f"💳 Pending Payments: *{pending_payments}*\n\n"
    msg += f"👇 Choose an option:"
    
    bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)

def show_admin_settings(chat_id, message_id=None):
    """Show settings toggle panel"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    settings_list = [
        ('force_join_enabled', '🔒 Force Join'),
        ('upi_enabled', '💳 UPI Payment'),
        ('usdt_enabled', '💰 USDT Payment'),
        ('auto_verify', '🤖 Auto Verify'),
        ('buy_from_store_enabled', '🛒 Buy from Store'),
        ('redeem_code_enabled', '🎟️ Redeem Code'),
    ]
    
    for key, label in settings_list:
        status = "✅ ON" if is_setting_on(key) else "❌ OFF"
        btn = types.InlineKeyboardButton(f"{label} — {status}", callback_data=f"toggle_{key}")
        markup.add(btn)
    
    # Config buttons
    upi_btn = types.InlineKeyboardButton(f"📝 Change UPI ID", callback_data="config_upi_id")
    usdt_btn = types.InlineKeyboardButton(f"📝 Change USDT Wallet", callback_data="config_usdt_wallet")
    store_btn = types.InlineKeyboardButton(f"📝 Change Store Link", callback_data="config_store_link")
    price_btn = types.InlineKeyboardButton(f"📝 Change Prices", callback_data="config_prices")
    back_btn = types.InlineKeyboardButton("🔙 Back", callback_data="admin_back")
    
    markup.add(upi_btn)
    markup.add(usdt_btn)
    markup.add(store_btn)
    markup.add(price_btn)
    markup.add(back_btn)
    
    msg = f"⚙️ *Bot Settings*\n\n"
    msg += f"💳 UPI ID: `{get_setting('upi_id')}`\n"
    msg += f"💰 USDT: `{get_setting('usdt_wallet')}`\n"
    msg += f"🛒 Store: `{get_setting('store_link')}`\n"
    msg += f"💵 Monthly: ₹{get_setting('monthly_price')}\n"
    msg += f"💵 Credits: ₹{get_setting('credit_price')}/10\n\n"
    msg += f"Toggle karne ke liye button dabao:"
    
    if message_id:
        try:
            bot.edit_message_text(msg, chat_id, message_id, parse_mode="Markdown", reply_markup=markup)
        except:
            bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)

def show_admin_pending_payments(chat_id, message_id=None):
    """Show pending payments for admin"""
    payments = get_pending_payments()
    
    if not payments:
        msg = "💳 *No Pending Payments*\n\nKoi pending payment nahi hai! ✅"
        markup = types.InlineKeyboardMarkup()
        back_btn = types.InlineKeyboardButton("🔙 Back", callback_data="admin_back")
        markup.add(back_btn)
        
        if message_id:
            try:
                bot.edit_message_text(msg, chat_id, message_id, parse_mode="Markdown", reply_markup=markup)
            except:
                bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)
        else:
            bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)
        return
    
    for payment in payments[:10]:  # Show latest 10
        markup = types.InlineKeyboardMarkup(row_width=2)
        approve_btn = types.InlineKeyboardButton("✅ Approve", callback_data=f"payment_approve_{payment['request_id']}")
        reject_btn = types.InlineKeyboardButton("❌ Reject", callback_data=f"payment_reject_{payment['request_id']}")
        markup.add(approve_btn, reject_btn)
        
        plan_name = "Monthly 30 Days" if payment['plan_type'] == 'monthly' else f"{payment['credits_requested']} Credits"
        
        msg = f"💳 *Payment #{payment['request_id']}*\n\n"
        msg += f"👤 User ID: `{payment['user_id']}`\n"
        msg += f"📦 Plan: *{plan_name}*\n"
        msg += f"💰 Amount: *₹{payment['amount']}*\n"
        msg += f"💳 Method: {payment['payment_type'].upper()}\n"
        msg += f"📝 Txn ID: `{payment['transaction_id']}`\n"
        msg += f"📅 Time: {payment['created_at']}"
        
        bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)

def show_user_details_admin(chat_id, target_user_id):
    """Show user details for admin management"""
    user_credits_info = get_user_credits(target_user_id)
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (target_user_id,))
    user_info = c.fetchone()
    conn.close()
    
    if not user_info:
        bot.send_message(chat_id, f"❌ User `{target_user_id}` not found in database.", parse_mode="Markdown")
        return
    
    is_banned = is_user_banned(target_user_id)
    has_membership = has_active_membership(target_user_id)
    
    # Escape markdown special chars in user fields
    safe_first = (user_info['first_name'] or 'N/A').replace('_', '\\_').replace('*', '\\*').replace('`', '\\`')
    safe_last = (user_info['last_name'] or '').replace('_', '\\_').replace('*', '\\*').replace('`', '\\`')
    safe_username = (user_info['username'] or 'N/A').replace('_', '\\_')
    
    msg = f"👤 *User Details*\n\n"
    msg += f"🆔 ID: `{target_user_id}`\n"
    msg += f"👤 Name: {safe_first} {safe_last}\n"
    msg += f"📛 Username: @{safe_username}\n"
    msg += f"📅 Joined: {user_info['date_joined']}\n\n"
    
    if user_credits_info:
        msg += f"💰 Credits: *{user_credits_info['credits']}*\n"
        msg += f"💎 Membership: *{'Active ✅' if has_membership else 'None ❌'}*\n"
        if has_membership:
            msg += f"📅 Expiry: {user_credits_info['membership_expiry']}\n"
        msg += f"🔍 Total Searches: {user_credits_info['total_searches']}\n"
    
    # Check Master Search custom access
    has_custom_master = False
    if user_credits_info:
        try:
            has_custom_master = (user_credits_info['master_search_access'] == 1)
        except (IndexError, KeyError):
            pass
    master_allowed = has_membership or has_custom_master or (target_user_id == ADMIN_USER_ID)
    master_status_str = "Active (VIP) 💎" if has_membership else ("Granted (Custom) ✅" if has_custom_master else "Disabled ❌")
    msg += f"⚡ Master Search: *{master_status_str}*\n"
    
    msg += f"🚫 Banned: *{'Yes ❌' if is_banned else 'No ✅'}*\n"
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    credits_btn = types.InlineKeyboardButton("💰 Give Credits", callback_data=f"admin_give_credits_{target_user_id}")
    membership_btn = types.InlineKeyboardButton("💎 Give 30d VIP", callback_data=f"admin_give_membership_{target_user_id}")
    
    if is_banned:
        ban_btn = types.InlineKeyboardButton("✅ Unban", callback_data=f"admin_unban_{target_user_id}")
    else:
        ban_btn = types.InlineKeyboardButton("🚫 Ban", callback_data=f"admin_ban_{target_user_id}")
    
    master_btn_text = "⚡ Revoke Master Search" if has_custom_master else "⚡ Allow Master Search"
    master_btn = types.InlineKeyboardButton(master_btn_text, callback_data=f"admin_toggle_master_{target_user_id}")
    
    back_btn = types.InlineKeyboardButton("🔙 Back", callback_data="admin_back")
    remove_credits_btn = types.InlineKeyboardButton("➖ Remove Credits", callback_data=f"admin_remove_credits_{target_user_id}")
    
    markup.add(credits_btn, membership_btn)
    markup.add(remove_credits_btn, ban_btn)
    markup.add(master_btn)
    markup.add(back_btn)
    
    bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)

# Admin input handlers storage
admin_input_handler = {}

# ============================================================
#              BOT COMMAND HANDLERS
# ============================================================

@bot.message_handler(commands=['start', 'msg', 'ban', 'unban', 'banned', 'admin', 'user', 'give', 'gencode', 'settings', 'payments', 'allowmaster', 'revokemaster'])
def handle_commands(message):
    user_id = message.from_user.id
    if rate_limiter.is_rate_limited(user_id, cooldown_seconds=0.8):
        return
    
    cmd = message.text.split()[0].lower() if message.text else ''
    
    if cmd == '/start':
        start(message)
    elif cmd == '/msg':
        start_announcement(message)
    elif cmd == '/ban':
        ban_user_command(message)
    elif cmd == '/unban':
        unban_user_command(message)
    elif cmd == '/banned':
        show_banned_users(message)
    elif cmd == '/admin':
        admin_command(message)
    elif cmd == '/user':
        user_info_command(message)
    elif cmd == '/give':
        give_command(message)
    elif cmd == '/gencode':
        gencode_command(message)
    elif cmd == '/settings':
        settings_command(message)
    elif cmd == '/payments':
        payments_command(message)
    elif cmd == '/allowmaster':
        handle_allow_master(message, True)
    elif cmd == '/revokemaster':
        handle_allow_master(message, False)

# ============================================================
#              /start HANDLER (WITH FORCE JOIN + CREDITS)
# ============================================================

def start(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    
    save_user(message.from_user.id, message.from_user.username, 
              message.from_user.first_name, message.from_user.last_name)
    
    # Check force join
    if not check_force_join(message.from_user.id):
        send_force_join_message(message.chat.id)
        return
    
    # Give free credits to first-time users
    credits_given = give_free_credits(message.from_user.id)
    
    user_info = get_user_credits(message.from_user.id)
    credits = user_info['credits'] if user_info else 0
    has_vip = has_active_membership(message.from_user.id)
    
    welcome_msg = f"👋 *Welcome to {BOT_NAME} Bot!*\n\n"
    
    if credits_given:
        welcome_msg += f"🎁 *Congratulations!* Aapko 2 FREE search credits mile hain!\n\n"
    
    if has_vip:
        welcome_msg += f"💎 *VIP Member* — Unlimited Searches ✅\n"
    else:
        welcome_msg += f"💰 *Credits:* {credits}\n"
    
    welcome_msg += f"\n🔍 I can find Number Info, Aadhaar Info, Family Info, and Vehicle Info.\n"
    welcome_msg += f"Just choose an option below! 👇"
    
    wrapped_send_message(message.chat.id, welcome_msg, parse_mode="Markdown")
    show_menu(message.chat.id)

# ============================================================
#              ADMIN COMMANDS
# ============================================================

def admin_command(message):
    if message.from_user.id != ADMIN_USER_ID:
        wrapped_reply_to(message, "❌ Unauthorized.")
        return
    show_admin_panel(message.chat.id)

def user_info_command(message):
    """Admin: /user <user_id>"""
    if message.from_user.id != ADMIN_USER_ID:
        wrapped_reply_to(message, "❌ Unauthorized.")
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        msg = bot.reply_to(message, "Please enter the User ID you want to inspect:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_admin_user_id_input)
        return
    
    try:
        target_id = int(parts[1].strip())
        show_user_details_admin(message.chat.id, target_id)
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID.")

def process_admin_user_id_input(message):
    if is_command_or_menu(message):
        return
    try:
        target_id = int(message.text.strip())
        show_user_details_admin(message.chat.id, target_id)
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID.")

def give_command(message):
    """Admin: /give <user_id> credits <amount> or /give <user_id> membership <days>"""
    if message.from_user.id != ADMIN_USER_ID:
        wrapped_reply_to(message, "❌ Unauthorized.")
        return
    
    parts = message.text.split()
    if len(parts) < 4:
        bot.reply_to(message, "Usage:\n`/give <user_id> credits <amount>`\n`/give <user_id> membership <days>`", parse_mode="Markdown")
        return
    
    try:
        target_id = int(parts[1].strip())
        give_type = parts[2].strip().lower()
        amount = int(parts[3].strip())
        
        if give_type == 'credits':
            ensure_user_credits(target_id)
            add_credits(target_id, amount)
            bot.reply_to(message, f"✅ {amount} credits given to user `{target_id}`.", parse_mode="Markdown")
            try:
                bot.send_message(target_id, f"🎁 *Gift from Admin!*\n\nAapko {amount} search credits mile hain! 🎉", parse_mode="Markdown")
            except:
                pass
        elif give_type == 'membership':
            give_membership(target_id, amount)
            bot.reply_to(message, f"✅ {amount} days membership given to user `{target_id}`.", parse_mode="Markdown")
            try:
                bot.send_message(target_id, f"💎 *VIP Membership Activated!*\n\n{amount} days ki membership mil gayi hai! 🎉\nUnlimited searches enjoy karo!", parse_mode="Markdown")
            except:
                pass
        else:
            bot.reply_to(message, "❌ Use `credits` or `membership`.", parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "❌ Invalid values.")

def handle_allow_master(message, grant=True):
    """Admin: /allowmaster <user_id> or /revokemaster <user_id>"""
    if message.from_user.id != ADMIN_USER_ID:
        wrapped_reply_to(message, "❌ Unauthorized.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        action_name = "allowmaster" if grant else "revokemaster"
        bot.reply_to(message, f"Usage:\n`/{action_name} <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID. Sirf digits dalein.", parse_mode="Markdown")
        return
        
    set_master_search_access(target_id, grant)
    action_str = "GRANTED ✅" if grant else "REVOKED ❌"
    bot.reply_to(message, f"⚡ Master Search access for user `{target_id}`: *{action_str}*", parse_mode="Markdown")
    if grant:
        try:
            bot.send_message(target_id, "⚡ *Master Deep Search Activated!*\n\nAdmin ne aapke account ke liye Master Deep Search (Number ➔ Aadhaar ➔ Family) ka access on kar diya hai! 🎉", parse_mode="Markdown")
        except:
            pass

def gencode_command(message):
    """Admin: /gencode free credits <amount> [qty] or /gencode paid credits <amount> [qty]"""
    if message.from_user.id != ADMIN_USER_ID:
        wrapped_reply_to(message, "❌ Unauthorized.")
        return
    
    parts = message.text.split()
    if len(parts) < 4:
        bot.reply_to(message, 
            "Usage:\n"
            "`/gencode free credits <amount> [qty]`\n"
            "`/gencode free monthly <days> [qty]`\n"
            "`/gencode paid credits <amount> [qty]`\n"
            "`/gencode paid monthly <days> [qty]`\n\n"
            "🆓 *Free* = 1 user sirf 1 code use kar payega (batch limit)\n"
            "💰 *Paid* = Har code independent, no limit", 
            parse_mode="Markdown")
        return
    
    try:
        mode = parts[1].strip().lower()  # free or paid
        code_type = parts[2].strip().lower()  # credits or monthly
        amount = int(parts[3].strip())
        quantity = int(parts[4].strip()) if len(parts) > 4 else 1
        
        if mode not in ['free', 'paid']:
            bot.reply_to(message, "❌ Use `free` or `paid` as first argument.", parse_mode="Markdown")
            return
        
        if quantity > 50:
            bot.reply_to(message, "❌ Maximum 50 codes at a time.")
            return
            
        if code_type in ['monthly', 'credits']:
            generated_codes = []
            is_free = (mode == 'free')
            batch_id = str(uuid.uuid4()) if is_free else None
            
            for _ in range(quantity):
                code = generate_redeem_code(code_type, amount, batch_id, is_free)
                generated_codes.append(f"`{code}`")
            
            value_text = f"{amount} days membership" if code_type == 'monthly' else f"{amount} credits"
            mode_text = "🆓 FREE (1 per user)" if is_free else "💰 PAID (no limit)"
            
            msg = f"🎟️ *{quantity} Redeem Code(s) Generated!*\n\n"
            msg += f"📦 Type: {code_type.title()}\n"
            msg += f"💎 Value: {value_text}\n"
            msg += f"🔖 Mode: {mode_text}\n\n"
            msg += f"📋 Codes:\n" + "\n".join(generated_codes) + "\n\n"
            if is_free:
                msg += f"⚠️ Ek user is batch ka sirf 1 code use kar payega!"
            else:
                msg += f"✅ Har code independent hai, koi limit nahi!"
            
            bot.reply_to(message, msg, parse_mode="Markdown")
        else:
            bot.reply_to(message, "❌ Use `monthly` or `credits`.", parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "❌ Invalid amount or quantity.")

def settings_command(message):
    if message.from_user.id != ADMIN_USER_ID:
        wrapped_reply_to(message, "❌ Unauthorized.")
        return
    show_admin_settings(message.chat.id)

def payments_command(message):
    if message.from_user.id != ADMIN_USER_ID:
        wrapped_reply_to(message, "❌ Unauthorized.")
        return
    show_admin_pending_payments(message.chat.id)

# ============================================================
#              ANNOUNCEMENT FEATURE
# ============================================================

def start_announcement(message):
    if message.from_user.id != ADMIN_USER_ID:
        wrapped_reply_to(message, "❌ You are not authorized to use this command.")
        return
    
    admin_announcement_data[ADMIN_USER_ID] = []
    msg = bot.send_message(ADMIN_USER_ID, "📢 *Announcement Mode*\n\nPlease send the messages you want to broadcast. You can send text, media, files, stickers, etc.\n\nWhen you are finished, type `/done` to start the broadcast.", parse_mode="Markdown")
    bot.register_next_step_handler(msg, collect_announcement_messages)

def collect_announcement_messages(message):
    if message.text == '/done':
        if not admin_announcement_data.get(ADMIN_USER_ID):
            bot.send_message(ADMIN_USER_ID, "❌ No messages collected. Announcement cancelled.")
            return
        confirm_announcement(message)
        return
    
    admin_announcement_data[ADMIN_USER_ID].append(message)
    bot.send_message(ADMIN_USER_ID, f"✅ Message {len(admin_announcement_data[ADMIN_USER_ID])} added. Send more or type `/done`.")
    bot.register_next_step_handler(message, collect_announcement_messages)

def confirm_announcement(message):
    broadcast_id = generate_broadcast_id()
    markup = types.InlineKeyboardMarkup()
    confirm_btn = types.InlineKeyboardButton("✅ Yes, Send to All", callback_data=f"bc_confirm:{broadcast_id}")
    cancel_btn = types.InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")
    markup.add(confirm_btn, cancel_btn)
    
    msg_count = len(admin_announcement_data[ADMIN_USER_ID])
    bot.send_message(ADMIN_USER_ID, 
                 f"📢 *Broadcast Confirmation*\n\n"
                 f"Total Messages: {msg_count}\n"
                 f"Total Users: {len(get_all_users())}\n\n"
                 f"Are you sure you want to send these messages to all users?",
                 reply_markup=markup, parse_mode="Markdown")

def run_broadcast(user_ids, messages_to_send):
    success_count = 0
    fail_count = 0
    total_users = len(user_ids)
    
    try:
        status_msg = bot.send_message(ADMIN_USER_ID, f"🚀 *Broadcast Started* to {total_users} users!", parse_mode="Markdown")
        status_msg_id = status_msg.message_id
    except Exception:
        status_msg_id = None
    
    last_update_time = time.time()
    
    for index, user_id in enumerate(user_ids, 1):
        user_success = True
        try:
            for msg in messages_to_send:
                try:
                    bot.copy_message(user_id, msg.chat.id, msg.message_id)
                except Exception as e:
                    err_str = str(e).lower()
                    if "forbidden" in err_str or "chat not found" in err_str or "user is deactivated" in err_str:
                        delete_user(user_id)
                        user_success = False
                        break
                    elif "retry after" in err_str:
                        time.sleep(2)
                        user_success = False
                        break
                    else:
                        user_success = False
                        break
            
            if user_success:
                success_count += 1
            else:
                fail_count += 1
        except Exception:
            fail_count += 1
        
        # Edit progress message every 5 seconds to avoid rate limits
        now = time.time()
        if (now - last_update_time >= 5.0 or index == total_users) and status_msg_id:
            try:
                progress = (index / total_users) * 100
                bot.edit_message_text(
                    f"📊 *Broadcast Progress*: {progress:.1f}%\n"
                    f"👥 Done: {index}/{total_users}\n"
                    f"✅ Success: {success_count}\n"
                    f"❌ Failed: {fail_count}",
                    ADMIN_USER_ID, status_msg_id, parse_mode="Markdown"
                )
                last_update_time = now
            except Exception:
                pass
        
        # 0.04s sleep = ~25 msgs/sec max (strictly adhering to Telegram 30 msg/sec global limit)
        time.sleep(0.04)
    
    try:
        bot.send_message(ADMIN_USER_ID, f"📊 *Broadcast Completed!*\n\n✅ Success: {success_count}\n❌ Failed/Cleaned: {fail_count}\n👥 Total: {total_users}", parse_mode="Markdown")
    except Exception:
        pass
    admin_announcement_data[ADMIN_USER_ID] = []

# ============================================================
#              CALLBACK QUERY HANDLERS
# ============================================================

@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    data = call.data
    
    # Anti-spam rate limiting (1.0s cooldown per user)
    if rate_limiter.is_rate_limited(user_id, cooldown_seconds=1.0):
        try:
            bot.answer_callback_query(call.id, "⏳ Please wait a moment!", show_alert=False)
        except Exception:
            pass
        return
    
    print(f"👉 [CALLBACK] Received: '{data}' from User ID: {user_id} (Admin: {ADMIN_USER_ID})")
    
    # --- Force Join Check ---
    if data == "check_force_join":
        if check_force_join(user_id):
            try:
                bot.answer_callback_query(call.id, "✅ Verified! Welcome!")
            except Exception:
                pass
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except:
                pass
            
            save_user(user_id, call.from_user.username, call.from_user.first_name, call.from_user.last_name)
            credits_given = give_free_credits(user_id)
            
            user_info = get_user_credits(user_id)
            credits = user_info['credits'] if user_info else 0
            has_vip = has_active_membership(user_id)
            
            welcome_msg = f"👋 *Welcome to {BOT_NAME} Bot!*\n\n"
            if credits_given:
                welcome_msg += f"🎁 *Congratulations!* Aapko 2 FREE search credits mile hain!\n\n"
            if has_vip:
                welcome_msg += f"💎 *VIP Member* — Unlimited Searches ✅\n"
            else:
                welcome_msg += f"💰 *Credits:* {credits}\n"
            welcome_msg += f"\n🔍 Choose an option below! 👇"
            
            wrapped_send_message(chat_id, welcome_msg, parse_mode="Markdown")
            show_menu(chat_id)
        else:
            bot.answer_callback_query(call.id, "❌ Abhi tak join nahi kiya! Pehle channel join karo.", show_alert=True)
        return
    
    # --- VIP Plans ---
    if data == "show_vip_plans":
        bot.answer_callback_query(call.id)
        show_vip_plans(chat_id)
        return
    
    if data == "plan_monthly":
        bot.answer_callback_query(call.id)
        monthly_price = int(get_setting('monthly_price'))
        show_payment_methods(chat_id, 'monthly', monthly_price)
        return
    
    if data == "plan_credits":
        bot.answer_callback_query(call.id)
        ask_credit_amount(chat_id)
        return
    
    if data == "plan_store":
        bot.answer_callback_query(call.id)
        store_link = get_setting('store_link')
        markup = types.InlineKeyboardMarkup()
        store_btn = types.InlineKeyboardButton("🛒 Open Store", url=store_link)
        markup.add(store_btn)
        bot.send_message(chat_id, "🛒 *Buy VIP from Store*\n\nNeeche button se store open karo aur VIP kharido! 👇", parse_mode="Markdown", reply_markup=markup)
        return
    
    if data == "plan_redeem":
        bot.answer_callback_query(call.id)
        msg = "🎟️ *Redeem Code*\n\nApna redeem code neeche type karo:"
        sent_msg = bot.send_message(chat_id, msg, parse_mode="Markdown")
        bot.register_next_step_handler(sent_msg, process_redeem_code_input)
        return
    
    # --- Payment Methods ---
    if data == "pay_upi":
        bot.answer_callback_query(call.id)
        show_upi_payment(chat_id)
        return
    
    if data == "pay_usdt":
        bot.answer_callback_query(call.id)
        show_usdt_payment(chat_id)
        return
    
    if data == "pay_cancel":
        bot.answer_callback_query(call.id, "❌ Payment cancelled.")
        payment_flow_data.pop(chat_id, None)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        return
    
    # --- Payment Approve/Reject (Admin) ---
    if data.startswith("payment_approve_"):
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        
        request_id = int(data.replace("payment_approve_", ""))
        req = approve_payment(request_id, user_id)
        
        if req:
            bot.answer_callback_query(call.id, "✅ Payment Approved!")
            
            plan_name = "Monthly 30 Days" if req['plan_type'] == 'monthly' else f"{req['credits_requested']} Credits"
            
            try:
                bot.edit_message_text(
                    f"✅ *APPROVED* — Payment #{request_id}\n\n"
                    f"👤 User: `{req['user_id']}`\n"
                    f"📦 Plan: {plan_name}\n"
                    f"💰 Amount: ₹{req['amount']}\n"
                    f"📝 Txn: `{req['transaction_id']}`",
                    chat_id, call.message.message_id, parse_mode="Markdown"
                )
            except:
                pass
            
            # Notify user
            try:
                if req['plan_type'] == 'monthly':
                    bot.send_message(req['user_id'], 
                                    "🎉 *Payment Approved!*\n\n"
                                    "💎 30 Days VIP Membership activated!\n"
                                    "Unlimited searches enjoy karo! 🚀", 
                                    parse_mode="Markdown")
                else:
                    bot.send_message(req['user_id'], 
                                    f"🎉 *Payment Approved!*\n\n"
                                    f"💰 {req['credits_requested']} credits add ho gaye hain!\n"
                                    f"Happy searching! 🚀", 
                                    parse_mode="Markdown")
            except:
                pass
        else:
            bot.answer_callback_query(call.id, "❌ Request not found or already processed.", show_alert=True)
        return
    
    if data.startswith("payment_reject_"):
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        
        request_id = int(data.replace("payment_reject_", ""))
        req = reject_payment(request_id, user_id)
        
        if req:
            bot.answer_callback_query(call.id, "❌ Payment Rejected!")
            
            try:
                bot.edit_message_text(
                    f"❌ *REJECTED* — Payment #{request_id}\n\n"
                    f"👤 User: `{req['user_id']}`\n"
                    f"💰 Amount: ₹{req['amount']}\n"
                    f"📝 Txn: `{req['transaction_id']}`",
                    chat_id, call.message.message_id, parse_mode="Markdown"
                )
            except:
                pass
            
            try:
                bot.send_message(req['user_id'], 
                                "❌ *Payment Rejected*\n\n"
                                "Aapki payment request reject ho gayi hai.\n"
                                "Agar galti se reject hua ho toh admin se contact karo.\n\n"
                                f"Contact: @{ADMIN_USERNAME}", 
                                parse_mode="Markdown")
            except:
                pass
        else:
            bot.answer_callback_query(call.id, "❌ Request not found or already processed.", show_alert=True)
        return
    
    # --- Broadcast ---
    if data.startswith('bc_'):
        if data == 'bc_cancel':
            bot.edit_message_text("❌ Broadcast cancelled.", chat_id, call.message.message_id)
            admin_announcement_data[ADMIN_USER_ID] = []
            return
        
        if data.startswith('bc_confirm:'):
            messages_to_send = admin_announcement_data.get(ADMIN_USER_ID)
            if not messages_to_send:
                bot.edit_message_text("❌ Broadcast messages not found.", chat_id, call.message.message_id)
                return
            
            bot.edit_message_text("⚡ Sending broadcast to all users...", chat_id, call.message.message_id)
            users = get_all_users()
            if not users:
                bot.edit_message_text("❌ No users found in database.", chat_id, call.message.message_id)
                return
            
            threading.Thread(target=run_broadcast, args=(users, messages_to_send), daemon=True).start()
        return
    
    # --- Admin Panel Callbacks ---
    if data == "admin_back":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_admin_panel(chat_id)
        return
    
    if data == "admin_manage_user":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        msg = bot.send_message(chat_id, "Please enter the User ID you want to manage:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_admin_user_id_input)
        return
    
    if data == "admin_users":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, 
                        "👥 *User Management*\n\n"
                        "Kisi user ki detail dekhne ke liye:\n"
                        "`/user <user_id>`\n\n"
                        "Credits dene ke liye:\n"
                        "`/give <user_id> credits <amount>`\n\n"
                        "Membership (VIP) dene ke liye:\n"
                        "`/give <user_id> membership <days>`\n\n"
                        "⚡ Master Search access toggle karne ke liye:\n"
                        "`/allowmaster <user_id>`\n"
                        "`/revokemaster <user_id>`",
                        parse_mode="Markdown")
        return
        
    if data == "admin_report":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        
        bot.answer_callback_query(call.id, "⏳ Generating report... Please wait.")
        
        conn = get_db()
        c = conn.cursor()
        
        # Get VIP users
        c.execute('''
            SELECT u.user_id, u.username, u.first_name, c.membership_expiry 
            FROM users u JOIN user_credits c ON u.user_id = c.user_id 
            WHERE c.membership_expiry > CURRENT_TIMESTAMP
            ORDER BY c.membership_expiry DESC
        ''')
        vip_users = c.fetchall()
        
        # Get All users with credits
        c.execute('''
            SELECT u.user_id, u.username, u.first_name, c.credits, c.daily_free_credits 
            FROM users u JOIN user_credits c ON u.user_id = c.user_id
            ORDER BY c.credits DESC
        ''')
        all_users = c.fetchall()
        
        conn.close()
        
        # Build report text
        report_text = f"=== {BOT_NAME} - Users Report ===\n"
        report_text += f"Generated At: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        report_text += f"--- VIP USERS ({len(vip_users)}) ---\n"
        if not vip_users:
            report_text += "No VIP users found.\n"
        else:
            for row in vip_users:
                username = f"@{row['username']}" if row['username'] else "No Username"
                report_text += f"ID: {row['user_id']} | Name: {row['first_name']} | {username} | Expiry: {row['membership_expiry']}\n"
                
        report_text += f"\n\n--- ALL USERS CREDITS ({len(all_users)}) ---\n"
        for row in all_users:
            username = f"@{row['username']}" if row['username'] else "No Username"
            report_text += f"ID: {row['user_id']} | Name: {row['first_name']} | {username} | Premium Credits: {row['credits']} | Daily: {row['daily_free_credits']}\n"
            
        # Write to file
        import os
        filename = "users_report.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(report_text)
            
        # Send file
        with open(filename, "rb") as f:
            bot.send_document(chat_id, f, caption="✅ Users Report Generated.")
        
        # Clean up
        if os.path.exists(filename):
            os.remove(filename)
        return
    
    if data == "admin_payments":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        show_admin_pending_payments(chat_id, call.message.message_id)
        return
    
    if data == "admin_codes":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, 
                        "🎟️ *Generate Redeem Codes*\n\n"
                        "Monthly membership code:\n"
                        "`/gencode monthly 30`\n\n"
                        "Credits code:\n"
                        "`/gencode credits 50`\n\n"
                        "Example: `/gencode credits 10` → 10 credits ka code banega",
                        parse_mode="Markdown")
        return
    
    if data == "admin_settings":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        show_admin_settings(chat_id, call.message.message_id)
        return
    
    if data == "admin_stats":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        
        total_users = get_total_users_count()
        vip_users = get_vip_users_count()
        pending_payments = get_pending_payments_count()
        
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT SUM(total_searches) as total FROM user_credits')
        result = c.fetchone()
        total_searches = result['total'] if result and result['total'] else 0
        
        c.execute("SELECT COUNT(*) as cnt FROM payment_requests WHERE status = 'approved'")
        result = c.fetchone()
        approved_payments = result['cnt'] if result else 0
        
        c.execute("SELECT SUM(amount) as total FROM payment_requests WHERE status = 'approved'")
        result = c.fetchone()
        total_revenue = result['total'] if result and result['total'] else 0
        
        c.execute("SELECT COUNT(*) as cnt FROM banned_users")
        result = c.fetchone()
        banned_count = result['cnt'] if result else 0
        conn.close()
        
        markup = types.InlineKeyboardMarkup()
        back_btn = types.InlineKeyboardButton("🔙 Back", callback_data="admin_back")
        markup.add(back_btn)
        
        msg = f"📊 *{BOT_NAME} — Statistics*\n\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"👥 Total Users: *{total_users}*\n"
        msg += f"💎 VIP Users: *{vip_users}*\n"
        msg += f"🚫 Banned Users: *{banned_count}*\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"🔍 Total Searches: *{total_searches}*\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"💳 Approved Payments: *{approved_payments}*\n"
        msg += f"💰 Total Revenue: *₹{total_revenue:.0f}*\n"
        msg += f"⏳ Pending Payments: *{pending_payments}*\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━"
        
        try:
            bot.edit_message_text(msg, chat_id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)
        except:
            bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)
        return
    
    # --- Settings Toggle ---
    if data.startswith("toggle_"):
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        
        key = data.replace("toggle_", "")
        current = get_setting(key)
        new_value = '0' if current == '1' else '1'
        set_setting(key, new_value)
        
        status = "ON ✅" if new_value == '1' else "OFF ❌"
        bot.answer_callback_query(call.id, f"Setting changed to {status}")
        show_admin_settings(chat_id, call.message.message_id)
        return
    
    # --- Config Changes ---
    if data == "config_upi_id":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        admin_input_handler[user_id] = 'upi_id'
        msg = bot.send_message(chat_id, "📝 Naya UPI ID enter karo:")
        bot.register_next_step_handler(msg, process_admin_config_input)
        return
    
    if data == "config_usdt_wallet":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        admin_input_handler[user_id] = 'usdt_wallet'
        msg = bot.send_message(chat_id, "📝 Naya USDT Wallet address enter karo:")
        bot.register_next_step_handler(msg, process_admin_config_input)
        return
    
    if data == "config_store_link":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        admin_input_handler[user_id] = 'store_link'
        msg = bot.send_message(chat_id, "📝 Naya Store link enter karo:")
        bot.register_next_step_handler(msg, process_admin_config_input)
        return
    
    if data == "config_prices":
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        admin_input_handler[user_id] = 'prices'
        msg = bot.send_message(chat_id, 
                              "📝 *Change Prices*\n\n"
                              "Format: `monthly_price,credit_price`\n"
                              "Example: `30,5` (₹30 monthly, ₹5 per 10 credits)\n\n"
                              "Enter karo:",
                              parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_admin_config_input)
        return
    
    # --- Admin User Management from inline buttons ---
    if data.startswith("admin_give_credits_"):
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        target_id = int(data.replace("admin_give_credits_", ""))
        admin_input_handler[user_id] = f'give_credits_{target_id}'
        msg = bot.send_message(chat_id, f"💰 User `{target_id}` ko kitne credits dene hain?", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_admin_config_input)
        return
    
    if data.startswith("admin_remove_credits_"):
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        target_id = int(data.replace("admin_remove_credits_", ""))
        admin_input_handler[user_id] = f'remove_credits_{target_id}'
        msg = bot.send_message(chat_id, f"➖ User `{target_id}` se kitne credits remove karne hain?", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_admin_config_input)
        return
    
    if data.startswith("admin_give_membership_"):
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        target_id = int(data.replace("admin_give_membership_", ""))
        give_membership(target_id, 30)
        bot.send_message(chat_id, f"✅ 30 days VIP membership given to user `{target_id}`!", parse_mode="Markdown")
        try:
            bot.send_message(target_id, "💎 *VIP Membership Activated!*\n\n30 days ki membership mil gayi hai! 🎉\nUnlimited searches enjoy karo!", parse_mode="Markdown")
        except:
            pass
        return
    
    if data.startswith("admin_toggle_master_"):
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        target_id = int(data.replace("admin_toggle_master_", ""))
        u_info = get_user_credits(target_id)
        current_status = False
        if u_info:
            try:
                current_status = (u_info['master_search_access'] == 1)
            except (IndexError, KeyError):
                pass
        new_status = not current_status
        set_master_search_access(target_id, new_status)
        status_txt = "GRANTED ✅" if new_status else "REVOKED ❌"
        bot.answer_callback_query(call.id, f"Master Search {status_txt}")
        bot.send_message(chat_id, f"⚡ Master Search access for user `{target_id}`: *{status_txt}*", parse_mode="Markdown")
        if new_status:
            try:
                bot.send_message(target_id, "⚡ *Master Deep Search Activated!*\n\nAdmin ne aapke account ke liye Master Deep Search (Number ➔ Aadhaar ➔ Family) ka access on kar diya hai! 🎉", parse_mode="Markdown")
            except:
                pass
        show_user_details_admin(chat_id, target_id)
        return
    
    if data.startswith("admin_ban_"):
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        target_id = int(data.replace("admin_ban_", ""))
        
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT username, first_name, last_name FROM users WHERE user_id = ?', (target_id,))
        u = c.fetchone()
        conn.close()
        
        ban_user(target_id, u['username'] if u else None, u['first_name'] if u else 'Unknown', 
                u['last_name'] if u else '', user_id, 'Banned via admin panel')
        
        bot.send_message(chat_id, f"🚫 User `{target_id}` banned!", parse_mode="Markdown")
        try:
            bot.send_message(target_id, f"🚫 ACCOUNT BANNED\n\nReason: Banned by admin\nContact: @{ADMIN_USERNAME}")
        except:
            pass
        return
    
    if data.startswith("admin_unban_"):
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        target_id = int(data.replace("admin_unban_", ""))
        unban_user(target_id)
        bot.send_message(chat_id, f"✅ User `{target_id}` unbanned!", parse_mode="Markdown")
        try:
            bot.send_message(target_id, "✅ Your account has been unbanned!")
        except:
            pass
        return
    
    # Fallback to stop loading spinner if no callback matched
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

def process_admin_config_input(message):
    """Process admin config input"""
    user_id = message.from_user.id
    if user_id != ADMIN_USER_ID:
        return
    
    handler_type = admin_input_handler.pop(user_id, None)
    if not handler_type:
        return
    
    text = message.text.strip() if message.text else ""
    
    if handler_type == 'upi_id':
        set_setting('upi_id', text)
        bot.send_message(message.chat.id, f"✅ UPI ID updated to: `{text}`", parse_mode="Markdown")
    elif handler_type == 'usdt_wallet':
        set_setting('usdt_wallet', text)
        bot.send_message(message.chat.id, f"✅ USDT Wallet updated to: `{text}`", parse_mode="Markdown")
    elif handler_type == 'store_link':
        set_setting('store_link', text)
        bot.send_message(message.chat.id, f"✅ Store link updated to: `{text}`", parse_mode="Markdown")
    elif handler_type == 'prices':
        try:
            parts = text.split(',')
            monthly = int(parts[0].strip())
            credits = int(parts[1].strip())
            set_setting('monthly_price', str(monthly))
            set_setting('credit_price', str(credits))
            bot.send_message(message.chat.id, f"✅ Prices updated!\nMonthly: ₹{monthly}\nCredits: ₹{credits}/10", parse_mode="Markdown")
        except:
            bot.send_message(message.chat.id, "❌ Invalid format. Use: `30,5`", parse_mode="Markdown")
    elif handler_type.startswith('give_credits_'):
        target_id = int(handler_type.replace('give_credits_', ''))
        try:
            amount = int(text)
            ensure_user_credits(target_id)
            add_credits(target_id, amount)
            bot.send_message(message.chat.id, f"✅ {amount} credits given to user `{target_id}`!", parse_mode="Markdown")
            try:
                bot.send_message(target_id, f"🎁 *Gift from Admin!*\n\nAapko {amount} search credits mile hain! 🎉", parse_mode="Markdown")
            except:
                pass
        except ValueError:
            bot.send_message(message.chat.id, "❌ Invalid number.")
    elif handler_type.startswith('remove_credits_'):
        target_id = int(handler_type.replace('remove_credits_', ''))
        try:
            amount = int(text)
            if amount <= 0:
                bot.send_message(message.chat.id, "❌ Amount must be positive.")
                return
            ensure_user_credits(target_id)
            conn = get_db()
            try:
                c = conn.cursor()
                c.execute('SELECT credits FROM user_credits WHERE user_id = ?', (target_id,))
                row = c.fetchone()
                current = row['credits'] if row else 0
                new_balance = max(0, current - amount)
                c.execute('UPDATE user_credits SET credits = ? WHERE user_id = ?', (new_balance, target_id))
                conn.commit()
            finally:
                conn.close()
            removed = current - new_balance
            bot.send_message(message.chat.id, f"✅ {removed} credits removed from user `{target_id}`!\n💰 New balance: {new_balance}", parse_mode="Markdown")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Invalid number.")

def process_redeem_code_input(message):
    """Process redeem code input from user"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if is_command_or_menu(message):
        return
    

    
    code = message.text.strip().upper() if message.text else ""
    
    if not code:
        bot.send_message(chat_id, "❌ Please enter a valid code.")
        return
    
    success, msg_text, plan_info = redeem_code(code, user_id)
    bot.send_message(chat_id, msg_text, parse_mode="Markdown")
    
    if success:
        # Notify admin
        try:
            admin_msg = f"🎟️ *Code Redeemed*\n\nUser: `{user_id}`\nCode: `{code}`"
            if plan_info:
                if plan_info['type'] == 'monthly':
                    admin_msg += f"\nPlan: {plan_info['days']} days membership"
                else:
                    admin_msg += f"\nPlan: {plan_info['amount']} credits"
            bot.send_message(ADMIN_USER_ID, admin_msg, parse_mode="Markdown")
        except:
            pass
    
    show_menu(chat_id)

# ============================================================
#           MENU BUTTON HANDLERS (WITH CREDIT CHECK)
# ============================================================

@bot.message_handler(func=lambda message: message.text in [
    "⚡ Master Search", "🔢 Num Info", "🆔 Adhar Info", "👨‍👩‍👧‍👦 Family Info", "🚗 Vehicle Info", 
    "💎 VIP Plans", "👤 My Account", "💬 Chat with Developer", "⚙️ Admin Panel"
])
def handle_menu_buttons(message):
    user_id = message.from_user.id
    if rate_limiter.is_rate_limited(user_id, cooldown_seconds=0.8):
        return
        
    if check_ban_status(user_id, message.chat.id):
        return
    
    # Force join check
    if not check_force_join(user_id):
        send_force_join_message(message.chat.id)
        return
    
    if message.text == "⚡ Master Search":
        ask_master_search(message)
    elif message.text == "🔢 Num Info":
        ask_number(message)
    elif message.text == "🆔 Adhar Info":
        ask_aadhaar_number(message)
    elif message.text == "👨‍👩‍👧‍👦 Family Info":
        ask_family_info(message)
    elif message.text == "🚗 Vehicle Info":
        ask_vehicle_info(message)
    elif message.text == "💎 VIP Plans":
        show_vip_plans(message.chat.id)
    elif message.text == "👤 My Account":
        show_my_account(message)
    elif message.text == "💬 Chat with Developer":
        chat_with_dev(message)
    elif message.text == "⚙️ Admin Panel" and message.chat.id == ADMIN_USER_ID:
        admin_command(message)

# ============================================================
#                 MY ACCOUNT
# ============================================================

def show_my_account(message):
    """Show user's account details"""
    user_id = message.from_user.id
    user_info = get_user_credits(user_id)
    
    has_vip = has_active_membership(user_id)
    credits = user_info['credits'] if user_info else 0
    daily_credits = user_info['daily_free_credits'] if (user_info and 'daily_free_credits' in user_info.keys()) else 0
    total_searches = user_info['total_searches'] if user_info else 0
    
    msg = f"👤 *My Account — {BOT_NAME}*\n\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🆔 User ID: `{user_id}`\n"
    msg += f"👤 Name: {message.from_user.first_name or 'N/A'}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    
    if has_vip:
        expiry = user_info['membership_expiry'] if user_info else 'N/A'
        msg += f"💎 *Status:* VIP Member ✅\n"
        msg += f"📅 *Expiry:* `{expiry}`\n"
        msg += f"🔓 *Searches:* Unlimited\n"
    else:
        msg += f"🎁 *Daily Free Credit:* {daily_credits} (Resets at midnight)\n"
        msg += f"💰 *Premium Credits:* {credits}\n"
        msg += f"💎 *Status:* Free User\n"
    
    master_access = is_master_search_allowed(user_id)
    msg += f"⚡ *Master Search:* {'Enabled ✅' if master_access else 'Locked (VIP Only) 🔒'}\n"
    msg += f"\n🔍 *Total Searches:* {total_searches}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    vip_btn = types.InlineKeyboardButton("💎 Buy VIP / Credits", callback_data="show_vip_plans")
    redeem_btn = types.InlineKeyboardButton("🎟️ Redeem Code", callback_data="plan_redeem")
    markup.add(vip_btn, redeem_btn)
    
    wrapped_send_message(message.chat.id, msg, parse_mode="Markdown", reply_markup=markup)

# ============================================================
#           SEARCH HANDLERS (WITH CREDIT DEDUCTION)
# ============================================================

def classify_master_search_input(text):
    """
    Smart classifier for Master Search input.
    Returns:
      ('mobile', 10-digit-string)
      ('aadhaar', 12-digit-string)
      ('ambiguous_12', 12-digit-string)
      or (None, None)
    """
    if not text:
        return None, None
    raw = str(text).strip()
    digits_only = re.sub(r'\D', '', raw)
    has_plus = raw.startswith('+')
    
    # Exactly 10 digits
    if len(digits_only) == 10:
        return 'mobile', digits_only
        
    # 11 digits starting with 0 (e.g. 09876543210)
    if len(digits_only) == 11 and digits_only.startswith('0'):
        return 'mobile', digits_only[1:]
        
    # Explicit +91 with 12 digits (e.g. +91 98765 43210)
    if has_plus and len(digits_only) == 12 and digits_only.startswith('91'):
        return 'mobile', digits_only[2:]
        
    # 12 digits
    if len(digits_only) == 12:
        # If does not start with 91, it's 100% Aadhaar (since Indian mobile country code is 91)
        if not digits_only.startswith('91'):
            return 'aadhaar', digits_only
            
        # Format 4-4-4 (e.g. 9123 4567 8901 or 9123-4567-8901) -> Aadhaar
        parts = re.split(r'[\s\-]+', raw)
        if len(parts) == 3 and all(len(p) == 4 for p in parts):
            return 'aadhaar', digits_only
            
        # Explicit +91 or 91 with space or hyphen after country code -> Mobile
        if raw.startswith('+91') or (len(parts) >= 2 and parts[0] in ['91', '+91']):
            return 'mobile', digits_only[2:]
            
        # Plain 12 digits starting with 91:
        # Check if 3rd digit is a mobile starting digit (6, 7, 8, 9)
        if digits_only[2] in ['6', '7', '8', '9']:
            return 'ambiguous_12', digits_only
            
        return 'aadhaar', digits_only
        
    return None, None


def execute_master_search_from_mobile(message, clean_num):
    """Execute deep OSINT starting from Mobile Number"""
    # --- STEP 1: Number Search ---
    wrapped_send_message(message.chat.id, "⚡ *Step 1/3:* Mobile number details fetch kar rahe hain... ⏳", parse_mode="Markdown")
    num_data = fetch_number_info_from_api(clean_num)
    
    if num_data.get("status") != "success":
        if message.from_user.id != ADMIN_USER_ID and not has_active_membership(message.from_user.id):
            add_credits(message.from_user.id, 1)
        wrapped_send_message(message.chat.id, f"❌ {num_data.get('message', 'No data found for this number')}")
        show_menu(message.chat.id)
        return
        
    results = num_data.get("results", [])
    count = num_data.get("count", 0)
    
    summary_msg = f"📊 *Step 1: Number Search Results*\n\n"
    summary_msg += f"📱 *Mobile Number*: `{clean_num}`\n"
    summary_msg += f"📈 *Total Records*: `{count}`\n"
    wrapped_send_message(message.chat.id, summary_msg, parse_mode="Markdown")
    
    for idx, r in enumerate(results[:3], 1):
        record_msg = f"👤 *Record #{idx}*\n"
        record_msg += f"📝 *Name*: `{r.get('name', 'N/A')}`\n"
        record_msg += f"👨 *Father*: `{r.get('fname', 'N/A')}`\n"
        record_msg += f"🆔 *Aadhaar*: `{r.get('aadhaar_id', 'N/A')}`\n"
        record_msg += f"🏠 *Address*: `{r.get('address', 'N/A')}`\n"
        if r.get('alt') and r.get('alt') != 'N/A':
            record_msg += f"📞 *Alt Mobile*: `{r.get('alt')}`\n"
        if r.get('circle') and r.get('circle') != 'N/A':
            record_msg += f"📍 *Circle*: `{r.get('circle')}`\n"
        wrapped_send_message(message.chat.id, record_msg, parse_mode="Markdown")
        time.sleep(0.3)
        
    # Extract unique 12-digit Aadhaar numbers
    seen_aadhaars = set()
    aadhaar_list = []
    for r in results:
        adh_raw = r.get("aadhaar_id") or r.get("record_id")
        adh = sanitize_aadhaar_number(adh_raw)
        if adh and adh not in seen_aadhaars:
            seen_aadhaars.add(adh)
            aadhaar_list.append(adh)
            
    if not aadhaar_list:
        wrapped_send_message(message.chat.id, "ℹ️ Is number se koi linked Aadhaar nahi mila. Master search samapt.", parse_mode="Markdown")
        show_menu(message.chat.id)
        return
        
    # --- STEP 2: Aadhaar Details ---
    primary_aadhaar = aadhaar_list[0]
    wrapped_send_message(message.chat.id, f"⚡ *Step 2/3:* Linked Aadhaar `{primary_aadhaar}` details fetch kar rahe hain... ⏳", parse_mode="Markdown")
    time.sleep(0.5)
    
    adh_data = fetch_aadhaar_info_from_api(primary_aadhaar)
    if adh_data.get("status") == "success":
        adh_msg = f"📄 *Step 2: Aadhaar Details*\n\n"
        adh_msg += f"🆔 *Aadhaar No*  : `{adh_data.get('aadhaar', primary_aadhaar)}`\n"
        adh_msg += f"👨 *Name*        : `{adh_data.get('name', 'N/A')}`\n"
        adh_msg += f"👴 *Father Name* : `{adh_data.get('fname', 'N/A')}`\n"
        adh_msg += f"🏠 *Address*     : `{adh_data.get('address', 'N/A')}`\n"
        
        mobiles = adh_data.get('mobiles', [])
        if mobiles:
            adh_msg += f"\n📱 *Other Associated Mobiles:*\n"
            for m in mobiles[:3]:
                adh_msg += f"• `{m.get('mobile', 'N/A')}`"
                if m.get('circle') and m.get('circle') != 'N/A':
                    adh_msg += f" ({m.get('circle')})"
                adh_msg += "\n"
        wrapped_send_message(message.chat.id, adh_msg, parse_mode="Markdown")
    else:
        wrapped_send_message(message.chat.id, f"⚠️ Aadhaar details: {adh_data.get('message', 'No details found')}")
        
    time.sleep(0.5)
    
    # --- STEP 3: Family Details ---
    wrapped_send_message(message.chat.id, f"⚡ *Step 3/3:* Family details fetch kar rahe hain... ⏳", parse_mode="Markdown")
    fam_data = fetch_family_info_from_api(primary_aadhaar)
    
    if fam_data.get("status") == "success":
        fam_msg = f"👨‍👩‍👧‍👦 *Step 3: Family Information*\n\n"
        fam_msg += f"🏤 *State*       : `{fam_data.get('state', 'N/A')}`\n"
        fam_msg += f"🏙️ *District*    : `{fam_data.get('district', 'N/A')}`\n"
        fam_msg += f"🪪 *Ration Card* : `{fam_data.get('rcId', 'N/A')}`\n"
        fam_msg += f"🏪 *FPS ID*      : `{fam_data.get('fpsId', 'N/A')}`\n\n"
        
        members = fam_data.get('members', [])
        if members:
            fam_msg += f"👥 *Family Members ({len(members)})*\n\n"
            for i, member in enumerate(members, 1):
                fam_msg += f"━━━ *Member {i}* ━━━\n"
                fam_msg += f"👤 *Name*    : `{member.get('memberName', 'N/A')}`\n"
                fam_msg += f"🆔 *Aadhaar* : `{member.get('aadhaar', 'N/A')}`\n"
                head_status = "✅ Yes" if member.get('familyHead') == 'Yes' else "❌ No"
                fam_msg += f"👑 *Head*    : `{head_status}`\n\n"
        else:
            fam_msg += "`No members found`\n"
        wrapped_send_message(message.chat.id, fam_msg, parse_mode="Markdown")
    else:
        wrapped_send_message(message.chat.id, f"⚠️ Family details: {fam_data.get('message', 'No family records found')}")
        
    time.sleep(0.5)
    wrapped_send_message(message.chat.id, "✅ *Master Search Completed!* 🎉", parse_mode="Markdown")
    show_menu(message.chat.id)


def execute_master_search_from_aadhaar(message, clean_adh, pre_adh_data=None):
    """Execute deep OSINT starting from Aadhaar Number"""
    # --- STEP 1: Aadhaar Details ---
    wrapped_send_message(message.chat.id, f"⚡ *Step 1/3:* Aadhaar `{clean_adh}` details fetch kar rahe hain... ⏳", parse_mode="Markdown")
    adh_data = pre_adh_data if pre_adh_data else fetch_aadhaar_info_from_api(clean_adh)
    
    linked_mobiles = []
    if adh_data.get("status") == "success":
        adh_msg = f"📄 *Step 1: Aadhaar Details*\n\n"
        adh_msg += f"🆔 *Aadhaar No*  : `{adh_data.get('aadhaar', clean_adh)}`\n"
        adh_msg += f"👨 *Name*        : `{adh_data.get('name', 'N/A')}`\n"
        adh_msg += f"👴 *Father Name* : `{adh_data.get('fname', 'N/A')}`\n"
        adh_msg += f"🏠 *Address*     : `{adh_data.get('address', 'N/A')}`\n"
        
        mobiles = adh_data.get('mobiles', [])
        if mobiles:
            adh_msg += f"\n📱 *Linked Mobiles:*\n"
            for m in mobiles[:5]:
                m_num = m.get('mobile', 'N/A')
                if m_num != 'N/A':
                    clean_m = sanitize_mobile_number(m_num)
                    if clean_m and clean_m not in linked_mobiles:
                        linked_mobiles.append(clean_m)
                adh_msg += f"• `{m_num}`"
                if m.get('circle') and m.get('circle') != 'N/A':
                    adh_msg += f" ({m.get('circle')})"
                adh_msg += "\n"
        wrapped_send_message(message.chat.id, adh_msg, parse_mode="Markdown")
    else:
        wrapped_send_message(message.chat.id, f"⚠️ Aadhaar details: {adh_data.get('message', 'No details found')}")
        
    time.sleep(0.5)
    
    # --- STEP 2: Family Details ---
    wrapped_send_message(message.chat.id, f"⚡ *Step 2/3:* Family details fetch kar rahe hain... ⏳", parse_mode="Markdown")
    fam_data = fetch_family_info_from_api(clean_adh)
    
    if fam_data.get("status") == "success":
        fam_msg = f"👨‍👩‍👧‍👦 *Step 2: Family Information*\n\n"
        fam_msg += f"🏤 *State*       : `{fam_data.get('state', 'N/A')}`\n"
        fam_msg += f"🏙️ *District*    : `{fam_data.get('district', 'N/A')}`\n"
        fam_msg += f"🪪 *Ration Card* : `{fam_data.get('rcId', 'N/A')}`\n"
        fam_msg += f"🏪 *FPS ID*      : `{fam_data.get('fpsId', 'N/A')}`\n\n"
        
        members = fam_data.get('members', [])
        if members:
            fam_msg += f"👥 *Family Members ({len(members)})*\n\n"
            for i, member in enumerate(members, 1):
                fam_msg += f"━━━ *Member {i}* ━━━\n"
                fam_msg += f"👤 *Name*    : `{member.get('memberName', 'N/A')}`\n"
                fam_msg += f"🆔 *Aadhaar* : `{member.get('aadhaar', 'N/A')}`\n"
                head_status = "✅ Yes" if member.get('familyHead') == 'Yes' else "❌ No"
                fam_msg += f"👑 *Head*    : `{head_status}`\n\n"
        else:
            fam_msg += "`No members found`\n"
        wrapped_send_message(message.chat.id, fam_msg, parse_mode="Markdown")
    else:
        wrapped_send_message(message.chat.id, f"⚠️ Family details: {fam_data.get('message', 'No family records found')}")
        
    time.sleep(0.5)
    
    # --- STEP 3: Linked Mobile Number Search ---
    if linked_mobiles:
        prim_mob = linked_mobiles[0]
        wrapped_send_message(message.chat.id, f"⚡ *Step 3/3:* Linked Mobile `{prim_mob}` records fetch kar rahe hain... ⏳", parse_mode="Markdown")
        time.sleep(0.5)
        num_data = fetch_number_info_from_api(prim_mob)
        if num_data.get("status") == "success":
            results = num_data.get("results", [])
            count = num_data.get("count", 0)
            summary_msg = f"📱 *Step 3: Linked Mobile Results*\n\n"
            summary_msg += f"📞 *Mobile Number*: `{prim_mob}`\n"
            summary_msg += f"📈 *Total Records*: `{count}`\n"
            wrapped_send_message(message.chat.id, summary_msg, parse_mode="Markdown")
            
            for idx, r in enumerate(results[:2], 1):
                record_msg = f"👤 *Record #{idx}*\n"
                record_msg += f"📝 *Name*: `{r.get('name', 'N/A')}`\n"
                record_msg += f"👨 *Father*: `{r.get('fname', 'N/A')}`\n"
                record_msg += f"🏠 *Address*: `{r.get('address', 'N/A')}`\n"
                if r.get('alt') and r.get('alt') != 'N/A':
                    record_msg += f"📞 *Alt Mobile*: `{r.get('alt')}`\n"
                wrapped_send_message(message.chat.id, record_msg, parse_mode="Markdown")
                time.sleep(0.3)
        else:
            wrapped_send_message(message.chat.id, f"⚠️ Mobile `{prim_mob}` records: {num_data.get('message', 'No records found')}")
    else:
        wrapped_send_message(message.chat.id, "ℹ️ *Step 3/3:* Is Aadhaar se koi direct mobile number link nahi mila.", parse_mode="Markdown")
        
    time.sleep(0.5)
    wrapped_send_message(message.chat.id, "✅ *Master Search Completed!* 🎉", parse_mode="Markdown")
    show_menu(message.chat.id)


def ask_master_search(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    
    user_id = message.from_user.id
    
    # Restrict Master Search to VIP members, Admin, or Admin-approved users
    if not is_master_search_allowed(user_id):
        markup = types.InlineKeyboardMarkup(row_width=1)
        vip_btn = types.InlineKeyboardButton("💎 Buy VIP Plan", callback_data="show_vip_plans")
        markup.add(vip_btn)
        if DEVELOPER_LINK:
            dev_btn = types.InlineKeyboardButton("💬 Contact Admin / Developer", url=DEVELOPER_LINK)
            markup.add(dev_btn)
            
        wrapped_send_message(
            message.chat.id,
            "🔒 *VIP Feature Only!*\n\n"
            "⚡ *Master Deep Search* (Mobile ➔ Linked Aadhaar ➔ Family Members) sirf **VIP Members** ya **Admin Approved** users ke liye available hai!\n\n"
            "✨ *VIP Plan Ke Fayde:*\n"
            "• ⚡ Unlimited Master Deep Searches (No limits)\n"
            "• 📱 Mobile ya 🆔 Aadhaar dono se full chain search\n"
            "• 👨‍👩‍👧‍👦 Auto Complete Chain: Number ➔ Aadhaar ➔ Family\n"
            "• 🚀 Ultra Fast Server Priority\n\n"
            "👉 VIP Plan lene ke liye niche button dabayein ya Admin se activate karwayein.",
            parse_mode="Markdown",
            reply_markup=markup
        )
        return
    
    if user_id != ADMIN_USER_ID and not has_active_membership(user_id):
        if not can_search(user_id):
            show_no_credits_message(message.chat.id)
            return
    
    msg = wrapped_send_message(
        message.chat.id,
        "⚡ *Master Deep Search*\n\n"
        "Ek hi search me automatic complete records niklenge:\n"
        "• 📱 *Mobile Number* dalein ➔ Mobile + Linked Aadhaar + Family\n"
        "• 🆔 *Aadhaar Number* dalein ➔ Aadhaar + Family + Linked Mobiles\n\n"
        "👇 Apna *Mobile Number* ya *Aadhaar Number* enter karein:\n"
        "_(e.g. `9876543210`, `+91 98765 43210`, ya `1234 5678 9012`)_",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_master_search_input)

def process_master_search_input(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    if is_command_or_menu(message):
        return
        
    search_type, clean_val = classify_master_search_input(message.text)
    if not search_type:
        msg = wrapped_send_message(
            message.chat.id,
            "❌ *Invalid Input!*\n\n"
            "Kripya valid:\n"
            "• *10-digit Mobile Number* (e.g. `9876543210` ya `+91 98765 43210`)\n"
            "• ya *12-digit Aadhaar Number* (e.g. `1234 5678 9012` ya `123456789012`)\n\n"
            "enter karein:",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, process_master_search_input)
        return
        
    # Deduct credit (admin and VIP membership bypass)
    if message.from_user.id != ADMIN_USER_ID and not has_active_membership(message.from_user.id):
        if not deduct_credit(message.from_user.id):
            show_no_credits_message(message.chat.id)
            return
            
    save_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    
    if search_type == 'mobile':
        execute_master_search_from_mobile(message, clean_val)
    elif search_type == 'aadhaar':
        execute_master_search_from_aadhaar(message, clean_val)
    elif search_type == 'ambiguous_12':
        # Check Aadhaar first
        adh_data = fetch_aadhaar_info_from_api(clean_val)
        if adh_data.get("status") == "success":
            execute_master_search_from_aadhaar(message, clean_val, pre_adh_data=adh_data)
        else:
            # Fallback to Mobile
            mob = clean_val[2:]
            execute_master_search_from_mobile(message, mob)

def ask_number(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    
    # Admin bypasses credit check
    if message.from_user.id != ADMIN_USER_ID:
        if not can_search(message.from_user.id):
            show_no_credits_message(message.chat.id)
            return
    
    msg = wrapped_send_message(message.chat.id, "📱 Please enter a *10-digit* mobile number (e.g. `9876543210` ya `+91 98765 43210`):", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_number_input)

def process_number_input(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    
    if is_command_or_menu(message):
        return
        
    clean_num = sanitize_mobile_number(message.text)
    if not clean_num:
        msg = wrapped_send_message(message.chat.id, "❌ Invalid number! Please enter a valid *10-digit* mobile number (e.g. `9876543210` ya `+91 98765 43210`):", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_number_input)
        return
        
    query = clean_num
    
    # Deduct credit (admin bypasses)
    if message.from_user.id != ADMIN_USER_ID:
        if not deduct_credit(message.from_user.id):
            show_no_credits_message(message.chat.id)
            return
    
    save_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    wrapped_send_message(message.chat.id, "🔎 Fetching details... Please wait ⏳")
    
    data = fetch_number_info_from_api(query)
    
    if data.get("status") != "success":
        # Refund credit on failure
        if message.from_user.id != ADMIN_USER_ID:
            add_credits(message.from_user.id, 1)
        wrapped_send_message(message.chat.id, f"❌ {data.get('message', 'No data found for this number')}")
        show_menu(message.chat.id)
        return
    
    results = data.get("results", [])
    count = data.get("count", 0)
    
    # Show remaining credits
    user_credits_info = get_user_credits(message.from_user.id)
    credits_msg = ""
    if message.from_user.id != ADMIN_USER_ID:
        if has_active_membership(message.from_user.id):
            credits_msg = "💎 VIP — Unlimited"
        else:
            remaining = user_credits_info['credits'] if user_credits_info else 0
            credits_msg = f"💰 Credits: {remaining}"
    
    summary_msg = f"📊 *Search Summary*\n\n"
    summary_msg += f"📱 *Mobile Number*: `{query}`\n"
    summary_msg += f"📈 *Total Records*: `{count}`\n"
    summary_msg += f"📅 *Timestamp*: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
    if credits_msg:
        summary_msg += f"{credits_msg}\n"
    summary_msg += f"\n────────────────────\n\n"
    wrapped_send_message(message.chat.id, summary_msg, parse_mode="Markdown")
    
    found_aadhaars = set()
    for i, p in enumerate(results, 1):
        res_msg = (
            f"📲 *Record {i}/{count}*\n\n"
            f"📞 *Mobile No*     : `{p.get('mobile','N/A')}`\n"
            f"👨 *Name*          : `{p.get('name','N/A')}`\n"
            f"👴 *Father Name*   : `{p.get('fname','N/A')}`\n"
            f"🏠 *Address*       : `{format_address(p.get('address','N/A'))}`\n"
            f"🖄 *Aadhaar ID*    : `{p.get('aadhaar_id','N/A')}`\n"
            f"📱 *Alt Mobile*    : `{p.get('alt','N/A')}`\n"
            f"📍 *Circle*        : `{p.get('circle','N/A')}`\n"
            f"📧 *Email*         : `{p.get('email','N/A')}`\n"
        )
        aadhaar_id = p.get('aadhaar_id')
        if aadhaar_id and aadhaar_id != 'N/A':
            found_aadhaars.add(aadhaar_id)
        wrapped_send_message(message.chat.id, res_msg, parse_mode="Markdown")
    
    if found_aadhaars:
        tip_msg = f"💡 *Pro Tip:*\n\n"
        tip_msg += f"📄 *Aadhaar ID Found:* `{list(found_aadhaars)[0]}`\n\n"
        tip_msg += "You can use this Aadhaar ID to get more information:\n"
        tip_msg += "• 🆔 *Adhar Info* - Get more phone numbers\n"
        tip_msg += "• 👨‍👩‍👧‍👦 *Family Info* - Get family details\n\n"
        tip_msg += "Try it for comprehensive information! 🚀"
        wrapped_send_message(message.chat.id, tip_msg, parse_mode="Markdown")
    
    show_menu(message.chat.id)

def ask_aadhaar_number(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    if message.from_user.id != ADMIN_USER_ID:
        if not can_search(message.from_user.id):
            show_no_credits_message(message.chat.id)
            return
    msg = wrapped_send_message(message.chat.id, "🆔 Please enter Aadhaar number:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_aadhaar_info_input)

def process_aadhaar_info_input(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
        
    if is_command_or_menu(message):
        return
        
    clean_adh = sanitize_aadhaar_number(message.text)
    if not clean_adh:
        msg = wrapped_send_message(message.chat.id, "❌ Kripya valid *12-digit* Aadhaar number enter karein:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_aadhaar_info_input)
        return
        
    query = clean_adh
    
    if message.from_user.id != ADMIN_USER_ID:
        if not deduct_credit(message.from_user.id):
            show_no_credits_message(message.chat.id)
            return
    
    save_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    wrapped_send_message(message.chat.id, "🔎 Fetching Aadhaar details... Please wait ⏳")
    data = fetch_aadhaar_info_from_api(query)
    
    if data.get("status") != "success":
        if message.from_user.id != ADMIN_USER_ID:
            add_credits(message.from_user.id, 1)
        wrapped_send_message(message.chat.id, f"❌ {data.get('message', 'No data found')}")
        show_menu(message.chat.id)
        return
    
    msg = f"📄 *Aadhaar Information*\n\n"
    msg += f"🆔 *Aadhaar No*    : `{data.get('aadhaar', 'N/A')}`\n"
    msg += f"👨 *Name*          : `{data.get('name', 'N/A')}`\n"
    msg += f"👴 *Father Name*   : `{data.get('fname', 'N/A')}`\n"
    msg += f"🏠 *Address*       : `{data.get('address', 'N/A')}`\n\n"
    
    mobiles = data.get('mobiles', [])
    if mobiles:
        msg += f"📱 *Associated Mobile Numbers*\n\n"
        for idx, mobile_info in enumerate(mobiles, 1):
            msg += f"*Number {idx}:*\n"
            msg += f"📞 *Mobile No*     : `{mobile_info.get('mobile', 'N/A')}`\n"
            if mobile_info.get('alt') and mobile_info.get('alt') != 'N/A':
                msg += f"📱 *Alt Mobile*    : `{mobile_info.get('alt')}`\n"
            if mobile_info.get('circle') and mobile_info.get('circle') != 'N/A':
                msg += f"📍 *Circle*        : `{mobile_info.get('circle')}`\n"
            msg += f"────────────────────\n"
    else:
        msg += f"📱 *Associated Mobile Numbers*\n\n"
        msg += f"`No mobile numbers found`\n"
    
    wrapped_send_message(message.chat.id, msg, parse_mode="Markdown")
    show_menu(message.chat.id)

def ask_family_info(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    if message.from_user.id != ADMIN_USER_ID:
        if not can_search(message.from_user.id):
            show_no_credits_message(message.chat.id)
            return
    msg = wrapped_send_message(message.chat.id, "👨‍👩‍👧‍👦 Please enter Aadhaar number for family details:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_family_info_input)

def process_family_info_input(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    if is_command_or_menu(message):
        return
        
    query = message.text.replace(" ", "") if message.text else ""
    if not query:
        msg = wrapped_send_message(message.chat.id, "❌ Please enter a valid Aadhaar number:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_family_info_input)
        return
    
    if message.from_user.id != ADMIN_USER_ID:
        if not deduct_credit(message.from_user.id):
            show_no_credits_message(message.chat.id)
            return
    
    save_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    wrapped_send_message(message.chat.id, "👨‍👩‍👧‍👦 Fetching family details... Please wait ⏳")
    data = fetch_family_info_from_api(query)
    
    if data.get("status") != "success":
        if message.from_user.id != ADMIN_USER_ID:
            add_credits(message.from_user.id, 1)
        wrapped_send_message(message.chat.id, f"❌ {data.get('message', 'No family information found')}")
        show_menu(message.chat.id)
        return
    
    msg = f"👨‍👩‍👧‍👦 *Family Information*\n\n"
    
    msg += f"🏤 *State*        : `{data.get('state', 'N/A')}`\n"
    msg += f"🏙️ *District*     : `{data.get('district', 'N/A')}`\n"
    msg += f"🪪 *Ration Card*  : `{data.get('rcId', 'N/A')}`\n"
    msg += f"🏪 *FPS ID*       : `{data.get('fpsId', 'N/A')}`\n\n"
    
    members = data.get('members', [])
    if members:
        msg += f"👥 *Family Members ({len(members)})*\n\n"
        for i, member in enumerate(members, 1):
            msg += f"━━━ *Member {i}* ━━━\n"
            msg += f"👤 *Name*     : `{member.get('memberName', 'N/A')}`\n"
            msg += f"🆔 *Aadhaar*  : `{member.get('aadhaar', 'N/A')}`\n"
            head_status = "✅ Yes" if member.get('familyHead') == 'Yes' else "❌ No"
            msg += f"👑 *Head*     : `{head_status}`\n"
            msg += "\n"
    else:
        msg += f"👥 *Family Members*\n\n"
        msg += f"`No members found`\n"
    
    wrapped_send_message(message.chat.id, msg, parse_mode="Markdown")
    show_menu(message.chat.id)

def ask_vehicle_info(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    if message.from_user.id != ADMIN_USER_ID:
        if not can_search(message.from_user.id):
            show_no_credits_message(message.chat.id)
            return
    msg = wrapped_send_message(message.chat.id, "🚗 Please enter *Vehicle Registration Number* (e.g., `UK04AP2300`):", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_vehicle_info_input)

def process_vehicle_info_input(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    if is_command_or_menu(message):
        return
    
    query = message.text.replace(" ", "").upper() if message.text else ""
    
    if message.from_user.id != ADMIN_USER_ID:
        if not deduct_credit(message.from_user.id):
            show_no_credits_message(message.chat.id)
            return
    
    save_user(message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    wrapped_send_message(message.chat.id, "🚗 Fetching vehicle details... Please wait ⏳")
    data = fetch_vehicle_info_from_api(query)
    
    if data.get("status") != "success":
        if message.from_user.id != ADMIN_USER_ID:
            add_credits(message.from_user.id, 1)
        wrapped_send_message(message.chat.id, f"❌ {data.get('message', 'No vehicle information found')}")
        show_menu(message.chat.id)
        return
    
    vehicle_data = data.get("data", {})
    msg = f"🚗 *Vehicle Information*\n\n"
    msg += f"🔢 *Registration No* : `{vehicle_data.get('reg', query)}`\n"
    msg += f"📞 *Mobile Number*   : `{vehicle_data.get('num', 'N/A')}`\n"
    
    # Show extra fields if available from API
    extra_fields = [
        ("👤", "Owner Name", "owner_name"),
        ("🏭", "Manufacturer", "maker"),
        ("🚙", "Model", "maker_modal"),
        ("🏷️", "Vehicle Type", "vh_class"),
        ("⛽", "Fuel Type", "fuel_type"),
        ("🎨", "Color", "vehicle_color"),
        ("📅", "Reg. Date", "regn_dt"),
        ("🔩", "Chassis No", "chasi_no"),
        ("🔧", "Engine No", "engine_no"),
        ("🏢", "Insurance", "insurance_company"),
        ("📅", "Insurance Upto", "insurance_upto"),
        ("🏛️", "RTO", "rto"),
    ]
    for emoji, label, key in extra_fields:
        val = vehicle_data.get(key)
        if val and val != "N/A":
            msg += f"{emoji} *{label}* : `{val}`\n"
    
    wrapped_send_message(message.chat.id, msg, parse_mode="Markdown")
    show_menu(message.chat.id)

def chat_with_dev(message):
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    markup = types.InlineKeyboardMarkup()
    btn = types.InlineKeyboardButton("💬 Open Chat", url=DEVELOPER_LINK)
    markup.add(btn)
    wrapped_send_message(message.chat.id, "🔧 You can contact the developer here 👇", reply_markup=markup)

# ============================================================
#              BAN/UNBAN COMMANDS (PRESERVED)
# ============================================================

def ban_user_command(message):
    if message.from_user.id != ADMIN_USER_ID:
        bot.reply_to(message, "❌ Unauthorized.")
        return
    command_parts = message.text.split(' ', 2)
    if len(command_parts) < 2:
        bot.reply_to(message, "Usage: `/ban <user_id> [reason]`")
        return
    try:
        user_id = int(command_parts[1].strip())
    except:
        bot.reply_to(message, "Invalid ID.")
        return
    reason = command_parts[2] if len(command_parts) > 2 else "No reason provided"
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT username, first_name, last_name FROM users WHERE user_id = ?', (user_id,))
    user_info = c.fetchone()
    conn.close()
    
    username = user_info['username'] if user_info else None
    first_name = user_info['first_name'] if user_info else "Unknown"
    last_name = user_info['last_name'] if user_info else ""
    
    ban_user(user_id, username, first_name, last_name, message.from_user.id, reason)
    
    try:
        ban_notification = f"🚫 ACCOUNT BANNED\n\nReason: {reason}\nContact: @{ADMIN_USERNAME}"
        bot.send_message(user_id, ban_notification)
        user_notified = True
    except:
        user_notified = False
    
    bot.reply_to(message, f"✅ User {user_id} banned. Notified: {user_notified}")

def unban_user_command(message):
    if message.from_user.id != ADMIN_USER_ID:
        bot.reply_to(message, "❌ Unauthorized.")
        return
    command_parts = message.text.split(' ', 1)
    if len(command_parts) < 2:
        bot.reply_to(message, "Usage: `/unban <user_id>`")
        return
    try:
        user_id = int(command_parts[1].strip())
    except:
        bot.reply_to(message, "Invalid ID.")
        return
    
    if unban_user(user_id):
        try:
            bot.send_message(user_id, "✅ Your account has been unbanned!")
            user_notified = True
        except:
            user_notified = False
        bot.reply_to(message, f"✅ User {user_id} unbanned. Notified: {user_notified}")
    else:
        bot.reply_to(message, "User not found in ban list.")

def show_banned_users(message):
    if message.from_user.id != ADMIN_USER_ID:
        bot.reply_to(message, "❌ Unauthorized.")
        return
    banned_users = get_all_banned_users()
    if not banned_users:
        bot.reply_to(message, "No users are currently banned.")
        return
    
    msg = "📋 Banned Users List\n\n"
    for i, user in enumerate(banned_users, 1):
        msg += f"{i}. ID: {user['user_id']} | @{user['username'] if user['username'] else 'N/A'}\n"
    bot.reply_to(message, msg)

# ============================================================
#              CATCH ALL HANDLER
# ============================================================

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_id = message.from_user.id
    if rate_limiter.is_rate_limited(user_id, cooldown_seconds=0.8):
        return
    
    if check_ban_status(message.from_user.id, message.chat.id):
        return
    
    if not check_force_join(message.from_user.id):
        send_force_join_message(message.chat.id)
        return
    
    # Check if user directly sent a mobile number (handles +91, spaces, leading 0, etc.)
    clean_num = sanitize_mobile_number(message.text)
    if clean_num:
        message.text = clean_num
        process_number_input(message)
        return
    
    show_menu(message.chat.id)

# ============================================================
#              MAIN
# ============================================================

if __name__ == "__main__":
    print(f"🚀 {BOT_NAME} Bot is running...")
    print(f"📊 Admin ID: {ADMIN_USER_ID}")
    print(f"🔒 Force Join: {'ON' if is_setting_on('force_join_enabled') else 'OFF'}")
    print(f"💳 UPI: {'ON' if is_setting_on('upi_enabled') else 'OFF'}")
    print(f"💰 USDT: {'ON' if is_setting_on('usdt_enabled') else 'OFF'}")
    print(f"🤖 Auto Verify: {'ON' if is_setting_on('auto_verify') else 'OFF'}")
    print(f"🛒 Store: {'ON' if is_setting_on('buy_from_store_enabled') else 'OFF'}")
    print(f"🎟️ Redeem: {'ON' if is_setting_on('redeem_code_enabled') else 'OFF'}")
    
    try:
        bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"Webhook cleanup error: {e}")
    
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20, allowed_updates=telebot.util.update_types)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)
