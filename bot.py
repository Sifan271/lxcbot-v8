import discord
from discord.ext import commands
import asyncio
import subprocess
import json
from datetime import datetime, timedelta
import shlex
import logging
import shutil
import os
from typing import Optional, List, Dict, Any
import threading
import time
import sqlite3
import random
import requests
import string
import secrets
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
# ─── Container runtime auto-detection ────────────────────────────────────────
def detect_lxc_binary() -> str:
    for candidate in ("incus", "lxc", "lxc-fs"):
        if shutil.which(candidate):
            return candidate
    return "lxc"

def detect_lxd_binary() -> str:
    for candidate in ("incus", "lxd"):
        if shutil.which(candidate):
            return candidate
    return "lxd"

LXC_CMD: str = detect_lxc_binary()
LXD_CMD: str = detect_lxd_binary()

import logging as _logging_tmp
_logging_tmp.getLogger("root").info(f"[Runtime] Container CLI : '{LXC_CMD}'")
_logging_tmp.getLogger("root").info(f"[Runtime] Daemon    CLI : '{LXD_CMD}'")
del _logging_tmp
# ─────────────────────────────────────────────────────────────────────────────

# Load environment variables
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
BOT_NAME = os.getenv('BOT_NAME', 'NebulaNodes VMS')
PREFIX = os.getenv('PREFIX', '!')
YOUR_SERVER_IP = os.getenv('127.0.0.0')
MAIN_ADMIN_ID = int(os.getenv('MAIN_ADMIN_ID', '1139926248181747782'))
VPS_USER_ROLE_ID = int(os.getenv('VPS_USER_ROLE_ID', '1552180023203598393'))
DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', 'default')
HOST_MOTD = os.getenv('HOST_MOTD', '')
BOT_VERSION = os.getenv('BOT_VERSION', '8.0-PRO')
BOT_DEVELOPER = os.getenv('BOT_DEVELOPER', 'Sifan')
BOT_THUMBNAIL_URL = os.getenv('BOT_THUMBNAIL_URL', '')
BOT_ICON_URL = os.getenv('BOT_ICON_URL', '')

# VPS Expiration Settings
DEFAULT_VPS_EXPIRATION_DAYS = int(os.getenv('DEFAULT_VPS_EXPIRATION_DAYS', '30'))
EXPIRATION_WARNING_DAYS = int(os.getenv('EXPIRATION_WARNING_DAYS', '1'))

# !deploy command — members with DEPLOY_ROLE_ID get free VPS instantly
# Set these in your .env file:
#   DEPLOY_ROLE_ID=<discord_role_id>   (the member role that can use !deploy)
#   DEPLOY_RAM=16                       (GB of RAM per deployed VPS)
#   DEPLOY_CPU=3                        (CPU cores per deployed VPS)
#   DEPLOY_DISK=80                      (GB disk per deployed VPS)
#   VPS_DEPLOY_LIMIT=2                  (max VPS per user via !deploy — users cannot create more than this)
#   DEPLOY_SLOT=2                       (total global VPS slot cap across ALL users, 0 = unlimited)
DEPLOY_ROLE_ID = int(os.getenv('DEPLOY_ROLE_ID', '0'))
DEPLOY_RAM     = int(os.getenv('DEPLOY_RAM',  '0'))
DEPLOY_CPU     = int(os.getenv('DEPLOY_CPU',  '0'))
DEPLOY_DISK    = int(os.getenv('DEPLOY_DISK', '00'))
# VPS_DEPLOY_LIMIT = max VPS a single user can create via !deploy
# Supports both VPS_DEPLOY_LIMIT (new) and DEPLOY_LIMIT (old) — VPS_DEPLOY_LIMIT takes priority
DEPLOY_LIMIT   = int(os.getenv('VPS_DEPLOY_LIMIT', os.getenv('DEPLOY_LIMIT', '0')))
DEPLOY_SLOT    = int(os.getenv('DEPLOY_SLOT', '0'))

# SSH Configuration
SSH_FIX_SCRIPT = """#!/bin/bash
cat > /etc/ssh/sshd_config << 'SSHEOF'

Port 22
AddressFamily any
ListenAddress 0.0.0.0
ListenAddress ::
PasswordAuthentication yes
PubkeyAuthentication yes
PermitRootLogin yes
PermitEmptyPasswords no
ChallengeResponseAuthentication no
UsePAM yes
MaxAuthTries 6
MaxSessions 10
SyslogFacility AUTH
LogLevel INFO
X11Forwarding yes
X11DisplayOffset 10
PrintMotd no
PrintLastLog yes
TCPKeepAlive yes
PermitUserEnvironment no
Subsystem sftp /usr/lib/openssh/sftp-server
SSHEOF
systemctl restart ssh 2>/dev/null || service ssh restart 2>/dev/null || /etc/init.d/ssh restart 2>/dev/null || true
"""

# OS Options for VPS Creation and Reinstall
OS_OPTIONS = [
    {"label": "Ubuntu 20.04 LTS", "value": "images:ubuntu/20.04"},
    {"label": "Ubuntu 22.04 LTS", "value": "images:ubuntu/22.04"},
    {"label": "Ubuntu 24.04 LTS", "value": "images:ubuntu/24.04"},
    {"label": "Debian 10 (Buster)", "value": "images:debian/10"},
    {"label": "Debian 11 (Bullseye)", "value": "images:debian/11"},
    {"label": "Debian 12 (Bookworm)", "value": "images:debian/12"},
    {"label": "Debian 13 (Trixie)", "value": "images:debian/13"},
]

# Configure logging to file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(f'{BOT_NAME.lower()}_vps_bot')

# Database setup
def get_db():
    conn = sqlite3.connect('vps.db')
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY
    )''')
    cur.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (str(MAIN_ADMIN_ID),))
    cur.execute('''CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        location TEXT,
        total_vps INTEGER,
        tags TEXT DEFAULT '[]',
        api_key TEXT,
        url TEXT,
        is_local INTEGER DEFAULT 0
    )''')
    # Add local node if not exists
    cur.execute('SELECT COUNT(*) FROM nodes WHERE is_local = 1')
    if cur.fetchone()[0] == 0:
        cur.execute('INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('Local Node', 'Local', 100, '[]', None, None, 1))  # Default capacity 100
    cur.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        node_id INTEGER NOT NULL DEFAULT 1,
        container_name TEXT UNIQUE NOT NULL,
        ram TEXT NOT NULL,
        cpu TEXT NOT NULL,
        storage TEXT NOT NULL,
        config TEXT NOT NULL,
        os_version TEXT DEFAULT 'ubuntu:22.04',
        status TEXT DEFAULT 'stopped',
        suspended INTEGER DEFAULT 0,
        whitelisted INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        shared_with TEXT DEFAULT '[]',
        suspension_history TEXT DEFAULT '[]',
        expiration_date TEXT DEFAULT NULL,
        root_password TEXT DEFAULT NULL
    )''')
    # Migrations
    cur.execute('PRAGMA table_info(vps)')
    info = cur.fetchall()
    columns = [col[1] for col in info]
    if 'os_version' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN os_version TEXT DEFAULT 'ubuntu:22.04'")
    if 'node_id' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN node_id INTEGER DEFAULT 1")
    if 'expiration_date' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN expiration_date TEXT DEFAULT NULL")
    if 'root_password' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN root_password TEXT DEFAULT NULL")
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')
    settings_init = [
        ('cpu_threshold', '90'),
        ('ram_threshold', '90'),
    ]
    for key, value in settings_init:
        cur.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    cur.execute('''CREATE TABLE IF NOT EXISTS port_allocations (
        user_id TEXT PRIMARY KEY,
        allocated_ports INTEGER DEFAULT 0
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS port_forwards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        vps_container TEXT NOT NULL,
        vps_port INTEGER NOT NULL,
        host_port INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()

def get_setting(key: str, default: Any = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def get_nodes() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes')
    rows = cur.fetchall()
    conn.close()
    nodes = [dict(row) for row in rows]
    for node in nodes:
        node['tags'] = json.loads(node['tags'])
        # Ensure is_local is properly converted
        node['is_local'] = int(node.get('is_local', 1)) == 1
    return nodes

def get_node(node_id: int) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes WHERE id = ?', (node_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        node = dict(row)
        node['tags'] = json.loads(node['tags'])
        # Ensure is_local is properly converted
        node['is_local'] = int(node.get('is_local', 1)) == 1
        return node
    return None

def get_vps_by_id(vps_id: int) -> Optional[Dict]:
    """Get VPS info by global VPS ID"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps WHERE id = ?', (vps_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        vps = dict(row)
        vps['shared_with'] = json.loads(vps.get('shared_with', '[]'))
        vps['suspension_history'] = json.loads(vps.get('suspension_history', '[]'))
        vps['suspended'] = bool(vps.get('suspended', 0))
        vps['whitelisted'] = bool(vps.get('whitelisted', 0))
        vps['os_version'] = vps.get('os_version', 'ubuntu:22.04')
        return vps
    return None

def get_current_vps_count(node_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM vps WHERE node_id = ?', (node_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count

def get_vps_data() -> Dict[str, List[Dict[str, Any]]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps')
    rows = cur.fetchall()
    conn.close()
    data = {}
    for row in rows:
        user_id = row['user_id']
        if user_id not in data:
            data[user_id] = []
        vps = dict(row)
        vps['shared_with'] = json.loads(vps['shared_with'])
        vps['suspension_history'] = json.loads(vps['suspension_history'])
        vps['suspended'] = bool(vps['suspended'])
        vps['whitelisted'] = bool(vps['whitelisted'])
        vps['os_version'] = vps.get('os_version', 'ubuntu:22.04')
        data[user_id].append(vps)
    return data

def get_admins() -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM admins')
    rows = cur.fetchall()
    conn.close()
    return [row['user_id'] for row in rows]

def save_vps_data():
    """Persist in-memory vps_data to SQLite via safe UPSERT.
    Never deletes rows — deletion is handled explicitly by delete commands.
    This prevents data loss on restart/reconnect race conditions.
    """
    if not vps_data:
        return  # Nothing to save; don't wipe the DB
    conn = get_db()
    cur = conn.cursor()
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            shared_json = json.dumps(vps.get('shared_with', []))
            history_json = json.dumps(vps.get('suspension_history', []))
            suspended_int = 1 if vps.get('suspended') else 0
            whitelisted_int = 1 if vps.get('whitelisted', False) else 0
            os_ver = vps.get('os_version', 'ubuntu:22.04')
            created_at = vps.get('created_at', datetime.now().isoformat())
            node_id = vps.get('node_id', 1)
            expiration_date = vps.get('expiration_date', None)
            root_password = vps.get('root_password', None)
            status = vps.get('status', 'stopped')
            if 'id' not in vps or vps['id'] is None:
                cur.execute(
                    '''INSERT OR IGNORE INTO vps
                       (user_id, node_id, container_name, ram, cpu, storage, config,
                        os_version, status, suspended, whitelisted, created_at,
                        shared_with, suspension_history, expiration_date, root_password)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'],
                     vps['storage'], vps['config'], os_ver, status,
                     suspended_int, whitelisted_int, created_at,
                     shared_json, history_json, expiration_date, root_password))
                vps['id'] = cur.lastrowid
            else:
                cur.execute(
                    '''UPDATE vps SET user_id=?, node_id=?, container_name=?, ram=?, cpu=?,
                       storage=?, config=?, os_version=?, status=?, suspended=?,
                       whitelisted=?, shared_with=?, suspension_history=?,
                       expiration_date=?, root_password=?
                       WHERE id=?''',
                    (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'],
                     vps['storage'], vps['config'], os_ver, status,
                     suspended_int, whitelisted_int, shared_json, history_json,
                     expiration_date, root_password, vps['id']))
    conn.commit()
    conn.close()

def save_admin_data():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM admins')
    for admin_id in admin_data['admins']:
        cur.execute('INSERT INTO admins (user_id) VALUES (?)', (admin_id,))
    conn.commit()
    conn.close()

# Port forwarding functions
def get_user_allocation(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT allocated_ports FROM port_allocations WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def get_user_used_ports(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM port_forwards WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0]

def allocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO port_allocations (user_id, allocated_ports) VALUES (?, COALESCE((SELECT allocated_ports FROM port_allocations WHERE user_id = ?), 0) + ?)', (user_id, user_id, amount))
    conn.commit()
    conn.close()

def deallocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE port_allocations SET allocated_ports = GREATEST(0, allocated_ports - ?) WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def get_available_host_port(node_id: int) -> Optional[int]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT host_port FROM port_forwards WHERE vps_container IN (SELECT container_name FROM vps WHERE node_id = ?)', (node_id,))
    used_ports = set(row[0] for row in cur.fetchall())
    conn.close()
    for _ in range(100):
        port = random.randint(20000, 50000)
        if port not in used_ports:
            return port
    return None

async def create_port_forward(user_id: str, container: str, vps_port: int, node_id: int) -> Optional[int]:
    host_port = get_available_host_port(node_id)
    if not host_port:
        return None
    try:
        await execute_lxc(container, f"config device add {container} tcp_proxy_{host_port} proxy listen=tcp:0.0.0.0:{host_port} connect=tcp:127.0.0.1:{vps_port}", node_id=node_id)
        await execute_lxc(container, f"config device add {container} udp_proxy_{host_port} proxy listen=udp:0.0.0.0:{host_port} connect=udp:127.0.0.1:{vps_port}", node_id=node_id)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO port_forwards (user_id, vps_container, vps_port, host_port, created_at) VALUES (?, ?, ?, ?, ?)',
                    (user_id, container, vps_port, host_port, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return host_port
    except Exception as e:
        logger.error(f"Failed to create port forward: {e}")
        return None

async def remove_port_forward(forward_id: int, is_admin: bool = False) -> tuple[bool, Optional[str]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id, vps_container, host_port FROM port_forwards WHERE id = ?', (forward_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, None
    user_id, container, host_port = row
    node_id = find_node_id_for_container(container)
    try:
        await execute_lxc(container, f"config device remove {container} tcp_proxy_{host_port}", node_id=node_id)
        await execute_lxc(container, f"config device remove {container} udp_proxy_{host_port}", node_id=node_id)
        cur.execute('DELETE FROM port_forwards WHERE id = ?', (forward_id,))
        conn.commit()
        conn.close()
        return True, user_id
    except Exception as e:
        logger.error(f"Failed to remove port forward {forward_id}: {e}")
        conn.close()
        return False, None

def get_user_forwards(user_id: str) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM port_forwards WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]

async def recreate_port_forwards(container_name: str) -> int:
    node_id = find_node_id_for_container(container_name)
    readded_count = 0
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT vps_port, host_port FROM port_forwards WHERE vps_container = ?', (container_name,))
    rows = cur.fetchall()
    for row in rows:
        vps_port = row['vps_port']
        host_port = row['host_port']
        try:
            await execute_lxc(container_name, f"config device add {container_name} tcp_proxy_{host_port} proxy listen=tcp:0.0.0.0:{host_port} connect=tcp:127.0.0.1:{vps_port}", node_id=node_id)
            await execute_lxc(container_name, f"config device add {container_name} udp_proxy_{host_port} proxy listen=udp:0.0.0.0:{host_port} connect=udp:127.0.0.1:{vps_port}", node_id=node_id)
            logger.info(f"Re-added port forward {host_port}->{vps_port} for {container_name}")
            readded_count += 1
        except Exception as e:
            logger.error(f"Failed to re-add port forward {host_port}->{vps_port} for {container_name}: {e}")
    conn.close()
    return readded_count

def find_node_id_for_container(container_name: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT node_id FROM vps WHERE container_name = ?', (container_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 1  # Default to local

# Initialize database
init_db()

# Load data at startup
vps_data = get_vps_data()
admin_data = {'admins': get_admins()}

# Auto-save flag to prevent constant file writes
_auto_save_pending = False

def mark_for_save():
    """Mark data for auto-save"""
    global _auto_save_pending
    _auto_save_pending = True

async def auto_save_task():
    """Background task to auto-save VPS data periodically"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            global _auto_save_pending
            if _auto_save_pending:
                save_vps_data()
                _auto_save_pending = False
                logger.debug("Auto-saved VPS data")
        except Exception as e:
            logger.error(f"Error in auto-save task: {e}")
        await asyncio.sleep(2)  # Check every 2 seconds

async def rotating_presence_task():
    """Background task to update bot watching status with VPS stats"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.now()
            total = running = expired = 0
            for user_vps_list in vps_data.values():
                for vps in user_vps_list:
                    total += 1
                    if vps.get('status') == 'running' and not vps.get('suspended'):
                        running += 1
                    exp = vps.get('expiration_date')
                    if exp and datetime.fromisoformat(exp) < now:
                        expired += 1
            slot_cap = DEPLOY_SLOT if DEPLOY_SLOT > 0 else 100
            status_name = f"🖥️ {total}/{slot_cap} VPS | 🟢 {running} Running | 🔴 {expired} Expired"
            await bot.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name=status_name)
            )
        except Exception as e:
            logger.error(f"Error in presence task: {e}")
        await asyncio.sleep(30)

def save_vps_data_immediate():
    """Immediate save - call this for critical operations"""
    save_vps_data()
    mark_for_save()  # Ensure it's saved

# Global settings from DB
CPU_THRESHOLD = int(get_setting('cpu_threshold', 90))
RAM_THRESHOLD = int(get_setting('ram_threshold', 90))

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Resource monitoring settings (logging only)
resource_monitor_active = True

# ═══════════════════════════════════════════════════════════════════════════
# MODERN UI/UX SYSTEM - Beautiful Discord Embeds
# ═══════════════════════════════════════════════════════════════════════════

# Professional Color Palette
COLOR_PRIMARY = 0x2c3e50      # Dark slate blue
COLOR_SUCCESS = 0x27ae60      # Modern green  
COLOR_ERROR = 0xe74c3c        # Bright red
COLOR_WARNING = 0xf39c12      # Amber
COLOR_INFO = 0x3498db         # Ocean blue
COLOR_NETWORK = 0x16a085      # Teal
COLOR_EXPIRED = 0xc0392b      # Dark red
COLOR_ACTIVE = 0x16a085       # Teal green
COLOR_SUSPENDED = 0x95a5a6    # Gray
COLOR_NODE = 0x8e44ad         # Purple

# Helper function to truncate text
def truncate_text(text, max_length=1024):
    if not text:
        return text
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

# Password generation and management functions
def generate_strong_password(length=16):
    """Generate a cryptographically strong password"""
    # Use mix of uppercase, lowercase, digits, and special characters
    charset = string.ascii_letters + string.digits + "!@#$%^&*"
    password = ''.join(secrets.choice(charset) for _ in range(length))
    return password

def sanitize_username_for_container(username: str) -> str:
    """
    Sanitize username for LXC container naming.
    LXC only allows alphanumeric and hyphen characters.
    Replace underscores, spaces, and other invalid chars with hyphens.
    """
    # Replace underscores and spaces with hyphens
    sanitized = username.replace('_', '-').replace(' ', '-')
    # Remove any character that's not alphanumeric or hyphen
    sanitized = ''.join(c for c in sanitized if c.isalnum() or c == '-')
    # Ensure it doesn't start or end with hyphen (LXC requirement)
    sanitized = sanitized.strip('-').lower()
    # Limit length to avoid issues (LXC container names have limits)
    sanitized = sanitized[:30]
    return sanitized

def get_vps_password(container_name):
    """Get password from VPS data"""
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            if vps['container_name'] == container_name:
                return vps.get('root_password', None)
    return None

def set_vps_password(container_name, password):
    """Set password for VPS"""
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            if vps['container_name'] == container_name:
                vps['root_password'] = password
                save_vps_data_immediate()
                return True
    return False

async def configure_ssh(container_name, node_id, password):
    """Configure SSH on VPS and set root password"""
    try:
        # Install openssh-server first
        await execute_lxc(container_name,
            f"exec {container_name} -- bash -c "
            f"'apt-get update -qq -y && apt-get install -y openssh-server openssh-client && mkdir -p /var/run/sshd'",
            node_id=node_id, timeout=180)
        logger.info(f"OpenSSH installed on {container_name}")

        # Write SSH config
        ssh_config = (
            "Port 22\n"
            "AddressFamily any\n"
            "ListenAddress 0.0.0.0\n"
            "ListenAddress ::\n"
            "PasswordAuthentication yes\n"
            "PubkeyAuthentication yes\n"
            "PermitRootLogin yes\n"
            "PermitEmptyPasswords no\n"
            "ChallengeResponseAuthentication no\n"
            "UsePAM yes\n"
            "MaxAuthTries 6\n"
            "MaxSessions 10\n"
            "SyslogFacility AUTH\n"
            "LogLevel INFO\n"
            "X11Forwarding yes\n"
            "X11DisplayOffset 10\n"
            "PrintMotd no\n"
            "PrintLastLog yes\n"
            "TCPKeepAlive yes\n"
            "PermitUserEnvironment no\n"
            "Subsystem sftp /usr/lib/openssh/sftp-server\n"
        )
        await execute_lxc(container_name,
            f"exec {container_name} -- bash -c \"cat > /etc/ssh/sshd_config << 'SSHEOF'\n{ssh_config}SSHEOF\"",
            node_id=node_id)
        logger.info(f"SSH config written on {container_name}")

        # Set root password
        await execute_lxc(container_name,
            f"exec {container_name} -- bash -c \"echo 'root:{password}' | chpasswd\"",
            node_id=node_id)
        logger.info(f"Root password set for {container_name}")

        # Restart SSH
        restart_cmd = (
            "systemctl restart ssh 2>/dev/null || "
            "systemctl restart sshd 2>/dev/null || "
            "service ssh restart 2>/dev/null || "
            "/etc/init.d/ssh restart 2>/dev/null || true"
        )
        await execute_lxc(container_name,
            f"exec {container_name} -- bash -c '{restart_cmd}'",
            node_id=node_id)
        logger.info(f"SSH restarted on {container_name}")

        # Store password
        set_vps_password(container_name, password)
        return True, password
    except Exception as e:
        logger.error(f"Failed to configure SSH for {container_name}: {e}")
        return False, str(e)

def truncate_text(text, max_length=1024):
    if not text:
        return text
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

# Create professional embeds with modern styling
def create_embed(title, description="", color=COLOR_PRIMARY):
    """Create a beautiful, modern embed"""
    embed = discord.Embed(
        title=f"🌟 {title}",
        description=truncate_text(description, 4096),
        color=color
    )
    embed.set_thumbnail(url=BOT_THUMBNAIL_URL)
    embed.set_footer(
        text=f"NebulaNodes • v{BOT_VERSION} • {datetime.now().strftime('%H:%M:%S')}",
        icon_url=BOT_ICON_URL
    )
    embed.timestamp = datetime.now()
    return embed

def add_field(embed, name, value, inline=False):
    """Add a field with professional formatting"""
    embed.add_field(
        name=f"➤ {name}",
        value=truncate_text(value, 1024),
        inline=inline
    )
    return embed

def create_success_embed(title, description=""):
    """Create a success embed (green)"""
    return create_embed(title, description, COLOR_SUCCESS)

def create_error_embed(title, description=""):
    """Create an error embed (red)"""
    return create_embed(title, description, COLOR_ERROR)

def create_info_embed(title, description=""):
    """Create an info embed (blue)"""
    return create_embed(title, description, COLOR_INFO)

def create_warning_embed(title, description=""):
    """Create a warning embed (orange)"""
    return create_embed(title, description, COLOR_WARNING)

# Visual helper functions
def create_progress_bar(value, max_value=100, length=15):
    """Create a visual progress bar with emoji blocks"""
    if max_value == 0:
        percentage = 0
    else:
        percentage = int((value / max_value) * 100)
    filled = int((percentage / 100) * length)
    bar = "🟩" * filled + "⬜" * (length - filled)
    return f"{bar} `{percentage}%`"

def format_expiration(vps):
    """Format expiration date with visual badge"""
    if not vps.get('expiration_date'):
        return "🔵 No expiration"
    
    exp_dt = datetime.fromisoformat(vps['expiration_date'])
    days = (exp_dt - datetime.now()).days
    
    if days < 0:
        return f"🔴 **EXPIRED** (`{abs(days)}d ago`)"
    elif days <= EXPIRATION_WARNING_DAYS:
        return f"🟡 **EXPIRING** (`{days}d left`)"
    else:
        return f"🟢 **ACTIVE** (`{days}d left`)"

def create_vps_card(vps, index):
    """Create a formatted VPS information card"""
    node = get_node(vps.get('node_id', 1))
    status_emoji = "🟢" if (vps.get('status') == 'running' and not vps.get('suspended')) else "🟡" if vps.get('suspended') else "🔴"
    node_emoji = "📍" if (node and node.get('is_local')) else "🌐"
    
    card = (
        f"**#{index}** `{vps['container_name']}`\n"
        f"{status_emoji} {vps.get('status', 'unknown').upper()}"
    )
    if vps.get('suspended'):
        card += " (SUSPENDED)"
    
    card += (
        f"\n⚙️ **Config:** {vps.get('config', 'Custom')}\n"
        f"💾 **RAM:** {vps['ram']} | **CPU:** {vps['cpu']} | **Disk:** {vps['storage']}\n"
        f"{node_emoji} **Node:** {node['name'] if node else 'Unknown'}\n"
        f"⏰ **Expiration:** {format_expiration(vps)}"
    )
    return card

# Admin checks
def is_admin():
    async def predicate(ctx):
        user_id = str(ctx.author.id)
        if user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", []):
            return True
        raise commands.CheckFailure("You need admin permissions to use this command. Contact support.")
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) == str(MAIN_ADMIN_ID):
            return True
        raise commands.CheckFailure("Only the main admin can use this command.")
    return commands.check(predicate)

# LXC command execution with multi-node support
async def execute_lxc(container_name: str, command: str, timeout=120, node_id: Optional[int] = None):
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)

    if not node:
        raise Exception(f"Node {node_id} not found")

    full_command = f"{LXC_CMD} {command}"   # ← was: "lxc {command}"

    if node['is_local']:
        try:
            cmd = shlex.split(full_command)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise asyncio.TimeoutError(f"Command timed out after {timeout} seconds")

            if proc.returncode != 0:
                error = stderr.decode().strip() if stderr else "Command failed with no error output"
                raise Exception(f"Local LXC command failed: {error}\nCommand: {full_command}")
            return stdout.decode().strip() if stdout else True
        except asyncio.TimeoutError as te:
            logger.error(f"LXC command timed out: {full_command} - {str(te)}")
            raise
        except Exception as e:
            logger.error(f"LXC Error: {full_command} - {str(e)}")
            raise
    else:
        url = f"{node['url']}/api/execute"
        data = {"command": full_command}
        params = {"api_key": node["api_key"]}
        try:
            response = requests.post(url, json=data, params=params, timeout=timeout)
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}"
                try:
                    error_detail = response.json()
                    if 'detail' in error_detail:
                        error_msg = error_detail['detail']
                    elif 'error' in error_detail:
                        error_msg = error_detail['error']
                    elif 'stderr' in error_detail:
                        error_msg = error_detail['stderr']
                except:
                    pass
                raise Exception(f"Remote execution failed on {node['name']}: {error_msg}\nCommand: {full_command}")
            res = response.json()
            if res.get("returncode", 1) != 0:
                stderr = res.get("stderr", "Command failed")
                raise Exception(f"Remote LXC command failed on {node['name']}: {stderr}\nCommand: {full_command}")
            return res.get("stdout", True)
        except requests.exceptions.ConnectionError:
            raise Exception(f"Node {node['name']} is unreachable (network error).")
        except requests.exceptions.Timeout:
            raise Exception(f"Remote execution timed out on {node['name']} (timeout after {timeout}s)")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Remote execution failed on {node['name']}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error executing command on node {node['name']}: {str(e)}")
            raise

# Apply LXC config
async def apply_lxc_config(container_name: str, node_id: int):
    try:
        await execute_lxc(container_name, f"config set {container_name} security.nesting true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.privileged true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.syscalls.intercept.mknod true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.syscalls.intercept.setxattr true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} linux.kernel_modules overlay,loop,nf_nat,ip_tables,ip6_tables,br_netfilter", node_id=node_id)
        raw_lxc_config = (
            "lxc.apparmor.profile = unconfined\n"
            "lxc.apparmor.allow_nesting = 1\n"
            "lxc.apparmor.allow_incomplete = 1\n"
            "\n"
            "lxc.cap.drop =\n"
            "lxc.cgroup.devices.allow = a\n"
            "lxc.cgroup2.devices.allow = a\n"
            "\n"
            "lxc.mount.auto = proc:rw sys:rw cgroup:rw shmounts:rw\n"
        )
        node = get_node(node_id)
        if node and node['is_local']:
            proc = await asyncio.create_subprocess_exec(
                LXC_CMD, "config", "set", container_name, "raw.lxc", "-",  # ← was: "lxc"
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=raw_lxc_config.encode()), timeout=30
            )
            if proc.returncode != 0:
                raise Exception(f"Failed to set raw.lxc: {stderr.decode().strip()}")
        else:
            escaped = raw_lxc_config.replace("\n", "\\n")
            await execute_lxc(
                container_name,
                f"config set {container_name} raw.lxc {escaped!r}",
                node_id=node_id,
            )
        logger.info(f"LXC permissions applied to {container_name} on node {node_id}")
    except Exception as e:
        logger.error(f"Failed to apply LXC config to {container_name}: {e}")

# Apply internal permissions
async def apply_internal_permissions(container_name: str, node_id: int):
    try:
        await asyncio.sleep(5)
        commands = [
            "mkdir -p /etc/sysctl.d/",
            "echo 'net.ipv4.ip_unprivileged_port_start=0' > /etc/sysctl.d/99-custom.conf",
            "echo 'net.ipv4.ping_group_range=0 2147483647' >> /etc/sysctl.d/99-custom.conf",
            "echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.d/99-custom.conf",
            "echo 'kernel.unprivileged_userns_clone=1' >> /etc/sysctl.d/99-custom.conf",
            "sysctl -p /etc/sysctl.d/99-custom.conf || true"
        ]
        for cmd in commands:
            try:
                await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{cmd}\"", node_id=node_id)
            except Exception as cmd_error:
                logger.warning(f"Command failed in {container_name}: {cmd} - {cmd_error}")
        logger.info(f"Internal permissions applied to {container_name}")
    except Exception as e:
        logger.error(f"Failed to apply internal permissions to {container_name}: {e}")

# Get or create VPS role
async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID

    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        return None

    role_name = f"{BOT_NAME} VPS User"

    # Try cached role
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role and role < me.top_role:
            return role
        VPS_USER_ROLE_ID = None

    # Find by name
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        if role >= me.top_role:
            try:
                await role.delete(reason="Role above bot, recreating")
            except discord.Forbidden:
                return None
            role = None
        else:
            VPS_USER_ROLE_ID = role.id
            return role

    # Create safely below bot
    try:
        role = await guild.create_role(
            name=role_name,
            color=discord.Color.dark_purple(),
            permissions=discord.Permissions.none(),
            reason=f"{BOT_NAME} VPS User role"
        )
        await role.edit(position=me.top_role.position - 1)
        VPS_USER_ROLE_ID = role.id
        logger.info(f"Created VPS role: {role.id}")
        return role
    except Exception as e:
        logger.error(f"Failed to create VPS role: {e}")
        return None

# Host resource functions
def get_host_cpu_usage():
    """Get host CPU usage - cross-platform compatible"""
    try:
        import platform
        system = platform.system()
        
        if system == "Windows":
            # Windows: Use wmic or psutil as fallback
            try:
                import psutil
                return psutil.cpu_percent(interval=1)
            except ImportError:
                # Fallback for Windows without psutil
                try:
                    result = subprocess.run(['wmic', 'os', 'get', 'TotalVisibleMemorySize'], 
                                          capture_output=True, text=True, timeout=5)
                    return 0.0  # Default value on Windows
                except:
                    return 0.0
        else:
            # Linux/Unix: Use mpstat or top
            if shutil.which("mpstat"):
                result = subprocess.run(['mpstat', '1', '1'], capture_output=True, text=True, timeout=10)
                output = result.stdout
                for line in output.split('\n'):
                    if 'all' in line and '%' in line:
                        parts = line.split()
                        idle = float(parts[-1])
                        return 100.0 - idle
            else:
                result = subprocess.run(['top', '-bn1'], capture_output=True, text=True, timeout=10)
                output = result.stdout
                for line in output.split('\n'):
                    if '%Cpu(s):' in line:
                        # Parse CPU line - format: %Cpu(s): us,sy,ni,id,wa,hi,si,st
                        cpu_data = line.split('%Cpu(s):')[1].strip()
                        parts = []
                        for item in cpu_data.split(','):
                            val = item.split()[0].strip()
                            try:
                                parts.append(float(val))
                            except ValueError:
                                parts.append(0.0)
                        
                        if len(parts) >= 8:
                            us = parts[0]
                            sy = parts[1]
                            ni = parts[2]
                            id_ = parts[3]
                            wa = parts[4]
                            hi = parts[5]
                            si = parts[6]
                            st = parts[7]
                            usage = us + sy + ni + wa + hi + si + st
                            return usage
            return 0.0
    except Exception as e:
        logger.debug(f"Error getting CPU usage: {e}")
        return 0.0

def get_host_ram_usage():
    """Get host RAM usage - cross-platform compatible"""
    try:
        import platform
        system = platform.system()
        
        if system == "Windows":
            # Windows: Use psutil or wmic
            try:
                import psutil
                mem = psutil.virtual_memory()
                return mem.percent
            except ImportError:
                # Fallback for Windows without psutil
                try:
                    result = subprocess.run(['wmic', 'OS', 'get', 'TotalVisibleMemorySize,FreePhysicalMemory'], 
                                          capture_output=True, text=True, timeout=5)
                    lines = result.stdout.strip().split('\n')
                    if len(lines) > 1:
                        values = lines[1].split()
                        if len(values) >= 2:
                            total = int(values[0])
                            free = int(values[1])
                            used = total - free
                            return (used / total * 100) if total > 0 else 0.0
                except:
                    pass
                return 0.0
        else:
            # Linux/Unix: Use free command
            result = subprocess.run(['free', '-m'], capture_output=True, text=True, timeout=10)
            lines = result.stdout.splitlines()
            if len(lines) > 1:
                mem = lines[1].split()
                total = int(mem[1])
                used = int(mem[2])
                return (used / total * 100) if total > 0 else 0.0
            return 0.0
    except Exception as e:
        logger.debug(f"Error getting RAM usage: {e}")
        return 0.0

async def get_host_stats(node_id: int) -> Dict:
    node = get_node(node_id)
    if node['is_local']:
        return {
            "cpu": get_host_cpu_usage(),
            "ram": get_host_ram_usage(),
            "disk": get_host_disk_usage()
        }
    else:
        # Remote node - handle gracefully if unreachable
        url = f"{node['url']}/api/get_host_stats"
        params = {"api_key": node["api_key"]}
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            stats = response.json()
            # Fallbacks if remote API doesn't provide
            stats['disk'] = stats.get('disk', 'Unknown')
            return stats
        except requests.exceptions.ConnectionError:
            # Remote node unreachable - return graceful defaults
            logger.debug(f"Remote node {node['name']} unreachable - returning default stats")
            return {"cpu": 0.0, "ram": 0.0, "disk": "Unknown"}
        except Exception as e:
            logger.debug(f"Failed to get stats from remote node {node['name']}: {e}")
            return {"cpu": 0.0, "ram": 0.0, "disk": "Unknown"}

def check_vps_expiration():
    """Check and auto-suspend expired VPS"""
    global bot
    try:
        warned_users = set()
        
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps.get('expiration_date'):
                    expiration_dt = datetime.fromisoformat(vps['expiration_date'])
                    days_remaining = (expiration_dt - datetime.now()).days
                    hours_remaining = ((expiration_dt - datetime.now()).total_seconds() / 3600)
                    
                    container_name = vps['container_name']
                    node_id = vps.get('node_id', 1)
                    
                    # Auto-suspend if expired
                    if days_remaining < 0:
                        if not vps.get('suspended', False):
                            try:
                                # Suspend the VPS
                                asyncio.run(execute_lxc(container_name, f"stop {container_name}", node_id=node_id))
                                vps['status'] = 'stopped'
                                vps['suspended'] = True
                                vps['suspension_history'].append({
                                    'time': datetime.now().isoformat(),
                                    'reason': f'Auto-suspended due to VPS expiration on {expiration_dt.strftime("%Y-%m-%d")}',
                                    'by': 'Expiration Monitor'
                                })
                                save_vps_data_immediate()
                                logger.warning(f"VPS {container_name} auto-suspended due to expiration")
                                
                                # Notify owner
                                try:
                                    owner = asyncio.run(bot.fetch_user(int(user_id)))
                                    dm_embed = create_error_embed("🔴 VPS Expired and Suspended",
                                        f"Your VPS `{container_name}` has expired and been suspended.\n\n"
                                        f"**Expiration Date:** {expiration_dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                                        f"Contact an admin to renew your VPS.")
                                    asyncio.run(owner.send(embed=dm_embed))
                                except Exception as e:
                                    logger.debug(f"Failed to notify user {user_id}: {e}")
                            except Exception as e:
                                logger.error(f"Failed to auto-suspend VPS {container_name}: {e}")
                    
                    # Send warning if expiring soon
                    elif 0 < hours_remaining <= (EXPIRATION_WARNING_DAYS * 24):
                        if user_id not in warned_users:
                            try:
                                owner = asyncio.run(bot.fetch_user(int(user_id)))
                                dm_embed = create_warning_embed("⏰ VPS Expiring Soon",
                                    f"Your VPS `{container_name}` will expire in {days_remaining} day(s)!\n\n"
                                    f"**Expiration Date:** {expiration_dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                                    f"Contact an admin to renew your VPS before it's automatically suspended.")
                                asyncio.run(owner.send(embed=dm_embed))
                                warned_users.add(user_id)
                                logger.info(f"Sent expiration warning to user {user_id}")
                            except Exception as e:
                                logger.debug(f"Failed to notify user {user_id}: {e}")
    except Exception as e:
        logger.error(f"Error in VPS expiration check: {e}")

def resource_monitor():
    global resource_monitor_active
    last_expiration_check = time.time()
    expiration_check_interval = 3600  # Check every hour
    
    while resource_monitor_active:
        try:
            # Check VPS expiration every hour
            if time.time() - last_expiration_check > expiration_check_interval:
                check_vps_expiration()
                last_expiration_check = time.time()
            
            nodes = get_nodes()
            for no... (257 KB left)
