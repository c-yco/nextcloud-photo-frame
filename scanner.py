import os
import time
import redis
import requests
import logging
import sys
from webdav3.client import Client
from PIL import Image, ExifTags
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Config
NC_OPTIONS = {
    'webdav_hostname': os.getenv('NC_URL'),
    'webdav_login':    os.getenv('NC_USER'),
    'webdav_password': os.getenv('NC_PASS')
}
client = Client(NC_OPTIONS)
r = redis.Redis(host=os.getenv('REDIS_HOST'), port=6379, decode_responses=True)

def get_exif_data(local_path):
    try:
        img = Image.open(local_path)
        if not img._getexif():
            return "Unknown", "Unknown"
            
        exif = { ExifTags.TAGS[k]: v for k, v in img._getexif().items() if k in ExifTags.TAGS }
        
        # Date
        date_taken = exif.get('DateTimeOriginal', 'Unknown')
        
        # GPS
        gps_info = exif.get('GPSInfo')
        gps_coords = "Unknown"
        if gps_info:
            # Placeholder for full GPS conversion logic
            gps_coords = "Present" 
            
        return date_taken, gps_coords
    except Exception as e:
        logger.error(f"Error reading EXIF: {e}")
        return "Unknown", "Unknown"

def is_favorite(file_path):
    # WebDAV PROPFIND for {http://owncloud.org/ns}favorite
    data = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop><oc:favorite/></d:prop></d:propfind>'
    try:
        resp = requests.request("PROPFIND", os.getenv('NC_URL') + file_path, 
                                auth=(os.getenv('NC_USER'), os.getenv('NC_PASS')), 
                                data=data, headers={'Depth': '0'})
        return "<oc:favorite>1</oc:favorite>" in resp.text
    except:
        return False

def process_file(file):
    r.incr("stats:last_scan_found")
    if not file.lower().endswith(('.jpg', '.jpeg', '.webp', '.png')):
        logger.info(f"Skipping non-image file: {file}")
        return
    
    # Download for EXIF
    local_path = f"/tmp/{os.path.basename(file)}"
    timestamp = "Unknown"
    gps = "Unknown"
    
    try:
        client.download_sync(remote_path=file, local_path=local_path)
        timestamp, gps = get_exif_data(local_path)
        if os.path.exists(local_path):
            os.remove(local_path)
    except Exception as e:
        logger.error(f"Error processing {file}: {e}")

    # Logic for weights
    # Base weight for unknown date
    weight = 10 
    
    if timestamp != "Unknown":
        try:
            # Parse EXIF date format: YYYY:MM:DD HH:MM:SS
            dt = datetime.strptime(timestamp, "%Y:%m:%d %H:%M:%S")
            age_years = (datetime.now() - dt).days / 365.0
            
            # Exponential decay: Newer photos have higher weight
            # Factor 0.85 means weight reduces by ~15% every year
            # Age 0: 100
            # Age 5: ~44
            # Age 10: ~19
            # Age 20: ~3
            weight = int(100 * (0.85 ** max(0, age_years)))
        except Exception as e:
            logger.warning(f"Could not calculate age for {file}: {e}")

    is_fav = is_favorite(file)
    if is_fav: weight *= 5
    
    # Ensure minimum weight of 1
    weight = max(1, weight)

    # 2. Store in Redis
    r.hset(f"photo:{file}", mapping={
        "path": file,
        "weight": weight,
        "timestamp": timestamp,
        "gps": gps
    })
    r.zadd("photo_pool", {f"photo:{file}": weight})
    r.incr("stats:last_scan_processed")
    logger.info(f"Processed {file}: Weight={weight}, Date={timestamp}, GPS={gps}")

def scan_recursive(path):
    try:
        # Add to scanned paths
        r.sadd("stats:scanned_paths", path)
        logger.info(f"Scanning directory: {path}")
        
        items = client.list(path)
        if not items: return

        # First item is the directory itself, skip it
        for item in items[1:]:
            logger.info(f"Found item in {path}: {item}")
            
            full_path = item
            if not item.startswith('/'):
                 full_path = os.path.join(path, item)
            
            # Check if directory (trailing slash convention)
            if full_path.endswith('/'):
                scan_recursive(full_path)
            else:
                process_file(full_path)
                
    except Exception as e:
        logger.error(f"Error scanning {path}: {e}")

def run_scan():
    photo_path = os.getenv('NC_PHOTO_PATH', '/Photos/')
    
    # Reset stats
    r.set("stats:last_scan_found", 0)
    r.set("stats:last_scan_processed", 0)
    r.delete("stats:scanned_paths")
    
    logger.info(f"Starting recursive scan of {photo_path}")
    scan_recursive(photo_path)

if __name__ == "__main__":
    while True:
        logger.info("Starting scan...")
        r.set("scanner:status", "running")
        run_scan()
        r.set("scanner:status", "idle")
        logger.info("Scan complete. Sleeping...")
        time.sleep(3600)
