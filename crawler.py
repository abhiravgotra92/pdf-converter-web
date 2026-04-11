"""
Headless website crawler + PDF builder for web deployment.
Adapted from the local crawl_to_pdf.py — no user input, no visible browser.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import tempfile
from collections import OrderedDict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fpdf import FPDF
from PIL import Image, UnidentifiedImageError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright
from requests.exceptions import RequestException

log = logging.getLogger(__name__)

DELAY      = 0.3
CONCURRENT = 4  # lower for server environment
MAX_PAGES  = 100


class _Config:
    base_url   = ""
    domain     = ""
    output_pdf = ""
    img_dir    = ""


def setup(url, work_dir):
    cfg = _Config()
    parsed       = urlparse(url)
    cfg.base_url = url.rstrip('/')
    cfg.domain   = parsed.netloc
    safe         = re.sub(r'[^\w.-]', '_', cfg.domain).strip('._')
    file_key     = hashlib.sha256(safe.encode()).hexdigest()[:16]
    cfg.output_pdf = os.path.join(work_dir, file_key + ".pdf")
    cfg.img_dir    = os.path.join(work_dir, "img_cache")
    os.makedirs(cfg.img_dir, exist_ok=True)
    return cfg


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean(text):
    return re.sub(r'\s+', ' ', text or '').strip()

def normalize(url):
    p = urlparse(url)
    return p._replace(fragment='', query='').geturl().rstrip('/')

def is_doc_link(url, domain):
    p = urlparse(url)
    return p.netloc == domain or p.netloc == ''

def extract_links(html, base, domain):
    soup = BeautifulSoup(html, 'lxml')
    links, seen = [], set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('#', 'mailto:', 'javascript:')):
            continue
        full = normalize(urljoin(base, href))
        if is_doc_link(full, domain) and full not in seen:
            seen.add(full)
            links.append(full)
    return links

def extract_content(html, base_url):
    soup = BeautifulSoup(html, 'lxml')
    for tag in soup.select(
        'nav, header, footer, script, style, button, '
        '[class*="sidebar"], [class*="nav-"], [class*="footer"], '
        '[class*="cookie"], [class*="breadcrumb"], [class*="search"], '
        '[class*="toc"], [class*="menu"], [role="navigation"]'
    ):
        tag.decompose()

    title = ''
    h1 = soup.find('h1')
    if h1:
        title = re.sub(r'[\u00b6\u00a7\u2190-\u21ff\u2600-\u26ff]', '', clean(h1.get_text()))
    elif soup.title:
        title = clean(soup.title.get_text())

    main = (
        soup.find('main') or soup.find('article') or
        soup.find(class_=re.compile(r'article|content|markdown|docs|post-body', re.I)) or
        soup.body
    )

    items, cur_heading, cur_level, cur_paras, seen_text = [], title, 1, [], set()

    def flush():
        text = ' '.join(cur_paras).strip()
        if cur_heading or text:
            items.append({'type': 'section', 'heading': cur_heading, 'level': cur_level, 'text': text})

    if main:
        for el in main.descendants:
            if not hasattr(el, 'name') or not el.name:
                continue
            if el.name in ('h1','h2','h3','h4','h5','h6'):
                flush(); cur_paras = []
                cur_heading = re.sub(r'[\u00b6\u00a7\u2190-\u21ff\u2600-\u26ff#]', '', clean(el.get_text())).strip()
                cur_level   = int(el.name[1])
            elif el.name == 'img':
                flush(); cur_paras = []; cur_heading = ''
                src = (el.get('src') or el.get('data-src') or el.get('data-lazy-src') or
                       (el.get('srcset','').split()[0] if el.get('srcset') else ''))
                if src and not src.startswith('data:'):
                    items.append({'type': 'image', 'src': urljoin(base_url, src), 'alt': clean(el.get('alt',''))})
            elif el.name in ('p','li','td','th','pre','blockquote','dd'):
                if el.parent and el.parent.name in ('li','ul','ol'):
                    continue
                t = clean(el.get_text())
                if len(t) > 1000: t = t[:1000] + '...'
                if t and t not in seen_text:
                    seen_text.add(t); cur_paras.append(t)
        flush()

    return {'title': title, 'items': items}


# ── Image helpers ─────────────────────────────────────────────────────────────

def safe_img_path(src, img_dir):
    name = hashlib.sha256(src.encode()).hexdigest()[:32] + '.jpg'
    return os.path.join(img_dir, name)

def save_image_bytes(src, data, img_dir):
    if not data: return None
    try:
        path = safe_img_path(src, img_dir)
        if os.path.exists(path): return path
        with io.BytesIO(data) as buf:
            img = Image.open(buf).convert('RGB')
            img.thumbnail((800, 600), Image.LANCZOS)
            img.save(path, 'JPEG', quality=75)
        return path
    except (UnidentifiedImageError, OSError, ValueError):
        return None

def download_image(src, img_dir):
    if src.startswith('data:') or '.svg' in src.lower(): return None
    try:
        path = safe_img_path(src, img_dir)
        if os.path.exists(path): return path
        r = requests.get(src, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        r.raise_for_status()
        return save_image_bytes(src, r.content, img_dir)
    except (RequestException, OSError, ValueError):
        return None

async def download_image_via_browser(page, src, img_dir):
    if src.startswith('data:') or '.svg' in src.lower(): return None
    try:
        path = safe_img_path(src, img_dir)
        if os.path.exists(path): return path
        data = await page.evaluate("""
            async (url) => {
                try {
                    const r = await fetch(url);
                    if (!r.ok) return null;
                    const buf = await r.arrayBuffer();
                    return Array.from(new Uint8Array(buf));
                } catch(e) { return null; }
            }
        """, src)
        if data:
            return save_image_bytes(src, bytes(data), img_dir)
    except Exception:
        return None


# ── Fetch one page ────────────────────────────────────────────────────────────

async def fetch_one(tab, url, cfg):
    try:
        await tab.goto(url, wait_until='commit', timeout=20000)
        try:
            await tab.wait_for_selector('h1, article, main, .content', timeout=4000)
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(DELAY)
        await tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.2)
        html    = await tab.content()
        content = extract_content(html, url)
        links   = extract_links(html, url, cfg.domain)
        img_items = [i for i in content.get('items', []) if i['type'] == 'image' and i.get('src')]
        if img_items:
            paths = await asyncio.gather(*[download_image_via_browser(tab, i['src'], cfg.img_dir) for i in img_items])
            for item, path in zip(img_items, paths):
                item['local_path'] = path
        return content, links
    except Exception as e:
        log.error("Error fetching %s: %s", url, e)
        return {'title': url.split('/')[-1], 'items': []}, []


# ── Crawl ─────────────────────────────────────────────────────────────────────

async def crawl(cfg, progress_cb=None):
    visited = OrderedDict()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                  '--disable-blink-features=AutomationControlled']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            viewport={'width': 1366, 'height': 768},
            locale='en-US',
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3]});
            window.chrome = {runtime: {}};
        """)

        tab = await context.new_page()
        await tab.goto(cfg.base_url, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(2)

        # Expand sidebar
        try:
            await tab.evaluate("""
                () => {
                    ['[aria-expanded="false"]','[class*="collapse"]','[class*="toggle"]'].forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => { try { el.click(); } catch(e) {} });
                    });
                }
            """)
            await asyncio.sleep(1)
        except Exception:
            pass

        html       = await tab.content()
        seed_links = extract_links(html, cfg.base_url, cfg.domain)
        queue      = [normalize(cfg.base_url)] + [l for l in seed_links if l != normalize(cfg.base_url)]
        queued     = set(queue)

        tabs = [tab] + [await context.new_page() for _ in range(CONCURRENT - 1)]

        idx = 0
        while idx < len(queue) and len(visited) < MAX_PAGES:
            batch = []
            while len(batch) < CONCURRENT and idx < len(queue) and len(visited) + len(batch) < MAX_PAGES:
                url = queue[idx]; idx += 1
                if url not in visited:
                    batch.append(url)
            if not batch:
                continue
            results = await asyncio.gather(
                *[fetch_one(tabs[i % CONCURRENT], batch[i], cfg) for i in range(len(batch))]
            )
            for url, (content, new_links) in zip(batch, results):
                visited[url] = content
                for link in new_links:
                    if link not in queued:
                        queued.add(link); queue.append(link)
            if progress_cb:
                progress_cb(len(visited), min(len(queue), MAX_PAGES), batch[-1])

        await browser.close()
    return visited


# ── PDF builder ───────────────────────────────────────────────────────────────

class DocPDF(FPDF):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(20, 20, 20)

    def header(self):
        self.set_font('Helvetica', 'I', 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, self.s(self.cfg.domain), align='C', new_x='LMARGIN', new_y='NEXT')

    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f'Page {self.page_no()}', align='C')

    def s(self, text):
        t = re.sub(r'[^\x00-\x7F]', ' ', text or '')
        t = re.sub(r'[\r\n\t]', ' ', t)
        t = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', t)
        return re.sub(r' {2,}', ' ', t).strip()

    def mc(self, w, h, txt):
        txt = re.sub(r'(\S{80})', r'\1 ', self.s(txt).strip())
        if not txt: return
        h = max(h, 5)
        self.set_x(self.l_margin)
        try:
            self.multi_cell(w, h, txt)
        except Exception:
            try:
                self.set_x(self.l_margin)
                self.multi_cell(w, h, txt[:200])
            except Exception:
                pass

    def cover(self, visited_count):
        self.add_page()
        self.set_fill_color(0, 96, 160)
        self.rect(0, 0, 210, 297, 'F')
        self.set_text_color(255, 255, 255)
        self.set_y(90)
        self.set_font('Helvetica', 'B', 22)
        self.mc(0, 12, self.cfg.domain)
        self.set_font('Helvetica', '', 16)
        self.cell(0, 10, 'Documentation', align='C', new_x='LMARGIN', new_y='NEXT')
        self.ln(8)
        self.set_font('Helvetica', 'I', 10)
        self.mc(0, 7, self.cfg.base_url)
        self.ln(5)
        self.set_font('Helvetica', '', 10)
        self.cell(0, 8, f'Total pages crawled: {visited_count}', align='C', new_x='LMARGIN', new_y='NEXT')
        self.set_text_color(0, 0, 0)

    def toc(self, entries):
        self.add_page()
        self.set_text_color(0, 0, 0)
        self.set_font('Helvetica', 'B', 18)
        self.cell(0, 12, 'Table of Contents', new_x='LMARGIN', new_y='NEXT')
        self.ln(2)
        self.set_draw_color(0, 96, 160)
        self.line(20, self.get_y(), 190, self.get_y())
        self.ln(5)
        for e in entries:
            indent = (e['level'] - 1) * 5
            self.set_x(20 + indent)
            self.set_font('Helvetica', 'B' if e['level'] == 1 else ('I' if e['level'] > 2 else ''),
                          10 if e['level'] == 1 else (9 if e['level'] == 2 else 8))
            title = self.s(e['title'][:75])
            pg    = str(e['page'])
            avail = 165 - indent - self.get_string_width(pg)
            dw    = self.get_string_width('.')
            dots  = max(0, int((avail - self.get_string_width(title)) / max(dw, 0.1)))
            self.cell(0, 6, ' '.join([title, '.' * dots, pg]), new_x='LMARGIN', new_y='NEXT')

    def doc_page(self, url, data):
        self.add_page()
        self.set_font('Helvetica', 'I', 7)
        self.set_text_color(130, 130, 130)
        self.cell(0, 5, self.s(url), new_x='LMARGIN', new_y='NEXT')
        self.set_draw_color(210, 210, 210)
        self.line(20, self.get_y(), 190, self.get_y())
        self.ln(3)
        sizes  = {1:15, 2:12, 3:10, 4:9}
        styles = {1:'B', 2:'B', 3:'B', 4:'BI'}
        colors = {1:(0,96,160), 2:(20,20,20), 3:(50,50,50), 4:(70,70,70)}
        for item in data.get('items', []):
            if item['type'] == 'section':
                h, t, lvl = self.s(item.get('heading','')), self.s(item.get('text','')), item.get('level',1)
                if h:
                    self.set_font('Helvetica', styles.get(lvl,'B'), sizes.get(lvl,9))
                    self.set_text_color(*colors.get(lvl,(0,0,0)))
                    self.mc(0, max(sizes.get(lvl,9)*0.65, 5), h)
                    if lvl == 1:
                        self.set_draw_color(0,96,160)
                        self.line(20, self.get_y(), 190, self.get_y())
                    self.ln(2); self.set_text_color(0,0,0)
                if t:
                    self.set_font('Helvetica', '', 9)
                    self.set_text_color(40,40,40)
                    self.mc(0, 5, t); self.ln(3)
            elif item['type'] == 'image':
                src      = item.get('src','')
                alt      = self.s(item.get('alt',''))
                img_path = item.get('local_path') or download_image(src, self.cfg.img_dir)
                if not src: continue
                if img_path:
                    try:
                        with Image.open(img_path) as im:
                            iw, ih = im.size
                        ratio = min(170/max(iw,1), 110/max(ih,1), 1.0)
                        w_mm  = min(iw*ratio*0.264583, 170)
                        h_mm  = min(ih*ratio*0.264583, 110)
                        if self.get_y() + h_mm > 265: self.add_page()
                        self.image(img_path, x=20, w=w_mm, h=h_mm); self.ln(2)
                        if alt:
                            self.set_font('Helvetica','I',7); self.set_text_color(120,120,120)
                            self.mc(0,4,alt); self.set_text_color(0,0,0)
                        self.ln(3)
                    except (OSError, ValueError):
                        pass
                elif alt:
                    self.set_font('Helvetica','I',8); self.set_text_color(120,120,120)
                    self.mc(0,5,f'[Image: {alt}]'); self.set_text_color(0,0,0); self.ln(2)


def build_pdf(cfg, visited):
    # Pass 1: dry-run for TOC page numbers
    m = DocPDF(cfg)
    m.cover(len(visited))
    m.add_page()
    toc_entries = []
    for url, data in visited.items():
        pg    = m.page + 1
        title = data.get('title') or url.split('/')[-1] or url
        depth = urlparse(url).path.rstrip('/').count('/') - 1
        toc_entries.append({'title': title, 'level': max(1, min(depth, 4)), 'page': pg})
        m.doc_page(url, data)

    # Pass 2: real PDF
    pdf = DocPDF(cfg)
    pdf.cover(len(visited))
    pdf.toc(toc_entries)
    for url, data in visited.items():
        pdf.doc_page(url, data)
    pdf.output(cfg.output_pdf)
    return cfg.output_pdf


async def run(url, work_dir, progress_cb=None):
    cfg     = setup(url, work_dir)
    visited = await crawl(cfg, progress_cb)
    if not visited:
        raise RuntimeError("No pages were crawled.")
    return build_pdf(cfg, visited), cfg.domain


async def crawl_only(url, work_dir, progress_cb=None):
    cfg     = setup(url, work_dir)
    visited = await crawl(cfg, progress_cb)
    if not visited:
        raise RuntimeError("No pages were crawled.")
    return visited, cfg.domain
