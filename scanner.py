import os
import time
import redis
import requests
import logging
import sys
import io
import re
from webdav3.client import Client
from PIL import Image, ExifTags
from datetime import datetime, timedelta
from croniter import croniter
from concurrent.futures import ThreadPoolExecutor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Config
NC_URL = os.getenv('NC_URL').rstrip('/')
NC_USER = os.getenv('NC_USER')
NC_PASS = os.getenv('NC_PASS')

NC_OPTIONS = {
    'webdav_hostname': NC_URL,
    'webdav_login':    NC_USER,
    'webdav_password': NC_PASS
}
client = Client(NC_OPTIONS)
r = redis.Redis(host=os.getenv('REDIS_HOST'), port=6379, decode_responses=True)

# Shared session for efficiency
session = requests.Session()
session.auth = (NC_USER, NC_PASS)

IGNORE_FILE = os.getenv('IGNORE_FILE', '.ignore')
MAX_WORKERS = int(os.getenv('SCANNER_PARALLEL', '4'))

def get_exif_data_from_bytes(data):
    try:
        img = Image.open(io.BytesIO(data))
        # Use getexif() instead of _getexif() for better format support (WebP, etc)
        exif_raw = img.getexif()
        if not exif_raw:
            return "Unknown", "Unknown"
            
        exif = { ExifTags.TAGS[k]: v for k, v in exif_raw.items() if k in ExifTags.TAGS }
        
        # Date taken is often in tag 36867 (DateTimeOriginal) 
        # but Tags mapping is more robust
        date_taken = exif.get('DateTimeOriginal', 'Unknown')
        if date_taken == "Unknown":
            # Fallback to other date tags
            date_taken = exif.get('DateTime', 'Unknown')
        
        # GPS
        gps_coords = "Present" if 34853 in exif_raw else "Unknown"
            
        return date_taken, gps_coords
    except Exception as e:
        return "Unknown", "Unknown"

def get_metadata(file_path):
    # WebDAV PROPFIND for favorite, fileid, getetag, and getcontentlength
    xml_data = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop><oc:favorite/><oc:fileid/><d:getetag/><d:getcontentlength/></d:prop></d:propfind>'
    try:
        url = NC_URL + '/' + file_path.lstrip('/')
        resp = session.request("PROPFIND", url, data=xml_data, headers={'Depth': '0'})
        resp.raise_for_status()
        
        is_fav = "<oc:favorite>1</oc:favorite>" in resp.text
        
        file_id = None
        match_id = re.search(r'<oc:fileid>(.*?)</oc:fileid>', resp.text)
        if match_id:
            file_id = match_id.group(1)
            
        etag = None
        match_etag = re.search(r'<d:getetag>(.*?)</d:getetag>', resp.text)
        if match_etag:
            etag = match_etag.group(1).strip('"')
            
        size = 0
        match_size = re.search(r'<d:getcontentlength>(.*?)</d:getcontentlength>', resp.text)
        if match_size:
            size = int(match_size.group(1))

        return is_fav, file_id, etag, size
    except Exception as e:
        logger.error(f"Metadata error for {file_path}: {e}")
        return False, None, None, 0

def process_file(file):
    r.incr("stats:last_scan_found")
    if not file.lower().endswith(('.jpg', '.jpeg', '.webp', '.png')):
        return
    
    # 1. Fetch metadata and check cache
    is_fav, file_id, etag, size = get_metadata(file)
    
    cached = r.hgetall(f"photo:{file}")
    if cached and cached.get('etag') == etag and etag:
        # Skip download and processing if etag matches
        # Just update the pool to ensure it's still there
        r.zadd("photo_pool", {f"photo:{file}": int(cached.get('weight', 10))})
        # Add a very infrequent log or just don't log at all for huge speed
        # But for debugging, let's keep it visible
        if r.incr("stats:logs_skipped") % 100 == 0:
             logger.info(f"Skipped {file} (cached and unchanged)...")
        return

    # 2. Prefer partial download for EXIF (Fetch first 256KB)
    url = NC_URL + '/' + file.lstrip('/')
    timestamp = "Unknown"
    gps = "Unknown"
    
    try:
        # Most EXIF data is in the first few KB
        # Fetching 256KB is usually enough even for WebP with chunks
        resp = session.get(url, headers={'Range': 'bytes=0-262143'})
        if resp.status_code in [200, 206]:
            timestamp, gps = get_exif_data_from_bytes(resp.content)
    except Exception as e:
        logger.error(f"Error reading EXIF for {file}: {e}")

    # 3. Calculate Weight
    weight = 10 
    
    # Fallback: Try to parse date from folder path if EXIF is missing
    if timestamp == "Unknown":
        # Look for YYYY-MM-DD or YYYYMMDD in path (greedy check)
        date_match = re.search(r'(\d{4})[-_]?(\d{2})[-_]?(\d{2})', file)
        if date_match:
            y, m, d = date_match.groups()
            # Simple validation to avoid matching high numbers like 99999999
            if 1970 <= int(y) <= 2100 and 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
                timestamp = f"{y}:{m}:{d} 12:00:00"
                logger.debug(f"Guessed date from path for {file}: {timestamp}")

    if timestamp != "Unknown":
        try:
            dt = datetime.strptime(timestamp, "%Y:%m:%d %H:%M:%S")
            age_years = (datetime.now() - dt).days / 365.0
            # Exponential decay: Newer photos are much more likely, 
            # but older photos still appear occasionally.
            weight = int(100 * (0.85 ** max(0, age_years)))
            
            # "Memory of the day" bonus: 10x weight if month and day match today
            now = datetime.now()
            if dt.month == now.month and dt.day == now.day:
                weight *= 10
                logger.info(f"Memory Bonus (10x) for {file} (Date: {dt.date()})")
        except Exception as e:
            logger.debug(f"Weight calculation failed for {file}: {e}")
            pass

    if is_fav: weight *= 5
    weight = max(1, weight)

    # 4. Store in Redis
    r.hset(f"photo:{file}", mapping={
        "path": file,
        "weight": weight,
        "timestamp": timestamp,
        "gps": gps,
        "file_id": file_id or "",
        "etag": etag or "",
        "size": size
    })
    r.zadd("photo_pool", {f"photo:{file}": weight})
    r.incr("stats:last_scan_processed")
    logger.info(f"Processed {file}: Weight={weight}, Cached={bool(cached)}")

def scan_recursive(path, executor):
    try:
        r.sadd("stats:scanned_paths", path)
        items = client.list(path)
        if not items: return

        # Check if directory is ignored
        for item in items[1:]:
            fn = os.path.basename(item.rstrip('/'))
            if fn == IGNORE_FILE:
                logger.info(f"Ignoring {path}")
                return

        for item in items[1:]:
            full_path = item if item.startswith('/') else os.path.join(path, item)
            
            if full_path.endswith('/'):
                scan_recursive(full_path, executor)
            else:
                executor.submit(process_file, full_path)
                
    except Exception as e:
        logger.error(f"Error scanning {path}: {e}")

def run_scan():
    photo_path = os.getenv('NC_PHOTO_PATH', '/Photos/')
    r.set("stats:last_scan_found", 0)
    r.set("stats:last_scan_processed", 0)
    r.delete("stats:scanned_paths")
    
    logger.info(f"Scanning {photo_path} with {MAX_WORKERS} threads...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        scan_recursive(photo_path, executor)

if __name__ == "__main__":
    cron_schedule = os.getenv('SCAN_CRON', '0 1 * * *') # Default daily at 1 AM
    logger.info(f"Scanner started. Schedule: {cron_schedule}")

    # Run immediately on startup
    logger.info("Starting initial scan...")
    r.set("scanner:status", "running")
    run_scan()
    r.set("scanner:status", "idle")
    r.set("stats:last_scan_time", datetime.now().isoformat())

    while True:
        try:
            now = datetime.now()
            iter = croniter(cron_schedule, now)
            next_run = iter.get_next(datetime)
            delay = (next_run - now).total_seconds()
            
            logger.info(f"Next scan scheduled for {next_run} (in {int(delay)} seconds)")
            if delay > 0:
                time.sleep(delay)
            
            logger.info("Starting scheduled scan...")
            r.set("scanner:status", "running")
            run_scan()
            r.set("scanner:status", "idle")
            r.set("stats:last_scan_time", datetime.now().isoformat())
        except Exception as e:
            logger.error(f"Error in scheduler loop: {e}")
            time.sleep(60) # Retry after 1 min on error
