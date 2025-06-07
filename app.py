from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
import openai
import os
import traceback
import requests
import pytz
import ipaddress
from datetime import datetime
from functools import wraps
import time
from collections import defaultdict, Counter
import pandas as pd
import plotly.express as px

app = Flask(__name__, static_folder='frontend', template_folder='templates')
app.debug = True
CORS(app)

openai.api_key = os.getenv("OPENAI_API_KEY")

openai_usage_log = []
request_count_by_ip = defaultdict(int)
ip_geo_cache = {}


def is_public_ip(ip):
    try:
        ip_obj = ipaddress.ip_address(ip)
        return not (ip_obj.is_private or ip_obj.is_loopback)
    except:
        return False


def country_code_to_flag_emoji(country_code):
    try:
        if not country_code or len(country_code) != 2:
            return ""
        return ''.join(chr(127397 + ord(c.upper())) for c in country_code)
    except:
        return ""


def get_geo_info(ip):
    if ip in ip_geo_cache:
        print(f"[CACHE HIT] {ip} → {ip_geo_cache[ip]}")
        return ip_geo_cache[ip]

    print(f"[LOOKUP] Fetching geolocation for IP: {ip}")

    if not is_public_ip(ip):
        geo_info = {
            "client_country": "Private IP",
            "client_city": "-",
            "client_local_time": "Unavailable",
            "client_flag": ""
        }
        ip_geo_cache[ip] = geo_info
        return geo_info

    # First attempt: ipapi.co
    try:
        res = requests.get(f"https://ipapi.co/{ip}/json/", timeout=2)
        if res.status_code == 200:
            data = res.json()
            print(f"[IPAPI RESPONSE] {ip} → {data}")

            country = data.get("country_name")
            if country:
                city = data.get("city", "Unknown")
                country_code = data.get("country", "")
                flag = country_code_to_flag_emoji(country_code)
                timezone = data.get("timezone", "Asia/Jerusalem")

                try:
                    tz = pytz.timezone(timezone)
                    local_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
                except:
                    local_time = "Unavailable"

                geo_info = {
                    "client_country": country,
                    "client_city": city,
                    "client_local_time": local_time,
                    "client_flag": flag
                }
                ip_geo_cache[ip] = geo_info
                return geo_info
    except Exception as e:
        print(f"[IPAPI ERROR] {ip} → {e}")

    # Fallback attempt: ip-api.com
    try:
        res = requests.get(
            f"http://ip-api.com/json/{ip}?fields=status,country,city,countryCode,timezone",
            timeout=2)
        if res.status_code == 200:
            data = res.json()
            print(f"[IP-API Fallback] {ip} → {data}")

            if data.get("status") == "success":
                country = data.get("country", "Unknown")
                city = data.get("city", "Unknown")
                flag = country_code_to_flag_emoji(data.get("countryCode", ""))
                timezone = data.get("timezone", "Asia/Jerusalem")

                try:
                    tz = pytz.timezone(timezone)
                    local_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
                except:
                    local_time = "Unavailable"

                geo_info = {
                    "client_country": country,
                    "client_city": city,
                    "client_local_time": local_time,
                    "client_flag": flag
                }
                ip_geo_cache[ip] = geo_info
                return geo_info
    except Exception as e:
        print(f"[IP-API ERROR] {ip} → {e}")

    geo_info = {
        "client_country": "Unknown",
        "client_city": "-",
        "client_local_time": "Unavailable",
        "client_flag": ""
    }
    ip_geo_cache[ip] = geo_info
    return geo_info


def get_ist_now():
    ist = pytz.timezone("Asia/Jerusalem")
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")


def require_token(f):

    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify(
                {'error': 'Missing or invalid Authorization header'}), 401
        token = auth_header.split('Bearer ')[1]
        expected_token = os.getenv('ACCESS_TOKEN')
        if not token or token != expected_token:
            return jsonify({'error': 'Invalid access token'}), 401
        return f(*args, **kwargs)

    return decorated_function


@app.route('/')
def index():
    try:
        return send_from_directory('frontend', 'index.html')
    except Exception as e:
        return f"Error: {str(e)}", 500


@app.route("/chat", methods=["POST"])
@require_token
def chat():
    try:
        data = request.json
        prompt = data.get("prompt")
        if not prompt:
            return jsonify({"error": "Missing prompt", "status": "error"}), 400

        client_ip = request.headers.get("X-Forwarded-For",
                                        "").split(",")[0].strip()
        request_count_by_ip[client_ip] += 1
        geo = get_geo_info(client_ip)

        start_time = time.time()
        response = openai.ChatCompletion.create(model="gpt-4",
                                                messages=[{
                                                    "role": "user",
                                                    "content": prompt
                                                }])
        duration_ms = round((time.time() - start_time) * 1000)

        usage = response.usage
        reply_text = response.choices[0].message.content
        print(reply_text)
        openai_usage_log.append({
            "timestamp": get_ist_now(),
            "client_ip": client_ip,
            "client_country": geo["client_country"],
            "client_city": geo["client_city"],
            "client_local_time": geo["client_local_time"],
            "client_flag": geo["client_flag"],
            "request_count": request_count_by_ip[client_ip],
            "prompt": prompt,
            "prompt_length_chars": len(prompt),
            "prompt_length_words": len(prompt.split()),
            "token_usage": usage.total_tokens,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "response_time_ms": duration_ms,
            "model": response.model,
            "response_preview": reply_text[:200]
        })

        if len(openai_usage_log) > 100:
            openai_usage_log.pop(0)

        return jsonify({"reply": reply_text, "status": "success"})

    except Exception as e:
        print("Error:", str(e))
        print(traceback.format_exc())
        return jsonify({"error": str(e), "status": "error"}), 500


@app.route("/debug", methods=["GET"])
def debug_info():
    if not openai_usage_log:
        return jsonify({"info": "No requests logged yet."})
    sorted_log = sorted(openai_usage_log,
                        key=lambda x: (x["timestamp"], x["client_ip"]),
                        reverse=True)
    return jsonify(sorted_log)


@app.route("/summary", methods=["GET"])
def summary_info():
    if not openai_usage_log:
        return jsonify({"info": "No data yet."})

    ip_counter = Counter(entry["client_ip"] for entry in openai_usage_log)
    country_counter = Counter(entry["client_country"]
                              for entry in openai_usage_log)

    total_requests = len(openai_usage_log)
    total_tokens = sum(entry["token_usage"] for entry in openai_usage_log)
    avg_tokens = round(total_tokens / total_requests, 2)

    summary = {
        "total_requests": total_requests,
        "total_tokens_used": total_tokens,
        "average_tokens_per_request": avg_tokens,
        "top_ips": ip_counter.most_common(5),
        "top_countries": country_counter.most_common(5),
        "last_updated": get_ist_now()
    }

    return jsonify(summary)


@app.route("/live", methods=["GET"])
def live_dashboard():
    if not openai_usage_log:
        return "<h3>No data yet. Please send some requests first.</h3>"

    df = pd.DataFrame(openai_usage_log)
    df['timestamp'] = pd.to_datetime(df['timestamp'])

    total_requests = len(df)
    total_tokens = df['token_usage'].sum()
    avg_tokens = round(total_tokens / total_requests, 2)

    country_data = df['client_country'].value_counts().reset_index()
    country_data.columns = ['country', 'count']

    country_fig = px.bar(country_data,
                         x='country',
                         y='count',
                         labels={
                             'country': 'Country',
                             'count': 'Requests'
                         },
                         title="Requests per Country")
    country_chart = country_fig.to_html(full_html=False)

    time_fig = px.line(df.sort_values('timestamp'),
                       x='timestamp',
                       y='token_usage',
                       title="Token Usage Over Time")
    time_chart = time_fig.to_html(full_html=False)

    recent_data = df.sort_values('timestamp', ascending=False).head(10)[[
        'timestamp', 'client_ip', 'client_country', 'prompt_tokens',
        'completion_tokens', 'token_usage'
    ]]

    return render_template("live.html",
                           total_requests=total_requests,
                           total_tokens=total_tokens,
                           avg_tokens=avg_tokens,
                           recent_table=recent_data.to_html(
                               index=False, classes="table table-striped"),
                           country_chart=country_chart,
                           time_chart=time_chart)


@app.route("/generate", methods=["POST"])
@require_token
def generate():
    try:
        data = request.json
        prompt = data.get("prompt")
        if not prompt:
            return jsonify({"error": "Missing prompt", "status": "error"}), 400

        client_ip = request.headers.get("X-Forwarded-For",
                                        "").split(",")[0].strip()
        request_count_by_ip[client_ip] += 1
        geo = get_geo_info(client_ip)

        start_time = time.time()
        try:
            # Parse the prompt string as a dictionary
            prompt_dict = eval(prompt)
            response = openai.ChatCompletion.create(**prompt_dict)
        except Exception as e:
            return jsonify({
                "error": f"Invalid prompt format: {str(e)}",
                "status": "error"
            }), 400

        duration_ms = round((time.time() - start_time) * 1000)

        usage = response.usage
        reply_text = response.choices[0].message.content

        # Log the request
        openai_usage_log.append({
            "timestamp": get_ist_now(),
            "client_ip": client_ip,
            "client_country": geo["client_country"],
            "client_city": geo["client_city"],
            "client_local_time": geo["client_local_time"],
            "client_flag": geo["client_flag"],
            "request_count": request_count_by_ip[client_ip],
            "prompt": prompt,
            "prompt_length_chars": len(prompt),
            "prompt_length_words": len(prompt.split()),
            "token_usage": usage.total_tokens,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "response_time_ms": duration_ms,
            "model": response.model,
            "response_preview": reply_text[:200]
        })

        if len(openai_usage_log) > 100:
            openai_usage_log.pop(0)

        return jsonify({"reply": reply_text, "status": "success"})

    except Exception as e:
        print("Error:", str(e))
        print(traceback.format_exc())
        return jsonify({"error": str(e), "status": "error"}), 500


# if __name__ == "__main__":
#     port = int(os.environ.get("PORT", 8080))
#     app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    port = 5000  # Run on port 5000
    app.run(host="0.0.0.0", port=port)
