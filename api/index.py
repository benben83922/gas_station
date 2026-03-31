import re
from flask import Flask, json, request, jsonify, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    QuickReply,
    QuickReplyButton,
    LocationAction,
    LocationMessage,
)
import math
import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# 替換成您在步驟一取得的金鑰
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")

# -------------------------
# 建立 Flask 伺服器
# -------------------------
app = Flask(__name__)

# 初始化 LineBotApi 和 WebhookHandler
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# -------------------------
# Supabase 查詢輔助函式
# -------------------------
def query_table(table_name, filters=None):
    """查詢 Supabase 表，回傳 list[dict]。
    filters: list of (method, args) e.g. [("neq", ("gas_92", 0)), ("eq", ("gas_ss", "1"))]
    """
    query = supabase_client.table(table_name).select("*")
    if filters:
        for method, args in filters:
            query = getattr(query, method)(*args)
    response = query.execute()
    return response.data


# -------------------------
# Haversine 計算兩點距離
# -------------------------
def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # 地球半徑 (公里)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c  # 公里數


# -------------------------
# 中油地址處理
# -------------------------
def format_cpc_address(s):
    """處理中油加油站地址，去除括號內文字並加上縣市區域前綴。"""
    address = s.get("address", "")
    if "(" in address or "（" in address:
        address = re.sub(r"[\(（][^）\)]*[\)）]", "", address)
    return s.get("country", "") + s.get("district", "") + address


# -------------------------
# API: 查詢所有加油站
# -------------------------
@app.route("/api/gas/all", methods=["GET"])
def get_all_gas_stations():

    # 從 Supabase 取得所有加油站
    stations_fpcc, stations_cpc = fetch_all_stations()

    result = []
    for s in stations_fpcc:
        result.append(build_station_item(s, "台塑"))
    for s in stations_cpc:
        result.append(build_station_item(s, "中油"))
    return json.dumps(result)


# -------------------------
# 建立 Supabase 篩選條件
# -------------------------
def build_filters(fuels, open_type, gas_ss, table_type):
    """根據前端參數建立 Supabase query filters。
    table_type: "fpcc" 或 "cpc"
    """
    filters = []

    # 油品篩選
    if fuels:
        for fuel in fuels:
            filters.append(("neq", (f"gas_{fuel}", 0)))

    # 營業時間篩選
    if open_type:
        if open_type == "24h":
            if table_type == "fpcc":
                filters.append(("eq", ("open_time", "24小時")))
            else:
                filters.append(("or_", ("open_time.eq.00:00-24:00,open_time.eq.00:00-23:59",)))
        elif open_type == "not24h":
            if table_type == "fpcc":
                filters.append(("neq", ("open_time", "24小時")))
            else:
                filters.append(("neq", ("open_time", "00:00-24:00")))
                filters.append(("neq", ("open_time", "00:00-23:59")))

    # 自助加油篩選
    if gas_ss and gas_ss != "all":
        if gas_ss == "yes":
            filters.append(("eq", ("gas_ss", "1")))
        elif gas_ss == "no":
            filters.append(("eq", ("gas_ss", "0")))

    return filters


# -------------------------
# 組裝單筆加油站結果
# -------------------------
def build_station_item(s, station_type, dist=None):
    """將一筆加油站資料組裝成 API 回傳格式。"""
    if station_type == "中油":
        address = format_cpc_address(s)
    else:
        address = s["address"]

    item = {
        "type": station_type,
        "name": s["station_name"],
        "address": address,
        "phone": s["phone"],
        "open_time": s["open_time"],
        "gas_92": s["gas_92"],
        "gas_95": s["gas_95"],
        "gas_98": s["gas_98"],
        "gas_diesel": s["gas_diesel"],
        "gas_ss": s["gas_ss"],
        "lat": s["latitude"],
        "lng": s["longitude"],
    }
    if dist is not None:
        item["distance_km"] = round(dist, 3)
    return item


# -------------------------
# 查詢兩品牌加油站
# -------------------------
def fetch_all_stations(fpcc_filters=None, cpc_filters=None, brand="all"):
    """查詢台塑/中油加油站，依 brand 決定查哪些表。"""
    stations_fpcc = []
    stations_cpc = []
    if brand in ("all", "fpcc"):
        stations_fpcc = query_table("fpcc_gas_station", fpcc_filters)
    if brand in ("all", "cpc"):
        stations_cpc = query_table("cpc_gas_station", cpc_filters)
    return stations_fpcc, stations_cpc


# -------------------------
# 過濾距離範圍內的加油站
# -------------------------
def filter_by_distance(stations, station_type, user_lat, user_lng, range_km, include_gas_types=False):
    """過濾距離範圍內的加油站並組裝結果。"""
    result = []
    for s in stations:
        dist = haversine(user_lat, user_lng, float(s["latitude"]), float(s["longitude"]))
        if dist <= range_km:
            item = build_station_item(s, station_type, dist)
            if include_gas_types:
                item["gas_types"] = format_gas_types(s)
            result.append(item)
    return result


# -------------------------
# API: 查詢附近加油站
# -------------------------
@app.route("/api/gas/nearby", methods=["POST"])
def get_nearby_gas_stations():
    data = request.get_json()
    if not data:
        return jsonify({"error": "請提供 JSON"}), 400

    has_location = "lat" in data and "lng" in data
    if has_location:
        user_lat = float(data.get("lat"))
        user_lng = float(data.get("lng"))
    range_km = float(data.get("range_km", 1.0))  # 預設 1 公里
    fuels = data.get("selectedFuels")
    brand = data.get("brand")
    open_type = data.get("openType")
    gas_ss = data.get("gas_ss")
    result = []

    # 建立篩選條件
    fpcc_filters = build_filters(fuels, open_type, gas_ss, "fpcc")
    cpc_filters = build_filters(fuels, open_type, gas_ss, "cpc")

    # 查詢對應品牌
    stations_fpcc, stations_cpc = fetch_all_stations(fpcc_filters, cpc_filters, brand)

    if has_location:
        result += filter_by_distance(stations_fpcc, "台塑", user_lat, user_lng, range_km)
        result += filter_by_distance(stations_cpc, "中油", user_lat, user_lng, range_km)
    else:
        for s in stations_fpcc:
            result.append(build_station_item(s, "台塑"))
        for s in stations_cpc:
            result.append(build_station_item(s, "中油"))

    # 依距離排序
    if has_location and result:
        result.sort(key=lambda x: x["distance_km"])
        return jsonify({"count": len(result), "range_km": range_km, "data": result})
    else:
        return jsonify({"count": len(result), "data": result})


# Line Bot 的 Webhook 接收路徑
@app.route("/api/callback", methods=["GET", "POST"])
def callback():
    if request.method == "GET":
        return "OK"

    elif request.method == "POST":
        signature = request.headers.get("X-Line-Signature")
        body = request.get_data(as_text=True)
        app.logger.info("Request body: " + body)

        try:
            handler.handle(body, signature)
        except InvalidSignatureError:
            print(
                "Invalid signature. Please check your channel access token/channel secret."
            )
            abort(400)

        return "OK"


# 處理訊息事件
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_message = event.message.text

    if user_message == "附近加油站":
        location_button = QuickReplyButton(action=LocationAction(label="分享目前位置"))
        quick_reply = QuickReply(items=[location_button])
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="好的，請點擊下方「分享目前位置」按鈕，我才能為您查詢 1 公里內的附近加油站。",
                quick_reply=quick_reply,
            ),
        )
    elif user_message == "即時油價":
        response = get_gas_price()
        if isinstance(response, str):
            reply_message = TextSendMessage(text=response)
        else:
            reply_message = response
        line_bot_api.reply_message(event.reply_token, reply_message)


# 處理位置事件
@handler.add(MessageEvent, message=LocationMessage)
def handle_location_message(event):
    latitude = event.message.latitude
    longitude = event.message.longitude

    result_text = get_nearby_gas_stations_for_line_bot(latitude, longitude)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result_text))


def get_nearby_gas_stations_for_line_bot(user_lat, user_lng):
    range_km = float(1.0)
    result = []

    try:
        stations_fpcc, stations_cpc = fetch_all_stations()
        result += filter_by_distance(stations_fpcc, "台塑", user_lat, user_lng, range_km, include_gas_types=True)
        result += filter_by_distance(stations_cpc, "中油", user_lat, user_lng, range_km, include_gas_types=True)

        if result:
            result.sort(key=lambda x: x["distance_km"])

            output_lines = [
                f"⛽️ 查詢到您附近 {range_km} 公里內共有 {len(result)} 個加油站："
            ]

            for i, station in enumerate(result[:5]):
                map_link = create_google_maps_url(
                    station["lat"], station["lng"], station["address"]
                )
                navigation_link = f"[使用 Google Maps 導航]({map_link})"

                output_lines.append(f"\n- - - - - - - - - - - - - - - - - -")
                output_lines.append(f"No.{i+1}：{station['type']} - {station['name']}")
                if (
                    station["open_time"] == "00:00-24:00"
                    or station["open_time"] == "00:00-23:59"
                    or station["open_time"] == "24小時"
                ):
                    output_lines.append(f"營業時間：24小時")
                else:
                    output_lines.append(f"營業時間：{station['open_time']}")
                output_lines.append(f"距離: {station['distance_km']} 公里")
                output_lines.append(f"油品: {station['gas_types']}")
                output_lines.append(f"📍 導航: {navigation_link}")

            if len(result) > 5:
                output_lines.append(f"\n... 尚有 {len(result) - 5} 個未顯示。")

            return "\n".join(output_lines)

        else:
            return f"很抱歉，在您附近 {range_km} 公里內找不到任何加油站。請嘗試移動位置或擴大搜尋範圍。"

    except Exception as e:
        print(f"Gas station query error: {e}")
        return "查詢加油站時發生錯誤，請稍後再試。"


def format_gas_types(station):
    """將加油站資料庫中的 0/1 欄位轉換成油品販售清單文字"""
    gas_list = []

    gas_map = {
        "gas_98": "98 無鉛",
        "gas_95": "95 無鉛",
        "gas_92": "92 無鉛",
        "gas_diesel": "柴油",
        "gas_ss": "自助加油",
    }

    for key, name in gas_map.items():
        if station.get(key) in (1, "1", True):
            gas_list.append(name)

    if not gas_list:
        return "N/A (無油品資訊)"

    return " | ".join(gas_list)


def create_google_maps_url(lat, lng, station_name):
    """生成導航至指定經緯度的 Google Maps 連結"""
    return f"https://www.google.com/maps/dir/?api=1&destination={station_name}&travelmode=driving"


# -------------------------
# 即時油價
# -------------------------
def get_gas_price():
    try:
        price_data = query_table("gas_price")

        if not price_data:
            return "很抱歉，目前資料庫中沒有油價資訊。"

        formatted_prices = {}
        update_date = None

        for p in price_data:
            company_type = p.get("brand", "Unknown")
            if update_date is None and "reptile_time" in p:
                update_date = p["reptile_time"]

            price_list = []

            if p.get("gas_98") is not None:
                price_list.append(f"98 無鉛: {p['gas_98']} 元")
            if p.get("gas_95") is not None:
                price_list.append(f"95 無鉛: {p['gas_95']} 元")
            if p.get("gas_92") is not None:
                price_list.append(f"92 無鉛: {p['gas_92']} 元")
            if p.get("gas_diesel") is not None:
                price_list.append(f"柴油: {p['gas_diesel']} 元")

            formatted_prices[company_type] = "\n".join(price_list)

        message_lines = ["⛽️ 台灣即時油價查詢 ⛽️", "--------------------------"]

        if update_date:
            message_lines.append(f"📅 更新日期: {update_date}")
            message_lines.append("--------------------------")

        if "cpc" in formatted_prices:
            message_lines.append("【中油】:")
            message_lines.append(formatted_prices["cpc"])
            message_lines.append("")

        if "fpcc" in formatted_prices:
            message_lines.append("【台塑】:")
            message_lines.append(formatted_prices["fpcc"])
            message_lines.append("")

        final_text = "\n".join(message_lines)
        return TextSendMessage(text=final_text)
    except Exception as e:
        print(f"Gas price query error: {e}")
        return "查詢油價時發生錯誤，請稍後再試。"
