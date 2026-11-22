"""SQLite schema definitions. Legacy tables are retained for safe upgrades."""

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    referrer INTEGER,
    joined_at TEXT NOT NULL,
    paid_until TEXT,
    banned INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT,
    balance REAL NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    total_purchases INTEGER NOT NULL DEFAULT 0,
    daily_streak INTEGER NOT NULL DEFAULT 0,
    last_daily_at TEXT,
    vip_forever INTEGER NOT NULL DEFAULT 0,
    last_active TEXT,
    vip_reminded_at TEXT,
    expired_notice_at TEXT,
    vip_removed_at TEXT,
    discount_percent REAL NOT NULL DEFAULT 0,
    discount_expires_at TEXT,
    discount_reserved_order_id INTEGER
);
CREATE TABLE IF NOT EXISTS buttons (
    button_id TEXT PRIMARY KEY,
    title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS texts (
    text_key TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tariffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    price REAL NOT NULL CHECK(price >= 0),
    days INTEGER NOT NULL CHECK(days > 0),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS products_tariffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_tariff_id INTEGER UNIQUE,
    title TEXT NOT NULL,
    price REAL NOT NULL CHECK(price >= 0),
    days INTEGER NOT NULL CHECK(days > 0),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS payment_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    tariff_id INTEGER,
    tariff_title TEXT NOT NULL,
    amount REAL NOT NULL,
    days INTEGER NOT NULL,
    receipt_file_id TEXT,
    receipt_type TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by INTEGER,
    admin_comment TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    tariff_id INTEGER,
    tariff_title TEXT NOT NULL,
    amount REAL NOT NULL,
    original_amount REAL NOT NULL DEFAULT 0,
    days INTEGER NOT NULL,
    payment_method TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'created',
    receipt_file_id TEXT,
    receipt_type TEXT,
    crypto_invoice_id TEXT,
    invite_link TEXT,
    discount_percent REAL NOT NULL DEFAULT 0,
    referral_cashback_paid INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by INTEGER,
    admin_comment TEXT
);
CREATE TABLE IF NOT EXISTS promos (
    code TEXT PRIMARY KEY,
    days INTEGER NOT NULL CHECK(days > 0),
    uses_left INTEGER NOT NULL CHECK(uses_left >= 0),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_uses (
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    used_at TEXT NOT NULL,
    PRIMARY KEY(user_id, code)
);
CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    reward_type TEXT NOT NULL CHECK(reward_type IN ('balance','points','vip_days','discount')),
    value REAL NOT NULL CHECK(value > 0),
    max_uses INTEGER NOT NULL DEFAULT 0,
    uses_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    reward_type TEXT NOT NULL,
    value REAL NOT NULL,
    activated_at TEXT NOT NULL,
    UNIQUE(code, user_id)
);
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL UNIQUE,
    rewarded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    rewarded_at TEXT
);
CREATE TABLE IF NOT EXISTS daily_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS game_contents (
    game_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    file_id TEXT,
    file_type TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    answered_at TEXT
);
CREATE TABLE IF NOT EXISTS tickets_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    category TEXT NOT NULL DEFAULT 'support',
    message TEXT NOT NULL,
    file_id TEXT,
    file_type TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    answered_at TEXT,
    answered_by INTEGER
);
CREATE TABLE IF NOT EXISTS ad_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    format TEXT NOT NULL,
    desired_date TEXT NOT NULL,
    brief TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by INTEGER
);
CREATE TABLE IF NOT EXISTS quests_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    task_type TEXT NOT NULL DEFAULT 'manual',
    channel_id TEXT,
    required_count INTEGER NOT NULL DEFAULT 1,
    reward_points INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quest_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    completed_at TEXT NOT NULL,
    proof_text TEXT,
    proof_file_id TEXT,
    proof_file_type TEXT,
    reviewed_at TEXT,
    reviewed_by INTEGER,
    UNIQUE(task_id, user_id)
);
CREATE TABLE IF NOT EXISTS transactions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    transaction_type TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'RUB',
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raffles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    prize TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    channel_url TEXT NOT NULL DEFAULT '',
    ends_at TEXT NOT NULL,
    winners_count INTEGER NOT NULL DEFAULT 1 CHECK(winners_count > 0),
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS raffle_participants (
    raffle_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY(raffle_id,user_id)
);
CREATE TABLE IF NOT EXISTS raffle_winners (
    raffle_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    place INTEGER NOT NULL,
    selected_at TEXT NOT NULL,
    PRIMARY KEY(raffle_id,place),
    UNIQUE(raffle_id,user_id)
);
CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    prize TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sponsor_channels TEXT NOT NULL DEFAULT '[]',
    ends_at TEXT NOT NULL,
    winners_count INTEGER NOT NULL DEFAULT 1,
    min_referrals INTEGER NOT NULL DEFAULT 0,
    min_balance REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS giveaway_participants (
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY(giveaway_id,user_id)
);
CREATE TABLE IF NOT EXISTS giveaway_winners (
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    place INTEGER NOT NULL,
    selected_at TEXT NOT NULL,
    PRIMARY KEY(giveaway_id, place),
    UNIQUE(giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS giveaway_winner_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    place INTEGER NOT NULL,
    selected_at TEXT NOT NULL,
    UNIQUE(giveaway_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer);
CREATE INDEX IF NOT EXISTS idx_users_paid_until ON users(paid_until);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_ads_status ON ad_requests(status);
CREATE INDEX IF NOT EXISTS idx_giveaways_end ON giveaways(status, ends_at);
CREATE INDEX IF NOT EXISTS idx_quests_active ON quests_tasks(active);
"""
