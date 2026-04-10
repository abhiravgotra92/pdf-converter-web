# 📄 Webpage to PDF Converter

A web app that crawls any website and converts every page into a structured, downloadable PDF — complete with a cover page, table of contents, and embedded images.

## 🌐 Live App

**[web-production-a5f28.up.railway.app](https://web-production-a5f28.up.railway.app)**

## ✨ Features

- Paste any website URL and get a full PDF back
- Crawls all linked pages on the same domain (up to 100 pages)
- Embeds images into the PDF
- Generates a cover page and table of contents
- Real-time progress — shows pages crawled, speed, ETA and current URL
- One-click PDF download when done

## 🛠 Tech Stack

- **Backend**: Python, Flask, Playwright (headless Chromium), fpdf2, BeautifulSoup
- **Frontend**: Vanilla HTML/CSS/JS
- **Deploy**: Railway (Docker)

## 🚀 Deploy Your Own

1. Fork this repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select your fork
4. Optionally add env var: `APP_PASSWORD = yourpassword`
5. Live in ~3 minutes
