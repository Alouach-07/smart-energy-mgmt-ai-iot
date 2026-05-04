import time
import json
from datetime import datetime, timezone
import pandas as pd
from influxdb_client_3 import InfluxDBClient3
import paho.mqtt.client as mqtt

# ────────────────────────────────────────────────
# CONFIGURATION
# ────────────────────────────────────────────────
INFLUX_URL = "http://localhost:8181"
INFLUX_TOKEN = "apiv3_sIO1ekgndQdnqY5epVf2JERj1AG2E_HfPw_a3l0erRcKM7S8FkiJfT7UwzY1EQMij6-VSNchBsHuhYhx-d-5ig"
DB_NAME = "house_monitor"
MEASUREMENT = "smart_home"

# MQTT
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "home/control/command"

# Default threshold (will be updated dynamically)
CURRENT_DYNAMIC_THRESHOLD = 6000 

client_influx = InfluxDBClient3(host=INFLUX_URL, token=INFLUX_TOKEN)
client_mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

# ────────────────────────────────────────────────
# SEASONAL LOGIC (From power_alert.py)
# ────────────────────────────────────────────────
def get_season_factor():
    """Determines the multiplier coefficient based on the current month"""
    month = datetime.now().month
    # WINTER: Dec, Jan, Feb -> Higher threshold (x1.5)
    if month in [12, 1, 2]:
        return 1.5
    # MID-SEASON: Mar, Apr, May, Sep, Oct, Nov -> Medium threshold (x1.2)
    elif month in [3, 4, 5, 9, 10, 11]:
        return 1.2
    # SUMMER: Jun, Jul, Aug -> Standard threshold (x1.0)
    else:
        return 1.0

# ────────────────────────────────────────────────
# DYNAMIC THRESHOLD CALCULATION
# ────────────────────────────────────────────────
def update_dynamic_threshold():
    """
    Calculates the dynamic spike threshold based on history and season.
    Logic extracted from your peaks_generate.py and power_alert.py scripts.
    """
    global CURRENT_DYNAMIC_THRESHOLD
    
    # 1. Load historical data (last 24h is enough for demo,
    # but 168h (7d) is ideal if you have enough data)
    query = f"""
    SELECT global_power_watts
    FROM {MEASUREMENT}
    WHERE time >= now() - interval '24 hours'
    ORDER BY time ASC
    """
    
    try:
        table = client_influx.query(query=query, database=DB_NAME)
        df = table.to_pandas()
        
        if df.empty:
            print(" Not enough data to calculate dynamic threshold. Using default.")
            return

        # 2. Statistical analysis of spikes
        mean_y = df['global_power_watts'].mean()
        std_y = df['global_power_watts'].std()
        
        # Identify abnormal values in history (existing spikes)
        # A spike is defined as > Mean + 3 standard deviations
        spike_limit = mean_y + 3 * std_y
        historical_spikes = df[df['global_power_watts'] > spike_limit]['global_power_watts']
        
        # Calculate spike base (average of excess or default value)
        # Subtract mean_y to get the order of magnitude of the "surplus"
        if not historical_spikes.empty:
            base_spike_value = (historical_spikes.mean() - mean_y)
        else:
            # Default value if no historical spike detected (e.g., 8000W)
            base_spike_value = 8000
            
        # 3. Apply seasonal factor
        factor = get_season_factor()
        dynamic_threshold = base_spike_value * factor
        
        # Update global variable
        CURRENT_DYNAMIC_THRESHOLD = dynamic_threshold
        
        print(f" Dynamic Threshold Updated: {CURRENT_DYNAMIC_THRESHOLD:.0f}W (Season x{factor})")

    except Exception as e:
        print(f"Error calculating dynamic threshold: {e}")

# ────────────────────────────────────────────────
# MQTT
# ────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    print(f"Connected to MQTT Broker with code {reason_code}")

client_mqtt.on_connect = on_connect

def start_mqtt():
    try:
        client_mqtt.connect(MQTT_BROKER, MQTT_PORT, 60)
        client_mqtt.loop_start()
    except Exception as e:
        print(f"MQTT Error: {e}")

def send_shutdown_command(room_key):
    payload = {
        "room": room_key,
        "action": "power_off",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    print(f" ANOMALY IDENTIFIED: Source = {room_key}")
    print(f"   -> Sending MQTT command: {payload}")
    
    try:
        info = client_mqtt.publish(MQTT_TOPIC, json.dumps(payload), qos=1)
        info.wait_for_publish()
    except Exception as e:
        print(f"Error sending: {e}")

# ────────────────────────────────────────────────
# REAL-TIME ANALYSIS
# ────────────────────────────────────────────────
def analyze_system():
    # Retrieve the latest data
    query = f"""
    SELECT *
    FROM {MEASUREMENT}
    ORDER BY time DESC
    LIMIT 1
    """
    
    try:
        table = client_influx.query(query=query, database=DB_NAME)
        df = table.to_pandas()
        if df.empty: return
        
        data = df.iloc[0].to_dict()
        global_power = data.get('global_power_watts', 0)
        
        # 1. Sensor Error Detection
        if global_power < 0:
            print(" Anomaly: Sensor Error (Negative)")
            return

        # 2. Blackout Detection
        if global_power == 0:
            print(" Anomaly: Blackout detected")
            return

        # 3. High Spike Detection (Using global dynamic threshold)
        if global_power > CURRENT_DYNAMIC_THRESHOLD:
            print(f" Anomaly: Global Spike ({global_power:.0f}W) > Dynamic Threshold ({CURRENT_DYNAMIC_THRESHOLD:.0f}W)")
            
            # --- SOURCE IDENTIFICATION ---
            rooms_map = {
                "living_total_power": "living",
                "kitchen_total_power": "kitchen",
                "bed1_total_power": "bed1",
                "bed2_total_power": "bed2",
                "bath_total_power": "bath",
                "guest_total_power": "guest"
            }
            
            max_power = 0
            culprit_room_key = None
            
            for field_name, room_key in rooms_map.items():
                power = data.get(field_name, 0)
                if isinstance(power, (int, float)) and power > max_power:
                    max_power = power
                    culprit_room_key = room_key
            
            if culprit_room_key:
                send_shutdown_command(culprit_room_key)
            else:
                print("Source not identified")

    except Exception as e:
        print(f"Analysis Error: {e}")

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Starting Feedback Control Module (Dynamic Threshold) ===")
    start_mqtt()
    
    # Initial threshold calculation
    update_dynamic_threshold()
    
    loop_counter = 0
    
    while True:
        # Every 5 seconds: Analyze system
        analyze_system()
        
        # Every 5 minutes (60 * 5s): Update dynamic threshold
        # To adapt to consumption or seasonal changes
        loop_counter += 1
        if loop_counter >= 60:
            update_dynamic_threshold()
            loop_counter = 0
            
        time.sleep(5)
