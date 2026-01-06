import os
import redis
import requests
import io
import base64
import qrcode
from PIL import Image, ImageOps
from flask import Flask, render_template_string, Response, stream_with_context
from datetime import datetime

app = Flask(__name__)
r = redis.Redis(host=os.getenv('REDIS_HOST'), port=6379, decode_responses=True)

# Translations
TRANSLATIONS = {
    'en': {
        'tom': 'Tom:',
        'indexing': 'Indexing...',
        'photos': 'Photos',
        'index': 'Index:',
        'months': ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
    },
    'de': {
        'tom': 'Morgen:',
        'indexing': 'Indiziere...',
        'photos': 'Fotos',
        'index': 'Index:',
        'months': ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni', 'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember']
    },
    'fr': {
        'tom': 'Demain:',
        'indexing': 'Indexation...',
        'photos': 'Photos',
        'index': 'Index:',
        'months': ['Janvier', 'Février', 'Mars', 'Avril', 'Mai', 'Juin', 'Juillet', 'Août', 'Septembre', 'Octobre', 'Novembre', 'Décembre']
    },
    'es': {
        'tom': 'Mañana:',
        'indexing': 'Indexando...',
        'photos': 'Fotos',
        'index': 'Índice:',
        'months': ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']
    }
}

@app.route('/')
def index():
    lang = os.getenv('APP_LANG', 'en')
    t = TRANSLATIONS.get(lang, TRANSLATIONS['en'])

    # Pick random photo based on weight from Redis
    photo_key = r.zrandmember("photo_pool")
    if not photo_key:
        return "No photos found in pool. Please wait for the scanner to populate the database."
        
    data = r.hgetall(photo_key)
    
    # Parse Date
    date_obj = None
    if data.get('timestamp') and data['timestamp'] != 'Unknown':
        try:
            date_obj = datetime.strptime(data['timestamp'], "%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass
            
    month_str = t['months'][date_obj.month - 1] if date_obj else ""
    year_str = date_obj.strftime("%Y") if date_obj else ""
    
    # Parse Location (Folder name as proxy)
    location_str = ""
    try:
        folder_name = os.path.basename(os.path.dirname(data['path']))
        # Optional: Clean up date prefix from folder name if present (e.g. "2009-05-24 Berliner Zoo" -> "Berliner Zoo")
        parts = folder_name.split(' ', 1)
        if len(parts) > 1 and any(char.isdigit() for char in parts[0]):
             location_str = parts[1]
        else:
             location_str = folder_name
    except:
        location_str = "Unknown Location"

    scanner_status = r.get("scanner:status") or "idle"

    # Weather Logic
    weather_data = None
    lat = os.getenv('WEATHER_LAT')
    lon = os.getenv('WEATHER_LON')
    
    if lat and lon:
        try:
            # Check cache first (15 min cache)
            cached_weather = r.get("weather:data")
            if cached_weather:
                import json
                weather_data = json.loads(cached_weather)
            else:
                # Fetch from Open-Meteo
                url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=2"
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    w = resp.json()
                    
                    # WMO Weather Codes mapping (simplified)
                    def get_icon(code):
                        if code == 0: return "☀️"
                        if code in [1,2,3]: return "⛅"
                        if code in [45,48]: return "🌫️"
                        if code in [51,53,55,61,63,65]: return "🌧️"
                        if code in [71,73,75,77]: return "❄️"
                        if code in [95,96,99]: return "⛈️"
                        return "🌡️"

                    current_temp = round(w['current']['temperature_2m'])
                    current_icon = get_icon(w['current']['weather_code'])
                    
                    # Tomorrow
                    tomorrow_max = round(w['daily']['temperature_2m_max'][1])
                    tomorrow_min = round(w['daily']['temperature_2m_min'][1])
                    tomorrow_icon = get_icon(w['daily']['weather_code'][1])
                    
                    weather_data = {
                        'current': {'temp': current_temp, 'icon': current_icon},
                        'tomorrow': {'max': tomorrow_max, 'min': tomorrow_min, 'icon': tomorrow_icon}
                    }
                    
                    # Cache for 15 mins
                    import json
                    r.setex("weather:data", 900, json.dumps(weather_data))
        except Exception as e:
            print(f"Weather error: {e}")

    # QR Code Logic
    qr_code_b64 = None
    nextcloud_link = "#"
    if os.getenv('SHOW_QR_CODE', 'false').lower() == 'true':
        try:
            nc_url = os.getenv('NC_URL', '')
            # Try to determine base URL
            base_url = nc_url
            if '/remote.php' in nc_url:
                base_url = nc_url.split('/remote.php')[0]
            
            # Extract folder and filename
            # data['path'] is usually something like /Photos/Album/img.jpg in WebDAV
            file_path = data.get('path', '')
            if file_path:
                directory = os.path.dirname(file_path)
                filename = os.path.basename(file_path)
                
                # Construct UI Link using File ID if available (Nextcloud 28+ style)
                from urllib.parse import quote
                file_id = data.get('file_id')
                if file_id:
                    # Nextcloud "select" URL is very reliable for opening a specific file by ID
                    nextcloud_link = f"{base_url}/index.php/apps/files/select/{file_id}"
                else:
                    # Fallback for older scans without file_id
                    nextcloud_link = f"{base_url}/index.php/apps/files/?dir={quote(directory)}&scrollto={quote(filename)}"
                
                print(f"DEBUG: Nextcloud Link Generated: {nextcloud_link}")
                
                # Generate QR
                qr = qrcode.QRCode(box_size=3, border=1)
                qr.add_data(nextcloud_link)
                qr.make(fit=True)
                
                img_io = io.BytesIO()
                qr_img = qr.make_image(fill_color="white", back_color="transparent")
                qr_img.save(img_io, format="PNG")
                img_io.seek(0)
                qr_code_b64 = base64.b64encode(img_io.getvalue()).decode()
        except Exception as e:
             print(f"QR Gen Error: {e}")

    total_photos = r.zcard("photo_pool") or 0
    last_scan_time = r.get("stats:last_scan_time")
    last_scan_str = ""
    if last_scan_time:
        try:
            dt = datetime.fromisoformat(last_scan_time)
            last_scan_str = dt.strftime("%d.%m %H:%M")
        except:
            pass

    return render_template_string("""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body, html { margin: 0; padding: 0; height: 100%; overflow: hidden; background: #000; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: white; }
                .container { position: relative; width: 100%; height: 100%; display: flex; justify-content: center; align-items: center; }
                .background {
                    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                    background-image: url('/image{{ data.path }}');
                    background-size: cover;
                    background-position: center;
                    filter: blur(25px) brightness(0.5);
                    z-index: 1;
                    transform: scale(1.1); /* Prevent blur edges */
                }
                .photo {
                    position: relative;
                    max-width: 95%;
                    max-height: 95%;
                    z-index: 2;
                    box-shadow: 0 0 30px rgba(0,0,0,0.7);
                }
                .overlay-top-right {
                    position: absolute; top: 30px; right: 40px; z-index: 3;
                    display: flex; flex-direction: column; align-items: flex-end;
                }
                .clock {
                    font-size: 3em; font-weight: 300;
                    text-shadow: 2px 2px 4px rgba(0,0,0,0.8);
                    font-family: monospace;
                }
                .weather-container {
                    margin-top: 10px;
                    display: flex;
                    flex-direction: column;
                    align-items: flex-end;
                    text-shadow: 1px 1px 3px rgba(0,0,0,0.8);
                    font-size: 2.5em;
                }
                .weather-row { display: flex; gap: 10px; align-items: center; }
                .weather-label { font-size: 0.7em; opacity: 0.8; margin-right: 5px; }
                
                .overlay-bottom-left {
                    position: absolute; bottom: 40px; left: 40px; z-index: 3;
                    text-shadow: 2px 2px 4px rgba(0,0,0,0.8);
                    display: flex; flex-direction: column;
                    align-items: flex-start;
                }
                .meta-row { display: flex; align-items: baseline; }
                .camera-icon { font-size: 1.2em; margin-right: 8px; }
                .month { font-size: 1.5em; font-weight: 600; text-transform: uppercase; margin-right: 10px; }
                .year-row { display: flex; align-items: baseline; margin-top: -10px; }
                .year { font-size: 5em; font-weight: 700; line-height: 1; letter-spacing: -2px; }
                .location { font-size: 2.5em; font-family: 'Georgia', serif; margin-left: 20px; font-weight: 400; }
                .badges-container {
                    position: absolute; bottom: 40px; right: 40px; z-index: 3;
                    display: flex; flex-direction: column; align-items: flex-end; gap: 10px;
                }
                .status-badge {
                    background-color: rgba(0, 0, 0, 0.6);
                    padding: 8px 12px;
                    border-radius: 20px;
                    font-size: 0.9em;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    backdrop-filter: blur(5px);
                    border: 1px solid rgba(255,255,255,0.1);
                }
                .status-dot {
                    width: 8px;
                    height: 8px;
                    background-color: #4CAF50;
                    border-radius: 50%;
                    animation: pulse 1.5s infinite;
                }
                .qr-container {
                    position: absolute; bottom: 120px; right: 40px; z-index: 3;
                    opacity: 0.7; transition: opacity 0.3s;
                }
                .qr-container:hover { opacity: 1; }
                .qr-img { border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
                
                @keyframes pulse {
                    0% { opacity: 1; transform: scale(1); }
                    50% { opacity: 0.5; transform: scale(0.8); }
                    100% { opacity: 1; transform: scale(1); }
                }
            </style>
            <script>
                function updateTime() {
                    const now = new Date();
                    const hours = String(now.getHours()).padStart(2, '0');
                    const minutes = String(now.getMinutes()).padStart(2, '0');
                    document.getElementById('clock').textContent = hours + ':' + minutes;
                }
                setInterval(updateTime, 1000);
                
                // Configurable reload logic
                const reloadInterval = {{ reload_interval * 1000 }};
                const quietTime = "{{ quiet_time }}";

                function checkReload() {
                    if (!quietTime) {
                        window.location.reload();
                        return;
                    }

                    const now = new Date();
                    const currentTime = now.getHours() * 60 + now.getMinutes();
                    
                    const ranges = quietTime.split(',');
                    let isQuiet = false;

                    for (const range of ranges) {
                        const parts = range.trim().split('-');
                        if (parts.length !== 2) continue;

                        const [startH, startM] = parts[0].split(':').map(Number);
                        const [endH, endM] = parts[1].split(':').map(Number);
                        
                        const start = startH * 60 + startM;
                        const end = endH * 60 + endM;

                        if (start <= end) {
                            if (currentTime >= start && currentTime < end) {
                                isQuiet = true;
                                break;
                            }
                        } else {
                            // Overnight range (e.g. 22:00-06:00)
                            if (currentTime >= start || currentTime < end) {
                                isQuiet = true;
                                break;
                            }
                        }
                    }

                    if (!isQuiet) {
                        window.location.reload();
                    } else {
                        // Check again after interval
                        setTimeout(checkReload, reloadInterval);
                    }
                }

                setTimeout(checkReload, reloadInterval);
            </script>
        </head>
        <body onload="updateTime()">
            <div class="container">
                <div class="background"></div>
                <img src="/image{{ data.path }}" class="photo">
                
                <div class="overlay-top-right">
                    <div id="clock" class="clock">--:--</div>
                    {% if weather %}
                    <div class="weather-container">
                        <div class="weather-row">
                            <span>{{ weather.current.icon }} {{ weather.current.temp }}°C</span>
                        </div>
                        <div class="weather-row" style="font-size: 0.8em; opacity: 0.9;">
                            <span class="weather-label">{{ t.tom }}</span>
                            <span>{{ weather.tomorrow.icon }} {{ weather.tomorrow.min }}° / {{ weather.tomorrow.max }}°</span>
                        </div>
                    </div>
                    {% endif %}
                </div>
                
                <div class="overlay-bottom-left">
                    <div class="meta-row">
                        <span class="camera-icon">📷</span>
                        <span class="month">{{ month }}</span>
                    </div>
                    <div class="year-row">
                        <span class="year">{{ year }}</span>
                        <span class="location">{{ location }}</span>
                    </div>
                </div>

                {% if qr_code %}
                <div class="qr-container">
                    <a href="{{ nc_link }}" target="_blank">
                        <img src="data:image/png;base64,{{ qr_code }}" class="qr-img">
                    </a>
                </div>
                {% endif %}

                <div class="badges-container">
                    <div class="status-badge">
                        <span>{{ total_photos }} {{ t.photos }}</span>
                    </div>
                    
                    {% if scanner_status == 'running' %}
                    <div class="status-badge">
                        <div class="status-dot"></div>
                        <span>{{ t.indexing }}</span>
                    </div>
                    {% elif last_scan_str %}
                    <div class="status-badge" style="opacity: 0.7;">
                        <span>{{ t.index }} {{ last_scan_str }}</span>
                    </div>
                    {% endif %}
                </div>
            </div>
        </body>
        </html>
    """, data=data, month=month_str, year=year_str, location=location_str, scanner_status=scanner_status, weather=weather_data, total_photos=total_photos, last_scan_str=last_scan_str, t=t, qr_code=qr_code_b64, nc_link=nextcloud_link, reload_interval=int(os.getenv('APP_RELOAD_INTERVAL', '30')), quiet_time=os.getenv('APP_QUIET_TIME', ''))

@app.route('/image/<path:filepath>')
def image_proxy(filepath):
    # Ensure filepath starts with / if it's missing
    if not filepath.startswith('/'):
        filepath = '/' + filepath
        
    full_url = f"{os.getenv('NC_URL')}{filepath}"
    
    try:
        # Fetch image from Nextcloud
        req = requests.get(full_url, auth=(os.getenv('NC_USER'), os.getenv('NC_PASS')))
        req.raise_for_status()
        
        # Open image and apply EXIF rotation
        image = Image.open(io.BytesIO(req.content))
        image = ImageOps.exif_transpose(image)
        
        # Save to buffer
        img_io = io.BytesIO()
        # Convert to RGB if necessary (e.g. for PNG with transparency saving as JPEG, though we prefer keeping format)
        # For simplicity and compatibility, we can convert to JPEG or keep original format if supported.
        # Let's try to preserve format, defaulting to JPEG if unknown.
        fmt = image.format or 'JPEG'
        image.save(img_io, format=fmt)
        img_io.seek(0)
        
        return Response(img_io, content_type=f'image/{fmt.lower()}')
    except Exception as e:
        return f"Error fetching image: {e}", 500

@app.route('/info')
def info():
    total_photos = r.zcard("photo_pool")
    last_scan_found = r.get("stats:last_scan_found") or 0
    last_scan_processed = r.get("stats:last_scan_processed") or 0
    scanned_paths = r.smembers("stats:scanned_paths")
    
    return {
        "total_photos_in_db": int(total_photos),
        "last_scan_found": int(last_scan_found),
        "last_scan_processed": int(last_scan_processed),
        "scanned_paths": list(scanned_paths)
    }


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
