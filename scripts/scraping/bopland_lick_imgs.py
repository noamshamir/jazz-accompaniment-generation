# scrape_bopland_lick_imgs.py
# Collects <img class="lick-img"> sources from bopland.org treble-clef licks pages,
# and stores results in JSON + CSV under ../../data/bopland/

import os
import csv
import json
import time
from tqdm import tqdm
from urllib.parse import urljoin
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import WebDriverException, NoSuchElementException
# Additional Selenium imports for improved driver and waiting
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE = "https://bopland.org/database#treble-clef-licks//p{page}"

def get_driver():
    opts = Options()
    # Use standard headless for wider Chrome compatibility
    opts.add_argument("--headless")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.set_page_load_timeout(30)
    return driver

def collect_lick_imgs(max_pages=250, wait_seconds=0.5):
    driver = get_driver()
    all_srcs = []
    try:
        for p in tqdm(range(1, max_pages + 1)):
            url = BASE.format(page=p)
            driver.get(url)
            # Wait for document ready and give the SPA a moment to render
            WebDriverWait(driver, max(wait_seconds, 2)).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            # Try to wait for at least one candidate element or time out gracefully
            try:
                WebDriverWait(driver, wait_seconds).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".lick-img"))
                )
            except Exception:
                pass  # continue; some pages may still load images only on scroll

            # Lazy-load guard: scroll to bottom until height stops changing
            last_height = 0
            for _ in range(5):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.4)
                new_height = driver.execute_script("return document.body.scrollHeight;") or 0
                if new_height == last_height:
                    break
                last_height = new_height

            try:
                elems = driver.find_elements(By.CSS_SELECTOR, ".lick-img, img.lick-img")
            except (NoSuchElementException, WebDriverException):
                elems = []

            page_srcs = []
            for el in elems:
                src = (el.get_attribute("src") or "").strip()

                # If the element isn't an <img>, try to find a nested <img>
                if not src:
                    try:
                        nested = el.find_element(By.TAG_NAME, "img")
                        src = (nested.get_attribute("src") or "").strip()
                    except Exception:
                        pass

                # Fallback to srcset (take the first URL)
                if not src:
                    srcset = (el.get_attribute("srcset") or "").strip()
                    if srcset:
                        src = srcset.split()[0].strip()

                # Fallback to CSS background-image
                if not src:
                    style = (el.get_attribute("style") or "")
                    if "background-image" in style:
                        # extract url("...") if present
                        import re
                        m = re.search(r'url\((?:\"|\')?(.*?)(?:\"|\')?\)', style)
                        if m:
                            src = m.group(1).strip()

                if src:
                    abs_src = urljoin(driver.current_url, src)
                    page_srcs.append(abs_src)

            page_srcs = list(dict.fromkeys(page_srcs))  # de-dupe, keep order
            if not page_srcs:
                break  # stop when a page yields no results

            all_srcs.extend(page_srcs)
            time.sleep(0.5)  # be nice
    finally:
        driver.quit()

    return list(dict.fromkeys(all_srcs))  # de-dupe across pages

if __name__ == "__main__":
    lick_imgs = collect_lick_imgs()

    # Prepare save folder
    save_dir = os.path.join("..", "..", "data", "bopland")
    os.makedirs(save_dir, exist_ok=True)

    # Save JSON
    json_path = os.path.join(save_dir, "lick_imgs.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"lick_imgs": lick_imgs}, f, ensure_ascii=False, indent=2)

    # Save CSV
    csv_path = os.path.join(save_dir, "lick_imgs.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "src"])
        for i, src in enumerate(lick_imgs, start=1):
            writer.writerow([i, src])

    print(f"Collected {len(lick_imgs)} lick image links.")
    print(f"JSON saved to {json_path}")
    print(f"CSV saved to {csv_path}")