# 🤖 WAmate - WhatsApp Self-Chat AI Second Brain

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/Google%20Gemini-Flash%20Lite-E37400?style=for-the-badge&logo=google&logoColor=white" alt="Gemini AI" />
  <img src="https://img.shields.io/badge/WhatsApp-Neonize%200.5.2-25D366?style=for-the-badge&logo=whatsapp&logoColor=white" alt="WhatsApp Neonize" />
  <img src="https://img.shields.io/badge/SQLite-FTS5%20Search-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite FTS5" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="MIT License" />
</p>

**WAmate** transforms your personal WhatsApp **"Message yourself" (You / Self-Chat)** into a blazing-fast, multimodal AI second brain. 

Operating **strictly inside your private self-chat**, WAmate captures, indexes, and organizes your entire WhatsApp ecosystem—messages, voice notes, photos, product links, contact cards, polls, and scanned PDFs—and lets you query everything or fetch actual files directly through WhatsApp.

---

### 💬 See It In Action

```text
You: !ask what was the model of mechanical keyboard Alex recommended yesterday?
WAmate: ⏳ [reacts immediately]
WAmate: In your direct chat with Alex yesterday, he shared 3 photos of mechanical keyboards while discussing switch types. The model he recommended is the Keychron Q1 Pro with wireless Bluetooth, hot-swappable switches, and a custom aluminum knob...
WAmate: ✅

You: !get Q3 financial summary
WAmate: ⏳
WAmate: [Delivers the actual PDF file directly to your chat]
📄 Q3_Financial_Summary_2026.pdf (245 KB)
WAmate: ✅

You: !status
WAmate: 📊 WAmate Second Brain Status
• Messages Synced: 28,450
• Contacts & Groups: 1,120 (36 groups)
• History Date Range: 2024-01-10 → 2026-03-15
• Database Size: 48.2 MB
• Sync Status: Fully indexed & Real-time active ✅
```

---

## 🌟 Key Features

* 🔒 **100% Self-Chat Isolation:** Listens **only** in your personal "Message yourself" ("You") chat. Never monitors, snoops on, or sends messages to external contacts or group chats.
* 💬 **Multi-Turn Memory & Swipe-Reply Awareness:** Remembers your recent self-chat Q&A context so follow-ups (*"send me the second one"*, *"calculate GST on that"*) work naturally. You can also swipe/reply to any message, photo, or PDF directly with `!ask` or `!get`, or run `!clear` to reset context.
* 🕒 **Timezone-Accurate Temporal Context & Async Processing:** Injects your live local date/time (`TZ`) into every query so *"today"*, *"yesterday"*, and *"this week"* resolve accurately, while executing AI calls asynchronously so incoming message sync never stalls.
* ⚡ **Ultra-Fast Batch Indexing (1,000+ msgs/sec):** High-performance SQLite batch engine syncs tens of thousands of past messages in seconds with a live console progress counter.
* 📄 **Universal File & Document Analysis (`!get` / `!send`):** Automatically extracts and analyzes contents inside all day-to-day shared WhatsApp files: plain text (`.txt`), logs (`.log`), notes (`.md`), Excel (`.xlsx`, `.xls`, `.csv`), Word (`.docx`, `.rtf`, `.odt`), PowerPoint (`.pptx`, `.ppt`), vCard contacts (`.vcf`), calendar events (`.ics`), Jupyter notebooks (`.ipynb`), ZIP archives (`.zip`), source code (`.py`, `.js`, etc.), and PDFs. Gemini can calculate totals, summarize slide decks, inspect tables, and deliver the actual files directly into your chat.
* 💾 **30-Day Expiration Protection, SHA-256 Deduplication & Compact DB:** Automatically pre-downloads incoming photos, documents, voice notes, and status updates locally to disk with **zero Gemini API calls** and **SHA-256 content deduplication** (duplicate forwards point to one copy on disk). Non-media messages skip raw BLOB storage, and thread-local SQLite connection pooling keeps queries fast.
* 🔑 **Automatic Multi-Key & Model Rotation (`GEMINI_API_KEYS`):** Configure multiple comma-separated Gemini API keys to automatically rotate past per-key `429` rate limits alongside candidate model fallbacks.
* 👁️ **Multimodal AI Vision & On-Demand OCR:** Powered by Google Gemini to analyze scanned receipts, handwritten notes, whiteboards, bills, and visual items (clothing colors, objects, payment screenshots) on-demand whenever you ask, caching results permanently in SQLite for **0ms repeat queries with zero extra API calls**.
* 🛡️ **Anti-Delete & Edit Vault:** Captures real-time Delete-for-Everyone (`REVOKE`) and message edit (`MESSAGE_EDIT`) events — preserving original wording, revision timelines, and any downloaded deleted media (`!deleted`, `!edits`, or `!ask`).
* 🔗 **Link Preview & Web Metadata Search:** Automatically indexes webpage titles, descriptions, and metadata from shared URLs (GitHub repositories, YouTube videos, Amazon/e-commerce products, technical blogs, news articles), making shared links instantly searchable by topic or keyword.
* 🆔 **Cryptographic WhatsApp LID Resolution:** Automatically cross-references WhatsApp's 15-digit privacy LIDs (`@lid`) against your phone address book, accurately identifying real contact names in group chats.
* ⏳ **Real-Time Visual Feedback:** Reacts immediately with an hourglass **`⏳`** on your message bubble when a query begins, switching to a checkmark **`✅`** once completed.
* 🎙️ **Voice Note Transcription & Rich Media Indexing:** On-demand Gemini audio transcription (`transcribe_voice_note`) converts voice notes/audio clips into permanently searchable transcripts cached in SQLite, alongside optional free local CPU OCR (`ENABLE_LOCAL_OCR=true` via system `tesseract`) and indexing of videos, vCard contacts, and polls.
* 📱 **Native WhatsApp Formatting:** Sanitized for mobile displays using native WhatsApp markdown (`*bold*`, `_italics_`, `• bullets`) with zero broken raw markdown headers or unreadable tables.
* 🔑 **Dual Pairing Modes:** Supports instant terminal QR code scanning or headless 8-character pairing codes (`ABCD-1234`) for remote VPS and cloud deployments.

---

## 🎮 Commands & Usage

All commands run directly in your WhatsApp **"Message yourself"** chat:

### 📱 In WhatsApp Self-Chat

| Command | Action | Example |
| :--- | :--- | :--- |
| `!ask <question>` | Ask anything about your messages, chats, or media (supports follow-ups & swipe-replies) | `!ask what did Alex message me about the project deadline?` |
| `!get <filename>` | Locate and deliver the real document file directly to you | `!get Q3 financial summary pdf` |
| `!send <filename>` | Alternative trigger to fetch and deliver a file | `!send project roadmap 2026` |
| `!deleted [name]` / `!edits [name]` | Browse deleted-for-everyone & edited messages saved by Anti-Delete Vault (`!vault`) | `!deleted Alex` |
| `!clear` | Clear recent multi-turn conversation memory (`!new`, `!forget`) | `!clear` |
| `!status` / `!stats` | View real-time database stats, message counts, and sync state | `!status` |
| `!help` | Show all self-chat commands and shortcuts | `!help` |
| `!unlink` | Unlink current WhatsApp account (keeps message history database) | `!unlink` |
| `!unlink --delete-data` | Unlink account and wipe all databases, sessions, and downloads | `!reset` |

---

### 💻 Terminal CLI Utility Commands

You can manage WAmate directly from your terminal:

```bash
# 1. Print indexing and database statistics without starting the bot
python3 main.py --status

# 2. Prune redundant raw message BLOBs and VACUUM database to reclaim disk space
python3 main.py --prune-db

# 3. Unlink account (prompts for confirmation; message history preserved)
python3 main.py --unlink

# 4. Unlink account and wipe all databases and downloaded files
python3 main.py --unlink --delete-data

# 5. Immediate headless reset (skips confirmation prompt; ideal for Docker/scripts)
python3 main.py --reset -y
```

---

## 🏗️ Architecture

```mermaid
flowchart TD
    subgraph WhatsApp["WhatsApp Network"]
        Phone["Your Phone"]
        Contacts["Contacts & Group Chats"]
        SelfChat["Self-Chat ('Message yourself')"]
    end

    subgraph WAmate["WAmate Core (Local / VPS / Docker)"]
        WASock["WhatsApp Engine (Neonize / whatsmeow)"]
        DB[("SQLite Database & FTS5\n- messages & fts index\n- contacts & LID mappings\n- media_ocr_cache\n- rich metadata")]
        Agent["Gemini Multimodal Agent\n(gemini-3.5-flash-lite)"]
        Formatter["WhatsApp Markdown Sanitizer"]
    end

    Contacts -->|Real-time Incoming/Outgoing| WASock
    Phone -->|HistorySync Batches| WASock
    WASock -->|Batch Index Messages & LIDs| DB

    SelfChat -->|'!ask what did Alex send me?'| WASock
    WASock -->|1. React ⏳ on bubble| SelfChat
    WASock -->|2. Dispatch query| Agent
    Agent <-->|Tools: search_history, inspect_ocr, find_media| DB
    Agent -->|Raw AI Response| Formatter
    Formatter -->|Sanitized WhatsApp Reply| WASock
    WASock -->|3. Send Reply + React ✅| SelfChat
```

---

## 🚀 Quick Start Guide

### 1. Prerequisites
* **Python 3.12+**
* **FFmpeg & SQLite3** (for media operations and audio processing):
  ```bash
  sudo apt update && sudo apt install -y ffmpeg libmagic1 sqlite3
  ```
* A free **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/).

---

### 2. Installation

Clone the repository and set up a virtual environment:

```bash
git clone https://github.com/y345-git/WAmate.git
cd WAmate

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

### 3. Configuration

Copy the example environment template:
```bash
cp .env.example .env
```

Configure your `.env` file:
```ini
# [REQUIRED] Google Gemini API Key
GEMINI_API_KEY=AIzaSy...your_gemini_api_key_here
# Optional comma-separated keys for automatic 429 quota rotation across projects
# GEMINI_API_KEYS=key_1,key_2

# [OPTIONAL] Gemini Model (default: gemini-3.5-flash-lite)
GEMINI_MODEL=gemini-3.5-flash-lite
CONVERSATION_MEMORY_TURNS=6
CONVERSATION_MEMORY_TTL_MINUTES=20

# [OPTIONAL] Command Prefix in Self-Chat (default: !ask)
COMMAND_PREFIX=!ask

# [OPTIONAL] Phone Number for 8-digit Pairing Code (country code + number, no '+' or dashes)
# e.g., 14155552671 (Leave blank to use console QR code)
PHONE_NUMBER=

# Data & Database Directory
DATA_DIR=./data
TZ=UTC
LOG_LEVEL=INFO

# [RECOMMENDED] Media Storage & Auto-Download (default: true, 0 Gemini API cost)
ENABLE_MEDIA_STORAGE=true
MEDIA_STORAGE_LOOKBACK_DAYS=30
MEDIA_STORAGE_MAX_SIZE_MB=0
MEDIA_STORAGE_TYPES=all
MEDIA_STORAGE_INCLUDE_STATUSES=true
MEDIA_STORAGE_DELAY_SECONDS=1.5
STATUS_MEDIA_RETENTION_DAYS=0

# [OPTIONAL] Local Background Indexing (0 Gemini API calls)
ENABLE_LOCAL_DOC_INDEX=true
# Image CPU OCR requires: sudo apt install -y tesseract-ocr
ENABLE_LOCAL_OCR=false
# Optional live self-chat alert when someone deletes a message for everyone
ANTI_DELETE_NOTIFY=false
```

---

### 4. Run WAmate

```bash
python3 main.py
```

* **QR Code Pairing (Default):** A clean ASCII QR code will display in your terminal. Open WhatsApp on your phone > **Linked Devices** > **Link a Device** and scan the QR code.
* **Phone Pairing Code:** If `PHONE_NUMBER` is set in `.env`, WAmate prints an 8-character pairing code (e.g. `ABCD-1234`). In WhatsApp, tap **Link with phone number instead** and enter the code.

> [!NOTE]
> Once paired, your authentication session is stored in `data/whatsapp_session.sqlite3`. On restarts, **WAmate reconnects in under 2 seconds without re-scanning**.

---

## 🐳 Docker Deployment (Recommended for VPS)

WAmate includes a containerized setup with volume mapping for databases and persistent sessions.

```bash
# 1. Start the container in background
docker compose up -d --build

# 2. View pairing QR code in container logs
docker logs -f wamate

# 3. Stop the container
docker compose down
```

---

## 💡 Example Queries to Try

Once WAmate completes its initial sync, try these queries in your self-chat:

* **Conversations & Catch-ups:**
  * `!ask summarize my chats from today`
  * `!ask what did Alex tell me about the project deadline yesterday?`
  * `!ask who messaged me about the server maintenance window?`
* **Visual Items, Photos & OCR:**
  * `!ask what was the model of the keyboard Alex shared?`
  * `!ask what is the total amount in the payment receipt screenshot sent by Sarah?`
  * `!ask read the handwritten whiteboard photo sent in Project Alpha group`
* **Documents, Spreadsheets, Notes & Files:**
  * `!get client onboarding checklist pdf`
  * `!ask what are the meeting action items in notes.txt?`
  * `!ask what is the grand total in the Q3 revenue spreadsheet?`
  * `!ask summarize the key points from slide 3 of the roadmap presentation`
  * `!ask what files are inside project_archive.zip?`
  * `!ask what is the contact number in speaker_card.vcf?`
  * `!send the latest vendor agreement from Acme Corp`
* **Products & Shared Links:**
  * `!ask what all monitor and desk setup links did David share today?`
  * `!ask find the article link for system design sent last week`
* **Contacts & Polls:**
  * `!ask find the contact card for Dr. Smith`
  * `!ask what were the options in the team lunch poll?`

---

## 🔒 Privacy & Local Security

* **Zero Cloud History Storage:** Your message history, contact names, and media stay 100% on your local machine or private server in SQLite.
* **Isolated Triggering:** WAmate ignores messages in all external 1-on-1 and group chats. Only commands issued by **you in your self-chat** trigger the assistant.
* **Safe Repository Defaults:** The `.gitignore` excludes session databases, SQLite message stores, `.env`, and downloaded files.

---

## 🛠️ Troubleshooting & FAQ

<details>
<summary><b>1. FFmpeg warning on startup?</b></summary>
Install FFmpeg on your host system:
<code>sudo apt update && sudo apt install -y ffmpeg</code>
</details>

<details>
<summary><b>2. How do I know when message history is fully synced?</b></summary>
Send <code>!status</code> in your WhatsApp self-chat or run <code>python3 main.py --status</code> in your terminal to see current indexed message counts and date ranges.
</details>

<details>
<summary><b>3. Why are media links older than 30 days failing to download?</b></summary>
WhatsApp media links expire on Meta's CDN after approximately 30 days. WAmate automatically falls back to newer matching files when available.
</details>

---

## 📄 License

This project is open-source and distributed under the [MIT License](LICENSE).
