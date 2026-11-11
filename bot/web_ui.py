import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config
from .database import DatabaseManager
from .utils import extract_document_text

logger = logging.getLogger(__name__)


def _clamp_int(val: str | None, default: int, min_val: int, max_val: int) -> int:
    try:
        parsed = int(val) if val is not None else default
    except (TypeError, ValueError):
        return default
    return max(min_val, min(max_val, parsed))


def _bool_param(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _build_html_page(has_ai: bool) -> str:
    """Return the self-contained WAmate Web Search & Media Browser UI."""
    ai_tab_btn = '<button class="tab-btn" data-tab="ask">🤖 Ask AI</button>' if has_ai else ""
    ai_panel_html = (
        """
        <section id="tab-ask" class="panel">
          <div class="ask-box">
            <div class="ask-hint">Ask questions directly across your WhatsApp chats, stored PDFs/spreadsheets, photos & voice notes using Gemini AI.</div>
            <div class="ask-input-row">
              <input id="askInput" type="text" placeholder="e.g. What is the GST invoice total from Incoses, or did anyone delete a message today?" />
              <button id="askBtn" class="primary-btn">Ask WAmate</button>
            </div>
            <div id="askStatus" class="muted"></div>
            <div id="askOutput" class="ask-output"></div>
          </div>
        </section>
        """
        if has_ai
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>WAmate — Second Brain Explorer</title>
<style>
  :root {{
    --bg: #0b141a;
    --panel: #111b21;
    --card: #202c33;
    --card-hover: #26353d;
    --border: #2a3942;
    --text: #e9edef;
    --muted: #8696a0;
    --accent: #00a884;
    --accent-dim: rgba(0,168,132,0.16);
    --danger: #f15c6d;
    --warn: #f59e0b;
    --bubble-me: #005c4b;
    --bubble-them: #202c33;
  }}
  * {{ box-sizing: border-box; scrollbar-width: thin; scrollbar-color: #2f414b transparent; }}
  ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
  ::-webkit-scrollbar-thumb {{ background: #2f414b; border-radius: 999px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
  }}
  header {{
    background: linear-gradient(180deg, #132027 0%, var(--panel) 100%);
    border-bottom: 1px solid var(--border);
    padding: 11px 18px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    justify-content: space-between;
  }}
  .brand {{
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 700;
    font-size: 1.08rem;
  }}
  .brand span.sub {{
    color: var(--muted);
    font-weight: 400;
    font-size: 0.85rem;
  }}
  .stats-strip {{
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    font-size: 0.77rem;
  }}
  .stat-pill {{
    background: rgba(32,44,51,0.88);
    border: 1px solid var(--border);
    padding: 4px 10px;
    border-radius: 999px;
    color: var(--text);
    white-space: nowrap;
  }}
  .stat-pill b {{ color: var(--accent); }}
  nav.tabs {{
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    padding: 0 18px;
    display: flex;
    gap: 6px;
    overflow-x: auto;
  }}
  .tab-btn {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--muted);
    padding: 11px 14px;
    font-size: 0.92rem;
    cursor: pointer;
    font-weight: 600;
    white-space: nowrap;
    transition: color 0.15s ease, border-color 0.15s ease;
  }}
  .tab-btn:hover {{ color: var(--text); }}
  .tab-btn.active {{
    color: var(--accent);
    border-bottom-color: var(--accent);
  }}
  main {{
    flex: 1;
    overflow: hidden;
    position: relative;
  }}
  .panel {{
    display: none;
    height: 100%;
    overflow-y: auto;
    padding: 16px 18px;
  }}
  .panel.active {{ display: block; }}
  .toolbar {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 11px;
    padding: 10px 12px;
    display: flex;
    flex-wrap: wrap;
    gap: 9px;
    margin-bottom: 14px;
    align-items: center;
  }}
  input[type="text"], input[type="search"], select {{
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 11px;
    border-radius: 8px;
    font-size: 0.9rem;
    outline: none;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }}
  input[type="text"]:focus, input[type="search"]:focus, select:focus {{
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-dim);
  }}
  .grow {{ flex: 1; min-width: 190px; }}
  .chat-filter-box {{ min-width: 195px; flex: 0.75; max-width: 280px; }}
  button.primary-btn {{
    background: var(--accent);
    color: #06211c;
    border: none;
    padding: 8px 15px;
    border-radius: 8px;
    font-weight: 700;
    cursor: pointer;
    font-size: 0.89rem;
    transition: filter 0.15s ease;
  }}
  button.primary-btn:hover {{ filter: brightness(1.08); }}
  button.ghost-btn, a.ghost-btn {{
    background: var(--card);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 11px;
    border-radius: 7px;
    cursor: pointer;
    font-size: 0.81rem;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    transition: background 0.15s ease, border-color 0.15s ease;
  }}
  button.ghost-btn:hover, a.ghost-btn:hover {{ background: var(--card-hover); border-color: #3a4f5a; }}
  button.link-pill {{
    background: var(--accent-dim);
    color: #7ee8cf;
    border: 1px solid rgba(0,168,132,0.32);
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.74rem;
    cursor: pointer;
    font-weight: 600;
    transition: filter 0.15s ease;
  }}
  button.link-pill:hover {{ filter: brightness(1.15); }}
  .scope-pills {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 5px;
  }}
  .scope-btn {{
    background: var(--card);
    color: var(--muted);
    border: 1px solid var(--border);
    padding: 5px 8px;
    border-radius: 7px;
    font-size: 0.77rem;
    font-weight: 600;
    cursor: pointer;
    text-align: center;
  }}
  .scope-btn.active {{
    background: var(--accent-dim);
    color: var(--accent);
    border-color: rgba(0,168,132,0.45);
  }}
  .phone-tag {{
    color: var(--muted);
    font-size: 0.74rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }}
  .empty-state {{
    background: var(--panel);
    border: 1px dashed var(--border);
    border-radius: 12px;
    padding: 28px 18px;
    text-align: center;
    color: var(--muted);
    font-size: 0.9rem;
  }}
  label.check {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--muted);
    font-size: 0.84rem;
    cursor: pointer;
    white-space: nowrap;
  }}
  .results-list {{
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-width: 1120px;
  }}
  .msg-card {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 14px;
    transition: border-color 0.15s ease;
  }}
  .msg-card:hover {{ border-color: #394b56; }}
  .msg-meta {{
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    font-size: 0.82rem;
    color: var(--muted);
  }}
  .msg-meta .who {{ color: var(--accent); font-weight: 600; }}
  .badge {{
    display: inline-block;
    padding: 2px 7px;
    border-radius: 5px;
    font-size: 0.72rem;
    font-weight: 600;
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    margin-left: 4px;
  }}
  .badge.deleted {{ background: rgba(241,92,109,0.16); color: #ff8f9c; border-color: rgba(241,92,109,0.38); }}
  .badge.edited {{ background: rgba(245,158,11,0.15); color: #fbbf24; border-color: rgba(245,158,11,0.35); }}
  .badge.saved {{ background: var(--accent-dim); color: var(--accent); border-color: rgba(0,168,132,0.35); }}
  .msg-body {{
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.45;
    font-size: 0.93rem;
  }}
  .orig-box {{
    margin-top: 8px;
    padding: 8px 10px;
    border-radius: 7px;
    background: rgba(255,255,255,0.03);
    border-left: 3px solid var(--warn);
    font-size: 0.85rem;
    white-space: pre-wrap;
  }}
  .media-bar {{
    margin-top: 9px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }}
  .thumb-img {{
    max-width: 240px;
    max-height: 170px;
    border-radius: 8px;
    border: 1px solid var(--border);
    cursor: pointer;
    display: block;
    margin-top: 8px;
    object-fit: cover;
  }}
  /* Split Chats Layout */
  .chats-layout {{
    display: grid;
    grid-template-columns: 320px 1fr;
    gap: 14px;
    height: calc(100vh - 135px);
  }}
  @media (max-width: 800px) {{
    .chats-layout {{ grid-template-columns: 1fr; height: auto; }}
    .chat-sidebar {{ max-height: 280px; }}
    .chat-view {{ min-height: 420px; }}
  }}
  .chat-sidebar {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}
  .chat-sidebar-top {{
    padding: 10px;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}
  .chat-list {{
    overflow-y: auto;
    flex: 1;
  }}
  .chat-item {{
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
  }}
  .chat-item:hover {{ background: var(--card); }}
  .chat-item.active {{ background: var(--accent-dim); border-left: 3px solid var(--accent); }}
  .chat-item-top {{
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 0.9rem;
    font-weight: 600;
  }}
  .chat-item-sub {{
    color: var(--muted);
    font-size: 0.78rem;
    margin-top: 3px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .chat-view {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}
  .chat-view-header {{
    padding: 11px 14px;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .chat-messages {{
    flex: 1;
    overflow-y: auto;
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}
  .bubble {{
    max-width: 78%;
    padding: 9px 12px;
    border-radius: 10px;
    background: var(--bubble-them);
    align-self: flex-start;
    border: 1px solid var(--border);
  }}
  .bubble.me {{
    background: var(--bubble-me);
    align-self: flex-end;
    border-color: rgba(0,168,132,0.4);
  }}
  .bubble-hdr {{
    font-size: 0.75rem;
    color: #9de1d4;
    margin-bottom: 4px;
    font-weight: 600;
  }}
  .bubble-ts {{
    font-size: 0.7rem;
    color: var(--muted);
    margin-top: 5px;
    text-align: right;
  }}
  /* Media Grid */
  .media-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
  }}
  .media-tile {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: transform 0.14s ease, border-color 0.14s ease;
  }}
  .media-tile:hover {{
    transform: translateY(-2px);
    border-color: rgba(0,168,132,0.52);
  }}
  .media-tile-preview {{
    height: 150px;
    background: #0d171d;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    cursor: pointer;
    position: relative;
  }}
  .media-tile-preview img, .media-tile-preview video {{
    width: 100%;
    height: 100%;
    object-fit: cover;
  }}
  .video-thumb-box {{
    position: relative;
    display: inline-block;
    max-width: 240px;
    margin-top: 8px;
    cursor: pointer;
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid var(--border);
    background: #0d171d;
  }}
  .video-thumb-box img, .video-thumb-box video {{
    display: block;
    max-width: 240px;
    max-height: 170px;
    min-width: 150px;
    min-height: 95px;
    width: 100%;
    object-fit: cover;
  }}
  .video-thumb-box .media-tile-icon {{
    width: 200px;
    height: 115px;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .play-badge {{
    position: absolute;
    inset: 0;
    margin: auto;
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: rgba(0, 0, 0, 0.58);
    border: 1.5px solid rgba(255, 255, 255, 0.8);
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.1rem;
    padding-left: 3px;
    pointer-events: none;
    transition: transform 0.15s ease, background 0.15s ease;
  }}
  .media-tile-preview:hover .play-badge, .video-thumb-box:hover .play-badge {{
    transform: scale(1.08);
    background: rgba(0, 168, 132, 0.88);
    border-color: #fff;
  }}
  .kind-chip {{
    position: absolute;
    right: 7px;
    bottom: 7px;
    background: rgba(0, 0, 0, 0.72);
    color: #e9edef;
    font-size: 0.68rem;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 4px;
    pointer-events: none;
    letter-spacing: 0.03em;
  }}
  .media-tile-icon {{
    font-size: 2.3rem;
  }}
  .media-tile-info {{
    padding: 10px;
    font-size: 0.8rem;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }}
  .media-tile-title {{
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .muted {{ color: var(--muted); font-size: 0.85rem; }}
  .load-more-wrap {{ margin: 14px 0; }}
  /* Modal */
  dialog {{
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 12px;
    max-width: 820px;
    width: 92vw;
    max-height: 88vh;
    padding: 16px;
  }}
  dialog::backdrop {{ background: rgba(0,0,0,0.72); }}
  .modal-hdr {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px;
  }}
  .modal-body {{
    overflow-y: auto;
    max-height: 70vh;
  }}
  .modal-body pre {{
    background: var(--bg);
    border: 1px solid var(--border);
    padding: 12px;
    border-radius: 8px;
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 0.84rem;
    line-height: 1.45;
  }}
  .ask-box {{ max-width: 860px; display: flex; flex-direction: column; gap: 12px; }}
  .ask-input-row {{ display: flex; gap: 10px; }}
  .ask-input-row input {{ flex: 1; }}
  .ask-output {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
    white-space: pre-wrap;
    line-height: 1.5;
    min-height: 120px;
  }}
</style>
</head>
<body>
<header>
  <div class="brand">
    <span>🧠 WAmate</span>
    <span class="sub">Second Brain Explorer</span>
  </div>
  <div id="statsStrip" class="stats-strip">Loading stats...</div>
</header>
<nav class="tabs">
  <button class="tab-btn active" data-tab="search">🔍 Search</button>
  <button class="tab-btn" data-tab="chats">💬 Chats</button>
  <button class="tab-btn" data-tab="media">🖼️ Media &amp; Docs</button>
  <button class="tab-btn" data-tab="vault">🛡️ Anti-Delete Vault</button>
  {ai_tab_btn}
</nav>
<main>
  <datalist id="chatSuggestList"></datalist>

  <!-- Search Panel -->
  <section id="tab-search" class="panel active">
    <div class="toolbar">
      <input id="searchQuery" class="grow" type="search" placeholder="Search messages, OCR text, documents, links, voice transcripts... (Press /)" />
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
      <label class="check"><input id="searchStatuses" type="checkbox" /> Include Statuses</label>
      <button id="searchBtn" class="primary-btn">Search</button>
      <button id="searchClearBtn" class="ghost-btn" title="Reset Search Filters">Clear</button>
    </div>
    <div id="searchResults" class="results-list"></div>
    <div class="load-more-wrap"><button id="searchMoreBtn" class="ghost-btn" style="display:none">Load More</button></div>
  </section>

  <!-- Chats Panel -->
  <section id="tab-chats" class="panel">
    <div class="chats-layout">
      <div class="chat-sidebar">
        <div class="chat-sidebar-top">
          <input id="chatFilterInput" type="search" placeholder="Filter chat name or phone (+91...)" />
          <div class="scope-pills" id="chatScopePills">
            <button type="button" class="scope-btn active" data-scope="all">All</button>
            <button type="button" class="scope-btn" data-scope="dm">1-on-1 DMs</button>
            <button type="button" class="scope-btn" data-scope="group">Groups</button>
          </div>
          <label class="check"><input id="chatIncludeStatuses" type="checkbox" /> Show Status Broadcasts</label>
        </div>
        <div id="chatList" class="chat-list"></div>
      </div>
      <div class="chat-view">
        <div class="chat-view-header">
          <div style="min-width:0">
            <div id="activeChatTitle">Select a chat from the left</div>
            <div id="activeChatSub" class="phone-tag"></div>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button id="chatSearchJumpBtn" class="ghost-btn" style="display:none">🔍 Search Chat</button>
            <button id="chatMediaJumpBtn" class="ghost-btn" style="display:none">🖼️ Media</button>
            <button id="loadOlderBtn" class="ghost-btn" style="display:none">Load Older</button>
          </div>
        </div>
        <div id="chatMessages" class="chat-messages">
          <div class="empty-state">Click any conversation to inspect message history, phone numbers, saved media &amp; cached AI/OCR text.</div>
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
      <label class="check"><input id="mediaStatuses" type="checkbox" checked /> Include Statuses</label>
      <button id="mediaSearchBtn" class="primary-btn">Apply</button>
      <button id="mediaClearBtn" class="ghost-btn" title="Reset Media Filters">Clear</button>
    </div>
    <div id="mediaGrid" class="media-grid"></div>
    <div class="load-more-wrap"><button id="mediaMoreBtn" class="ghost-btn" style="display:none">Load More</button></div>
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
      <button id="vaultRefreshBtn" class="primary-btn">Refresh Vault</button>
      <button id="vaultClearBtn" class="ghost-btn" title="Reset Vault Filters">Clear</button>
    </div>
    <div id="vaultResults" class="results-list"></div>
  </section>

  {ai_panel_html}
</main>

<dialog id="previewDialog">
  <div class="modal-hdr">
    <strong id="modalTitle">Preview</strong>
    <button class="ghost-btn" onclick="document.getElementById('previewDialog').close()">Close</button>
  </div>
  <div id="modalBody" class="modal-body"></div>
</dialog>

<script>
const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const fmtBytes = (b) => {{
  if (!b) return '';
  const u = ['B','KB','MB','GB'];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) {{ v /= 1024; i++; }}
  return `${{v.toFixed(i === 0 ? 0 : 1)}} ${{u[i]}}`;
}};

const fmtPhone = (p) => p ? `+${{String(p).replace(/^\\+/, '')}}` : '';

function debounce(fn, ms=220) {{
  let timer = null;
  return (...args) => {{
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  }};
}}

async function apiGet(url) {{
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
  return res.json();
}}

// Tabs & Cross-Tab Navigation
function switchTab(tabName, opts={{}}) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabName));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === `tab-${{tabName}}`));
  if (!opts.skipAutoLoad) {{
    if (tabName === 'chats' && !window._chatsLoaded) loadChats();
    if (tabName === 'media' && !window._mediaLoaded) loadMedia(true);
    if (tabName === 'vault' && !window._vaultLoaded) loadVault();
  }}
}}
document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

function jumpToChat(jid, name, phone='') {{
  switchTab('chats', {{ skipAutoLoad: true }});
  openChat(jid, name, false, phone);
}}

function filterSearchByChat(target) {{
  switchTab('search');
  document.getElementById('searchChat').value = target || '';
  runSearch(true);
}}

function filterMediaByChat(target) {{
  switchTab('media', {{ skipAutoLoad: true }});
  document.getElementById('mediaChat').value = target || '';
  loadMedia(true);
}}

// Chat / Phone Suggestions Datalist
let _lastSuggestQ = null;
async function loadChatSuggestions(q='') {{
  const norm = (q || '').trim();
  if (norm === _lastSuggestQ) return;
  _lastSuggestQ = norm;
  try {{
    const items = await apiGet(`/api/chat_suggestions?q=${{encodeURIComponent(norm)}}&limit=120`);
    const dl = document.getElementById('chatSuggestList');
    dl.innerHTML = items.map(c => {{
      const val = c.phone_number || c.chat_name || c.chat_jid;
      const phoneTxt = c.phone_number ? ` · ${{fmtPhone(c.phone_number)}}` : (c.is_group ? ' · Group' : '');
      return `<option value="${{esc(val)}}">${{esc(c.chat_name)}}${{esc(phoneTxt)}}</option>`;
    }}).join('');
  }} catch (_) {{}}
}}
const debouncedSuggest = debounce((val) => loadChatSuggestions(val), 220);
['searchChat','mediaChat','vaultQuery'].forEach(id => {{
  document.getElementById(id).addEventListener('input', e => debouncedSuggest(e.target.value));
}});

// Stats
async function loadStats() {{
  try {{
    const s = await apiGet('/api/stats');
    document.getElementById('statsStrip').innerHTML = `
      <span class="stat-pill">Msgs: <b>${{(s.total_messages||0).toLocaleString()}}</b></span>
      <span class="stat-pill">Chats: <b>${{(s.total_contacts||0).toLocaleString()}}</b></span>
      <span class="stat-pill">Media: <b>${{(s.total_stored_media||0).toLocaleString()}}</b> (${{s.stored_mb||0}} MB)</span>
      <span class="stat-pill">CPU OCR: <b>${{(s.total_local_ocr||0).toLocaleString()}}</b></span>
      <span class="stat-pill">Doc Index: <b>${{(s.total_local_docs||0).toLocaleString()}}</b></span>
      <span class="stat-pill">AI Cache: <b>${{(s.total_ocr_cached||0).toLocaleString()}}</b></span>
      <span class="stat-pill">Vault: <b>${{s.total_deleted||0}} del / ${{s.total_edited||0}} edit</b></span>
      <span class="stat-pill">DB: <b>${{s.db_size_mb||0}} MB</b></span>
    `;
  }} catch (e) {{
    document.getElementById('statsStrip').textContent = 'Failed to load stats';
  }}
}}

function mediaUrl(id, dl=false) {{
  return `/api/media/${{encodeURIComponent(id)}}${{dl ? '?download=1' : ''}}`;
}}

function videoThumbUrl(id) {{
  return `/api/video_thumb/${{encodeURIComponent(id)}}`;
}}

function escArg(s) {{
  return esc(JSON.stringify(String(s ?? '')));
}}

function fallbackVideoThumb(imgEl, id) {{
  if (!imgEl) return;
  imgEl.onerror = null;
  const v = document.createElement('video');
  v.muted = true;
  v.playsInline = true;
  v.preload = 'metadata';
  v.src = `${{mediaUrl(id)}}#t=0.5`;
  v.onerror = () => {{
    v.parentElement?.querySelector('.play-badge')?.remove();
    const icon = document.createElement('div');
    icon.className = 'media-tile-icon';
    icon.textContent = '🎬';
    v.replaceWith(icon);
  }};
  imgEl.replaceWith(v);
}}

function videoThumbMarkup(id) {{
  return `<img loading="lazy" src="${{videoThumbUrl(id)}}" onerror="fallbackVideoThumb(this,${{escArg(id)}})" /><span class="play-badge">▶</span><span class="kind-chip">VIDEO</span>`;
}}

function renderMediaSnippet(m) {{
  if (!m.has_local_media) return '';
  const url = mediaUrl(m.id, false);
  const dlUrl = mediaUrl(m.id, true);
  const mt = (m.media_type || '').toLowerCase();
  const mime = (m.media_mimetype || '').toLowerCase();
  const label = m.media_filename || m.media_type || 'Media';
  let preview = '';
  if (mt === 'image' || mt === 'sticker' || mime.startsWith('image/')) {{
    preview = `<img class="thumb-img" loading="lazy" src="${{url}}" onclick="openItemModal(${{escArg(m.id)}},${{escArg(label)}},'image')" />`;
  }} else if (mt === 'voice_note' || mt === 'audio' || mime.startsWith('audio/')) {{
    preview = `<audio controls preload="none" src="${{url}}" style="margin-top:8px;max-width:100%"></audio>`;
  }} else if (mt === 'video' || mime.startsWith('video/')) {{
    const vTitle = m.media_filename || 'Video';
    preview = `
      <div class="video-thumb-box" onclick="openItemModal(${{escArg(m.id)}},${{escArg(vTitle)}},'video')" title="Play Video">
        ${{videoThumbMarkup(m.id)}}
      </div>
      <div class="media-bar">
        <button class="ghost-btn" onclick="openItemModal(${{escArg(m.id)}},${{escArg(vTitle)}},'video')">▶ Play Video</button>
        <a class="ghost-btn" href="${{dlUrl}}">⬇ Download ${{fmtBytes(m.media_size)}}</a>
      </div>`;
  }} else {{
    preview = `<div class="media-bar">
      <button class="ghost-btn" onclick="openDocModal(${{escArg(m.id)}},${{escArg(m.media_filename || 'Document')}})">📄 Preview / Extracted Text</button>
      <a class="ghost-btn" href="${{url}}" target="_blank">🔗 Open</a>
      <a class="ghost-btn" href="${{dlUrl}}">⬇ Download ${{fmtBytes(m.media_size)}}</a>
    </div>`;
  }}
  return preview;
}}

function renderMessageCard(m) {{
  let badges = `<span class="badge">${{esc(m.media_type)}}</span>`;
  if (m.has_local_media) badges += `<span class="badge saved">💾 Saved</span>`;
  if (m.ocr_cached) badges += `<span class="badge saved">✨ AI Cached</span>`;
  if (m.is_deleted) badges += `<span class="badge deleted">🗑️ Deleted ${{esc(m.deleted_at)}}</span>`;
  if (m.is_edited) badges += `<span class="badge edited">✏️ Edited ${{esc(m.edited_at)}}</span>`;
  const phonePill = m.chat_phone ? `<span class="phone-tag">${{esc(fmtPhone(m.chat_phone))}}</span>` : '';
  const origHtml = (m.original_text && m.original_text !== m.text && (m.is_deleted || m.is_edited))
    ? `<div class="orig-box"><strong>Preserved Original:</strong>\n${{esc(m.original_text)}}</div>` : '';
  const ocrHtml = m.ocr_cached
    ? `<div class="orig-box" style="border-left-color:var(--accent)"><strong>AI Vision / Transcript Cache:</strong>\n${{esc(m.ocr_cached.slice(0, 1200))}}${{m.ocr_cached.length > 1200 ? '...' : ''}}</div>` : '';
  return `
    <div class="msg-card">
      <div class="msg-meta">
        <div>
          <span class="who">${{esc(m.sender)}}</span> in
          <button class="link-pill" onclick="jumpToChat(${{escArg(m.chat_jid)}},${{escArg(m.chat_name)}},${{escArg(m.chat_phone || '')}})" title="Open this chat">💬 ${{esc(m.chat_name)}}</button>
          ${{phonePill}}
          ${{badges}}
        </div>
        <div>${{esc(m.timestamp_str)}}</div>
      </div>
      <div class="msg-body">${{esc(m.text || m.media_filename || '[Empty]')}}</div>
      ${{origHtml}}
      ${{ocrHtml}}
      ${{renderMediaSnippet(m)}}
    </div>
  `;
}}

// Search Tab
let searchOffset = 0;
async function runSearch(reset=true) {{
  if (reset) {{ searchOffset = 0; document.getElementById('searchResults').innerHTML = '<div class="muted">Searching...</div>'; }}
  const q = document.getElementById('searchQuery').value.trim();
  const chat = document.getElementById('searchChat').value.trim();
  const scope = document.getElementById('searchScope').value;
  const mediaType = document.getElementById('searchType').value;
  const statuses = document.getElementById('searchStatuses').checked ? '1' : '0';
  const rows = await apiGet(`/api/search?q=${{encodeURIComponent(q)}}&chat=${{encodeURIComponent(chat)}}&scope=${{encodeURIComponent(scope)}}&media_type=${{encodeURIComponent(mediaType)}}&statuses=${{statuses}}&limit=50&offset=${{searchOffset}}`);
  const box = document.getElementById('searchResults');
  if (reset) box.innerHTML = '';
  if (!rows.length && reset) {{
    box.innerHTML = '<div class="empty-state">🔍 No matching messages found.<br/><span class="muted">Try a shorter keyword, phone digits (e.g. 98505), or switch chat scope.</span></div>';
  }} else {{
    box.insertAdjacentHTML('beforeend', rows.map(renderMessageCard).join(''));
    searchOffset += rows.length;
  }}
  document.getElementById('searchMoreBtn').style.display = rows.length === 50 ? 'inline-flex' : 'none';
}}
document.getElementById('searchBtn').addEventListener('click', () => runSearch(true));
document.getElementById('searchMoreBtn').addEventListener('click', () => runSearch(false));
document.getElementById('searchScope').addEventListener('change', () => runSearch(true));
document.getElementById('searchType').addEventListener('change', () => runSearch(true));
document.getElementById('searchStatuses').addEventListener('change', () => runSearch(true));
document.getElementById('searchClearBtn').addEventListener('click', () => {{
  document.getElementById('searchQuery').value = '';
  document.getElementById('searchChat').value = '';
  document.getElementById('searchScope').value = 'all';
  document.getElementById('searchType').value = 'all';
  document.getElementById('searchStatuses').checked = false;
  runSearch(true);
}});
['searchQuery','searchChat'].forEach(id => document.getElementById(id).addEventListener('keydown', e => {{ if (e.key === 'Enter') runSearch(true); }}));

// Chats Tab
let activeChatJid = '';
let activeChatPhone = '';
let oldestChatTs = 0;
let chatScope = 'all';
async function loadChats() {{
  window._chatsLoaded = true;
  const q = document.getElementById('chatFilterInput').value.trim();
  const st = document.getElementById('chatIncludeStatuses').checked ? '1' : '0';
  const chats = await apiGet(`/api/chats?q=${{encodeURIComponent(q)}}&scope=${{encodeURIComponent(chatScope)}}&statuses=${{st}}&limit=100`);
  const el = document.getElementById('chatList');
  if (!chats.length) {{ el.innerHTML = '<div class="empty-state" style="margin:10px">No matching chats or phone numbers.</div>'; return; }}
  el.innerHTML = chats.map(c => `
    <div class="chat-item ${{c.chat_jid === activeChatJid ? 'active' : ''}}" onclick="openChat(${{escArg(c.chat_jid)}},${{escArg(c.chat_name)}},false,${{escArg(c.phone_number || '')}})">
      <div class="chat-item-top">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{c.is_group ? '👥 ' : '👤 '}}${{esc(c.chat_name)}}</span>
        <span class="badge">${{c.msg_count}}</span>
      </div>
      ${{c.phone_number ? `<div class="phone-tag">${{esc(fmtPhone(c.phone_number))}}</div>` : ''}}
      <div class="chat-item-sub">${{esc(c.last_time)}} · ${{esc(c.preview || c.last_media_type)}}</div>
    </div>
  `).join('');
}}
document.getElementById('chatFilterInput').addEventListener('input', debounce(() => loadChats(), 200));
document.getElementById('chatIncludeStatuses').addEventListener('change', () => loadChats());
document.querySelectorAll('#chatScopePills .scope-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    chatScope = btn.dataset.scope;
    document.querySelectorAll('#chatScopePills .scope-btn').forEach(b => b.classList.toggle('active', b === btn));
    loadChats();
  }});
}});

function renderBubble(m) {{
  const tags = [];
  if (m.is_deleted) tags.push(`<span class="badge deleted">🗑️ Deleted</span>`);
  if (m.is_edited) tags.push(`<span class="badge edited">✏️ Edited</span>`);
  const orig = (m.original_text && m.original_text !== m.text && (m.is_deleted || m.is_edited))
    ? `<div class="orig-box"><strong>Original:</strong> ${{esc(m.original_text)}}</div>` : '';
  return `
    <div class="bubble ${{m.is_from_me ? 'me' : ''}}">
      <div class="bubble-hdr">${{esc(m.sender)}} ${{tags.join(' ')}}</div>
      <div class="msg-body">${{esc(m.text || m.media_filename || '')}}</div>
      ${{orig}}
      ${{renderMediaSnippet(m)}}
      <div class="bubble-ts">${{esc(m.timestamp_str)}}</div>
    </div>
  `;
}}

async function openChat(jid, name, older=false, phone='') {{
  activeChatJid = jid;
  if (!older) activeChatPhone = phone || '';
  document.getElementById('activeChatTitle').textContent = name || jid;
  document.getElementById('activeChatSub').textContent = activeChatPhone ? fmtPhone(activeChatPhone) : jid;
  document.getElementById('chatSearchJumpBtn').style.display = 'inline-flex';
  document.getElementById('chatMediaJumpBtn').style.display = 'inline-flex';
  const msgBox = document.getElementById('chatMessages');
  if (!older) {{ oldestChatTs = 0; msgBox.innerHTML = '<div class="muted">Loading messages...</div>'; }}
  const rows = await apiGet(`/api/chat?jid=${{encodeURIComponent(jid)}}&before=${{older ? oldestChatTs : 0}}&limit=80`);
  if (!older) msgBox.innerHTML = '';
  if (rows.length) {{
    oldestChatTs = rows[0].timestamp;
    const html = rows.map(renderBubble).join('');
    if (older) msgBox.insertAdjacentHTML('afterbegin', html);
    else {{ msgBox.innerHTML = html; msgBox.scrollTop = msgBox.scrollHeight; }}
  }} else if (!older) {{
    msgBox.innerHTML = '<div class="muted">No messages found.</div>';
  }}
  document.getElementById('loadOlderBtn').style.display = rows.length === 80 ? 'inline-flex' : 'none';
  loadChats();
}}
document.getElementById('loadOlderBtn').addEventListener('click', () => {{
  if (activeChatJid) openChat(activeChatJid, document.getElementById('activeChatTitle').textContent, true, activeChatPhone);
}});
document.getElementById('chatSearchJumpBtn').addEventListener('click', () => {{
  if (activeChatJid) filterSearchByChat(activeChatPhone || activeChatJid);
}});
document.getElementById('chatMediaJumpBtn').addEventListener('click', () => {{
  if (activeChatJid) filterMediaByChat(activeChatPhone || activeChatJid);
}});

// Media Gallery Tab
let mediaOffset = 0;
async function loadMedia(reset=true) {{
  window._mediaLoaded = true;
  if (reset) mediaOffset = 0;
  const f = document.getElementById('mediaFilter').value;
  const chat = document.getElementById('mediaChat').value.trim();
  const scope = document.getElementById('mediaScope').value;
  const q = document.getElementById('mediaQuery').value.trim();
  const statuses = document.getElementById('mediaStatuses').checked ? '1' : '0';
  const rows = await apiGet(`/api/media_list?filter=${{encodeURIComponent(f)}}&chat=${{encodeURIComponent(chat)}}&scope=${{encodeURIComponent(scope)}}&q=${{encodeURIComponent(q)}}&statuses=${{statuses}}&limit=60&offset=${{mediaOffset}}`);
  const grid = document.getElementById('mediaGrid');
  if (reset) grid.innerHTML = '';
  if (!rows.length && reset) {{ grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">🖼️ No stored media matching these filters.</div>'; }}
  else {{
    grid.insertAdjacentHTML('beforeend', rows.map(m => {{
      const mt = (m.media_type || '').toLowerCase();
      const mime = (m.media_mimetype || '').toLowerCase();
      const isImg = mt === 'image' || mt === 'sticker' || mime.startsWith('image/');
      const isVid = mt === 'video' || mime.startsWith('video/');
      const icon = mt === 'document' ? '📄' : (mt.includes('audio') || mt === 'voice_note' ? '🎙️' : '📦');
      const fname = m.media_filename || `${{m.media_type}}_${{m.id.slice(0,8)}}`;
      const modalKind = isImg ? 'image' : (isVid ? 'video' : mt);
      let prev = `<div class="media-tile-icon">${{icon}}</div>`;
      if (isImg) prev = `<img loading="lazy" src="${{mediaUrl(m.id)}}" />`;
      else if (isVid) prev = videoThumbMarkup(m.id);
      const clickCall = mt === 'document'
        ? `openDocModal(${{escArg(m.id)}},${{escArg(fname)}})`
        : `openItemModal(${{escArg(m.id)}},${{escArg(fname)}},${{escArg(modalKind)}})`;
      const filterTarget = m.chat_phone || m.chat_name;
      return `
        <div class="media-tile">
          <div class="media-tile-preview" onclick="${{clickCall}}">${{prev}}</div>
          <div class="media-tile-info">
            <div class="media-tile-title" title="${{esc(fname)}}">${{esc(fname)}}</div>
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <button class="link-pill" onclick="filterMediaByChat(${{escArg(filterTarget)}})" title="Filter media by this chat / phone">${{esc(m.chat_name)}}</button>
              <span class="muted">${{fmtBytes(m.media_size)}}</span>
            </div>
            ${{m.chat_phone ? `<div class="phone-tag">${{esc(fmtPhone(m.chat_phone))}}</div>` : ''}}
            <div class="muted">${{esc(m.timestamp_str)}}</div>
            <div class="media-bar">
              <a class="ghost-btn" href="${{mediaUrl(m.id, true)}}">⬇ Save</a>
              ${{isVid ? `<button class="ghost-btn" onclick="${{clickCall}}">▶ Play</button>` : ''}}
              ${{mt === 'document' ? `<button class="ghost-btn" onclick="openDocModal(${{escArg(m.id)}},${{escArg(fname)}})">Text</button>` : ''}}
            </div>
          </div>
        </div>
      `;
    }}).join(''));
    mediaOffset += rows.length;
  }}
  document.getElementById('mediaMoreBtn').style.display = rows.length === 60 ? 'inline-flex' : 'none';
}}
document.getElementById('mediaSearchBtn').addEventListener('click', () => loadMedia(true));
document.getElementById('mediaFilter').addEventListener('change', () => loadMedia(true));
document.getElementById('mediaScope').addEventListener('change', () => loadMedia(true));
document.getElementById('mediaStatuses').addEventListener('change', () => loadMedia(true));
document.getElementById('mediaMoreBtn').addEventListener('click', () => loadMedia(false));
document.getElementById('mediaClearBtn').addEventListener('click', () => {{
  document.getElementById('mediaFilter').value = 'all';
  document.getElementById('mediaChat').value = '';
  document.getElementById('mediaScope').value = 'all';
  document.getElementById('mediaQuery').value = '';
  document.getElementById('mediaStatuses').checked = true;
  loadMedia(true);
}});
['mediaChat','mediaQuery'].forEach(id => document.getElementById(id).addEventListener('keydown', e => {{ if (e.key === 'Enter') loadMedia(true); }}));

// Vault Tab
async function loadVault() {{
  window._vaultLoaded = true;
  const mode = document.getElementById('vaultMode').value;
  const q = document.getElementById('vaultQuery').value.trim();
  const scope = document.getElementById('vaultScope').value;
  const rows = await apiGet(`/api/vault?mode=${{encodeURIComponent(mode)}}&q=${{encodeURIComponent(q)}}&scope=${{encodeURIComponent(scope)}}&limit=50`);
  const el = document.getElementById('vaultResults');
  if (!rows.length) {{
    el.innerHTML = '<div class="empty-state">🛡️ No matching deleted or edited messages found.<br/><span class="muted">Incoming revokes &amp; edits are archived here automatically.</span></div>';
    return;
  }}
  el.innerHTML = rows.map(r => {{
    const badge = r.is_deleted && r.is_edited ? '<span class="badge deleted">🗑️ Deleted &amp; ✏️ Edited</span>'
      : (r.is_deleted ? `<span class="badge deleted">🗑️ Deleted ${{esc(r.deleted_at)}}</span>` : `<span class="badge edited">✏️ Edited ${{esc(r.edited_at)}}</span>`);
    const revs = (r.revisions || []).map(rv => `<div>• <b>${{esc(rv.event)}}</b> (${{esc(rv.timestamp)}}) by ${{esc(rv.actor)}}: ${{esc(rv.previous_text || '[Empty]')}} → ${{esc(rv.new_text || '[Deleted]')}}</div>`).join('');
    return `
      <div class="msg-card">
        <div class="msg-meta">
          <div>
            <span class="who">${{esc(r.sender)}}</span> in
            <button class="link-pill" onclick="jumpToChat(${{escArg(r.chat_jid)}},${{escArg(r.chat)}})" title="Open this chat">💬 ${{esc(r.chat)}}</button>
            ${{badge}}
          </div>
          <div>Sent: ${{esc(r.sent_at)}}</div>
        </div>
        <div class="orig-box"><strong>Preserved Original:</strong>\n${{esc(r.original_text || r.current_text || '[No text]')}}</div>
        ${{r.is_edited && r.current_text ? `<div class="msg-body" style="margin-top:8px"><strong>Latest Text:</strong> ${{esc(r.current_text)}}</div>` : ''}}
        ${{revs ? `<div class="muted" style="margin-top:8px">${{revs}}</div>` : ''}}
        ${{r.media_saved_on_disk ? `<div class="media-bar"><a class="ghost-btn" href="${{mediaUrl(r.id, true)}}">⬇ Saved Media (${{esc(r.media_filename || r.media_type)}})</a><button class="ghost-btn" onclick="openItemModal(${{escArg(r.id)}},${{escArg(r.media_filename || r.media_type)}},${{escArg(r.media_type)}})">👁️ Inspect</button></div>` : ''}}
      </div>
    `;
  }}).join('');
}}
document.getElementById('vaultRefreshBtn').addEventListener('click', loadVault);
document.getElementById('vaultMode').addEventListener('change', loadVault);
document.getElementById('vaultScope').addEventListener('change', loadVault);
document.getElementById('vaultQuery').addEventListener('keydown', e => {{ if (e.key === 'Enter') loadVault(); }});
document.getElementById('vaultClearBtn').addEventListener('click', () => {{
  document.getElementById('vaultMode').value = 'all';
  document.getElementById('vaultQuery').value = '';
  document.getElementById('vaultScope').value = 'all';
  loadVault();
}});

// Global '/' search shortcut
document.addEventListener('keydown', e => {{
  if (e.key === '/' && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)) {{
    e.preventDefault();
    switchTab('search');
    document.getElementById('searchQuery').focus();
  }}
}});

// Modals
document.getElementById('previewDialog').addEventListener('close', () => {{
  const mb = document.getElementById('modalBody');
  mb.querySelectorAll('video, audio').forEach(el => {{
    try {{ el.pause(); el.removeAttribute('src'); el.load(); }} catch (_) {{}}
  }});
  mb.innerHTML = '';
}});

function openItemModal(id, title, kind) {{
  const dlg = document.getElementById('previewDialog');
  document.getElementById('modalTitle').textContent = title;
  const u = mediaUrl(id);
  const dl = mediaUrl(id, true);
  let body = '';
  if (kind === 'image' || kind === 'sticker') body = `<img src="${{u}}" style="max-width:100%;border-radius:8px" />`;
  else if (kind === 'video') body = `<video controls autoplay playsinline poster="${{videoThumbUrl(id)}}" src="${{u}}" style="width:100%;max-width:100%;max-height:60vh;border-radius:8px;background:#000"></video>`;
  else if (kind === 'audio' || kind === 'voice_note') body = `<audio controls autoplay src="${{u}}" style="width:100%"></audio>`;
  else body = `<iframe src="${{u}}" style="width:100%;height:60vh;border:none;background:#fff;border-radius:8px"></iframe>`;
  document.getElementById('modalBody').innerHTML = `${{body}}<div class="media-bar"><a class="ghost-btn" href="${{dl}}">⬇ Download Original</a></div>`;
  dlg.showModal();
}}

async function openDocModal(id, title) {{
  const dlg = document.getElementById('previewDialog');
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').innerHTML = '<div class="muted">Extracting document contents...</div>';
  dlg.showModal();
  try {{
    const res = await apiGet(`/api/doc_preview/${{encodeURIComponent(id)}}`);
    const text = res.preview || res.ocr_cached || 'No extractable text available.';
    document.getElementById('modalBody').innerHTML = `
      <div class="media-bar">
        <a class="ghost-btn" href="${{mediaUrl(id)}}" target="_blank">🔗 Open Raw File</a>
        <a class="ghost-btn" href="${{mediaUrl(id, true)}}">⬇ Download</a>
      </div>
      <pre>${{esc(text)}}</pre>
    `;
  }} catch (e) {{
    document.getElementById('modalBody').innerHTML = `<div class="muted">Preview failed: ${{esc(e.message)}}</div>`;
  }}
}}

// Ask AI Tab
const askBtn = document.getElementById('askBtn');
if (askBtn) {{
  const submitAsk = async () => {{
    const q = document.getElementById('askInput').value.trim();
    if (!q) return;
    const st = document.getElementById('askStatus');
    const out = document.getElementById('askOutput');
    st.textContent = 'Querying WAmate Gemini agent...';
    out.textContent = '';
    askBtn.disabled = true;
    try {{
      const res = await fetch('/api/ask', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ query: q }})
      }});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${{res.status}}`);
      st.textContent = 'Done';
      out.textContent = data.answer || '';
    }} catch (e) {{
      st.textContent = 'Error';
      out.textContent = e.message;
    }} finally {{
      askBtn.disabled = false;
    }}
  }};
  askBtn.addEventListener('click', submitAsk);
  document.getElementById('askInput').addEventListener('keydown', e => {{ if (e.key === 'Enter') submitAsk(); }});
}}

loadStats();
loadChatSuggestions('');
runSearch(true);
</script>
</body>
</html>
"""


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
        self.thumbs_dir = (self.downloads_dir.parent / "thumbs").resolve()
        self._thumb_sem = threading.Semaphore(2)
        self._thumb_locks_guard = threading.Lock()
        self._thumb_locks: dict[str, threading.Lock] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """Return True while the HTTP server socket is actively bound and serving."""
        return self._server is not None

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

            def _send_json(self, data: Any, status: int = 200) -> None:
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self._send_bytes(status, "application/json; charset=utf-8", payload)

            def _check_auth(self) -> bool:
                if not parent.password:
                    return True
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Basic "):
                    try:
                        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", errors="ignore")
                        _, _, supplied_pw = decoded.partition(":")
                        if hmac.compare_digest(supplied_pw, parent.password):
                            return True
                    except Exception:
                        pass
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
                if not self._check_auth():
                    return

                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                qs = urllib.parse.parse_qs(parsed.query)

                try:
                    if path == "/" or path == "/index.html":
                        html = _build_html_page(has_ai=parent.gemini is not None).encode("utf-8")
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
                        self._send_json(stats)
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
                        limit = _clamp_int(qs.get("limit", ["50"])[0], 50, 1, 200)
                        offset = _clamp_int(qs.get("offset", ["0"])[0], 0, 0, 50000)
                        self._send_json(
                            parent.db.search_messages_web(
                                query=q,
                                chat_jid=chat,
                                chat_scope=scope,
                                media_type=media_type,
                                include_statuses=statuses,
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
                        limit = _clamp_int(qs.get("limit", ["60"])[0], 60, 1, 200)
                        offset = _clamp_int(qs.get("offset", ["0"])[0], 0, 0, 50000)
                        self._send_json(
                            parent.db.list_stored_media(
                                media_filter=flt,
                                query=q,
                                chat=chat,
                                chat_scope=scope,
                                include_statuses=statuses,
                                limit=limit,
                                offset=offset,
                            )
                        )
                        return

                    if path == "/api/vault":
                        mode = (qs.get("mode", ["all"])[0] or "all").strip()
                        q = (qs.get("q", [""])[0] or "").strip()
                        scope = (qs.get("scope", ["all"])[0] or "all").strip()
                        limit = _clamp_int(qs.get("limit", ["50"])[0], 50, 1, 200)
                        self._send_json(
                            parent.db.find_deleted_or_edited_messages(
                                mode=mode, chat_name_or_jid=q, chat_scope=scope, limit=limit
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

                    self._send_json({"error": "Not found"}, status=404)
                except Exception as exc:
                    logger.exception("WebUI GET error on %s: %s", path, exc)
                    self._send_json({"error": str(exc)}, status=500)

            def do_POST(self) -> None:
                if not self._check_auth():
                    return
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/api/ask":
                    self._send_json({"error": "Not found"}, status=404)
                    return
                if not parent.gemini:
                    self._send_json({"error": "Gemini AI agent is not active on this Web UI instance."}, status=503)
                    return

                try:
                    raw_len = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError:
                    raw_len = 0
                if raw_len <= 0 or raw_len > 16384:
                    self._send_json({"error": "Invalid request payload size"}, status=400)
                    return

                try:
                    payload = json.loads(self.rfile.read(raw_len).decode("utf-8"))
                except Exception:
                    self._send_json({"error": "Invalid JSON body"}, status=400)
                    return

                query = (payload.get("query") or "").strip()
                if not query:
                    self._send_json({"error": "Query cannot be empty"}, status=400)
                    return

                if parent.bot_client and hasattr(parent.bot_client, "_begin_query"):
                    parent.bot_client._begin_query()
                try:
                    answer = parent.gemini.ask(query)
                    self._send_json({"answer": answer})
                except Exception as exc:
                    logger.exception("WebUI /api/ask error: %s", exc)
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
