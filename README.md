# DataVizAI — AI-Enhanced Survey Data Preparation & Report Writing

> **Problem Statement PS-4** | Ministry of Statistics & Programme Implementation (MoSPI), Government of India
> Track: Data Processing and Analysis

A Flask-based web application that automates the full lifecycle of NSS (National Statistical Survey) data: ingestion → cleaning → weighted estimation → AI-generated reports with margins of error.

---

Live deployment Link:[datavizai.onrender.com](https://datavizai.onrender.com/)

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Environment Variables](#environment-variables)
4. [Running the Application](#running-the-application)
5. [Using the Application (Step-by-Step)](#using-the-application-step-by-step)
6. [Project Structure](#project-structure)
7. [Tech Stack](#tech-stack)

---
<img width="1280" height="687" alt="WhatsApp Image 2026-06-18 at 4 32 57 PM" src="https://github.com/user-attachments/assets/1a8e86ab-e732-48e2-8e25-a877dce21641" />
<img width="1280" height="687" alt="WhatsApp Image 2026-06-18 at 4 33 28 PM" src="https://github.com/user-attachments/assets/a0b38f1b-85f3-436f-811b-2569495459c2" />
<img width="1280" height="687" alt="WhatsApp Image 2026-06-18 at 4 34 05 PM" src="https://github.com/user-attachments/assets/0f5489c3-1442-4612-9ec4-66fd8b7f18e8" />

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11 or higher |
| pip | latest |
| Git | any recent version |

No Docker or virtual environment manager is strictly required, but `venv` is recommended.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
```

### 2. Create and activate a virtual environment (recommended)

```bash
# Linux / macOS
python3 -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

If a `requirements.txt` is not present, install manually:

```bash
pip install flask flask-login werkzeug pandas numpy scipy \
            openpyxl pdfplumber matplotlib plotly \
            google-genai groq pymupdf
```

---

## Environment Variables

Create a `.env` file in the project root (or export these in your shell):

```env
# Required — Flask session encryption key
SESSION_SECRET=your-random-secret-key-here

# AI API keys (at least one is required for report generation)
GEMINI_API_KEY=your-gemini-api-key
GROQ_API_KEY=your-groq-api-key

# Optional — AI response cache TTL in seconds (default: 86400 = 24 h)
# Set to 0 to cache forever
AI_CACHE_TTL_SECONDS=86400
```

> **Never commit `.env` to Git.** Add it to `.gitignore`.

To load `.env` automatically, install `python-dotenv`:

```bash
pip install python-dotenv
```

Then add at the top of `flask_app/app.py` (if not already present):

```python
from dotenv import load_dotenv
load_dotenv()
```
<img width="1280" height="687" alt="WhatsApp Image 2026-06-18 at 4 33 12 PM" src="https://github.com/user-attachments/assets/b51d6161-2dc5-4a5a-9b20-f11fe42bd5fa" />

<img width="1280" height="687" alt="WhatsApp Image 2026-06-18 at 4 34 35 PM" src="https://github.com/user-attachments/assets/45ab1be5-a8fd-4d84-9d30-683f99d326fd" />

---

## Running the Application

```bash
python flask_app/app.py
```

The server starts on **http://localhost:5000**.

Open your browser and navigate to `http://localhost:5000`.

---

## Using the Application (Step-by-Step)

The app follows a 6-step pipeline that mirrors the MoSPI data-processing workflow:

| Step | What you do | What the app does |
|---|---|---|
| **1 — Upload CSV** | Upload the raw NSS microdata CSV (e.g. `hhscsL7.csv`) | Detects encoding, reads levels, extracts dataset metadata |
| **2 — Upload Layout** | Upload the Data Layout Excel file | Parses block numbers, field names, variable-to-label mappings |
| **3 — Upload Schedule PDF** | Upload the Survey Schedule PDF | Extracts CODES FOR BLOCK N sections → value label codebook |
| **4 — Consolidate** | Click "Consolidate Dataset" | Resolves all variable names, replaces coded values with labels, merges layout + codebook into a single analysis-ready CSV |
| **5 — Clean & Preprocess** | Configure imputation method, outlier detection | Applies missing-value imputation (mean / median / KNN), IQR / Z-score / winsorization outlier handling, rule-based validation |
| **6 — Generate Tables & Report** | Define table specs (dimensions, measures, weight column) | Computes weighted estimates + 95% MOE, selects charts automatically, calls Groq/Gemini to write analysis, insights, recommendations, and trend summary — outputs a fully formatted HTML report |

---

## Project Structure

```
├── flask_app/
│   ├── app.py                  # Main Flask application (all processing logic)
│   ├── auth.py                 # Authentication routes
│   ├── ai_cache/               # Disk-based AI response cache (auto-created)
│   ├── uploads/                # Uploaded files (gitignored)
│   ├── outputs/                # Processed datasets, generated tables, reports
│   ├── templates/
│   │   ├── index.html          # Main single-page UI
│   │   ├── home.html
│   │   ├── login.html
│   │   └── admin.html
│   └── static/
│       ├── app.js              # Frontend logic (upload, filters, table, export)
│       ├── style.css           # Styles
│       ├── auth.js
│       └── admin_dash.js
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, Flask |
| Data Processing | pandas, NumPy, SciPy |
| File Parsing | openpyxl (Excel), pdfplumber (PDF) |
| Statistical Estimation | Linearisation variance estimator (sandwich SE), 95% MOE |
| Visualisation | Plotly (interactive), Matplotlib (static fallback) |
| AI Text Generation | Groq (`llama-3.3-70b-versatile`) + Google Gemini (`gemini-2.5-flash`) |
| AI Response Cache | SHA-256 keyed disk cache (TTL-configurable) |
| Auth | Flask-Login |
| Frontend | Vanilla HTML + CSS + JavaScript |
---

