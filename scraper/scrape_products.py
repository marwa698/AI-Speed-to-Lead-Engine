import argparse
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

# ---------- إعدادات ----------
BASE_LIST_URL = "https://books.toscrape.com/catalogue/page-{}.html"
HEADERS = {"User-Agent": "ProductIntelligenceBot/1.0 (student project)"}
DELAY = 1  # ثانية بين كل طلب (rate limiting)

ROOT = Path(__file__).resolve().parent.parent   # فولدر المشروع
RAW_DIR = ROOT / "data" / "raw"
LOG_DIR = ROOT / "logs"


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "scraper.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


# ---------- جلب الصفحة (مع retry 3 مرات) ----------
@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def fetch(url):
    """بترجّع HTML الصفحة، أو None لو الصفحة مش موجودة (404)."""
    response = requests.get(url, headers=HEADERS, timeout=10)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    response.encoding = "utf-8"   # عشان رمز £ يطلع صح
    return response.text


# ---------- لينكات الكتب في صفحة قائمة ----------
def get_product_links(page_number):
    url = BASE_LIST_URL.format(page_number)
    html = fetch(url)
    if html is None:
        return []
    soup = BeautifulSoup(html, "lxml")
    return [urljoin(url, a["href"]) for a in soup.select("article.product_pod h3 a")]


# ---------- بيانات كتاب واحد (القيم زي ما هي، من غير تنضيف) ----------
def scrape_product(url):
    html = fetch(url)
    if html is None:
        raise ValueError(f"Product page not found: {url}")
    soup = BeautifulSoup(html, "lxml")
    return {
        "product_name": soup.find("h1").text,
        "category": soup.select("ul.breadcrumb li a")[2].text,
        "price": soup.select_one("p.price_color").text,           # "£53.74"
        "rating": soup.select_one("p.star-rating")["class"][1],   # "One"
        "availability": soup.select_one("p.availability").text.strip(),
        "product_url": url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------- البرنامج الرئيسي ----------
def main(limit=None):
    """بتسحب المنتجات وبترجّع مسار ملف الـ JSON (Airflow محتاجه)."""
    setup_logging()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # 1) اجمع لينكات الكتب من كل الصفحات
    links, page = [], 1
    while True:
        page_links = get_product_links(page)
        if not page_links:
            break                       # مفيش صفحات تانية
        links.extend(page_links)
        logging.info(f"Page {page}: {len(page_links)} links (total {len(links)})")
        time.sleep(DELAY)
        if limit and len(links) >= limit:
            links = links[:limit]
            break
        page += 1

    # 2) ادخلي صفحة كل كتاب
    products = []
    for i, url in enumerate(links, 1):
        try:
            products.append(scrape_product(url))
            logging.info(f"[{i}/{len(links)}] OK: {url}")
        except Exception as e:
            logging.error(f"[{i}/{len(links)}] FAILED: {url} - {e}")
        time.sleep(DELAY)

    # 3) احفظي الناتج
    name = "sample_products.json" if limit else f"products_{date.today().isoformat()}.json"
    path = RAW_DIR / name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    logging.info(f"Saved {len(products)} products to {path}")
    return str(path)
def retry_failed():
    """بتعيد سحب الكتب اللي فشلت (من logs/scraper.log) وبتضيفها لملف النهاردة."""
    setup_logging()
    path = RAW_DIR / f"products_{date.today().isoformat()}.json"
    products = json.loads(path.read_text(encoding="utf-8"))
    done = {p["product_url"] for p in products}

    log_text = (LOG_DIR / "scraper.log").read_text(encoding="utf-8")
    failed = {
        line.split("FAILED: ")[1].split(" - ")[0]
        for line in log_text.splitlines()
        if "FAILED: " in line
    }
    todo = sorted(failed - done)       # الفاشل اللي لسه ما اتسحبش
    logging.info(f"Retrying {len(todo)} failed products")

    still_failed = 0
    for i, url in enumerate(todo, 1):
        try:
            products.append(scrape_product(url))
            logging.info(f"[retry {i}/{len(todo)}] OK: {url}")
        except Exception as e:
            still_failed += 1
            logging.error(f"[retry {i}/{len(todo)}] FAILED AGAIN: {url} - {e}")
        time.sleep(DELAY)

    path.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(f"Now {len(products)} products in {path} ({still_failed} still failing)")
    return str(path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="اسحبي عدد محدد بس (للتجربة)")
    parser.add_argument("--retry-failed", action="store_true", help="أعيدي سحب الكتب الفاشلة بس")
    args = parser.parse_args()
    if args.retry_failed:
        retry_failed()
    else:
        main(limit=args.limit)