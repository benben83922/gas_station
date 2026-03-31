from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time
import os
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# === 設定 Chrome options ===
options = Options()
options.add_experimental_option("excludeSwitches", ["enable-automation"])
options.add_experimental_option("useAutomationExtension", False)
options.add_argument("--headless=new")
options.add_argument("--window-size=1920,1080")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")

# === 反偵測設定 ===
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
options.add_argument(f"user-agent={user_agent}")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument("--lang=zh-TW")

# === 啟動瀏覽器 ===
driver = webdriver.Chrome(
    service=Service(ChromeDriverManager().install()), options=options
)
driver.execute_cdp_cmd(
    "Page.addScriptToEvaluateOnNewDocument",
    {
        "source": """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-TW', 'zh', 'en-US', 'en'] });
    window.chrome = { runtime: {} };
  """
    },
)
driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
    "width": 1920, "height": 1080,
    "deviceScaleFactor": 1, "mobile": False
})
driver.get("https://www.fpcc.com.tw/tw/events/stations")
time.sleep(5)
country_option = driver.find_element(By.NAME, "scity")
options = country_option.find_elements(By.TAG_NAME, "option")
data = []
for opt in options:
    city_data = []
    city = opt.text
    url = f"https://www.fpcc.com.tw/tw/events/stations/{city}/0/0/0"
    # 開新分頁
    driver.execute_script(f"window.open('{url}');")
    time.sleep(3)
    # 切換到新分頁
    driver.switch_to.window(driver.window_handles[1])
    no_data = driver.find_element(By.XPATH, "/html/body/div[1]/div[3]/div[3]/ul/li/p")
    no_data_text = no_data.text
    print(
        "------------------------------------------------------------------------------------"
    )
    if no_data_text == "您所搜尋的項目，沒有找到適合的結果。":
        print(city, "沒有資料")
        driver.close()
        driver.switch_to.window(driver.window_handles[0])
    else:
        table = driver.find_element(By.CLASS_NAME, "reload-layout")
        table_row = table.find_elements(By.CSS_SELECTOR, "li")
        print(city, "有", len(table_row), "筆資料")
        for ele in table_row:
            detail = ele.find_elements(By.CSS_SELECTOR, ("div"))
            data_detail = []
            for item in detail:
                title = item.get_attribute("data-title")
                if title in ["站名", "地址", "電話", "營業時間"]:
                    data_detail.append(item.text)
                elif title in [
                    "92無鉛汽油",
                    "95+無鉛汽油",
                    "98無鉛汽油",
                    "超級柴油",
                    "自助加油設備",
                ]:
                    children = item.find_elements(
                        By.XPATH, "./*"
                    )  # 這裡 "./*" 表示所有直接子節點
                    if len(children) > 0:
                        data_detail.append(True)
                        # print("有子節點")
                    else:
                        data_detail.append(False)
                        # print("沒有子節點")
                else:
                    pass
            city_data.append(data_detail)
            # print("data_detail：", data_detail)
        city_data.pop(0)
        # print("city_data：", city_data)
        data.append(city_data)
        driver.close()
        driver.switch_to.window(driver.window_handles[0])
        # break
driver.quit()
final_data = []
for item in data:
    for element in item:
        final_data.append(element)

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

insert_data = []
for detail_data in final_data:
    insert_data.append(
        {
            "station_name": detail_data[0],
            "address": detail_data[1],
            "phone": detail_data[2],
            "open_time": detail_data[3],
            "gas_92": detail_data[4],
            "gas_95": detail_data[5],
            "gas_98": detail_data[6],
            "gas_diesel": detail_data[7],
            "gas_ss": detail_data[8],
        }
    )

# === 從資料庫撈出現有資料 ===
existing = supabase.table("fpcc_gas_station").select("*").execute()
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
        lat, lng = geocode(item["address"])
        item["latitude"] = lat
        item["longitude"] = lng
        print(f"  [新增] {item['station_name']} → {lat}, {lng}")
    supabase.table("fpcc_gas_station").insert(to_insert).execute()
    print(f"新增 {len(to_insert)} 筆")

# === 更新：地址有異動才重新查座標 ===
for item in to_update:
    db_row = db_map[item["station_name"]]
    address_changed = item.get("address") != db_row.get("address")
    if address_changed:
        lat, lng = geocode(item["address"])
        item["latitude"] = lat
        item["longitude"] = lng
        print(f"  [更新+座標] {item['station_name']} → {lat}, {lng}")
    else:
        print(f"  [更新] {item['station_name']}（地址未變，保留原座標）")
    supabase.table("fpcc_gas_station").update(item).eq("station_name", item["station_name"]).execute()
if to_update:
    print(f"更新 {len(to_update)} 筆")

for name in to_delete:
    supabase.table("fpcc_gas_station").delete().eq("station_name", name).execute()
if to_delete:
    print(f"刪除 {len(to_delete)} 筆")
if not to_insert and not to_update and not to_delete:
    print("資料無異動")
