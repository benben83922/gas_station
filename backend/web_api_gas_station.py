import re
from flask import Flask, json, request, jsonify, abort
from flask_cors import CORS
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
import pandas as pd
import math
import requests
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
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}})  # 自動處理 CORS

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
@app.route("/gas/all", methods=["GET"])
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
@app.route("/gas/nearby", methods=["POST"])
def get_nearby_gas_stations():
    data = request.get_json()
    if not data:
        return jsonify({"error": "請提供 JSON"}), 400
    print(data)

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

    print("fuels：", fuels, "brand：", brand, "open_type：", open_type)

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


# 載入所有加油站資料
def load_gas_stations():
    """載入並合併中油和台塑加油站資料。"""
    stations_1, stations_2 = fetch_all_stations()

    all_stations = []

    for i in stations_1:
        all_stations.append(
            {
                "station_name": i["station_name"],
                "address": i["address"],
                "lng": i["longitude"],
                "lat": i["latitude"],
                "provider": "CPC",
            }
        )
    for i in stations_2:
        all_stations.append(
            {
                "station_name": i["station_name"],
                "address": i["address"],
                "lng": i["longitude"],
                "lat": i["latitude"],
                "provider": "FPCC",
            }
        )
    df = pd.DataFrame(all_stations)

    return df


# Line Bot 的 Webhook 接收路徑
@app.route("/callback", methods=["GET", "POST"])
def callback():
    if request.method == "GET":
        # 這是 Line 在 Developers Console 上點擊 "Verify" 時發送的請求
        # 只需要簡單回傳 'OK'，代表連線成功
        return "OK"

    elif request.method == "POST":
        # 這是使用者發送訊息時，Line 平台發送的實際事件請求
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
    # 注意：這裡的函數名稱應與你註冊到 handler 的一致，我改回 handle_text_message
    user_message = event.message.text

    if user_message == "附近加油站":
        # --- 修正點 1: 確保只呼叫一次 reply_message ---
        # 建立 Location Action 按鈕
        location_button = QuickReplyButton(action=LocationAction(label="分享目前位置"))

        # 建立 Quick Reply 容器
        quick_reply = QuickReply(items=[location_button])

        # 發送訊息：要求用戶點擊按鈕分享位置
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="好的，請點擊下方「分享目前位置」按鈕，我才能為您查詢 1 公里內的附近加油站。",
                quick_reply=quick_reply,  # 將 Quick Reply 附加到訊息上
            ),
        )
    elif user_message == "即時油價":
        response = get_gas_price()
        # 回覆一般文字
        if isinstance(response, str):
            reply_message = TextSendMessage(text=response)
        else:
            # 如果是 TextSendMessage 物件 (成功獲取的油價)，則直接使用
            reply_message = response

        # 3. 使用正確的 reply_message 變數來回覆用戶
        # --- 關鍵修正點：使用 reply_message 變數 ---
        line_bot_api.reply_message(event.reply_token, reply_message)


# 處理位置事件
@handler.add(MessageEvent, message=LocationMessage)
def handle_location_message(event):
    # 從 event.message 中取出經緯度等資訊
    latitude = event.message.latitude
    longitude = event.message.longitude
    title = event.message.title
    # address = event.message.address # 這個通常是選填，我們先用 Lat/Lon 查詢

    # 呼叫查詢函數，並直接取得 LINE 格式的字串結果
    result_text = get_nearby_gas_stations_for_line_bot(latitude, longitude)

    # --- 修正點 2: 使用查詢結果回覆用戶 ---
    # 因為這個處理過程可能較久，我們假設 get_nearby_gas_stations_for_line_bot 已經在 30 秒內完成
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result_text))


# --- 修正點 2: 將回傳值改成 LINE Bot 可用的文字訊息 ---
def get_nearby_gas_stations_for_line_bot(user_lat, user_lng):
    range_km = float(1.0)  # 預設 1 公里
    result = []

    try:
        stations_fpcc, stations_cpc = fetch_all_stations()
        result += filter_by_distance(stations_fpcc, "台塑", user_lat, user_lng, range_km, include_gas_types=True)
        result += filter_by_distance(stations_cpc, "中油", user_lat, user_lng, range_km, include_gas_types=True)

        # 依距離排序
        if result:
            result.sort(key=lambda x: x["distance_km"])

            # ---------------------------
            # --- 最終輸出格式化 (重點) ---
            # ---------------------------
            output_lines = [
                f"⛽️ 查詢到您附近 {range_km} 公里內共有 {len(result)} 個加油站："
            ]

            # 只顯示最接近的 5 個，避免 Line 訊息過長
            for i, station in enumerate(result[:5]):

                # 建立導航連結
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
                output_lines.append(f"油品: {station['gas_types']}")  # 輸出油品資訊
                output_lines.append(f"📍 導航: {navigation_link}")  # 輸出導航連結

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

    # 油品對應的中文名稱
    gas_map = {
        "gas_98": "98 無鉛",
        "gas_95": "95 無鉛",
        "gas_92": "92 無鉛",
        "gas_diesel": "柴油",
        "gas_ss": "自助加油",
    }

    # 檢查每個油品欄位
    for key, name in gas_map.items():
        # 假設資料庫回傳的 s[key] 是 1 或 0 (int 或 str)
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

        # 檢查是否查到資料
        if not price_data:
            return "很抱歉，目前資料庫中沒有油價資訊。"

        # ---------------------------
        # 數據處理與格式化
        # ---------------------------

        # 創建一個字典來存儲不同油品公司的價格
        formatted_prices = {}
        update_date = None

        for p in price_data:
            company_type = p.get("brand", "Unknown")  # 假設有 'type' 欄位
            print(company_type)
            # 將最新更新日期記錄下來 (假設所有記錄的日期都一樣，取第一個即可)
            if update_date is None and "reptile_time" in p:
                update_date = p["reptile_time"]

            # 建立該公司的價格清單
            price_list = []

            # 使用 .get() 來安全取值，並將價格格式化為字串
            if p.get("gas_98") is not None:
                price_list.append(f"98 無鉛: {p['gas_98']} 元")
            if p.get("gas_95") is not None:
                price_list.append(f"95 無鉛: {p['gas_95']} 元")
            if p.get("gas_92") is not None:
                price_list.append(f"92 無鉛: {p['gas_92']} 元")
            if p.get("gas_diesel") is not None:
                price_list.append(f"柴油: {p['gas_diesel']} 元")

            formatted_prices[company_type] = "\n".join(price_list)

        # ---------------------------
        # 組織最終回覆訊息
        # ---------------------------

        # 標題行
        message_lines = ["⛽️ 台灣即時油價查詢 ⛽️", "--------------------------"]

        if update_date:
            message_lines.append(f"📅 更新日期: {update_date}")
            message_lines.append("--------------------------")

        # 加入中油價格
        if "cpc" in formatted_prices:
            message_lines.append("【中油】:")
            message_lines.append(formatted_prices["cpc"])
            message_lines.append("")

        # 加入台塑價格
        if "fpcc" in formatted_prices:
            message_lines.append("【台塑】:")
            message_lines.append(formatted_prices["fpcc"])
            message_lines.append("")

        # 組裝成一個大字串
        final_text = "\n".join(message_lines)

        # 回傳 LINE SDK 的訊息物件
        return TextSendMessage(text=final_text)
    except Exception as e:
        print(f"Gas price query error: {e}")
        return "查詢油價時發生錯誤，請稍後再試。"


# -------------------------
# API 根目錄測試
# -------------------------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"msg": "加油站 API 運行中"})


# -------------------------
# 主程式
# -------------------------
if __name__ == "__main__":
    # 開發測試用（正式環境由 gunicorn 啟動，不會進入此區塊）
    app.run(
        host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG", "false").lower() == "true"
    )
