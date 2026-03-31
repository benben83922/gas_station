from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import re
import time
import os
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# === 設定 Chrome options ===
options = Options()
options.add_argument("--headless=new")  # 不開視窗（可省略）
options.add_argument("--disable-gpu")
options.add_argument("--no-sandbox")

# === 啟動瀏覽器 ===
driver = webdriver.Chrome(
    service=Service(ChromeDriverManager().install()), options=options
)
driver.get("https://vipmbr.cpc.com.tw/mbwebs/service_search.aspx")
search_button = driver.find_element(By.ID, "btnQuery")
search_button.click()
time.sleep(5)
table_1 = driver.find_element(By.ID, "MyGridView1")
table_2 = driver.find_element(By.ID, "MyGridView2")
table_1_detail = table_1.find_elements(By.CSS_SELECTOR, "tr")
table_2_detail = table_2.find_elements(By.CSS_SELECTOR, "tr")
data_1 = []
data_2 = []
for i in table_1_detail:
    row_element_1 = []
    station_element_1 = i.find_elements(By.CSS_SELECTOR, "td")
    for j in station_element_1:
        row_element_1.append(j.text)
        # print(j.text)
    data_1.append(row_element_1)
for k in table_2_detail:
    row_element_2 = []
    station_element_2 = k.find_elements(By.CSS_SELECTOR, "td")
    for l in station_element_2:
        row_element_2.append(l.text)
        # print(l.text)
    data_2.append(row_element_2)
driver.quit()

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")


# === Google Geocoding API ===
def geocode(address):
    """透過 Google Geocoding API 將地址轉為經緯度，失敗回傳 (None, None)。"""
    if not GOOGLE_MAPS_API_KEY:
        print(f"  [跳過] 未設定 GOOGLE_MAPS_API_KEY")
        return None, None
    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={address}&key={GOOGLE_MAPS_API_KEY}"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data["status"] == "OK":
            loc = data["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
        else:
            print(f"  [Geocode] {address} → {data['status']}")
    except Exception as e:
        print(f"  [Geocode 錯誤] {address} → {e}")
    return None, None


def build_cpc_full_address(item):
    """組合中油完整地址（去括號 + 縣市區域）。"""
    address = item.get("address", "")
    if "(" in address or "（" in address:
        address = re.sub(r"[\(（][^）\)]*[\)）]", "", address)
    return item.get("country", "") + item.get("district", "") + address

"""
data_1
index7 98
index8 95
index9 92
index12 柴油
index13 汽油自助
index14 柴油自助
index15 廁所
index16 無障礙廁所

data_2
index7 98
index8 95
index9 92
index11 柴油
index12 汽油自助
index13 柴油自助
index14 廁所
index15 無障礙廁所
"""
data_1.pop(0)
data_2.pop(0)

insert_data = []

for item_1 in data_1:
    print(item_1)
    insert_data.append(
        {
            "country": item_1[0],
            "district": item_1[1],
            "station_type": item_1[2],
            "station_name": item_1[3].split("\n")[0],
            "address": item_1[4],
            "phone": item_1[5],
            "open_time": item_1[6],
            "gas_98": item_1[7] == "●",
            "gas_95": item_1[8] == "●",
            "gas_92": item_1[9] == "●",
            "gas_diesel": item_1[12] == "●",
            "gas_ss": item_1[13] == "●",
            "gas_diesel_ss": item_1[14] == "●",
            "toilet": item_1[15] == "●",
            "accessible_toilet": item_1[16] == "●",
        }
    )

for item_2 in data_2:
    insert_data.append(
        {
            "country": item_2[0],
            "district": item_2[1],
            "station_type": item_2[2],
            "station_name": item_2[3].split("\n")[0],
            "address": item_2[4],
            "phone": item_2[5],
            "open_time": item_2[6],
            "gas_98": item_2[7] == "●",
            "gas_95": item_2[8] == "●",
            "gas_92": item_2[9] == "●",
            "gas_diesel": item_2[11] == "●",
            "gas_ss": item_2[12] == "●",
            "gas_diesel_ss": item_2[13] == "●",
            "toilet": item_2[14] == "●",
            "accessible_toilet": item_2[15] == "●",
        }
    )

# === 從資料庫撈出現有資料 ===
existing = supabase.table("cpc_gas_station").select("*").execute()
db_map = {row["station_name"]: row for row in existing.data}

# === 比對爬取資料與資料庫 ===
to_insert = []
to_update = []
scraped_names = set()

for item in insert_data:
    scraped_names.add(item["station_name"])
    if item["station_name"] not in db_map:
        to_insert.append(item)
    else:
        db_row = db_map[item["station_name"]]
        changed = any(item[k] != db_row.get(k) for k in item)
        if changed:
            to_update.append(item)

# === 找出要刪除的（資料庫有，爬蟲沒有）===
to_delete = [name for name in db_map if name not in scraped_names]

# === 新增：查詢經緯度後寫入 ===
if to_insert:
    for item in to_insert:
        full_address = build_cpc_full_address(item)
        lat, lng = geocode(full_address)
        item["latitude"] = lat
        item["longitude"] = lng
        print(f"  [新增] {item['station_name']} → {lat}, {lng}")
    supabase.table("cpc_gas_station").insert(to_insert).execute()
    print(f"新增 {len(to_insert)} 筆")

# === 更新：地址有異動才重新查座標 ===
for item in to_update:
    db_row = db_map[item["station_name"]]
    address_changed = (
        item.get("country") != db_row.get("country")
        or item.get("district") != db_row.get("district")
        or item.get("address") != db_row.get("address")
    )
    if address_changed:
        full_address = build_cpc_full_address(item)
        lat, lng = geocode(full_address)
        item["latitude"] = lat
        item["longitude"] = lng
        print(f"  [更新+座標] {item['station_name']} → {lat}, {lng}")
    else:
        print(f"  [更新] {item['station_name']}（地址未變，保留原座標）")
    supabase.table("cpc_gas_station").update(item).eq("station_name", item["station_name"]).execute()
if to_update:
    print(f"更新 {len(to_update)} 筆")

for name in to_delete:
    supabase.table("cpc_gas_station").delete().eq("station_name", name).execute()
if to_delete:
    print(f"刪除 {len(to_delete)} 筆")
if not to_insert and not to_update and not to_delete:
    print("資料無異動")
