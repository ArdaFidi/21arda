import sys
import os
import time
import json
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from selenium import webdriver
from selenium.common.exceptions import (
    UnexpectedAlertPresentException,
    TimeoutException
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CFG_PATH     = os.path.join(BASE_DIR, "config.json")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
ARCHIVE_DIR  = os.path.join(BASE_DIR, "zips")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR,  exist_ok=True)

if not os.path.isfile(CFG_PATH):
    raise SystemExit(f"\u274c config.json not found in {BASE_DIR}")

try:
    with open(CFG_PATH, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
except json.JSONDecodeError as e:
    raise SystemExit(f"\u274c config.json is not valid JSON: {e}")

for key in ("start_date", "end_date"):
    if key not in cfg:
        raise SystemExit(f"\u274c config.json must contain '{key}'")

try:
    start_date = datetime.strptime(cfg["start_date"], "%Y-%m-%d").date()
    end_date   = datetime.strptime(cfg["end_date"],   "%Y-%m-%d").date()
except ValueError as e:
    raise SystemExit(f"\u274c Date format error: {e}")

reverse = bool(cfg.get("reverse", False))

if reverse:
    current   = end_date
    step      = timedelta(days=-1)
    condition = lambda d: d >= start_date
else:
    current   = start_date
    step      = timedelta(days=1)
    condition = lambda d: d <= end_date

BASE_URL     = "https://ekap.kik.gov.tr/EKAP/Ilan/BultenIndirme.aspx"
TENDER_TYPES = {
    "3": "HIZMET",
    "4": "DANISMANLIK",
}
INPUT_FMT    = "%d.%m.%Y"
NAME_FMT     = "%d%m%Y"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# install driver once and reuse path (step 4)
DRIVER_PATH = ChromeDriverManager().install()


def setup_driver(download_dir: str) -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless")
    opts.add_argument("--disable-gpu")
    opts.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    })
    return webdriver.Chrome(service=Service(DRIVER_PATH), options=opts)


def wait_for_download(directory: str, before: set) -> str | None:
    """Wait for a new file to appear, polling quickly (step 2)."""
    start_t = time.time()
    while time.time() - start_t < 30:  # shorter timeout
        time.sleep(0.5)
        added = set(os.listdir(directory)) - before
        non_cr = [f for f in added if not f.endswith(".crdownload")]
        if non_cr:
            return non_cr[0]
    return None


def wait_for_complete(directory: str) -> None:
    start_t = time.time()
    while time.time() - start_t < 10:
        if not any(fn.endswith(".crdownload") for fn in os.listdir(directory)):
            return
        time.sleep(0.2)


def download_for_type(d, val, name) -> None:
    dl_dir = os.path.join(DOWNLOAD_DIR, name)
    os.makedirs(dl_dir, exist_ok=True)
    driver = setup_driver(dl_dir)
    wait = WebDriverWait(driver, 20)
    try:
        driver.get(BASE_URL)
        date_input = wait.until(EC.element_to_be_clickable((
            By.ID,
            "ctl00_ContentPlaceHolder1_etBultenTarihi_EkapTakvimTextBox_etBultenTarihi"
        )))
        date_input.clear()
        date_input.send_keys(d.strftime(INPUT_FMT))

        Select(driver.find_element(
            By.ID, "ctl00_ContentPlaceHolder1_ddlstBxIhaleTur"
        )).select_by_value(val)

        before = set(os.listdir(dl_dir))
        try:
            driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_btnYukle").click()
        except UnexpectedAlertPresentException:
            try:
                driver.switch_to.alert.accept()
            except Exception:
                pass
            logging.info(f"    \u26a0\ufe0f Skipped {name} (holiday/weekend)")
            driver.quit()
            return

        downloaded = wait_for_download(dl_dir, before)
        if not downloaded:
            logging.warning(f"    \u2718 Timeout waiting for {name}")
            driver.quit()
            return

        wait_for_complete(dl_dir)
        src = os.path.join(dl_dir, downloaded)
        dst = os.path.join(ARCHIVE_DIR, f"BULTEN_{d.strftime(NAME_FMT)}_{name}.zip")
        os.replace(src, dst)
        logging.info(f"    \u2714 Saved {os.path.basename(dst)}")
    except TimeoutException:
        logging.warning(f"   \u2718 Date picker not found for {d.isoformat()}, skipping.")
    finally:
        driver.quit()


# main processing loop with parallel tender types (step 1)

d = current
while condition(d):
    if d.weekday() >= 5:
        logging.info(f"\u23ed Skipping weekend {d.isoformat()}")
        d += step
        continue

    logging.info(f"\u2192 Processing {d.isoformat()}")

    with ThreadPoolExecutor(max_workers=len(TENDER_TYPES)) as executor:
        futures = [executor.submit(download_for_type, d, val, name)
                   for val, name in TENDER_TYPES.items()]
        for fut in as_completed(futures):
            fut.result()

    d += step

logging.info("Done. All ZIPs are in the 'zips/' folder.")
