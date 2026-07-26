import network
import time
import urequests
import math
import machine
from machine import ADC, Pin
import ntptime

# ─── 時間ズレ補正用：目覚めた瞬間のタイムスタンプを最優先で記録 ───
start_ticks = time.ticks_ms()

# --- 設定 ---
WIFI_SSID = "aterm-788953-g"
WIFI_PASSWORD = "9e69c0bce599a"
GAS_URL = "https://script.google.com/macros/s/AKfycbwfDrp8OVcvpt4Y-qmNejOhGeIR4U9aBFZPunoJQNnMV1LIQcbuUxby9De_M6sW7z5B/exec"

VAL_DRY = 240
VAL_WET = 33400
B_CONSTANT = 3950
RESISTOR_BASE = 10000
TEMP_BASE = 298.15

# 🚨 【超重要】アクティブ・ロー対応：起動した瞬間に即 HIGH(1=OFF) 出力
pump_relay = Pin(16, Pin.OUT, value=1) 

sensor_vcc = Pin(15, Pin.OUT, value=0)
temp_vcc = Pin(14, Pin.OUT, value=0)
soil_sensor = ADC(Pin(28))
temp_sensor = ADC(Pin(27))
bat_sensor = ADC(Pin(26))  # 5Vラインまたは分圧バッテリー接続ピン

def get_battery_voltage():
    avg_raw = sum([bat_sensor.read_u16() for _ in range(5)]) / 5
    return avg_raw * 3.3 / 65535 * 3.11

def get_moisture_data():
    sensor_vcc.value(1)
    time.sleep(0.02)
    raw_val = sum([soil_sensor.read_u16() for _ in range(8)]) / 8
    sensor_vcc.value(0)
    
    if raw_val < VAL_DRY: percent = 0.0
    elif raw_val > VAL_WET: percent = 100.0
    else: percent = (raw_val - VAL_DRY) * 100 / (VAL_WET - VAL_DRY)
    return int(raw_val), max(0.0, min(100.0, percent))

def get_temperature():
    temp_vcc.value(1)
    time.sleep(0.01)
    raw_val = sum([temp_sensor.read_u16() for _ in range(8)]) / 8
    temp_vcc.value(0)
    
    if raw_val <= 500: return 0.0
    try:
        resistance = RESISTOR_BASE * (65535 / raw_val - 1)
        steinhart = math.log(resistance / RESISTOR_BASE) / B_CONSTANT + (1.0 / TEMP_BASE)
        return (1.0 / steinhart) - 273.15 - 19.5
    except:
        return 0.0

def connect_wifi_with_retry(wlan, retries=3):
    try:
        wlan.config(pm=0xa24c1003)
    except:
        pass
        
    for attempt in range(1, retries + 1):
        if wlan.isconnected():
            return True
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        
        timeout = 10
        while not wlan.isconnected() and timeout > 0:
            time.sleep(0.5)
            timeout -= 0.5
            
        if wlan.isconnected():
            return True
        time.sleep(1)
    return False

def sync_time_and_get_sleep_ms(wlan):
    """
    正確に1時間毎（毎時00分05秒狙い）に起動するようにスリープ時間を逆算する関数
    """
    default_sleep_ms = 3600 * 1000
    now_year = time.localtime()[0]
    
    if now_year < 2020:
        try:
            print("NTP同期を実行します...")
            ntptime.host = "ntp.nict.jp"
            ntptime.settime()
        except Exception as e:
            print("NTP同期エラー:", e)
            return default_sleep_ms

    local_time = time.localtime(time.time() + 9 * 3600)
    hour, minute, second = local_time[3], local_time[4], local_time[5]
    print("現在時刻(JST): {:02d}:{:02d}:{:02d}".format(hour, minute, second))
    
    remaining_seconds = ((59 - minute) * 60) + (60 - second) + 5
    return remaining_seconds * 1000

# --- メイン処理 ---

# 1. センサー測定
raw_m, m_pc = get_moisture_data()
t_c = get_temperature()
v_v = get_battery_voltage()

# 2. Wi-Fiセットアップ
wlan = network.WLAN(network.STA_IF)
wlan.active(True)

sleep_time_ms = 3600 * 1000  # デフォルトスリープ値（1時間）

# 🚨 リレーは処理開始時点で確実にOFF(HIGH)を再宣言
pump_relay.value(1)

try:
    if connect_wifi_with_retry(wlan, retries=3):
        # 時刻同期とスリープ時間計算
        sleep_time_ms = sync_time_and_get_sleep_ms(wlan)
        
        local_time = time.localtime(time.time() + 9 * 3600)
        current_hour = local_time[3]
        
        # ─── ポンプ制御 ───
        # ※ 5時00分 〜 16時59分 までを散水対象（17時になったらスキップ）とする場合は '< 17' にします
        # 17時台も動かしたい場合は '<= 17' のままにしてください。ここでは17時に止まるよう '< 17' としています。
        if 5 <= current_hour < 17:
            print("【ポンプ動作】現在{}時です。10秒間散水します。".format(current_hour))
            try:
                pump_relay.value(0)   # LOW(0) でリレーON
                time.sleep(10)        # 10秒間散水
            finally:
                pump_relay.value(1)   # 何があっても絶対に HIGH(1) でリレーOFF
                print("【ポンプ停止】散水が完了しました。")
        else:
            print("【ポンプ待機】現在{}時です（時間外のため散水スキップ）。".format(current_hour))
            pump_relay.value(1)
        
        # ─── データ送信 ───
        print("データ送信を開始します...")
        request_url = "{}?raw={}&moisture={:.1f}&temp={:.1f}&vsys={:.2f}".format(
            GAS_URL, raw_m, m_pc, t_c, v_v
        )
        try:
            response = urequests.get(request_url, timeout=7)
            response.close()
            print("データ送信成功")
        except Exception as e:
            print("データ送信失敗:", e)
    else:
        print("Wi-Fi接続失敗: スキップしてスリープへ移行します。")

finally:
    # 🚨【絶対防御】Wi-Fi接続の成功・失敗にかかわらず、スリープ直前に必ずリレーをOFF(1)にする
    pump_relay.value(1)

# 3. Wi-Fiチップ停止
try:
    if wlan.isconnected():
        wlan.disconnect()
    time.sleep(0.5)
    wlan.active(False)
except:
    pass

# 給電ピン・リレーピンの最終確認
sensor_vcc.value(0)
temp_vcc.value(0)
pump_relay.value(1)  # スリープ中もリレーOFF(HIGH)を維持

# 4. ─── 時間ズレ補正 ───
end_ticks = time.ticks_ms()
processing_time_ms = time.ticks_diff(end_ticks, start_ticks)

final_sleep_ms = sleep_time_ms - processing_time_ms

if final_sleep_ms < 5000:
    final_sleep_ms = 5000

print("今回の実処理時間: {} ms (約{:.2f}秒)".format(processing_time_ms, processing_time_ms / 1000))
print("補正後の純粋なスリープ時間: {} ms".format(final_sleep_ms))
print("ディープスリープに入ります。")

# 正確に補正されたミリ秒数でスリープ
machine.deepsleep(int(final_sleep_ms))