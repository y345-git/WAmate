import base64
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus, cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config
from .database import DatabaseManager, parse_date_to_timestamp
from .utils import extract_document_text

logger = logging.getLogger(__name__)

_SESSION_COOKIE_NAME = "wamate_session"
_SESSION_TTL_SEC = 14 * 24 * 3600


def _clamp_int(val: str | None, default: int, min_val: int, max_val: int) -> int:
    try:
        parsed = int(val) if val is not None else default
    except (TypeError, ValueError):
        return default
    return max(min_val, min(max_val, parsed))


def _bool_param(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_date_param(val: str | None, end_of_day: bool = False) -> int:
    """Parse YYYY-MM-DD or unix epoch string into a Unix timestamp (or 0 if blank/invalid)."""
    return parse_date_to_timestamp(val, end_of_day=end_of_day, tz_name=Config.TZ)


_LOGOUT_BTN_HTML = (
    '<button id="logoutBtn" type="button" class="ghost-btn logout-btn" '
    'title="Sign out of this browser session">'
    '<svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
    '<polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>'
    '<span>Sign Out</span></button>'
)

_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Sign In — WAmate Second Brain</title>
<style>
  :root {
    --bg: #070d12;
    --panel: #0e171e;
    --card: #16232d;
    --border: rgba(255, 255, 255, 0.09);
    --text: #eef3f7;
    --muted: #7f929f;
    --accent: #00c896;
    --accent-dim: rgba(0, 200, 150, 0.14);
    --danger: #f45b69;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    background:
      radial-gradient(circle at 20% 20%, rgba(0, 200, 150, 0.09), transparent 45%),
      radial-gradient(circle at 80% 80%, rgba(56, 189, 248, 0.08), transparent 45%),
      var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Inter", sans-serif;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .login-card {
    width: 100%;
    max-width: 390px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 28px 26px 24px;
    box-shadow: 0 22px 54px rgba(0, 0, 0, 0.55);
  }
  .brand-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 18px;
  }
  .brand-mark {
    width: 40px;
    height: 40px;
    border-radius: 12px;
    background: linear-gradient(135deg, #00c896, #0ea5e9);
    display: flex;
    align-items: center;
    justify-content: center;
    color: #041d19;
    flex-shrink: 0;
  }
  .brand-mark svg { width: 22px; height: 22px; }
  .brand-title { font-size: 1.15rem; font-weight: 700; letter-spacing: -0.02em; }
  .brand-sub { font-size: 0.78rem; color: var(--muted); }
  .desc { font-size: 0.85rem; color: var(--muted); line-height: 1.48; margin: 0 0 18px; }
  label { display: block; font-size: 0.78rem; font-weight: 600; color: #c2cfd8; margin-bottom: 7px; }
  input[type="password"] {
    width: 100%;
    padding: 11px 13px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--text);
    font-size: 0.94rem;
    outline: none;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  input[type="password"]:focus {
    border-color: rgba(0, 200, 150, 0.65);
    box-shadow: 0 0 0 3px rgba(0, 200, 150, 0.2);
  }
  .submit-btn {
    width: 100%;
    margin-top: 14px;
    padding: 11px 16px;
    border: none;
    border-radius: 10px;
    background: linear-gradient(135deg, #00c896, #14b8a6);
    color: #03201b;
    font-size: 0.92rem;
    font-weight: 700;
    cursor: pointer;
    transition: opacity 0.15s ease, transform 0.1s ease;
  }
  .submit-btn:hover { opacity: 0.93; }
  .submit-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .err-box {
    display: none;
    margin-top: 12px;
    padding: 9px 11px;
    border-radius: 9px;
    font-size: 0.82rem;
    background: rgba(244, 91, 105, 0.14);
    border: 1px solid rgba(244, 91, 105, 0.36);
    color: #ff9aa3;
  }
</style>
</head>
<body>
  <form id="loginForm" class="login-card">
    <div class="brand-row">
      <div class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
          <path d="M12 8v4l2.5 1.5"/>
        </svg>
      </div>
      <div>
        <div class="brand-title">WAmate</div>
        <div class="brand-sub">Second Brain Explorer</div>
      </div>
    </div>
    <p class="desc">Enter your Web UI password to unlock local chat search, media vault, and AI assistant.</p>
    <label for="pwInput">Access Password</label>
    <input id="pwInput" type="password" autocomplete="current-password" placeholder="Enter WEB_UI_PASSWORD" required autofocus />
    <button id="loginBtn" type="submit" class="submit-btn">Unlock Explorer</button>
    <div id="loginErr" class="err-box"></div>
  </form>
<script>
  const form = document.getElementById('loginForm');
  const pwInput = document.getElementById('pwInput');
  const btn = document.getElementById('loginBtn');
  const errBox = document.getElementById('loginErr');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errBox.style.display = 'none';
    btn.disabled = true;
    btn.textContent = 'Signing in...';
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pwInput.value })
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Incorrect password');
      }
      window.location.href = '/';
    } catch (err) {
      errBox.textContent = err.message || 'Sign in failed';
      errBox.style.display = 'block';
      pwInput.focus();
      pwInput.select();
    } finally {
      btn.disabled = false;
      btn.textContent = 'Unlock Explorer';
    }
  });
</script>
</body>
</html>
"""


def _build_login_page() -> str:
    """Return the dark session-cookie login screen for password-protected Web UI."""
    return _LOGIN_HTML


_AI_TAB_BTN = (
    '<button class="tab-btn tab-ai" data-tab="ask">'
    '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 3l1.9 4.7L18.6 9.6l-4.7 1.9L12 16.2l-1.9-4.7L5.4 9.6l4.7-1.9L12 3z"/>'
    '<path d="M5 18l.8 2 .8.8-.8.8-.8 2-.8-2-.8-.8.8-.8.8-2z"/>'
    '<path d="M19 14l1 2.2 2.2 1-2.2 1-1 2.2-1-2.2-2.2-1 2.2-1 1-2.2z"/>'
    '</svg><span>Ask AI</span></button>'
)

_AI_PANEL_HTML = r"""
  <section id="tab-ask" class="panel panel-ask">
    <div class="ask-shell">
      <div class="ask-topbar">
        <div class="ask-topbar-left">
          <div class="ai-orb" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 3l1.9 4.7L18.6 9.6l-4.7 1.9L12 16.2l-1.9-4.7L5.4 9.6l4.7-1.9L12 3z"/>
              <path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z"/>
            </svg>
          </div>
          <div>
            <div class="ask-title-row">
              <span class="ask-title">WAmate AI Assistant</span>
              <span id="askModelBadge" class="ai-model-pill"><span class="live-dot"></span>Gemini Ready</span>
            </div>
            <div class="ask-subtitle">Multi-turn reasoning across chats, OCR receipts, documents, voice notes &amp; vault</div>
          </div>
        </div>
        <div class="ask-topbar-right">
          <div class="ask-scope-wrap" title="Optionally focus AI questions on a specific contact, group, or phone number">
            <svg class="ico-sm muted-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
            <input id="askChatFocus" list="chatSuggestList" type="search" placeholder="Scope to chat or phone (+91...)" />
            <button id="askFocusClearBtn" type="button" class="icon-clear" title="Clear chat scope" style="display:none">&times;</button>
          </div>
          <button id="askClearBtn" type="button" class="ghost-btn ask-new-btn" title="Clear thread and reset AI conversation memory">
            <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
            <span>New Chat</span>
          </button>
        </div>
      </div>

      <div id="askOutput" class="ask-stream"></div>

      <div class="ask-composer-dock">
        <div id="askFocusBanner" class="ask-focus-banner" style="display:none">
          <span class="muted">Context focus:</span>
          <span id="askFocusChip" class="focus-chip"></span>
        </div>
        <div id="askItemBanner" class="ask-focus-banner" style="display:none">
          <span class="muted">Attached item:</span>
          <span id="askItemChip" class="focus-chip item-chip"></span>
          <button id="askItemClearBtn" type="button" class="icon-clear" title="Detach item" style="position:static;margin-left:2px">&times;</button>
        </div>
        <div class="ask-composer">
          <textarea id="askInput" rows="1" placeholder="Ask anything about your WhatsApp messages, invoices, PDFs, voice notes, or deleted texts..."></textarea>
          <button id="askBtn" type="button" class="ai-send-btn" title="Ask WAmate (Enter)">
            <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>
            <span>Ask</span>
          </button>
        </div>
        <div class="ask-footer-meta">
          <span id="askStatus" class="ask-status-txt"><span class="live-dot"></span> Ready &middot; Multi-turn memory active</span>
          <span class="ask-hint-kbd"><kbd>Enter</kbd> send &middot; <kbd>Shift</kbd>+<kbd>Enter</kbd> newline</span>
        </div>
      </div>
    </div>
  </section>
"""

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>WAmate — Second Brain Explorer</title>
<style>
  :root {
    --bg: #070d12;
    --panel: #0e171e;
    --surface: #111c24;
    --card: #16232d;
    --card-hover: #1c2c38;
    --border: rgba(255, 255, 255, 0.08);
    --border-strong: rgba(255, 255, 255, 0.14);
    --text: #eef3f7;
    --text-soft: #c2cfd8;
    --muted: #7f929f;
    --accent: #00c896;
    --accent-bright: #19e0ad;
    --accent-dim: rgba(0, 200, 150, 0.14);
    --accent-ring: rgba(0, 200, 150, 0.24);
    --ai-blue: #38bdf8;
    --ai-grad: linear-gradient(135deg, #00c896 0%, #2dd4bf 50%, #38bdf8 100%);
    --danger: #f45b69;
    --warn: #f59e0b;
    --bubble-me: #0c4d41;
    --bubble-them: #16232d;
    --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.22);
    --shadow-lg: 0 12px 36px rgba(0, 0, 0, 0.42);
  }
  * {
    box-sizing: border-box;
    scrollbar-width: thin;
    scrollbar-color: rgba(127, 146, 159, 0.35) transparent;
  }
  ::-webkit-scrollbar { width: 7px; height: 7px; }
  ::-webkit-scrollbar-thumb { background: rgba(127, 146, 159, 0.32); border-radius: 999px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(127, 146, 159, 0.5); }
  ::-webkit-scrollbar-track { background: transparent; }

  body {
    margin: 0;
    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
    font-feature-settings: "cv02", "cv03", "cv04", "cv11";
    background:
      radial-gradient(ellipse 75% 45% at 12% -10%, rgba(0, 200, 150, 0.08), transparent 60%),
      radial-gradient(ellipse 60% 40% at 88% -8%, rgba(56, 189, 248, 0.06), transparent 60%),
      var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .ico { width: 16px; height: 16px; flex-shrink: 0; }
  .ico-sm { width: 14px; height: 14px; flex-shrink: 0; }
  .muted-ico { color: var(--muted); }

  /* Header */
  header {
    background: rgba(14, 23, 30, 0.88);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    padding: 10px 20px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    justify-content: space-between;
    z-index: 10;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 11px;
  }
  .brand-mark {
    width: 34px;
    height: 34px;
    border-radius: 10px;
    background: linear-gradient(135deg, rgba(0, 200, 150, 0.22), rgba(56, 189, 248, 0.18));
    border: 1px solid rgba(0, 200, 150, 0.38);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--accent-bright);
    box-shadow: 0 0 18px rgba(0, 200, 150, 0.16);
  }
  .brand-mark svg { width: 19px; height: 19px; }
  .brand-text { display: flex; align-items: baseline; gap: 9px; }
  .brand-name { font-weight: 700; font-size: 1.06rem; letter-spacing: -0.02em; }
  .brand-sub {
    color: var(--muted);
    font-weight: 500;
    font-size: 0.78rem;
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--border);
  }

  .stats-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    font-size: 0.75rem;
    font-variant-numeric: tabular-nums;
  }
  .stat-pill {
    background: rgba(22, 35, 45, 0.78);
    border: 1px solid var(--border);
    padding: 4px 10px;
    border-radius: 999px;
    color: var(--text-soft);
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .stat-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); opacity: 0.9; }
  .stat-dot.blue { background: var(--ai-blue); }
  .stat-dot.warn { background: var(--warn); }
  .stat-pill b { color: var(--text); font-weight: 600; }

  /* Navigation Tabs */
  nav.tabs {
    background: rgba(14, 23, 30, 0.76);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border);
    padding: 6px 20px;
    display: flex;
    gap: 6px;
    overflow-x: auto;
    flex-shrink: 0;
  }
  .tab-btn {
    background: transparent;
    border: 1px solid transparent;
    color: var(--muted);
    padding: 7px 13px;
    border-radius: 9px;
    font-size: 0.87rem;
    cursor: pointer;
    font-weight: 600;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    transition: color 0.15s ease, background 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .tab-btn:hover { color: var(--text); background: rgba(255, 255, 255, 0.04); }
  .tab-btn.active {
    color: var(--text);
    background: var(--card);
    border-color: var(--border-strong);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05), var(--shadow-sm);
  }
  .tab-btn.active .ico { color: var(--accent); }
  .tab-btn.tab-ai.active {
    background: linear-gradient(135deg, rgba(0, 200, 150, 0.16), rgba(56, 189, 248, 0.14));
    border-color: rgba(0, 200, 150, 0.38);
  }
  .tab-btn.tab-ai .ico { color: var(--ai-blue); }
  .tab-btn.tab-ai.active .ico { color: var(--accent-bright); }
  .tab-kbd {
    font-size: 0.68rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    color: var(--muted);
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--border);
    padding: 0 5px;
    border-radius: 4px;
    line-height: 1.45;
  }

  main {
    flex: 1;
    overflow: hidden;
    position: relative;
  }
  .panel {
    display: none;
    height: 100%;
    overflow-y: auto;
    padding: 18px 20px;
  }
  .panel.active { display: block; }
  .panel-ask.active { display: flex; flex-direction: column; padding: 0; overflow: hidden; }

  /* Controls & Toolbars */
  .toolbar {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 13px;
    padding: 10px 12px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
    align-items: center;
    box-shadow: var(--shadow-sm);
  }
  .toolbar-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
    font-size: 0.81rem;
    color: var(--muted);
    min-height: 22px;
  }
  .active-chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .filter-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 9px;
    border-radius: 999px;
    background: var(--accent-dim);
    border: 1px solid rgba(0, 200, 150, 0.3);
    color: #9bf3da;
    font-size: 0.75rem;
    font-weight: 500;
  }

  input[type="text"], input[type="search"], input[type="date"], select, textarea {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 11px;
    border-radius: 9px;
    font-size: 0.88rem;
    font-family: inherit;
    outline: none;
    color-scheme: dark;
    transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
  }
  input[type="date"] { padding: 7px 9px; font-size: 0.81rem; width: 132px; }
  input[type="text"]::placeholder, input[type="search"]::placeholder, textarea::placeholder {
    color: var(--muted);
  }
  input[type="text"]:focus, input[type="search"]:focus, input[type="date"]:focus, select:focus {
    border-color: var(--accent);
    background: #192834;
    box-shadow: 0 0 0 3px var(--accent-ring);
  }
  select { cursor: pointer; }
  .grow { flex: 1; min-width: 210px; }
  .chat-filter-box { min-width: 195px; flex: 0.72; max-width: 280px; }

  button.primary-btn {
    background: var(--accent);
    color: #052019;
    border: none;
    padding: 8px 16px;
    border-radius: 9px;
    font-weight: 700;
    cursor: pointer;
    font-size: 0.87rem;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: transform 0.12s ease, filter 0.15s ease, box-shadow 0.15s ease;
    box-shadow: 0 2px 10px rgba(0, 200, 150, 0.22);
  }
  button.primary-btn:hover { filter: brightness(1.08); }
  button.primary-btn:active { transform: scale(0.98); }

  button.ghost-btn, a.ghost-btn {
    background: var(--card);
    color: var(--text-soft);
    border: 1px solid var(--border);
    padding: 6px 11px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.8rem;
    font-weight: 500;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
  }
  button.ghost-btn:hover, a.ghost-btn:hover {
    background: var(--card-hover);
    border-color: var(--border-strong);
    color: var(--text);
  }

  button.link-pill {
    background: var(--accent-dim);
    color: #8cf2d5;
    border: 1px solid rgba(0, 200, 150, 0.28);
    padding: 2px 9px;
    border-radius: 999px;
    font-size: 0.75rem;
    cursor: pointer;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    transition: filter 0.15s ease, border-color 0.15s ease;
  }
  button.link-pill:hover { filter: brightness(1.16); border-color: rgba(0, 200, 150, 0.5); }

  .scope-pills {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 4px;
    background: var(--bg);
    padding: 3px;
    border-radius: 9px;
    border: 1px solid var(--border);
  }
  .scope-btn {
    background: transparent;
    color: var(--muted);
    border: none;
    padding: 5px 8px;
    border-radius: 6px;
    font-size: 0.76rem;
    font-weight: 600;
    cursor: pointer;
    text-align: center;
    transition: background 0.14s ease, color 0.14s ease;
  }
  .scope-btn:hover { color: var(--text); }
  .scope-btn.active {
    background: var(--card);
    color: var(--accent-bright);
    box-shadow: var(--shadow-sm);
  }

  .phone-tag {
    color: var(--muted);
    font-size: 0.73rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    letter-spacing: 0.01em;
  }

  .empty-state {
    background: linear-gradient(180deg, rgba(17, 28, 36, 0.9), rgba(14, 23, 30, 0.75));
    border: 1px dashed var(--border-strong);
    border-radius: 14px;
    padding: 34px 20px;
    text-align: center;
    color: var(--text-soft);
    font-size: 0.92rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
  }
  .empty-state svg { width: 28px; height: 28px; color: var(--muted); opacity: 0.85; }

  label.check {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-soft);
    font-size: 0.82rem;
    cursor: pointer;
    white-space: nowrap;
    padding: 5px 9px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid var(--border);
    user-select: none;
  }
  label.check input { accent-color: var(--accent); cursor: pointer; margin: 0; }

  /* Contact Avatar */
  .avatar {
    width: 32px;
    height: 32px;
    border-radius: 10px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 0.76rem;
    font-weight: 700;
    color: #fff;
    flex-shrink: 0;
    letter-spacing: 0.02em;
    border: 1px solid rgba(255, 255, 255, 0.14);
  }
  .avatar.sm { width: 26px; height: 26px; border-radius: 8px; font-size: 0.68rem; }

  /* Message & Vault Cards */
  .results-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-width: 1140px;
  }
  .msg-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 13px 15px;
    transition: border-color 0.15s ease, transform 0.12s ease, box-shadow 0.15s ease;
  }
  .msg-card:hover {
    border-color: var(--border-strong);
    box-shadow: var(--shadow-sm);
  }
  .msg-meta {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-bottom: 9px;
    font-size: 0.82rem;
    color: var(--muted);
  }
  .msg-meta-left {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 7px;
  }
  .msg-meta .who { color: var(--text); font-weight: 600; }
  .msg-ts { font-size: 0.76rem; font-variant-numeric: tabular-nums; color: var(--muted); }

  .badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 7px;
    border-radius: 6px;
    font-size: 0.7rem;
    font-weight: 600;
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text-soft);
    text-transform: capitalize;
  }
  .badge.deleted { background: rgba(244, 91, 105, 0.14); color: #ff949e; border-color: rgba(244, 91, 105, 0.34); }
  .badge.edited { background: rgba(245, 158, 11, 0.14); color: #fbbf24; border-color: rgba(245, 158, 11, 0.34); }
  .badge.saved { background: var(--accent-dim); color: #8ff4d8; border-color: rgba(0, 200, 150, 0.3); }
  .badge.ai { background: rgba(56, 189, 248, 0.13); color: #7dd3fc; border-color: rgba(56, 189, 248, 0.3); }

  .msg-body {
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.52;
    font-size: 0.92rem;
    color: var(--text);
  }
  .orig-box {
    margin-top: 9px;
    padding: 9px 12px;
    border-radius: 9px;
    background: rgba(255, 255, 255, 0.025);
    border: 1px solid var(--border);
    border-left: 3px solid var(--warn);
    font-size: 0.84rem;
    line-height: 1.48;
    white-space: pre-wrap;
  }
  .orig-box.cache { border-left-color: var(--ai-blue); }
  .orig-box.del { border-left-color: var(--danger); background: rgba(244, 91, 105, 0.04); }

  .media-bar {
    margin-top: 10px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 7px;
  }
  .thumb-img {
    max-width: 240px;
    max-height: 175px;
    border-radius: 10px;
    border: 1px solid var(--border-strong);
    cursor: pointer;
    display: block;
    margin-top: 9px;
    object-fit: cover;
    transition: transform 0.15s ease;
  }
  .thumb-img:hover { transform: scale(1.015); }

  /* Split Chats Layout */
  .chats-layout {
    display: grid;
    grid-template-columns: 330px 1fr;
    gap: 14px;
    height: calc(100vh - 134px);
  }
  .chat-sidebar {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 13px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: var(--shadow-sm);
  }
  .chat-sidebar-top {
    padding: 11px;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 8px;
    background: rgba(255, 255, 255, 0.015);
  }
  .chat-list { overflow-y: auto; flex: 1; }
  .chat-item {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    display: flex;
    align-items: flex-start;
    gap: 10px;
    transition: background 0.12s ease;
  }
  .chat-item:hover { background: rgba(255, 255, 255, 0.035); }
  .chat-item.active {
    background: rgba(0, 200, 150, 0.1);
    box-shadow: inset 3px 0 0 var(--accent);
  }
  .chat-item-main { flex: 1; min-width: 0; }
  .chat-item-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
    font-size: 0.88rem;
    font-weight: 600;
  }
  .chat-item-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chat-item-sub {
    color: var(--muted);
    font-size: 0.77rem;
    margin-top: 3px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .chat-view {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 13px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: var(--shadow-sm);
  }
  .chat-view-header {
    padding: 11px 15px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    background: rgba(255, 255, 255, 0.015);
  }
  .chat-view-who { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .chat-view-title { font-weight: 700; font-size: 0.96rem; }
  .chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 9px;
    background-image: radial-gradient(rgba(255, 255, 255, 0.03) 1px, transparent 1px);
    background-size: 18px 18px;
  }
  .bubble {
    max-width: 76%;
    padding: 9px 13px;
    border-radius: 12px 12px 12px 3px;
    background: var(--bubble-them);
    align-self: flex-start;
    border: 1px solid var(--border);
    box-shadow: var(--shadow-sm);
  }
  .bubble.me {
    background: var(--bubble-me);
    border-radius: 12px 12px 3px 12px;
    align-self: flex-end;
    border-color: rgba(0, 200, 150, 0.35);
  }
  .bubble-hdr {
    font-size: 0.74rem;
    color: #8beed3;
    margin-bottom: 4px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .bubble-ts {
    font-size: 0.69rem;
    color: rgba(226, 236, 243, 0.62);
    margin-top: 5px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  /* Media Grid */
  .media-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(232px, 1fr));
    gap: 13px;
  }
  .media-tile {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: transform 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .media-tile:hover {
    transform: translateY(-2px);
    border-color: rgba(0, 200, 150, 0.45);
    box-shadow: var(--shadow-sm);
  }
  .media-tile-preview {
    height: 156px;
    background: #0a1218;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    cursor: pointer;
    position: relative;
  }
  .media-tile-preview img, .media-tile-preview video {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform 0.25s ease;
  }
  .media-tile:hover .media-tile-preview img { transform: scale(1.03); }

  .doc-thumb-badge {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    color: var(--text-soft);
  }
  .doc-ext-pill {
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    padding: 4px 10px;
    border-radius: 7px;
    background: rgba(56, 189, 248, 0.14);
    border: 1px solid rgba(56, 189, 248, 0.34);
    color: #7dd3fc;
  }
  .doc-ext-pill.pdf { background: rgba(244, 91, 105, 0.16); border-color: rgba(244, 91, 105, 0.35); color: #ff949e; }
  .doc-ext-pill.sheet { background: rgba(0, 200, 150, 0.15); border-color: rgba(0, 200, 150, 0.35); color: #7ff0d1; }
  .doc-ext-pill.audio { background: rgba(168, 85, 247, 0.16); border-color: rgba(168, 85, 247, 0.35); color: #d8b4fe; }

  .video-thumb-box {
    position: relative;
    display: inline-block;
    max-width: 240px;
    margin-top: 8px;
    cursor: pointer;
    border-radius: 10px;
    overflow: hidden;
    border: 1px solid var(--border-strong);
    background: #0a1218;
  }
  .video-thumb-box img, .video-thumb-box video {
    display: block;
    max-width: 240px;
    max-height: 170px;
    min-width: 150px;
    min-height: 95px;
    width: 100%;
    object-fit: cover;
  }
  .video-thumb-box .media-tile-icon {
    width: 200px;
    height: 115px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .play-badge {
    position: absolute;
    inset: 0;
    margin: auto;
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: rgba(7, 13, 18, 0.66);
    backdrop-filter: blur(4px);
    border: 1.5px solid rgba(255, 255, 255, 0.85);
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.05rem;
    padding-left: 3px;
    pointer-events: none;
    transition: transform 0.15s ease, background 0.15s ease;
  }
  .media-tile-preview:hover .play-badge, .video-thumb-box:hover .play-badge {
    transform: scale(1.09);
    background: var(--accent);
    color: #052019;
    border-color: #fff;
  }
  .kind-chip {
    position: absolute;
    right: 8px;
    bottom: 8px;
    background: rgba(7, 13, 18, 0.78);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: #eef3f7;
    font-size: 0.65rem;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 5px;
    pointer-events: none;
    letter-spacing: 0.04em;
  }
  .media-tile-icon { font-size: 2rem; color: var(--muted); }
  .media-tile-info {
    padding: 11px 12px;
    font-size: 0.79rem;
    display: flex;
    flex-direction: column;
    gap: 5px;
    flex: 1;
  }
  .media-tile-title {
    font-weight: 600;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .muted { color: var(--muted); font-size: 0.83rem; }
  .load-more-wrap { margin: 16px 0; display: flex; justify-content: center; }

  /* Preview Dialog */
  dialog {
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    max-width: 860px;
    width: 92vw;
    max-height: 88vh;
    padding: 16px 18px;
    box-shadow: var(--shadow-lg);
  }
  dialog::backdrop {
    background: rgba(3, 8, 12, 0.76);
    backdrop-filter: blur(5px);
  }
  .modal-hdr {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    margin-bottom: 12px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 10px;
  }
  .modal-body { overflow-y: auto; max-height: 72vh; }
  .modal-body pre {
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 14px;
    border-radius: 10px;
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 0.84rem;
    line-height: 1.55;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }

  /* =========================================================
     Modern Ask AI Workspace
     ========================================================= */
  .ask-shell {
    height: 100%;
    display: flex;
    flex-direction: column;
    position: relative;
  }
  .ask-topbar {
    background: rgba(14, 23, 30, 0.78);
    border-bottom: 1px solid var(--border);
    padding: 10px 20px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-shrink: 0;
  }
  .ask-topbar-left { display: flex; align-items: center; gap: 11px; min-width: 0; }
  .ai-orb {
    width: 36px;
    height: 36px;
    border-radius: 11px;
    background: linear-gradient(135deg, rgba(0, 200, 150, 0.22), rgba(56, 189, 248, 0.24));
    border: 1px solid rgba(56, 189, 248, 0.35);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ai-blue);
    flex-shrink: 0;
    box-shadow: 0 0 20px rgba(56, 189, 248, 0.14);
  }
  .ai-orb svg { width: 18px; height: 18px; }
  .ask-title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .ask-title { font-weight: 700; font-size: 0.96rem; letter-spacing: -0.01em; }
  .ask-subtitle { font-size: 0.76rem; color: var(--muted); margin-top: 2px; }

  .ai-model-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 2px 9px;
    border-radius: 999px;
    background: rgba(56, 189, 248, 0.1);
    border: 1px solid rgba(56, 189, 248, 0.28);
    color: #9ce0fd;
    font-size: 0.72rem;
    font-weight: 600;
  }
  .live-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-bright);
    box-shadow: 0 0 8px var(--accent);
    display: inline-block;
  }

  .ask-topbar-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .ask-scope-wrap {
    position: relative;
    display: flex;
    align-items: center;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 0 8px 0 10px;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .ask-scope-wrap:focus-within {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-ring);
  }
  .ask-scope-wrap input {
    border: none;
    background: transparent;
    padding: 7px 6px;
    width: 220px;
    font-size: 0.82rem;
    box-shadow: none !important;
  }
  .icon-clear {
    border: none;
    background: transparent;
    color: var(--muted);
    font-size: 1rem;
    line-height: 1;
    cursor: pointer;
    padding: 2px 5px;
    border-radius: 4px;
  }
  .icon-clear:hover { color: var(--text); background: rgba(255, 255, 255, 0.07); }

  .ask-stream {
    flex: 1;
    overflow-y: auto;
    padding: 22px 20px 18px;
  }
  .ask-stream-inner {
    max-width: 860px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  /* AI Welcome View */
  .ask-welcome {
    max-width: 820px;
    margin: 18px auto 8px;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }
  .welcome-emblem {
    width: 56px;
    height: 56px;
    border-radius: 16px;
    background: linear-gradient(135deg, rgba(0, 200, 150, 0.2), rgba(56, 189, 248, 0.22));
    border: 1px solid rgba(56, 189, 248, 0.36);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--accent-bright);
    margin-bottom: 14px;
    box-shadow: 0 10px 35px rgba(0, 200, 150, 0.14);
  }
  .welcome-emblem svg { width: 28px; height: 28px; }
  .ask-welcome h2 {
    margin: 0 0 6px;
    font-size: 1.42rem;
    font-weight: 700;
    letter-spacing: -0.025em;
    background: linear-gradient(90deg, #ffffff 10%, #a5f3fc 90%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .ask-welcome p {
    margin: 0 0 16px;
    color: var(--muted);
    font-size: 0.9rem;
    max-width: 580px;
    line-height: 1.5;
  }
  .cap-strip {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 6px;
    margin-bottom: 22px;
  }
  .cap-pill {
    font-size: 0.74rem;
    color: var(--text-soft);
    background: rgba(255, 255, 255, 0.035);
    border: 1px solid var(--border);
    padding: 4px 10px;
    border-radius: 999px;
  }

  .ask-prompts {
    width: 100%;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    text-align: left;
  }
  .prompt-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 13px 14px;
    cursor: pointer;
    color: inherit;
    font-family: inherit;
    display: flex;
    flex-direction: column;
    gap: 6px;
    text-align: left;
    transition: border-color 0.16s ease, transform 0.14s ease, background 0.16s ease, box-shadow 0.16s ease;
  }
  .prompt-card:hover {
    background: var(--card);
    border-color: rgba(0, 200, 150, 0.42);
    transform: translateY(-1px);
    box-shadow: var(--shadow-sm);
  }
  .prompt-card-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 0.74rem;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }
  .prompt-card-txt {
    font-size: 0.86rem;
    color: var(--text-soft);
    line-height: 1.42;
  }

  /* AI Conversation Bubbles */
  .ai-turn-user {
    align-self: flex-end;
    max-width: 82%;
    background: linear-gradient(135deg, rgba(0, 200, 150, 0.18), rgba(0, 168, 132, 0.12));
    border: 1px solid rgba(0, 200, 150, 0.34);
    border-radius: 14px 14px 4px 14px;
    padding: 11px 15px;
    box-shadow: var(--shadow-sm);
  }
  .ai-turn-user-meta {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 7px;
    font-size: 0.71rem;
    color: #9bf3da;
    margin-bottom: 4px;
  }
  .ai-turn-user-text {
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 0.93rem;
    line-height: 1.5;
    color: var(--text);
  }

  .ai-turn-bot {
    align-self: stretch;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px 17px;
    box-shadow: var(--shadow-sm);
  }
  .ai-turn-bot.error { border-color: rgba(244, 91, 105, 0.42); background: rgba(244, 91, 105, 0.05); }
  .ai-bot-hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding-bottom: 10px;
    margin-bottom: 11px;
    border-bottom: 1px solid var(--border);
  }
  .ai-bot-who {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.82rem;
    font-weight: 700;
    color: var(--text);
  }
  .ai-mini-badge {
    width: 24px;
    height: 24px;
    border-radius: 7px;
    background: var(--ai-grad);
    color: #04201b;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .ai-mini-badge svg { width: 14px; height: 14px; }
  .ai-latency {
    font-size: 0.71rem;
    font-weight: 500;
    color: var(--muted);
    background: rgba(255, 255, 255, 0.04);
    padding: 2px 7px;
    border-radius: 999px;
    font-variant-numeric: tabular-nums;
  }

  /* Rich AI Markdown Typography */
  .ai-markdown { font-size: 0.93rem; line-height: 1.64; color: var(--text); }
  .ai-markdown p { margin: 0 0 10px; }
  .ai-markdown p:last-child { margin-bottom: 0; }
  .ai-markdown h3, .ai-markdown h4 {
    margin: 14px 0 7px;
    color: #fff;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .ai-markdown h3:first-child, .ai-markdown h4:first-child { margin-top: 0; }
  .ai-markdown ul, .ai-markdown ol {
    margin: 6px 0 12px;
    padding-left: 20px;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .ai-markdown li::marker { color: var(--accent); font-weight: 700; }
  .ai-markdown strong { color: #ffffff; font-weight: 700; }
  .ai-markdown em { color: #cbd5e1; font-style: italic; }
  .ai-markdown code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.83em;
    background: rgba(255, 255, 255, 0.07);
    border: 1px solid var(--border);
    padding: 1px 6px;
    border-radius: 5px;
    color: #93e8f8;
  }
  .ai-code-wrap {
    margin: 10px 0;
    background: #080f15;
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    overflow: hidden;
  }
  .ai-code-lang {
    padding: 4px 10px;
    font-size: 0.7rem;
    text-transform: uppercase;
    color: var(--muted);
    background: rgba(255, 255, 255, 0.03);
    border-bottom: 1px solid var(--border);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  .ai-code-wrap pre {
    margin: 0;
    padding: 11px 13px;
    overflow-x: auto;
    font-size: 0.83rem;
    line-height: 1.48;
  }
  .ai-code-wrap pre code { background: none; border: none; padding: 0; color: #dbeafe; }
  .ai-callout {
    margin: 8px 0;
    padding: 9px 12px;
    border-radius: 9px;
    background: rgba(245, 158, 11, 0.09);
    border: 1px solid rgba(245, 158, 11, 0.28);
    border-left: 3px solid var(--warn);
    font-size: 0.88rem;
  }

  /* AI Thinking Animation */
  .ai-thinking {
    display: flex;
    align-items: center;
    gap: 12px;
    background: var(--surface);
    border: 1px solid rgba(56, 189, 248, 0.28);
    border-radius: 14px;
    padding: 13px 16px;
  }
  .typing-dots { display: inline-flex; gap: 4px; align-items: center; }
  .typing-dots span {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--ai-blue);
    animation: pulseDot 1.2s infinite ease-in-out;
  }
  .typing-dots span:nth-child(2) { animation-delay: 0.18s; background: var(--accent); }
  .typing-dots span:nth-child(3) { animation-delay: 0.36s; }
  @keyframes pulseDot {
    0%, 80%, 100% { transform: scale(0.65); opacity: 0.35; }
    40% { transform: scale(1.1); opacity: 1; }
  }

  /* Composer Dock */
  .ask-composer-dock {
    padding: 8px 20px 14px;
    background: linear-gradient(180deg, rgba(7, 13, 18, 0) 0%, rgba(7, 13, 18, 0.94) 24%, var(--bg) 100%);
    flex-shrink: 0;
  }
  .ask-composer-dock > * {
    max-width: 860px;
    margin-left: auto;
    margin-right: auto;
  }
  .ask-focus-banner {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-bottom: 7px;
    font-size: 0.77rem;
  }
  .focus-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 2px 10px;
    border-radius: 999px;
    background: var(--accent-dim);
    border: 1px solid rgba(0, 200, 150, 0.36);
    color: #9bf3da;
    font-weight: 600;
  }
  .ask-composer {
    display: flex;
    align-items: flex-end;
    gap: 10px;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 15px;
    padding: 8px 9px 8px 14px;
    box-shadow: var(--shadow-lg);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .ask-composer:focus-within {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-ring), var(--shadow-lg);
  }
  .ask-composer textarea {
    flex: 1;
    border: none;
    background: transparent;
    resize: none;
    padding: 6px 0;
    min-height: 24px;
    max-height: 150px;
    line-height: 1.45;
    font-size: 0.94rem;
    box-shadow: none !important;
  }
  .ai-send-btn {
    background: var(--ai-grad);
    color: #041d18;
    border: none;
    border-radius: 11px;
    padding: 9px 16px;
    font-weight: 700;
    font-size: 0.87rem;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
    transition: transform 0.12s ease, filter 0.15s ease, opacity 0.15s ease;
  }
  .ai-send-btn:hover { filter: brightness(1.08); }
  .ai-send-btn:active { transform: scale(0.97); }
  .ai-send-btn:disabled { opacity: 0.55; cursor: not-allowed; }

  .ask-footer-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 7px;
    padding: 0 4px;
    font-size: 0.74rem;
    color: var(--muted);
  }
  .ask-status-txt { display: inline-flex; align-items: center; gap: 6px; }
  .ask-hint-kbd kbd {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 5px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.7rem;
  }

  .toast {
    position: fixed;
    right: 20px;
    bottom: 20px;
    background: var(--card);
    color: var(--text);
    border: 1px solid rgba(0, 200, 150, 0.45);
    padding: 8px 14px;
    border-radius: 10px;
    font-size: 0.82rem;
    font-weight: 600;
    box-shadow: var(--shadow-lg);
    opacity: 0;
    transform: translateY(8px);
    pointer-events: none;
    transition: opacity 0.18s ease, transform 0.18s ease;
    z-index: 60;
  }
  .header-right {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .logout-btn {
    padding: 5px 10px;
    font-size: 0.77rem;
    border-radius: 9px;
    color: var(--text-soft);
  }
  .logout-btn:hover { color: #ff949e; border-color: rgba(244, 91, 105, 0.42); }

  .date-range-group {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }
  .date-sep { color: var(--muted); font-size: 0.78rem; }
  .preset-group {
    display: inline-flex;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 2px;
    gap: 2px;
  }
  .preset-btn {
    background: transparent;
    color: var(--muted);
    border: none;
    padding: 4px 8px;
    font-size: 0.74rem;
    font-weight: 600;
    border-radius: 7px;
    cursor: pointer;
    transition: background 0.14s ease, color 0.14s ease;
  }
  .preset-btn:hover { color: var(--text); }
  .preset-btn.active {
    background: var(--accent-dim);
    color: var(--accent-bright);
  }

  .msg-action-bar {
    margin-top: 9px;
    padding-top: 8px;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .bubble-actions {
    margin-top: 6px;
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    opacity: 0.85;
  }
  .bubble:hover .bubble-actions { opacity: 1; }
  .action-pill {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--border);
    color: var(--text-soft);
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 0.73rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.14s ease, border-color 0.14s ease, color 0.14s ease;
  }
  .action-pill svg { width: 12px; height: 12px; flex-shrink: 0; }
  .action-pill:hover { background: var(--card-hover); color: var(--text); border-color: var(--border-strong); }
  .action-pill.ai-pill:hover {
    background: rgba(56, 189, 248, 0.14);
    border-color: rgba(56, 189, 248, 0.42);
    color: #93e8f8;
  }
  .action-pill.wa-pill:hover {
    background: var(--accent-dim);
    border-color: rgba(0, 200, 150, 0.44);
    color: #9bf3da;
  }
  .action-pill:disabled { opacity: 0.55; cursor: not-allowed; }
  .focus-chip.item-chip {
    background: rgba(56, 189, 248, 0.14);
    border-color: rgba(56, 189, 248, 0.34);
    color: #bae6fd;
  }

  .toast.show { opacity: 1; transform: translateY(0); }

  @media (max-width: 820px) {
    header { padding: 10px 14px; }
    nav.tabs { padding: 6px 12px; }
    .panel { padding: 12px; }
    .chats-layout { grid-template-columns: 1fr; height: auto; }
    .chat-sidebar { max-height: 280px; }
    .chat-view { min-height: 430px; }
    .grow, .chat-filter-box { min-width: 100%; max-width: none; }
    input[type="date"] { width: 122px; }
    .ask-topbar { padding: 10px 14px; }
    .ask-scope-wrap input { width: 170px; }
    .ask-stream { padding: 14px 12px; }
    .ask-composer-dock { padding: 8px 12px 12px; }
    .ask-prompts { grid-template-columns: 1fr; }
    .ai-turn-user { max-width: 94%; }
  }

  /* Broadcast & Campaigns Styles */
  .bcast-sub-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 18px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  .bcast-sub-btn {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 7px 14px;
    border-radius: 9999px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--border);
    color: var(--muted);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .bcast-sub-btn:hover {
    color: var(--text);
    background: rgba(255, 255, 255, 0.07);
    border-color: rgba(255, 255, 255, 0.14);
  }
  .bcast-sub-btn.active {
    background: var(--accent-dim);
    color: var(--accent);
    border-color: var(--accent);
  }
  .bcast-sub-panel {
    display: none;
    animation: fadeIn 0.18s ease-in-out;
  }
  .bcast-sub-panel.active {
    display: block;
  }
  .bcast-form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 18px;
  }
  .bcast-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .bcast-card.full-card {
    grid-column: 1 / -1;
  }
  .bcast-card-hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .bcast-title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    color: var(--text);
  }
  .bcast-field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .bcast-label {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--muted);
  }
  .bcast-label-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .bcast-role-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
  }
  .bcast-input, .bcast-textarea, .bcast-select {
    width: 100%;
    background: #091219;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
    color: var(--text);
    font-size: 13px;
    outline: none;
    box-sizing: border-box;
    transition: border-color 0.15s ease;
  }
  .bcast-input:focus, .bcast-textarea:focus, .bcast-select:focus {
    border-color: var(--accent);
  }
  .bcast-textarea {
    resize: vertical;
    font-family: inherit;
    line-height: 1.45;
  }
  .bcast-toggle-group {
    display: flex;
    gap: 8px;
    background: #091219;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 3px;
  }
  .bcast-toggle-opt {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 500;
    color: var(--muted);
    cursor: pointer;
    transition: all 0.15s ease;
    user-select: none;
  }
  .bcast-toggle-opt input {
    display: none;
  }
  .bcast-toggle-opt:has(input:checked) {
    background: var(--accent-dim);
    color: var(--accent);
  }
  .bcast-role-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    min-height: 28px;
  }
  .role-pill-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 9999px;
    font-size: 12px;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.03);
    color: var(--muted);
    cursor: pointer;
    transition: all 0.15s ease;
    user-select: none;
  }
  .role-pill-btn:hover {
    color: var(--text);
    border-color: rgba(255, 255, 255, 0.2);
  }
  .role-pill-btn.selected {
    border-color: var(--pill-color, var(--accent));
    background: color-mix(in srgb, var(--pill-color, var(--accent)) 18%, transparent);
    color: var(--text);
    font-weight: 600;
  }
  .role-pill-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background-color: var(--pill-color, #00c896);
  }
  .bcast-ac-wrapper {
    position: relative;
  }
  .bcast-ac-dropdown {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    background: #0f1c25;
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
    max-height: 220px;
    overflow-y: auto;
    z-index: 80;
  }
  .bcast-ac-item {
    padding: 8px 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 13px;
    cursor: pointer;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }
  .bcast-ac-item:hover {
    background: rgba(255, 255, 255, 0.07);
    color: var(--accent);
  }
  .bcast-chips-container {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 4px;
  }
  .bcast-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.3);
    color: #bae6fd;
    padding: 3px 8px;
    border-radius: 6px;
    font-size: 12px;
  }
  .bcast-chip-del {
    background: transparent;
    border: none;
    color: #bae6fd;
    cursor: pointer;
    font-size: 13px;
    line-height: 1;
    padding: 0;
  }
  .bcast-preview-box {
    background: #091219;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 12px;
  }
  .bcast-preview-hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .bcast-preview-list {
    margin-top: 8px;
    max-height: 140px;
    overflow-y: auto;
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    padding-top: 6px;
    border-top: 1px solid var(--border);
  }
  .bcast-delay-row {
    display: flex;
    gap: 12px;
  }
  .bcast-delay-input {
    display: flex;
    align-items: center;
    gap: 6px;
    background: #091219;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 4px 8px;
    font-size: 12px;
  }
  .bcast-delay-input input.num-sm {
    width: 60px;
    border: none;
    background: transparent;
    padding: 2px 4px;
    text-align: center;
  }
  .bcast-btn {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 10px 16px;
    font-weight: 600;
  }
  .bcast-toolbar {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }
  .bcast-table-wrapper {
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: #091219;
  }
  .bcast-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    text-align: left;
  }
  .bcast-table th {
    background: rgba(255, 255, 255, 0.03);
    padding: 9px 12px;
    color: var(--muted);
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  .bcast-table td {
    padding: 10px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    vertical-align: middle;
  }
  .bcast-table tr:hover td {
    background: rgba(255, 255, 255, 0.02);
  }
  .badge-status {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 9999px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
  }
  .badge-pending { background: rgba(245, 158, 11, 0.15); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.3); }
  .badge-sent { background: rgba(0, 200, 150, 0.15); color: #00c896; border: 1px solid rgba(0, 200, 150, 0.3); }
  .badge-failed { background: rgba(244, 91, 105, 0.15); color: #f45b69; border: 1px solid rgba(244, 91, 105, 0.3); }
  .badge-cancelled { background: rgba(127, 146, 159, 0.15); color: #7f929f; border: 1px solid rgba(127, 146, 159, 0.3); }
  .badge-processing { background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }

  .bcast-roles-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
    margin-top: 10px;
  }
  .bcast-role-card {
    background: #091219;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .bcast-role-card-left {
    display: flex;
    align-items: center;
    gap: 9px;
  }
  .bcast-role-card-name {
    font-weight: 600;
    font-size: 14px;
  }
  .bcast-role-card-count {
    font-size: 11px;
    color: var(--muted);
  }
  .bcast-role-creator-bar {
    display: flex;
    gap: 10px;
    align-items: center;
    background: #091219;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid var(--border);
  }
  .bcast-color-picker-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--muted);
  }
  .bcast-color-input {
    width: 34px;
    height: 30px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: transparent;
    cursor: pointer;
    padding: 0;
  }
  .bcast-excel-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 20px;
  }
  .bcast-feature-list {
    margin: 0;
    padding-left: 20px;
    font-size: 13px;
    color: var(--muted);
    line-height: 1.6;
  }
  .bcast-desc {
    font-size: 13px;
    color: var(--muted);
    line-height: 1.5;
    margin: 0;
  }
  .bcast-pagination {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
  }
  .bcast-modal {
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 0;
    box-shadow: 0 25px 60px rgba(0,0,0,0.7);
    max-width: 520px;
    width: 90%;
  }
  .bcast-modal-lg {
    max-width: 760px;
  }
  .bcast-modal-body {
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .bcast-modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 10px;
  }
  .bcast-file-picker {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .bcast-file-preview {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #091219;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 12px;
  }
  .bcast-logs-container {
    max-height: 440px;
    overflow-y: auto;
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
        <path d="M12 8v4l2.5 1.5"/>
      </svg>
    </div>
    <div class="brand-text">
      <span class="brand-name">WAmate</span>
      <span class="brand-sub">Second Brain</span>
    </div>
  </div>
  <div class="header-right">
    <div id="statsStrip" class="stats-strip"><span class="stat-pill">Loading stats...</span></div>
    __LOGOUT_BTN__
  </div>
</header>

<nav class="tabs">
  <button class="tab-btn active" data-tab="search">
    <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
    <span>Search</span><span class="tab-kbd">/</span>
  </button>
  <button class="tab-btn" data-tab="chats">
    <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
    <span>Chats</span>
  </button>
  <button class="tab-btn" data-tab="media">
    <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>
    <span>Media &amp; Docs</span>
  </button>
  <button class="tab-btn" data-tab="vault">
    <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
    <span>Anti-Delete Vault</span>
  </button>
  <button class="tab-btn" data-tab="broadcast">
    <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.2 19.1 19.1"/></svg>
    <span>Broadcast</span>
  </button>
  __AI_TAB_BTN__
</nav>

<main>
  <datalist id="chatSuggestList"></datalist>

  <!-- Search Panel -->
  <section id="tab-search" class="panel active">
    <div class="toolbar">
      <input id="searchQuery" class="grow" type="search" placeholder="Search messages, OCR text, PDFs/CSVs, links, voice transcripts... (Press /)" />
      <input id="searchChat" class="chat-filter-box" list="chatSuggestList" type="search" placeholder="Chat name or phone (+91...)" />
      <select id="searchScope" title="Chat Scope">
        <option value="all">All Chats</option>
        <option value="dm">1-on-1 DMs</option>
        <option value="group">Groups Only</option>
      </select>
      <select id="searchType">
        <option value="all">All Types</option>
        <option value="image">Photos / Images</option>
        <option value="document">Documents</option>
        <option value="audio">Voice Notes / Audio</option>
        <option value="video">Videos</option>
        <option value="link">Links</option>
        <option value="text">Plain Text</option>
        <option value="contact">Contact Cards</option>
        <option value="location">Locations</option>
      </select>
      <label class="check"><input id="searchStatuses" type="checkbox" /> Statuses</label>
      <div class="date-range-group" data-date-group="search" title="Filter messages by date range">
        <input id="searchFrom" type="date" aria-label="From date" title="From date" />
        <span class="date-sep">&ndash;</span>
        <input id="searchTo" type="date" aria-label="To date" title="To date" />
        <div class="preset-group">
          <button type="button" class="preset-btn active" data-preset="all">All</button>
          <button type="button" class="preset-btn" data-preset="today">Today</button>
          <button type="button" class="preset-btn" data-preset="7d">7d</button>
          <button type="button" class="preset-btn" data-preset="30d">30d</button>
        </div>
      </div>
      <button id="searchBtn" class="primary-btn">
        <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        <span>Search</span>
      </button>
      <button id="searchClearBtn" class="ghost-btn" title="Reset Search Filters">Clear</button>
    </div>
    <div id="searchSummary" class="toolbar-summary"></div>
    <div id="searchResults" class="results-list"></div>
    <div class="load-more-wrap"><button id="searchMoreBtn" class="ghost-btn" style="display:none">Load More Results</button></div>
  </section>

  <!-- Chats Panel -->
  <section id="tab-chats" class="panel">
    <div class="chats-layout">
      <div class="chat-sidebar">
        <div class="chat-sidebar-top">
          <input id="chatFilterInput" type="search" placeholder="Filter chat name or phone (+91...)" />
          <div class="scope-pills" id="chatScopePills">
            <button type="button" class="scope-btn active" data-scope="all">All</button>
            <button type="button" class="scope-btn" data-scope="dm">DMs</button>
            <button type="button" class="scope-btn" data-scope="group">Groups</button>
          </div>
          <label class="check"><input id="chatIncludeStatuses" type="checkbox" /> Show Status Broadcasts</label>
        </div>
        <div id="chatList" class="chat-list"></div>
      </div>
      <div class="chat-view">
        <div class="chat-view-header">
          <div class="chat-view-who">
            <div id="activeChatAvatar" class="avatar" style="display:none"></div>
            <div style="min-width:0">
              <div id="activeChatTitle" class="chat-view-title">Select a conversation</div>
              <div id="activeChatSub" class="phone-tag"></div>
            </div>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button id="chatSearchJumpBtn" class="ghost-btn" style="display:none">Search Chat</button>
            <button id="chatMediaJumpBtn" class="ghost-btn" style="display:none">Media</button>
            <button id="chatAskJumpBtn" class="ghost-btn" style="display:none">Ask AI</button>
            <button id="loadOlderBtn" class="ghost-btn" style="display:none">Load Older</button>
          </div>
        </div>
        <div id="chatMessages" class="chat-messages">
          <div class="empty-state" style="margin:auto;max-width:440px">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <div>Select any conversation on the left to browse full history, phone metadata, saved media &amp; cached AI/OCR text.</div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- Media Gallery Panel -->
  <section id="tab-media" class="panel">
    <div class="toolbar">
      <select id="mediaFilter">
        <option value="all">All Stored Media</option>
        <option value="image">Images</option>
        <option value="document">Documents (PDF, Excel, Word...)</option>
        <option value="audio">Voice Notes &amp; Audio</option>
        <option value="video">Videos</option>
        <option value="status">Status Updates</option>
        <option value="sticker">Stickers</option>
      </select>
      <input id="mediaChat" class="chat-filter-box" list="chatSuggestList" type="search" placeholder="Chat name or phone (+91...)" />
      <select id="mediaScope" title="Chat Scope">
        <option value="all">All Chats</option>
        <option value="dm">1-on-1 DMs</option>
        <option value="group">Groups Only</option>
      </select>
      <input id="mediaQuery" class="grow" type="search" placeholder="Filter by filename, caption or OCR text..." />
      <label class="check"><input id="mediaStatuses" type="checkbox" checked /> Statuses</label>
      <div class="date-range-group" data-date-group="media" title="Filter stored media by date range">
        <input id="mediaFrom" type="date" aria-label="From date" title="From date" />
        <span class="date-sep">&ndash;</span>
        <input id="mediaTo" type="date" aria-label="To date" title="To date" />
        <div class="preset-group">
          <button type="button" class="preset-btn active" data-preset="all">All</button>
          <button type="button" class="preset-btn" data-preset="today">Today</button>
          <button type="button" class="preset-btn" data-preset="7d">7d</button>
          <button type="button" class="preset-btn" data-preset="30d">30d</button>
        </div>
      </div>
      <button id="mediaSearchBtn" class="primary-btn">Apply</button>
      <button id="mediaClearBtn" class="ghost-btn" title="Reset Media Filters">Clear</button>
    </div>
    <div id="mediaSummary" class="toolbar-summary"></div>
    <div id="mediaGrid" class="media-grid"></div>
    <div class="load-more-wrap"><button id="mediaMoreBtn" class="ghost-btn" style="display:none">Load More Media</button></div>
  </section>

  <!-- Anti-Delete Vault Panel -->
  <section id="tab-vault" class="panel">
    <div class="toolbar">
      <select id="vaultMode">
        <option value="all">Deleted &amp; Edited</option>
        <option value="deleted">Deleted for Everyone Only</option>
        <option value="edited">Edited Messages Only</option>
      </select>
      <input id="vaultQuery" class="grow" list="chatSuggestList" type="search" placeholder="Filter by contact, group or phone number (+91...)" />
      <select id="vaultScope" title="Chat Scope">
        <option value="all">All Chats</option>
        <option value="dm">1-on-1 DMs</option>
        <option value="group">Groups Only</option>
      </select>
      <label class="check" title="Include deleted or edited WhatsApp Status updates"><input id="vaultStatuses" type="checkbox" /> Statuses</label>
      <div class="date-range-group" data-date-group="vault" title="Filter vault items by date range">
        <input id="vaultFrom" type="date" aria-label="From date" title="From date" />
        <span class="date-sep">&ndash;</span>
        <input id="vaultTo" type="date" aria-label="To date" title="To date" />
        <div class="preset-group">
          <button type="button" class="preset-btn active" data-preset="all">All</button>
          <button type="button" class="preset-btn" data-preset="today">Today</button>
          <button type="button" class="preset-btn" data-preset="7d">7d</button>
          <button type="button" class="preset-btn" data-preset="30d">30d</button>
        </div>
      </div>
      <button id="vaultRefreshBtn" class="primary-btn">Refresh Vault</button>
      <button id="vaultClearBtn" class="ghost-btn" title="Reset Vault Filters">Clear</button>
    </div>
    <div id="vaultResults" class="results-list"></div>
  </section>

  <!-- Broadcast & Campaigns Panel -->
  <section id="tab-broadcast" class="panel">
    <div class="bcast-sub-nav">
      <button type="button" class="bcast-sub-btn active" data-sub="send" onclick="switchBcastSubTab('send')">
        <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        <span>Send</span>
      </button>
      <button type="button" class="bcast-sub-btn" data-sub="schedule" onclick="switchBcastSubTab('schedule')">
        <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        <span>Schedule</span>
      </button>
      <button type="button" class="bcast-sub-btn" data-sub="contacts" onclick="switchBcastSubTab('contacts')">
        <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        <span>Contacts</span>
      </button>
      <button type="button" class="bcast-sub-btn" data-sub="groups" onclick="switchBcastSubTab('groups')">
        <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 21a8 8 0 0 0-16 0"/><circle cx="10" cy="8" r="5"/><path d="M22 20c0-3.37-2-6.5-5-7.5"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        <span>Groups</span>
      </button>
      <button type="button" class="bcast-sub-btn" data-sub="roles" onclick="switchBcastSubTab('roles')">
        <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
        <span>Roles</span>
      </button>
      <button type="button" class="bcast-sub-btn" data-sub="history" onclick="switchBcastSubTab('history')">
        <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 14 14"/><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
        <span>History</span>
      </button>
      <button type="button" class="bcast-sub-btn" data-sub="excel" onclick="switchBcastSubTab('excel')">
        <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
        <span>Excel</span>
      </button>
    </div>

    <!-- Sub-panel 1: Send Broadcast -->
    <div id="bcast-panel-send" class="bcast-sub-panel active">
      <div class="bcast-form-grid">
        <div class="bcast-card">
          <div class="bcast-card-hdr">
            <h3 class="bcast-title">1. Target Audience &amp; Roles</h3>
            <span id="sendTargetBadge" class="badge-status badge-processing">Targeting 0</span>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Audience Scope</label>
            <div class="bcast-toggle-group">
              <label class="bcast-toggle-opt"><input type="radio" name="sendAudience" value="both" checked onchange="updateSendPreview()"/><span>All (Contacts &amp; Groups)</span></label>
              <label class="bcast-toggle-opt"><input type="radio" name="sendAudience" value="contacts" onchange="updateSendPreview()"/><span>Contacts Only</span></label>
              <label class="bcast-toggle-opt"><input type="radio" name="sendAudience" value="groups" onchange="updateSendPreview()"/><span>Groups Only</span></label>
            </div>
          </div>
          <div class="bcast-field">
            <div class="bcast-label-row">
              <label class="bcast-label">Filter by Roles (Optional)</label>
              <div class="bcast-role-actions">
                <button type="button" class="text-link" onclick="selectAllRoles('sendRolePills'); updateSendPreview();">Select All</button>
                <span class="sep">&middot;</span>
                <button type="button" class="text-link" onclick="clearAllRoles('sendRolePills'); updateSendPreview();">Clear</button>
              </div>
            </div>
            <div id="sendRolePills" class="bcast-role-pills"></div>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Specific Recipients (Optional)</label>
            <div class="bcast-ac-wrapper">
              <input id="sendSpecificInput" type="text" class="bcast-input" placeholder="Type contact or group name / phone to add..." autocomplete="off"/>
              <div id="sendSpecificDropdown" class="bcast-ac-dropdown" style="display:none"></div>
            </div>
            <div id="sendSpecificChips" class="bcast-chips-container"></div>
          </div>
          <div id="sendPreviewContainer" class="bcast-preview-box">
            <div class="bcast-preview-hdr">
              <span id="sendPreviewSummary">Loading recipients preview...</span>
              <button type="button" class="text-link" onclick="toggleRecipientChips('sendPreviewList')">Show / Hide List</button>
            </div>
            <div id="sendPreviewList" class="bcast-preview-list" style="display:none"></div>
          </div>
        </div>

        <div class="bcast-card">
          <div class="bcast-card-hdr">
            <h3 class="bcast-title">2. Message &amp; Media</h3>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Message Text</label>
            <textarea id="sendMessageText" class="bcast-textarea" rows="6" placeholder="Write your broadcast message here...&#10;&#10;Supported formatting:&#10;*bold* _italic_ ~strike~ ```monospace```&#10;Use {name} to personalize with recipient's name" oninput="updateCharCount('sendMessageText', 'sendCharCount')"></textarea>
            <div class="bcast-meta-row" style="display:flex; justify-content:space-between; margin-top:4px;">
              <span id="sendCharCount" class="bcast-hint" style="font-size:11px; color:var(--muted);">0 characters</span>
              <span class="bcast-hint" style="font-size:11px; color:var(--muted);">Markdown &amp; {name} personalization supported</span>
            </div>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Attachment (Optional — Image, Video, Audio, Document)</label>
            <div class="bcast-file-picker">
              <input id="sendFileInput" type="file" onchange="handleFileSelect(this, 'sendFilePreview', 'sendAttachData')"/>
              <div id="sendFilePreview" class="bcast-file-preview" style="display:none"></div>
            </div>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Delivery Pacing (Anti-Spam Safety)</label>
            <div class="bcast-delay-row">
              <div class="bcast-delay-input">
                <span class="prefix">Min Delay:</span>
                <input id="sendDelayMin" type="number" step="0.5" min="0.5" max="10.0" value="1.0" class="bcast-input num-sm"/>
                <span class="suffix">sec</span>
              </div>
              <div class="bcast-delay-input">
                <span class="prefix">Max Delay:</span>
                <input id="sendDelayMax" type="number" step="0.5" min="1.0" max="15.0" value="2.5" class="bcast-input num-sm"/>
                <span class="suffix">sec</span>
              </div>
            </div>
            <span class="bcast-hint" style="font-size:11px; color:var(--muted); margin-top:4px;">Randomized delay between sends avoids spam flags and keeps your session safe.</span>
          </div>
          <div class="bcast-actions-row" style="margin-top:6px;">
            <button id="sendBroadcastBtn" type="button" class="primary-btn bcast-btn" onclick="submitBroadcastSend()">
              <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
              <span>Send Broadcast Now</span>
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Sub-panel 2: Schedule Campaign -->
    <div id="bcast-panel-schedule" class="bcast-sub-panel">
      <div class="bcast-form-grid">
        <div class="bcast-card">
          <div class="bcast-card-hdr">
            <h3 class="bcast-title">Schedule a Future Campaign</h3>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Send Date &amp; Time (Local Time)</label>
            <input id="schedTimeInput" type="datetime-local" class="bcast-input"/>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Audience Scope</label>
            <div class="bcast-toggle-group">
              <label class="bcast-toggle-opt"><input type="radio" name="schedAudience" value="both" checked onchange="updateSchedPreview()"/><span>All</span></label>
              <label class="bcast-toggle-opt"><input type="radio" name="schedAudience" value="contacts" onchange="updateSchedPreview()"/><span>Contacts</span></label>
              <label class="bcast-toggle-opt"><input type="radio" name="schedAudience" value="groups" onchange="updateSchedPreview()"/><span>Groups</span></label>
            </div>
          </div>
          <div class="bcast-field">
            <div class="bcast-label-row">
              <label class="bcast-label">Target Roles</label>
              <div class="bcast-role-actions">
                <button type="button" class="text-link" onclick="selectAllRoles('schedRolePills'); updateSchedPreview();">Select All</button>
                <span class="sep">&middot;</span>
                <button type="button" class="text-link" onclick="clearAllRoles('schedRolePills'); updateSchedPreview();">Clear</button>
              </div>
            </div>
            <div id="schedRolePills" class="bcast-role-pills"></div>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Specific Recipients (Optional)</label>
            <div class="bcast-ac-wrapper">
              <input id="schedSpecificInput" type="text" class="bcast-input" placeholder="Type name or phone to add..." autocomplete="off"/>
              <div id="schedSpecificDropdown" class="bcast-ac-dropdown" style="display:none"></div>
            </div>
            <div id="schedSpecificChips" class="bcast-chips-container"></div>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Message</label>
            <textarea id="schedMessageText" class="bcast-textarea" rows="4" placeholder="Message content to schedule..."></textarea>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Attachment (Optional)</label>
            <input id="schedFileInput" type="file" onchange="handleFileSelect(this, 'schedFilePreview', 'schedAttachData')"/>
            <div id="schedFilePreview" class="bcast-file-preview" style="display:none"></div>
          </div>
          <button id="schedSubmitBtn" type="button" class="primary-btn bcast-btn" onclick="submitScheduleCampaign()">
            <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            <span>Schedule Campaign</span>
          </button>
        </div>

        <div class="bcast-card">
          <div class="bcast-card-hdr">
            <h3 class="bcast-title">Active Scheduled Queue</h3>
            <button type="button" class="ghost-btn-sm" onclick="loadScheduledQueue()">Refresh</button>
          </div>
          <div id="schedQueueTableContainer" class="bcast-table-wrapper">
            <div class="bcast-empty">Loading scheduled queue...</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Sub-panel 3: Contacts Manager -->
    <div id="bcast-panel-contacts" class="bcast-sub-panel">
      <div class="bcast-card full-card">
        <div class="bcast-toolbar">
          <input id="contactsSearchInput" type="search" class="bcast-input grow" placeholder="Search contacts by name, phone or notes..." oninput="debounce(loadContactsList, 250)()"/>
          <select id="contactsRoleFilter" class="bcast-select" style="max-width:200px;" onchange="loadContactsList()">
            <option value="">All Roles</option>
          </select>
          <button type="button" class="primary-btn-sm" onclick="openAddContactModal()">
            <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            <span>Add Custom Contact</span>
          </button>
        </div>
        <div id="contactsTableContainer" class="bcast-table-wrapper">
          <div class="bcast-empty">Loading contacts...</div>
        </div>
        <div id="contactsPagination" class="bcast-pagination"></div>
      </div>
    </div>

    <!-- Sub-panel 4: Groups Manager -->
    <div id="bcast-panel-groups" class="bcast-sub-panel">
      <div class="bcast-card full-card">
        <div class="bcast-toolbar">
          <input id="groupsSearchInput" type="search" class="bcast-input grow" placeholder="Search groups by name or JID..." oninput="debounce(loadGroupsList, 250)()"/>
          <select id="groupsRoleFilter" class="bcast-select" style="max-width:200px;" onchange="loadGroupsList()">
            <option value="">All Roles</option>
          </select>
        </div>
        <div id="groupsTableContainer" class="bcast-table-wrapper">
          <div class="bcast-empty">Loading groups...</div>
        </div>
        <div id="groupsPagination" class="bcast-pagination"></div>
      </div>
    </div>

    <!-- Sub-panel 5: Roles Manager -->
    <div id="bcast-panel-roles" class="bcast-sub-panel">
      <div class="bcast-card full-card">
        <div class="bcast-card-hdr">
          <h3 class="bcast-title">Manage Custom Roles</h3>
        </div>
        <div class="bcast-role-creator-bar">
          <input id="newRoleNameInput" type="text" class="bcast-input grow" placeholder="Enter new role name (e.g. VIP, Clients, Family, Nature)..." maxlength="50"/>
          <div class="bcast-color-picker-wrap">
            <label for="newRoleColorInput">Badge Color:</label>
            <input id="newRoleColorInput" type="color" value="#00c896" class="bcast-color-input"/>
          </div>
          <button type="button" class="primary-btn-sm" onclick="addNewRole()">Create Role</button>
        </div>
        <div id="rolesManagerGrid" class="bcast-roles-grid">
          <div class="bcast-empty">Loading roles...</div>
        </div>
      </div>
    </div>

    <!-- Sub-panel 6: Campaign History -->
    <div id="bcast-panel-history" class="bcast-sub-panel">
      <div class="bcast-card full-card">
        <div class="bcast-toolbar">
          <input id="historySearchInput" type="search" class="bcast-input grow" placeholder="Search campaign history by message content or role..." oninput="debounce(loadCampaignHistory, 250)()"/>
          <button type="button" class="ghost-btn-sm" onclick="loadCampaignHistory()">Refresh</button>
        </div>
        <div id="historyTableContainer" class="bcast-table-wrapper">
          <div class="bcast-empty">Loading campaign history...</div>
        </div>
        <div id="historyPagination" class="bcast-pagination"></div>
      </div>
    </div>

    <!-- Sub-panel 7: Excel Export / Import -->
    <div id="bcast-panel-excel" class="bcast-sub-panel">
      <div class="bcast-excel-grid">
        <div class="bcast-card">
          <div class="bcast-card-hdr">
            <h3 class="bcast-title">Backup / Export to Excel</h3>
          </div>
          <p class="bcast-desc">Download your entire WhatsApp address book, custom contacts, groups, roles, and notes formatted into a multi-sheet Microsoft Excel (.xlsx) workbook.</p>
          <ul class="bcast-feature-list">
            <li><strong>Roles Sheet:</strong> Names, theme colors, and creation timestamps.</li>
            <li><strong>Contacts Sheet:</strong> Names, phones, push names, role tags, and notes.</li>
            <li><strong>Groups Sheet:</strong> Group subjects, JIDs, and assigned roles.</li>
          </ul>
          <a href="/api/campaigns/export_excel" download="wamate_contacts_roles.xlsx" class="primary-btn bcast-btn" style="text-decoration:none; display:inline-flex;">
            <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            <span>Download Excel Workbook (.xlsx)</span>
          </a>
        </div>

        <div class="bcast-card">
          <div class="bcast-card-hdr">
            <h3 class="bcast-title">Restore / Import from Excel</h3>
          </div>
          <p class="bcast-desc">Upload a previously exported or edited <code>.xlsx</code> workbook to restore contacts, sync role mappings, or batch-update contact notes.</p>
          <div class="bcast-field">
            <label class="bcast-label">Import Mode</label>
            <div class="bcast-toggle-group">
              <label class="bcast-toggle-opt"><input type="radio" name="excelImportMode" value="merge" checked/><span>Merge (Preserve existing, add new &amp; update)</span></label>
              <label class="bcast-toggle-opt"><input type="radio" name="excelImportMode" value="replace"/><span>Replace (Wipe and replace all custom roles)</span></label>
            </div>
          </div>
          <div class="bcast-field">
            <label class="bcast-label">Choose .xlsx File</label>
            <input id="excelFileInput" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" class="bcast-input"/>
          </div>
          <button id="excelImportBtn" type="button" class="primary-btn bcast-btn" onclick="importExcelWorkbook()">
            <svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
            <span>Import &amp; Restore Workbook</span>
          </button>
        </div>
      </div>
    </div>
  </section>

  __AI_PANEL_HTML__
</main>

<dialog id="addContactModal" class="bcast-modal">
  <div class="modal-hdr">
    <strong>Add Custom Contact</strong>
    <button class="ghost-btn" onclick="document.getElementById('addContactModal').close()">✕</button>
  </div>
  <div class="modal-body bcast-modal-body">
    <div class="bcast-field">
      <label class="bcast-label">Contact Name</label>
      <input id="modalContactName" type="text" class="bcast-input" placeholder="e.g. Yash Patel"/>
    </div>
    <div class="bcast-field">
      <label class="bcast-label">Phone Number or JID</label>
      <input id="modalContactPhone" type="text" class="bcast-input" placeholder="e.g. +919876543210"/>
    </div>
    <div class="bcast-field">
      <label class="bcast-label">Assign Roles</label>
      <div id="modalContactRoles" class="bcast-role-pills"></div>
    </div>
    <div class="bcast-field">
      <label class="bcast-label">Notes (Optional)</label>
      <textarea id="modalContactNotes" class="bcast-textarea" rows="2" placeholder="e.g. Client from Mumbai office"></textarea>
    </div>
    <div class="bcast-modal-actions">
      <button type="button" class="ghost-btn" onclick="document.getElementById('addContactModal').close()">Cancel</button>
      <button type="button" class="primary-btn-sm" onclick="saveNewContact()">Save Contact</button>
    </div>
  </div>
</dialog>

<dialog id="campaignLogsModal" class="bcast-modal bcast-modal-lg">
  <div class="modal-hdr">
    <strong id="campaignLogsModalTitle">Campaign Execution Logs</strong>
    <button class="ghost-btn" onclick="document.getElementById('campaignLogsModal').close()">✕</button>
  </div>
  <div class="modal-body">
    <div id="campaignLogsModalContent" class="bcast-logs-container"></div>
  </div>
</dialog>

<dialog id="previewDialog">
  <div class="modal-hdr">
    <strong id="modalTitle" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Preview</strong>
    <button class="ghost-btn" onclick="document.getElementById('previewDialog').close()">Close</button>
  </div>
  <div id="modalBody" class="modal-body"></div>
</dialog>

<div id="toast" class="toast" role="status" aria-live="polite"></div>

<script>
__WAMATE_CAPS_JS__
const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const escArg = (s) => esc(JSON.stringify(String(s ?? '')));

const fmtBytes = (b) => {
  if (!b) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
};

const fmtPhone = (p) => p ? `+${String(p).replace(/^\+/, '')}` : '';

function debounce(fn, ms=220) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

let _toastTimer = null;
function showToast(msg) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 2100);
}

const AVATAR_PALETTES = [
  'linear-gradient(135deg,#0f766e,#14b8a6)',
  'linear-gradient(135deg,#0369a1,#38bdf8)',
  'linear-gradient(135deg,#4338ca,#818cf8)',
  'linear-gradient(135deg,#7c3aed,#c084fc)',
  'linear-gradient(135deg,#be123c,#fb7185)',
  'linear-gradient(135deg,#b45309,#f59e0b)',
  'linear-gradient(135deg,#047857,#34d399)',
];

function avatarPalette(seed) {
  let h = 0;
  const str = String(seed || 'W');
  for (let i = 0; i < str.length; i++) h = ((h << 5) - h + str.charCodeAt(i)) | 0;
  return AVATAR_PALETTES[Math.abs(h) % AVATAR_PALETTES.length];
}

function initialsFor(name) {
  const clean = String(name || '?').replace(/[^\p{L}\p{N}\s+]/gu, '').trim();
  if (!clean) return '?';
  const parts = clean.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return clean.slice(0, 2).toUpperCase();
}

function avatarHtml(name, seed, isGroup=false, sm=false) {
  const bg = avatarPalette(seed || name);
  const cls = sm ? 'avatar sm' : 'avatar';
  const inner = isGroup
    ? '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
    : esc(initialsFor(name));
  return `<div class="${cls}" style="background:${bg}">${inner}</div>`;
}

async function apiGet(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// Tabs & Cross-Tab Navigation
function switchTab(tabName, opts={}) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabName));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === `tab-${tabName}`));
  if (!opts.skipAutoLoad) {
    if (tabName === 'chats' && !window._chatsLoaded) loadChats();
    if (tabName === 'media' && !window._mediaLoaded) loadMedia(true);
    if (tabName === 'vault' && !window._vaultLoaded) loadVault();
    if (tabName === 'broadcast') initBroadcastTab();
    if (tabName === 'ask') setTimeout(() => document.getElementById('askInput')?.focus(), 40);
  }
}
document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

function jumpToChat(jid, name, phone='') {
  switchTab('chats', { skipAutoLoad: true });
  openChat(jid, name, false, phone);
}

function filterSearchByChat(target) {
  switchTab('search');
  document.getElementById('searchChat').value = target || '';
  runSearch(true);
}

function filterMediaByChat(target) {
  switchTab('media', { skipAutoLoad: true });
  document.getElementById('mediaChat').value = target || '';
  loadMedia(true);
}

function askAiAboutChat(target) {
  if (!document.getElementById('tab-ask')) return;
  switchTab('ask');
  const inp = document.getElementById('askChatFocus');
  if (inp) {
    inp.value = target || '';
    syncAskScopeUi();
  }
  document.getElementById('askInput')?.focus();
}

// Direct Message / Media Actions (Ask AI + Send to WhatsApp + Date Filters)
window._msgMeta = {};
let _askContext = null;

function rememberMsgMeta(m) {
  if (!m || !m.id) return;
  const keys = Object.keys(window._msgMeta);
  if (keys.length > 600) {
    for (let i = 0; i < 150; i++) delete window._msgMeta[keys[i]];
  }
  const chatName = m.chat_name || m.chat || '';
  const senderName = m.sender || chatName || 'Unknown';
  const snippetRaw = (m.media_filename || m.original_text || m.text || m.media_type || '').replace(/\s+/g, ' ').trim();
  const snippet = snippetRaw.length > 52 ? `${snippetRaw.slice(0, 52)}...` : snippetRaw;
  window._msgMeta[m.id] = {
    id: m.id,
    chatTarget: m.chat_phone || chatName || m.chat_jid || '',
    label: `${senderName}${chatName && senderName !== chatName ? ` (${chatName})` : ''}${snippet ? `: ${snippet}` : ''}`
  };
}

function setAskItemContext(ctx) {
  _askContext = ctx && ctx.id ? ctx : null;
  const banner = document.getElementById('askItemBanner');
  const chip = document.getElementById('askItemChip');
  if (banner && chip) {
    banner.style.display = _askContext ? 'flex' : 'none';
    if (_askContext) chip.textContent = _askContext.label || _askContext.id;
  }
}

function askAiAboutMessage(msgId) {
  if (!document.getElementById('tab-ask')) return;
  const dlg = document.getElementById('previewDialog');
  if (dlg && dlg.open) dlg.close();
  const meta = window._msgMeta[msgId] || { id: msgId, label: msgId, chatTarget: '' };
  setAskItemContext({ id: msgId, label: meta.label || msgId });
  switchTab('ask');
  const fc = document.getElementById('askChatFocus');
  if (fc && meta.chatTarget) {
    fc.value = meta.chatTarget;
    syncAskScopeUi();
  }
  const inp = document.getElementById('askInput');
  if (inp) {
    if (!inp.value.trim()) inp.placeholder = 'Ask a question about the attached message/file...';
    inp.focus();
  }
}

async function sendToWhatsApp(msgId, btn) {
  if (!msgId || !WAMATE_CAPS.hasWa) return;
  const origHtml = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Sending...';
  }
  try {
    const res = await fetch('/api/send_to_whatsapp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ msg_id: msgId })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
    showToast(data.detail || 'Sent to your WhatsApp self-chat');
    if (btn) {
      btn.textContent = 'Sent';
      setTimeout(() => { btn.innerHTML = origHtml; btn.disabled = false; }, 1800);
      return;
    }
  } catch (e) {
    showToast(`Send failed: ${e.message}`);
  }
  if (btn) {
    btn.innerHTML = origHtml;
    btn.disabled = false;
  }
}

const _AI_BTN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 4.7L18.6 9.6l-4.7 1.9L12 16.2l-1.9-4.7L5.4 9.6l4.7-1.9L12 3z"/></svg>';
const _WA_BTN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>';

function renderMsgActions(id, opts={}) {
  if (!id) return '';
  const parts = [];
  if (WAMATE_CAPS.hasAi) {
    parts.push(`<button type="button" class="action-pill ai-pill" onclick="askAiAboutMessage(${escArg(id)})" title="Ask WAmate AI about this item">${_AI_BTN_SVG}<span>Ask AI</span></button>`);
  }
  if (WAMATE_CAPS.hasWa && opts.canSendWa !== false) {
    parts.push(`<button type="button" class="action-pill wa-pill" onclick="sendToWhatsApp(${escArg(id)}, this)" title="Send directly to your WhatsApp Saved Messages / self-chat">${_WA_BTN_SVG}<span>Send to WA</span></button>`);
  }
  if (!parts.length) return '';
  const cls = opts.compact ? 'bubble-actions' : 'msg-action-bar';
  return `<div class="${cls}">${parts.join('')}</div>`;
}

function fmtLocalYmd(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function setPresetActive(groupName, activePreset) {
  document.querySelectorAll(`[data-date-group="${groupName}"] .preset-btn`).forEach(b => {
    b.classList.toggle('active', b.dataset.preset === activePreset);
  });
}

function setupDateGroup(groupName, fromId, toId, onChangeFn) {
  const fromEl = document.getElementById(fromId);
  const toEl = document.getElementById(toId);
  if (!fromEl || !toEl) return;
  const syncPreset = () => {
    const f = fromEl.value;
    const t = toEl.value;
    if (!f && !t) { setPresetActive(groupName, 'all'); return; }
    const todayStr = fmtLocalYmd(new Date());
    if (f === todayStr && t === todayStr) { setPresetActive(groupName, 'today'); return; }
    const d7 = new Date(); d7.setDate(d7.getDate() - 6);
    if (f === fmtLocalYmd(d7) && t === todayStr) { setPresetActive(groupName, '7d'); return; }
    const d30 = new Date(); d30.setDate(d30.getDate() - 29);
    if (f === fmtLocalYmd(d30) && t === todayStr) { setPresetActive(groupName, '30d'); return; }
    setPresetActive(groupName, '');
  };
  [fromEl, toEl].forEach(el => el.addEventListener('change', () => { syncPreset(); onChangeFn(); }));
  document.querySelectorAll(`[data-date-group="${groupName}"] .preset-btn`).forEach(btn => {
    btn.addEventListener('click', () => {
      const p = btn.dataset.preset;
      const now = new Date();
      const todayStr = fmtLocalYmd(now);
      if (p === 'all') { fromEl.value = ''; toEl.value = ''; }
      else if (p === 'today') { fromEl.value = todayStr; toEl.value = todayStr; }
      else if (p === '7d') { const d = new Date(); d.setDate(d.getDate() - 6); fromEl.value = fmtLocalYmd(d); toEl.value = todayStr; }
      else if (p === '30d') { const d = new Date(); d.setDate(d.getDate() - 29); fromEl.value = fmtLocalYmd(d); toEl.value = todayStr; }
      setPresetActive(groupName, p);
      onChangeFn();
    });
  });
}

function resetDateGroup(groupName, fromId, toId) {
  const f = document.getElementById(fromId);
  const t = document.getElementById(toId);
  if (f) f.value = '';
  if (t) t.value = '';
  setPresetActive(groupName, 'all');
}

document.getElementById('logoutBtn')?.addEventListener('click', async () => {
  try { await fetch('/api/logout', { method: 'POST' }); } catch (_) {}
  window.location.href = '/';
});

// Chat / Phone Suggestions Datalist
let _lastSuggestQ = null;
async function loadChatSuggestions(q='') {
  const norm = (q || '').trim();
  if (norm === _lastSuggestQ) return;
  _lastSuggestQ = norm;
  try {
    const items = await apiGet(`/api/chat_suggestions?q=${encodeURIComponent(norm)}&limit=120`);
    const dl = document.getElementById('chatSuggestList');
    dl.innerHTML = items.map(c => {
      const val = c.phone_number || c.chat_name || c.chat_jid;
      const phoneTxt = c.phone_number ? ` · ${fmtPhone(c.phone_number)}` : (c.is_group ? ' · Group' : '');
      return `<option value="${esc(val)}">${esc(c.chat_name)}${esc(phoneTxt)}</option>`;
    }).join('');
  } catch (_) {}
}
const debouncedSuggest = debounce((val) => loadChatSuggestions(val), 220);
['searchChat', 'mediaChat', 'vaultQuery', 'askChatFocus'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('input', e => debouncedSuggest(e.target.value));
});

// Stats
async function loadStats() {
  try {
    const s = await apiGet('/api/stats');
    document.getElementById('statsStrip').innerHTML = `
      <span class="stat-pill"><span class="stat-dot"></span>Msgs <b>${(s.total_messages||0).toLocaleString()}</b></span>
      <span class="stat-pill">Chats <b>${(s.total_contacts||0).toLocaleString()}</b></span>
      <span class="stat-pill"><span class="stat-dot blue"></span>Media <b>${(s.total_stored_media||0).toLocaleString()}</b> (${s.stored_mb||0} MB)</span>
      <span class="stat-pill">Indexed <b>${((s.total_local_ocr||0) + (s.total_local_docs||0)).toLocaleString()}</b></span>
      <span class="stat-pill">AI Cache <b>${(s.total_ocr_cached||0).toLocaleString()}</b></span>
      <span class="stat-pill"><span class="stat-dot warn"></span>Vault <b>${s.total_deleted||0}</b> del / <b>${s.total_edited||0}</b> edit</span>
      <span class="stat-pill">DB <b>${s.db_size_mb||0} MB</b></span>
    `;
    const badge = document.getElementById('askModelBadge');
    if (badge && s.model_name) {
      const keysTxt = s.api_keys_count > 1 ? ` · ${s.api_keys_count} keys` : '';
      badge.innerHTML = `<span class="live-dot"></span>${esc(s.model_name)}${esc(keysTxt)}`;
    }
  } catch (e) {
    document.getElementById('statsStrip').innerHTML = '<span class="stat-pill">Stats unavailable</span>';
  }
}

function mediaUrl(id, dl=false) {
  return `/api/media/${encodeURIComponent(id)}${dl ? '?download=1' : ''}`;
}

function videoThumbUrl(id) {
  return `/api/video_thumb/${encodeURIComponent(id)}`;
}

function fallbackVideoThumb(imgEl, id) {
  if (!imgEl) return;
  imgEl.onerror = null;
  const v = document.createElement('video');
  v.muted = true;
  v.playsInline = true;
  v.preload = 'metadata';
  v.src = `${mediaUrl(id)}#t=0.5`;
  v.onerror = () => {
    v.parentElement?.querySelector('.play-badge')?.remove();
    const icon = document.createElement('div');
    icon.className = 'media-tile-icon';
    icon.textContent = '🎬';
    v.replaceWith(icon);
  };
  imgEl.replaceWith(v);
}

function videoThumbMarkup(id) {
  return `<img loading="lazy" src="${videoThumbUrl(id)}" onerror="fallbackVideoThumb(this,${escArg(id)})" /><span class="play-badge">▶</span><span class="kind-chip">VIDEO</span>`;
}

function renderMediaSnippet(m) {
  if (!m.has_local_media) return '';
  const url = mediaUrl(m.id, false);
  const dlUrl = mediaUrl(m.id, true);
  const mt = (m.media_type || '').toLowerCase();
  const mime = (m.media_mimetype || '').toLowerCase();
  const label = m.media_filename || m.media_type || 'Media';
  let preview = '';
  if (mt === 'image' || mt === 'sticker' || mime.startsWith('image/')) {
    preview = `<img class="thumb-img" loading="lazy" src="${url}" onclick="openItemModal(${escArg(m.id)},${escArg(label)},'image')" />`;
  } else if (mt === 'voice_note' || mt === 'audio' || mime.startsWith('audio/')) {
    preview = `<audio controls preload="none" src="${url}" style="margin-top:8px;max-width:100%"></audio>`;
  } else if (mt === 'video' || mime.startsWith('video/')) {
    const vTitle = m.media_filename || 'Video';
    preview = `
      <div class="video-thumb-box" onclick="openItemModal(${escArg(m.id)},${escArg(vTitle)},'video')" title="Play Video">
        ${videoThumbMarkup(m.id)}
      </div>
      <div class="media-bar">
        <button class="ghost-btn" onclick="openItemModal(${escArg(m.id)},${escArg(vTitle)},'video')">▶ Play</button>
        <a class="ghost-btn" href="${dlUrl}">Download ${fmtBytes(m.media_size)}</a>
      </div>`;
  } else {
    preview = `<div class="media-bar">
      <button class="ghost-btn" onclick="openDocModal(${escArg(m.id)},${escArg(m.media_filename || 'Document')})">Preview Text</button>
      <a class="ghost-btn" href="${url}" target="_blank">Open</a>
      <a class="ghost-btn" href="${dlUrl}">Download ${fmtBytes(m.media_size)}</a>
    </div>`;
  }
  return preview;
}

function renderMessageCard(m) {
  rememberMsgMeta(m);
  let badges = `<span class="badge">${esc(m.media_type)}</span>`;
  if (m.has_local_media) badges += `<span class="badge saved">Saved</span>`;
  if (m.ocr_cached) badges += `<span class="badge ai">AI Cached</span>`;
  if (m.is_deleted) badges += `<span class="badge deleted">Deleted ${esc(m.deleted_at)}</span>`;
  if (m.is_edited) badges += `<span class="badge edited">Edited ${esc(m.edited_at)}</span>`;
  const phonePill = m.chat_phone ? `<span class="phone-tag">${esc(fmtPhone(m.chat_phone))}</span>` : '';
  const origHtml = (m.original_text && m.original_text !== m.text && (m.is_deleted || m.is_edited))
    ? `<div class="orig-box del"><strong>Preserved Original:</strong>\n${esc(m.original_text)}</div>` : '';
  const ocrHtml = m.ocr_cached
    ? `<div class="orig-box cache"><strong>AI Vision / Transcript Cache:</strong>\n${esc(m.ocr_cached.slice(0, 1200))}${m.ocr_cached.length > 1200 ? '...' : ''}</div>` : '';
  return `
    <div class="msg-card">
      <div class="msg-meta">
        <div class="msg-meta-left">
          ${avatarHtml(m.sender || m.chat_name, m.chat_jid, false, true)}
          <span class="who">${esc(m.sender)}</span>
          <span class="muted">in</span>
          <button class="link-pill" onclick="jumpToChat(${escArg(m.chat_jid)},${escArg(m.chat_name)},${escArg(m.chat_phone || '')})" title="Open conversation">${esc(m.chat_name)}</button>
          ${phonePill}
          ${badges}
        </div>
        <div class="msg-ts">${esc(m.timestamp_str)}</div>
      </div>
      <div class="msg-body">${esc(m.text || m.media_filename || '[Empty]')}</div>
      ${origHtml}
      ${ocrHtml}
      ${renderMediaSnippet(m)}
      ${renderMsgActions(m.id)}
    </div>
  `;
}

// Search Tab
let searchOffset = 0;
function renderSearchSummary(count) {
  const q = document.getElementById('searchQuery').value.trim();
  const chat = document.getElementById('searchChat').value.trim();
  const scope = document.getElementById('searchScope').value;
  const mediaType = document.getElementById('searchType').value;
  const fromD = document.getElementById('searchFrom').value;
  const toD = document.getElementById('searchTo').value;
  const chips = [];
  if (q) chips.push(`Query: ${q}`);
  if (chat) chips.push(`Chat/Phone: ${chat}`);
  if (scope !== 'all') chips.push(scope === 'dm' ? '1-on-1 DMs' : 'Groups');
  if (mediaType !== 'all') chips.push(`Type: ${mediaType}`);
  if (fromD || toD) chips.push(`Dates: ${fromD || '...'} -> ${toD || '...'}`);
  document.getElementById('searchSummary').innerHTML = `
    <span>${count ? `Showing ${count} loaded result${count === 1 ? '' : 's'}` : 'No results'}</span>
    <div class="active-chips">${chips.map(c => `<span class="filter-chip">${esc(c)}</span>`).join('')}</div>
  `;
}

async function runSearch(reset=true) {
  if (reset) { searchOffset = 0; document.getElementById('searchResults').innerHTML = '<div class="muted">Searching messages &amp; indexes...</div>'; }
  const q = document.getElementById('searchQuery').value.trim();
  const chat = document.getElementById('searchChat').value.trim();
  const scope = document.getElementById('searchScope').value;
  const mediaType = document.getElementById('searchType').value;
  const statuses = document.getElementById('searchStatuses').checked ? '1' : '0';
  const fromD = document.getElementById('searchFrom').value;
  const toD = document.getElementById('searchTo').value;
  const rows = await apiGet(`/api/search?q=${encodeURIComponent(q)}&chat=${encodeURIComponent(chat)}&scope=${encodeURIComponent(scope)}&media_type=${encodeURIComponent(mediaType)}&statuses=${statuses}&from=${encodeURIComponent(fromD)}&to=${encodeURIComponent(toD)}&limit=50&offset=${searchOffset}`);
  const box = document.getElementById('searchResults');
  if (reset) box.innerHTML = '';
  if (!rows.length && reset) {
    box.innerHTML = '<div class="empty-state"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg><div>No matching messages found.</div><div class="muted">Try shorter keywords, phone digits (e.g. 98505), or widen date filters.</div></div>';
  } else {
    box.insertAdjacentHTML('beforeend', rows.map(renderMessageCard).join(''));
    searchOffset += rows.length;
  }
  renderSearchSummary(searchOffset);
  document.getElementById('searchMoreBtn').style.display = rows.length === 50 ? 'inline-flex' : 'none';
}
document.getElementById('searchBtn').addEventListener('click', () => runSearch(true));
document.getElementById('searchMoreBtn').addEventListener('click', () => runSearch(false));
document.getElementById('searchScope').addEventListener('change', () => runSearch(true));
document.getElementById('searchType').addEventListener('change', () => runSearch(true));
document.getElementById('searchStatuses').addEventListener('change', () => runSearch(true));
setupDateGroup('search', 'searchFrom', 'searchTo', () => runSearch(true));
document.getElementById('searchClearBtn').addEventListener('click', () => {
  document.getElementById('searchQuery').value = '';
  document.getElementById('searchChat').value = '';
  document.getElementById('searchScope').value = 'all';
  document.getElementById('searchType').value = 'all';
  document.getElementById('searchStatuses').checked = false;
  resetDateGroup('search', 'searchFrom', 'searchTo');
  runSearch(true);
});
['searchQuery', 'searchChat'].forEach(id => document.getElementById(id).addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(true); }));

// Chats Tab
let activeChatJid = '';
let activeChatPhone = '';
let oldestChatTs = 0;
let chatScope = 'all';
async function loadChats() {
  window._chatsLoaded = true;
  const q = document.getElementById('chatFilterInput').value.trim();
  const st = document.getElementById('chatIncludeStatuses').checked ? '1' : '0';
  const chats = await apiGet(`/api/chats?q=${encodeURIComponent(q)}&scope=${encodeURIComponent(chatScope)}&statuses=${st}&limit=100`);
  const el = document.getElementById('chatList');
  if (!chats.length) { el.innerHTML = '<div class="empty-state" style="margin:12px">No matching chats or phone numbers.</div>'; return; }
  el.innerHTML = chats.map(c => `
    <div class="chat-item ${c.chat_jid === activeChatJid ? 'active' : ''}" onclick="openChat(${escArg(c.chat_jid)},${escArg(c.chat_name)},false,${escArg(c.phone_number || '')})">
      ${avatarHtml(c.chat_name, c.chat_jid, !!c.is_group)}
      <div class="chat-item-main">
        <div class="chat-item-top">
          <span class="chat-item-name">${esc(c.chat_name)}</span>
          <span class="badge">${c.msg_count}</span>
        </div>
        ${c.phone_number ? `<div class="phone-tag">${esc(fmtPhone(c.phone_number))}</div>` : ''}
        <div class="chat-item-sub">${esc(c.last_time)} &middot; ${esc(c.preview || c.last_media_type)}</div>
      </div>
    </div>
  `).join('');
}
document.getElementById('chatFilterInput').addEventListener('input', debounce(() => loadChats(), 200));
document.getElementById('chatIncludeStatuses').addEventListener('change', () => loadChats());
document.querySelectorAll('#chatScopePills .scope-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    chatScope = btn.dataset.scope;
    document.querySelectorAll('#chatScopePills .scope-btn').forEach(b => b.classList.toggle('active', b === btn));
    loadChats();
  });
});

function renderBubble(m) {
  rememberMsgMeta({ ...m, chat_jid: activeChatJid, chat_name: document.getElementById('activeChatTitle')?.textContent || '', chat_phone: activeChatPhone });
  const tags = [];
  if (m.is_deleted) tags.push('<span class="badge deleted">Deleted</span>');
  if (m.is_edited) tags.push('<span class="badge edited">Edited</span>');
  const orig = (m.original_text && m.original_text !== m.text && (m.is_deleted || m.is_edited))
    ? `<div class="orig-box del"><strong>Original:</strong> ${esc(m.original_text)}</div>` : '';
  return `
    <div class="bubble ${m.is_from_me ? 'me' : ''}">
      <div class="bubble-hdr">${esc(m.sender)} ${tags.join(' ')}</div>
      <div class="msg-body">${esc(m.text || m.media_filename || '')}</div>
      ${orig}
      ${renderMediaSnippet(m)}
      ${renderMsgActions(m.id, { compact: true })}
      <div class="bubble-ts">${esc(m.timestamp_str)}</div>
    </div>
  `;
}

async function openChat(jid, name, older=false, phone='') {
  activeChatJid = jid;
  if (!older) activeChatPhone = phone || '';
  const titleTxt = name || jid;
  document.getElementById('activeChatTitle').textContent = titleTxt;
  document.getElementById('activeChatSub').textContent = activeChatPhone ? fmtPhone(activeChatPhone) : jid;
  const av = document.getElementById('activeChatAvatar');
  av.style.display = 'inline-flex';
  av.style.background = avatarPalette(jid);
  av.textContent = jid.endsWith('@g.us') ? 'GP' : initialsFor(titleTxt);

  document.getElementById('chatSearchJumpBtn').style.display = 'inline-flex';
  document.getElementById('chatMediaJumpBtn').style.display = 'inline-flex';
  const askJump = document.getElementById('chatAskJumpBtn');
  if (askJump && document.getElementById('tab-ask')) askJump.style.display = 'inline-flex';

  const msgBox = document.getElementById('chatMessages');
  if (!older) { oldestChatTs = 0; msgBox.innerHTML = '<div class="muted">Loading conversation...</div>'; }
  const rows = await apiGet(`/api/chat?jid=${encodeURIComponent(jid)}&before=${older ? oldestChatTs : 0}&limit=80`);
  if (!older) msgBox.innerHTML = '';
  if (rows.length) {
    oldestChatTs = rows[0].timestamp;
    const html = rows.map(renderBubble).join('');
    if (older) msgBox.insertAdjacentHTML('afterbegin', html);
    else { msgBox.innerHTML = html; msgBox.scrollTop = msgBox.scrollHeight; }
  } else if (!older) {
    msgBox.innerHTML = '<div class="muted">No messages found in this chat.</div>';
  }
  document.getElementById('loadOlderBtn').style.display = rows.length === 80 ? 'inline-flex' : 'none';
  loadChats();
}
document.getElementById('loadOlderBtn').addEventListener('click', () => {
  if (activeChatJid) openChat(activeChatJid, document.getElementById('activeChatTitle').textContent, true, activeChatPhone);
});
document.getElementById('chatSearchJumpBtn').addEventListener('click', () => {
  if (activeChatJid) filterSearchByChat(activeChatPhone || document.getElementById('activeChatTitle').textContent || activeChatJid);
});
document.getElementById('chatMediaJumpBtn').addEventListener('click', () => {
  if (activeChatJid) filterMediaByChat(activeChatPhone || document.getElementById('activeChatTitle').textContent || activeChatJid);
});
document.getElementById('chatAskJumpBtn')?.addEventListener('click', () => {
  if (activeChatJid) askAiAboutChat(activeChatPhone || document.getElementById('activeChatTitle').textContent || activeChatJid);
});

// Media Gallery Tab
let mediaOffset = 0;
function mediaDocPreviewHtml(mt, fname) {
  const ext = (fname.includes('.') ? fname.split('.').pop() : mt).toUpperCase().slice(0, 6);
  let cls = 'doc-ext-pill';
  if (ext === 'PDF') cls += ' pdf';
  else if (['CSV', 'XLS', 'XLSX', 'ODS'].includes(ext)) cls += ' sheet';
  else if (mt.includes('audio') || mt === 'voice_note') cls += ' audio';
  return `<div class="doc-thumb-badge"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg><span class="${cls}">${esc(ext || 'FILE')}</span></div>`;
}

async function loadMedia(reset=true) {
  window._mediaLoaded = true;
  if (reset) mediaOffset = 0;
  const f = document.getElementById('mediaFilter').value;
  const chat = document.getElementById('mediaChat').value.trim();
  const scope = document.getElementById('mediaScope').value;
  const q = document.getElementById('mediaQuery').value.trim();
  const statuses = document.getElementById('mediaStatuses').checked ? '1' : '0';
  const fromD = document.getElementById('mediaFrom').value;
  const toD = document.getElementById('mediaTo').value;
  const rows = await apiGet(`/api/media_list?filter=${encodeURIComponent(f)}&chat=${encodeURIComponent(chat)}&scope=${encodeURIComponent(scope)}&q=${encodeURIComponent(q)}&statuses=${statuses}&from=${encodeURIComponent(fromD)}&to=${encodeURIComponent(toD)}&limit=60&offset=${mediaOffset}`);
  const grid = document.getElementById('mediaGrid');
  if (reset) grid.innerHTML = '';
  if (!rows.length && reset) {
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3-3-11 9"/></svg><div>No stored media matching these filters.</div></div>';
  } else {
    grid.insertAdjacentHTML('beforeend', rows.map(m => {
      rememberMsgMeta(m);
      const mt = (m.media_type || '').toLowerCase();
      const mime = (m.media_mimetype || '').toLowerCase();
      const isImg = mt === 'image' || mt === 'sticker' || mime.startsWith('image/');
      const isVid = mt === 'video' || mime.startsWith('video/');
      const fname = m.media_filename || `${m.media_type}_${m.id.slice(0, 8)}`;
      const modalKind = isImg ? 'image' : (isVid ? 'video' : mt);
      let prev = mediaDocPreviewHtml(mt, fname);
      if (isImg) prev = `<img loading="lazy" src="${mediaUrl(m.id)}" /><span class="kind-chip">${esc(mt === 'sticker' ? 'STICKER' : 'IMAGE')}</span>`;
      else if (isVid) prev = videoThumbMarkup(m.id);
      const clickCall = mt === 'document'
        ? `openDocModal(${escArg(m.id)},${escArg(fname)})`
        : `openItemModal(${escArg(m.id)},${escArg(fname)},${escArg(modalKind)})`;
      const filterTarget = m.chat_phone || m.chat_name;
      return `
        <div class="media-tile">
          <div class="media-tile-preview" onclick="${clickCall}">${prev}</div>
          <div class="media-tile-info">
            <div class="media-tile-title" title="${esc(fname)}">${esc(fname)}</div>
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <button class="link-pill" onclick="filterMediaByChat(${escArg(filterTarget)})" title="Filter media by this chat / phone">${esc(m.chat_name)}</button>
              <span class="muted">${fmtBytes(m.media_size)}</span>
            </div>
            ${m.chat_phone ? `<div class="phone-tag">${esc(fmtPhone(m.chat_phone))}</div>` : ''}
            <div class="muted" style="font-size:0.74rem">${esc(m.timestamp_str)}</div>
            <div class="media-bar" style="margin-top:auto;padding-top:4px">
              <a class="ghost-btn" href="${mediaUrl(m.id, true)}">Save</a>
              ${isVid ? `<button class="ghost-btn" onclick="${clickCall}">▶ Play</button>` : ''}
              ${mt === 'document' ? `<button class="ghost-btn" onclick="openDocModal(${escArg(m.id)},${escArg(fname)})">Extract Text</button>` : ''}
              ${WAMATE_CAPS.hasAi ? `<button class="action-pill ai-pill" onclick="askAiAboutMessage(${escArg(m.id)})" title="Ask AI about this file">${_AI_BTN_SVG}<span>Ask AI</span></button>` : ''}
              ${WAMATE_CAPS.hasWa ? `<button class="action-pill wa-pill" onclick="sendToWhatsApp(${escArg(m.id)}, this)" title="Send to your WhatsApp self-chat">${_WA_BTN_SVG}<span>Send</span></button>` : ''}
            </div>
          </div>
        </div>
      `;
    }).join(''));
    mediaOffset += rows.length;
  }
  document.getElementById('mediaSummary').textContent = mediaOffset ? `Showing ${mediaOffset} stored media item${mediaOffset === 1 ? '' : 's'}` : '';
  document.getElementById('mediaMoreBtn').style.display = rows.length === 60 ? 'inline-flex' : 'none';
}
document.getElementById('mediaSearchBtn').addEventListener('click', () => loadMedia(true));
document.getElementById('mediaFilter').addEventListener('change', () => loadMedia(true));
document.getElementById('mediaScope').addEventListener('change', () => loadMedia(true));
document.getElementById('mediaStatuses').addEventListener('change', () => loadMedia(true));
document.getElementById('mediaMoreBtn').addEventListener('click', () => loadMedia(false));
setupDateGroup('media', 'mediaFrom', 'mediaTo', () => loadMedia(true));
document.getElementById('mediaClearBtn').addEventListener('click', () => {
  document.getElementById('mediaFilter').value = 'all';
  document.getElementById('mediaChat').value = '';
  document.getElementById('mediaScope').value = 'all';
  document.getElementById('mediaQuery').value = '';
  document.getElementById('mediaStatuses').checked = true;
  resetDateGroup('media', 'mediaFrom', 'mediaTo');
  loadMedia(true);
});
['mediaChat', 'mediaQuery'].forEach(id => document.getElementById(id).addEventListener('keydown', e => { if (e.key === 'Enter') loadMedia(true); }));

// Vault Tab
async function loadVault() {
  window._vaultLoaded = true;
  const mode = document.getElementById('vaultMode').value;
  const q = document.getElementById('vaultQuery').value.trim();
  const scope = document.getElementById('vaultScope').value;
  const statuses = document.getElementById('vaultStatuses').checked ? '1' : '0';
  const fromD = document.getElementById('vaultFrom').value;
  const toD = document.getElementById('vaultTo').value;
  const rows = await apiGet(`/api/vault?mode=${encodeURIComponent(mode)}&q=${encodeURIComponent(q)}&scope=${encodeURIComponent(scope)}&statuses=${statuses}&from=${encodeURIComponent(fromD)}&to=${encodeURIComponent(toD)}&limit=50`);
  const el = document.getElementById('vaultResults');
  if (!rows.length) {
    el.innerHTML = '<div class="empty-state"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg><div>No matching deleted or edited messages found.</div><div class="muted">Incoming revokes &amp; message edits are preserved here automatically.</div></div>';
    return;
  }
  el.innerHTML = rows.map(r => {
    rememberMsgMeta(r);
    const badge = r.is_deleted && r.is_edited ? '<span class="badge deleted">Deleted &amp; Edited</span>'
      : (r.is_deleted ? `<span class="badge deleted">Deleted ${esc(r.deleted_at)}</span>` : `<span class="badge edited">Edited ${esc(r.edited_at)}</span>`);
    const revs = (r.revisions || []).map(rv => `<div>&bull; <b>${esc(rv.event)}</b> (${esc(rv.timestamp)}) by ${esc(rv.actor)}: ${esc(rv.previous_text || '[Empty]')} &rarr; ${esc(rv.new_text || '[Deleted]')}</div>`).join('');
    return `
      <div class="msg-card">
        <div class="msg-meta">
          <div class="msg-meta-left">
            ${avatarHtml(r.sender || r.chat, r.chat_jid, false, true)}
            <span class="who">${esc(r.sender)}</span>
            <span class="muted">in</span>
            <button class="link-pill" onclick="jumpToChat(${escArg(r.chat_jid)},${escArg(r.chat)})" title="Open this chat">${esc(r.chat)}</button>
            ${badge}
          </div>
          <div class="msg-ts">Sent ${esc(r.sent_at)}</div>
        </div>
        <div class="orig-box ${r.is_deleted ? 'del' : ''}"><strong>Preserved Original:</strong>\n${esc(r.original_text || r.current_text || '[No text]')}</div>
        ${r.is_edited && r.current_text ? `<div class="msg-body" style="margin-top:8px"><strong>Latest Text:</strong> ${esc(r.current_text)}</div>` : ''}
        ${revs ? `<div class="muted" style="margin-top:8px;font-size:0.79rem">${revs}</div>` : ''}
        ${r.media_saved_on_disk ? `<div class="media-bar"><a class="ghost-btn" href="${mediaUrl(r.id, true)}">Saved Media (${esc(r.media_filename || r.media_type)})</a><button class="ghost-btn" onclick="openItemModal(${escArg(r.id)},${escArg(r.media_filename || r.media_type)},${escArg(r.media_type)})">Inspect</button></div>` : ''}
        ${renderMsgActions(r.id)}
      </div>
    `;
  }).join('');
}
document.getElementById('vaultRefreshBtn').addEventListener('click', loadVault);
document.getElementById('vaultMode').addEventListener('change', loadVault);
document.getElementById('vaultScope').addEventListener('change', loadVault);
document.getElementById('vaultStatuses').addEventListener('change', loadVault);
document.getElementById('vaultQuery').addEventListener('keydown', e => { if (e.key === 'Enter') loadVault(); });
setupDateGroup('vault', 'vaultFrom', 'vaultTo', loadVault);
document.getElementById('vaultClearBtn').addEventListener('click', () => {
  document.getElementById('vaultMode').value = 'all';
  document.getElementById('vaultQuery').value = '';
  document.getElementById('vaultScope').value = 'all';
  document.getElementById('vaultStatuses').checked = false;
  resetDateGroup('vault', 'vaultFrom', 'vaultTo');
  loadVault();
});

// Global '/' search shortcut
document.addEventListener('keydown', e => {
  if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) {
    e.preventDefault();
    switchTab('search');
    document.getElementById('searchQuery').focus();
  }
});

// Modals
document.getElementById('previewDialog').addEventListener('close', () => {
  const mb = document.getElementById('modalBody');
  mb.querySelectorAll('video, audio').forEach(el => {
    try { el.pause(); el.removeAttribute('src'); el.load(); } catch (_) {}
  });
  mb.innerHTML = '';
});

function openItemModal(id, title, kind) {
  const dlg = document.getElementById('previewDialog');
  document.getElementById('modalTitle').textContent = title;
  const u = mediaUrl(id);
  const dl = mediaUrl(id, true);
  let body = '';
  if (kind === 'image' || kind === 'sticker') body = `<img src="${u}" style="max-width:100%;border-radius:10px;display:block;margin:0 auto" />`;
  else if (kind === 'video') body = `<video controls autoplay playsinline poster="${videoThumbUrl(id)}" src="${u}" style="width:100%;max-width:100%;max-height:60vh;border-radius:10px;background:#000"></video>`;
  else if (kind === 'audio' || kind === 'voice_note') body = `<audio controls autoplay src="${u}" style="width:100%;margin:12px 0"></audio>`;
  else body = `<iframe src="${u}" style="width:100%;height:60vh;border:none;background:#fff;border-radius:10px"></iframe>`;
  const askBtnHtml = WAMATE_CAPS.hasAi ? `<button type="button" class="action-pill ai-pill" onclick="askAiAboutMessage(${escArg(id)})">${_AI_BTN_SVG}<span>Ask AI</span></button>` : '';
  const waBtnHtml = WAMATE_CAPS.hasWa ? `<button type="button" class="action-pill wa-pill" onclick="sendToWhatsApp(${escArg(id)}, this)">${_WA_BTN_SVG}<span>Send to WA</span></button>` : '';
  document.getElementById('modalBody').innerHTML = `${body}<div class="media-bar"><a class="ghost-btn" href="${dl}">Download Original</a>${askBtnHtml}${waBtnHtml}</div>`;
  dlg.showModal();
}

async function openDocModal(id, title) {
  const dlg = document.getElementById('previewDialog');
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').innerHTML = '<div class="muted">Extracting document contents...</div>';
  dlg.showModal();
  try {
    const res = await apiGet(`/api/doc_preview/${encodeURIComponent(id)}`);
    const text = res.preview || res.ocr_cached || 'No extractable text available.';
    const askBtnHtml = WAMATE_CAPS.hasAi ? `<button type="button" class="action-pill ai-pill" onclick="askAiAboutMessage(${escArg(id)})">${_AI_BTN_SVG}<span>Ask AI</span></button>` : '';
    const waBtnHtml = WAMATE_CAPS.hasWa ? `<button type="button" class="action-pill wa-pill" onclick="sendToWhatsApp(${escArg(id)}, this)">${_WA_BTN_SVG}<span>Send to WA</span></button>` : '';
    document.getElementById('modalBody').innerHTML = `
      <div class="media-bar" style="margin-bottom:10px">
        <a class="ghost-btn" href="${mediaUrl(id)}" target="_blank">Open Raw File</a>
        <a class="ghost-btn" href="${mediaUrl(id, true)}">Download</a>
        ${askBtnHtml}
        ${waBtnHtml}
      </div>
      <pre>${esc(text)}</pre>
    `;
  } catch (e) {
    document.getElementById('modalBody').innerHTML = `<div class="muted">Preview failed: ${esc(e.message)}</div>`;
  }
}

// =========================================================
// Modern Ask AI Chat Experience
// =========================================================
window._aiAnswers = {};
let _aiTurnSeq = 0;
let _aiHistory = [];

function fmtInlineAi(rawLine) {
  const clean = String(rawLine ?? '').replace(/\x00/g, '');
  const codeTokens = [];
  let s = esc(clean).replace(/`([^`\n]+)`/g, (_, code) => {
    const idx = codeTokens.push(`<code>${code}</code>`) - 1;
    return `\x00IC${idx}\x00`;
  });
  s = s.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[\s(>\[])\*([^\s*][^*\n]*?[^\s*]|[^\s*])\*(?=[\s.,;:!?)<\]]|$)/g, '$1<strong>$2</strong>');
  s = s.replace(/(^|[\s(>\[])_([^\s_][^_\n]*?[^\s_]|[^\s_])_(?=[\s.,;:!?)<\]]|$)/g, '$1<em>$2</em>');
  s = s.replace(/(^|[\s(>\[])~([^\s~][^~\n]*?[^\s~]|[^\s~])~(?=[\s.,;:!?)<\]]|$)/g, '$1<del>$2</del>');
  s = s.replace(/\x00IC(\d+)\x00/g, (_, idx) => codeTokens[Number(idx)] || '');
  return s;
}

function renderAiMarkdown(rawText) {
  const text = String(rawText ?? '').replace(/\r\n/g, '\n').trim();
  if (!text) return '<p class="muted">No answer returned.</p>';

  const parts = text.split(/(```[a-zA-Z0-9_-]*\n?[\s\S]*?```)/g);
  let html = '';
  for (let i = 0; i < parts.length; i++) {
    const seg = parts[i];
    if (!seg) continue;
    if (i % 2 === 1) {
      const m = seg.match(/^```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```$/);
      const lang = m ? m[1] : '';
      const body = m ? m[2] : seg;
      html += `<div class="ai-code-wrap">${lang ? `<div class="ai-code-lang">${esc(lang)}</div>` : ''}<pre><code>${esc(body.replace(/^\n+|\n+$/g, ''))}</code></pre></div>`;
      continue;
    }

    const lines = seg.split('\n');
    let listTag = '';
    let paraBuf = [];
    const flushPara = () => {
      if (paraBuf.length) {
        html += `<p>${paraBuf.map(fmtInlineAi).join('<br/>')}</p>`;
        paraBuf = [];
      }
    };
    const closeList = () => {
      if (listTag) { html += `</${listTag}>`; listTag = ''; }
    };

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        flushPara();
        closeList();
        continue;
      }
      const hMatch = trimmed.match(/^(#{1,3})\s+(.+)$/);
      if (hMatch) {
        flushPara();
        closeList();
        const tag = hMatch[1].length <= 2 ? 'h3' : 'h4';
        html += `<${tag}>${fmtInlineAi(hMatch[2])}</${tag}>`;
        continue;
      }
      const ulMatch = line.match(/^\s*(?:\u2022|-|\*)\s+(.+)$/);
      if (ulMatch) {
        flushPara();
        if (listTag && listTag !== 'ul') closeList();
        if (!listTag) { html += '<ul>'; listTag = 'ul'; }
        html += `<li>${fmtInlineAi(ulMatch[1])}</li>`;
        continue;
      }
      const olMatch = line.match(/^\s*\d+\.\s+(.+)$/);
      if (olMatch) {
        flushPara();
        if (listTag && listTag !== 'ol') closeList();
        if (!listTag) { html += '<ol>'; listTag = 'ol'; }
        html += `<li>${fmtInlineAi(olMatch[1])}</li>`;
        continue;
      }
      if (trimmed.startsWith('\u26a0')) {
        flushPara();
        closeList();
        html += `<div class="ai-callout">${fmtInlineAi(trimmed)}</div>`;
        continue;
      }
      closeList();
      paraBuf.push(line);
    }
    flushPara();
    closeList();
  }
  return html;
}

function renderAskWelcomeHtml() {
  const starSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 4.7L18.6 9.6l-4.7 1.9L12 16.2l-1.9-4.7L5.4 9.6l4.7-1.9L12 3z"/><path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z"/></svg>';
  const prompts = [
    { cat: 'Invoices & Bills', q: 'Find all recent invoices, GST bills, or payment amounts from my chats and PDFs' },
    { cat: 'Anti-Delete Vault', q: 'Were any messages deleted for everyone or edited recently? Show originals' },
    { cat: 'Voice & Photos OCR', q: 'What important details were shared in recent voice notes, photos, or screenshots?' },
    { cat: 'Daily Catch-Up', q: 'Summarize key messages, tasks, and pending follow-ups across my chats today' },
  ];
  return `
    <div class="ask-welcome">
      <div class="welcome-emblem">${starSvg}</div>
      <h2>Ask anything across your WhatsApp memory</h2>
      <p>Query your full conversation history, scanned receipts, PDFs &amp; spreadsheets, voice note transcripts, and deleted messages using Gemini tool calling.</p>
      <div class="cap-strip">
        <span class="cap-pill">Chats &amp; Contacts</span>
        <span class="cap-pill">PDF / Excel / CSV</span>
        <span class="cap-pill">CPU + AI Vision OCR</span>
        <span class="cap-pill">Voice Transcripts</span>
        <span class="cap-pill">Anti-Delete Vault</span>
      </div>
      <div class="ask-prompts">
        ${prompts.map(p => `
          <button type="button" class="prompt-card" onclick="useAskPrompt(${escArg(p.q)})">
            <div class="prompt-card-top"><span>${esc(p.cat)}</span><span>&rarr;</span></div>
            <div class="prompt-card-txt">${esc(p.q)}</div>
          </button>
        `).join('')}
      </div>
    </div>
  `;
}

function useAskPrompt(promptText) {
  const inp = document.getElementById('askInput');
  if (!inp) return;
  inp.value = promptText;
  autoGrowAskInput();
  submitAsk();
}

function syncAskScopeUi() {
  const focusVal = (document.getElementById('askChatFocus')?.value || '').trim();
  const clearBtn = document.getElementById('askFocusClearBtn');
  const banner = document.getElementById('askFocusBanner');
  const chip = document.getElementById('askFocusChip');
  if (clearBtn) clearBtn.style.display = focusVal ? 'inline-block' : 'none';
  if (banner && chip) {
    banner.style.display = focusVal ? 'flex' : 'none';
    if (focusVal) chip.textContent = focusVal;
  }
}

function autoGrowAskInput() {
  const inp = document.getElementById('askInput');
  if (!inp) return;
  inp.style.height = 'auto';
  inp.style.height = `${Math.min(inp.scrollHeight, 150)}px`;
}

function copyAiAnswer(turnId, btn) {
  const raw = window._aiAnswers[turnId] || '';
  if (!raw) return;
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(raw).then(() => {
      if (btn) { btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = 'Copy'; }, 1500); }
      showToast('Copied answer to clipboard');
    }).catch(() => {});
  }
}

function renderAskThread(isThinking=false) {
  const out = document.getElementById('askOutput');
  if (!out) return;
  if (!_aiHistory.length && !isThinking) {
    out.innerHTML = renderAskWelcomeHtml();
    return;
  }
  const starSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 4.7L18.6 9.6l-4.7 1.9L12 16.2l-1.9-4.7L5.4 9.6l4.7-1.9L12 3z"/></svg>';
  const cards = _aiHistory.map(item => {
    if (item.role === 'user') {
      const scopeTag = item.chat ? `<span class="filter-chip" style="padding:1px 7px;font-size:0.68rem">Scope: ${esc(item.chat)}</span>` : '';
      const ctxTag = item.contextLabel ? `<span class="filter-chip" style="padding:1px 7px;font-size:0.68rem;border-color:rgba(56,189,248,0.34);background:rgba(56,189,248,0.12);color:#bae6fd">Item: ${esc(item.contextLabel)}</span>` : '';
      return `
        <div class="ai-turn-user">
          <div class="ai-turn-user-meta">${scopeTag}${ctxTag}<span>You &middot; ${esc(item.ts)}</span></div>
          <div class="ai-turn-user-text">${esc(item.text)}</div>
        </div>
      `;
    }
    const latBadge = item.latencyMs ? `<span class="ai-latency">${(item.latencyMs / 1000).toFixed(1)}s</span>` : '';
    return `
      <div class="ai-turn-bot ${item.isError ? 'error' : ''}">
        <div class="ai-bot-hdr">
          <div class="ai-bot-who">
            <span class="ai-mini-badge">${starSvg}</span>
            <span>WAmate AI</span>
            ${latBadge}
          </div>
          <div style="display:flex;gap:6px">
            <button type="button" class="ghost-btn" onclick="copyAiAnswer(${item.id}, this)">Copy</button>
          </div>
        </div>
        <div class="ai-markdown">${item.isError ? `<p style="color:#ff949e">${esc(item.text)}</p>` : renderAiMarkdown(item.text)}</div>
      </div>
    `;
  }).join('');

  const thinkingHtml = isThinking ? `
    <div class="ai-thinking">
      <span class="ai-mini-badge">${starSvg}</span>
      <div class="typing-dots"><span></span><span></span><span></span></div>
      <span id="askThinkLabel" class="muted">Searching chats, OCR receipts &amp; documents...</span>
    </div>
  ` : '';

  out.innerHTML = `<div class="ask-stream-inner">${cards}${thinkingHtml}</div>`;
  out.scrollTop = out.scrollHeight;
}

async function loadAskHistory() {
  if (!WAMATE_CAPS.hasAi) return;
  try {
    const rows = await apiGet('/api/ask/history?limit=80');
    if (!Array.isArray(rows) || !rows.length) return;
    _aiHistory = [];
    window._aiAnswers = {};
    rows.forEach(r => {
      if (r.role === 'user') {
        _aiHistory.push({
          role: 'user',
          text: r.text || '',
          chat: r.chat || '',
          contextLabel: r.context_label || '',
          ts: r.ts || ''
        });
      } else {
        const turnId = ++_aiTurnSeq;
        window._aiAnswers[turnId] = r.text || '';
        _aiHistory.push({
          role: 'bot',
          id: turnId,
          text: r.text || '',
          latencyMs: r.latency_ms || 0,
          isError: !!r.is_error
        });
      }
    });
    renderAskThread(false);
    const st = document.getElementById('askStatus');
    if (st) st.innerHTML = `<span class="live-dot"></span> Restored ${rows.length} saved turn${rows.length === 1 ? '' : 's'} &middot; Ready`;
  } catch (_) {}
}

const askBtn = document.getElementById('askBtn');
if (askBtn) {
  let _thinkTimer = null;
  renderAskThread(false);

  document.getElementById('askItemClearBtn')?.addEventListener('click', () => {
    setAskItemContext(null);
    const inp = document.getElementById('askInput');
    if (inp) inp.placeholder = 'Ask anything about your WhatsApp messages, invoices, PDFs, voice notes, or deleted texts...';
  });

  window.submitAsk = async () => {
    const inp = document.getElementById('askInput');
    const q = inp.value.trim();
    if (!q || askBtn.disabled) return;
    const chatFocus = (document.getElementById('askChatFocus')?.value || '').trim();
    const ctxSnap = _askContext ? { ..._askContext } : null;
    const st = document.getElementById('askStatus');

    const nowTime = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    _aiHistory.push({
      role: 'user',
      text: q,
      chat: chatFocus,
      contextLabel: ctxSnap ? ctxSnap.label : '',
      ts: nowTime
    });
    inp.value = '';
    setAskItemContext(null);
    inp.placeholder = 'Ask anything about your WhatsApp messages, invoices, PDFs, voice notes, or deleted texts...';
    autoGrowAskInput();

    askBtn.disabled = true;
    const t0 = Date.now();
    renderAskThread(true);
    st.innerHTML = '<span class="live-dot"></span> Querying Gemini tools (0.0s)...';
    clearInterval(_thinkTimer);
    _thinkTimer = setInterval(() => {
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      st.innerHTML = `<span class="live-dot"></span> Querying Gemini tools (${elapsed}s)...`;
    }, 200);

    try {
      const payload = { query: q };
      if (chatFocus) payload.chat = chatFocus;
      if (ctxSnap && ctxSnap.id) payload.context_msg_id = ctxSnap.id;
      const res = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

      const answer = data.answer || '';
      const turnId = ++_aiTurnSeq;
      window._aiAnswers[turnId] = answer;
      const ms = data.latency_ms || (Date.now() - t0);
      _aiHistory.push({ role: 'bot', id: turnId, text: answer, latencyMs: ms, isError: false });
      st.innerHTML = `<span class="live-dot"></span> Done in ${(ms / 1000).toFixed(1)}s &middot; Multi-turn memory active`;
    } catch (e) {
      const turnId = ++_aiTurnSeq;
      _aiHistory.push({ role: 'bot', id: turnId, text: e.message, latencyMs: 0, isError: true });
      st.textContent = `Error: ${e.message}`;
    } finally {
      clearInterval(_thinkTimer);
      askBtn.disabled = false;
      renderAskThread(false);
      inp.focus();
    }
  };

  askBtn.addEventListener('click', window.submitAsk);
  const askInp = document.getElementById('askInput');
  askInp.addEventListener('input', autoGrowAskInput);
  askInp.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      window.submitAsk();
    }
  });

  document.getElementById('askChatFocus')?.addEventListener('input', syncAskScopeUi);
  document.getElementById('askFocusClearBtn')?.addEventListener('click', () => {
    const fc = document.getElementById('askChatFocus');
    if (fc) { fc.value = ''; syncAskScopeUi(); }
  });
  document.getElementById('askClearBtn')?.addEventListener('click', async () => {
    _aiHistory = [];
    window._aiAnswers = {};
    setAskItemContext(null);
    renderAskThread(false);
    document.getElementById('askStatus').innerHTML = '<span class="live-dot"></span> New conversation &middot; Memory reset';
    try {
      await fetch('/api/ask/clear', { method: 'POST' });
    } catch (_) {}
    showToast('AI conversation reset');
    document.getElementById('askInput')?.focus();
  });
}

// ==========================================
// Broadcast, Roles, Scheduling & Excel Engine
// ==========================================
window._cachedRoles = [];
window._sendSpecificRecipients = [];
window._schedSpecificRecipients = [];
window._sendAttachData = null;
window._schedAttachData = null;
window._contactsPage = 0;
window._groupsPage = 0;
window._historyPage = 0;
window._bcastInitialized = false;
const BCAST_PAGE_SIZE = 50;

async function fetchCachedRoles(force = false) {
  if (window._cachedRoles.length && !force) return window._cachedRoles;
  try {
    const res = await fetch('/api/roles');
    const data = await res.json();
    window._cachedRoles = Array.isArray(data.roles) ? data.roles : [];
    syncRoleFilters();
  } catch (e) {
    console.error('Failed fetching roles:', e);
  }
  return window._cachedRoles;
}

function syncRoleFilters() {
  const roles = window._cachedRoles;
  ['contactsRoleFilter', 'groupsRoleFilter'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const cur = el.value;
    el.innerHTML = '<option value="">All Roles</option>' +
      roles.map(r => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join('');
    if (cur && roles.some(r => r.name === cur)) el.value = cur;
  });
}

function switchBcastSubTab(subId) {
  document.querySelectorAll('.bcast-sub-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.sub === subId));
  document.querySelectorAll('.bcast-sub-panel').forEach(p => p.classList.toggle('active', p.id === `bcast-panel-${subId}`));
  if (subId === 'send') initSendTab();
  else if (subId === 'schedule') initScheduleTab();
  else if (subId === 'contacts') loadContactsList(0);
  else if (subId === 'groups') loadGroupsList(0);
  else if (subId === 'roles') loadRolesManager();
  else if (subId === 'history') loadCampaignHistory(0);
}

function initBroadcastTab() {
  if (!window._bcastInitialized) {
    window._bcastInitialized = true;
    fetchCachedRoles().then(() => {
      switchBcastSubTab('send');
    });
  }
}

function renderRolePills(containerId, selectedList = [], onChange = null) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const roles = window._cachedRoles || [];
  if (!roles.length) {
    el.innerHTML = '<span class="bcast-hint" style="font-size:12px;color:var(--muted)">No roles created yet. Add one in the Roles tab.</span>';
    return;
  }
  el.innerHTML = roles.map(r => {
    const isSel = selectedList.includes(r.name);
    return `<button type="button" class="role-pill-btn ${isSel ? 'selected' : ''}" style="--pill-color:${esc(r.color || '#00c896')}" data-role="${esc(r.name)}" onclick="toggleRolePill(this, '${containerId}', ${onChange ? escArg(onChange.name || '') : "''"})">
      <span class="role-pill-dot"></span>
      <span>${esc(r.name)}</span>
    </button>`;
  }).join('');
}

function toggleRolePill(btn, containerId, callbackName) {
  btn.classList.toggle('selected');
  if (callbackName && typeof window[callbackName] === 'function') {
    window[callbackName]();
  } else if (containerId === 'sendRolePills') {
    updateSendPreview();
  } else if (containerId === 'schedRolePills') {
    updateSchedPreview();
  }
}

function selectAllRoles(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.querySelectorAll('.role-pill-btn').forEach(b => b.classList.add('selected'));
}

function clearAllRoles(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.querySelectorAll('.role-pill-btn').forEach(b => b.classList.remove('selected'));
}

function getSelectedRoles(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return [];
  const selected = [];
  el.querySelectorAll('.role-pill-btn.selected').forEach(b => {
    if (b.dataset.role) selected.push(b.dataset.role);
  });
  return selected;
}

function setupSpecificAutocomplete(inputId, dropdownId, chipsContainerId, targetArrayName, onUpdate) {
  const inp = document.getElementById(inputId);
  const dd = document.getElementById(dropdownId);
  if (!inp || !dd) return;

  inp.addEventListener('input', debounce(async () => {
    const q = inp.value.trim();
    if (!q) { dd.style.display = 'none'; return; }
    try {
      const res = await fetch(`/api/recipients/lookup?q=${encodeURIComponent(q)}`);
      const data = await res.json();
      const list = data.results || [];
      if (!list.length) {
        dd.innerHTML = '<div class="bcast-ac-item" style="color:var(--muted);cursor:default">No contacts or groups found</div>';
        dd.style.display = 'block';
        return;
      }
      dd.innerHTML = list.map(item => {
        const title = esc(item.name || item.push_name || item.phone || item.jid);
        const sub = esc(item.phone || item.jid);
        const badge = item.is_group ? '<span class="stat-pill" style="font-size:10px;">Group</span>' : '<span class="stat-pill" style="font-size:10px;">DM</span>';
        return `<div class="bcast-ac-item" onclick="addSpecificRecipient('${targetArrayName}', '${chipsContainerId}', ${escArg(item.jid)}, ${escArg(item.name || item.phone || item.jid)}, ${escArg(item.phone || '')}, ${escArg(inputId)}, ${escArg(dropdownId)}, ${onUpdate ? escArg(onUpdate.name || '') : "''"})">
          <div><strong>${title}</strong> <span style="font-size:11px;color:var(--muted)">(${sub})</span></div>
          ${badge}
        </div>`;
      }).join('');
      dd.style.display = 'block';
    } catch (_) {}
  }, 220));

  document.addEventListener('click', (e) => {
    if (!inp.contains(e.target) && !dd.contains(e.target)) {
      dd.style.display = 'none';
    }
  });
}

function addSpecificRecipient(arrName, containerId, jid, name, phone, inputId, ddId, cbName) {
  const arr = window[arrName];
  if (!arr.some(x => x.jid === jid)) {
    arr.push({ jid, name, phone });
    renderSpecificChips(containerId, arrName, cbName);
    if (cbName && typeof window[cbName] === 'function') window[cbName]();
  }
  const inp = document.getElementById(inputId);
  if (inp) inp.value = '';
  const dd = document.getElementById(ddId);
  if (dd) dd.style.display = 'none';
}

function removeSpecificRecipient(arrName, containerId, jid, cbName) {
  window[arrName] = window[arrName].filter(x => x.jid !== jid);
  renderSpecificChips(containerId, arrName, cbName);
  if (cbName && typeof window[cbName] === 'function') window[cbName]();
}

function renderSpecificChips(containerId, arrName, cbName) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const arr = window[arrName] || [];
  el.innerHTML = arr.map(item => `
    <span class="bcast-chip">
      <span>${esc(item.name || item.phone || item.jid)}</span>
      <button type="button" class="bcast-chip-del" onclick="removeSpecificRecipient('${arrName}', '${containerId}', ${escArg(item.jid)}, ${escArg(cbName || '')})">&times;</button>
    </span>
  `).join('');
}

function handleFileSelect(inputEl, previewId, key) {
  const preview = document.getElementById(previewId);
  const file = inputEl.files && inputEl.files[0];
  if (!file) {
    window[key] = null;
    if (preview) { preview.style.display = 'none'; preview.innerHTML = ''; }
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const b64 = String(reader.result).split(',')[1];
    window[key] = {
      filename: file.name,
      mimetype: file.type || 'application/octet-stream',
      data: b64,
      size: file.size,
    };
    if (preview) {
      preview.style.display = 'flex';
      preview.innerHTML = `
        <span>📎 <strong>${esc(file.name)}</strong> (${fmtBytes(file.size)})</span>
        <button type="button" class="ghost-btn-sm" onclick="clearFileAttachment('${inputEl.id}', '${previewId}', '${key}')">Remove</button>
      `;
    }
  };
  reader.readAsDataURL(file);
}

function clearFileAttachment(inputId, previewId, key) {
  const inp = document.getElementById(inputId);
  if (inp) inp.value = '';
  const preview = document.getElementById(previewId);
  if (preview) { preview.style.display = 'none'; preview.innerHTML = ''; }
  window[key] = null;
}

function updateCharCount(textareaId, counterId) {
  const ta = document.getElementById(textareaId);
  const el = document.getElementById(counterId);
  if (ta && el) el.textContent = `${ta.value.length} characters`;
}

function toggleRecipientChips(listId) {
  const el = document.getElementById(listId);
  if (!el) return;
  el.style.display = (el.style.display === 'none' || !el.style.display) ? 'flex' : 'none';
}

async function updateSendPreview() {
  const audEl = document.querySelector('input[name="sendAudience"]:checked');
  const audience = audEl ? audEl.value : 'both';
  const roles = getSelectedRoles('sendRolePills');
  try {
    const res = await fetch(`/api/campaigns/preview?roles=${encodeURIComponent(roles.join(','))}&audience=${encodeURIComponent(audience)}`);
    const data = await res.json();
    let recipients = Array.isArray(data.recipients) ? [...data.recipients] : [];
    
    // Merge specific recipients without duplicates
    const seen = new Set(recipients.map(r => r.jid));
    window._sendSpecificRecipients.forEach(s => {
      if (!seen.has(s.jid)) {
        recipients.push({ jid: s.jid, name: s.name, phone: s.phone, is_group: s.jid.includes('@g.us') });
        seen.add(s.jid);
      }
    });

    const total = recipients.length;
    const contactsCount = recipients.filter(r => !r.is_group).length;
    const groupsCount = recipients.filter(r => r.is_group).length;

    const badge = document.getElementById('sendTargetBadge');
    if (badge) {
      badge.textContent = `Targeting ${total}`;
      badge.className = `badge-status ${total > 0 ? 'badge-sent' : 'badge-pending'}`;
    }

    const summary = document.getElementById('sendPreviewSummary');
    if (summary) {
      summary.textContent = `${total} recipient(s) selected (${contactsCount} contacts, ${groupsCount} groups)`;
    }

    const list = document.getElementById('sendPreviewList');
    if (list) {
      list.innerHTML = recipients.map(r => `
        <span class="stat-pill" style="font-size:11px;" title="${esc(r.jid)}">
          ${r.is_group ? '👥' : '👤'} ${esc(r.name || r.phone || r.jid)}
        </span>
      `).join('');
    }
  } catch (e) {
    console.error('Failed to update recipient preview:', e);
  }
}

function initSendTab() {
  fetchCachedRoles().then(() => {
    renderRolePills('sendRolePills', [], updateSendPreview);
    setupSpecificAutocomplete('sendSpecificInput', 'sendSpecificDropdown', 'sendSpecificChips', '_sendSpecificRecipients', updateSendPreview);
    updateSendPreview();
  });
}

async function submitBroadcastSend() {
  const btn = document.getElementById('sendBroadcastBtn');
  const msgInput = document.getElementById('sendMessageText');
  const message = (msgInput ? msgInput.value : '').trim();
  const audEl = document.querySelector('input[name="sendAudience"]:checked');
  const audience = audEl ? audEl.value : 'both';
  const roles = getSelectedRoles('sendRolePills');
  const attachment = window._sendAttachData;

  if (!message && !attachment) {
    showToast('Please enter a message or select an attachment to broadcast.');
    return;
  }

  const minD = parseFloat(document.getElementById('sendDelayMin')?.value || '1.0');
  const maxD = parseFloat(document.getElementById('sendDelayMax')?.value || '2.5');

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="live-dot"></span> Sending broadcast to WhatsApp...';
  }

  try {
    const res = await fetch('/api/campaigns/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        audience,
        roles,
        specific_recipients: window._sendSpecificRecipients,
        message,
        attachment,
        delay_min: minD,
        delay_max: maxD,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
    
    const summary = data.result || {};
    showToast(`Broadcast completed: ${summary.sent || 0} sent, ${summary.failed || 0} failed in ${(summary.duration || 0).toFixed(1)}s`);
    if (msgInput) msgInput.value = '';
    clearFileAttachment('sendFileInput', 'sendFilePreview', '_sendAttachData');
    window._sendSpecificRecipients = [];
    renderSpecificChips('sendSpecificChips', '_sendSpecificRecipients', 'updateSendPreview');
    updateSendPreview();
  } catch (e) {
    showToast(`Broadcast failed: ${e.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg><span>Send Broadcast Now</span>';
    }
  }
}

function initScheduleTab() {
  fetchCachedRoles().then(() => {
    renderRolePills('schedRolePills', [], updateSchedPreview);
    setupSpecificAutocomplete('schedSpecificInput', 'schedSpecificDropdown', 'schedSpecificChips', '_schedSpecificRecipients', updateSchedPreview);
    loadScheduledQueue();
    const now = new Date(Date.now() + 3600000);
    const pad = n => String(n).padStart(2, '0');
    const localIso = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
    const inp = document.getElementById('schedTimeInput');
    if (inp && !inp.value) inp.value = localIso;
  });
}

function updateSchedPreview() {}

async function submitScheduleCampaign() {
  const timeInput = document.getElementById('schedTimeInput');
  const schedTime = (timeInput ? timeInput.value : '').trim();
  const msgInput = document.getElementById('schedMessageText');
  const message = (msgInput ? msgInput.value : '').trim();
  const audEl = document.querySelector('input[name="schedAudience"]:checked');
  const audience = audEl ? audEl.value : 'both';
  const roles = getSelectedRoles('schedRolePills');
  const attachment = window._schedAttachData;

  if (!schedTime) {
    showToast('Please select a date and time for the scheduled campaign.');
    return;
  }
  if (!message && !attachment) {
    showToast('Please enter a message or choose an attachment to schedule.');
    return;
  }

  const btn = document.getElementById('schedSubmitBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Scheduling...'; }

  try {
    const res = await fetch('/api/scheduled/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scheduled_time: schedTime,
        target_audience: audience,
        roles,
        specific_recipients: window._schedSpecificRecipients,
        message,
        attachment,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);

    showToast('Campaign scheduled successfully!');
    if (msgInput) msgInput.value = '';
    clearFileAttachment('schedFileInput', 'schedFilePreview', '_schedAttachData');
    window._schedSpecificRecipients = [];
    renderSpecificChips('schedSpecificChips', '_schedSpecificRecipients', null);
    loadScheduledQueue();
  } catch (e) {
    showToast(`Scheduling failed: ${e.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg><span>Schedule Campaign</span>';
    }
  }
}

async function loadScheduledQueue() {
  const container = document.getElementById('schedQueueTableContainer');
  if (!container) return;
  try {
    const res = await fetch('/api/scheduled?status=all');
    const data = await res.json();
    const items = data.campaigns || [];
    if (!items.length) {
      container.innerHTML = '<div class="bcast-empty" style="padding:28px;text-align:center;color:var(--muted)">No scheduled campaigns in queue.</div>';
      return;
    }
    container.innerHTML = `
      <table class="bcast-table">
        <thead>
          <tr>
            <th>Scheduled For</th>
            <th>Audience / Roles</th>
            <th>Message</th>
            <th>Attachment</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${items.map(item => {
            const statusClass = `badge-${item.status || 'pending'}`;
            const isPending = (item.status === 'pending');
            const hasAtt = item.attachment_filename ? `📎 ${esc(item.attachment_filename)}` : '—';
            const rolesStr = (item.roles || []).join(', ') || 'All';
            const msgSnippet = esc((item.message || '').slice(0, 48)) || '(attachment only)';
            return `
              <tr>
                <td style="white-space:nowrap;font-weight:600;">${esc(item.scheduled_time || '')}</td>
                <td><span class="stat-pill" style="font-size:11px;">${esc(item.target_audience || 'both')}</span> <span style="font-size:11px;color:var(--muted)">(${esc(rolesStr)})</span></td>
                <td>${msgSnippet}</td>
                <td style="font-size:11px;">${hasAtt}</td>
                <td><span class="badge-status ${statusClass}">${esc(item.status || 'pending')}</span></td>
                <td style="white-space:nowrap;">
                  ${isPending ? `<button type="button" class="primary-btn-sm" onclick="sendScheduledNow(${item.id})">Send Now</button> <button type="button" class="ghost-btn-sm" onclick="cancelScheduled(${item.id})">Cancel</button>` : ''}
                  <button type="button" class="ghost-btn-sm" style="color:var(--danger)" onclick="deleteScheduled(${item.id})">Delete</button>
                </td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;
  } catch (e) {
    container.innerHTML = `<div class="bcast-empty" style="color:var(--danger)">Error loading queue: ${esc(e.message)}</div>`;
  }
}

async function sendScheduledNow(campId) {
  if (!confirm('Send this scheduled campaign immediately?')) return;
  try {
    const res = await fetch('/api/scheduled/send_now', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: campId }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'Failed');
    showToast('Scheduled campaign triggered for immediate sending!');
    loadScheduledQueue();
  } catch (e) {
    showToast(`Action failed: ${e.message}`);
  }
}

async function cancelScheduled(campId) {
  try {
    const res = await fetch('/api/scheduled/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: campId }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'Failed');
    showToast('Scheduled campaign cancelled.');
    loadScheduledQueue();
  } catch (e) {
    showToast(`Action failed: ${e.message}`);
  }
}

async function deleteScheduled(campId) {
  if (!confirm('Permanently delete this scheduled campaign?')) return;
  try {
    const res = await fetch('/api/scheduled/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: campId }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'Failed');
    showToast('Scheduled campaign deleted.');
    loadScheduledQueue();
  } catch (e) {
    showToast(`Action failed: ${e.message}`);
  }
}

// Contacts List
async function loadContactsList(page = 0) {
  window._contactsPage = page;
  const container = document.getElementById('contactsTableContainer');
  const pagination = document.getElementById('contactsPagination');
  const searchInput = document.getElementById('contactsSearchInput');
  const roleFilter = document.getElementById('contactsRoleFilter');
  const q = (searchInput ? searchInput.value : '').trim();
  const role = (roleFilter ? roleFilter.value : '').trim();

  await fetchCachedRoles();
  const offset = page * BCAST_PAGE_SIZE;

  try {
    const res = await fetch(`/api/contacts_groups?is_group=0&q=${encodeURIComponent(q)}&role=${encodeURIComponent(role)}&limit=${BCAST_PAGE_SIZE}&offset=${offset}`);
    const data = await res.json();
    const entities = data.entities || [];
    const total = data.total || 0;

    if (!entities.length) {
      container.innerHTML = '<div class="bcast-empty" style="padding:28px;text-align:center;color:var(--muted)">No contacts found. Use the "Add Custom Contact" button above or start a chat on WhatsApp.</div>';
      if (pagination) pagination.innerHTML = '';
      return;
    }

    container.innerHTML = `
      <table class="bcast-table">
        <thead>
          <tr>
            <th>Contact Name</th>
            <th>Phone / JID</th>
            <th>Push Name</th>
            <th>Roles Assigned</th>
            <th>Notes</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${entities.map(c => {
            const roleChips = (c.roles || []).map(rName => {
              const rObj = window._cachedRoles.find(r => r.name === rName);
              const color = rObj ? rObj.color : '#00c896';
              return `<span class="stat-pill" style="--pill-color:${esc(color)};border-color:${esc(color)};color:var(--text);font-size:11px;">${esc(rName)} <button type="button" class="bcast-chip-del" onclick="quickToggleRole(${escArg(c.jid)}, ${escArg(rName)}, false, false)">&times;</button></span>`;
            }).join(' ');

            return `
              <tr>
                <td><strong>${esc(c.name || c.push_name || 'Unnamed')}</strong></td>
                <td><code style="font-size:12px;">${esc(c.phone_number || c.jid || '')}</code></td>
                <td style="color:var(--muted);">${esc(c.push_name || '')}</td>
                <td>
                  <div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;">
                    ${roleChips}
                    <button type="button" class="ghost-btn-sm" style="padding:2px 7px;font-size:11px;" onclick="promptAddRole(${escArg(c.jid)}, false, ${escArg(JSON.stringify(c.roles || []))})">+ Role</button>
                  </div>
                </td>
                <td style="color:var(--muted);font-size:12px;">${esc(c.notes || '')}</td>
                <td>
                  <button type="button" class="ghost-btn-sm" style="color:var(--danger)" onclick="deleteCustomContact(${escArg(c.jid)})">Delete</button>
                </td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;

    if (pagination) {
      const maxPages = Math.ceil(total / BCAST_PAGE_SIZE);
      if (maxPages <= 1) {
        pagination.innerHTML = `<span style="font-size:12px;color:var(--muted)">Showing all ${total} contacts</span>`;
      } else {
        pagination.innerHTML = `
          <span style="font-size:12px;color:var(--muted)">Page ${page + 1} of ${maxPages} (${total} contacts)</span>
          <button type="button" class="ghost-btn-sm" ${page === 0 ? 'disabled' : ''} onclick="loadContactsList(${page - 1})">Prev</button>
          <button type="button" class="ghost-btn-sm" ${page >= maxPages - 1 ? 'disabled' : ''} onclick="loadContactsList(${page + 1})">Next</button>
        `;
      }
    }
  } catch (e) {
    container.innerHTML = `<div class="bcast-empty" style="color:var(--danger)">Error loading contacts: ${esc(e.message)}</div>`;
  }
}

function openAddContactModal() {
  const dlg = document.getElementById('addContactModal');
  if (!dlg) return;
  document.getElementById('modalContactName').value = '';
  document.getElementById('modalContactPhone').value = '';
  document.getElementById('modalContactNotes').value = '';
  fetchCachedRoles().then(() => {
    renderRolePills('modalContactRoles', [], null);
    dlg.showModal();
  });
}

async function saveNewContact() {
  const name = document.getElementById('modalContactName').value.trim();
  const phone = document.getElementById('modalContactPhone').value.trim();
  const notes = document.getElementById('modalContactNotes').value.trim();
  const roles = getSelectedRoles('modalContactRoles');

  if (!name && !phone) {
    showToast('Please enter a name or phone number.');
    return;
  }

  try {
    const res = await fetch('/api/contacts/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, phone_or_jid: phone, roles, notes }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'Failed');
    showToast('Contact saved successfully!');
    document.getElementById('addContactModal').close();
    loadContactsList(0);
  } catch (e) {
    showToast(`Save failed: ${e.message}`);
  }
}

async function deleteCustomContact(jid) {
  if (!confirm(`Delete contact (${jid})?`)) return;
  try {
    const res = await fetch('/api/contacts/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jid }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'Failed');
    showToast('Contact deleted.');
    loadContactsList(window._contactsPage || 0);
  } catch (e) {
    showToast(`Delete failed: ${e.message}`);
  }
}

// Groups List
async function loadGroupsList(page = 0) {
  window._groupsPage = page;
  const container = document.getElementById('groupsTableContainer');
  const pagination = document.getElementById('groupsPagination');
  const searchInput = document.getElementById('groupsSearchInput');
  const roleFilter = document.getElementById('groupsRoleFilter');
  const q = (searchInput ? searchInput.value : '').trim();
  const role = (roleFilter ? roleFilter.value : '').trim();

  await fetchCachedRoles();
  const offset = page * BCAST_PAGE_SIZE;

  try {
    const res = await fetch(`/api/contacts_groups?is_group=1&q=${encodeURIComponent(q)}&role=${encodeURIComponent(role)}&limit=${BCAST_PAGE_SIZE}&offset=${offset}`);
    const data = await res.json();
    const entities = data.entities || [];
    const total = data.total || 0;

    if (!entities.length) {
      container.innerHTML = '<div class="bcast-empty" style="padding:28px;text-align:center;color:var(--muted)">No WhatsApp groups indexed yet. Groups will appear automatically as active messages arrive.</div>';
      if (pagination) pagination.innerHTML = '';
      return;
    }

    container.innerHTML = `
      <table class="bcast-table">
        <thead>
          <tr>
            <th>Group Subject / Name</th>
            <th>Group JID</th>
            <th>Assigned Roles</th>
          </tr>
        </thead>
        <tbody>
          ${entities.map(g => {
            const roleChips = (g.roles || []).map(rName => {
              const rObj = window._cachedRoles.find(r => r.name === rName);
              const color = rObj ? rObj.color : '#00c896';
              return `<span class="stat-pill" style="--pill-color:${esc(color)};border-color:${esc(color)};color:var(--text);font-size:11px;">${esc(rName)} <button type="button" class="bcast-chip-del" onclick="quickToggleRole(${escArg(g.jid)}, ${escArg(rName)}, false, true)">&times;</button></span>`;
            }).join(' ');

            return `
              <tr>
                <td><strong>👥 ${esc(g.name || 'Group')}</strong></td>
                <td><code style="font-size:12px;">${esc(g.jid || '')}</code></td>
                <td>
                  <div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;">
                    ${roleChips}
                    <button type="button" class="ghost-btn-sm" style="padding:2px 7px;font-size:11px;" onclick="promptAddRole(${escArg(g.jid)}, true, ${escArg(JSON.stringify(g.roles || []))})">+ Role</button>
                  </div>
                </td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;

    if (pagination) {
      const maxPages = Math.ceil(total / BCAST_PAGE_SIZE);
      if (maxPages <= 1) {
        pagination.innerHTML = `<span style="font-size:12px;color:var(--muted)">Showing all ${total} groups</span>`;
      } else {
        pagination.innerHTML = `
          <span style="font-size:12px;color:var(--muted)">Page ${page + 1} of ${maxPages} (${total} groups)</span>
          <button type="button" class="ghost-btn-sm" ${page === 0 ? 'disabled' : ''} onclick="loadGroupsList(${page - 1})">Prev</button>
          <button type="button" class="ghost-btn-sm" ${page >= maxPages - 1 ? 'disabled' : ''} onclick="loadGroupsList(${page + 1})">Next</button>
        `;
      }
    }
  } catch (e) {
    container.innerHTML = `<div class="bcast-empty" style="color:var(--danger)">Error loading groups: ${esc(e.message)}</div>`;
  }
}

async function quickToggleRole(jid, roleName, shouldAdd, isGroup) {
  try {
    const rolesRes = await fetch(`/api/contacts_groups?is_group=${isGroup ? 1 : 0}&q=${encodeURIComponent(jid)}&limit=1`);
    const data = await rolesRes.json();
    const ent = (data.entities && data.entities[0]) || { roles: [] };
    let newRoles = [...(ent.roles || [])];
    if (shouldAdd && !newRoles.includes(roleName)) newRoles.push(roleName);
    if (!shouldAdd) newRoles = newRoles.filter(r => r !== roleName);

    await fetch('/api/contacts_groups/update_roles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jid, roles: newRoles }),
    });
    if (isGroup) loadGroupsList(window._groupsPage || 0);
    else loadContactsList(window._contactsPage || 0);
  } catch (e) {
    showToast(`Role update failed: ${e.message}`);
  }
}

function promptAddRole(jid, isGroup, currentRolesJson) {
  const currentRoles = typeof currentRolesJson === 'string' ? JSON.parse(currentRolesJson) : (currentRolesJson || []);
  const available = window._cachedRoles.filter(r => !currentRoles.includes(r.name));
  if (!available.length) {
    showToast('All existing roles are already assigned.');
    return;
  }
  const roleName = prompt(`Choose role to assign:\n\n${available.map(r => r.name).join(', ')}`);
  if (!roleName) return;
  const match = available.find(r => r.name.toLowerCase() === roleName.trim().toLowerCase());
  if (!match) {
    showToast('Role not found. Please type the exact role name or create it first.');
    return;
  }
  quickToggleRole(jid, match.name, true, isGroup);
}

// Roles Manager
async function loadRolesManager() {
  const grid = document.getElementById('rolesManagerGrid');
  if (!grid) return;
  try {
    const roles = await fetchCachedRoles(true);
    if (!roles.length) {
      grid.innerHTML = '<div class="bcast-empty" style="grid-column:1/-1;padding:24px;text-align:center;color:var(--muted)">No custom roles yet. Use the bar above to create one.</div>';
      return;
    }
    grid.innerHTML = roles.map(r => `
      <div class="bcast-role-card">
        <div class="bcast-role-card-left">
          <span class="role-pill-dot" style="--pill-color:${esc(r.color || '#00c896')};width:12px;height:12px;"></span>
          <div>
            <div class="bcast-role-card-name">${esc(r.name)}</div>
            <div class="bcast-role-card-count">${r.assigned_count || 0} assigned</div>
          </div>
        </div>
        <button type="button" class="ghost-btn-sm" style="color:var(--danger)" onclick="deleteRole(${escArg(r.name)})" title="Delete Role">✕</button>
      </div>
    `).join('');
  } catch (e) {
    grid.innerHTML = `<div class="bcast-empty" style="color:var(--danger)">Error loading roles: ${esc(e.message)}</div>`;
  }
}

async function addNewRole() {
  const inp = document.getElementById('newRoleNameInput');
  const col = document.getElementById('newRoleColorInput');
  const name = (inp ? inp.value : '').trim();
  const color = (col ? col.value : '#00c896').trim();

  if (!name) {
    showToast('Please enter a role name.');
    return;
  }

  try {
    const res = await fetch('/api/roles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, color }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'Failed');
    showToast(`Role "${name}" created!`);
    if (inp) inp.value = '';
    loadRolesManager();
  } catch (e) {
    showToast(`Failed creating role: ${e.message}`);
  }
}

async function deleteRole(roleName) {
  if (!confirm(`Delete role "${roleName}"? This will unassign it from all contacts and groups.`)) return;
  try {
    const res = await fetch('/api/roles/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: roleName }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'Failed');
    showToast(`Role "${roleName}" deleted.`);
    loadRolesManager();
  } catch (e) {
    showToast(`Failed deleting role: ${e.message}`);
  }
}

// Campaign History
async function loadCampaignHistory(page = 0) {
  window._historyPage = page;
  const container = document.getElementById('historyTableContainer');
  const pagination = document.getElementById('historyPagination');
  const searchInput = document.getElementById('historySearchInput');
  const q = (searchInput ? searchInput.value : '').trim();
  const offset = page * BCAST_PAGE_SIZE;

  try {
    const res = await fetch(`/api/campaigns/history?q=${encodeURIComponent(q)}&limit=${BCAST_PAGE_SIZE}&offset=${offset}`);
    const data = await res.json();
    const items = data.history || [];

    if (!items.length) {
      container.innerHTML = '<div class="bcast-empty" style="padding:28px;text-align:center;color:var(--muted)">No campaign history recorded yet.</div>';
      if (pagination) pagination.innerHTML = '';
      return;
    }

    container.innerHTML = `
      <table class="bcast-table">
        <thead>
          <tr>
            <th>Date &amp; Time</th>
            <th>Type</th>
            <th>Audience / Roles</th>
            <th>Message</th>
            <th>Targets</th>
            <th>Duration</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${items.map(h => {
            const dateStr = h.started_at ? new Date(h.started_at * 1000).toLocaleString() : '—';
            const rolesStr = (h.roles || []).join(', ') || 'All';
            const msgSnippet = esc((h.message || '').slice(0, 48)) || '(attachment only)';
            const targetsBadge = `<span class="badge-status badge-sent">${h.sent_count || 0} sent</span> ${h.failed_count > 0 ? `<span class="badge-status badge-failed">${h.failed_count} failed</span>` : ''}`;

            return `
              <tr>
                <td style="white-space:nowrap;font-weight:600;">${esc(dateStr)}</td>
                <td><span class="stat-pill" style="font-size:11px;">${esc(h.campaign_type || 'Broadcast')}</span></td>
                <td>${esc(h.target_audience || 'both')} <span style="font-size:11px;color:var(--muted)">(${esc(rolesStr)})</span></td>
                <td>${msgSnippet}</td>
                <td>${targetsBadge}</td>
                <td style="font-size:12px;color:var(--muted);">${(h.duration_sec || 0).toFixed(1)}s</td>
                <td>
                  <button type="button" class="primary-btn-sm" onclick="viewCampaignLogs(${escArg(JSON.stringify(h.logs || []))}, ${escArg(dateStr)})">View Logs</button>
                </td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;

    if (pagination) {
      const maxPages = Math.ceil(total / BCAST_PAGE_SIZE);
      if (maxPages <= 1) {
        pagination.innerHTML = '';
      } else {
        pagination.innerHTML = `
          <button type="button" class="ghost-btn-sm" ${page === 0 ? 'disabled' : ''} onclick="loadCampaignHistory(${page - 1})">Prev</button>
          <span style="font-size:12px;color:var(--muted)">Page ${page + 1}</span>
          <button type="button" class="ghost-btn-sm" ${items.length < BCAST_PAGE_SIZE ? 'disabled' : ''} onclick="loadCampaignHistory(${page + 1})">Next</button>
        `;
      }
    }
  } catch (e) {
    container.innerHTML = `<div class="bcast-empty" style="color:var(--danger)">Error loading history: ${esc(e.message)}</div>`;
  }
}

function viewCampaignLogs(logsJson, dateStr) {
  const dlg = document.getElementById('campaignLogsModal');
  const title = document.getElementById('campaignLogsModalTitle');
  const content = document.getElementById('campaignLogsModalContent');
  if (!dlg || !content) return;

  const logs = typeof logsJson === 'string' ? JSON.parse(logsJson) : (logsJson || []);
  if (title) title.textContent = `Campaign Delivery Logs — ${dateStr}`;

  if (!logs.length) {
    content.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted)">No per-recipient log entries recorded for this campaign.</div>';
  } else {
    content.innerHTML = `
      <table class="bcast-table">
        <thead>
          <tr>
            <th>Recipient</th>
            <th>JID / Phone</th>
            <th>Status</th>
            <th>Error Detail</th>
          </tr>
        </thead>
        <tbody>
          ${logs.map(log => {
            const isOk = (log.status === 'sent');
            const stBadge = isOk ? '<span class="badge-status badge-sent">SENT</span>' : '<span class="badge-status badge-failed">FAILED</span>';
            return `
              <tr>
                <td><strong>${esc(log.name || 'Unknown')}</strong></td>
                <td><code>${esc(log.phone || log.jid || '')}</code></td>
                <td>${stBadge}</td>
                <td style="color:${isOk ? 'var(--muted)' : 'var(--danger)'};font-size:12px;">${esc(log.error || 'Delivered')}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;
  }
  dlg.showModal();
}

// Excel Import
async function importExcelWorkbook() {
  const fileInput = document.getElementById('excelFileInput');
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!file) {
    showToast('Please choose an .xlsx Excel workbook file to upload.');
    return;
  }

  const modeEl = document.querySelector('input[name="excelImportMode"]:checked');
  const mode = modeEl ? modeEl.value : 'merge';
  const btn = document.getElementById('excelImportBtn');

  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Importing workbook...';
  }

  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const b64 = String(reader.result).split(',')[1];
      const res = await fetch('/api/campaigns/import_excel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, data: b64 }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || 'Import failed');

      const counts = data.counts || {};
      showToast(`Excel imported! Roles: ${counts.roles || 0}, Contacts: ${counts.contacts || 0}, Groups: ${counts.groups || 0}`);
      fileInput.value = '';
      fetchCachedRoles(true);
    } catch (e) {
      showToast(`Import error: ${e.message}`);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<svg class="ico-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg><span>Import &amp; Restore Workbook</span>';
      }
    }
  };
  reader.readAsDataURL(file);
}

loadStats();
loadChatSuggestions('');
loadAskHistory();
runSearch(true);
</script>
</body>
</html>
"""


def _build_html_page(has_ai: bool, has_wa: bool = False, has_auth: bool = False) -> str:
    """Return the self-contained WAmate Web Search & Media Browser UI."""
    caps_js = (
        "const WAMATE_CAPS = "
        + json.dumps({"hasAi": bool(has_ai), "hasWa": bool(has_wa), "hasAuth": bool(has_auth)})
        + ";"
    )
    return (
        _HTML_TEMPLATE.replace("__AI_TAB_BTN__", _AI_TAB_BTN if has_ai else "")
        .replace("__AI_PANEL_HTML__", _AI_PANEL_HTML if has_ai else "")
        .replace("__LOGOUT_BTN__", _LOGOUT_BTN_HTML if has_auth else "")
        .replace("__WAMATE_CAPS_JS__", caps_js)
    )


def _generate_excel_workbook(data: dict[str, list[dict[str, Any]]]) -> bytes:
    """Generate a stylized Excel workbook (.xlsx) containing Roles, Contacts, and Groups sheets."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    header_fill = PatternFill(start_color="1A365D", end_color="1A365D", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    # 1. Roles sheet
    ws_roles = wb.active
    ws_roles.title = "Roles"
    ws_roles.append(["Role Name", "Color Hex", "Created At"])
    for cell in ws_roles[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for r in data.get("roles", []):
        ws_roles.append([r.get("name", ""), r.get("color", "#00c896"), r.get("created_at", "")])
    ws_roles.column_dimensions["A"].width = 24
    ws_roles.column_dimensions["B"].width = 16
    ws_roles.column_dimensions["C"].width = 22

    # 2. Contacts sheet
    ws_contacts = wb.create_sheet(title="Contacts")
    ws_contacts.append(["Name", "Phone / JID", "Push Name", "Roles (comma-separated)", "Notes"])
    for cell in ws_contacts[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for c in data.get("contacts", []):
        roles_str = ", ".join(c.get("roles", []))
        ws_contacts.append([
            c.get("name", "") or c.get("push_name", ""),
            c.get("phone_number", "") or c.get("jid", ""),
            c.get("push_name", ""),
            roles_str,
            c.get("notes", ""),
        ])
    ws_contacts.column_dimensions["A"].width = 26
    ws_contacts.column_dimensions["B"].width = 26
    ws_contacts.column_dimensions["C"].width = 22
    ws_contacts.column_dimensions["D"].width = 32
    ws_contacts.column_dimensions["E"].width = 36

    # 3. Groups sheet
    ws_groups = wb.create_sheet(title="Groups")
    ws_groups.append(["Group Name", "Group JID", "Roles (comma-separated)"])
    for cell in ws_groups[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for g in data.get("groups", []):
        roles_str = ", ".join(g.get("roles", []))
        ws_groups.append([
            g.get("name", ""),
            g.get("jid", ""),
            roles_str,
        ])
    ws_groups.column_dimensions["A"].width = 32
    ws_groups.column_dimensions["B"].width = 36
    ws_groups.column_dimensions["C"].width = 32

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _parse_excel_workbook(file_bytes: bytes) -> dict[str, list[dict[str, Any]]]:
    """Parse an uploaded .xlsx workbook for Roles, Contacts, and Groups sheets."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    roles: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []

    if "Roles" in wb.sheetnames:
        ws = wb["Roles"]
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) > 1:
            for row in rows[1:]:
                if not row or not row[0]:
                    continue
                name = str(row[0]).strip()
                color = str(row[1]).strip() if len(row) > 1 and row[1] else "#00c896"
                if name:
                    roles.append({"name": name, "color": color})

    if "Contacts" in wb.sheetnames:
        ws = wb["Contacts"]
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) > 1:
            for row in rows[1:]:
                if not row:
                    continue
                name = str(row[0] or "").strip()
                phone_or_jid = str(row[1] or "").strip()
                push_name = str(row[2] or "").strip() if len(row) > 2 else ""
                roles_raw = str(row[3] or "").strip() if len(row) > 3 else ""
                notes = str(row[4] or "").strip() if len(row) > 4 else ""
                if phone_or_jid or name:
                    r_list = [r.strip() for r in re.split(r"[,;]+", roles_raw) if r.strip()]
                    contacts.append({
                        "name": name,
                        "phone_or_jid": phone_or_jid,
                        "push_name": push_name,
                        "roles": r_list,
                        "notes": notes,
                    })

    if "Groups" in wb.sheetnames:
        ws = wb["Groups"]
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) > 1:
            for row in rows[1:]:
                if not row:
                    continue
                name = str(row[0] or "").strip()
                jid = str(row[1] or "").strip()
                roles_raw = str(row[2] or "").strip() if len(row) > 2 else ""
                if jid or name:
                    r_list = [r.strip() for r in re.split(r"[,;]+", roles_raw) if r.strip()]
                    groups.append({
                        "name": name,
                        "jid": jid,
                        "roles": r_list,
                    })

    return {"roles": roles, "contacts": contacts, "groups": groups}


class WebUIServer:
    """Lightweight, zero-dependency local Web Search & Media Browser UI."""

    def __init__(
        self,
        db: DatabaseManager,
        host: str = "127.0.0.1",
        port: int = 8765,
        password: str = "",
        gemini: Any = None,
        bot_client: Any = None,
        downloads_dir: Path | None = None,
    ):
        self.db = db
        self.host = host or "127.0.0.1"
        self.port = 8765 if port is None else int(port)
        self.password = password or ""
        self.gemini = gemini
        self.bot_client = bot_client
        self.downloads_dir = (downloads_dir or Config.DOWNLOADS_DIR).resolve()
        self.campaign_attachments_dir = (self.downloads_dir.parent / "campaign_attachments").resolve()
        self.campaign_attachments_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir = (self.downloads_dir.parent / "thumbs").resolve()
        self._thumb_sem = threading.Semaphore(2)
        self._thumb_locks_guard = threading.Lock()
        self._thumb_locks: dict[str, threading.Lock] = {}
        self._session_secret = secrets.token_bytes(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """Return True while the HTTP server socket is actively bound and serving."""
        return self._server is not None

    def _create_session_token(self, ttl_sec: int = _SESSION_TTL_SEC) -> str:
        """Create an HMAC-SHA256 signed session cookie token."""
        exp = int(time.time()) + max(60, ttl_sec)
        sig = hmac.new(self._session_secret, str(exp).encode("ascii"), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def _verify_session_token(self, token: str) -> bool:
        """Validate HMAC signature and expiration of a session cookie token."""
        if not token or "." not in token:
            return False
        exp_str, _, sig = token.partition(".")
        if not exp_str.isdigit() or not sig:
            return False
        if int(exp_str) <= int(time.time()):
            return False
        expected = hmac.new(self._session_secret, exp_str.encode("ascii"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    @staticmethod
    def _extract_video_frame_ffmpeg(ffmpeg_bin: str, video_path: Path, out_path: Path) -> bool:
        """Extract one scaled JPEG frame from video_path via ffmpeg, writing atomically to out_path."""
        tmp_path = out_path.with_name(f"{out_path.stem}.{threading.get_ident()}.tmp.jpg")
        try:
            for seek_sec in ("0.5", "0"):
                cmd = [
                    ffmpeg_bin,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    seek_sec,
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(480,iw)':-2",
                    "-q:v",
                    "4",
                    "-y",
                    str(tmp_path),
                ]
                proc = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )
                if proc.returncode == 0 and tmp_path.is_file() and tmp_path.stat().st_size > 0:
                    tmp_path.replace(out_path)
                    return True
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            return False
        except Exception as exc:
            logger.debug("Video thumbnail extraction failed for %s: %s", video_path.name, exc)
            return False
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _get_or_create_video_thumbnail(self, file_path: Path) -> bytes | None:
        """Return cached JPEG thumbnail bytes for a local video file, generating on demand with ffmpeg."""
        try:
            stat = file_path.stat()
        except OSError:
            return None

        key_src = f"{file_path}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
        cache_key = hashlib.sha256(key_src).hexdigest()[:24]
        thumb_path = self.thumbs_dir / f"{cache_key}.jpg"

        if thumb_path.is_file() and thumb_path.stat().st_size > 0:
            return thumb_path.read_bytes()

        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return None

        with self._thumb_locks_guard:
            lock = self._thumb_locks.get(cache_key)
            if lock is None:
                # Bound lock map growth cleanly on long-running servers
                if len(self._thumb_locks) > 512:
                    self._thumb_locks.clear()
                lock = threading.Lock()
                self._thumb_locks[cache_key] = lock

        with lock:
            if thumb_path.is_file() and thumb_path.stat().st_size > 0:
                return thumb_path.read_bytes()
            self.thumbs_dir.mkdir(parents=True, exist_ok=True)
            with self._thumb_sem:
                if not self._extract_video_frame_ffmpeg(ffmpeg_bin, file_path, thumb_path):
                    return None
            return thumb_path.read_bytes() if thumb_path.is_file() else None

    def _build_handler(self):
        parent = self

        class WAmateHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("WebUI: " + format, *args)

            def _send_bytes(
                self,
                status: int,
                content_type: str,
                body: bytes,
                extra_headers: dict[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                if extra_headers:
                    for k, v in extra_headers.items():
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _send_json(
                self, data: Any, status: int = 200, extra_headers: dict[str, str] | None = None
            ) -> None:
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self._send_bytes(
                    status,
                    "application/json; charset=utf-8",
                    payload,
                    extra_headers=extra_headers,
                )

            def _has_valid_basic_auth(self) -> bool:
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Basic "):
                    return False
                try:
                    decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", errors="ignore")
                    _, _, supplied_pw = decoded.partition(":")
                    return hmac.compare_digest(supplied_pw, parent.password)
                except Exception:
                    return False

            def _has_valid_session_cookie(self) -> bool:
                raw_cookie = self.headers.get("Cookie", "")
                if not raw_cookie:
                    return False
                try:
                    jar = cookies.SimpleCookie()
                    jar.load(raw_cookie)
                    morsel = jar.get(_SESSION_COOKIE_NAME)
                    return bool(morsel and parent._verify_session_token(morsel.value))
                except Exception:
                    return False

            def _check_auth(self, allow_login_page: bool = False) -> bool:
                if not parent.password:
                    return True
                if self._has_valid_basic_auth() or self._has_valid_session_cookie():
                    return True
                if allow_login_page:
                    self._send_bytes(
                        HTTPStatus.OK,
                        "text/html; charset=utf-8",
                        _build_login_page().encode("utf-8"),
                        extra_headers={"Cache-Control": "no-store"},
                    )
                    return False
                self._send_bytes(
                    HTTPStatus.UNAUTHORIZED,
                    "text/plain; charset=utf-8",
                    b"Authentication required",
                    extra_headers={"WWW-Authenticate": 'Basic realm="WAmate Second Brain"'},
                )
                return False

            def _resolve_safe_media_path(self, msg_id: str) -> tuple[dict[str, Any], Path] | tuple[None, None]:
                info = parent.db.get_message_media_info(msg_id)
                if not info or not info.get("local_path") or str(info["local_path"]).startswith("["):
                    return None, None
                try:
                    resolved = Path(info["local_path"]).resolve()
                    if not resolved.is_relative_to(parent.downloads_dir) or not resolved.is_file():
                        return None, None
                    return info, resolved
                except Exception:
                    return None, None

            def _serve_media(self, msg_id: str, force_download: bool = False) -> None:
                info, file_path = self._resolve_safe_media_path(msg_id)
                if not info or not file_path:
                    self._send_json({"error": "Media file not found on disk"}, status=404)
                    return

                mime = (info.get("media_mimetype") or "").strip()
                if not mime or mime == "application/octet-stream":
                    guessed, _ = mimetypes.guess_type(str(file_path))
                    mime = guessed or "application/octet-stream"

                raw_name = (info.get("media_filename") or file_path.name).strip()
                safe_name = re.sub(r'["\\\r\n]', "_", raw_name)
                disp_type = "attachment" if force_download else "inline"
                total_size = file_path.stat().st_size

                range_hdr = self.headers.get("Range")
                start = 0
                end = total_size - 1
                status_code = 200
                if range_hdr and total_size > 0 and range_hdr.startswith("bytes="):
                    m = re.match(r"bytes=(\d*)-(\d*)", range_hdr.strip())
                    if m:
                        s_str, e_str = m.groups()
                        if s_str:
                            start = min(int(s_str), total_size - 1)
                        if e_str:
                            end = min(int(e_str), total_size - 1)
                        if not s_str and e_str:
                            start = max(0, total_size - int(e_str))
                            end = total_size - 1
                        if end >= start:
                            status_code = 206

                length = (end - start + 1) if total_size > 0 else 0
                self.send_response(status_code)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Disposition", f'{disp_type}; filename="{safe_name}"')
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "private, max-age=300")
                if status_code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
                self.end_headers()

                if length > 0:
                    with open(file_path, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                qs = urllib.parse.parse_qs(parsed.query)

                if not self._check_auth(allow_login_page=(path in ("/", "/index.html"))):
                    return

                try:
                    if path == "/" or path == "/index.html":
                        html = _build_html_page(
                            has_ai=parent.gemini is not None,
                            has_wa=parent.bot_client is not None,
                            has_auth=bool(parent.password),
                        ).encode("utf-8")
                        self._send_bytes(200, "text/html; charset=utf-8", html)
                        return

                    if path == "/api/stats":
                        stats = parent.db.get_stats()
                        stored_mb = 0.0
                        if parent.downloads_dir.exists():
                            try:
                                stored_mb = round(
                                    sum(f.stat().st_size for f in parent.downloads_dir.glob("*") if f.is_file())
                                    / (1024 * 1024),
                                    2,
                                )
                            except Exception:
                                pass
                        stats["stored_mb"] = stored_mb
                        stats["gemini_available"] = parent.gemini is not None
                        stats["whatsapp_connected"] = parent.bot_client is not None
                        stats["model_name"] = Config.GEMINI_MODEL
                        stats["api_keys_count"] = len(Config.GEMINI_API_KEYS)
                        self._send_json(stats)
                        return

                    if path == "/api/ask/history":
                        limit = _clamp_int(qs.get("limit", ["80"])[0], 80, 1, 200)
                        self._send_json(parent.db.get_web_ai_history(limit=limit))
                        return

                    if path == "/api/chats":
                        q = (qs.get("q", [""])[0] or "").strip()
                        scope = (qs.get("scope", ["all"])[0] or "all").strip()
                        statuses = _bool_param(qs.get("statuses", ["0"])[0])
                        limit = _clamp_int(qs.get("limit", ["80"])[0], 80, 1, 200)
                        offset = _clamp_int(qs.get("offset", ["0"])[0], 0, 0, 50000)
                        self._send_json(
                            parent.db.list_chats(
                                search=q, chat_scope=scope, include_statuses=statuses, limit=limit, offset=offset
                            )
                        )
                        return

                    if path == "/api/chat_suggestions":
                        q = (qs.get("q", [""])[0] or "").strip()
                        limit = _clamp_int(qs.get("limit", ["120"])[0], 120, 1, 250)
                        self._send_json(parent.db.list_chat_suggestions(query=q, limit=limit))
                        return

                    if path == "/api/chat":
                        jid = (qs.get("jid", [""])[0] or "").strip()
                        before = _clamp_int(qs.get("before", ["0"])[0], 0, 0, 10**12)
                        limit = _clamp_int(qs.get("limit", ["80"])[0], 80, 1, 200)
                        self._send_json(parent.db.get_chat_messages_paged(chat_jid=jid, limit=limit, before_ts=before))
                        return

                    if path == "/api/search":
                        q = (qs.get("q", [""])[0] or "").strip()
                        chat = (qs.get("chat", [""])[0] or "").strip()
                        scope = (qs.get("scope", ["all"])[0] or "all").strip()
                        media_type = (qs.get("media_type", ["all"])[0] or "all").strip()
                        statuses = _bool_param(qs.get("statuses", ["0"])[0])
                        date_from = _parse_date_param(qs.get("from", [""])[0], end_of_day=False)
                        date_to = _parse_date_param(qs.get("to", [""])[0], end_of_day=True)
                        limit = _clamp_int(qs.get("limit", ["50"])[0], 50, 1, 200)
                        offset = _clamp_int(qs.get("offset", ["0"])[0], 0, 0, 50000)
                        self._send_json(
                            parent.db.search_messages_web(
                                query=q,
                                chat_jid=chat,
                                chat_scope=scope,
                                media_type=media_type,
                                include_statuses=statuses,
                                date_from=date_from,
                                date_to=date_to,
                                limit=limit,
                                offset=offset,
                            )
                        )
                        return

                    if path == "/api/media_list":
                        flt = (qs.get("filter", ["all"])[0] or "all").strip()
                        q = (qs.get("q", [""])[0] or "").strip()
                        chat = (qs.get("chat", [""])[0] or "").strip()
                        scope = (qs.get("scope", ["all"])[0] or "all").strip()
                        statuses = _bool_param(qs.get("statuses", ["1"])[0])
                        date_from = _parse_date_param(qs.get("from", [""])[0], end_of_day=False)
                        date_to = _parse_date_param(qs.get("to", [""])[0], end_of_day=True)
                        limit = _clamp_int(qs.get("limit", ["60"])[0], 60, 1, 200)
                        offset = _clamp_int(qs.get("offset", ["0"])[0], 0, 0, 50000)
                        self._send_json(
                            parent.db.list_stored_media(
                                media_filter=flt,
                                query=q,
                                chat=chat,
                                chat_scope=scope,
                                include_statuses=statuses,
                                date_from=date_from,
                                date_to=date_to,
                                limit=limit,
                                offset=offset,
                            )
                        )
                        return

                    if path == "/api/vault":
                        mode = (qs.get("mode", ["all"])[0] or "all").strip()
                        q = (qs.get("q", [""])[0] or "").strip()
                        scope = (qs.get("scope", ["all"])[0] or "all").strip()
                        statuses = _bool_param(qs.get("statuses", ["0"])[0])
                        date_from = _parse_date_param(qs.get("from", [""])[0], end_of_day=False)
                        date_to = _parse_date_param(qs.get("to", [""])[0], end_of_day=True)
                        limit = _clamp_int(qs.get("limit", ["50"])[0], 50, 1, 200)
                        self._send_json(
                            parent.db.find_deleted_or_edited_messages(
                                mode=mode,
                                chat_name_or_jid=q,
                                chat_scope=scope,
                                date_from=date_from,
                                date_to=date_to,
                                include_statuses=statuses,
                                limit=limit,
                            )
                        )
                        return

                    if path.startswith("/api/media/"):
                        msg_id = urllib.parse.unquote(path[len("/api/media/"):]).strip("/")
                        force_dl = _bool_param(qs.get("download", ["0"])[0])
                        self._serve_media(msg_id, force_download=force_dl)
                        return

                    if path.startswith("/api/video_thumb/"):
                        msg_id = urllib.parse.unquote(path[len("/api/video_thumb/"):]).strip("/")
                        info, file_path = self._resolve_safe_media_path(msg_id)
                        if not info or not file_path:
                            self._send_json({"error": "Video file not found on disk"}, status=404)
                            return
                        mt = (info.get("media_type") or "").lower()
                        mime = (info.get("media_mimetype") or "").lower()
                        is_vid = (
                            mt == "video"
                            or mime.startswith("video/")
                            or file_path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".3gp", ".avi", ".m4v"}
                        )
                        if not is_vid:
                            self._send_json({"error": "Not a video file"}, status=400)
                            return
                        jpg = parent._get_or_create_video_thumbnail(file_path)
                        if not jpg:
                            self._send_json({"error": "Video thumbnail unavailable"}, status=404)
                            return
                        self._send_bytes(
                            200,
                            "image/jpeg",
                            jpg,
                            extra_headers={"Cache-Control": "private, max-age=86400"},
                        )
                        return

                    if path.startswith("/api/doc_preview/"):
                        msg_id = urllib.parse.unquote(path[len("/api/doc_preview/"):]).strip("/")
                        info, file_path = self._resolve_safe_media_path(msg_id)
                        if not info or not file_path:
                            self._send_json({"error": "Document not stored on disk"}, status=404)
                            return
                        preview = extract_document_text(str(file_path), max_chars=24000)
                        self._send_json({"preview": preview, "ocr_cached": info.get("ocr_cached") or ""})
                        return

                    # Broadcast, Roles, Scheduling & Campaign Endpoints
                    if path == "/api/roles":
                        self._send_json({"roles": parent.db.get_roles()})
                        return

                    if path == "/api/recipients/lookup":
                        q = (qs.get("q", [""])[0] or "").strip()
                        limit = _clamp_int(qs.get("limit", ["20"])[0], 20, 1, 100)
                        self._send_json({"results": parent.db.lookup_recipients_autocomplete(query=q, limit=limit)})
                        return

                    if path == "/api/campaigns/preview":
                        raw_roles = (qs.get("roles", [""])[0] or "").strip()
                        roles_list = [r.strip() for r in raw_roles.split(",") if r.strip()]
                        audience = (qs.get("audience", ["both"])[0] or "both").strip().lower()
                        recipients = parent.db.lookup_recipients_by_roles(roles=roles_list, target_audience=audience)
                        self._send_json({"count": len(recipients), "recipients": recipients})
                        return

                    if path == "/api/scheduled":
                        status = (qs.get("status", ["all"])[0] or "all").strip().lower()
                        self._send_json({"campaigns": parent.db.get_scheduled_campaigns(status=status)})
                        return

                    if path == "/api/campaigns/history":
                        q = (qs.get("q", [""])[0] or "").strip()
                        limit = _clamp_int(qs.get("limit", ["50"])[0], 50, 1, 200)
                        offset = _clamp_int(qs.get("offset", ["0"])[0], 0, 0, 50000)
                        h_res = parent.db.get_campaign_history(query=q, limit=limit, offset=offset)
                        self._send_json({"history": h_res.get("items", []), "total": h_res.get("total", 0)})
                        return

                    if path == "/api/contacts_groups":
                        is_group = _clamp_int(qs.get("is_group", ["0"])[0], 0, 0, 1)
                        q = (qs.get("q", [""])[0] or "").strip()
                        role = (qs.get("role", [""])[0] or "").strip()
                        limit = _clamp_int(qs.get("limit", ["50"])[0], 50, 1, 200)
                        offset = _clamp_int(qs.get("offset", ["0"])[0], 0, 0, 50000)
                        entities, total = parent.db.list_entities_with_roles(
                            is_group=bool(is_group), search=q, role=role, limit=limit, offset=offset
                        )
                        self._send_json({"entities": entities, "total": total})
                        return

                    if path == "/api/campaigns/export_excel":
                        data = parent.db.export_workbook_data()
                        xlsx_bytes = _generate_excel_workbook(data)
                        self._send_bytes(
                            200,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            xlsx_bytes,
                            extra_headers={
                                "Content-Disposition": 'attachment; filename="wamate_contacts_roles.xlsx"',
                                "Cache-Control": "no-cache",
                            },
                        )
                        return

                    self._send_json({"error": "Not found"}, status=404)
                except Exception as exc:
                    logger.exception("WebUI GET error on %s: %s", path, exc)
                    self._send_json({"error": str(exc)}, status=500)

            def _read_json_body(self, max_bytes: int = 16384) -> dict[str, Any] | None:
                try:
                    raw_len = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    raw_len = 0
                if raw_len <= 0 or raw_len > max_bytes:
                    return None
                try:
                    data = json.loads(self.rfile.read(raw_len).decode("utf-8"))
                    return data if isinstance(data, dict) else None
                except Exception:
                    return None

            def _drain_body(self) -> None:
                try:
                    raw_len = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    raw_len = 0
                if 0 < raw_len <= 16384:
                    try:
                        self.rfile.read(raw_len)
                    except Exception:
                        pass

            def do_POST(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path

                if path == "/api/login":
                    payload = self._read_json_body(max_bytes=4096)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON body"}, status=400)
                        return
                    supplied = str(payload.get("password") or "")
                    if not parent.password or hmac.compare_digest(supplied, parent.password):
                        cookie_val = parent._create_session_token() if parent.password else ""
                        headers = (
                            {
                                "Set-Cookie": (
                                    f"{_SESSION_COOKIE_NAME}={cookie_val}; Path=/; "
                                    f"HttpOnly; SameSite=Lax; Max-Age={_SESSION_TTL_SEC}"
                                )
                            }
                            if cookie_val
                            else None
                        )
                        self._send_json({"ok": True}, extra_headers=headers)
                        return
                    self._send_json({"error": "Invalid password"}, status=401)
                    return

                if path == "/api/logout":
                    self._drain_body()
                    self._send_json(
                        {"ok": True},
                        extra_headers={
                            "Set-Cookie": f"{_SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                        },
                    )
                    return

                if not self._check_auth():
                    return

                if path == "/api/send_to_whatsapp":
                    if not parent.bot_client or not hasattr(parent.bot_client, "send_media_by_id_to_self"):
                        self._send_json({"error": "WhatsApp client is not connected in this mode."}, status=503)
                        return
                    payload = self._read_json_body(max_bytes=4096)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON body"}, status=400)
                        return
                    msg_id = str(payload.get("msg_id") or "").strip()
                    if not msg_id:
                        self._send_json({"error": "msg_id is required"}, status=400)
                        return
                    try:
                        ok, detail = parent.bot_client.send_media_by_id_to_self(msg_id)
                        if ok:
                            self._send_json({"ok": True, "detail": detail})
                        else:
                            status_code = 404 if "not found" in detail.lower() else 500
                            self._send_json({"ok": False, "error": detail}, status=status_code)
                    except Exception as exc:
                        logger.exception("WebUI /api/send_to_whatsapp error: %s", exc)
                        self._send_json({"ok": False, "error": str(exc)}, status=500)
                    return

                # Broadcast, Roles, Scheduling & Campaign POST Endpoints
                if path == "/api/roles":
                    payload = self._read_json_body(max_bytes=4096)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON"}, status=400)
                        return
                    name = str(payload.get("name") or "").strip()
                    color = str(payload.get("color") or "#00c896").strip()
                    if not name:
                        self._send_json({"error": "Role name cannot be empty"}, status=400)
                        return
                    parent.db.add_role(name=name, color=color)
                    self._send_json({"ok": True, "roles": parent.db.get_roles()})
                    return

                if path == "/api/roles/delete":
                    payload = self._read_json_body(max_bytes=4096)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON"}, status=400)
                        return
                    name = str(payload.get("name") or "").strip()
                    if not name:
                        self._send_json({"error": "Role name is required"}, status=400)
                        return
                    parent.db.delete_role(name=name)
                    self._send_json({"ok": True, "roles": parent.db.get_roles()})
                    return

                if path == "/api/contacts_groups/update_roles":
                    payload = self._read_json_body(max_bytes=16384)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON"}, status=400)
                        return
                    jid = str(payload.get("jid") or "").strip()
                    roles = payload.get("roles")
                    if not jid:
                        self._send_json({"error": "jid is required"}, status=400)
                        return
                    if not isinstance(roles, list):
                        roles = []
                    parent.db.set_entity_roles(jid=jid, roles=roles)
                    self._send_json({"ok": True, "roles": parent.db.get_entity_roles(jid)})
                    return

                if path == "/api/contacts/create":
                    payload = self._read_json_body(max_bytes=16384)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON"}, status=400)
                        return
                    name = str(payload.get("name") or "").strip()
                    phone = str(payload.get("phone_or_jid") or "").strip()
                    notes = str(payload.get("notes") or "").strip()
                    roles = payload.get("roles") or []
                    if not phone and not name:
                        self._send_json({"error": "Name or phone number is required"}, status=400)
                        return
                    saved = parent.db.save_contact_with_roles(name=name, phone_or_jid=phone, roles=roles, notes=notes)
                    self._send_json({"ok": True, "contact": saved})
                    return

                if path == "/api/contacts/delete":
                    payload = self._read_json_body(max_bytes=4096)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON"}, status=400)
                        return
                    jid = str(payload.get("jid") or "").strip()
                    if not jid:
                        self._send_json({"error": "jid is required"}, status=400)
                        return
                    ok = parent.db.delete_custom_contact(jid)
                    self._send_json({"ok": ok})
                    return

                if path == "/api/campaigns/send":
                    if not parent.bot_client or not hasattr(parent.bot_client, "send_broadcast_message"):
                        self._send_json({"error": "WhatsApp client is not connected in this mode."}, status=503)
                        return
                    payload = self._read_json_body(max_bytes=35 * 1024 * 1024)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON payload or payload exceeds 35MB"}, status=400)
                        return
                    aud = (payload.get("audience") or "both").strip().lower()
                    roles = payload.get("roles") or []
                    specific = payload.get("specific_recipients") or []
                    message = str(payload.get("message") or "").strip()
                    delay_min = float(payload.get("delay_min") or 1.0)
                    delay_max = float(payload.get("delay_max") or 2.0)
                    att = payload.get("attachment")

                    # Look up recipients
                    recipients = parent.db.lookup_recipients_by_roles(roles=roles, target_audience=aud)
                    # Merge specific recipients
                    seen_jids = {r.get("jid") for r in recipients if r.get("jid")}
                    for item in specific:
                        s_jid = (item.get("jid") if isinstance(item, dict) else str(item)).strip()
                        if s_jid and s_jid not in seen_jids:
                            recipients.append({
                                "jid": s_jid,
                                "name": (item.get("name") if isinstance(item, dict) else "") or s_jid,
                                "phone": (item.get("phone") if isinstance(item, dict) else "") or s_jid.split("@")[0],
                                "is_group": "@g.us" in s_jid,
                                "roles": [],
                            })
                            seen_jids.add(s_jid)

                    if not recipients:
                        self._send_json({"error": "No recipients match selected audience and roles."}, status=400)
                        return

                    att_path = ""
                    att_fname = ""
                    att_mime = ""
                    if att and isinstance(att, dict) and att.get("data"):
                        att_fname = str(att.get("filename") or "attachment.bin").strip()
                        att_mime = str(att.get("mimetype") or "application/octet-stream").strip()
                        safe_stem = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", att_fname)
                        disk_path = parent.campaign_attachments_dir / f"{uuid.uuid4().hex}_{safe_stem}"
                        try:
                            b_data = base64.b64decode(att["data"])
                            disk_path.write_bytes(b_data)
                            att_path = str(disk_path)
                        except Exception as e_att:
                            logger.exception("Failed to write campaign attachment: %s", e_att)
                            self._send_json({"error": f"Failed saving attachment: {e_att}"}, status=400)
                            return

                    try:
                        res = parent.bot_client.send_broadcast_message(
                            recipients=recipients,
                            message=message,
                            attachment_path=att_path,
                            attachment_filename=att_fname,
                            attachment_mimetype=att_mime,
                            min_delay=delay_min,
                            max_delay=delay_max,
                        )
                        parent.db.record_campaign_history(
                            campaign_type="Instant Broadcast",
                            message=message,
                            attachment_path=att_path,
                            attachment_filename=att_fname,
                            roles=roles,
                            target_audience=aud,
                            recipients_count=res.get("total", len(recipients)),
                            sent_count=res.get("sent", 0),
                            failed_count=res.get("failed", 0),
                            duration_sec=res.get("duration", 0.0),
                            started_at=res.get("started_at", int(time.time())),
                            finished_at=res.get("finished_at", int(time.time())),
                            logs=res.get("logs", []),
                        )
                        self._send_json({"ok": True, "result": res})
                    except Exception as e_bcast:
                        logger.exception("Broadcast failed: %s", e_bcast)
                        self._send_json({"ok": False, "error": str(e_bcast)}, status=500)
                    return

                if path == "/api/scheduled/create":
                    payload = self._read_json_body(max_bytes=35 * 1024 * 1024)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON"}, status=400)
                        return
                    message = str(payload.get("message") or "").strip()
                    sched_time = str(payload.get("scheduled_time") or "").strip()
                    aud = (payload.get("target_audience") or "both").strip().lower()
                    roles = payload.get("roles") or []
                    specific = payload.get("specific_recipients") or []
                    att = payload.get("attachment")

                    if not sched_time:
                        self._send_json({"error": "Scheduled date and time is required"}, status=400)
                        return

                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(sched_time)
                        sched_ts = int(dt.timestamp())
                    except Exception as e_dt:
                        self._send_json({"error": f"Invalid scheduled_time format ({sched_time}): {e_dt}"}, status=400)
                        return

                    att_path = ""
                    att_fname = ""
                    att_mime = ""
                    if att and isinstance(att, dict) and att.get("data"):
                        att_fname = str(att.get("filename") or "attachment.bin").strip()
                        att_mime = str(att.get("mimetype") or "application/octet-stream").strip()
                        safe_stem = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", att_fname)
                        disk_path = parent.campaign_attachments_dir / f"{uuid.uuid4().hex}_{safe_stem}"
                        try:
                            b_data = base64.b64decode(att["data"])
                            disk_path.write_bytes(b_data)
                            att_path = str(disk_path)
                        except Exception as e_att:
                            self._send_json({"error": f"Failed saving attachment: {e_att}"}, status=400)
                            return

                    camp_id = parent.db.create_scheduled_campaign(
                        message=message,
                        scheduled_time=sched_time,
                        scheduled_ts=sched_ts,
                        target_audience=aud,
                        roles=roles,
                        specific_recipients=specific,
                        attachment_path=att_path,
                        attachment_filename=att_fname,
                        attachment_mimetype=att_mime,
                    )
                    self._send_json({"ok": True, "id": camp_id})
                    return

                if path == "/api/scheduled/cancel":
                    payload = self._read_json_body(max_bytes=4096)
                    camp_id = int(payload.get("id") or 0) if payload else 0
                    if not camp_id:
                        self._send_json({"error": "id is required"}, status=400)
                        return
                    parent.db.cancel_scheduled_campaign(camp_id)
                    self._send_json({"ok": True})
                    return

                if path == "/api/scheduled/delete":
                    payload = self._read_json_body(max_bytes=4096)
                    camp_id = int(payload.get("id") or 0) if payload else 0
                    if not camp_id:
                        self._send_json({"error": "id is required"}, status=400)
                        return
                    parent.db.delete_scheduled_campaign(camp_id)
                    self._send_json({"ok": True})
                    return

                if path == "/api/scheduled/send_now":
                    payload = self._read_json_body(max_bytes=4096)
                    camp_id = int(payload.get("id") or 0) if payload else 0
                    if not camp_id:
                        self._send_json({"error": "id is required"}, status=400)
                        return
                    if parent.bot_client and hasattr(parent.bot_client, "trigger_scheduled_campaign_now"):
                        parent.bot_client.trigger_scheduled_campaign_now(camp_id)
                    self._send_json({"ok": True})
                    return

                if path == "/api/campaigns/import_excel":
                    payload = self._read_json_body(max_bytes=35 * 1024 * 1024)
                    if payload is None:
                        self._send_json({"error": "Invalid JSON or file too large"}, status=400)
                        return
                    mode = str(payload.get("mode") or "merge").strip().lower()
                    raw_b64 = str(payload.get("data") or "").strip()
                    if not raw_b64:
                        self._send_json({"error": "No Excel workbook data received"}, status=400)
                        return
                    try:
                        file_bytes = base64.b64decode(raw_b64)
                        parsed_data = _parse_excel_workbook(file_bytes)
                        counts = parent.db.import_workbook_data(parsed_data, mode=mode)
                        self._send_json({"ok": True, "counts": counts})
                    except Exception as e_imp:
                        logger.exception("Failed to import Excel workbook: %s", e_imp)
                        self._send_json({"ok": False, "error": str(e_imp)}, status=400)
                    return

                if path not in ("/api/ask", "/api/ask/clear"):
                    self._send_json({"error": "Not found"}, status=404)
                    return
                if not parent.gemini:
                    self._send_json({"error": "Gemini AI agent is not active on this Web UI instance."}, status=503)
                    return

                if path == "/api/ask/clear":
                    self._drain_body()
                    cleared = 0
                    if hasattr(parent.gemini, "clear_history"):
                        try:
                            cleared = int(parent.gemini.clear_history() or 0)
                        except Exception as exc:
                            logger.debug("WebUI /api/ask/clear warning: %s", exc)
                    db_cleared = 0
                    try:
                        db_cleared = parent.db.clear_web_ai_history()
                    except Exception as exc:
                        logger.debug("WebUI db AI clear warning: %s", exc)
                    self._send_json({"cleared": cleared, "db_cleared": db_cleared})
                    return

                payload = self._read_json_body(max_bytes=16384)
                if payload is None:
                    self._send_json({"error": "Invalid JSON body or size"}, status=400)
                    return

                query = (payload.get("query") or "").strip()
                if not query:
                    self._send_json({"error": "Query cannot be empty"}, status=400)
                    return

                chat_focus = (payload.get("chat") or "").strip()
                context_msg_id = (payload.get("context_msg_id") or "").strip()
                context_label = ""
                prefixes: list[str] = []
                if chat_focus:
                    prefixes.append(f"[Focus chat scope: prioritize searching conversation/contact '{chat_focus}']")

                if context_msg_id:
                    ctx = parent.db.get_message_context_by_id(context_msg_id)
                    if ctx:
                        who = ctx.get("sender") or ctx.get("chat_name") or "Unknown"
                        when = ctx.get("timestamp_str") or ""
                        context_label = f"{who} ({when})" if when else who
                        ctx_lines = [f"[Attached Message Context (msg_id={context_msg_id})]"]
                        ctx_lines.append(
                            f"Chat: {ctx.get('chat_name', '')} ({ctx.get('chat_phone') or ctx.get('chat_jid', '')})"
                        )
                        ctx_lines.append(f"Sender: {who} at {when}")
                        ctx_lines.append(f"Type: {ctx.get('media_type', 'text')}")
                        if ctx.get("media_filename"):
                            ctx_lines.append(f"Filename: {ctx['media_filename']}")
                        if ctx.get("text"):
                            ctx_lines.append(f"Text/Caption: {str(ctx['text'])[:1500]}")
                        if ctx.get("original_text") and ctx.get("original_text") != ctx.get("text"):
                            ctx_lines.append(f"Preserved Original: {str(ctx['original_text'])[:1500]}")
                        if ctx.get("ocr_cached"):
                            ctx_lines.append(f"Cached OCR/Transcript: {str(ctx['ocr_cached'])[:2500]}")
                        if ctx.get("has_local_media"):
                            ctx_lines.append(
                                "Note: Local media/document file is saved on disk; call analyze_media or search tools if full file inspection is needed."
                            )
                        prefixes.append("\n".join(ctx_lines))

                effective_query = ("\n\n".join(prefixes) + f"\n{query}") if prefixes else query

                try:
                    parent.db.append_web_ai_turn(
                        role="user",
                        content=query,
                        chat_focus=chat_focus,
                        context_msg_id=context_msg_id,
                        context_label=context_label,
                    )
                except Exception as exc:
                    logger.debug("Failed to record web AI user turn: %s", exc)

                if parent.bot_client and hasattr(parent.bot_client, "_begin_query"):
                    parent.bot_client._begin_query()
                t0 = time.monotonic()
                try:
                    answer = parent.gemini.ask(effective_query)
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    try:
                        parent.db.append_web_ai_turn(
                            role="bot",
                            content=answer,
                            chat_focus=chat_focus,
                            context_msg_id=context_msg_id,
                            context_label=context_label,
                            latency_ms=latency_ms,
                            is_error=False,
                        )
                    except Exception as exc:
                        logger.debug("Failed to record web AI bot turn: %s", exc)
                    self._send_json(
                        {"answer": answer, "latency_ms": latency_ms, "context_label": context_label}
                    )
                except Exception as exc:
                    logger.exception("WebUI /api/ask error: %s", exc)
                    try:
                        parent.db.append_web_ai_turn(
                            role="bot",
                            content=str(exc),
                            chat_focus=chat_focus,
                            context_msg_id=context_msg_id,
                            context_label=context_label,
                            is_error=True,
                        )
                    except Exception:
                        pass
                    self._send_json({"error": str(exc)}, status=500)
                finally:
                    if parent.bot_client and hasattr(parent.bot_client, "_end_query"):
                        parent.bot_client._end_query()

        return WAmateHandler

    def start(self) -> bool:
        """Start the HTTP server in a daemon thread; returns True on bind success."""
        if self._server is not None:
            return True
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.password:
            logger.warning(
                "SECURITY WARNING: Web UI is bound to non-localhost (%s:%d) without WEB_UI_PASSWORD! "
                "Set WEB_UI_PASSWORD in .env immediately if exposed beyond localhost/Tailscale.",
                self.host,
                self.port,
            )

        handler_cls = self._build_handler()

        class _ReusableHTTPServer(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            self._server = _ReusableHTTPServer((self.host, self.port), handler_cls)
        except OSError as exc:
            logger.warning("Could not start Web UI on http://%s:%d (%s)", self.host, self.port, exc)
            self._server = None
            return False

        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="WAmateWebUI"
        )
        self._thread.start()
        auth_note = "password-protected" if self.password else "no password (localhost-only safe)"
        logger.info("Web Search & Media Browser UI started at http://%s:%d (%s).", self.host, self.port, auth_note)
        return True

    def stop(self) -> None:
        """Shut down the Web UI HTTP server cleanly."""
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:
                logger.debug("WebUI stop warning: %s", exc)
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
